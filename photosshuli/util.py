# -*- coding: utf-8 -*-
"""通用工具:哈希、EXIF、视频探测、dHash、质量评分、缩略图"""
import io
import os
import json
import hashlib
import subprocess
from datetime import datetime

from PIL import Image, ImageOps, ImageFilter, ImageStat

from . import config

def _find_tool(name):
    """定位 ffmpeg/ffprobe:环境变量 > PATH > 常见安装位置"""
    import shutil
    p = shutil.which(name)
    if p:
        return p
    import glob
    for pat in (rf"C:\ffmpeg*\bin\{name}.exe",
                os.path.expanduser(f"~\\ffmpeg*\\bin\\{name}.exe"),
                f"/usr/bin/{name}", "/usr/local/bin/" + name, "/opt/homebrew/bin/" + name):
        hits = glob.glob(pat)
        if hits:
            return hits[0]
    return name


FFPROBE = os.environ.get("PHOTOSHULI_FFPROBE") or _find_tool("ffprobe")
FFMPEG = os.environ.get("PHOTOSHULI_FFMPEG") or _find_tool("ffmpeg")
try:  # 让 HEIC 在有 pillow_heif 时可读,没有时优雅降级
    import pillow_heif
    pillow_heif.register_heif_opener()
    _HEIF = True
except Exception:
    _HEIF = False


def open_image(path):
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    return img


def md5_of(path, chunk=1024 * 1024):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _get_exif_tags(raw):
    from PIL.ExifTags import TAGS, IFD, GPSTAGS
    tags = {TAGS.get(k, k): v for k, v in raw.items()}
    try:
        for k, v in raw.get_ifd(IFD.ExifIfd).items():
            tags.setdefault(TAGS.get(k, k), v)
        gps = raw.get_ifd(IFD.GPSInfo)
        if gps:
            tags["GPSInfo"] = {GPSTAGS.get(k, k): v for k, v in gps.items()}
    except Exception:
        pass
    return tags


def exif_dt(tags):
    for k in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
        v = tags.get(k)
        if v:
            return str(v)
    return ""


def exif_gps(tags):
    gps = tags.get("GPSInfo") or {}
    if not gps:
        return "", ""

    def conv(vals, ref):
        try:
            d, m, s = [float(x) for x in list(vals)[:3]]
            dec = d + m / 60 + s / 3600
            if ref in ("S", "W"):
                dec = -dec
            return f"{dec:.6f}"
        except Exception:
            return ""
    return (conv(gps.get("GPSLatitude", []), gps.get("GPSLatitudeRef", "")),
            conv(gps.get("GPSLongitude", []), gps.get("GPSLongitudeRef", "")))


def exif_of(path):
    """图片 EXIF:拍摄时间/经纬度/相机。失败返回空。"""
    try:
        img = open_image(path)
        raw = img.getexif()
        tags = _get_exif_tags(raw)
        lat, lon = exif_gps(tags)
        cam = f"{tags.get('Make', '')} {tags.get('Model', '')}".strip()
        return exif_dt(tags), lat, lon, cam
    except Exception:
        return "", "", "", ""


def livp_inner_meta(path):
    """livp=zip;内存读取内部图片的拍摄时间,不落盘"""
    try:
        import zipfile
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.lower().endswith((".heic", ".heif", ".jpg", ".jpeg")):
                    data = z.read(n)
                    img = Image.open(io.BytesIO(data))
                    tags = _get_exif_tags(img.getexif())
                    return exif_dt(tags)
    except Exception:
        pass
    return ""


def probe_video(path):
    """视频元数据:拍摄时间/GPS/时长/分辨率;ffprobe 不可用则全空"""
    dt = lat = lon = dur = res = ""
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=30)
        j = json.loads(r.stdout or "{}")
        fmt = j.get("format", {})
        tags = fmt.get("tags", {}) or {}
        dt = tags.get("creation_time", "")
        loc = tags.get("com.apple.quicktime.location.ISO6709", "") or tags.get("location", "")
        if loc:
            import re
            m = re.findall(r"([+-]?\d+(?:\.\d+)?)", loc)
            try:
                lat, lon = m[0], m[1]
            except Exception:
                pass
        try:
            dur = f"{float(fmt.get('duration', 0)):.0f}s"
        except Exception:
            dur = ""
        for st in j.get("streams", []):
            if st.get("width"):
                res = f"{st['width']}x{st.get('height', '')}"
                break
    except Exception:
        pass
    return dt, lat, lon, dur, res


