# -*- coding: utf-8 -*-
"""photosshuli 测试:合成数据覆盖 识别/重复/相似/分类/回收/服务端全链路"""
import io
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image, ImageFilter  # noqa: E402

from photosshuli import config as C  # noqa: E402
from photosshuli import recognize, duplicates, similar, classify, scanner, recycle, util  # noqa: E402
from photosshuli.server import App, make_server  # noqa: E402


def make_jpeg(path, size=(800, 600), mode="noise", exif_dt=None, model=None):
    w, h = size
    if mode == "noise":
        rnd = random.Random(42)
        img = Image.new("RGB", (w, h))
        img.putdata([(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
                     for _ in range(w * h)])
    elif mode == "pattern":
        # 渐变+大色块:dHash 在缩放/压暗下稳定,且压缩后体积可控
        img = Image.new("RGB", (w, h))
        px = img.load()
        for y in range(h):
            for x in range(w):
                px[x, y] = (int(255 * x / max(w - 1, 1)), int(255 * y / max(h - 1, 1)), 90)
        from PIL import ImageDraw
        d = ImageDraw.Draw(img)
        d.rectangle([w // 8, h // 8, w // 3, h // 3], fill=(20, 20, 60))
        d.ellipse([w // 2, h // 2, w - 20, h - 20], fill=(220, 180, 40))
    else:
        img = Image.new("RGB", (w, h), (120, 130, 140))
    ex = None
    if exif_dt or model:
        from PIL import Image as _I
        ex = _I.Exif()
        if exif_dt:
            ex[306] = exif_dt
        if model:
            ex[272] = model
    if ex:
        img.save(path, "JPEG", exif=ex)
    else:
        img.save(path, "JPEG")
    return path


def pad_to(path, min_size=90 * 1024):
    """追加零字节,确保文件体积超过缓存缩略图阈值(读取不受影响)"""
    if os.path.getsize(path) < min_size:
        with open(path, "ab") as f:
            f.write(b"\x00" * (min_size - os.path.getsize(path) + 1024))


class FixtureMixin:
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pshuli_test_")
        self.a = os.path.join(self.root, "a")
        self.b = os.path.join(self.root, "b")
        os.makedirs(self.a)
        os.makedirs(self.b)
        base = make_jpeg(os.path.join(self.a, "IMG_0001.jpg"), mode="pattern",
                         exif_dt="2026:02:10 10:00:00", model="TestCam")
        pad_to(base)
        for name in ("IMG_0001.jpg", "IMG_0001_copy.jpg"):
            shutil.copyfile(base, os.path.join(self.b, name))
        # 相似变体:同图缩小+压暗,无 EXIF
        img = Image.open(base)
        small = img.resize((600, 450)).point(lambda v: max(0, v - 40))
        small.save(os.path.join(self.a, "IMG_0002.jpg"), "JPEG")
        pad_to(os.path.join(self.a, "IMG_0002.jpg"))
        make_jpeg(os.path.join(self.a, "Screenshot_2026-01-01.png"), size=(300, 300),
                  mode="flat")
        make_jpeg(os.path.join(self.a, "_thumb_cache.jpg"), size=(20, 20), mode="flat")
        with open(os.path.join(self.a, "hiv00001.mp4"), "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 5000)
        with open(os.path.join(self.a, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("hello")
        make_jpeg(os.path.join(self.a, "none.jpg"), size=(900, 700), mode="noise")
        self._stagger_mtimes()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _stagger_mtimes(self):
        """各文件 mtime 拉开,避免同秒创建导致 size:mtime 类键相互碰撞"""
        i = 0
        for dp, _, fns in os.walk(self.root):
            for fn in fns:
                p2 = os.path.join(dp, fn)
                os.utime(p2, (1_790_000_000 + i, 1_790_000_000 + i))
                i += 1

    def scan(self):
        files, _ = scanner.scan_roots([self.root], deep_video=False)
        return files


class TestRecognize(FixtureMixin, unittest.TestCase):
    def test_classes(self):
        files = {os.path.basename(f["path"]): f for f in self.scan()}
        self.assertEqual(files["IMG_0001.jpg"]["cls"], C.CLS_CAMERA)
        self.assertEqual(files["Screenshot_2026-01-01.png"]["cls"], C.CLS_SCREENSHOT)
        self.assertEqual(files["_thumb_cache.jpg"]["cls"], C.CLS_CACHE)
        self.assertEqual(files["hiv00001.mp4"]["cls"], C.CLS_DASHCAM)
        self.assertEqual(files["notes.txt"]["cls"], C.CLS_DOC)
        self.assertEqual(files["none.jpg"]["cls"], C.CLS_NOEXIF)
        self.assertEqual(files["IMG_0002.jpg"]["cls"], C.CLS_NOEXIF)


class TestDuplicates(FixtureMixin, unittest.TestCase):
    def test_exact_group_and_retention(self):
        files = self.scan()
        groups = duplicates.find_exact_dups(files)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(len(g), 3)
        keeper = duplicates.pick_keeper(g, [self.a, self.b])
        self.assertEqual(keeper["path"], os.path.join(self.a, "IMG_0001.jpg"))
        keeper2 = duplicates.pick_keeper(g, [self.b, self.a])
        self.assertTrue(keeper2["path"].startswith(self.b))


class TestSimilar(FixtureMixin, unittest.TestCase):
    def test_group_and_recommend(self):
        files = self.scan()
        cands = [f for f in files if f["cls"] not in C.RECYCLE_SUGGESTED]
        groups = similar.group_similar(cands, threshold=10)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        paths = {f["path"] for f in g}
        self.assertIn(os.path.join(self.a, "IMG_0001.jpg"), paths)
        self.assertIn(os.path.join(self.a, "IMG_0002.jpg"), paths)
        rec = similar.recommend(g)
        self.assertGreaterEqual(rec["score"], max(f["score"] for f in g))
        # 变体(缩小+压暗)不应被评为最佳
        self.assertNotEqual(rec["path"], os.path.join(self.a, "IMG_0002.jpg"))

    def test_unrelated_not_grouped(self):
        img1 = make_jpeg(os.path.join(self.a, "s1.jpg"), size=(600, 600), mode="noise")
        img2 = make_jpeg(os.path.join(self.a, "s2.jpg"), size=(600, 600), mode="pattern")
        f1 = {"path": img1, "dhash": util.dhash(img1), "score": 50, "size": 1}
        f2 = {"path": img2, "dhash": util.dhash(img2), "score": 50, "size": 1}
        self.assertTrue(util.hamming(f1["dhash"], f2["dhash"]) > 10)
        groups = similar.group_similar([f1, f2], threshold=8)
        self.assertEqual(groups, [])


class TestClassify(unittest.TestCase):
    def R(self, **kw):
        base = dict(cls=C.CLS_CAMERA, rel="x.jpg", dt="", lat="", lon="",
                    mtime_dt="2026-02-10", cluster="")
        base.update(kw)
        return base

    def test_place(self):
        root = "R"
        act, tgt, why = classify.suggest(
            self.R(lat="+30.19", lon="+120.23", dt="2026-02-10 10:00", cluster="+30.19,+120.23"),
            {"+30.19,+120.23": "杭州"}, root)
        self.assertEqual(act, "archive")
        self.assertIn("旅行", tgt)
        self.assertIn("杭州", tgt)
        self.assertIn("2026-02", tgt)

    def test_time_and_fallback_year(self):
        act, tgt, _ = classify.suggest(self.R(dt="2025-01-02 03:04"), {}, "R")
        self.assertEqual((act, tgt), ("archive", os.path.join("R", "2025")))
        act, tgt, why = classify.suggest(self.R(), {}, "R")
        self.assertEqual((act, tgt), ("archive", os.path.join("R", "2026")))
        self.assertIn("三无", why)

    def test_thing_and_recycle(self):
        act, tgt, _ = classify.suggest(self.R(cls=C.CLS_DASHCAM), {}, "R")
        self.assertEqual(tgt, os.path.join("R", "行车记录仪", "2026"))
        act, tgt, _ = classify.suggest(self.R(cls=C.CLS_SCREENSHOT), {}, "R")
        self.assertEqual(act, "recycle")


class TestUtilNew(unittest.TestCase):
    def test_parse_roots(self):
        text = '  "D:\\照片" \nE:\\库;F:\\x, "D:\\照片",  '
        r = util.parse_roots(text)
        self.assertEqual(r, ["D:\\照片", "E:\\库", "F:\\x"])
        self.assertEqual(util.parse_roots(["A", "a", "B"]), ["A", "B"])
        self.assertEqual(util.parse_roots(""), [])
        self.assertEqual(util.parse_roots(None), [])

    def test_find_drives(self):
        d = util.find_drives()
        self.assertTrue(len(d) >= 1)


class TestIncrementalScan(FixtureMixin, unittest.TestCase):
    def test_reuse_and_change(self):
        files1, info1 = scanner.scan_roots([self.root], deep_video=False)
        self.assertEqual(info1["reused"], 0)
        self.assertEqual(info1["fresh"], 9)
        files2, info2 = scanner.scan_roots([self.root], deep_video=False,
                                           previous_files=files1)
        self.assertEqual(info2["reused"], 9)
        self.assertEqual(info2["fresh"], 0)
        # 修改一个文件 → 只重处理该文件(用固定旧时间戳,保证与扫描时的 mtime 不同)
        os.utime(os.path.join(self.a, "none.jpg"), (1_700_000_000, 1_700_000_000))
        files3, info3 = scanner.scan_roots([self.root], deep_video=False,
                                           previous_files=files2)
        self.assertEqual(info3["fresh"], 1)
        self.assertEqual(info3["reused"], 8)
        # 结果一致性:两次扫描的文件数与路径集合一致
        self.assertEqual({f["path"] for f in files2}, {f["path"] for f in files3})


class TestRecycle(FixtureMixin, unittest.TestCase):
    def test_plan_and_execute(self):
        files = self.scan()
        by = {os.path.basename(f["path"]): f for f in files}
        p1 = by["IMG_0001_copy.jpg"]["path"]
        p2 = by["Screenshot_2026-01-01.png"]["path"]
        decisions = {p1: {"action": "recycle", "target": ""},
                     p2: {"action": "archive", "target": os.path.join(self.root, "归档")}}
        plan = recycle.build_plan(files, decisions)
        self.assertEqual(len(plan), 2)
        s = recycle.plan_summary(plan)
        self.assertEqual(s["count"], 2)
        self.assertEqual(s["recycle"], 1)
        log = os.path.join(self.root, "log.csv")
        r = recycle.execute(plan, log)
        self.assertEqual(r["ok"], 2)
        self.assertTrue(os.path.exists(os.path.join(
            self.root, C.RECYCLE_DIR, "b", "IMG_0001_copy.jpg")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "归档", "Screenshot_2026-01-01.png")))
        self.assertFalse(os.path.exists(p1))
        # 二次执行:源缺失 → 跳过
        plan2 = recycle.build_plan(self.scan(), decisions)
        r2 = recycle.execute(plan2, log)
        self.assertEqual(r2["skip"], 2)


class TestServerEndToEnd(FixtureMixin, unittest.TestCase):
    def test_full_flow(self):
        app = App(home=os.path.join(self.root, ".photosshuli"))
        httpd = make_server(app, 0)
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        base = f"http://127.0.0.1:{port}"
        try:
            def post(path, body):
                req = urllib.request.Request(base + path,
                                             data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"})
                return json.load(urllib.request.urlopen(req, timeout=60))

            def get(path):
                return json.load(urllib.request.urlopen(base + path, timeout=60))

            post("/api/settings", {"roots": [self.root]})
            post("/api/scan", {})
            for _ in range(300):
                st = get("/api/scanstatus")
                if not st.get("running"):
                    break
                time.sleep(0.1)
            data = get("/api/data")
            self.assertEqual(data["stats"]["total"], 9)
            self.assertEqual(len(data["dupGroups"]), 1)
            self.assertGreaterEqual(len(data["simGroups"]), 1)
            # 决策:整组确认 → 非保留者进回收站
            gid = data["dupGroups"][0]["id"]
            post("/api/dupconfirm", {"id": gid})
            plan = get("/api/plan")
            self.assertEqual(plan["summary"]["recycle"], 2)
            res = post("/api/apply", {"execute": True})["result"]
            self.assertEqual(res["ok"], 2)
            kept = data["dupGroups"][0]["keeper"]["path"]
            self.assertTrue(os.path.exists(kept))
            recycle_dir = os.path.join(self.root, C.RECYCLE_DIR)
            self.assertTrue(os.path.isdir(recycle_dir))
            # 执行后:按目标条件轮询(等待自动增量重扫真正完成,避免启动竞态)
            moved_paths = [m["path"] for m in data["dupGroups"][0]["members"]
                           if m["path"] != kept]
            data3 = None
            for _ in range(300):
                data3 = get("/api/data")
                st = data3["scan"]
                if (not st["running"] and not st["stale"]
                        and not any(f["path"] in moved_paths for f in data3["files"])):
                    break
                time.sleep(0.1)
            self.assertFalse(data3["scan"]["stale"])
            self.assertEqual(data3["counts"]["decided"], 0)
            self.assertFalse(any(f["path"] in moved_paths for f in data3["files"]))
            self.assertTrue(any(f["path"] == kept for f in data3["files"]))
            # 目录浏览/盘符接口
            self.assertTrue(len(get("/api/drives")["drives"]) >= 1)
            br = get("/api/browse?path=" + urllib.parse.quote(self.root))
            self.assertIn("a", br["dirs"])
            self.assertIn("b", br["dirs"])
            # 自动建议
            post("/api/autosuggest", {})
            d2 = get("/api/data")
            self.assertGreater(len(d2["decisions"]), 0)
        finally:
            httpd.shutdown()
            th.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
