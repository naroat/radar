#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据监控 · Chrome Web Store 官方运营数据抓取

使用 Chrome Web Store API v2 的 publishers.items.queryMetrics，
为自家插件拉取商店侧运营指标（每日一行）：
  installs      累计安装
  installs-7d   近 7 天安装
  uninstalls    卸载
  users         日活
  users-7d      7 日活
  impressions   商店曝光
  page-views    详情页浏览

用法:
  python3 scripts/monitor.py                    # 抓取并追加写入 data/monitor.json
  python3 scripts/monitor.py --days 60          # 回补最近 60 天
  python3 scripts/monitor.py --no-save          # 只拉取打印，不落盘

凭据配置（二选一，本地优先读配置文件，CI 读环境变量）:
  1) scripts/monitor_config.json（已 gitignore，不入库）:
     {
       "publisher": "publishers/C0xxxxxx",
       "extensions": [
         {"id": "abcdefg...", "name": "插件名"}
       ],
       "oauth": {
         "client_id": "xxx.apps.googleusercontent.com",
         "client_secret": "GOCSPX-...",
         "refresh_token": "1//0g..."
       }
     }
  2) 环境变量（GitHub Actions secrets）:
     CWS_CLIENT_ID / CWS_CLIENT_SECRET / CWS_REFRESH_TOKEN
     CWS_PUBLISHER（publishers/{id}，只填 id 也行）
     CWS_EXTENSIONS（逗号分隔 "插件ID:名称,插件ID2:名称2"）

未配置凭据时脚本以 {"skipped": true} 正常退出，不阻断每日榜单流水线。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("INFO_RADAR_DATA_DIR", os.path.join(ROOT, "data"))
DEFAULT_CONFIG = os.path.join(ROOT, "scripts", "monitor_config.json")
MONITOR_FILE = os.path.join(DATA_DIR, "monitor.json")

TOKEN_URL = "https://oauth2.googleapis.com/token"
METRICS_URL = ("https://chromewebstore.googleapis.com/v2/"
               "{publisher}/items/{item}:queryMetrics")
SCOPE = "https://www.googleapis.com/auth/chromewebstore.readonly"

# 指标名 -> monitor.json 行字段名（-7d 后缀转下划线）
METRICS = [
    "installs", "installs-7d", "uninstalls",
    "users", "users-7d", "impressions", "page-views",
]
MAX_ROWS = 400          # monitor.json 最多保留的日期行数（每插件每天一行）
TOKEN_TTL = 3300        # access_token 提前一点过期，避免边界失效


# ---------------------------------------------------------------- 配置

def load_config(config_path):
    """本地配置文件 + 环境变量合并；缺关键凭据返回 None（视为未配置）。"""
    cfg = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                cfg = json.load(f) or {}
        except Exception as exc:
            print(json.dumps({"error": "配置文件解析失败: %s" % exc},
                             ensure_ascii=False))
            return None

    oauth = dict(cfg.get("oauth") or {})
    oauth.update({
        "client_id": os.environ.get("CWS_CLIENT_ID") or oauth.get("client_id"),
        "client_secret": os.environ.get("CWS_CLIENT_SECRET")
                         or oauth.get("client_secret"),
        "refresh_token": os.environ.get("CWS_REFRESH_TOKEN")
                         or oauth.get("refresh_token"),
    })

    publisher = os.environ.get("CWS_PUBLISHER") or cfg.get("publisher") or ""

    extensions = list(cfg.get("extensions") or [])
    env_exts = os.environ.get("CWS_EXTENSIONS") or ""
    if env_exts:
        extensions = []
        for part in env_exts.split(","):
            part = part.strip()
            if not part:
                continue
            eid, _, name = part.partition(":")
            extensions.append({"id": eid.strip(), "name": (name or eid).strip()})

    # 去掉占位符 / 未填的条目
    placeholders = ("EXTENSION_ID", "插件ID", "xxx", "abcdefg")
    extensions = [e for e in extensions
                  if e.get("id") and not any(p in e["id"] for p in placeholders)]
    oauth = {k: v for k, v in oauth.items() if v}
    oauth_ready = len(oauth) == 3

    if not publisher or not extensions or not oauth_ready:
        return None
    return {"publisher": publisher, "extensions": extensions, "oauth": oauth}


# ---------------------------------------------------------------- 网络

