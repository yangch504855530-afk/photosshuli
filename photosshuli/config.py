# -*- coding: utf-8 -*-
"""photosshuli 全局配置"""
VERSION = "0.1.3"
TOOL_NAME = "photosshuli"
DEFAULT_PORT = 8630
# 缩略图最长边
THUMB_SIZE = 360
# 扫描线程数(None=自动)
SCAN_WORKERS = None

# 目录内文件大小阈值:低于该值的图片视为疑似缓存缩略图
CACHE_SIZE_MAX = 64 * 1024
# 相似判定:dHash 汉明距离阈值(0~64,越小越严格)
DEFAULT_SIMILAR_THRESHOLD = 8
# GPS 聚类网格(度,约 2km)
CLUSTER_PRECISION = 2
# 回收站目录名(建在扫描根目录下,同盘移动可找回)
RECYCLE_DIR = "_PhotosShuli回收站"
# 扫描时跳过的目录名
SKIP_DIRS = {RECYCLE_DIR, ".photosshuli", "$RECYCLE.BIN", "System Volume Information", "@Recycle"}

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".bmp", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".wmv", ".ts", ".mkv", ".flv"}
LIVP_EXT = {".livp"}
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT | LIVP_EXT

# 内容识别类别
CLS_CAMERA = "相机照片"        # EXIF 带相机型号
CLS_NOEXIF = "无元数据图片"    # 图片但无任何 EXIF
CLS_SCREENSHOT = "截图"
CLS_CHAT = "聊天图片"
CLS_CACHE = "疑似缓存缩略图"
CLS_LIVE = "实况照片"
CLS_DASHCAM = "行车记录仪"
CLS_VIDEO = "视频"
CLS_DOC = "文档/其他"
CLS_BROKEN = "无法读取"

# 建议进入回收站的类别
RECYCLE_SUGGESTED = {CLS_SCREENSHOT, CLS_CHAT, CLS_CACHE}
