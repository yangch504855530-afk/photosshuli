# -*- coding: utf-8 -*-
"""v0.1.4 新增行为测试:确认尊重保留决策 / 已归档不再建议 / 簇改名同步 / 执行后自动重扫 / AI 测连接"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from photosshuli import classify  # noqa: E402
from photosshuli.server import App, make_server  # noqa: E402
from photosshuli import ai as aimod  # noqa: E402

from test_engine import FixtureMixin  # noqa: E402


def post(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=120))


def get(base, path):
    return json.load(urllib.request.urlopen(base + path, timeout=120))


class TestAlreadyArchived(unittest.TestCase):
    def test_keep_when_inside_target(self):
        root = "R"
        rec = {"cls": "相机照片", "rel": r"旅行\杭州\2026-02 杭州\IMG.jpg",
               "dt": "2026-02-10 10:00", "lat": "+30.19", "lon": "+120.23",
               "mtime_dt": "2026-02-10", "cluster": "+30.19,+120.23",
               "path": os.path.join(root, "旅行", "杭州", "2026-02 杭州", "IMG.jpg")}
        act, tgt, why = classify.suggest(rec, {"+30.19,+120.23": "杭州"}, root)
        self.assertEqual(act, "keep")
        self.assertIn("已在归档位置", why)
        # 不在目标内仍建议归档
        rec2 = dict(rec, rel=r"照片\IMG.jpg",
                    path=os.path.join(root, "照片", "IMG.jpg"))
        act2, _, _ = classify.suggest(rec2, {"+30.19,+120.23": "杭州"}, root)
        self.assertEqual(act2, "archive")
        # 行车记录仪同理
        rec3 = {"cls": "行车记录仪", "rel": r"行车记录仪\2026\hiv.mp4", "dt": "",
                "mtime_dt": "2026-01-01", "path": os.path.join(root, "行车记录仪", "2026", "hiv.mp4")}
        act3, _, why3 = classify.suggest(rec3, {}, root)
        self.assertEqual(act3, "keep")


class TestConfirmRespectsKeep(FixtureMixin, unittest.TestCase):
    def test_dup_and_sim(self):
        app = App(home=os.path.join(self.root, ".photosshuli"))
        httpd = make_server(app, 0)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        try:
            post(base, "/api/scan", {"roots": [self.root], "force_full": True})
            while get(base, "/api/scanstatus")["running"]:
                time.sleep(0.1)
            data = get(base, "/api/data")
            # 相似组:保留第2张,再确认 → 第2张必须是 keep
            g = data["simGroups"][0]
            keep_path = g["members"][1]["path"]
            post(base, "/api/decide", {"path": keep_path, "action": "keep"})
            post(base, "/api/simconfirm", {"id": g["id"]})
            d = get(base, "/api/data")["decisions"]
            self.assertEqual(d[keep_path]["action"], "keep")
            others = [m["path"] for m in g["members"][2:]]
            for p in others:
                self.assertEqual(d[p]["action"], "recycle")
            # 重复组:同样规则
            dg = data["dupGroups"][0]
            dkeep = dg["members"][-1]["path"]
            post(base, "/api/decide", {"path": dkeep, "action": "keep"})
            post(base, "/api/dupconfirm", {"id": dg["id"]})
            d2 = get(base, "/api/data")["decisions"]
            self.assertEqual(d2[dkeep]["action"], "keep")
        finally:
            httpd.shutdown()

    def test_cluster_rename_updates_decisions(self):
        app = App(home=os.path.join(self.root, ".photosshuli"))
        httpd = make_server(app, 0)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        try:
            post(base, "/api/scan", {"roots": [self.root], "force_full": True})
            while get(base, "/api/scanstatus")["running"]:
                time.sleep(0.1)
            k = "+30.19,+120.23"
            post(base, "/api/settings", {"cluster_names": {k: "旧名"}})
            f0 = get(base, "/api/data")["files"][0]["path"]
            post(base, "/api/decide", {"path": f0, "action": "archive",
                                       "target": os.path.join(self.root, "旅行", "旧名", "2026-02 旧名")})
            post(base, "/api/settings", {"cluster_names": {k: "新名"}})
            d = get(base, "/api/data")["decisions"][f0]
            self.assertNotIn("旧名", d["target"])
            self.assertIn("新名", d["target"])
        finally:
            httpd.shutdown()

    def test_auto_rescan_after_execute(self):
        app = App(home=os.path.join(self.root, ".photosshuli"))
        httpd = make_server(app, 0)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        try:
            post(base, "/api/settings", {"roots": [self.root]})
            post(base, "/api/scan", {"roots": [self.root], "force_full": True})
            while get(base, "/api/scanstatus")["running"]:
                time.sleep(0.1)
            data = get(base, "/api/data")
            p = data["files"][0]["path"]
            post(base, "/api/decide", {"path": p, "action": "recycle"})
            res = post(base, "/api/apply", {"execute": True})
            self.assertEqual(res["result"]["ok"], 1)
            # 自动增量重扫:按条件轮询(索引中不再出现该文件)
            for _ in range(300):
                d2 = get(base, "/api/data")
                if (not d2["scan"]["running"] and not d2["scan"]["stale"]
                        and not any(f["path"] == p for f in d2["files"])):
                    break
                time.sleep(0.1)
            self.assertFalse(d2["scan"]["stale"])
            self.assertFalse(os.path.exists(p))
        finally:
            httpd.shutdown()


class TestAIPing(FixtureMixin, unittest.TestCase):
    def test_ping(self):
        app = App(home=os.path.join(self.root, ".photosshuli"))
        app.settings.update({"ai_consent": True, "ai_key": "test"})
        fake = aimod.AIClient("test", transport=lambda url, payload, timeout:
                              {"choices": [{"message": {"content": "正常"}}]})
        app.make_ai_client = lambda: fake
        httpd = make_server(app, 0)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"
        try:
            post(base, "/api/scan", {"roots": [self.root], "force_full": True})
            while get(base, "/api/scanstatus")["running"]:
                time.sleep(0.1)
            r = post(base, "/api/ai/ping", {})
            self.assertTrue(r["ok"])
            self.assertIn("正常", r["reply"])
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
