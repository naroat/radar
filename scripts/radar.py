#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信息雷达 · 每日多平台抓取

17 个来源、23 个板块，按三个视角归类：
  热门产品  GitHub / Product Hunt / Hugging Face / Hacker News ·
           Chrome 应用商店 / Firefox 插件 / GreasyFork / App Store 中美 / 少数派 / w2solo 作品
  热门话题  Hacker News / 今日头条 / 百度热搜 / 哔哩哔哩 / 掘金 / Lobsters / V2EX / w2solo 热议
  项目需求  Hacker News Ask HN / V2EX 创意 / Stack Overflow / w2solo 点子

用法:
  python3 scripts/radar.py                    # 抓取并写入 data/<日期>-<daily|weekly|monthly>.json
  python3 scripts/radar.py --out /tmp/r.json  # 额外把原始记录 JSON 写到指定文件
  python3 scripts/radar.py --no-save          # 只抓取打印，不落盘
  python3 scripts/radar.py --only github,hn   # 只跑指定来源（调试用）

落盘规则（JSON 为唯一数据源）：
  data/2026-10-21-daily.json    当日全部常规榜单（日榜 / 热榜 / 需求榜）
  data/2026-10-21-weekly.json   周末追加的周榜（GitHub、Product Hunt）
  data/2026-10-21-monthly.json  月末追加的月榜
  data/index.json               日期 + 周期清单，页面据此做历史回看
