# -*- coding: utf-8 -*-
"""功能一:文件夹内容识别 —— 判断每个文件属于哪一类"""
import os

from . import config as C


def _has(name, *keys):
    low = name.lower()
    return any(k in low for k in keys)


def classify_name(name):
    """基于文件名的内容识别,返回类别或 None"""
    if _has(name, "screenshot", "screen shot", "screen_shot", "截屏", "截图", "snip"):
        return C.CLS_SCREENSHOT
    if _has(name, "wechat", "weixin", "mmexport", "wx_camera", "qq", "_chat",
            "pin_image", "微信", "COMMERCIAL"):
        return C.CLS_CHAT
    if _has(name, "_thumb_", "_mini_", "thumb_", ".thumbnail", "cache"):
        return C.CLS_CACHE
    stem = os.path.splitext(name)[0].lower()
    if stem.startswith("hiv") and stem[3:].isdigit():
        return C.CLS_DASHCAM
    return None


def classify(rec):
    """综合识别。rec 需含: name/ext/size/kind/cam/dt。返回类别字符串"""
    ext = (rec.get("ext") or "").lower()
    name = rec.get("name") or ""
    by_name = classify_name(name)
    if rec.get("kind") == "doc":
        return C.CLS_DOC
    if rec.get("kind") == "broken":
        return C.CLS_BROKEN
    if ext == ".livp":
        return C.CLS_LIVE
    if ext in C.VIDEO_EXT:
        return by_name or C.CLS_VIDEO
    if ext in C.IMAGE_EXT:
        if by_name:
            return by_name
        if rec.get("cam"):
            # 有相机 EXIF 的一律视为真实照片(小体积平滑图也常见于真实拍摄)
            return C.CLS_CAMERA
        if rec.get("size", 0) < C.CACHE_SIZE_MAX:
            return C.CLS_CACHE
        return C.CLS_NOEXIF
    return C.CLS_DOC
