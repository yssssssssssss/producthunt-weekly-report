# Product Hunt Deep Enrichment Method

This project uses a two-stage workflow. The first stage collects ranked weekly leaderboard metadata. The second stage is the only stage allowed to produce product-level analysis.

## Rule

Do not show product analysis for an item until it has been deep-enriched.

An item is deep-enriched only after these sources have been checked:

1. Product Hunt weekly leaderboard card.
2. Product Hunt product detail page.
3. Product Hunt comments / maker notes visible on the detail page.
4. Official website reached from the `Visit website` link.

If a project has only leaderboard metadata, the HTML must show `待富集` and must not show:

- Product-level risk.
- User feedback summary.
- Technical stack claim.
- Official website conclusion.

## Evidence Standards

- `做什么` must be Chinese, must explain the actual workflow, and may quote the original tagline only as supporting evidence.
- `官网补充` must come from the official website or maker notes, not from Product Hunt topics.
- `技术栈/数据线索` must separate confirmed evidence from unknowns. Product Hunt topics are classification labels, not a technology stack.
- `风险` must describe the product's own execution, market, compliance, privacy, business model, distribution, or adoption risks.
- `用户反馈` must come from Product Hunt detail page comments or reviews. If comments are unavailable, say that clearly.
- Unknown model providers, cloud vendors, backend frameworks, databases, or security architecture must be marked `未公开`.
- Core report sections must be Chinese-only summaries. Project names and topic tags may remain in their original language, but `做什么`, `官网补充`, `技术栈/数据线索`, `市场情况`, `风险`, `用户反馈`, and `判断` must not paste English taglines, maker notes, website copy, or raw comments.

## Commands

Collect leaderboard metadata:

```bash
python3 tools/collect_agent_browser.py --year 2026 --from-week 1 --through latest-complete --top 50
```

Deep-enrich one week:

```bash
python3 tools/enrich_agent_browser.py --week-file data/weeks/2026-W01.json
```

Deep-enrich specific ranks:

```bash
python3 tools/enrich_agent_browser.py --week-file data/weeks/2026-W01.json --ranks 1,2,3
```

Refresh already-enriched data after improving analysis rules, without refetching official websites:

```bash
python3 tools/enrich_agent_browser.py --year 2026 --from-week 1 --through 2026-W20 --no-only-pending --reuse-websites --detail-batch-size 5
```

Validate and render:

```bash
python3 tools/ph_report.py validate
python3 tools/ph_report.py render
```

## Display Contract

The HTML renderer checks `enrichment_status`.

- `enrichment_status = enriched`: show the full analysis sections.
- Otherwise: show only leaderboard metadata and a `待富集` notice.

This prevents collection progress from being mistaken for product research.

## Current No-Token Method

1. Collect weekly leaderboard metadata through the real Product Hunt page Apollo state with `tools/collect_agent_browser.py`.
2. For each ranked item, open/fetch its Product Hunt product detail page through `agent-browser`.
3. Extract the `Visit website` link from the detail page, then fetch the official site.
4. Use the first Product Hunt rich-text block as maker note only when it contains explicit maker-style signals such as `Hi Product Hunt`, `Hey Hunters`, `I built`, or `we built`.
5. Treat remaining rich-text blocks as Product Hunt user feedback samples. If no reliable samples are present, say feedback is limited.
6. Generate analysis only from Product Hunt detail content, official website metadata/text, and visible comments.
7. Keep Product Hunt topics as classification labels only; they are never technology-stack evidence.
8. Run detail pages in small batches, usually `--detail-batch-size 5`, so the browser daemon stays responsive.
9. Reuse existing website objects with `--reuse-websites` when only analysis rules change.
10. Render only Chinese summaries in the HTML core sections; do not display raw English comments or raw English source excerpts.
