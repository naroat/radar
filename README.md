# 信息雷达

每天早上抓取 17 个平台的热门产品、热门话题与项目需求，为每条记录生成「解读」和「适用场景」，
产出一个可免登录访问的静态页面，并保留全部历史数据。

- 线上页面（固定链接）：https://naroat.github.io/radar/
- 备份线路：https://info-radar-18063.app.workbuddy.host/?sharecode=QMFTB4W3OB
- 数据源：本地 JSON 文件（每天一个），不使用任何数据库
- 运行方式：GitHub Actions 每天 08:00 自动跑（不依赖本机开机），也可本地手动执行

## 目录结构

```
radar/
├── .github/workflows/
│   └── daily.yml       每日 08:00 抓取→解读→同步→提交→发布 Pages
├── scripts/            所有脚本
│   ├── radar.py              抓取（17 来源 / 23 板块）
│   ├── annotate.py           为条目补「解读 / 场景」（人工/Agent 写回）
│   ├── annotate_llm.py       为条目补「解读 / 场景」（调大模型，CI 用）
│   ├── sync_site.py          把 web/ 与 data/ 同步到 site/
│   ├── remove_source.py      下线某个来源并清理其历史数据
│   └── test_render.js        jsdom 渲染自测
├── web/
│   └── index.html      页面源码（单文件，原生 JS，fetch 读取 data/）
├── data/               数据（唯一数据源）
│   ├── 2026-10-23-daily.json     日榜 / 热榜 / 需求榜
│   ├── 2026-10-23-weekly.json    周榜（周末才生成）
│   ├── 2026-10-23-monthly.json   月榜（月末才生成）
│   └── index.json                日期清单，页面据此刻画历史
└── site/               发布产物（由 sync_site.py 生成，不入库）
    ├── index.html
    └── data/*.json
```

## 数据口径

三个视角、23 个板块：

| 类别 | 来源 |
|---|---|
| 热门产品 | GitHub 日榜 / Product Hunt 日榜 / Hugging Face / Hacker News / Chrome 应用商店 / Firefox 插件 / GreasyFork / App Store 中美免费榜 / 少数派 / w2solo 作品榜 |
| 热门话题 | Hacker News / 今日头条 / 百度热搜 / 哔哩哔哩 / 掘金 / Lobsters / V2EX / w2solo 热议 |
| 项目需求 | Hacker News Ask HN / V2EX 创意节点 / Stack Overflow / w2solo 点子 |

每条记录字段：

```json
{
  "rank": 1,
  "title": "……",
  "url": "https://……",
  "desc": "来源侧提供的简介",
  "heat": "2012 star · JavaScript",
  "source": "GitHub",
  "board": "日榜",
  "category": "热门产品",
  "note": "解读：它做什么、解决什么问题",
  "scene": "场景：谁会用、用在哪"
}
```

**解读与场景是这个项目最有价值的部分**，由每日任务逐条生成，不是模板套话。

## 云端定时运行（GitHub Actions）

`.github/workflows/daily.yml` 每天 UTC 00:00（北京时间 08:00）跑一次，流程是
抓取 → 用大模型补解读 → 同步页面 → 提交 `data/` 回 main → 把 `site/` 推到 `gh-pages`，
由 GitHub Pages 以固定域名 `https://naroat.github.io/radar/` 对外提供。

需要一次性配置（在仓库 Settings 里）：

| 项目 | 位置 | 说明 |
|---|---|---|
| `LLM_API_KEY` | Secrets and variables → Actions → Secrets | 大模型 Key（默认走 DeepSeek，OpenAI 兼容） |
| `LLM_BASE_URL` | 同上 → Variables（可选） | 默认 `https://api.deepseek.com/v1` |
| `LLM_MODEL` | 同上 → Variables（可选） | 默认 `deepseek-chat` |
| Pages 源 | Settings → Pages | Source 选 `Deploy from a branch`，分支 `gh-pages`，目录 `/ (root)` |

没配 `LLM_API_KEY` 时不会报错：抓取和发布照常，只跳过解读（页面上那些条目就没有解读文案）。
手动触发：Actions → 信息雷达每日抓取 → Run workflow。

## 日常使用（本地）

```bash
PY=/path/to/python3

# 1. 抓取并落盘（重跑同一天会保留已有解读）
$PY scripts/radar.py

# 2. 看还缺哪些解读
$PY scripts/annotate.py --date 2026-10-23 --missing

# 3. 写回解读（按「来源+榜单+标题」精确匹配，不会写错条目）
$PY scripts/annotate.py --date 2026-10-23 --items '[
  {"source":"GitHub","board":"日榜","title":"……","note":"……","scene":"……"}
]'

# 4. 同步到发布目录
$PY scripts/sync_site.py

# 5.（可选）没配 Key 的环境想试大模型批量解读
LLM_API_KEY=sk-xxx $PY scripts/annotate_llm.py --date 2026-10-23
```

调试时只跑部分来源：

```bash
$PY scripts/radar.py --only github,w2solo --no-save
$PY scripts/radar.py --only github --out /tmp/r.json   # 不落盘，写到临时文件
```

下线一个来源（默认只预演，确认后加 `--execute`）：

```bash
$PY scripts/remove_source.py --source Steam
$PY scripts/remove_source.py --source Steam --execute
```

渲染自测（需要 jsdom）：

```bash
NODE_PATH=/path/to/node_modules node scripts/test_render.js
```

## 抓取要点（改代码前看一眼）

- **代理链**：`fetch()` 会依次尝试环境变量代理、本机代理端口（仅 macOS）、直连，带重试。
  CI 上设 `INFO_RADAR_DIRECT=1` 强制直连，不做本机端口探测。
- **去重**：按「类别 + 来源 + 榜单」分组，组内按来源自身热度排序取前 10。
- **保留解读**：重跑同一天时，新数据会按 `(source, board, title)` 与旧数据合并，
  已写好的 `note` / `scene` 不会丢；榜单变化产生的新条目会缺解读，需要补。
- **w2solo**：只有 `/api/v3/topics.json` 可用（支持 `node_id` / `limit` / `offset`）。
  需求榜以外包节点最多 4 条为限，否则整榜会变成「找人接活」的招募贴。
- **Product Hunt** 偶发返回空，重跑一次即可。
- 已下线的来源：Reddit、Steam（用户明确不要游戏内容），不要再加回。

## 已知抓不到的平台

微博热搜（403）、知乎热榜（需登录）、抖音热榜（接口风控）、Google Play（网络不通）、
Edge 插件商店（页面空壳）、chrome-stats（JS 反爬）、Indie Hackers（RSS 404）。
