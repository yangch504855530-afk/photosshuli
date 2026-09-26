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


def suggest(rec, cluster_names, root, archive_root=None):
    """返回 (动作, 目标目录或None, 说明)。
    动作: recycle(建议回收) / archive(归档,带目标) / keep(不动)。
    archive_root:统一归档主库(分散多根时把照片聚拢到一个库);空=各 root 内归档。
    时间优先级:EXIF 拍摄时间 > 文件名日期 > mtime 年份(三无兜底)。"""
    cls = rec.get("cls") or ""
    rel = rec.get("rel", "")
    base = archive_root or root
    eff_dt = rec.get("dt") or util.date_from_name(rec.get("name") or "")
    if cls in C.RECYCLE_SUGGESTED:
        return ("recycle", None, f"{cls}·建议清理")
    if cls == C.CLS_DASHCAM:
        year = util.year_of(rec.get("dt")) or util.year_of(
            util.date_from_name(rec.get("name") or "")) or (rec.get("mtime_dt") or "")[:4] or "未知时间"
        tgt = os.path.join(base, "行车记录仪", year)
        if _inside(rec.get("path"), tgt):
            return ("keep", None, "已在归档位置,无需移动")
        return ("archive", tgt, "事物:行车记录仪")
    if rec.get("lat") and rec.get("lon"):
        key = rec.get("cluster") or cluster_key(rec["lat"], rec["lon"])
        name = cluster_names.get(key) or f"地点{key}"
        month = util.month_of(rec.get("dt")) or "未知时间"
        tgt = os.path.join(base, "旅行", name, f"{month} {name}")
        if _inside(rec.get("path"), tgt):
            return ("keep", None, "已在归档位置,无需移动")
        return ("archive", tgt, f"地点:{name}({month})")
    if eff_dt:
        year = util.year_of(eff_dt)
        src = "时间" if rec.get("dt") else "文件名日期"
        if year:
            return ("archive", os.path.join(base, year), f"{src}:{year}")
    year = (rec.get("mtime_dt") or "")[:4]
    if year.isdigit():
        return ("archive", os.path.join(base, year), f"三无·按年份{year}归档")
    return ("keep", None, "无法判断")
