# -*- coding: utf-8 -*-
"""回收站执行:决策 → 计划预览 → 执行移动(同盘移入 回收站目录,可找回)"""
import csv
import io
import os
import shutil
import time

from . import config as C
from . import util


def build_plan(files, decisions):
    """files: 索引记录列表;decisions: {path:{action,target}}。
    返回 plan 条目列表 + 汇总。action: recycle / archive / keep(忽略)"""
    by_path = {f["path"]: f for f in files}
    plan = []
    for path, dec in decisions.items():
        action = dec.get("action", "")
        rec = by_path.get(path)
        src = path if os.path.exists(path) else None
        if action == "recycle":
            root = (rec or {}).get("root") or os.path.splitdrive(path)[0] + os.sep
            rel = (rec or {}).get("rel") or os.path.basename(path)
            dst = os.path.join(root, C.RECYCLE_DIR, rel.replace("/", os.sep))
        elif action == "archive":
            dst = os.path.join(dec.get("target") or "", os.path.basename(path))
        else:
            continue
        plan.append({"path": path, "src": src, "dst": dst, "action": action,
                     "size": (rec or {}).get("size", 0)})
    return plan


def plan_summary(plan):
    ok = [p for p in plan if p["src"]]
    miss = [p for p in plan if not p["src"]]
    bytes_ = sum(p["size"] for p in ok)
    return {"count": len(plan), "missing": len(miss),
            "bytes": bytes_, "recycle": sum(1 for p in plan if p["action"] == "recycle"),
            "archive": sum(1 for p in plan if p["action"] == "archive")}


def unique_path(p):
    if not os.path.exists(p):
        return p
    b, e = os.path.splitext(p)
    for i in range(1, 999):
        q = f"{b}_{i}{e}"
        if not os.path.exists(q):
            return q
    return p + ".dup"


def execute(plan, log_path):
    """执行移动。返回 {ok, fail, skip}"""
    res = {"ok": 0, "fail": 0, "skip": 0, "moved": []}
    logf = io.open(log_path, "a", encoding="utf-8-sig", newline="")
    w = csv.writer(logf)
    if logf.tell() == 0:
        w.writerow(["时间", "路径", "动作", "源", "目标", "结果"])
    for item in plan:
        t = util.ts()
        if not item["src"]:
            w.writerow([t, item["path"], item["action"], "", item["dst"], "源缺失·跳过"])
            res["skip"] += 1
            continue
        if os.path.normcase(os.path.abspath(item["src"])) == os.path.normcase(os.path.abspath(item["dst"])):
            w.writerow([t, item["path"], item["action"], item["src"], item["dst"], "原地·跳过"])
            res["skip"] += 1
            continue
        last_err = None
        for attempt in range(3):
            try:
                os.makedirs(os.path.dirname(item["dst"]), exist_ok=True)
                shutil.move(item["src"], unique_path(item["dst"]))
                w.writerow([t, item["path"], item["action"], item["src"], item["dst"], "OK"])
                res["ok"] += 1
                res["moved"].append(item["path"])
                last_err = None
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < 2:
                    time.sleep(1.0)  # 文件可能被缩略图/播放器等短暂占用,稍候重试
        if last_err is not None:
            w.writerow([t, item["path"], item["action"], item["src"], item["dst"], f"失败:{last_err}"])
            res["fail"] += 1
    logf.close()
    return res


def find_restorable(log_path):
    """从执行日志解析可还原项:结果=OK 的移动,且目标文件还在原处。
    返回 [{path(现位置), src(原位置), action, time}]"""
    out = []
    if not os.path.exists(log_path):
        return out
    with io.open(log_path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("结果") != "OK" or row.get("动作") not in ("recycle", "archive"):
                continue
            dst, src = row.get("目标") or "", row.get("源") or ""
            if dst and src and os.path.exists(dst) and not os.path.exists(src):
                out.append({"path": dst, "src": src,
                            "action": row.get("动作"), "time": row.get("时间")})
    out.reverse()  # 最近的在前
    return out


def restore(items, log_path):
    """把文件移回原位。返回 {ok, fail, skip}"""
    res = {"ok": 0, "fail": 0, "skip": 0}
    logf = io.open(log_path, "a", encoding="utf-8-sig", newline="")
    w = csv.writer(logf)
    if logf.tell() == 0:
        w.writerow(["时间", "路径", "动作", "源", "目标", "结果"])
    for it in items:
        t = util.ts()
        src, dst = it["path"], it["src"]
        try:
            if not os.path.exists(src):
                w.writerow([t, src, "还原", src, dst, "源缺失·跳过"])
                res["skip"] += 1
                continue
            if os.path.exists(dst):
                w.writerow([t, src, "还原", src, dst, "原位已占用·跳过"])
                res["skip"] += 1
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
            w.writerow([t, src, "还原", src, dst, "OK"])
            res["ok"] += 1
        except Exception as e:  # noqa: BLE001
            w.writerow([t, src, "还原", src, dst, f"失败:{e}"])
            res["fail"] += 1
    logf.close()
    return res
