# 信息雷达

每天早上抓取 17 个平台的热门产品、热门话题与项目需求，为每条记录生成「解读」和「适用场景」，
产出一个可免登录访问的静态页面，并保留全部历史数据。

- 线上页面：https://info-radar-18063.app.workbuddy.host/?sharecode=QMFTB4W3OB
- 数据源：本地 JSON 文件（每天一个），不使用任何数据库

## 目录结构

```
radar/
├── scripts/            所有脚本
│   ├── radar.py              抓取（17 来源 / 23 板块）
│   ├── annotate.py           为条目补「解读 / 场景」
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

## 日常使用

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

- **代理链**：`fetch()` 会依次尝试直连与本地代理（127.0.0.1:7897 等），带重试。
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
