# -*- coding: utf-8 -*-
"""v0.1.7 测试:文件名日期兜底 / 统一归档库 / 相似度距离+MD5证据 / 停止扫描"""
import io
import json
import os
import sys
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from photosshuli import classify, util  # noqa: E402
from photosshuli.server import App, make_server  # noqa: E402

from test_engine import FixtureMixin  # noqa: E402


class TestDateFromName(unittest.TestCase):
    def test_patterns(self):
        self.assertEqual(util.date_from_name("IMG_20230611_123456.jpg"), "2023-06-11")
        self.assertEqual(util.date_from_name("Screenshot_20260101-121314.png"), "2026-01-01")
        self.assertEqual(util.date_from_name("WX20230611-220015.jpg"), "2023-06-11")
        self.assertEqual(util.date_from_name("2023-06-11 出游.jpg"), "2023-06-11")
        self.assertEqual(util.date_from_name("2023.06.11.jpg"), "2023-06-11")
        self.assertEqual(util.date_from_name("IMG_20231399.jpg"), "")   # 非法月日
        self.assertEqual(util.date_from_name("无日期.jpg"), "")


class TestNameDateFallback(unittest.TestCase):
    def R(self, name, mtime_dt="2026-09-26"):
        return {"cls": "无元数据图片", "rel": name, "dt": "", "lat": "", "lon": "",
                "mtime_dt": mtime_dt, "cluster": "", "name": name,
                "path": os.path.join("R", name)}

    def test_name_date_beats_mtime(self):
        act, tgt, why = classify.suggest(
            self.R("IMG_20230611_123456.jpg"), {}, "R")
        self.assertEqual(act, "archive")
        self.assertEqual(tgt, os.path.join("R", "2023"))
        self.assertIn("文件名日期", why)

    def test_archive_root(self):
        rec = {"cls": "相机照片", "rel": "a.jpg", "dt": "2026-02-10 10:00",
               "lat": "+30.19", "lon": "+120.23", "mtime_dt": "2026-02-10",
               "cluster": "+30.19,+120.23", "name": "a.jpg",
               "path": os.path.join("D盘", "a.jpg")}
        act, tgt, _ = classify.suggest(rec, {"+30.19,+120.23": "杭州"}, "D盘",
                                       archive_root="E:\主库")
        self.assertTrue(tgt.startswith("E:\主库"), tgt)
        self.assertIn("旅行", tgt)
        # 无主库时在各自根内
        act2, tgt2, _ = classify.suggest(rec, {"+30.19,+120.23": "杭州"}, "D盘")
        self.assertTrue(tgt2.startswith("D盘"), tgt2)


class TestEvidenceAndCancel(FixtureMixin, unittest.TestCase):
    def test_md5_and_dists_and_cancel(self):
        app = App(home=os.path.join(self.root, ".photosshuli"))
        httpd = make_server(app, 0)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"

        def post(path, body):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=60))

        def get(path):
            return json.load(urllib.request.urlopen(base + path, timeout=60))

        try:
            post("/api/scan", {"roots": [self.root], "force_full": True})
            while get("/api/scanstatus")["running"]:
                time.sleep(0.1)
            data = get("/api/data")
            # 重复组带 MD5 证据
            dg = data["dupGroups"][0]
            self.assertEqual(len(dg["hash"]), 32)
            # 相似组带相似度距离
            sg = data["simGroups"][0]
            self.assertTrue(all(0 <= v <= 64 for v in sg["dists"].values()))
            self.assertEqual(sg["dists"][sg["recommended"]["path"]], 0)
            # 停止扫描:端点可用
            r = post("/api/scan/cancel", {})
            self.assertTrue(r["ok"])
            # 引擎级取消:第 6 个文件后触发 → 只处理少量文件即返回
            from photosshuli import scanner
            n = {"i": 0}
            def cancel():
                n["i"] += 1
                return n["i"] > 5
            files, info = scanner.scan_roots([self.root], deep_video=False, cancel=cancel)
            self.assertLess(len(files), data["stats"]["total"])
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
