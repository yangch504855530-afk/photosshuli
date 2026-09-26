# -*- coding: utf-8 -*-
"""v0.1.6 测试:RAW 识别 / 分桶相似分组等价性 / 回收站还原 / 决策导入"""
import io
import json
import os
import random
import sys
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from photosshuli import config as C  # noqa: E402
from photosshuli import recycle, similar, util  # noqa: E402
from photosshuli.server import App, make_server  # noqa: E402

from test_engine import FixtureMixin  # noqa: E402


class TestRAW(FixtureMixin, unittest.TestCase):
    def test_raw_recognized(self):
        p = os.path.join(self.a, "DSC_0001.CR2")
        with open(p, "wb") as f:
            f.write(b"II*\x00" + os.urandom(1000))
        files = self.scan()
        rec = next(f for f in files if f["name"] == "DSC_0001.CR2")
        self.assertEqual(rec["kind"], "raw")
        self.assertEqual(rec["cls"], C.CLS_RAW)


class TestBucketGrouping(unittest.TestCase):
    @staticmethod
    def _bruteforce(hashes, threshold):
        n = len(hashes)
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(n):
            for j in range(i + 1, n):
                if util.hamming(hashes[i], hashes[j]) <= threshold:
                    ra, rb = find(i), find(j)
                    if ra != rb:
                        parent[rb] = ra
        groups = {}
        for i in range(n):
            groups.setdefault(find(i), set()).add(i)
        return {frozenset(g) for g in groups.values() if len(g) > 1}

    def test_bucket_equals_bruteforce(self):
        rnd = random.Random(11)
        hashes = [f"{rnd.getrandbits(64):016x}" for _ in range(400)]
        # 注入 3 个簇:基准哈希 + 若干 ≤8 位翻转变体
        base = int(hashes[0], 16)
        for k in range(1, 9):
            bits = rnd.sample(range(64), k)
            v = base
            for b in bits:
                v ^= 1 << b
            hashes.append(f"{v:016x}")
        files = [{"path": str(i), "dhash": h, "score": rnd.random(), "size": 1}
                 for i, h in enumerate(hashes)]
        got = {frozenset(int(f["path"]) for f in g)
               for g in similar.group_similar(files, threshold=8)}
        want = self._bruteforce(hashes, 8)
        self.assertEqual(got, want)
        # 分桶应当真的省了比对:候选对数远小于全对
        pairs = similar._candidate_pairs(hashes, 8)
        self.assertIsNotNone(pairs)
        self.assertLess(len(pairs), len(hashes) * (len(hashes) - 1) // 2 / 10)

    def test_threshold_over_8_fallback(self):
        rnd = random.Random(3)
        hashes = [f"{rnd.getrandbits(64):016x}" for _ in range(50)]
        files = [{"path": str(i), "dhash": h, "score": 0, "size": 1}
                 for i, h in enumerate(hashes)]
        got = {frozenset(int(f["path"]) for f in g)
               for g in similar.group_similar(files, threshold=10)}
        want = self._bruteforce(hashes, 10)
        self.assertEqual(got, want)


class TestRestore(FixtureMixin, unittest.TestCase):
    def test_restore_flow(self):
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
            victim = next(f["path"] for f in data["files"]
                          if f["name"] == "none.jpg")
            post("/api/decide", {"path": victim, "action": "recycle"})
            post("/api/apply", {"execute": True})
            self.assertFalse(os.path.exists(victim))
            # 可还原列表包含它
            items = get("/api/restore/list")["items"]
            hit = next(x for x in items if x["src"] == victim)
            self.assertEqual(hit["action"], "recycle")
            # 还原
            r = post("/api/restore", {"items": [hit]})
            self.assertEqual(r["result"]["ok"], 1)
            self.assertTrue(os.path.exists(victim))
            # 重复还原 → 跳过
            r2 = post("/api/restore", {"items": [hit]})
            self.assertEqual(r2["result"]["skip"], 1)
            # 还原后索引同步(自动重扫)
            for _ in range(200):
                if not get("/api/scanstatus")["running"]:
                    break
                time.sleep(0.1)
            data2 = get("/api/data")
            self.assertTrue(any(f["path"] == victim for f in data2["files"]))
        finally:
            httpd.shutdown()


class TestImportDecisions(FixtureMixin, unittest.TestCase):
    def test_import_csv(self):
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
            f1 = data["files"][0]["path"]
            f2 = data["files"][1]["path"]
            csv_text = ("路径,动作,目标,大小,内容类别,拍摄时间\n"
                        f"{f1},recycle,,100,相机照片,\n"
                        f"{f2},archive,{os.path.join(self.root, '2026')},1,x,\n"
                        "not-a-real-path,recycle,,1,x,\n")
            r = post("/api/decide/import", {"csv": csv_text})
            self.assertEqual(r["imported"], 3)
            d = get("/api/data")["decisions"]
            self.assertEqual(d[f1]["action"], "recycle")
            self.assertEqual(d[f2]["target"], os.path.join(self.root, "2026"))
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
