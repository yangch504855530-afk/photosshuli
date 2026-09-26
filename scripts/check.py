# -*- coding: utf-8 -*-
"""photosshuli 发布门禁:测试 + 泄漏扫描 + 版本一致性。任一失败退出码非 0。
用法: python scripts/check.py [--skip-tests]"""
import io
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

FAIL = []

print("=" * 46)
print("photosshuli 发布门禁")
print("=" * 46)

# ---- 门禁1:全量测试 ----
if "--skip-tests" not in sys.argv:
    print("\n[1/3] 全量测试...")
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                       capture_output=True, text=True, timeout=600)
    tail = (r.stderr or "").strip().splitlines()[-3:]
    for ln in tail:
        print("   ", ln)
    if r.returncode != 0:
        FAIL.append("测试未全绿")
else:
    print("\n[1/3] 全量测试:跳过(--skip-tests)")

# ---- 门禁2:个人路径/密钥泄漏扫描 ----
print("\n[2/3] 泄漏扫描...")
PATTERNS = [
    (r"50485", "个人用户名目录"),
    (r"pshuli_user_sim", "本机临时目录"),
    (r"F:\\Photos", "个人媒体路径"),
    (r"C:\\Users\\[A-Za-z]", "个人用户目录"),
    (r"chenghaoyang", "个人账号名"),
    (r"sk-[A-Za-z0-9]{16,}", "疑似 API Key"),
]
SCAN_DIRS = ["photosshuli", "web", "scripts", "docs", "tests"]
scan_hits = []
for d in SCAN_DIRS:
    for dp, dns, fns in os.walk(d):
        dns[:] = [x for x in dns if x != "__pycache__"]
        for fn in fns:
            if not fn.endswith((".py", ".html", ".md", ".bat", ".yml")):
                continue
            fp = os.path.join(dp, fn)
            if os.path.normcase(os.path.abspath(fp)) == os.path.normcase(os.path.abspath(__file__)):
                continue  # 检查器自身包含模式串,跳过
            try:
                txt = io.open(fp, encoding="utf-8").read()
            except Exception:
                continue
            for pat, why in PATTERNS:
                for m in re.finditer(pat, txt):
                    ln = txt[:m.start()].count("\n") + 1
                    scan_hits.append(f"{fp}:{ln} 命中[{why}] {m.group(0)[:24]}")
if scan_hits:
    for h in scan_hits:
        print("   ✗", h)
    FAIL.append(f"泄漏扫描命中 {len(scan_hits)} 处")
else:
    print("    ✓ 干净(无个人路径/密钥)")

# ---- 门禁3:版本一致性 ----
print("\n[3/3] 版本一致性...")
from photosshuli import config  # noqa: E402
ver = config.VERSION
print(f"    config.VERSION = {ver}")
if not re.match(r"^\d+\.\d+\.\d+$", ver):
    FAIL.append(f"版本号格式异常: {ver}")
readme = io.open("README.md", encoding="utf-8").read()
if f"v{ver}" not in readme:
    FAIL.append(f"README 未包含 v{ver} 标记")
tags = set(subprocess.run(["git", "tag"], capture_output=True, text=True).stdout.split())
remote = subprocess.run(["git", "ls-remote", "--tags", "origin"],
                        capture_output=True, text=True).stdout
remote_tags = {ln.split("/")[-1] for ln in remote.splitlines() if ln.strip()}
tags |= remote_tags
if f"v{ver}" in tags:
    FAIL.append(f"标签 v{ver} 已存在(本地或远端),请先升版本号")
else:
    print(f"    ✓ v{ver} 标签可用(已核对远端)")

# ---- 结果 ----
print("\n" + "=" * 46)
if FAIL:
    print(f"门禁未通过({len(FAIL)}):")
    for f in FAIL:
        print("  ✗", f)
    sys.exit(1)
print("门禁全部通过 ✓  可以发布。")
print("=" * 46)