def dhash(path, size=8):
    """感知哈希:9x8 灰度行内比较 → 64bit 十六进制。失败返回 ''。"""
    try:
        img = open_image(path).convert("L").resize((size + 1, size), Image.LANCZOS)
        px = img.tobytes()
        bits = 0
        for row in range(size):
            for col in range(size):
                left = px[row * (size + 1) + col]
                right = px[row * (size + 1) + col + 1]
                bits = (bits << 1) | (1 if left > right else 0)
        return f"{bits:016x}"
    except Exception:
        return ""


def hamming(h1, h2):
    if not h1 or not h2:
        return 64
    return bin(int(h1, 16) ^ int(h2, 16)).count("1")


def quality_score(path):
    """技术质量评分 0~100:清晰度50% + 曝光25% + 亮度居中15% + 分辨率10%"""
    try:
        img = open_image(path)
        w, h = img.size
        gray = img.convert("L").resize((256, 256), Image.LANCZOS)
        sharp = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).stddev[0]
        hist = gray.histogram()
        total = sum(hist) or 1
        dark = sum(hist[:16]) / total
        bright = sum(hist[240:]) / total
        mean = ImageStat.Stat(gray).mean[0] / 255.0
        clip = max(0.0, 1.0 - (dark + bright) * 2.0)
        centered = max(0.0, 1.0 - abs(mean - 0.5) * 2.0)
        res_bonus = min(1.0, (w * h) / 4_000_000)
        score = (min(sharp / 60.0, 1.0) * 50 + clip * 25 +
                 centered * 15 + res_bonus * 10)
        return round(score, 1)
    except Exception:
        return 0.0


def make_thumb(path, out, size=None):
    """生成缩略图;视频用 ffmpeg 抽 1s 帧;livp 解包内部图;失败返回 False"""
    size = size or config.THUMB_SIZE
    ext = os.path.splitext(str(path))[1].lower()
    tmp = out + ".tmp.jpg"
    try:
        if ext in config.VIDEO_EXT:
            base_cmd = [FFMPEG, "-y", "-v", "quiet", "-i", str(path),
                        "-frames:v", "1", "-vf", f"scale={size}:-2", tmp]
            # 先尝试定位到 1s(快);部分视频时间戳异常无法 seek,退回从头取帧
            rc = subprocess.run(base_cmd[:3] + ["-ss", "1"] + base_cmd[3:],
                                capture_output=True, timeout=60).returncode
            if rc != 0 or not os.path.exists(tmp):
                subprocess.run(base_cmd, capture_output=True, timeout=120, check=True)
        elif ext in config.LIVP_EXT:
            import zipfile
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    if n.lower().endswith((".heic", ".heif", ".jpg", ".jpeg")):
                        img = open_image(io.BytesIO(z.read(n))).convert("RGB")
                        img.thumbnail((size, size))
                        img.save(tmp, "JPEG", quality=75)
                        break
                else:
                    return False
        else:
            img = open_image(path).convert("RGB")
            img.thumbnail((size, size))
            img.save(tmp, "JPEG", quality=75)
        os.replace(tmp, out)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def norm_dt(dt):
    """统一 '2026:02:10 12:00:00' → '2026-02-10 12:00'"""
    if not dt:
        return ""
    d = str(dt).strip()
    import re
    m = re.match(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T]?(\d{2}:\d{2})?", d)
    if m:
        return "-".join(m.group(1, 2, 3)) + (" " + m.group(4) if m.group(4) else "")
    return d


def month_of(dt):
    d = norm_dt(dt).replace("(继承)", "").strip()
    if len(d) >= 7:
        return d[:4] + "-" + d[5:7]
    return ""


def year_of(dt):
    d = norm_dt(dt)
    return d[:4] if len(d) >= 4 else ""


def ts():
    return datetime.now().isoformat(timespec="seconds")