重跑同一天时会自动保留已生成过的「解读 / 场景」，不会被覆盖丢失。
"""

import argparse
import datetime
import html as htmllib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from concurrent import futures

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

TOP_N = 10
TIMEOUT = 30

CAT_PRODUCT = "热门产品"
CAT_TOPIC = "热门话题"
CAT_DEMAND = "项目需求"
CAT_TREND = "趋势信号"

BOARD_DAY, BOARD_WEEK, BOARD_MONTH = "日榜", "周榜", "月榜"
BOARD_HOT, BOARD_NEED = "热榜", "需求榜"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("INFO_RADAR_DATA_DIR", os.path.join(ROOT, "data"))
PERIOD_OF_BOARD = {BOARD_WEEK: "weekly", BOARD_MONTH: "monthly"}


# ---------------------------------------------------------------- 网络层


def _proxy_candidates():
    """按优先级返回代理列表: 环境变量代理 -> 本机常见代理端口 -> 直连。

    CI（GitHub Actions 等境外 runner）上不需要本机代理端口探测，直接跳过，
    避免无谓的连接失败与重试耗时；显式设置 INFO_RADAR_DIRECT=1 也可强制直连。
    """
    if os.environ.get("INFO_RADAR_DIRECT") == "1":
        return [None]

    cands = []
    env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if env_proxy:
        cands.append(env_proxy)

    if sys.platform == "darwin":
        for port in ("7897", "7890", "1087", "8080"):
            p = "http://127.0.0.1:%s" % port
            if p not in cands:
                cands.append(p)

    cands.append(None)
    return cands


def fetch(url, accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
          tries=3, api=False):
    """带代理切换和重试的 GET，返回文本。

    统一走系统 curl：部分站点（Product Hunt 等）会对 Python 自带 TLS 指纹直接 403。
    api=True 时不发浏览器专属头，避免个别 JSON 接口因 Sec-Fetch-* 异常。
    """
    if api:
        headers = {"User-Agent": "info-radar/1.0 (WorkBuddy)", "Accept": "application/json, */*"}
    else:
        headers = {
            "User-Agent": UA,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        }
    last_err = None
    for attempt in range(tries):
        for proxy in _proxy_candidates():
            cmd = ["curl", "-sS", "--max-time", str(TIMEOUT), "-o", "-",
                   "-w", "\n__HTTP_CODE__%{http_code}"]
            if proxy:
                cmd += ["-x", proxy]
            for k, v in headers.items():
                cmd += ["-H", "%s: %s" % (k, v)]
            cmd.append(url)
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT + 10)
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
                continue
            raw = proc.stdout.decode("utf-8", "replace")
            code = ""
            marker = "\n__HTTP_CODE__"
            if marker in raw:
                raw, _, code = raw.rpartition(marker)
                code = code.strip()
            if code == "200" and len(raw) > 60:
                return raw
            last_err = "HTTP %s (len=%d)" % (code or "?", len(raw))
            if code in ("403", "429", "503"):
                time.sleep(2)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError("抓取失败 %s (%s)" % (url, last_err))


def get_json(url, api=True):
    return json.loads(fetch(url, api=api))


# ---------------------------------------------------------------- 解析工具

def clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = htmllib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def clip(s, n=180):
    s = clean(s)
    return s if len(s) <= n else s[:n].rstrip() + "…"


def num(s):
    try:
        return int(str(s).replace(",", "").replace("+", "").strip())
    except Exception:  # noqa: BLE001
        return None


def human(n):
    if n is None:
        return "-"
    if n >= 1000000:
        return "%.1fm" % (n / 1000000.0)
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return str(n)


# ---------------------------------------------------------------- GitHub

def parse_github(text, period):
    unit = {"daily": "today", "weekly": "this week", "monthly": "this month"}[period]
    rows = []
    chunks = text.split('<article class="Box-row">')[1:]
    for i, chunk in enumerate(chunks[:TOP_N]):
        h2 = re.search(r"<h2[^>]*>(.*?)</h2>", chunk, re.S)
        if not h2:
            continue
        href = re.search(r'href="/([^"/]+/[^"/?#]+)"', h2.group(1))
        if not href:
            continue
        repo = href.group(1).strip("/")

        p_m = re.search(r'<p[^>]*class="[^"]*color-fg-muted[^"]*"[^>]*>(.*?)</p>', chunk, re.S)
        desc = clean(p_m.group(1)) if p_m else ""

        lang_m = re.search(r'itemprop="programmingLanguage">([^<]+)<', chunk)
        lang = clean(lang_m.group(1)) if lang_m else ""

        total_stars = None
        sm = re.search(re.escape("/%s/stargazers" % repo) + r'[^>]*>(.*?)</a>', chunk, re.S)
        if sm:
            digits = re.findall(r"([\d,]{2,})", sm.group(1)[-60:])
            if digits:
                total_stars = num(digits[-1])

        gain_m = re.search(r"([\d,]+)\s*stars?\s*%s" % unit.replace(" ", r"\s*"), chunk)
        gain = num(gain_m.group(1)) if gain_m else None

        heat = []
        if gain is not None:
            heat.append("+%s %s" % (gain, unit))
        if total_stars is not None:
            heat.append("总计 %s★" % "{:,}".format(total_stars))
        if lang:
            heat.append(lang)

        rows.append({"rank": i + 1, "title": repo, "desc": desc,
                     "heat": " · ".join(heat) or "-",
                     "url": "https://github.com/%s" % repo})
    return rows


def src_github(today, is_weekend, is_month_end):
    out = []
    plans = [(BOARD_DAY, "daily")]
    if is_weekend:
        plans.append((BOARD_WEEK, "weekly"))
    if is_month_end:
        plans.append((BOARD_MONTH, "monthly"))
    for board, period in plans:
        txt = fetch("https://github.com/trending?since=%s" % period)
        rows = parse_github(txt, period)
        if rows:
            out.append((board, rows))
    return out


# ---------------------------------------------------------------- Product Hunt

def _extract_json_array(text, start_idx):
    depth = 0
    in_str = False
    esc = False
    for i in range(start_idx, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return json.loads(text[start_idx:i + 1])
    raise ValueError("edges array not balanced")


def parse_ph(text, rank_key):
    idx = text.find('"homefeedItems"')
    if idx < 0:
        return []
    arr_idx = text.find('"edges":[', idx)
    if arr_idx < 0:
        return []
    arr_idx = text.find("[", arr_idx)
    edges = _extract_json_array(text, arr_idx)

    items = []
    for edge in edges:
        node = (edge or {}).get("node") or {}
        name = node.get("name")
        if not name:
            continue
        rank = num(node.get(rank_key) or node.get("dailyRank")) or 999
        topics = [t.get("node", {}).get("name", "")
                  for t in (node.get("topics", {}) or {}).get("edges", [])]
        topics = [t for t in topics if t][:3]
        score = node.get("latestScore") or node.get("launchDayScore")
        heat = []
        if score is not None:
            heat.append("%s votes" % score)
        if node.get("commentsCount") is not None:
            heat.append("%s comments" % node["commentsCount"])
        items.append({
            "rank": rank,
            "title": name,
            "desc": clean(node.get("tagline") or "") + (
                ("　[" + " / ".join(topics) + "]") if topics else ""),
            "heat": " · ".join(heat) or "-",
            "url": "https://www.producthunt.com/products/%s" % node.get("slug", ""),
        })
    items.sort(key=lambda x: x["rank"])
    return items[:TOP_N]


def src_producthunt(today, is_weekend, is_month_end):
    out = []
    pt_day = today - datetime.timedelta(days=1)
    plans = [(BOARD_DAY,
              ["/leaderboard/daily/%d/%02d/%02d" % (pt_day.year, pt_day.month, pt_day.day)],
              "dailyRank")]
    if is_weekend:
        iso = pt_day.isocalendar()
        prev = (pt_day - datetime.timedelta(days=7)).isocalendar()
        plans.append((BOARD_WEEK, [
            "/leaderboard/weekly/%d/%d" % (iso[0], iso[1]),
            "/leaderboard/weekly/%d/%d" % (prev[0], prev[1]),
        ], "weeklyRank"))
    if is_month_end:
        plans.append((BOARD_MONTH,
                      ["/leaderboard/monthly/%d/%02d" % (pt_day.year, pt_day.month)],
                      "monthlyRank"))

    for board, paths, rank_key in plans:
        rows = []
        for path in paths:
            try:
                rows = parse_ph(fetch("https://www.producthunt.com" + path), rank_key)
            except Exception:  # noqa: BLE001
                rows = []
            if rows:
                break
        if rows:
            out.append((board, rows))
    return out


# ---------------------------------------------------------------- Hacker News

def _hn_row(h, i, tag=""):
    points = h.get("points")
    comments = h.get("num_comments")
    heat = []
    if points is not None:
        heat.append("%s points" % points)
    if comments is not None:
        heat.append("%s comments" % comments)
    oid = h.get("objectID")
    url = h.get("url") or "https://news.ycombinator.com/item?id=%s" % oid
    desc = clip(h.get("story_text") or "", 170) or tag
    return {
        "rank": i + 1,
        "title": clean(h.get("title") or ""),
        "desc": desc,
        "heat": " · ".join(heat) or "-",
        "url": url,
    }


def src_hackernews(today):
    """HN 三段：Show HN 归热门产品，首页其余归热门话题，Ask HN 归项目需求。"""
    out = []

    # Show HN：近 2 天发布的新品，按分数排序
    cut_show = int(time.time()) - 2 * 86400
    flt_show = urllib.parse.quote("created_at_i>%d" % cut_show)
    ds = get_json("https://hn.algolia.com/api/v1/search_by_date?tags=show_hn"
                  "&hitsPerPage=100&numericFilters=" + flt_show)
    shows = [h for h in (ds.get("hits") or []) if h.get("title")]
    shows.sort(key=lambda h: h.get("points") or 0, reverse=True)
    rows_show = [_hn_row(h, i, "Show HN 新品首发") for i, h in enumerate(shows[:TOP_N])]
    if rows_show:
        out.append((CAT_PRODUCT, BOARD_HOT, rows_show))

    # 首页其余热帖
    d = get_json("https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=40")
    hits = [h for h in (d.get("hits") or []) if h.get("title")]
    hits.sort(key=lambda h: h.get("points") or 0, reverse=True)
    others = [h for h in hits
              if not (h.get("title") or "").lower().startswith("show hn")]
    rows_hot = [_hn_row(h, i, "Hacker News 首页") for i, h in enumerate(others[:TOP_N])]
    if rows_hot:
        out.append((CAT_TOPIC, BOARD_HOT, rows_hot))

    # Ask HN：按时间倒序取近 3 天，本地按分数排序
    cutoff = int(time.time()) - 3 * 86400
    flt = urllib.parse.quote("created_at_i>%d" % cutoff)
    d2 = get_json("https://hn.algolia.com/api/v1/search_by_date?tags=ask_hn"
                  "&hitsPerPage=100&numericFilters=" + flt)
    asks = [h for h in (d2.get("hits") or [])
            if h.get("title") and ("ask hn" in (h.get("title") or "").lower()
                                   or "tell hn" in (h.get("title") or "").lower())]
    asks.sort(key=lambda h: h.get("points") or 0, reverse=True)
    rows_ask = [_hn_row(h, i, "Ask HN 社区提问") for i, h in enumerate(asks[:TOP_N])]
    if rows_ask:
        out.append((CAT_DEMAND, BOARD_NEED, rows_ask))
    return out


# ---------------------------------------------------------------- Lobsters

def src_lobsters(today):
    data = get_json("https://lobste.rs/hottest.json")
    rows = []
    for i, p in enumerate((data or [])[:TOP_N]):
        heat = []
        if p.get("score") is not None:
            heat.append("%s points" % p["score"])
        if p.get("comment_count") is not None:
            heat.append("%s comments" % p["comment_count"])
        tags = p.get("tags") or []
        if tags:
            heat.append(" / ".join(tags[:3]))
        rows.append({
            "rank": i + 1,
            "title": clean(p.get("title")),
            "desc": clip(p.get("description_plain") or "", 170) or "技术社区热帖",
            "heat": " · ".join(heat) or "-",
            "url": p.get("url") or p.get("short_id_url") or "https://lobste.rs",
        })
    return [(BOARD_HOT, rows)] if rows else []


# ---------------------------------------------------------------- V2EX

def src_v2ex(today):
    data = get_json("https://www.v2ex.com/api/topics/hot.json")
    topics = [t for t in (data or [])
              if ((t.get("node") or {}).get("title") or "") != "推广"]
    rows = []
    for i, t in enumerate(topics[:TOP_N]):
        node = (t.get("node") or {}).get("title") or ""
        heat = []
        if t.get("replies") is not None:
            heat.append("%s 回复" % t["replies"])
        if node:
            heat.append(node)
        rows.append({
            "rank": i + 1,
            "title": clean(t.get("title")),
            "desc": clip(t.get("content") or "", 170) or "V2EX 热门讨论",
            "heat": " · ".join(heat) or "-",
            "url": t.get("url") or "https://www.v2ex.com",
        })
    return [(BOARD_HOT, rows)] if rows else []


NOISE_WORDS = ("抽奖", "红包", "福利", "赠送", "拼车", "求码", "邀请码", "返利")
# 只保留「求方案 / 提想法」语义的帖子，过滤掉纯闲聊与引流贴
IDEA_HINT = re.compile(r"想|有没有|如何|怎么|什么|建议|想法|创意|求|方案|需求|"
                       r"能不能|可不可以|推荐|替代|实现|吗|？")


def src_v2ex_ideas(today):
    """V2EX 创意节点：把「我想做 / 求建议」类帖子当作需求信号。

    创意节点里混有抽奖、送福利等非需求贴，按关键词剔除。
    """
    data = get_json("https://www.v2ex.com/api/topics/show.json?node_name=ideas")
    topics = [t for t in (data or [])
              if t.get("title")
              and IDEA_HINT.search(t.get("title") or "")
              and not any(w in (t.get("title") or "") + (t.get("content") or "")
                          for w in NOISE_WORDS)]
    topics.sort(key=lambda t: t.get("replies") or 0, reverse=True)
    rows = []
    for i, t in enumerate(topics[:TOP_N]):
        heat = []
        if t.get("replies") is not None:
            heat.append("%s 回复" % t["replies"])
        heat.append("V2EX 创意")
        rows.append({
            "rank": i + 1,
            "title": clean(t.get("title")),
            "desc": clip(t.get("content") or "", 170) or "V2EX 创意节点讨论",
            "heat": " · ".join(heat),
            "url": t.get("url") or "https://www.v2ex.com/go/ideas",
        })
    return [(BOARD_NEED, rows)] if rows else []


# ---------------------------------------------------------------- Google Trends

def src_gtrends(today):
    xml_text = fetch("https://trends.google.com/trending/rss?geo=US",
                     accept="application/rss+xml,application/xml;q=0.9,*/*;q=0.8")
    items = re.findall(r"<item>(.*?)</item>", xml_text, re.S)
    rows = []
    for i, it in enumerate(items[:TOP_N]):
        t_m = re.search(r"<title>(.*?)</title>", it, re.S)
        if not t_m:
            continue
        title = clean(t_m.group(1))
        traffic_m = re.search(r"<ht:approx_traffic>(.*?)</ht:approx_traffic>", it, re.S)
        traffic = clean(traffic_m.group(1)) if traffic_m else ""
        news = re.findall(r"<ht:news_item_title>(.*?)</ht:news_item_title>", it, re.S)
        desc = " / ".join(clean(n) for n in news[:2]) or "Google 美国区搜索趋势"
        heat = []
        if traffic:
            heat.append("%s 次搜索" % traffic)
        heat.append("US 热搜")
        rows.append({
            "rank": i + 1,
            "title": title,
            "desc": clip(desc, 170),
            "heat": " · ".join(heat),
            "url": "https://trends.google.com/trends/explore?geo=US&q=" + urllib.parse.quote(title),
        })
    return [(CAT_TREND, BOARD_HOT, rows)] if rows else []


# ---------------------------------------------------------------- Hugging Face

def src_huggingface(today):
    data = get_json("https://huggingface.co/api/models?sort=trendingScore&limit=40&direction=-1")
    rows = []
    for i, m in enumerate((data or [])[:TOP_N]):
        mid = m.get("id") or ""
        tags = [t for t in (m.get("tags") or []) if ":" not in t][:3]
        heat = []
        if m.get("trendingScore") is not None:
            heat.append("趋势 %s" % m["trendingScore"])
        if m.get("likes") is not None:
            heat.append("%s likes" % human(m["likes"]))
        if m.get("downloads") is not None:
            heat.append("%s 下载" % human(m["downloads"]))
        if tags:
            heat.append(" / ".join(tags))
        rows.append({
            "rank": i + 1,
            "title": mid,
            "desc": "AI 模型 · " + (m.get("pipeline_tag") or "模型"),
            "heat": " · ".join(heat) or "-",
            "url": "https://huggingface.co/%s" % mid,
        })
    return [(CAT_PRODUCT, BOARD_HOT, rows)] if rows else []


# ---------------------------------------------------------------- 浏览器插件平台
#
# Chrome 应用商店：分类页 SSR 只有 id 与 slug，名称/用户数要去详情页取；
# Firefox 插件：AMO 公开 API，按 hotness（近期热度上升）排序；
# GreasyFork：用户脚本热度榜（浏览器插件生态里最常见的油猴脚本）。

CWS_ITEM_RE = re.compile(r"\./detail/([a-z0-9][a-z0-9\-]{1,70})/([a-z0-9]{32})")
CWS_USERS_RE = re.compile(r"([\d,]+)\s*\+?\s*users")
CWS_RATE_RE = re.compile(r"Average rating ([\d.]+)")


def cn_num(n):
    """12000 -> 1.2万，方便中文阅读。"""
    try:
        n = int(str(n).replace(",", "").strip())
    except Exception:  # noqa: BLE001
        return str(n)
    if n >= 100000000:
        return "%.1f亿" % (n / 1e8)
    if n >= 10000:
        return ("%.0f万" % (n / 1e4)) if n >= 100000 else ("%.1f万" % (n / 1e4))
    return str(n)


def _cws_detail(slug, ext_id):
    """详情页：名称 / 简介 / 用户数 / 评分。失败返回 None（跳过该条）。"""
    url = "https://chromewebstore.google.com/detail/%s/%s" % (slug, ext_id)
    try:
        t = fetch(url, tries=2)
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r'<meta name="description" content="([^"]*)"', t) or \
        re.search(r'og:description"\s*content="([^"]*)"', t)
    desc = clean(m.group(1)) if m else ""
    mt = re.search(r"<title>(.*?)</title>", t, re.S)
    title = clean(mt.group(1)) if mt else ""
    title = re.sub(r"\s*-\s*Chrome Web Store\s*$", "", title).strip() or slug.replace("-", " ")
    mu = CWS_USERS_RE.search(t)
    users = cn_num(mu.group(1)) + " 用户" if mu else ""
    mr = CWS_RATE_RE.search(t)
    rating = ("★%s" % mr.group(1)) if mr else ""
    heat = " · ".join([x for x in (users, rating) if x]) or "Chrome 精选"
    return {"title": title, "desc": clip(desc, 170) or "Chrome 扩展", "heat": heat, "url": url}


def src_chrome_store(today):
    text = fetch("https://chromewebstore.google.com/category/extensions?hl=en")
    seen, pairs = set(), []
    for slug, ext_id in CWS_ITEM_RE.findall(text):
        if ext_id in seen:
            continue
        seen.add(ext_id)
        pairs.append((slug, ext_id))
    # 详情页串行要 2 分钟，改成小并发（5 路）压到 30 秒左右
    rows = []
    with futures.ThreadPoolExecutor(max_workers=5) as ex:
        for d in ex.map(lambda p: _cws_detail(p[0], p[1]), pairs[:TOP_N]):
            if not d:
                continue
            d["rank"] = len(rows) + 1
            rows.append(d)
    return [(CAT_PRODUCT, BOARD_HOT, rows)] if rows else []


def _amo_locale(v):
    if isinstance(v, dict):
        for k in ("zh-CN", "en-US", "en-GB", "en"):
            if v.get(k):
                return v[k]
        for v2 in v.values():
            if v2:
                return v2
        return ""
    return v or ""


def src_firefox_addons(today):
    url = ("https://addons.mozilla.org/api/v5/addons/search/?app=firefox"
           "&type=extension&sort=hotness&page_size=%d" % max(TOP_N, 10))
    data = get_json(url)
    rows = []
    for i, r in enumerate((data.get("results") or [])[:TOP_N]):
        slug = r.get("slug") or ""
        link = r.get("url") or ""
        if link.startswith("/"):
            link = "https://addons.mozilla.org" + link
        ratings = r.get("ratings") or {}
        heat = []
        if r.get("average_daily_users") is not None:
            heat.append("日均用户 %s" % human(r["average_daily_users"]))
        if r.get("weekly_downloads") is not None:
            heat.append("周下载 %s" % human(r["weekly_downloads"]))
        if ratings.get("average"):
            heat.append("★%.1f" % ratings["average"])
        rows.append({
            "rank": i + 1,
            "title": clean(_amo_locale(r.get("name"))) or slug,
            "desc": clip(_amo_locale(r.get("summary")), 170) or "Firefox 扩展",
            "heat": " · ".join(heat) or "-",
            "url": link or "https://addons.mozilla.org",
        })
    return [(CAT_PRODUCT, BOARD_HOT, rows)] if rows else []


GF_LI_RE = re.compile(r'<li data-script-id="(\d+)" data-script-name="([^"]*)"'
                      r'[^>]*data-script-daily-installs="(\d*)"'
                      r'[^>]*data-script-total-installs="(\d*)"(.*?)</li>', re.S)
GF_LINK_RE = re.compile(r'<a class="script-link" href="([^"]+)"')
GF_DESC_RE = re.compile(r'<span class="script-description description">(.*?)</span>', re.S)


def src_greasyfork(today):
    text = fetch("https://greasyfork.org/en/scripts?sort=hot")
    rows = []
    for sid, raw_name, daily, total, body in GF_LI_RE.findall(text):
        if len(rows) >= TOP_N:
            break
        name = clean(htmllib.unescape(raw_name))
        m = GF_LINK_RE.search(body)
        link = "https://greasyfork.org" + m.group(1) if m else "https://greasyfork.org"
        md = GF_DESC_RE.search(body)
        desc = clip(md.group(1), 170) if md else ""
        heat = []
        if daily:
            heat.append("日安装 %s" % cn_num(daily))
        if total:
            heat.append("总安装 %s" % cn_num(total))
        rows.append({
            "rank": len(rows) + 1,
            "title": name or ("脚本 %s" % sid),
            "desc": desc or "用户脚本（油猴）",
            "heat": " · ".join(heat) or "-",
            "url": link,
        })
    return [(CAT_PRODUCT, BOARD_HOT, rows)] if rows else []


# ---------------------------------------------------------------- 中文大众热榜
#
# 今日头条热榜 / 百度热搜 / 哔哩哔哩热门视频：中文世界当天最热的事件与内容。
# 微博热搜、知乎热榜、抖音热榜均需登录态或触发风控，抓不到，不做。


def src_toutiao(today):
    """今日头条热榜：带 HotValue 热度值。"""
    data = get_json("https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc")
    rows = []
    for it in (data.get("data") or []):
        if len(rows) >= TOP_N:
            break
        title = clean(it.get("Title") or "")
        if not title:
            continue
        heat = "热度 %s" % cn_num(it.get("HotValue")) if it.get("HotValue") else "头条热榜"
        rows.append({
            "rank": len(rows) + 1,
            "title": title,
            "desc": clean(it.get("Label") or "") or "今日头条热榜事件",
            "heat": heat,
            "url": it.get("Url") or "https://www.toutiao.com/",
        })
    return [(CAT_TOPIC, BOARD_HOT, rows)] if rows else []


def src_baidu_hot(today):
    """百度热搜实时榜：数亿用户真实搜索数据。"""
    data = get_json("https://top.baidu.com/api/board?platform=wise&tab=realtime")
    # cards[].content 里可能再套一层 {"content": [...]}，递归把带 word 的条目摊平
    items = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("word"):
                items.append(node)
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for card in ((data.get("data") or {}).get("cards") or []):
        walk(card.get("content"))
    rows = []
    for it in items:
        if len(rows) >= TOP_N:
            break
        word = clean(it.get("word") or "")
        if not word:
            continue
        tag = clean(it.get("labelTagName") or "")
        hot_flag = {"1": "新上榜", "3": "热度飙升"}.get(str(it.get("hotTag") or ""), "")
        extra = " / ".join([x for x in (tag, hot_flag) if x])
        rows.append({
            "rank": len(rows) + 1,
            "title": word,
            "desc": ("百度热搜 · " + extra) if extra else "百度热搜实时热点",
            "heat": "热搜第 %d 名" % (len(rows) + 1),
            "url": it.get("url") or "https://www.baidu.com/s?wd=%s" % urllib.parse.quote(word),
        })
    return [(CAT_TOPIC, BOARD_HOT, rows)] if rows else []


def src_bilibili(today):
    """B 站综合热门视频。"""
    data = get_json("https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1")
    rows = []
    for it in ((data.get("data") or {}).get("list") or []):
        if len(rows) >= TOP_N:
            break
        title = clean(it.get("title") or "")
        if not title:
            continue
        bvid = it.get("bvid") or ""
        stat = it.get("stat") or {}
        owner = (it.get("owner") or {}).get("name") or ""
        tname = it.get("tname") or ""
        heat = []
        if stat.get("view"):
            heat.append("%s 播放" % cn_num(stat["view"]))
        if stat.get("like"):
            heat.append("%s 点赞" % cn_num(stat["like"]))
        if stat.get("reply"):
            heat.append("%s 评论" % cn_num(stat["reply"]))
        rows.append({
            "rank": len(rows) + 1,
            "title": title,
            "desc": " · ".join([x for x in (owner, tname) if x]) or "B 站热门视频",
            "heat": " · ".join(heat) or "-",
            "url": ("https://www.bilibili.com/video/%s" % bvid) if bvid else "https://www.bilibili.com/",
        })
    return [(CAT_TOPIC, BOARD_HOT, rows)] if rows else []


# ---------------------------------------------------------------- 移动端应用榜
#
# App Store：Apple 官方 RSS 接口，中国区与美国区免费榜。
# 游戏类（Steam 等）用户明确不要，已下线。


def _apple_free_board(country, board_name):
    data = get_json("https://rss.marketingtools.apple.com/api/v2/%s/apps/top-free/10/apps.json"
                    % country)
    rows = []
    for a in ((data.get("feed") or {}).get("results") or [])[:TOP_N]:
        name = clean(a.get("name") or "")
        if not name:
            continue
        genres = " / ".join(clean(g.get("name") or "") for g in (a.get("genres") or []) if g.get("name"))
        artist = clean(a.get("artistName") or "")
        desc = " · ".join([x for x in (genres, artist) if x]) or "App Store 应用"
        rows.append({
            "rank": len(rows) + 1,
            "title": name,
            "desc": desc,
            "heat": "免费榜 第 %d 名" % (len(rows) + 1),
            "url": a.get("url") or "https://www.apple.com/",
        })
    return (CAT_PRODUCT, board_name, rows)


def src_appstore(today):
    """App Store 免费榜（中国 + 美国）。任一区失败不影响另一区。"""
    out = []
    for country, board in (("cn", "免费榜（中国）"), ("us", "免费榜（美国）")):
        try:
            group = _apple_free_board(country, board)
        except Exception:  # noqa: BLE001
            continue
        if group[2]:
            out.append(group)
    return out


# ---------------------------------------------------------------- 中文开发者与好物社区

def src_juejin(today):
    """掘金热门文章（前端/后端/移动端综合榜）。"""
    data = get_json("https://api.juejin.cn/content_api/v1/content/article_rank?category_id=1&type=hot")
    rows = []
    for entry in (data.get("data") or []):
        if len(rows) >= TOP_N:
            break
        c = entry.get("content") or {}
        title = clean(c.get("title") or "")
        if not title:
            continue
        cnt = entry.get("content_counter") or {}
        heat = []
        if cnt.get("hot_rank"):
            heat.append("热度 %s" % cnt["hot_rank"])
        if cnt.get("view"):
            heat.append("%s 阅读" % cn_num(cnt["view"]))
        if cnt.get("like"):
            heat.append("%s 赞" % cn_num(cnt["like"]))
        cid = c.get("content_id") or ""
        rows.append({
            "rank": len(rows) + 1,
            "title": title,
            "desc": clip(clean(c.get("brief") or ""), 170) or "掘金技术热文",
            "heat": " · ".join(heat) or "-",
            "url": ("https://juejin.cn/post/%s" % cid) if cid else "https://juejin.cn/",
        })
    return [(CAT_TOPIC, BOARD_HOT, rows)] if rows else []


def src_sspai(today):
    """少数派首页时间流：工具 / 好物 / 效率方法推荐。"""
    data = get_json("https://sspai.com/api/v1/article/index/page/get?limit=20&offset=0&created_at=0")
    rows = []
    for a in (data.get("data") or []):
        if len(rows) >= TOP_N:
            break
        title = clean(a.get("title") or "")
        if not title:
            continue
        heat = []
        if a.get("like_count"):
            heat.append("%s 赞" % cn_num(a["like_count"]))
        if a.get("view_count"):
            heat.append("%s 阅读" % cn_num(a["view_count"]))
        rows.append({
            "rank": len(rows) + 1,
            "title": title,
            "desc": clip(clean(a.get("summary") or ""), 170) or "少数派文章",
            "heat": " · ".join(heat) or "编辑推荐",
            "url": "https://sspai.com/post/%s" % a.get("id"),
        })
    return [(CAT_PRODUCT, BOARD_HOT, rows)] if rows else []


def src_stackoverflow(today):
    """Stack Overflow 热门提问：开发者真实卡住的疑难，是需求富矿。"""
    data = get_json("https://api.stackexchange.com/2.3/questions?order=desc&sort=hot"
                    "&site=stackoverflow&pagesize=15&filter=withbody")
    rows = []
    for q in (data.get("items") or []):
        if len(rows) >= TOP_N:
            break
        title = clean(htmllib.unescape(q.get("title") or ""))
        if not title:
            continue
        body = re.sub(r"<[^>]+>", " ", q.get("body") or "")
        desc = clip(clean(htmllib.unescape(body)), 160)
        tags = " / ".join(q.get("tags") or [])[:40]
        heat = []
        if q.get("view_count") is not None:
            heat.append("%s 浏览" % cn_num(q["view_count"]))
        if q.get("score") is not None:
            heat.append("%s 赞" % q["score"])
        if q.get("answer_count") is not None:
            heat.append("%d 回答" % q["answer_count"])
        rows.append({
            "rank": len(rows) + 1,
            "title": title,
            "desc": desc or (("技术标签：" + tags) if tags else "Stack Overflow 提问"),
            "heat": " · ".join(heat) or "-",
            "url": q.get("link") or "https://stackoverflow.com/",
        })
    return [(CAT_DEMAND, BOARD_NEED, rows)] if rows else []


# ---------------------------------------------------------------- w2solo 独立开发者社区

# w2solo 是中文独立开发者社区（Homeland 程序），公开 API：/api/v3/topics.json
# 支持 node_id 过滤与 limit/offset。节点号：9 我的作品 / 22 奇思妙想 / 83 开坑 / 67 外包零活。
W2SOLO_API = "https://w2solo.com/api/v3/topics.json"
W2SOLO_NODE_WORKS = 9      # 我的作品
W2SOLO_NODE_IDEAS = 22     # 奇思妙想
W2SOLO_NODE_KAIKEN = 83    # 开坑（新点子）
W2SOLO_NODE_OUTSOURCE = 67  # 外包零活

# 需求板块里剔除的噪声：招聘、广告、回收站等不属于「用户想要什么」的帖子
W2SOLO_NOISE = ("招聘", "招人", "内推", "求职", "求职", "广告", "推广",
                "回收", "社群事务", "线下活动", "线上活动")


def _w2solo_topics(node_id=None, limit=30):
    url = "%s?limit=%d" % (W2SOLO_API, limit)
    if node_id:
        url += "&node_id=%d" % node_id
    data = get_json(url)
    return [t for t in (data.get("topics") or [])
            if t.get("title") and not t.get("deleted")]


def _w2solo_row(t, i, extra=""):
    heat = []
    if t.get("hits"):
        heat.append("%s 浏览" % cn_num(t["hits"]))
    if t.get("replies_count"):
        heat.append("%d 回复" % t["replies_count"])
    if t.get("likes_count"):
        heat.append("%d 赞" % t["likes_count"])
    if extra:
        heat.append(extra)
    who = ((t.get("user") or {}).get("name") or (t.get("user") or {}).get("login") or "")
    node = t.get("node_name") or ""
    desc = " · ".join([x for x in (node, ("作者 " + who) if who else "") if x])
    return {
        "rank": i + 1,
        "title": clean(t.get("title")),
        "desc": desc or "w2solo 独立开发者社区",
        "heat": " · ".join(heat) or "-",
        "url": "https://w2solo.com/topics/%s" % t.get("id"),
    }


def src_w2solo(today):
    """w2solo 独立开发者社区：一次抓三个板块。

    · 作品榜（热门产品）：「我的作品」节点最新发布的产品
    · 热榜（热门话题）：全站最新讨论里热度最高的，排除作品与需求节点避免重复
    · 需求榜（项目需求）：奇思妙想 + 开坑 + 外包零活，即开发者真实想做/想找人做的事
    """
    groups = []

    # 1）作品榜
    works = _w2solo_topics(W2SOLO_NODE_WORKS, limit=TOP_N + 5)[:TOP_N]
    if works:
        groups.append((CAT_PRODUCT, "作品榜",
                       [_w2solo_row(t, i, "新作品") for i, t in enumerate(works)]))

    # 2）热榜：全站最新 60 条里按浏览排序，排除作品/需求节点
    skip_nodes = {W2SOLO_NODE_WORKS, W2SOLO_NODE_IDEAS,
                  W2SOLO_NODE_KAIKEN, W2SOLO_NODE_OUTSOURCE}
    feed = [t for t in _w2solo_topics(None, limit=60) if t.get("node_id") not in skip_nodes]
    feed.sort(key=lambda t: (t.get("hits") or 0, t.get("replies_count") or 0), reverse=True)
    hot = feed[:TOP_N]
    if hot:
        groups.append((CAT_TOPIC, BOARD_HOT,
                       [_w2solo_row(t, i, "社区热议") for i, t in enumerate(hot)]))

    # 3）需求榜：以「奇思妙想 + 开坑」为主（真正的点子与想做的东西），
    #    外包零活只补最多 4 条，避免整榜变成「找人接活」的招募贴。
    needs = []
    seen = set()

    def _collect(nid, cap):
        got = []
        for t in _w2solo_topics(nid, limit=15):
            title = clean(t.get("title") or "")
            if not title or title in seen:
                continue
            if any(w in title for w in W2SOLO_NOISE):
                continue
            seen.add(title)
            got.append(t)
            if len(got) >= cap:
                break
        return got

    needs += _collect(W2SOLO_NODE_IDEAS, TOP_N)
    needs += _collect(W2SOLO_NODE_KAIKEN, TOP_N)
    needs += _collect(W2SOLO_NODE_OUTSOURCE, 4)
    needs.sort(key=lambda t: (t.get("hits") or 0), reverse=True)
    needs = needs[:TOP_N]
    if needs:
        groups.append((CAT_DEMAND, BOARD_NEED,
                       [_w2solo_row(t, i, "社区需求") for i, t in enumerate(needs)]))

    return groups


# ---------------------------------------------------------------- 主流程

# (key, 默认类别, 来源, 抓取函数, 是否需要周期参数)
SOURCE_PLAN = [
    ("github", CAT_PRODUCT, "GitHub", "src_github", True),
    ("producthunt", CAT_PRODUCT, "Product Hunt", "src_producthunt", True),
    ("huggingface", CAT_PRODUCT, "Hugging Face", "src_huggingface", False),
    ("hackernews", CAT_PRODUCT, "Hacker News", "src_hackernews", False),
    ("chrome", CAT_PRODUCT, "Chrome 应用商店", "src_chrome_store", False),
    ("firefox", CAT_PRODUCT, "Firefox 插件", "src_firefox_addons", False),
    ("greasyfork", CAT_PRODUCT, "GreasyFork", "src_greasyfork", False),
    ("appstore", CAT_PRODUCT, "App Store", "src_appstore", False),
    ("sspai", CAT_PRODUCT, "少数派", "src_sspai", False),
    ("toutiao", CAT_TOPIC, "今日头条", "src_toutiao", False),
    ("baidu", CAT_TOPIC, "百度热搜", "src_baidu_hot", False),
    ("bilibili", CAT_TOPIC, "哔哩哔哩", "src_bilibili", False),
    ("juejin", CAT_TOPIC, "掘金", "src_juejin", False),
    ("lobsters", CAT_TOPIC, "Lobsters", "src_lobsters", False),
    ("v2ex", CAT_TOPIC, "V2EX", "src_v2ex", False),
    ("demand", CAT_DEMAND, "V2EX", "src_v2ex_ideas", False),
    ("stackoverflow", CAT_DEMAND, "Stack Overflow", "src_stackoverflow", False),
    ("w2solo", CAT_TOPIC, "w2solo", "src_w2solo", False),
]


def build_records(today=None, only=None):
    today = today or datetime.date.today()
    is_weekend = today.weekday() >= 5
    is_month_end = (today + datetime.timedelta(days=1)).month != today.month
    date_str = today.strftime("%Y-%m-%d")

    records = []
    summary = {"date": date_str, "weekend": is_weekend, "month_end": is_month_end,
               "sources": {}, "errors": []}

    g = globals()
    for key, category, source, fn_name, needs_period in SOURCE_PLAN:
        if only and key not in only:
            continue
        fn = g.get(fn_name)
        if not fn:
            continue
        try:
            groups = fn(today, is_weekend, is_month_end) if needs_period else fn(today)
        except Exception as e:  # noqa: BLE001
            summary["errors"].append("%s: %s" % (source, str(e)[:120]))
            continue

        for group in groups:
            if len(group) == 3:
                cat, board, rows = group
            else:
                board, rows = group
                cat = category
            for r in rows:
                records.append({
                    "标题": {"text": r["title"][:200]},
                    "日期": {"date": date_str},
                    "来源": {"select": source},
                    "榜单": {"select": board},
                    "排名": {"number": r["rank"]},
                    "简介": {"text": r["desc"][:500]},
                    "热度": {"text": r["heat"][:200]},
                    "链接": {"url": {"text": r["title"][:100], "link": r["url"]}},
                    "类别": {"select": cat},
                })
            label = "%s/%s/%s" % (cat, source, board)
            summary["sources"][label] = summary["sources"].get(label, 0) + len(rows)

    return records, summary


# ---------------------------------------------------------------- 摘要

STOPWORDS = set("""the and for with this that from your you are was were has have had not but
into out over under new using use used how what why when who which will can app show hacker
news ask https http www com org net io github reddit about all any get make made just like
一个 大家 什么 可以 这个 现在 有没有 想做 有没有人 技术社区热帖 新品首发 社区提问
创意 节点讨论 讨论
""".split())

URL_RE = re.compile(r"https?://\S+")

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\+\#\.\-]{2,}|[\u4e00-\u9fff]{2,}")


def _top_keywords(rows, topn=3):
    """从标题与简介里挑高频词，作为「今天在聊什么」的线索。"""
    cnt = {}
    for r in rows:
        text = URL_RE.sub(" ", (r.get("title") or "") + " " + (r.get("desc") or ""))
        for tk in TOKEN_RE.findall(text):
            t = tk.lower()
            if len(t) < 3 or t in STOPWORDS:
                continue
            cnt[t] = cnt.get(t, 0) + 1
    # 出现次数 >= 条目数的词属于该来源的通用词（如每个 Show HN 标题都带 show），剔除
    pairs = [(k, v) for k, v in cnt.items() if v < max(2, len(rows))]
    pairs.sort(key=lambda kv: (-kv[1], kv[0]))
    hot = [p for p in pairs if p[1] > 1][:topn]
    return hot or pairs[:topn]


def summarize_group(rows):
    """一句话速览：条数 + 榜首 + 高频词。"""
    rows = sorted(rows, key=lambda r: r.get("rank") or 999)
    if not rows:
        return "无数据"
    parts = ["%d 条" % len(rows)]
    top = rows[0]
    parts.append("榜首「%s」%s" % (top.get("title", "")[:44], top.get("heat", "")))
    kws = _top_keywords(rows)
    if kws:
        parts.append("高频：" + "、".join("%s(%d)" % (k, v) for k, v in kws))
    return " · ".join(parts)


def build_summaries(records, date_str):
    """每个（类别, 来源）一条；再为每个类别补一条来源=全部的总览。"""
    groups = {}
    for r in records:
        cat = r["类别"]["select"]
        src = r["来源"]["select"]
        groups.setdefault((cat, src), []).append({
            "rank": r["排名"]["number"],
            "title": r["标题"]["text"],
            "desc": r["简介"]["text"],
            "heat": r["热度"]["text"],
        })

    out = []
    cats = {}
    for (cat, src), rows in sorted(groups.items()):
        cats.setdefault(cat, []).extend(rows)
        out.append({"category": cat, "source": src,
                    "count": len(rows), "text": summarize_group(rows)})
    for cat in sorted(cats):
        srcs = sorted({s for (c, s) in groups if c == cat})
        rows = cats[cat]
        text = summarize_group(rows) + " · 覆盖 " + "、".join(srcs)
        out.append({"category": cat, "source": "全部",
                    "count": len(rows), "text": text})
    return out


def build_summaries_from_items(items, date_str):
    """与 build_summaries 同口径，但直接读落盘条目，保证「沿用上次」的板块也被统计到。"""
    groups = {}
    for it in items:
        groups.setdefault((it.get("category"), it.get("source")), []).append({
            "rank": it.get("rank"),
            "title": it.get("title"),
            "desc": it.get("desc"),
            "heat": it.get("heat"),
        })
    out = []
    cats = {}
    for (cat, src), rows in sorted(groups.items()):
        cats.setdefault(cat, []).extend(rows)
        out.append({"category": cat, "source": src,
                    "count": len(rows), "text": summarize_group(rows)})
    for cat in sorted(cats):
        srcs = sorted({s for (c, s) in groups if c == cat})
        text = summarize_group(cats[cat]) + " · 覆盖 " + "、".join(srcs)
        out.append({"category": cat, "source": "全部",
                    "count": len(cats[cat]), "text": text})
    return out


# ---------------------------------------------------------------- JSON 落盘
#
# 数据以 JSON 文件为唯一存储，命名：<日期>-<daily|weekly|monthly>.json，
# 另用 data/index.json 维护「哪天有哪些周期文件」，供页面做历史回看。


def _prop_text(v):
    if isinstance(v, dict):
        return (v.get("text") or "").strip()
    return (v or "").strip() if isinstance(v, str) else ""


def _prop_select(v):
    if isinstance(v, dict):
        return (v.get("select") or "").strip()
    return (v or "").strip() if isinstance(v, str) else ""


def _prop_number(v):
    if isinstance(v, dict):
        return v.get("number")
    return v


def _prop_url(v):
    # 支持两种形态：写入用的 {"url": {"text":..,"link":..}}，读取返回的 [{"link":..}]
    if isinstance(v, list):
        for x in v:
            if isinstance(x, dict) and x.get("link"):
                return (x.get("link") or "").strip()
        return ""
    if isinstance(v, dict):
        u = v.get("url")
        if isinstance(u, dict):
            return (u.get("link") or "").strip()
        if isinstance(u, list):
            return _prop_url(u)
        return (u or "").strip() if isinstance(u, str) else ""
    return (v or "").strip() if isinstance(v, str) else ""


def record_to_item(r):
    """资料库属性格式 -> 页面用的扁平 item。"""
    return {
        "rank": _prop_number(r.get("排名")),
        "title": _prop_text(r.get("标题")),
        "url": _prop_url(r.get("链接")),
        "desc": _prop_text(r.get("简介")),
        "heat": _prop_text(r.get("热度")),
        "source": _prop_select(r.get("来源")),
        "board": _prop_select(r.get("榜单")),
        "category": _prop_select(r.get("类别")),
        "note": _prop_text(r.get("解读")),
        "scene": _prop_text(r.get("场景")),
    }


def item_key(it):
    return (it.get("source") or "", it.get("board") or "", it.get("title") or "")


def period_of(board):
    return PERIOD_OF_BOARD.get(board, "daily")


def load_day_file(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def write_period_file(date_str, period, records, summary):
    """写一个周期文件；已存在时合并保留旧的「解读 / 场景」。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "%s-%s.json" % (date_str, period))
    items = [record_to_item(r) for r in records]

    old = load_day_file(path)
    kept = 0
    carried = []
    if old and isinstance(old.get("items"), list):
        old_map = {}
        for it in old["items"]:
            if isinstance(it, dict):
                old_map[item_key(it)] = it
        for it in items:
            prev = old_map.get(item_key(it))
            if prev:
                if not it.get("note") and prev.get("note"):
                    it["note"] = prev["note"]
                    kept += 1
                if not it.get("scene") and prev.get("scene"):
                    it["scene"] = prev["scene"]

        # 某个板块这次一条都没抓到（源抽风/被墙），沿用同日上一次的结果，避免越跑越少
        have = set((it.get("source"), it.get("board")) for it in items)
        for pair in sorted(set((i.get("source"), i.get("board"))
                              for i in old["items"] if isinstance(i, dict)) - have):
            rows = [i for i in old["items"]
                    if isinstance(i, dict) and (i.get("source"), i.get("board")) == pair]
            if rows:
                items.extend(dict(r) for r in rows)
                carried.append("%s/%s 沿用上次 %d 条" % (pair[0], pair[1], len(rows)))

    items.sort(key=lambda x: (x.get("rank") if isinstance(x.get("rank"), int) else 999))
    src_counts = {}
    for it in items:
        src_counts[it["source"]] = src_counts.get(it["source"], 0) + 1

    payload = {
        "date": date_str,
        "period": period,
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(items),
        "sources": src_counts,
        "errors": (summary.get("errors") or []) + carried,
        "summaries": build_summaries_from_items(items, date_str),
        "items": items,
    }

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return {"file": os.path.basename(path), "count": len(items),
            "kept_notes": kept, "carried": carried}


