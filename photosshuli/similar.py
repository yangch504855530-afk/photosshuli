# -*- coding: utf-8 -*-
"""功能三:相似照片合并(dHash 分组 + 技术质量择优)"""
from . import util


def _blocks(h, nblocks=8):
    """64bit 十六进制哈希 → nblocks 个子块(默认每块 8 bit)"""
    h = (h or "").strip()
    if not h:
        return []
    try:
        v = int(h, 16) & ((1 << 64) - 1)
    except ValueError:
        return []
    per = 64 // nblocks
    return [(v >> (i * per)) & ((1 << per) - 1) for i in range(nblocks)]


def _candidate_pairs(hashes, threshold):
    """返回可能相近的对(子块完全相同者)。阈值≤nblocks 时由鸽笼原理保证不漏。"""
    if threshold > 8:
        return None  # 超出鸽笼保证,调用方退回全比对
    n = len(hashes)
    index = {}
    for i, h in enumerate(hashes):
        for bi, blk in enumerate(_blocks(h)):
            index.setdefault((bi, blk), []).append(i)
    pairs = set()
    for lst in index.values():
        if len(lst) < 2:
            continue
        for a in range(len(lst)):
            for b in range(a + 1, len(lst)):
                pairs.add((lst[a], lst[b]) if lst[a] < lst[b] else (lst[b], lst[a]))
    return pairs


def group_similar(files, threshold=None):
    """相似分组:图片感知哈希汉明距离 ≤ threshold 归为一组(并查集)。
    v0.1.6:8 字节块分桶索引,大库从 O(n²) 降到近似线性(阈值≤8 时不漏配对)。
    返回 [[rec,...],...] 组员>1,组内按质量分降序。"""
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
    pairs = _candidate_pairs(hashes, threshold)
    if pairs is None:  # 阈值>8:退回全比对
        pairs = {(i, j) for i in range(n) for j in range(i + 1, n)}
    for i, j in pairs:
        if util.hamming(hashes[i], hashes[j]) <= threshold:
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
