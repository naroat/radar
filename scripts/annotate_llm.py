#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
annotate_llm.py —— 用大模型批量补「解读 / 场景」（CI 无人值守用）

和 annotate.py 的区别：annotate.py 是给 Agent 手工写回用的（按标题精确匹配），
这里走 OpenAI 兼容的 chat completions 接口，按类别口径批量生成，适合 GitHub Actions。

配置（环境变量）：
  LLM_API_KEY    必填；没有则跳过解读，只打印 skipped，退出码 0（不影响抓取与发布）
  LLM_BASE_URL   默认 https://api.deepseek.com/v1
  LLM_MODEL      默认 deepseek-chat
  LLM_BATCH      每批条数，默认 20

注意：GitHub Models（models.github.ai）已于 2026-07-30 下线，现在任何请求都返回
200 + text/plain "OK"，不能再当免费兜底，所以这里只认 LLM_API_KEY。

用法:
  python3 scripts/annotate_llm.py                      # 补今天还缺解读的条目
  python3 scripts/annotate_llm.py --date 2026-10-26
  python3 scripts/annotate_llm.py --dry-run            # 只统计缺多少条
  python3 scripts/annotate_llm.py --limit 40           # 只处理前 40 条（调试）

幂等：已有 note 的条目一律跳过，重跑不会覆盖人工写过的解读。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import radar  # noqa: E402

DEFAULT_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

# 每一类写「解读」的口径，和人工写回时的标准保持一致
CATEGORY_RULES = {
    "热门产品": (
        "这一条在做什么、解决什么问题、和同类比有什么不一样。",
        "适用人群或落地场景，例如「正在批量投简历的求职者」「新能源车主」。",
    ),
    "热门话题": (
        "在讲什么、为什么值得关注、影响谁。涉政或社会事件只做中性客观陈述，不评论、不站队。",
        "关注这件事的人群或岗位，例如「关注体育饭圈治理的媒体从业者」。",
    ),
    "项目需求": (
        "用户真实想要什么、可以落成什么样的产品。",
        "可能来做这件事的人，例如「想做垂直工具的独立开发者」。",
    ),
}

SYSTEM_PROMPT = (
    "你是一名中文科技媒体的资深编辑，擅长用一句人话把一件陌生的事物讲清楚。"
    "你只输出 JSON 数组，不输出任何解释、不使用 markdown 代码块。"
)


def _payload(category, batch):
    rule, scene_rule = CATEGORY_RULES.get(
        category, (CATEGORY_RULES["热门产品"][0], CATEGORY_RULES["热门产品"][1]))
    lines = []
    for i, it in enumerate(batch, 1):
        desc = (it.get("desc") or "").strip()
        if len(desc) > 300:
            desc = desc[:300] + "…"
        lines.append("%d. 来源：%s｜标题：%s｜简介：%s"
                     % (i, it.get("source") or "", it.get("title") or "", desc or "（无）"))
    user = (
        "类别：%s\n\n"
        "【解读】写什么：%s\n"
        "要求：40-70 字，一句或两句，说人话；不要复述标题；不要以「这是一个」开头；不要用营销腔。\n\n"
        "【场景】写什么：%s\n"
        "要求：20-40 字，直接写人群或场景，不要写成完整句子。\n\n"
        "【硬性约束】\n"
        "1. 只能依据给出的标题和简介，不要编造功能、公司名、版本号或数据；\n"
        "2. 拿不准就用保守表述，例如「从标题看，这是一个…」；\n"
        "3. 全部用简体中文，不要把英文标题原样抄进解读；\n"
        "4. 解读里不要出现「本条」「该条目」这类元指代。\n\n"
        "条目：\n%s\n\n"
        "输出 JSON 数组，长度必须等于 %d，每项形如 "
        "{\"id\":1,\"note\":\"解读\",\"scene\":\"场景\"}，不要有其他字段。"
        % (category, rule, scene_rule, "\n".join(lines), len(batch))
    )
    return user


def _chat(base_url, model, api_key, user, timeout=180):
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        "temperature": 0.4,
        "max_tokens": 4096,
    }).encode("utf-8")
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer %s" % api_key,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return data["choices"][0]["message"]["content"]


def resolve_config():
    """只认自己的 Key；GitHub Models 已下线，不再兜底。"""
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        return {"provider": "none", "key": "", "base": "", "models": []}
    return {
        "provider": "custom",
        "key": key,
        "base": os.environ.get("LLM_BASE_URL", "").strip() or DEFAULT_BASE,
        "models": [os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL],
    }


