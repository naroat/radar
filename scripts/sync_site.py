#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_site.py —— 把页面与 JSON 数据同步到发布目录

    ../data/*.json          （抓取落盘的数据源）
    ../web/index.html       （页面源码）
              ↓ 同步
    ../site/                （发布目录，交给静态托管）
        index.html
        data/*.json

用法:
    python3 scripts/sync_site.py                       # 使用默认页面路径
    python3 scripts/sync_site.py --page /path/index.html
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")
SITE_DIR = os.path.join(ROOT, "site")
DEFAULT_PAGE = os.path.join(ROOT, "web", "index.html")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", default=DEFAULT_PAGE, help="页面源码 index.html 路径")
    ap.add_argument("--site", default=SITE_DIR)
    args = ap.parse_args()

    if not os.path.exists(args.page):
        print(json.dumps({"error": "页面不存在: %s" % args.page}, ensure_ascii=False))
        return 1

    site_data = os.path.join(args.site, "data")
    os.makedirs(site_data, exist_ok=True)
    shutil.copy2(args.page, os.path.join(args.site, "index.html"))

    copied = []
    for f in sorted(glob.glob(os.path.join(DATA_DIR, "*.json"))):
        if f.endswith(".tmp"):
            continue
        shutil.copy2(f, os.path.join(site_data, os.path.basename(f)))
        copied.append(os.path.basename(f))

    print(json.dumps({
        "site": args.site,
        "page": os.path.basename(args.page),
        "data_files": copied,
        "data_count": len([c for c in copied if c != "index.json"]),
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
