# -*- coding: utf-8 -*-
"""扫描引擎:遍历根目录 → 元数据 + 内容识别 + 哈希/评分 → 索引
v0.1.2:多线程处理 + 增量扫描(未变化文件复用上次结果)"""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Lock

from . import config as C
from . import util, recognize
from .classify import cluster_key


def _normcase(p):
    return os.path.normcase(os.path.normpath(p))


def scan_roots(roots, deep_video=True, progress=None, cancel=None,
               previous_files=None, workers=None):
    """扫描多个根目录。返回 (files, info)。
    info: {"reused": 复用数, "fresh": 新处理数, "workers": 线程数, "total": 总数}
    previous_files 提供时做增量:大小+mtime 未变的文件直接复用旧记录。"""
    roots = [os.path.normpath(os.path.abspath(r)) for r in roots]
    workers = workers or C.SCAN_WORKERS or min(8, os.cpu_count() or 4)

    # 第一遍:收集 (root, rel, path, size, mtime)
    entries = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in C.SKIP_DIRS]
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                entries.append((root, os.path.relpath(p, root).replace(os.sep, "/"),
                                p, st.st_size, int(st.st_mtime)))

    cache = {}
    if previous_files:
        rootset = {_normcase(r) for r in roots}
        for rec in previous_files:
            try:
                if _normcase(rec["root"]) in rootset:
                    cache[_normcase(rec["path"])] = rec
            except KeyError:
                continue

    todo, reused = [], []
    for root, rel, p, size, mtime in entries:
        old = cache.get(_normcase(p))
        if old is not None and old.get("size") == size and old.get("mtime") == mtime:
            reused.append(old)
        else:
            todo.append((root, rel, p, size, mtime))

    total = len(entries)
    done = [len(reused)]
    lk = Lock()

    def tick():
        with lk:
            done[0] += 1
            if progress and done[0] % 20 == 0:
                progress(done[0], total)

    def process(entry):
        if cancel and cancel():
            return None
        root, rel, p, size, mtime = entry
        ext = os.path.splitext(p)[1].lower()
        kind = ("photo" if ext in C.IMAGE_EXT else
                "video" if ext in C.VIDEO_EXT else
                "livp" if ext in C.LIVP_EXT else
                "raw" if ext in C.RAW_EXT else "doc")
        rec = {
            "root": root, "rel": rel, "path": p, "name": os.path.basename(p),
            "ext": ext, "kind": kind, "size": size, "mtime": mtime,
            "mtime_dt": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d"),
            "dt": "", "lat": "", "lon": "", "cam": "",
            "dur": "", "res": "", "dhash": "", "score": 0.0,
        }
        try:
            if kind == "photo":
                rec["dt"], rec["lat"], rec["lon"], rec["cam"] = util.exif_of(p)
                rec["dhash"] = util.dhash(p)
                rec["score"] = util.quality_score(p)
            elif kind == "livp":
                rec["dt"] = util.livp_inner_meta(p)
                rec["dhash"] = util.dhash(p)
                rec["score"] = util.quality_score(p)
            elif kind == "video" and deep_video:
                rec["dt"], rec["lat"], rec["lon"], rec["dur"], rec["res"] = util.probe_video(p)
        except Exception:
            rec["kind"] = "broken"
        rec["cls"] = recognize.classify(rec)
        rec["cluster"] = cluster_key(rec["lat"], rec["lon"]) if rec["lat"] and rec["lon"] else ""
        tick()
        return rec

    fresh = []
    if todo:
        if workers > 1 and len(todo) > 1:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for r in ex.map(process, todo):
                    if r:
                        fresh.append(r)
        else:
            for r in map(process, todo):
                if r:
                    fresh.append(r)

    files = reused + fresh
    files.sort(key=lambda f: _normcase(f["path"]))
    info = {"reused": len(reused), "fresh": len(fresh),
            "workers": workers if todo else 0, "total": total}
    if progress:
        progress(total, total)
    return files, info


def stats_of(files):
    from collections import Counter
    cls = Counter(f["cls"] for f in files)
    kind = Counter(f["kind"] for f in files)
    size = sum(f["size"] for f in files)
    nometa = sum(1 for f in files if not f["dt"] and not f["cluster"]
                 and f["cls"] in (C.CLS_NOEXIF, C.CLS_VIDEO, C.CLS_DOC))
    return {"total": len(files), "bytes": size, "cls": dict(cls),
            "kind": dict(kind), "no_meta": nometa}
