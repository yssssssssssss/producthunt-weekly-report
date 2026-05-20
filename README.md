# Product Hunt Weekly Top 50 Report

这个项目从 `https://www.producthunt.com/leaderboard/weekly/{year}/{week}` 周榜开始，持续沉淀每周 Top 50 项目的中文分析，并生成一个可以增长的静态 HTML 页面。

当前主路径不依赖 Product Hunt API token：由 Codex app 自己打开周榜、项目详情页和官网，整理成结构化 JSON；本地脚本只负责校验、合并和渲染。这样比硬爬 Product Hunt HTML 更稳，也不会把 Cloudflare/动态加载问题塞进一段脆弱脚本里。

当前状态：已采集并深度富集 `2026-W01` 到 `2026-W20`，共 `998` 个真实榜单项目。`2026-W04` 和 `2026-W20` 各缺 1 个 rank，是 Product Hunt 页面分页返回中的缺口，未补造。

## 项目结构

- `data/weeks/*.json`：每周研究结果，一个文件一周。
- `docs/index.html`：最终可部署页面。
- `docs/codex-research-playbook.md`：Codex 采集周榜时必须遵守的研究流程和 JSON 结构。
- `docs/deep-enrichment-method.md`：进入详情页/官网后才能生成分析的质量规则。
- `tools/ph_report.py`：周范围计算、缺失周检测、JSON 校验、HTML 渲染；可选保留 API-backed update，但不是主路径。
- `tools/enrich_agent_browser.py`：进入 Product Hunt 二级页和官网，批量生成深度富集分析。
- `.github/workflows/update-report.yml`：CI 中只做校验和重新渲染，不负责联网研究。

## Codex 采集流程

公网访问：

```text
https://yssssssssssss.github.io/producthunt-weekly-report/
```

本地查看：

```bash
python3 -m http.server 8080 -d docs
```

然后访问 `http://localhost:8080`。

查看还缺哪些周：

```bash
python3 tools/ph_report.py missing-weeks --year 2026 --from-week 1 --through latest-complete
```

为某一周打印 Codex 研究提示：

```bash
python3 tools/ph_report.py codex-prompt --year 2026 --week 1 --top 50
```

快速抓取 2026 已完成周的周榜分页基础数据：

```bash
python3 tools/collect_agent_browser.py --year 2026 --from-week 1 --through latest-complete --top 50
```

这个命令使用 `agent-browser` 打开真实 Product Hunt 页面，并通过页面自己的 Apollo `fetchMore` 分页拿 Top 50；不需要 Product Hunt API token。它先写入排名、tagline、topics、votes/comments 等基础数据。详情页评论和官网内容需要后续富集。

深度富集一周：

```bash
python3 tools/enrich_agent_browser.py --week-file data/weeks/2026-W01.json --detail-batch-size 5
python3 tools/ph_report.py validate
python3 tools/ph_report.py render
```

刷新已富集项目的分析规则，不重复抓官网：

```bash
python3 tools/enrich_agent_browser.py --year 2026 --from-week 1 --through latest-complete --no-only-pending --reuse-websites --detail-batch-size 5
python3 tools/ph_report.py validate
python3 tools/ph_report.py render
```

深度富集前，页面只显示 `待富集`，不会展示产品级风险、技术栈或用户反馈结论。

Codex 根据提示使用网页能力研究后，写入：

```text
data/weeks/2026-W01.json
```

然后校验并生成页面：

```bash
python3 tools/ph_report.py validate data/weeks/2026-W01.json
python3 tools/ph_report.py render
```

## 每个项目需要包含什么

每个 Top 50 项目至少要有：

- 排名、项目名、Product Hunt 详情页 URL、官网 URL。
- Product Hunt tagline / 介绍、votes、comments、topics。
- 官网细节：产品功能、目标用户、集成、价格/套餐线索、文档/技术线索。
- 详情页用户反馈：评论主题、典型问题、用户认可点、质疑点。
- 中文分析字段：做什么、目标用户、官网补充、技术/数据线索、市场情况、风险、判断。
- PH topics 只是分类标签，不能当成技术栈；未公开的模型、云厂商、数据库、后端框架必须写“未公开”。
- HTML 核心分析区必须使用中文摘要，不直接展示英文 tagline、官网原文、maker note 或评论原文；英文仅保留在项目名、外部链接和 topic 标签等元数据位置。

完整 schema 见 [docs/codex-research-playbook.md](docs/codex-research-playbook.md)。

## 每周自动更新

我已经在 Codex app 里创建自动化：`update-product-hunt-weekly-report`。

计划时间：每周二北京时间 09:00。  
任务：找出缺失周，使用 Codex 自身网页能力采集最近完整周，写入 `data/weeks/*.json`，运行校验和渲染。

为什么是每周二：Product Hunt 当前周榜在周中仍会变动，周二处理上一完整周更稳。

## 部署建议

当前已使用 GitHub Pages 部署，发布源是 `main` 分支的 `docs/` 目录：

```text
https://github.com/yssssssssssss/producthunt-weekly-report
https://yssssssssssss.github.io/producthunt-weekly-report/
```

Codex 每周更新本地数据和 HTML 后，会提交并推送到 GitHub，GitHub Pages 随后刷新公网页面。

Cloudflare Pages 也合适：连接 GitHub 仓库，把 `docs/` 作为静态输出目录。Vercel 也能用，但对一个静态报告偏重。

## 可选：Product Hunt API 模式

`tools/ph_report.py update` 仍保留 API-backed 路径，适合以后你拿到 Product Hunt token 时做半自动抓取。但当前项目不要求它。

```bash
export PRODUCT_HUNT_TOKEN="可选"
python3 tools/ph_report.py update --year 2026 --from-week 1 --through latest-complete --top 50 --comments 20
```

没有 token 时，请使用 Codex 研究流程，不要运行这个命令。

## 重要限制

- Product Hunt 页面会混入推广卡，Codex 采集时必须只保留有真实排名的项目。
- 官网若是 JS-heavy，可能只能抓取首屏、metadata 或可见文案；未知信息必须标注“未公开”。
- 技术栈通常不公开，不能凭感觉写。只能写官网、文档、招聘页、公开 repo、页面源码或 Product Hunt Built-with 明确出现的线索。
- 评论摘要来自 Product Hunt 详情页样本，不代表全网用户反馈。
