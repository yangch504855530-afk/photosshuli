# -*- coding: utf-8 -*-
"""AI 能力(智谱 BigModel paas/v4,OpenAI 兼容视觉接口)
三件事:相似组择优 / 内容打标 / 地点簇命名。
设计约束:密钥只在用户本机;每次调用由用户手动触发;无密钥时全部功能回退本地规则。"""
import base64
import io
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from . import util

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-4.6v"
MAX_IMAGES_PER_CALL = 8
THUMB_LONG_EDGE = 512

RANK_PROMPT = """你是照片筛选助手。下面是从同一次拍摄场景挑出的{n}张相似照片(已编号 0..{last})。
请按"人眼审美"挑出最好的一张,标准:主体清晰、曝光舒服、构图完整、表情/瞬间最佳;模糊、闭眼、抖动、遮挡的优先排除。
只输出 JSON:{{"best": 编号, "reason": "一句话理由"}}"""

TAG_PROMPT = """你是照片整理助手。下面{n}张照片按顺序编号 0..{last}(文件名依次为:{names})。
对每张输出:类别(风景/人物/美食/宠物/文档/证件/聊天截图/屏幕截图/表情包/票据/建筑/运动/夜景/自拍/其他)、
3个以内标签、质量问题(模糊/过曝/过暗/无,选其一)、整理建议(保留/可删/由人决定)。
只输出 JSON 数组:[{{"i":0,"category":"...","tags":["..."],"quality":"...","suggest":"保留|可删|由人决定"}}]"""

NAME_PROMPT = """这是同一地点拍摄的照片样张。地点坐标:{key}。
请给这个地点起一个简短中文地名(2-6个字,如"洱海""普吉岛""外滩"),不要日期和序号。
只输出 JSON:{{"name":"..."}}"""


def b64_of_image(path, long_edge=THUMB_LONG_EDGE):
    """读图 → 缩放 → JPEG base64(视频/livp 走已有缩略图;失败返回 None)"""
    try:
        src = path
        img = util.open_image(src)
        img.thumbnail((long_edge, long_edge))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def _extract_json(text):
    """模型输出健壮解析:剥 ``` 围栏,抓第一个 JSON 对象/数组"""
    text = re.sub(r"```(?:json)?", "", text or "").strip()
    for pat in (r"\[.*\]", r"\{.*\}"):
        m = re.search(pat, text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                continue
    raise ValueError("AI 返回中未找到有效 JSON")


class AIClient:
    """BigModel 视觉客户端。transport 可注入用于离线测试。"""

    def __init__(self, api_key, model=None, base_url=None, transport=None, timeout=120):
        self.api_key = (api_key or os.environ.get("BIGMODEL_API_KEY", "")).strip()
        self.model = model or os.environ.get("PHOTOSHULI_AI_MODEL", DEFAULT_MODEL)
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self._transport = transport or self._http_transport

    # ---- transport ----
    def _http_transport(self, url, payload, timeout):
        import urllib.request
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def ready(self):
        return bool(self.api_key)

    def chat_vision(self, images_b64, prompt):
        content = [{"type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b}"}}
                   for b in images_b64 if b]
        if not content:
            raise ValueError("没有可用的图片")
        content.append({"type": "text", "text": prompt})
        payload = {"model": self.model, "temperature": 0.2,
                   "messages": [{"role": "user", "content": content}]}
        resp = self._transport(self.base_url + "/chat/completions", payload, self.timeout)
        return resp["choices"][0]["message"]["content"]

    # ---- 能力一:相似组择优 ----
    def rank_group(self, entries):
        """entries: [(path, name), ...](≤8)。返回 {best_index, reason, ranking_raw}"""
        n = len(entries)
        prompt = RANK_PROMPT.format(n=n, last=n - 1)
        imgs = [b64_of_image(p) for p, _ in entries]
        out = self.chat_vision(imgs, prompt)
        data = _extract_json(out)
        best = int(data.get("best", 0))
        if not (0 <= best < n):
            best = 0
        return {"best": best, "reason": str(data.get("reason", ""))[:120]}

    # ---- 能力二:批量打标 ----
    def tag_batch(self, entries):
        """entries: [(path, name), ...](≤8)。返回 [{i,category,tags,quality,suggest}]"""
        n = len(entries)
        names = ", ".join(f"{i}={name}" for i, (_, name) in enumerate(entries))
        prompt = TAG_PROMPT.format(n=n, last=n - 1, names=names)
        imgs = [b64_of_image(p) for p, _ in entries]
        arr = _extract_json(self.chat_vision(imgs, prompt))
        if not isinstance(arr, list):
            raise ValueError("AI 返回不是数组")
        out = []
        for row in arr[:n]:
            try:
                i = int(row.get("i", 0))
                out.append({"i": i,
                            "category": str(row.get("category", "其他"))[:12],
                            "tags": [str(t)[:16] for t in (row.get("tags") or [])[:3]],
                            "quality": str(row.get("quality", "无"))[:8],
                            "suggest": str(row.get("suggest", "由人决定"))[:6]})
            except Exception:
                continue
        return out

    # ---- 能力三:地点簇命名 ----
    def name_cluster(self, entries, key):
        """entries: [(path, name), ...](≤4)。返回 name"""
        prompt = NAME_PROMPT.format(key=key)
        imgs = [b64_of_image(p) for p, _ in entries]
        data = _extract_json(self.chat_vision(imgs, prompt))
        name = re.sub(r"[\s\\/:*?\"<>|]", "", str(data.get("name", "")))[:12]
        return name or "未命名地点"

    # ---- 并发批处理 ----
    def map_batches(self, jobs, worker, workers=2):
        """jobs: list of entry-lists;worker(batch)->result。返回 [results]"""
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            return list(ex.map(worker, jobs))
