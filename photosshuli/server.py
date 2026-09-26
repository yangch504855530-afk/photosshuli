# -*- coding: utf-8 -*-
"""photosshuli 本地 Web 服务(默认只读,任何删除都是移入回收站目录)
v0.1.2:增量/多线程扫描编排、缩略图自动预热、目录浏览、执行后索引失效引导"""
import hashlib
import os
import time
import threading
import urllib.parse
import http.server
import socketserver
import webbrowser

from . import config as C
from . import scanner, duplicates, similar, classify, recycle, util
from .store import Store


class App:
    def __init__(self, home=None):
        self.store = Store(home)
        self.settings = self.store.load_settings()
        self.files = []
        self.roots = []
        self.scan_status = {"running": False, "done": 0, "total": 0, "files": 0,
                            "reused": 0, "fresh": 0, "error": "",
                            "thumbs_done": 0, "thumbs_total": 0, "stale": False}
        self._lock = threading.Lock()
        self._preheat_pause = threading.Event()   # set=继续, clear=暂停取新任务
        self._preheat_busy = False
        self._preheat_thread = None
        self._preheat_pause.set()

    # ---------- scan ----------
    def run_scan(self, roots=None, deep_video=None, force_full=False):
        roots = util.parse_roots(roots if roots is not None else self.settings.get("roots"))
        if roots is not None:
            self.settings["roots"] = roots
            self.store.save_settings(self.settings)
        deep_video = self.settings.get("deep_video", True) if deep_video is None else deep_video
        roots = [r for r in roots if os.path.isdir(r)]
        if not roots:
            self.scan_status.update(running=False, error="目录不存在:请检查路径(注意去掉引号,使用绝对路径)")
            return
        self.scan_status.update(running=True, done=0, total=0, files=0,
                                reused=0, fresh=0, error="", stale=False)

        def prog(done, total):
            self.scan_status.update(done=done, total=total)

        def preheat():
            need = [f["path"] for f in self.files
                    if f.get("kind") in ("photo", "video", "livp")]
            self.scan_status["thumbs_total"] = len(need)
            self.scan_status["thumbs_done"] = 0
            for p in need:
                self._preheat_pause.wait()
                self._preheat_busy = True
                try:
                    tp = self.thumb_path(p)
                    if not os.path.exists(tp):
                        os.makedirs(os.path.dirname(tp), exist_ok=True)
                        try:
                            util.make_thumb(p, tp)
                        except Exception:
                            pass
                finally:
                    with self._lock:
                        self.scan_status["thumbs_done"] += 1
                        self._preheat_busy = False

        def work():
            try:
                files, info = scanner.scan_roots(
                    roots, deep_video=deep_video, progress=prog,
                    previous_files=None if force_full else self.files)
                with self._lock:
                    self.files = files
                    self.roots = roots
                    self.store.save_index(files, roots)
                    self.scan_status.update(running=False, files=len(files),
                                            reused=info["reused"], fresh=info["fresh"])
                if files and self.settings.get("auto_preheat", True):
                    self._preheat_thread = threading.Thread(target=preheat, daemon=True)
                    self._preheat_thread.start()
            except Exception as e:  # noqa: BLE001
                self.scan_status.update(running=False, error=str(e))
        threading.Thread(target=work, daemon=True).start()

    def load_or_scan(self):
        files, roots = self.store.load_index()
        if files:
            self.files, self.roots = files, roots
            self.scan_status.update(files=len(files))

    # ---------- derived views ----------
    def dup_groups(self):
        picks = self.settings.get("dup_picks", {})
        groups = duplicates.find_exact_dups(self.files)
        out = []
        for g in groups:
            gid = hashlib.md5("|".join(sorted(f["path"] for f in g)).encode()).hexdigest()[:12]
            keeper = picks.get(gid) or duplicates.pick_keeper(g, self._priorities())
            out.append({"id": gid, "keeper": keeper,
                        "members": sorted(g, key=lambda f: f["path"])})
        return out

    def similar_groups(self):
        picks = self.settings.get("sim_picks", {})
        cands = [f for f in self.files if f.get("cls") not in C.RECYCLE_SUGGESTED]
        groups = similar.group_similar(cands, self.settings.get("similar_threshold"))
        out = []
        for g in groups:
            gid = hashlib.md5("|".join(sorted(f["path"] for f in g)).encode()).hexdigest()[:12]
            rec = picks.get(gid) or similar.recommend(g)
            out.append({"id": gid, "recommended": rec, "members": g})
        return out

    def suggestions(self):
        names = self.settings.get("cluster_names", {})
        return {f["path"]: classify.suggest(f, names, f["root"]) for f in self.files}

    def clusters(self):
        return classify.build_clusters(self.files)

    def _priorities(self):
        return self.settings.get("priorities") or self.roots

    def stats(self):
        return scanner.stats_of(self.files)

    def counts(self):
        sug = self.suggestions().values()
        return {
            "dup_groups": len(self.dup_groups()),
            "sim_groups": len(self.similar_groups()),
            "recycle_suggested": sum(1 for s in sug if s[0] == "recycle"),
            "archive_suggested": sum(1 for s in sug if s[0] == "archive"),
            "decided": len(self.store.load_decisions()),
            "no_meta": self.stats()["no_meta"],
        }

    # ---------- thumbs ----------
    def thumb_path(self, path):
        h = hashlib.md5(path.encode("utf-8")).hexdigest()[:16]
        return os.path.join(self.store.dir, "thumbs", h + ".jpg")

    def guard(self, path):
        p = os.path.normpath(os.path.abspath(path))
        for r in self.roots:
            if p == os.path.normpath(os.path.abspath(r)) or p.startswith(os.path.normpath(os.path.abspath(r)) + os.sep):
                return p
        raise ValueError("路径不在扫描范围内")


