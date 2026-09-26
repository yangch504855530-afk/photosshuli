# -*- coding: utf-8 -*-
"""v0.1.8 测试:云占位符识别 / 目录级对比 / 整理报告"""
import io
import json
import os
import shutil
import sys
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from photosshuli.server import App, make_server  # noqa: E402

from test_engine import FixtureMixin, make_jpeg  # noqa: E402


def post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=120))


def get(base, path):
    return json.load(urllib.request.urlopen(base + path, timeout=120))


class TestCloudLogic(unittest.TestCase):
    class FakeStat:
        st_file_attributes = 0x401000  # OFFLINE | RECALL_ON_DATA_ACCESS

    class FakeNormal:
        st_file_attributes = 0

    def test_logic(self):
        from photosshuli import scanner
        if os.name == "nt":
            self.assertTrue(scanner.is_cloud_placeholder("x", self.FakeStat()))
            self.assertFalse(scanner.is_cloud_placeholder("x", self.FakeNormal()))


@unittest.skipUnless(os.name == "nt", "云占位属性仅 Windows")
class TestCloudPlaceholder(FixtureMixin, unittest.TestCase):
    def test_placeholder_skipped(self):
        import ctypes
        p = make_jpeg(os.path.join(self.a, "cloud_IMG.jpg"))
        FILE_ATTRIBUTE_OFFLINE = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.SetFileAttributesW(p, FILE_ATTRIBUTE_OFFLINE)
        # 系统可能不接受用户态设置的占位属性;属性没粘住就跳过集成断言
        fa = os.stat(p).st_file_attributes
        if not (fa & 0x1000):
            self.skipTest("本机 NTFS 不保留手动设置的 OFFLINE 属性")
        try:
            files = self.scan()
            rec = next(f for f in files if f["name"] == "cloud_IMG.jpg")
            self.assertTrue(rec["cloud"])
            self.assertEqual(rec["dhash"], "")  # 未读取内容
            # 占位文件不参与精确去重(避免触发下载)
            shutil.copyfile(p, os.path.join(self.b, "cloud_IMG_copy.jpg"))
            files2 = self.scan()
            from photosshuli import duplicates
            dups = [g for g in duplicates.find_exact_dups(files2)
                    if any(m["cloud"] for m in g["members"])]
            self.assertEqual(dups, [])
        finally:
            kernel32.SetFileAttributesW(p, 128)  # NORMAL


class TestCompareAndReport(FixtureMixin, unittest.TestCase):
    def test_compare_endpoint(self):
        dirA = os.path.join(self.root, "欧洲行")
        dirB = os.path.join(self.root, "欧洲行-整理")
        os.makedirs(dirA)
        os.makedirs(dirB)
        # 两边一致 2 张;仅A 1 张;仅B 1 张
        same = make_jpeg(os.path.join(dirA, "same1.jpg"))
        shutil.copyfile(same, os.path.join(dirB, "same1.jpg"))
        same2 = make_jpeg(os.path.join(dirA, "same2.jpg"), size=(300, 200))
        shutil.copyfile(same2, os.path.join(dirB, "same2.jpg"))
        make_jpeg(os.path.join(dirA, "only_a.jpg"), size=(100, 100))
        make_jpeg(os.path.join(dirB, "only_b.jpg"), size=(120, 90))
        app = App(home=os.path.join(self.root, ".photosshuli"))
        httpd = make_server(app, 0)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        try:
            r = post(base, "/api/compare", {"a": dirA, "b": dirB})
            self.assertEqual(r["matched"], 2)
            self.assertEqual(r["aOnlyCount"], 1)
            self.assertEqual(r["bOnlyCount"], 1)
            self.assertGreater(r["matchedBytes"], 0)
            # 报告端点
            post(base, "/api/scan", {"roots": [self.root], "force_full": True})
            for _ in range(200):
                if not get(base, "/api/scanstatus")["running"]:
                    break
                time.sleep(0.1)
            rep = urllib.request.urlopen(base + "/api/report", timeout=60).read().decode("utf-8")
            self.assertIn("photosshuli 整理报告", rep)
            self.assertIn("内容构成", rep)
            self.assertIn("精确重复组", rep)
            self.assertIn("决策统计", rep)
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
