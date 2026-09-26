# -*- coding: utf-8 -*-
"""photosshuli 本地 Web 服务(默认只读,任何删除都是移入回收站目录)"""
import hashlib
import os
import threading
import urllib.parse
import http.server
import socketserver

from . import config as C
from . import scanner, duplicates, similar, classify, recycle, util
from .store import Store


class App:
    def __init__(self, home=None):
        self.store = Store(home)
        self.settings = self.store.load_settings()
        self.files = []
        self.roots = []
        self.scan_status = {"running": False, "done": 0, "total": 0, "files": 0, "error": ""}
        self._lock = threading.Lock()

    # ---------- scan ----------
    def run_scan(self, roots=None, deep_video=None):
        roots = roots if roots is not None else self.settings["roots"]
        deep_video = self.settings.get("deep_video", True) if deep_video is None else deep_video
        roots = [r for r in roots if r and os.path.isdir(r)]
        if not roots:
            self.scan_status.update(running=False, error="目录不存在,请先在设置里填有效目录")
            return
        self.scan_status.update(running=True, done=0, total=len(roots), files=0, error="")

        def prog(d, t):
            self.scan_status.update(done=d, total=t)

        def work():
            try:
                files, _ = scanner.scan_roots(roots, deep_video=deep_video, progress=prog)
                with self._lock:
                    self.files = files
                    self.roots = roots
                    self.store.save_index(files, roots)
                    self.scan_status.update(running=False, files=len(files))
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
            keeper = picks.get(gid) or duplicates.pick_keeper(
                g, self._priorities())
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

    # ---------- helpers ----------
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
        ln = int(self.headers.get("Content-Length", 0))
        import json
        return json.loads(self.rfile.read(ln) or b"{}")

    # ---------- GET ----------
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
                    "scan": app.scan_status,
                    "version": C.VERSION,
                })
            elif u.path == "/api/scanstatus":
                self._json(app.scan_status)
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

    # ---------- POST ----------
    def do_POST(self):
        app = self.app
        try:
            body = self._body()
            if self.path == "/api/scan":
                app.run_scan(body.get("roots"), body.get("deep_video"))
                self._json({"ok": True, "status": app.scan_status})
            elif self.path == "/api/scanstatus":
                self._json(app.scan_status)
            elif self.path == "/api/settings":
                s = app.settings
                for k in ("roots", "priorities", "similar_threshold", "cluster_names",
                          "dup_picks", "sim_picks", "deep_video"):
                    if k in body:
                        s[k] = body[k]
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
                    log = os.path.join(app.store.dir, "applied_log.csv")
                    self._json({"result": recycle.execute(plan, log)})
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


def main(port=None, home=None):
    app = App(home)
    app.load_or_scan()
    httpd = make_server(app, port or C.DEFAULT_PORT)
    print(f"photosshuli v{C.VERSION} 已启动: http://127.0.0.1:{port or C.DEFAULT_PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
