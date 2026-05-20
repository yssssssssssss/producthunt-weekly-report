#!/usr/bin/env python3
"""
Collect Product Hunt weekly leaderboard pages through agent-browser.

This is the no-token path. It opens the real Product Hunt page in Codex's
browser session and asks the page's own Apollo client to paginate the
leaderboard. It intentionally records collection notes instead of pretending
that product detail/website enrichment has already happened.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import ph_report


ROOT = Path(__file__).resolve().parents[1]


def run_agent_browser(args: list[str], timeout: int = 60) -> str:
    command = ["agent-browser", "--session", "ph2026", *args]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(command)}\n{result.stderr or result.stdout}")
    return result.stdout.strip()


def js_collect(top: int) -> str:
    return f"""
(async () => {{
  const client = window.__APOLLO_CLIENT__ || window.apolloClient;
  if (!client) throw new Error("Apollo client not found");
  const query = [...client.queryManager.getObservableQueries().values()]
    .find(q => q.options?.query?.definitions?.[0]?.name?.value === "LeaderboardWeeklyPage");
  if (!query) throw new Error("LeaderboardWeeklyPage query not found");

  const seen = new Set();
  const seenRanks = new Set();
  const posts = [];
  const appendEdges = (edges) => {{
    for (const edge of edges || []) {{
      const node = edge?.node;
      if (!node || node.__typename !== "Post" || !node.weeklyRank) continue;
      const rank = Number(node.weeklyRank);
      if (!Number.isFinite(rank) || rank < 1 || rank > {top}) continue;
      if (seen.has(node.id)) continue;
      if (seenRanks.has(rank)) continue;
      seen.add(node.id);
      seenRanks.add(rank);
      posts.push({{
        id: node.id || "",
        name: node.name || "",
        slug: node.slug || "",
        tagline: node.tagline || "",
        weeklyRank: rank,
        dailyRank: node.dailyRank ? Number(node.dailyRank) : null,
        monthlyRank: node.monthlyRank ? Number(node.monthlyRank) : null,
        votesCount: Number(node.latestScore || node.launchDayScore || 0),
        commentsCount: Number(node.commentsCount || 0),
        featuredAt: node.featuredAt || "",
        shortenedUrl: node.shortenedUrl || "",
        thumbnailImageUuid: node.thumbnailImageUuid || "",
        product: node.redirectToProduct || node.product || null,
        topics: (node.topics?.edges || []).map(e => e.node).filter(Boolean)
      }});
    }}
  }};

  let current = query.getCurrentResult().data?.homefeedItems;
  appendEdges(current?.edges || []);
  let pageInfo = current?.pageInfo || {{}};
  let pages = 1;

  while (posts.length < {top} && pageInfo?.hasNextPage && pages < 8) {{
    const response = await query.fetchMore({{ variables: {{ cursor: pageInfo.endCursor, includeLayout: false }} }});
    const next = response?.data?.homefeedItems;
    appendEdges(next?.edges || []);
    pageInfo = next?.pageInfo || {{}};
    pages += 1;
    await new Promise(resolve => setTimeout(resolve, 250));
  }}

  posts.sort((a, b) => a.weeklyRank - b.weeklyRank);
  return JSON.stringify({{
    pageInfo,
    pagesFetched: pages,
    rankedCount: posts.length,
    posts: posts.slice(0, {top})
  }});
}})()
""".strip()


def normalize_post(raw: dict[str, Any]) -> dict[str, Any]:
    product_slug = ((raw.get("product") or {}).get("slug") or raw.get("slug") or "").strip()
    product_url = f"https://www.producthunt.com/products/{product_slug}" if product_slug else ""
    topics = [
        {"name": topic.get("name", ""), "slug": topic.get("slug", "")}
        for topic in raw.get("topics", [])
        if topic.get("name")
    ]
    item = {
        "rank": int(raw["weeklyRank"]),
        "fallback_rank": int(raw["weeklyRank"]),
        "id": str(raw.get("id") or ""),
        "name": raw.get("name") or "",
        "slug": raw.get("slug") or "",
        "tagline": raw.get("tagline") or "",
        "description": raw.get("tagline") or "",
        "product_url": product_url,
        "website_url": "",
        "votes_count": raw.get("votesCount") or 0,
        "comments_count": raw.get("commentsCount") or 0,
        "reviews_count": 0,
        "reviews_rating": 0,
        "featured_at": raw.get("featuredAt") or "",
        "thumbnail_url": raw.get("thumbnailImageUuid") or "",
        "media": [],
        "product_links": [],
        "makers": [],
        "topics": topics,
        "comments": [],
        "website": {
            "status": "pending_detail_enrichment",
            "url": "",
            "final_url": "",
            "title": "",
            "description": "尚未进入官网补充；当前文件先收录 Product Hunt 周榜分页数据。",
            "headings": [],
            "text_excerpt": "",
        },
        "feedback": {
            "sample_size": 0,
            "summary": "尚未进入 Product Hunt 详情页评论区补充；当前文件先收录周榜排名、tagline、topics、votes/comments。",
            "themes": [],
            "top_comments": [],
        },
        "sources": [
            {"type": "producthunt", "url": product_url},
            {"type": "leaderboard", "url": ""},
        ],
    }
    item["analysis"] = ph_report.fallback_analysis(item)
    item["analysis"]["official_details"] = "待补充：需要进入项目官网提取功能、集成、定价和技术线索。"
    item["analysis"]["feedback_summary"] = item["feedback"]["summary"]
    item["analysis"]["technical_signals"] = ph_report.infer_technical_signals(item)
    item["analysis"]["risks"] = "当前仅完成榜单基础抓取。进一步判断前，需要补充官网、详情页评论、价格、集成和公开技术栈。"
    item["analysis"]["verdict"] = "已进入 2026 周榜观察库；待详情页和官网二次富集后再做最终判断。"
    return item


def collect_week(year: int, week: int, top: int, overwrite: bool) -> Path:
    path = ph_report.week_file(year, week)
    if path.exists() and not overwrite:
        print(f"[skip] {path.relative_to(ROOT)} exists")
        return path

    url = ph_report.PH_WEEKLY_URL.format(year=year, week=week)
    print(f"[open] {url}")
    run_agent_browser(["open", url], timeout=60)
    time.sleep(1)
    raw = run_agent_browser(["eval", js_collect(top)], timeout=90)
    parsed = json.loads(json.loads(raw))
    start, end = ph_report.week_bounds(year, week)
    items = [normalize_post(post) for post in parsed["posts"]]
    for item in items:
        item["sources"][1]["url"] = url
    missing_ranks = [rank for rank in range(1, top + 1) if rank not in {item["rank"] for item in items}]
    notes = [
        f"Collected {len(items)} ranked Product Hunt posts through the page Apollo client.",
        "This pass stores leaderboard metadata first; product detail pages, user feedback, and official websites still need enrichment.",
    ]
    if missing_ranks:
        notes.append(f"Missing ranks after duplicate/out-of-range filtering: {missing_ranks}.")

    payload = {
        "schema_version": ph_report.SCHEMA_VERSION,
        "year": year,
        "week": week,
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "source_url": url,
        "generated_at": ph_report.utc_now(),
        "top_n": top,
        "collection_method": "codex_app_agent_browser_apollo_fetch_more",
        "collection_notes": notes,
        "pagination": {
            "pages_fetched": parsed.get("pagesFetched"),
            "ranked_count": parsed.get("rankedCount"),
            "last_page_info": parsed.get("pageInfo"),
        },
        "items": items,
    }

    ph_report.DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[write] {path.relative_to(ROOT)} ({len(items)} products)")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect Product Hunt weekly rankings through agent-browser")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--from-week", type=int, default=1)
    parser.add_argument("--through", default="latest-complete")
    parser.add_argument("--tz", default="Asia/Shanghai")
    parser.add_argument("--top", type=int, default=50)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--max-weeks", type=int, default=0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    through_year, through_week = ph_report.parse_through(args.through, args.tz)
    weeks = ph_report.iter_weeks(args.year, args.from_week, through_year, through_week)
    if args.max_weeks:
        weeks = weeks[: args.max_weeks]
    for year, week in weeks:
        collect_week(year, week, args.top, args.overwrite)
    ph_report.render_report()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        raise