def _parse_array(text):
    """从模型返回里抠出 JSON 数组，容忍 ```json 围栏与前后废话。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    start, end = t.find("["), t.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(t[start:end + 1])
    except Exception:  # noqa: BLE001
        return None


def _clean_text(s, limit):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    s = s.strip("“”\"'")
    return s[:limit]


def annotate_batch(category, batch, cfg, stats):
    user = _payload(category, batch)
    attempts = 0
    while attempts < 3 and cfg["models"]:
        attempts += 1
        model = cfg["models"][0]
        try:
            raw = _chat(cfg["base"], model, cfg["key"], user)
            arr = _parse_array(raw)
            if not arr:
                raise ValueError("返回不是 JSON 数组")
            by_id = {}
            for row in arr:
                try:
                    by_id[int(row.get("id"))] = row
                except Exception:  # noqa: BLE001
                    continue
            hit = 0
            for i, it in enumerate(batch, 1):
                row = by_id.get(i)
                if not row:
                    continue
                note = _clean_text(row.get("note"), 160)
                scene = _clean_text(row.get("scene"), 80)
                if len(note) < 12:
                    continue
                it["note"] = note
                if len(scene) >= 2:
                    it["scene"] = scene
                hit += 1
            stats["written"] += hit
            stats["failed"] += len(batch) - hit
            return
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:160]
            except Exception:  # noqa: BLE001
                pass
            # 模型不存在/无权限：换链上的下一个，不消耗尝试次数
            if e.code in (400, 403, 404) and len(cfg["models"]) > 1:
                bad = cfg["models"].pop(0)
                stats["errors"].append("模型 %s 不可用(HTTP %s)，改用 %s"
                                       % (bad, e.code, cfg["models"][0]))
                attempts -= 1
                continue
            stats["errors"].append("%s: HTTP %s %s" % (category, e.code, detail))
        except Exception as e:  # noqa: BLE001
            stats["errors"].append("%s/%d: %s" % (category, attempts, str(e)[:120]))
        if cfg["models"]:
            time.sleep(3 * attempts)
    stats["failed"] += len(batch)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    date_str = args.date or __import__("datetime").date.today().isoformat()
    cfg = resolve_config()
    batch_size = int(os.environ.get("LLM_BATCH") or 20)

    files = []
    for period in ("daily", "weekly", "monthly"):
        path = os.path.join(radar.DATA_DIR, "%s-%s.json" % (date_str, period))
        doc = radar.load_day_file(path)
        if doc:
            files.append((period, path, doc))

    if not files:
        print(json.dumps({"date": date_str, "error": "没有数据文件"}, ensure_ascii=False))
        return 1

    todo = {}
    for period, path, doc in files:
        for it in doc.get("items") or []:
            if (it.get("note") or "").strip():
                continue
            todo.setdefault(it.get("category") or "热门产品", []).append(it)

    total_missing = sum(len(v) for v in todo.values())
    if args.limit:
        left = args.limit
        for cat in list(todo):
            todo[cat] = todo[cat][:left]
            left -= len(todo[cat])
            if left <= 0:
                del todo[cat]
                break
        total_missing = sum(len(v) for v in todo.values())

    if args.dry_run or not total_missing:
        print(json.dumps({"date": date_str, "missing": total_missing,
                          "dry_run": args.dry_run, "provider": cfg["provider"],
                          "model": (cfg["models"] or [""])[0]},
                         ensure_ascii=False))
        return 0

    if not cfg["key"]:
        print(json.dumps({
            "date": date_str, "skipped": "no_LLM_API_KEY",
            "missing": total_missing,
            "hint": "仓库 Settings → Secrets and variables → Actions 里加 LLM_API_KEY 才会自动写解读",
        }, ensure_ascii=False))
        return 0

    stats = {"written": 0, "failed": 0, "errors": []}
    for category, items in todo.items():
        for i in range(0, len(items), batch_size):
            chunk = items[i:i + batch_size]
            annotate_batch(category, chunk, cfg, stats)
            time.sleep(1)

    for period, path, doc in files:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    radar.rebuild_index()

    left = 0
    for period, path, doc in files:
        left += sum(1 for it in doc.get("items") or [] if not (it.get("note") or "").strip())

    print(json.dumps({
        "date": date_str, "provider": cfg["provider"],
        "model": (cfg["models"] or [""])[0],
        "written": stats["written"], "failed": stats["failed"],
        "still_missing": left, "errors": stats["errors"][:5],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