def get_access_token(oauth):
    data = urllib.parse.urlencode({
        "client_id": oauth["client_id"],
        "client_secret": oauth["client_secret"],
        "refresh_token": oauth["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as res:
        doc = json.loads(res.read().decode())
    if "access_token" not in doc:
        raise RuntimeError("换取 access_token 失败: %s" % json.dumps(doc)[:300])
    return doc["access_token"]


def _date_dict(dt):
    return {"year": dt.year, "month": dt.month, "day": dt.day}


def query_metrics(token, publisher, item, first_date, last_date):
    """调一次 queryMetrics，返回 [{date: 'YYYY-MM-DD', metrics: {...}}]。"""
    params = []
    for name in METRICS:
        params.append(("metrics", name))
    params.extend([
        ("timeSpec.firstDate.year", str(first_date.year)),
        ("timeSpec.firstDate.month", str(first_date.month)),
        ("timeSpec.firstDate.day", str(first_date.day)),
        ("timeSpec.lastDate.year", str(last_date.year)),
        ("timeSpec.lastDate.month", str(last_date.month)),
        ("timeSpec.lastDate.day", str(last_date.day)),
        ("pageSize", "1000"),
    ])
    url = METRICS_URL.format(publisher=publisher, item=item) + "?" \
        + urllib.parse.urlencode(params)
    rows, page_token = [], None
    while True:
        full = url + (("&pageToken=" + urllib.parse.quote(page_token))
                      if page_token else "")
        req = urllib.request.Request(full, headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=60) as res:
            doc = json.loads(res.read().decode())
        for row in doc.get("metricRows") or []:
            rows.append(_parse_row(row))
        page_token = doc.get("nextPageToken")
        if not page_token:
            break
    return rows


def _parse_row(row):
    date = row.get("date") or {}
    if isinstance(date, dict):
        date_str = "%04d-%02d-%02d" % (date.get("year", 1970),
                                       date.get("month", 1),
                                       date.get("day", 1))
    else:
        date_str = str(date)[:10]
    dims, vals = {}, {}
    for dv in row.get("dimensionValues") or []:
        dims[dv.get("dimension", "")] = dv.get("value", "")
    for mv in row.get("metricValues") or []:
        vals[mv.get("metric", "")] = _to_num(mv.get("value"))
    return {"date": date_str, "dims": dims, "metrics": vals}


def _to_num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- 落盘

def load_monitor():
    if os.path.exists(MONITOR_FILE):
        try:
            with open(MONITOR_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"updated_at": "", "extensions": [], "rows": []}


def save_monitor(doc):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = MONITOR_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp, MONITOR_FILE)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30,
                    help="拉取最近 N 天（含今天；官方 T+1，今天可能为空）")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--no-save", action="store_true", help="只打印不落盘")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if not cfg:
        print(json.dumps({
            "skipped": True,
            "reason": "未配置 Chrome Web Store 凭据（scripts/monitor_config.json 或 CWS_* 环境变量），跳过监控抓取",
            "setup": "参考 scripts/monitor.py 文件头注释完成 OAuth 配置",
        }, ensure_ascii=False))
        return 0

    publisher = cfg["publisher"]
    if not publisher.startswith("publishers/"):
        publisher = "publishers/" + publisher
    exts = {e["id"]: e.get("name") or e["id"] for e in cfg["extensions"]}

    token = get_access_token(cfg["oauth"])
    token_at = time.time()

    last_date = datetime.date.today()          # 官方 T+1，今天大概率空，允许
    first_date = last_date - datetime.timedelta(days=max(args.days, 1) - 1)

    new_rows = []
    for eid, name in exts.items():
        if time.time() - token_at > TOKEN_TTL:
            token = get_access_token(cfg["oauth"])
            token_at = time.time()
        rows = query_metrics(token, publisher, eid, first_date, last_date)
        for r in rows:
            m = r["metrics"]
            row = {
                "date": r["date"],
                "id": eid,
                "name": name,
                "installs": m.get("installs", 0),
                "installs_7d": m.get("installs-7d", 0),
                "uninstalls": m.get("uninstalls", 0),
                "users": m.get("users", 0),
                "users_7d": m.get("users-7d", 0),
                "impressions": m.get("impressions", 0),
                "page_views": m.get("page-views", 0),
            }
            if r["dims"]:
                row["dims"] = r["dims"]
            new_rows.append(row)
        time.sleep(0.4)

    if args.no_save:
        print(json.dumps({"extensions": list(exts.values()),
                          "rows": len(new_rows)}, ensure_ascii=False))
        return 0

    doc = load_monitor()
    doc["extensions"] = [{"id": eid, "name": name} for eid, name in exts.items()]
    doc["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z")

    # 按 (date, id) 去重合并：新数据覆盖旧数据
    old = {(r.get("date"), r.get("id")): r for r in doc.get("rows") or []}
    for r in new_rows:
        old[(r["date"], r["id"])] = r
    all_rows = sorted(old.values(), key=lambda x: x["date"], reverse=True)
    # 只保留最近 MAX_ROWS 个日期
    keep_dates = {r["date"] for r in all_rows[:MAX_ROWS]}
    doc["rows"] = [r for r in all_rows if r["date"] in keep_dates]
    save_monitor(doc)

    print(json.dumps({
        "ok": True,
        "extensions": list(exts.values()),
        "fetched": len(new_rows),
        "total_rows": len(doc["rows"]),
        "latest_date": doc["rows"][0]["date"] if doc["rows"] else None,
        "saved": MONITOR_FILE,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
