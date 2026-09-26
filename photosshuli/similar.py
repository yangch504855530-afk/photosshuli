# -*- coding: utf-8 -*-
"""功能三:相似照片合并(dHash 分组 + 技术质量择优)"""
from . import util


def group_similar(files, threshold=None):
    """相似分组:图片感知哈希汉明距离 ≤ threshold 归为一组(并查集)。
    仅对含 dhash 的记录参与。返回 [[rec,...],...] 组员>1,组内按质量分降序。"""
    threshold = threshold if threshold is not None else 8
    cands = [f for f in files if f.get("dhash")]
    n = len(cands)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    hashes = [f["dhash"] for f in cands]
    for i in range(n):
        hi = hashes[i]
        for j in range(i + 1, n):
            if abs(len(hi) - len(hashes[j])):
                continue
            if util.hamming(hi, hashes[j]) <= threshold:
                union(i, j)

    groups = {}
    for i, f in enumerate(cands):
        groups.setdefault(find(i), []).append(f)
    out = [sorted(g, key=lambda f: -(f.get("score") or 0))
           for g in groups.values() if len(g) > 1]
    out.sort(key=lambda g: -len(g))
    return out


def recommend(group):
    """组内择优:技术质量评分最高者(清晰度/曝光/分辨率),组已按分数排序"""
    return group[0] if group else None
