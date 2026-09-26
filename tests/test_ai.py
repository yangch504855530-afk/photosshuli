# -*- coding: utf-8 -*-
"""AI 功能测试:全部离线(fake transport / monkeypatch),不打真实 API"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from photosshuli import ai as aimod  # noqa: E402
from photosshuli.server import App, make_server  # noqa: E402

from test_engine import FixtureMixin, make_jpeg  # noqa: E402


class FakeTransport:
    """按 prompt 关键字返回罐头响应,记录调用"""
    def __init__(self):
        self.calls = []

    def __call__(self, url, payload, timeout):
        self.calls.append(payload)
        prompt = payload["messages"][0]["content"][-1]["text"]
        if "最好的一张" in prompt:
            body = '{"best": 1, "reason": "第二张最清晰"}'
        elif "照片整理助手" in prompt:
            body = json.dumps([
                {"i": i, "category": "风景", "tags": ["湖", "山"],
                 "quality": "无" if i % 2 == 0 else "模糊",
                 "suggest": "保留" if i % 2 == 0 else "可删"}
                for i in range(6)])
        elif "地名" in prompt:
            body = '{"name": "洱海"}'
        else:
            body = "{}"
        return {"choices": [{"message": {"content": f"```json\n{body}\n```"}}]}


def fake_client(app_transport=None):
    return aimod.AIClient("test-key", transport=app_transport or FakeTransport())


class TestAIClient(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ai_ut_")
        self.paths = [make_jpeg(os.path.join(self.tmp, f"im{i}.jpg")) for i in range(6)]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rank(self):
        cli = fake_client()
        r = cli.rank_group([(p, f"im{i}.jpg") for i, p in enumerate(self.paths[:3])])
        self.assertEqual(r["best"], 1)
        self.assertIn("清晰", r["reason"])

    def test_rank_best_clamped(self):
        cli = aimod.AIClient("k", transport=FakeTransport())
        # 罐头返回 best=1,单图组越界 → 回退 0
        r = cli.rank_group([(self.paths[0], "im0.jpg")])
        self.assertEqual(r["best"], 0)

    def test_tag_batch(self):
        cli = fake_client()
        rows = cli.tag_batch([(p, f"im{i}.jpg") for i, p in enumerate(self.paths)])
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["category"], "风景")
        self.assertEqual(rows[1]["suggest"], "可删")

    def test_name_cluster_sanitized(self):
        cli = aimod.AIClient("k", transport=FakeTransport())
        name = cli.name_cluster([(self.paths[0], "im0.jpg")], "+30.19,+120.23")
        self.assertEqual(name, "洱海")

    def test_extract_json_fences(self):
        self.assertEqual(aimod._extract_json('```json\n{"a":1}\n```'), {"a": 1})
        self.assertEqual(aimod._extract_json('前言 [ {"i":0} ] 后记'), [{"i": 0}])

    def test_b64(self):
        tmp = tempfile.mkdtemp()
        p = make_jpeg(os.path.join(tmp, "a.jpg"))
        b = aimod.b64_of_image(p)
        self.assertTrue(len(b) > 100)
        self.assertEqual(aimod.b64_of_image(os.path.join(tmp, "no.jpg")), None)


class TestAIServer(FixtureMixin, unittest.TestCase):
    def test_ai_flow(self):
        app = App(home=os.path.join(self.root, ".photosshuli"))
        app.settings.update({"ai_consent": True, "ai_key": "test",
                             "ai_daily_cap": 3})
        fake = FakeTransport()
        app.make_ai_client = lambda: aimod.AIClient("test", transport=fake)
        httpd = make_server(app, 0)
        port = httpd.server_address[1]
        import threading
        import time as _t
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
                _t.sleep(0.1)
            data = get("/api/data")
            # 状态
            st = get("/api/ai/status")
            self.assertTrue(st["ready"])
            # AI 择优:组内最佳改写为模型选择
            gid = data["simGroups"][0]["id"]
            old_keeper = data["simGroups"][0]["recommended"]["path"]
            r = post("/api/ai/rank", {"id": gid})
            self.assertTrue(r["ok"])
            self.assertIn("清晰", r["reason"])
            data2 = get("/api/data")
            g2 = next(x for x in data2["simGroups"] if x["id"] == gid)
            self.assertNotEqual(g2["recommended"]["path"], old_keeper)  # fake 选第2张
            # AI 打标:挑两张相机照片,并给它们互不相同的 mtime(避免 size:mtime 键撞车)
            cams = [f["path"] for f in data2["files"] if f["cls"] == "相机照片"][:2]
            for j, cp in enumerate(cams):
                os.utime(cp, (1_700_000_100 + j, 1_700_000_100 + j))
            r = post("/api/ai/tag", {"paths": cams})
            self.assertEqual(r["applied"], 2)
            self.assertEqual(r["calls"], 1)  # 2 张 → 1 个批次
            data3 = get("/api/data")
            withai = [f for f in data3["files"] if f.get("ai")]
            self.assertEqual(len(withai), 2)
            self.assertEqual({f["path"] for f in withai}, set(cams))
            self.assertEqual(withai[0]["ai"]["category"], "风景")
            # AI 命名地点簇(夹具照片无 GPS,注入一个簇用于接口验证)
            cluster_key = "+30.19,+120.23"
            app.files[0]["cluster"] = cluster_key
            app.files[1]["cluster"] = cluster_key
            r = post("/api/ai/namecluster", {"key": cluster_key})
            self.assertEqual(r["name"], "洱海")
            self.assertEqual(get("/api/data")["settings"]["cluster_names"][cluster_key], "洱海")
            # 日限额:cap=3,rank1+tag1+命名1 已用满 → 第 4 次应被拒
            try:
                post("/api/ai/namecluster", {"key": cluster_key})
                cap_rejected = False
            except urllib.error.HTTPError as e:
                cap_rejected = e.code == 400
            self.assertTrue(cap_rejected, "超过日限额应返回 400")
            try:
                post("/api/ai/rank", {"id": gid})
                raised = False
            except urllib.error.HTTPError as e:
                raised = e.code == 400
            self.assertTrue(raised, "超过日限额应返回 400")
            # 未授权拒绝:关掉 consent
            post("/api/settings", {"ai_consent": False})
            try:
                post("/api/ai/tag", {"paths": cams})
                raised2 = False
            except urllib.error.HTTPError as e:
                raised2 = e.code == 400
            self.assertTrue(raised2, "未同意时应返回 400")
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
