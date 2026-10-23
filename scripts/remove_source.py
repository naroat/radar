#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
remove_source.py —— 从 JSON 数据里下线某个来源

用法:
    python3 remove_source.py --source Steam              # 先看会删多少（dry run）
    python3 remove_source.py --source Steam --execute    # 真删：删条目 + 重算该日摘要 + 重建 index
    python3 remove_source.py --source Steam --date 2026-10-21 --execute   # 只处理某一天

说明：删完后该日的 summaries（速览）会按剩余条目重算，index.json 也会重建。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import radar  # noqa: E402


def as_records(items):
    """把 JSON 里的 items 还原成 build_summaries 需要的记录结构。"""
    out = []
    for it in items:
        out.append({
            "类别": {"select": it.get("category") or ""},
            "来源": {"select": it.get("source") or ""},
            "排名": {"number": it.get("rank")},
            "标题": {"text": it.get("title") or ""},
            "简介": {"text": it.get("desc") or ""},
            "热度": {"text": it.get("heat") or ""},
        })
    return out


def period_files(date_filter=None):
    out = []
    for name in sorted(os.listdir(radar.DATA_DIR)):
        if not name.endswith(".json") or name == "index.json" or name.endswith(".tmp"):
            continue
        import re
        m = re.match(r"^(\d{4}-\d{2}-\d{2})-(daily|weekly|monthly)\.json$", name)
        if not m:
            continue
        if date_filter and m.group(1) != date_filter:
            continue
        path = os.path.join(radar.DATA_DIR, name)
        doc = radar.load_day_file(path)
        if doc:
            out.append((m.group(1), m.group(2), path, doc))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="要下线的来源名，如 Steam")
    ap.add_argument("--date", default="", help="只处理这一天（默认全部日期）")
    ap.add_argument("--execute", action="store_true", help="真的写回（默认只预演）")
    args = ap.parse_args()

    src = args.source
    report = []
    for date_str, period, path, doc in period_files(args.date or None):
        items = doc.get("items") or []
        kept = [it for it in items if it.get("source") != src]
        removed = len(items) - len(kept)
        # sources 统计里去掉该来源（key 可能是「类别/来源/榜单」或纯来源名）
        old_sources = doc.get("sources")
        new_sources = None
        if isinstance(old_sources, dict):
            new_sources = {k: v for k, v in old_sources.items()
                           if k != src and not k.split("/")[1:2] == [src]
                           and not (len(k.split("/")) == 3 and k.split("/")[1] == src)}
            if new_sources == old_sources:
                new_sources = None
        if not removed and new_sources is None:
            continue
        entry = {"file": os.path.basename(path), "date": date_str,
                 "removed": removed, "kept": len(kept)}
        if args.execute:
            if removed:
                doc["items"] = kept
                doc["count"] = len(kept)
                doc["summaries"] = radar.build_summaries(as_records(kept), date_str)
            if new_sources is not None:
                doc["sources"] = new_sources
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        report.append(entry)

    if args.execute:
        radar.rebuild_index()

    print(json.dumps({"source": src, "execute": bool(args.execute),
                      "files": report,
                      "total_removed": sum(r["removed"] for r in report)},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