class Handler(http.server.BaseHTTPRequestHandler):
    app: App = None  # 注入
    server_version = f"photosshuli/{C.VERSION}"

    def log_message(self, *a):
        pass

    def _send(self, body, code=200, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            import json
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(obj, code)

    def _body(self):
        import json
        ln = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(ln) or b"{}")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        app = self.app
        try:
            if u.path == "/":
                web = os.path.join(os.path.dirname(__file__), "..", "web", "index.html")
                with open(web, "rb") as f:
                    self._send(f.read(), ctype="text/html; charset=utf-8")
            elif u.path == "/api/data":
                self._json({
                    "files": app.files, "roots": app.roots,
                    "settings": app.settings,
                    "dupGroups": app.dup_groups(),
                    "simGroups": app.similar_groups(),
                    "suggestions": app.suggestions(),
                    "clusters": app.clusters(),
                    "decisions": app.store.load_decisions(),
                    "stats": app.stats(),
                    "counts": app.counts(),
                    "scan": app.scan_status,
                    "version": C.VERSION,
                })
            elif u.path == "/api/scanstatus":
                self._json(app.scan_status)
            elif u.path == "/api/drives":
                self._json({"drives": util.find_drives()})
            elif u.path == "/api/browse":
                p = (q.get("path") or [""])[0] or os.path.expanduser("~")
                p = os.path.normpath(os.path.abspath(p))
                if not os.path.isdir(p):
                    self._json({"error": "目录不存在"}, 404)
                    return
                dirs = []
                try:
                    for n in sorted(os.listdir(p), key=str.lower):
                        if n.startswith((".", "$")):
                            continue
                        full = os.path.join(p, n)
                        if os.path.isdir(full):
                            dirs.append(n)
                except OSError as e:
                    self._json({"error": str(e)}, 400)
                    return
                parent = os.path.dirname(p) if os.path.dirname(p) != p else None
                self._json({"path": p, "dirs": dirs, "parent": parent})
            elif u.path == "/api/plan":
                plan = recycle.build_plan(app.files, app.store.load_decisions())
                self._json({"plan": plan, "summary": recycle.plan_summary(plan)})
            elif u.path == "/api/export":
                import csv
                import io
                dec = app.store.load_decisions()
                by = {f["path"]: f for f in app.files}
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["路径", "动作", "目标", "大小", "内容类别", "拍摄时间"])
                for p, d in dec.items():
                    f = by.get(p, {})
                    w.writerow([p, d.get("action", ""), d.get("target", ""),
                                f.get("size", ""), f.get("cls", ""), f.get("dt", "")])
                out = os.path.join(app.store.dir, "decisions.csv")
                io.open(out, "w", encoding="utf-8-sig").write(buf.getvalue())
                self._json({"ok": True, "count": len(dec), "file": out})
            elif u.path == "/thumb":
                rel = (q.get("path") or [""])[0]
                tp = app.thumb_path(rel)
                if not os.path.exists(tp):
                    os.makedirs(os.path.dirname(tp), exist_ok=True)
                    util.make_thumb(app.guard(rel), tp)
                if os.path.exists(tp):
                    with open(tp, "rb") as f:
                        self._send(f.read(), ctype="image/jpeg")
                else:
                    self._json({"error": "thumb failed"}, 404)
            elif u.path == "/media":
                p = app.guard((q.get("path") or [""])[0])
                with open(p, "rb") as f:
                    self._send(f.read(8 * 1024 * 1024), ctype="application/octet-stream")
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 400)

    def do_POST(self):
        app = self.app
        try:
            body = self._body()
            if self.path == "/api/scan":
                app.run_scan(body.get("roots"), body.get("deep_video"),
                             bool(body.get("force_full")))
                self._json({"ok": True, "status": app.scan_status})
            elif self.path == "/api/settings":
                s = app.settings
                for k in ("priorities", "similar_threshold", "cluster_names",
                          "dup_picks", "sim_picks", "deep_video", "auto_preheat"):
                    if k in body:
                        s[k] = body[k]
                if "roots" in body:
                    s["roots"] = util.parse_roots(body["roots"])
                if "priorities" in body:
                    s["priorities"] = util.parse_roots(body["priorities"])
                app.store.save_settings(s)
                self._json({"ok": True, "settings": s})
            elif self.path == "/api/decide":
                n = app.store.decide(body.get("path", ""), body.get("action", ""),
                                     body.get("target", ""))
                self._json({"ok": True, "decided": n})
            elif self.path == "/api/batch":
                action, target = body.get("action", ""), body.get("target", "")
                paths = body.get("paths") or []
                d = app.store.load_decisions()
                for p in paths:
                    if action:
                        d[p] = {"action": action, "target": target, "time": util.ts()}
                    else:
                        d.pop(p, None)
                app.store.save_decisions(d)
                self._json({"ok": True, "count": len(paths)})
            elif self.path == "/api/dupconfirm":
                gid = body["id"]
                g = next((x for x in app.dup_groups() if x["id"] == gid), None)
                if not g:
                    self._json({"error": "group not found"}, 404)
                    return
                d = app.store.load_decisions()
                for m in g["members"]:
                    if m["path"] != g["keeper"]["path"]:
                        d[m["path"]] = {"action": "recycle", "target": "", "time": util.ts()}
                app.store.save_decisions(d)
                self._json({"ok": True, "group": gid})
            elif self.path == "/api/simconfirm":
                gid = body["id"]
                g = next((x for x in app.similar_groups() if x["id"] == gid), None)
                if not g:
                    self._json({"error": "group not found"}, 404)
                    return
                d = app.store.load_decisions()
                for m in g["members"]:
                    if m["path"] != g["recommended"]["path"]:
                        d[m["path"]] = {"action": "recycle", "target": "", "time": util.ts()}
                app.store.save_decisions(d)
                self._json({"ok": True, "group": gid})
            elif self.path == "/api/autosuggest":
                sug = app.suggestions()
                d = app.store.load_decisions()
                n = 0
                for p, (action, target, _why) in sug.items():
                    if action in ("recycle", "archive"):
                        d[p] = {"action": action, "target": target or "", "time": util.ts()}
                        n += 1
                app.store.save_decisions(d)
                self._json({"ok": True, "count": n})
            elif self.path == "/api/apply":
                plan = recycle.build_plan(app.files, app.store.load_decisions())
                if body.get("execute"):
                    # 防止 Windows 文件锁:暂停缩略图预热,等当前 ffmpeg/解码结束
                    app._preheat_pause.clear()
                    waited = 0.0
                    while app._preheat_busy and waited < 150:
                        time.sleep(0.2)
                        waited += 0.2
                    log = os.path.join(app.store.dir, "applied_log.csv")
                    res = recycle.execute(plan, log)
                    moved = res.pop("moved", [])
                    d = app.store.load_decisions()
                    for p in moved:
                        d.pop(p, None)
                    app.store.save_decisions(d)
                    app.scan_status["stale"] = bool(moved)
                    self._json({"result": res, "moved": len(moved)})
                    app._preheat_pause.set()
                else:
                    self._json({"plan": plan, "summary": recycle.plan_summary(plan)})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 400)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def make_server(app, port):
    handler = type("H", (Handler,), {"app": app})
    return Server(("127.0.0.1", port), handler)


def main(port=None, home=None, open_browser=True):
    app = App(home)
    app.load_or_scan()
    port = port or C.DEFAULT_PORT
    httpd = None
    for try_port in range(port, port + 10):
        try:
            httpd = make_server(app, try_port)
            port = try_port
            break
        except OSError:
            continue
    if httpd is None:
        print(f"端口 {port}~{port + 9} 全部被占用,请用 --port 指定其他端口")
        return
    url = f"http://127.0.0.1:{port}"
    print(f"photosshuli v{C.VERSION} 已启动: {url}  (Ctrl+C 退出)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
