# -*- coding: utf-8 -*-
"""命令行入口:
  python -m photosshuli server [--port 8630]
  python -m photosshuli scan <目录...>
  python -m photosshuli plan
  python -m photosshuli apply [--execute]
"""
import argparse
import os

from . import config as C
from . import scanner, recycle
from .store import Store


def main():
    ap = argparse.ArgumentParser(prog="photosshuli", description="照片视频整理工具")
    ap.add_argument("--home", default=None, help="数据目录(默认 ./.photosshuli)")
    sub = ap.add_subparsers(dest="cmd")
    sp = sub.add_parser("server", help="启动 Web 界面")
    sp.add_argument("--port", type=int, default=C.DEFAULT_PORT)
    sp.add_argument("--lan", action="store_true",
                    help="允许局域网访问(默认仅本机 127.0.0.1)")
    ss = sub.add_parser("scan", help="扫描目录建立索引")
    ss.add_argument("roots", nargs="+")
    splan = sub.add_parser("plan", help="预览将执行的移动")
    sap = sub.add_parser("apply", help="执行移动(--execute 才真正执行)")
    sap.add_argument("--execute", action="store_true")
    args = ap.parse_args()

    store = Store(args.home)
    if args.cmd == "server":
        from . import server
        server.main(port=args.port, home=args.home, lan=args.lan)
    elif args.cmd == "scan":
        files, _ = scanner.scan_roots(args.roots)
        store.save_index(files, [os.path.abspath(r) for r in args.roots])
        print(f"扫描完成: {len(files)} 个文件 → {store.index_path}")
    elif args.cmd == "plan":
        files, _ = store.load_index()
        plan = recycle.build_plan(files, store.load_decisions())
        print(recycle.plan_summary(plan))
        for p in plan[:50]:
            print(f"  [{p['action']}] {p['path']} -> {p['dst']}")
        if len(plan) > 50:
            print(f"  ... 共 {len(plan)} 项")
    elif args.cmd == "apply":
        files, _ = store.load_index()
        plan = recycle.build_plan(files, store.load_decisions())
        print("预览:", recycle.plan_summary(plan))
        if not args.execute:
            print("未执行。加 --execute 真正执行(移动进回收站目录,可找回)。")
            return
        r = recycle.execute(plan, os.path.join(store.dir, "applied_log.csv"))
        print("执行结果:", r)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
