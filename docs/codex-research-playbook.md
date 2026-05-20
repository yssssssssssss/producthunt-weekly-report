# Codex Research Playbook

目标：使用 Codex app 自身的网页访问能力，为 Product Hunt 每周 Top 50 生成可持续增长的中文项目研究数据。不要依赖 Product Hunt API token。

## 周榜入口

URL 规则：

```text
https://www.producthunt.com/leaderboard/weekly/{year}/{week}/all
```

示例：

```text
https://www.producthunt.com/leaderboard/weekly/2026/1/all
```

`year` 是年份，`week` 是 ISO 周序号。默认从 `2026-W01` 采集到最近一个完整周。

## 采集原则

1. 只保留真实排名项目，不要把 sponsored/promoted/featured ad 卡片算进排名。
2. 如果默认页只显示部分项目，切到 `/all`，并通过项目详情页、搜索结果或其它可核验来源补齐。
3. 每个项目至少打开两个页面：Product Hunt 详情页、项目官网。
4. 用户反馈来自 Product Hunt 详情页评论，优先总结高票评论、maker 回复、重复出现的问题。
5. 技术栈必须有公开证据：官网、文档、公开 repo、招聘页、页面源码、Product Hunt Built-with。没有证据就写“未公开”。
6. 不要编造 votes、comments、价格、融资、技术栈。看不到就留空或标注“未公开/未显示”。

## 推荐工作流

```bash
python3 tools/ph_report.py missing-weeks --year 2026 --from-week 1 --through latest-complete
python3 tools/collect_agent_browser.py --year 2026 --from-week 1 --through latest-complete --top 50
python3 tools/enrich_agent_browser.py --year 2026 --from-week 1 --through latest-complete --detail-batch-size 5
```

采集脚本会写入：

```text
data/weeks/2026-W01.json
```

再运行：

```bash
python3 tools/ph_report.py validate data/weeks/2026-W01.json
python3 tools/ph_report.py render
```

如果只是调整分析规则，不需要重新抓官网，可以复用已有官网内容：

```bash
python3 tools/enrich_agent_browser.py --year 2026 --from-week 1 --through latest-complete --no-only-pending --reuse-websites --detail-batch-size 5
```

## JSON Schema

每周文件：

```json
{
  "schema_version": 1,
  "year": 2026,
  "week": 1,
  "week_start": "2025-12-29",
  "week_end": "2026-01-04",
  "source_url": "https://www.producthunt.com/leaderboard/weekly/2026/1/all",
  "generated_at": "2026-05-20T00:00:00+00:00",
  "top_n": 50,
  "collection_method": "codex_app_web_research",
  "items": []
}
```

每个 item：

```json
{
  "rank": 1,
  "fallback_rank": 1,
  "id": "",
  "name": "Product name",
  "slug": "product-slug",
  "tagline": "Product Hunt tagline",
  "description": "Product Hunt longer description or maker note summary",
  "product_url": "https://www.producthunt.com/products/...",
  "website_url": "https://example.com",
  "votes_count": 0,
  "comments_count": 0,
  "reviews_count": 0,
  "reviews_rating": 0,
  "featured_at": "",
  "thumbnail_url": "",
  "media": [],
  "product_links": [],
  "makers": [
    {
      "name": "",
      "username": "",
      "url": "",
      "headline": ""
    }
  ],
  "topics": [
    {
      "name": "Artificial Intelligence",
      "slug": "artificial-intelligence"
    }
  ],
  "website": {
    "status": "ok",
    "url": "https://example.com",
    "final_url": "https://example.com",
    "title": "Official site title",
    "description": "Official meta or visible summary",
    "headings": ["Visible heading"],
    "text_excerpt": "Useful visible homepage/details excerpt"
  },
  "feedback": {
    "sample_size": 5,
    "summary": "用户反馈主题中文总结。",
    "themes": ["定价问题", "集成需求"],
    "top_comments": [
      {
        "body": "Short comment excerpt",
        "votes_count": 0,
        "user": "Commenter",
        "url": "https://www.producthunt.com/..."
      }
    ]
  },
  "analysis": {
    "what_it_does": "它具体做什么。",
    "target_users": "目标用户是谁。",
    "official_details": "官网补充到的更具体信息。",
    "technical_signals": "公开技术栈/数据/集成线索；未知就写未公开。",
    "market_context": "市场、竞品、需求强弱。",
    "feedback_summary": "详情页用户反馈总结。",
    "risks": "产品、市场、合规、平台依赖等风险。",
    "verdict": "你的判断：值得关注/风险高/需要继续验证等。"
  },
  "sources": [
    {
      "type": "producthunt",
      "url": "https://www.producthunt.com/products/..."
    },
    {
      "type": "website",
      "url": "https://example.com"
    }
  ]
}
```

## 分析质量标准

- “做什么”要讲清楚真实工作流，不要只翻译 tagline。
- “官网补充”要来自官网看到的具体内容，例如集成、功能模块、价格、使用步骤。
- “技术/数据线索”要把事实和推断分开。能说“官网文档显示有 API”，不能说“后端应该是 Node.js”。
- PH topics 只能作为分类标签，不能写成技术栈。
- Product Hunt 详情页富文本中，只有带 maker 信号的首段才可作为团队说明；其余块按评论样本处理。
- 核心展示内容必须是中文摘要，不要把英文 tagline、maker note、官网文案或评论原文直接粘贴到分析区。英文只允许出现在项目名、链接、来源名称或 topic 标签中。
- “市场情况”至少包含目标场景、替代品或竞品、付费动机。
- “风险”要尖锐：冷启动、同质化、合规、隐私、平台依赖、模型成本、数据质量。
- “判断”要短而明确，不要写空话。