def rebuild_index():
    """扫描 data/ 目录，重建 index.json（日期倒序）。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    days = {}
    for name in os.listdir(DATA_DIR):
        if not name.endswith(".json") or name == "index.json" or name.endswith(".tmp"):
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})-(daily|weekly|monthly)\.json$", name)
        if not m:
            continue
        date_str, period = m.group(1), m.group(2)
        doc = load_day_file(os.path.join(DATA_DIR, name)) or {}
        rec = days.setdefault(date_str, {"date": date_str, "periods": [], "counts": {}, "total": 0})
        if period not in rec["periods"]:
            rec["periods"].append(period)
        cnt = doc.get("count")
        if not isinstance(cnt, int):
            cnt = len(doc.get("items") or [])
        rec["counts"][period] = cnt
        rec["total"] += cnt

    ordered = []
    for rec in days.values():
        rec["periods"].sort(key=lambda p: ["daily", "weekly", "monthly"].index(p))
        ordered.append(rec)
    ordered.sort(key=lambda r: r["date"], reverse=True)

    index = {
        "updated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "days": ordered,
    }
    path = os.path.join(DATA_DIR, "index.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return index


def save_json(records, summary):
    """按周期分组落盘 + 重建索引。"""
    buckets = {}
    for r in records:
        board = _prop_select(r.get("榜单"))
        buckets.setdefault(period_of(board), []).append(r)

    files = []
    for period in ("daily", "weekly", "monthly"):
        rows = buckets.get(period) or []
        if not rows:
            continue
        files.append(write_period_file(summary["date"], period, rows, summary))
    index = rebuild_index()
    return {"files": files, "days": len(index["days"])}


def main():
    global TOP_N
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", help="额外把原始记录 JSON 写到文件")
    ap.add_argument("--no-save", action="store_true", help="不写 data/ 目录下的 JSON")
    ap.add_argument("--top", type=int, default=TOP_N)
    ap.add_argument("--only", default="", help="只跑指定来源：github,producthunt,huggingface,"
                                               "hackernews,chrome,firefox,greasyfork,appstore,"
                                               "sspai,toutiao,baidu,bilibili,juejin,"
                                               "lobsters,v2ex,demand,stackoverflow,w2solo")
    args = ap.parse_args()

    TOP_N = args.top
    only = set(x.strip() for x in args.only.split(",") if x.strip()) or None

    records, summary = build_records(only=only)
    payload = json.dumps(records, ensure_ascii=False, indent=1)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload)

    print(json.dumps(summary, ensure_ascii=False))

    if not args.no_save:
        saved = save_json(records, summary)
        print(json.dumps({"saved": saved}, ensure_ascii=False))
    elif not args.out:
        print(payload)

    return 0


if __name__ == "__main__":
    sys.exit(main())
