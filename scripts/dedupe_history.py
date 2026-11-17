#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dedupe_history.py —— 对历史数据补做增量去重

radar.py 从某天起写入时就会剔除「最近窗口内已上榜过」的条目，但更早的历史文件
里仍保留着大量重复（GitHub trending 这类每天老面孔）。本脚本按日期从早到晚
重放一遍同样的规则：同一条目只保留它最早出现的那一天，后面的天数里删掉。

默认只预演（打印会删多少），加 --execute 才真正改写 data/*.json。

用法:
    python3 scripts/dedupe_history.py                     # 预演
    python3 scripts/dedupe_history.py --execute           # 实际写入
    python3 scripts/dedupe_history.py --days 3            # 用 3 天窗口预演
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import radar  # noqa: E402

DATA_DIR = radar.DATA_DIR
PERIODS = ("daily", "weekly", "monthly")


def scan_files():
    """返回 {period: [(date, path), ...]}，按日期升序。"""
    out = {p: [] for p in PERIODS}
    for name in os.listdir(DATA_DIR):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})-(daily|weekly|monthly)\.json$", name)
        if not m:
            continue
        out[m.group(2)].append((m.group(1), os.path.join(DATA_DIR, name)))
    for p in PERIODS:
        out[p].sort()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="daily 的去重窗口天数")
    ap.add_argument("--execute", action="store_true", help="实际写入（默认只预演）")
    args = ap.parse_args()

    radar.DEDUPE_DAYS["daily"] = args.days
    radar.DEDUPE_DAYS["weekly"] = max(0, args.days * 4)
    radar.DEDUPE_DAYS["monthly"] = max(0, args.days * 12)

    files = scan_files()
    report = []
    total_before = total_after = 0

    for period in PERIODS:
        # 逐日推进：seen[sig] = 最早出现的日期
        seen = {}
        for date_str, path in files[period]:
            doc = radar.load_day_file(path)
            if not doc or not isinstance(doc.get("items"), list):
                continue
            before = len(doc["items"])
            kept = []
            for it in doc["items"]:
                if not isinstance(it, dict):
                    continue
                sig = radar._item_sig(it)
                if not sig:
                    kept.append(it)
                    continue
                # 窗口内出现过就直接丢掉
                first = seen.get(sig)
                if first:
                    try:
                        gap = (datetime.date.fromisoformat(date_str)
                               - datetime.date.fromisoformat(first)).days
                    except ValueError:
                        gap = 0
                    if 0 < gap <= radar.DEDUPE_DAYS[period]:
                        continue
                seen[sig] = first or date_str
                kept.append(it)

            total_before += before
            total_after += len(kept)
            if len(kept) != before:
                report.append({"file": os.path.basename(path),
                               "before": before, "after": len(kept),
                               "removed": before - len(kept)})
                if args.execute:
                    src_counts = {}
                    for it in kept:
                        src_counts[it.get("source", "")] = src_counts.get(it.get("source", ""), 0) + 1
                    doc["items"] = kept
                    doc["count"] = len(kept)
                    doc["sources"] = src_counts
                    doc["summaries"] = radar.build_summaries_from_items(kept, date_str)
                    doc["dedupe"] = {"window_days": radar.DEDUPE_DAYS[period],
                                     "removed": before - len(kept),
                                     "backfilled": True}
                    tmp = path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(doc, f, ensure_ascii=False, indent=1)
                    os.replace(tmp, path)

    if args.execute:
        radar.rebuild_index()

    print(json.dumps({
        "execute": args.execute,
        "window_days": args.days,
        "files_changed": len(report),
        "items_before": total_before,
        "items_after": total_after,
        "removed": total_before - total_after,
        "detail": report[:20],
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
