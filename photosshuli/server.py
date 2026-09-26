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
from . import scanner, duplicates, similar, classify, recycle, util, ai as aimod
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
        self._scan_cancel = threading.Event()

    # ---------- AI(智谱 BigModel) ----------
    def make_ai_client(self):
        return aimod.AIClient(self.settings.get("ai_key"),
                              model=self.settings.get("ai_model"),
                              base_url=self.settings.get("ai_base_url"))

    def _ai_guard(self):
        """返回错误文案或 None(可用)"""
        s = self.settings
        if not s.get("ai_consent"):
            return "未开启云 AI:请在总览页勾选同意(照片缩略图将上传至智谱 API)"
        cli = self.make_ai_client()
        if not cli.ready():
            return "未配置 API Key:请在总览页 AI 设置里填入智谱 API Key"
        today = util.ts()[:10]
        calls = s.get("ai_calls", {})
        if calls.get("date") != today:
            calls = {"date": today, "n": 0}
        cap = int(s.get("ai_daily_cap", 300))
        if calls["n"] >= cap:
            return f"今日 AI 调用已达上限({cap}),可在设置中调整"
        return None

    def _bump_ai_calls(self, n=1):
        today = util.ts()[:10]
        calls = self.settings.get("ai_calls", {})
        if calls.get("date") != today:
            calls = {"date": today, "n": 0}
        calls["n"] += n
        self.settings["ai_calls"] = calls
        self.store.save_settings(self.settings)

    def _ai_source(self, rec):
        """AI 用的图片源:优先用已生成的缩略图(更小更快),否则原文件"""
        tp = self.thumb_path(rec["path"])
        if os.path.exists(tp):
            return tp
        return rec["path"]

    def ai_tag_key(self, rec):
        return f"{rec.get('size')}:{rec.get('mtime')}"

    def ai_ping(self):
        """连接测试:消耗 1 次调用"""
        err = self._ai_guard()
        if err:
            return {"error": err}
        srcs = [f for f in self.files if f.get("kind") in ("photo", "livp", "video")]
        if not srcs:
            return {"error": "索引里没有图片可测"}
        src = self._ai_source(srcs[0])
        b = aimod.b64_of_image(src)
        cli = self.make_ai_client()
        try:
            reply = cli.chat_vision([b], "请只回复两个字:正常")
            self._bump_ai_calls(1)
            return {"ok": True, "reply": (reply or "").strip()[:20], "model": cli.model}
        except Exception as e:  # noqa: BLE001
            return {"error": f"连接失败:{e}"}

    def merge_ai_tags(self, files):
        tags = self.store.load_ai_tags()
        if not tags:
            return files
        # 浅拷贝附加 ai 字段,避免改动索引本体
        out = []
        for f in files:
            t = tags.get(self.ai_tag_key(f))
            if t:
                g = dict(f)
                g["ai"] = {"category": t.get("category"), "tags": t.get("tags"),
                           "quality": t.get("quality"), "suggest": t.get("suggest")}
                out.append(g)
            else:
                out.append(f)
        return out

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
                                reused=0, fresh=0, error="", stale=False,
                                cancelled=False)
        self._scan_cancel.clear()

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
                    previous_files=None if force_full else self.files,
                    cancel=self._scan_cancel.is_set)
                cancelled = self._scan_cancel.is_set()
                with self._lock:
                    if not cancelled and files:
                        self.files = files
                        self.roots = roots
                        self.store.save_index(files, roots)
                    self.scan_status.update(running=False, files=len(self.files),
                                            reused=info["reused"], fresh=info["fresh"],
                                            cancelled=cancelled)
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
            members = g["members"]
            gid = hashlib.md5("|".join(sorted(f["path"] for f in members)).encode()).hexdigest()[:12]
            pick = picks.get(gid)
            keeper = next((m for m in members if m["path"] == pick), None) or                 duplicates.pick_keeper(members, self._priorities())
            out.append({"id": gid, "hash": g["hash"], "keeper": keeper,
                        "members": sorted(members, key=lambda f: f["path"])})
        return out

    def similar_groups(self):
        picks = self.settings.get("sim_picks", {})
        cands = [f for f in self.files if f.get("cls") not in C.RECYCLE_SUGGESTED]
        groups = similar.group_similar(cands, self.settings.get("similar_threshold"))
        out = []
        for g in groups:
            gid = hashlib.md5("|".join(sorted(f["path"] for f in g)).encode()).hexdigest()[:12]
            pick = picks.get(gid)
            rec = next((m for m in g if m["path"] == pick), None) or similar.recommend(g)
            dists = {m["path"]: util.hamming(m["dhash"], rec["dhash"]) for m in g}
            out.append({"id": gid, "recommended": rec, "members": g, "dists": dists})
        return out

    def suggestions(self):
        names = self.settings.get("cluster_names", {})
        aroot = self.settings.get("archive_root") or ""
        return {f["path"]: classify.suggest(f, names, f["root"], aroot or None)
                for f in self.files}

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
                    "files": app.merge_ai_tags(app.files), "roots": app.roots,
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
            elif u.path == "/api/ai/ping":
                r = app.ai_ping()
                self._json(r, 400 if "error" in r else 200)
            elif u.path == "/api/ai/status":
                s = app.settings
                calls = s.get("ai_calls", {})
                cli = app.make_ai_client()
                self._json({"ready": bool(cli.ready() and s.get("ai_consent")),
                            "has_key": cli.ready(), "consent": bool(s.get("ai_consent")),
                            "model": cli.model,
                            "calls_today": calls.get("n", 0) if calls.get("date") == util.ts()[:10] else 0,
                            "cap": int(s.get("ai_daily_cap", 300))})
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
            elif u.path == "/api/report":
                # 整理报告(Markdown):库概况/类别构成/重复与建议/决策统计
                import hashlib as _h
                st = app.stats()
                dec = app.store.load_decisions()
                lines = [f"# photosshuli 整理报告",
                         f"",
                         f"- 生成时间:{util.ts()}",
                         f"- 扫描根:{'; '.join(app.roots) if app.roots else '(无)'}",
                         f"- 文件总数:{st['total']} / {st['bytes']/1073741824:.2f} GiB",
                         f"- 三无文件:{st['no_meta']}",
                         f"", f"## 内容构成", f"",
                         f"| 类别 | 数量 |", f"|---|---|"]
                for k, v in sorted(st["cls"].items(), key=lambda kv: -kv[1]):
                    lines.append(f"| {k} | {v} |")
                lines += [f"", f"## 重复与相似", f""]
                dgs = app.dup_groups()
                lines.append(f"- 精确重复组:{len(dgs)} 组")
                if dgs:
                    lines.append(f"- 可释放(按保留规则):"
                                 f"{sum(sum(m['size'] for m in g['members']) - g['members'][0]['size'] for g in dgs)/1073741824:.2f} GiB")
                lines.append(f"- 相似组:{len(app.similar_groups())} 组")
                lines += [f"", f"## 决策统计", f"",
                          f"- 已决策:{len(dec)} 项",
                          f"- 回收站:{sum(1 for v in dec.values() if v.get('action')=='recycle')}",
                          f"- 归档:{sum(1 for v in dec.values() if v.get('action')=='archive')}"]
                body = chr(10).join(lines).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition",
                                 "attachment; filename=photosshuli-report.md")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/restore/list":
                log = os.path.join(app.store.dir, "applied_log.csv")
                self._json({"items": recycle.find_restorable(log)})
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
            elif self.path == "/api/scan/cancel":
                app._scan_cancel.set()
                self._json({"ok": True})
            elif self.path == "/api/settings":
                s = app.settings
                old_cluster_names = dict(s.get("cluster_names", {}))
                for k in ("priorities", "similar_threshold", "cluster_names",
                          "dup_picks", "sim_picks", "deep_video", "auto_preheat",
                          "ai_consent", "ai_key", "ai_model", "ai_base_url", "ai_daily_cap",
                          "archive_root"):
                    if k in body:
                        s[k] = body[k]
                if "roots" in body:
                    s["roots"] = util.parse_roots(body["roots"])
                if "priorities" in body:
                    s["priorities"] = util.parse_roots(body["priorities"])
                if "cluster_names" in body:
                    old_names = old_cluster_names
                    new_names = body["cluster_names"] or {}
                    s["cluster_names"] = new_names
                    # 簇改名 → 同步既有归档决策里的目标路径
                    d = app.store.load_decisions()
                    changed = False
                    for k, old_nm in old_names.items():
                        new_nm = new_names.get(k, "")
                        if old_nm and new_nm and old_nm != new_nm:
                            for p, dec in d.items():
                                if dec.get("action") == "archive" and old_nm in (dec.get("target") or ""):
                                    dec["target"] = dec["target"].replace(old_nm, new_nm)
                                    changed = True
                    if changed:
                        app.store.save_decisions(d)
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
                n = 0
                for m in g["members"]:
                    if m["path"] == g["keeper"]["path"]:
                        continue
                    if d.get(m["path"], {}).get("action") == "keep":
                        continue  # 用户明确要保留的,确认不覆盖
                    d[m["path"]] = {"action": "recycle", "target": "", "time": util.ts()}
                    n += 1
                app.store.save_decisions(d)
                self._json({"ok": True, "group": gid, "marked": n})
            elif self.path == "/api/simconfirm":
                gid = body["id"]
                g = next((x for x in app.similar_groups() if x["id"] == gid), None)
                if not g:
                    self._json({"error": "group not found"}, 404)
                    return
                d = app.store.load_decisions()
                n = 0
                for m in g["members"]:
                    if m["path"] == g["recommended"]["path"]:
                        continue
                    if d.get(m["path"], {}).get("action") == "keep":
                        continue  # 用户明确要保留的,确认不覆盖
                    d[m["path"]] = {"action": "recycle", "target": "", "time": util.ts()}
                    n += 1
                app.store.save_decisions(d)
                self._json({"ok": True, "group": gid, "marked": n})
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
            elif self.path == "/api/ai/rank":
                err = app._ai_guard()
                if err:
                    self._json({"error": err}, 400)
                    return
                gid = body.get("id")
                g = next((x for x in app.similar_groups() if x["id"] == gid), None)
                if not g:
                    self._json({"error": "group not found"}, 404)
                    return
                members = g["members"][:aimod.MAX_IMAGES_PER_CALL]
                entries = [(app._ai_source(m), m["name"]) for m in members]
                cli = app.make_ai_client()
                r = cli.rank_group(entries)
                app._bump_ai_calls(1)
                best = members[r["best"]]["path"]
                picks = app.settings.get("sim_picks", {})
                picks[gid] = best
                reasons = app.settings.get("sim_ai_reasons", {})
                reasons[gid] = r["reason"]
                app.store.save_settings(app.settings)
                self._json({"ok": True, "best": best, "reason": r["reason"]})
            elif self.path == "/api/ai/tag":
                err = app._ai_guard()
                if err:
                    self._json({"error": err}, 400)
                    return
                by_path = {f["path"]: f for f in app.files}
                recs = [by_path[p] for p in (body.get("paths") or []) if p in by_path]
                recs = recs[:48]
                if not recs:
                    self._json({"error": "没有可识别的文件"}, 400)
                    return
                entries = [(app._ai_source(r), r["name"]) for r in recs]
                chunks = [entries[i:i + 6] for i in range(0, len(entries), 6)]
                cli = app.make_ai_client()

                def run_chunk(chunk):
                    rows = cli.tag_batch(chunk)
                    return list(zip([c[1] for c in chunk], rows))

                mapped = cli.map_batches(chunks, run_chunk, workers=2)
                tags = app.store.load_ai_tags()
                n_applied = 0
                for chunk, rows in zip(chunks, mapped):
                    for name, row in rows:
                        if 0 <= row["i"] < len(chunk):
                            src_path = chunk[row["i"]][0]
                            rec = next(r for r in recs if app._ai_source(r) == src_path)
                            tags[app.ai_tag_key(rec)] = {**row, "time": util.ts(),
                                                         "model": cli.model}
                            n_applied += 1
                app.store.save_ai_tags(tags)
                app._bump_ai_calls(len(chunks))
                self._json({"ok": True, "applied": n_applied, "calls": len(chunks)})
            elif self.path == "/api/ai/namecluster":
                err = app._ai_guard()
                if err:
                    self._json({"error": err}, 400)
                    return
                key = body.get("key", "")
                files = [f for f in app.files if f.get("cluster") == key]
                files.sort(key=lambda f: -(f.get("score") or 0))
                sample = files[:4]
                if not sample:
                    self._json({"error": "簇为空"}, 404)
                    return
                entries = [(app._ai_source(f), f["name"]) for f in sample]
                cli = app.make_ai_client()
                name = cli.name_cluster(entries, key)
                app._bump_ai_calls(1)
                names = app.settings.get("cluster_names", {})
                names[key] = name
                app.store.save_settings(app.settings)
                self._json({"ok": True, "name": name})
            elif self.path == "/api/compare":
                # 目录级重复对比(只读):A/B 两目录按 文件名+大小 比对
                import os as _os
                a = (body.get("a") or "").strip()
                b = (body.get("b") or "").strip()
                if not (_os.path.isdir(a) and _os.path.isdir(b)):
                    self._json({"error": "目录不存在"}, 400)
                    return

                def walk_files(root):
                    out = {}
                    for dp, dns, fns in _os.walk(root):
                        dns[:] = [x for x in dns
                                  if x not in C.SKIP_DIRS and not x.startswith(".")]
                        for fn in fns:
                            p2 = _os.path.join(dp, fn)
                            try:
                                st2 = _os.stat(p2)
                            except OSError:
                                continue
                            out[fn + ":" + str(st2.st_size)] = p2
                    return out

                fa, fb = walk_files(a), walk_files(b)
                common = set(fa) & set(fb)
                a_only, b_only = set(fa) - set(fb), set(fb) - set(fa)
                cb = sum(_os.path.getsize(fa[k]) for k in common)
                self._json({
                    "a": a, "b": b,
                    "aCount": len(fa), "bCount": len(fb),
                    "matched": len(common),
                    "matchedBytes": cb,
                    "aOnlyCount": len(a_only),
                    "bOnlyCount": len(b_only),
                    "aOnlyBytes": sum(_os.path.getsize(fa[k]) for k in a_only),
                    "bOnlyBytes": sum(_os.path.getsize(fb[k]) for k in b_only),
                    "matchedSample": [fa[k] for k in list(common)[:50]],
                    "aOnlySample": [fa[k] for k in list(a_only)[:50]],
                    "bOnlySample": [fb[k] for k in list(b_only)[:50]],
                })
            elif self.path == "/api/restore/list":
                log = os.path.join(app.store.dir, "applied_log.csv")
                self._json({"items": recycle.find_restorable(log)})
            elif self.path == "/api/restore":
                log = os.path.join(app.store.dir, "applied_log.csv")
                res = recycle.restore(body.get("items") or [], log)
                if res["ok"]:
                    app.scan_status["stale"] = True
                    app.run_scan()
                self._json({"result": res})
            elif self.path == "/api/decide/import":
                if body.get("csv"):
                    import csv as _csv
                    import io as _io
                    rows = list(_csv.reader(_io.StringIO(body["csv"].replace("﻿", ""))))
                    rows = rows[1:] if rows and rows[0] and rows[0][0].startswith("路径") else rows
                else:
                    rows = body.get("rows") or []
                d = app.store.load_decisions()
                n = 0
                for row in rows:
                    try:
                        path, action = row[0].strip(), (row[1] or "").strip()
                    except Exception:
                        continue
                    if not path or action not in ("recycle", "archive", "keep"):
                        continue
                    d[path] = {"action": action, "target": (row[2] if len(row) > 2 else "") or "",
                               "time": util.ts()}
                    n += 1
                app.store.save_decisions(d)
                self._json({"ok": True, "imported": n})
            elif self.path == "/api/ai/ping":
                r = app.ai_ping()
                self._json(r, 400 if "error" in r else 200)
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
                    if moved:
                        app.run_scan()  # 自动增量重扫,索引立即跟上
                else:
                    self._json({"plan": plan, "summary": recycle.plan_summary(plan)})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 400)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def make_server(app, port, host="127.0.0.1"):
    handler = type("H", (Handler,), {"app": app})
    return Server((host, port), handler)


def main(port=None, home=None, open_browser=True, lan=False):
    app = App(home)
    app.load_or_scan()
    port = port or C.DEFAULT_PORT
    httpd = None
    for try_port in range(port, port + 10):
        try:
            httpd = make_server(app, try_port, host="0.0.0.0" if lan else "127.0.0.1")
            port = try_port
            break
        except OSError:
            continue
    if httpd is None:
        print(f"端口 {port}~{port + 9} 全部被占用,请用 --port 指定其他端口")
        return
    if lan:
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            lip = s.getsockname()[0]
            s.close()
        except Exception:
            lip = "本机IP"
        url = f"http://{lip}:{port}  (局域网可访问,注意同一 WiFi 内任何人都能操作)"
    else:
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
