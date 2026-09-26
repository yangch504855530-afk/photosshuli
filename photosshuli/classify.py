# -*- coding: utf-8 -*-
"""功能四:分类归档 —— 地点/时间/事物,三无兜底按年份"""
import os

from . import config as C
from . import util


def _inside(path, target):
    """文件是否已在目标目录内(含其子目录)"""
    if not path:
        return False
    p = os.path.normcase(os.path.normpath(path))
    t = os.path.normcase(os.path.normpath(target))
    return p == t or p.startswith(t + os.sep)


def cluster_key(lat, lon, precision=None):
    precision = precision if precision is not None else C.CLUSTER_PRECISION
    try:
        return f"{round(float(lat), precision):+.2f},{round(float(lon), precision):+.2f}"
    except Exception:
        return ""


def build_clusters(files):
    out = {}
    for f in files:
        key = f.get("cluster")
        if not key:
            continue
        e = out.setdefault(key, {"key": key, "count": 0, "files": []})
        e["count"] += 1
        e["files"].append(f["path"])
    return sorted(out.values(), key=lambda x: -x["count"])


def suggest(rec, cluster_names, root):
    """返回 (动作, 目标目录或None, 说明)。
    动作: recycle(建议回收) / archive(归档,带目标) / keep(不动) """
    cls = rec.get("cls") or ""
    rel = rec.get("rel", "")
    if cls in C.RECYCLE_SUGGESTED:
        return ("recycle", None, f"{cls}·建议清理")
    if cls == C.CLS_DASHCAM:
        year = util.year_of(rec.get("dt")) or (rec.get("mtime_dt") or "")[:4] or "未知时间"
        tgt = os.path.join(root, "行车记录仪", year)
        if _inside(rec.get("path"), tgt):
            return ("keep", None, "已在归档位置,无需移动")
        return ("archive", tgt, "事物:行车记录仪")
    if rec.get("lat") and rec.get("lon"):
        key = rec.get("cluster") or cluster_key(rec["lat"], rec["lon"])
        name = cluster_names.get(key) or f"地点{key}"
        month = util.month_of(rec.get("dt")) or "未知时间"
        tgt = os.path.join(root, "旅行", name, f"{month} {name}")
        if _inside(rec.get("path"), tgt):
            return ("keep", None, "已在归档位置,无需移动")
        return ("archive", tgt, f"地点:{name}({month})")
    if rec.get("dt"):
        year = util.year_of(rec.get("dt"))
        if year:
            return ("archive", os.path.join(root, year), f"时间:{year}")
    year = (rec.get("mtime_dt") or "")[:4]
    if year.isdigit():
        return ("archive", os.path.join(root, year), f"三无·按年份{year}归档")
    return ("keep", None, "无法判断")
