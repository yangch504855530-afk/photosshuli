# -*- coding: utf-8 -*-
"""状态持久化:索引、设置、决策(Atomic JSON)"""
import io
import json
import os

from . import config as C


def data_dir(home=None):
    d = home or os.environ.get("PHOTOSHULI_HOME") or os.path.join(os.getcwd(), ".photosshuli")
    os.makedirs(d, exist_ok=True)
    return d


def _write_json(path, obj):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


class Store:
    def __init__(self, home=None):
        self.dir = data_dir(home)
        self.index_path = os.path.join(self.dir, "index.json")
        self.settings_path = os.path.join(self.dir, "settings.json")
        self.decisions_path = os.path.join(self.dir, "decisions.json")

    # ---- settings ----
    def load_settings(self):
        s = _read_json(self.settings_path, {})
        s.setdefault("roots", [])
        s.setdefault("priorities", [])
        s.setdefault("similar_threshold", C.DEFAULT_SIMILAR_THRESHOLD)
        s.setdefault("cluster_names", {})
        s.setdefault("deep_video", True)
        return s

    def save_settings(self, s):
        _write_json(self.settings_path, s)

    # ---- index ----
    def save_index(self, files, roots):
        _write_json(self.index_path, {"scanned_at": util_now(), "roots": roots, "files": files})

    def load_index(self):
        d = _read_json(self.index_path, None)
        return (d or {}).get("files", []), (d or {}).get("roots", [])

    # ---- decisions ----
    def load_decisions(self):
        return _read_json(self.decisions_path, {})

    def save_decisions(self, d):
        _write_json(self.decisions_path, d)

    def decide(self, path, action, target=""):
        d = self.load_decisions()
        if action:
            d[path] = {"action": action, "target": target, "time": util_now()}
        else:
            d.pop(path, None)
        self.save_decisions(d)
        return len(d)


def util_now():
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")
