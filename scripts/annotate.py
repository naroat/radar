#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
annotate.py —— 给 JSON 数据文件补「解读 / 场景」

用法:
    # 1) 列出某天还缺解读的条目
    python3 annotate.py --date 2026-10-21 --missing

    # 2) 写回（--items 为 JSON 数组，按 来源+榜单+标题 匹配）
    python3 annotate.py --date 2026-10-21 --items '[
      {"source":"GitHub","board":"日榜","title":"owner/repo",
       "note":"一句话解读","scene":"适用人群或场景"}
    ]'

匹配不到的条目会列在 unmatched 里，不会误写其他条目。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import radar  # noqa: E402


def load_day(date_str, period):
    path = os.path.join(radar.DATA_DIR, "%s-%s.json" % (date_str, period))
    return path, radar.load_day_file(path)


def all_period_files(date_str):
    out = []
    for p in ("daily", "weekly", "monthly"):
        path, doc = load_day(date_str, p)
        if doc:
            out.append((p, path, doc))
    return out


def cmd_missing(date_str):
    rows = []
    for period, path, doc in all_period_files(date_str):
        for it in doc.get("items") or []:
            if not (it.get("note") or "").strip():
                rows.append({
                    "period": period, "rank": it.get("rank"),
                    "category": it.get("category"), "source": it.get("source"),
                    "board": it.get("board"), "title": it.get("title"),
                    "desc": (it.get("desc") or "")[:160], "url": it.get("url"),
                })
    print(json.dumps({"date": date_str, "missing": len(rows), "items": rows},
                     ensure_ascii=False, indent=1))
    return 0


def cmd_write(date_str, items):
    files = all_period_files(date_str)
    if not files:
        print(json.dumps({"error": "该日期没有数据文件: %s" % date_str}, ensure_ascii=False))
        return 1

    idx = {}
    for period, path, doc in files:
        for it in doc.get("items") or []:
            idx[(it.get("source"), it.get("board"), it.get("title"))] = (it, period)

    done, unmatched = 0, []
    for a in items:
        key = (a.get("source"), a.get("board"), a.get("title"))
        hit = idx.get(key)
        if not hit:
            unmatched.append(a.get("title"))
            continue
        it, _ = hit
        if a.get("note"):
            it["note"] = a["note"]
            done += 1
        if a.get("scene"):
            it["scene"] = a["scene"]

    for period, path, doc in files:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    radar.rebuild_index()

    print(json.dumps({"date": date_str, "written": done,
                      "unmatched": unmatched}, ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--missing", action="store_true", help="列出缺解读的条目")
    ap.add_argument("--items", default="", help="要写回的条目 JSON 数组")
    args = ap.parse_args()

    if args.missing:
        return cmd_missing(args.date)
    if args.items:
        return cmd_write(args.date, json.loads(args.items))
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
