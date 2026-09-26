# -*- coding: utf-8 -*-
"""扫描引擎:遍历根目录 → 元数据 + 内容识别 + 哈希/评分 → 索引"""
import os
import time
from datetime import datetime

from . import config as C
from . import util, recognize
from .classify import cluster_key


def scan_roots(roots, deep_video=True, progress=None, cancel=None):
    """扫描多个根目录。返回 files 列表与 clusters。
    记录字段: root/rel/path/name/ext/kind/size/mtime/mtime_dt/dt/lat/lon/cam/dur/res/cls/dhash/score/cluster
    """
    files = []
    roots = [os.path.normpath(os.path.abspath(r)) for r in roots]
    total_roots = len(roots)
    for ri, root in enumerate(roots):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in C.SKIP_DIRS]
            for fn in filenames:
                if cancel and cancel():
                    return files, []
                p = os.path.join(dirpath, fn)
                ext = os.path.splitext(fn)[1].lower()
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                kind = ("photo" if ext in C.IMAGE_EXT else
                        "video" if ext in C.VIDEO_EXT else
                        "livp" if ext in C.LIVP_EXT else "doc")
                rec = {
                    "root": root,
                    "rel": os.path.relpath(p, root).replace(os.sep, "/"),
                    "path": p,
                    "name": fn,
                    "ext": ext,
                    "kind": kind,
                    "size": st.st_size,
                    "mtime": int(st.st_mtime),
                    "mtime_dt": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d"),
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
                    elif kind == "video":
                        if deep_video:
                            rec["dt"], rec["lat"], rec["lon"], rec["dur"], rec["res"] = util.probe_video(p)
                except Exception:
                    kind = rec["kind"] = "broken"
                rec["cls"] = recognize.classify(rec)
                if rec["lat"] and rec["lon"]:
                    rec["cluster"] = cluster_key(rec["lat"], rec["lon"])
                else:
                    rec["cluster"] = ""
                files.append(rec)
        if progress:
            progress(ri + 1, total_roots)
    return files, []


def stats_of(files):
    from collections import Counter
    cls = Counter(f["cls"] for f in files)
    kind = Counter(f["kind"] for f in files)
    size = sum(f["size"] for f in files)
    nometa = sum(1 for f in files if not f["dt"] and not f["cluster"]
                 and f["cls"] in (C.CLS_NOEXIF, C.CLS_VIDEO, C.CLS_DOC))
    return {"total": len(files), "bytes": size, "cls": dict(cls),
            "kind": dict(kind), "no_meta": nometa}
