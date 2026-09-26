# -*- coding: utf-8 -*-
"""功能二:精确重复识别 + 文件夹保留规则"""
import os
from collections import defaultdict

from . import util


def find_exact_dups(files, progress=None):
    """精确重复分组:先按大小分桶,桶内再算 MD5。
    files: 记录列表(含 path/size)。返回 [[rec, ...], ...] 仅保留组员>1 的组。"""
    by_size = defaultdict(list)
    for f in files:
        if f.get("kind") in ("doc", "broken") or f.get("cloud"):
            continue  # 云占位符读内容会触发下载,不参与精确去重
        by_size[f["size"]].append(f)
    groups = []
    sizes = sorted((s for s, lst in by_size.items() if len(lst) > 1), reverse=True)
    for i, size in enumerate(sizes):
        if progress:
            progress(i + 1, len(sizes))
        by_hash = defaultdict(list)
        for f in by_size[size]:
            try:
                h = util.md5_of(f["path"])
            except OSError:
                continue
            by_hash[h].append(f)
        for h, lst in by_hash.items():
            if len(lst) > 1:
                groups.append({"hash": h, "members": lst})
    return groups


def _priority_rank(path, priorities):
    """路径在保留优先级列表中的序号;未匹配 → 列表长度(最低优先)"""
    p = os.path.normcase(os.path.normpath(path))
    for i, pref in enumerate(priorities):
        pp = os.path.normcase(os.path.normpath(pref))
        if p == pp or p.startswith(pp + os.sep):
            return i
    return len(priorities)


def pick_keeper(group, priorities):
    """按规则选保留者:
    1) 优先级目录靠前者保留(优先级列表=用户在设置里排的顺序)
    2) 同级中质量分高者保留(如可算)
    3) 再同则修改时间新者保留"""
    def key(f):
        score = f.get("score") or 0
        return (_priority_rank(f["path"], priorities), -score, -f.get("mtime", 0))
    return sorted(group, key=key)[0]
