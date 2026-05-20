#!/usr/bin/env python3
"""
Build a growing Product Hunt weekly leaderboard report.

The script deliberately keeps dependencies at zero. It uses Product Hunt's
official GraphQL API for rankings/comments, fetches product websites with the
standard library, and renders a static HTML report under docs/index.html.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "weeks"
DOCS_DIR = ROOT / "docs"
REPORT_PATH = DOCS_DIR / "index.html"
PH_API = "https://api.producthunt.com/v2/api/graphql"
PH_WEEKLY_URL = "https://www.producthunt.com/leaderboard/weekly/{year}/{week}/all"
SCHEMA_VERSION = 1
ANALYSIS_FIELDS = [
    "what_it_does",
    "target_users",
    "official_details",
    "technical_signals",
    "market_context",
    "feedback_summary",
    "risks",
    "verdict",
]


def utc_now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()


def week_bounds(year: int, week: int) -> tuple[dt.date, dt.date]:
    start = dt.date.fromisocalendar(year, week, 1)
    return start, start + dt.timedelta(days=6)


def latest_complete_iso_week(tz_name: str) -> tuple[int, int]:
    today = dt.datetime.now(ZoneInfo(tz_name)).date()
    current = today.isocalendar()
    previous = today - dt.timedelta(days=7)
    # Product Hunt weekly pages can exist mid-week, but the current week is
    # still moving. Default to the last completed ISO week.
    if current.week == 1 and today.weekday() < 6:
        previous = today - dt.timedelta(days=7)
    iso = previous.isocalendar()
    return iso.year, iso.week


def week_file(year: int, week: int) -> Path:
    return DATA_DIR / f"{year}-W{week:02d}.json"


def empty_week_payload(year: int, week: int, top_n: int = 50) -> dict[str, Any]:
    start, end = week_bounds(year, week)
    return {
        "schema_version": SCHEMA_VERSION,
        "year": year,
        "week": week,
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "source_url": PH_WEEKLY_URL.format(year=year, week=week),
        "generated_at": utc_now(),
        "top_n": top_n,
        "collection_method": "codex_app_web_research",
        "items": [],
    }


def compact_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def strip_html(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value or "", flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return compact_space(html.unescape(value))


def truncate(value: str, limit: int) -> str:
    value = compact_space(value)
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "..."


class VisibleTextParser(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "iframe"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta_description = ""
        self.meta_keywords = ""
        self.headings: list[str] = []
        self.links: list[dict[str, str]] = []
        self._skip_depth = 0
        self._in_title = False
        self._heading: str | None = None
        self._heading_buffer: list[str] = []
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = {k.lower(): v or "" for k, v in attrs}
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag in {"h1", "h2", "h3"}:
            self._heading = tag
            self._heading_buffer = []
        elif tag == "meta":
            name = (attr.get("name") or attr.get("property") or "").lower()
            content = attr.get("content", "")
            if name in {"description", "og:description", "twitter:description"} and not self.meta_description:
                self.meta_description = compact_space(content)
            elif name == "keywords":
                self.meta_keywords = compact_space(content)
        elif tag == "a" and attr.get("href"):
            self.links.append({"href": attr["href"], "text": ""})

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        elif self._heading == tag:
            text = compact_space(" ".join(self._heading_buffer))
            if text and text not in self.headings:
                self.headings.append(text)
            self._heading = None
            self._heading_buffer = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = compact_space(data)
        if not text:
            return
        if self._in_title:
            self.title = compact_space(f"{self.title} {text}")
        if self._heading:
            self._heading_buffer.append(text)
        self._text.append(text)

    @property
    def text(self) -> str:
        return compact_space(" ".join(self._text))


def http_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
    parsed = json.loads(body)
    if parsed.get("errors"):
        raise RuntimeError(json.dumps(parsed["errors"], ensure_ascii=False, indent=2))
    return parsed


def fetch_url(url: str, timeout: int, max_bytes: int) -> tuple[int | None, str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 ProductHuntWeeklyReport/1.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "")
            raw = response.read(max_bytes + 1)
            text = raw[:max_bytes].decode("utf-8", errors="replace")
            return response.status, response.geturl(), text if "text" in content_type or "html" in content_type else ""
    except urllib.error.HTTPError as exc:
        return exc.code, url, ""
    except Exception:
        return None, url, ""


def extract_website(url: str, timeout: int = 15, max_bytes: int = 300_000) -> dict[str, Any]:
    if not url:
        return {"status": "missing", "url": "", "final_url": "", "title": "", "description": "", "headings": [], "text_excerpt": ""}
    status, final_url, body = fetch_url(url, timeout=timeout, max_bytes=max_bytes)
    if not body:
        return {"status": "failed", "http_status": status, "url": url, "final_url": final_url, "title": "", "description": "", "headings": [], "text_excerpt": ""}
    parser = VisibleTextParser()
    parser.feed(body)
    return {
        "status": "ok",
        "http_status": status,
        "url": url,
        "final_url": final_url,
        "title": truncate(parser.title, 180),
        "description": truncate(parser.meta_description, 500),
        "keywords": [compact_space(k) for k in parser.meta_keywords.split(",") if compact_space(k)][:12],
        "headings": [truncate(h, 180) for h in parser.headings[:12]],
        "text_excerpt": truncate(parser.text, 1800),
    }


PH_QUERY = """
query WeeklyPosts($first: Int!, $after: String, $postedAfter: DateTime!, $postedBefore: DateTime!, $comments: Int!) {
  posts(first: $first, after: $after, featured: true, postedAfter: $postedAfter, postedBefore: $postedBefore, order: RANKING) {
    totalCount
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        slug
        tagline
        description
        url
        website
        votesCount
        commentsCount
        reviewsCount
        reviewsRating
        createdAt
        featuredAt
        dailyRank
        weeklyRank
        monthlyRank
        yearlyRank
        thumbnail { url }
        media { type url videoUrl }
        productLinks { type url }
        makers { name username url headline }
        topics(first: 12) {
          edges { node { name slug } }
        }
        comments(first: $comments, order: VOTES_COUNT) {
          edges {
            node {
              id
              body
              createdAt
              url
              votesCount
              user { name username url headline }
            }
          }
        }
      }
    }
  }
}
"""


@dataclass
class ProductHuntClient:
    token: str
    timeout: int = 30

    def weekly_posts(self, year: int, week: int, top_n: int, comments: int) -> list[dict[str, Any]]:
        start, end = week_bounds(year, week)
        variables = {
            "first": min(max(top_n * 2, top_n), 100),
            "after": None,
            "postedAfter": f"{start.isoformat()}T00:00:00Z",
            "postedBefore": f"{(end + dt.timedelta(days=1)).isoformat()}T00:00:00Z",
            "comments": comments,
        }
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        items: list[dict[str, Any]] = []
        while True:
            payload = {"query": PH_QUERY, "variables": variables}
            parsed = http_json(PH_API, payload, headers, self.timeout)
            connection = parsed["data"]["posts"]
            items.extend(edge["node"] for edge in connection["edges"])
            page_info = connection["pageInfo"]
            if not page_info["hasNextPage"] or len(items) >= top_n:
                break
            variables["after"] = page_info["endCursor"]
        return normalize_posts(items, top_n)


def normalize_posts(posts: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    def rank_key(post: dict[str, Any]) -> tuple[int, int]:
        rank = post.get("weeklyRank")
        if isinstance(rank, int) and rank > 0:
            return rank, -int(post.get("votesCount") or 0)
        return 10_000, -int(post.get("votesCount") or 0)

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for post in posts:
        if post["id"] in seen:
            continue
        seen.add(post["id"])
        unique.append(post)

    unique.sort(key=rank_key)
    normalized: list[dict[str, Any]] = []
    for index, post in enumerate(unique[:top_n], start=1):
        topics = [edge["node"] for edge in (post.get("topics") or {}).get("edges", [])]
        comments = [
            {
                "id": node.get("id"),
                "body": strip_html(node.get("body") or ""),
                "created_at": node.get("createdAt"),
                "url": node.get("url"),
                "votes_count": node.get("votesCount") or 0,
                "user": node.get("user") or {},
            }
            for node in [edge["node"] for edge in (post.get("comments") or {}).get("edges", [])]
        ]
        normalized.append(
            {
                "rank": post.get("weeklyRank") if isinstance(post.get("weeklyRank"), int) else index,
                "fallback_rank": index,
                "id": post.get("id"),
                "name": post.get("name") or "",
                "slug": post.get("slug") or "",
                "tagline": post.get("tagline") or "",
                "description": post.get("description") or "",
                "product_url": post.get("url") or "",
                "website_url": post.get("website") or "",
                "votes_count": post.get("votesCount") or 0,
                "comments_count": post.get("commentsCount") or 0,
                "reviews_count": post.get("reviewsCount") or 0,
                "reviews_rating": post.get("reviewsRating") or 0,
                "featured_at": post.get("featuredAt"),
                "thumbnail_url": ((post.get("thumbnail") or {}).get("url") or ""),
                "media": post.get("media") or [],
                "product_links": post.get("productLinks") or [],
                "makers": post.get("makers") or [],
                "topics": topics,
                "comments": comments,
            }
        )
    normalized.sort(key=lambda item: item["rank"] or item["fallback_rank"])
    return normalized


def infer_target_users(item: dict[str, Any]) -> str:
    text = " ".join(
        [
            item.get("name", ""),
            item.get("tagline", ""),
            item.get("description", ""),
            " ".join(topic.get("name", "") for topic in item.get("topics", [])),
        ]
    ).lower()
    rules = [
        (("developer", "coding", "github", "api", "devtools", "code"), "开发者、工程团队、技术负责人"),
        (("sales", "lead", "crm", "outreach"), "销售团队、增长团队、B2B 创始人"),
        (("marketing", "seo", "content", "brand"), "市场、内容、品牌和增长团队"),
        (("design", "figma", "ui", "prototype"), "设计师、产品经理、前端团队"),
        (("finance", "payment", "billing", "tax"), "SaaS 创始人、财务/支付工程团队"),
        (("recruit", "hiring", "jobs", "candidate"), "招聘团队、HR、求职者"),
        (("ai agent", "agent", "automation", "workflow"), "希望把 AI agent 接入工作流的团队"),
        (("consumer", "social", "photo", "video", "sleep"), "消费者用户或创作者"),
    ]
    matches = [label for keys, label in rules if any(key in text for key in keys)]
    return "；".join(dict.fromkeys(matches)) or "早期采用者、创业团队或该垂直场景的专业用户"


def infer_technical_signals(item: dict[str, Any]) -> str:
    website = item.get("website") or {}
    parts: list[str] = []
    topics = [topic.get("name", "") for topic in item.get("topics", []) if topic.get("name")]
    if topics:
        parts.append("PH topics: " + ", ".join(topics[:6]))
    headings = website.get("headings") or []
    if headings:
        parts.append("官网强调: " + " / ".join(headings[:4]))
    links = item.get("product_links") or []
    if links:
        parts.append("公开链接类型: " + ", ".join(sorted({link.get("type", "") for link in links if link.get("type")})))
    if not parts:
        parts.append("公开技术栈不足，需要后续通过文档、招聘页或工程博客补充。")
    return "；".join(parts)


def summarize_feedback(comments: list[dict[str, Any]], comments_count: int) -> dict[str, Any]:
    if not comments:
        return {
            "sample_size": 0,
            "summary": "Product Hunt 详情页没有抓到可用评论；需要人工复核是否评论较少、API 权限不足或页面反馈集中在外部社区。",
            "themes": [],
            "top_comments": [],
        }
    buckets = {
        "正面认可/祝贺": ("congrats", "great", "love", "awesome", "amazing", "useful", "nice", "cool"),
        "定价/套餐问题": ("pricing", "price", "cost", "free", "paid", "plan"),
        "隐私/安全/数据": ("privacy", "security", "data", "gdpr", "soc2", "hipaa"),
        "集成/兼容性": ("integrat", "api", "slack", "notion", "github", "figma", "zapier"),
        "功能请求/路线图": ("feature", "roadmap", "support", "can you", "would be", "request"),
    }
    joined = "\n".join(comment["body"].lower() for comment in comments)
    themes = [name for name, keys in buckets.items() if any(key in joined for key in keys)]
    question_count = sum(1 for comment in comments if "?" in comment["body"])
    if question_count:
        themes.append(f"有 {question_count} 条评论是问题或澄清请求")
    top_comments = [
        {
            "body": truncate(comment["body"], 300),
            "votes_count": comment.get("votes_count", 0),
            "user": (comment.get("user") or {}).get("name") or (comment.get("user") or {}).get("username") or "",
            "url": comment.get("url") or "",
        }
        for comment in sorted(comments, key=lambda c: c.get("votes_count", 0), reverse=True)[:5]
    ]
    if themes:
        theme_text = "；".join(dict.fromkeys(themes))
        summary = f"抓取到 {len(comments)} 条高票评论/样本，主要反馈集中在：{theme_text}。"
    else:
        summary = f"抓取到 {len(comments)} 条评论样本，整体以发布支持、使用场景讨论和早期问题为主。"
    if comments_count > len(comments):
        summary += f" 详情页总评论数为 {comments_count}，当前仅采样其中 {len(comments)} 条。"
    return {"sample_size": len(comments), "summary": summary, "themes": list(dict.fromkeys(themes)), "top_comments": top_comments}


def fallback_analysis(item: dict[str, Any]) -> dict[str, str]:
    website = item.get("website") or {}
    base = item.get("description") or item.get("tagline") or website.get("description") or ""
    website_detail = website.get("description") or "官网首页没有提供可解析的补充描述。"
    feedback = item.get("feedback", {}).get("summary", "")
    votes = item.get("votes_count", 0)
    comments = item.get("comments_count", 0)
    return {
        "what_it_does": truncate(f"{item.get('tagline', '')} {base}".strip(), 520),
        "target_users": infer_target_users(item),
        "official_details": truncate(website_detail, 520),
        "technical_signals": infer_technical_signals(item),
        "market_context": f"Product Hunt 周榜排名 #{item.get('rank')}，获得 {votes} votes、{comments} comments。可视为早期市场兴趣信号，不等于真实营收或留存。",
        "feedback_summary": feedback,
        "risks": "需要重点核验：官网承诺与真实产品成熟度是否一致、是否有清晰付费路径、是否存在数据/隐私/平台依赖风险。",
        "verdict": "值得进入候选观察池；若要投资或复制，需要继续验证用户留存、付费意愿、获客成本和差异化壁垒。",
    }


def call_openai_analysis(item: dict[str, Any], timeout: int) -> dict[str, str] | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    model = os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
    prompt = {
        "product": {
            "name": item.get("name"),
            "tagline": item.get("tagline"),
            "description": item.get("description"),
            "topics": [topic.get("name") for topic in item.get("topics", [])],
            "votes_count": item.get("votes_count"),
            "comments_count": item.get("comments_count"),
            "website": item.get("website"),
            "feedback": item.get("feedback"),
        },
        "task": "用中文输出 JSON。字段必须包括 what_it_does,target_users,official_details,technical_signals,market_context,feedback_summary,risks,verdict。不要编造技术栈；未知就写未公开。",
    }
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": "你是严谨的产品和市场分析师。只根据给定材料判断，未知信息明确标注未公开。"},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        parsed = http_json("https://api.openai.com/v1/responses", payload, headers, timeout)
    except Exception as exc:
        print(f"[warn] OpenAI analysis failed for {item.get('name')}: {exc}", file=sys.stderr)
        return None
    text = ""
    for chunk in parsed.get("output", []):
        for content in chunk.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                text += content.get("text", "")
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None
    try:
        result = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return {key: compact_space(str(result.get(key, ""))) for key in fallback_analysis(item)}


def enrich_item(item: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if args.fetch_websites:
        item["website"] = extract_website(item.get("website_url", ""), timeout=args.website_timeout)
        time.sleep(args.website_sleep)
    else:
        item.setdefault("website", {})
    item["feedback"] = summarize_feedback(item.get("comments", []), item.get("comments_count", 0))
    analysis = call_openai_analysis(item, args.llm_timeout) if args.llm else None
    item["analysis"] = analysis or fallback_analysis(item)
    item["sources"] = [
        {"type": "producthunt", "url": item.get("product_url", "")},
        {"type": "website", "url": item.get("website", {}).get("final_url") or item.get("website_url", "")},
    ]
    return item


def update_week(year: int, week: int, args: argparse.Namespace) -> Path:
    path = week_file(year, week)
    if path.exists() and not args.overwrite:
        print(f"[skip] {path.relative_to(ROOT)} exists")
        return path
    token = os.getenv("PRODUCT_HUNT_TOKEN")
    if not token:
        raise SystemExit("PRODUCT_HUNT_TOKEN is required. Create a Product Hunt API token and export it before running update.")
    start, end = week_bounds(year, week)
    client = ProductHuntClient(token=token, timeout=args.api_timeout)
    print(f"[fetch] Product Hunt weekly {year}/W{week:02d} ({start}..{end})")
    items = client.weekly_posts(year, week, args.top, args.comments)
    enriched = [enrich_item(item, args) for item in items]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "year": year,
        "week": week,
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "source_url": PH_WEEKLY_URL.format(year=year, week=week),
        "generated_at": utc_now(),
        "top_n": args.top,
        "items": enriched,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[write] {path.relative_to(ROOT)} ({len(enriched)} products)")
    return path


def parse_through(value: str, tz: str) -> tuple[int, int]:
    if value == "latest-complete":
        return latest_complete_iso_week(tz)
    match = re.fullmatch(r"(\d{4})-?W?(\d{1,2})", value)
    if not match:
        raise argparse.ArgumentTypeError("through must be latest-complete or YYYY-Www")
    return int(match.group(1)), int(match.group(2))


def iter_weeks(from_year: int, from_week: int, through_year: int, through_week: int) -> list[tuple[int, int]]:
    weeks: list[tuple[int, int]] = []
    start = dt.date.fromisocalendar(from_year, from_week, 1)
    end = dt.date.fromisocalendar(through_year, through_week, 1)
    cursor = start
    while cursor <= end:
        iso = cursor.isocalendar()
        weeks.append((iso.year, iso.week))
        cursor += dt.timedelta(days=7)
    return weeks


def load_weeks() -> list[dict[str, Any]]:
    weeks: list[dict[str, Any]] = []
    for path in sorted(DATA_DIR.glob("*.json")):
        try:
            weeks.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            print(f"[warn] skip invalid {path}: {exc}", file=sys.stderr)
    weeks.sort(key=lambda item: (item.get("year", 0), item.get("week", 0)))
    return weeks


def validate_week_payload(payload: dict[str, Any], path: Path | None = None) -> list[str]:
    label = str(path.relative_to(ROOT)) if path else f"{payload.get('year')}-W{payload.get('week')}"
    errors: list[str] = []
    for key in ("year", "week", "week_start", "week_end", "source_url", "items"):
        if key not in payload:
            errors.append(f"{label}: missing top-level field {key}")
    items = payload.get("items")
    if not isinstance(items, list):
        errors.append(f"{label}: items must be a list")
        return errors
    top_n = int(payload.get("top_n") or 50)
    if len(items) > top_n:
        errors.append(f"{label}: has {len(items)} items, expected at most {top_n}")
    ranks: set[int] = set()
    for index, item in enumerate(items, start=1):
        prefix = f"{label}: item {index}"
        rank = item.get("rank") or item.get("fallback_rank")
        if not isinstance(rank, int):
            errors.append(f"{prefix}: rank must be an integer")
        elif rank in ranks:
            errors.append(f"{prefix}: duplicate rank {rank}")
        else:
            ranks.add(rank)
        for key in ("name", "tagline", "product_url"):
            if not compact_space(str(item.get(key, ""))):
                errors.append(f"{prefix}: missing {key}")
        analysis = item.get("analysis")
        if not isinstance(analysis, dict):
            errors.append(f"{prefix}: analysis must be an object")
        else:
            for field in ANALYSIS_FIELDS:
                if not compact_space(str(analysis.get(field, ""))):
                    errors.append(f"{prefix}: analysis.{field} is empty")
        feedback = item.get("feedback")
        if not isinstance(feedback, dict):
            errors.append(f"{prefix}: feedback must be an object")
        elif not compact_space(str(feedback.get("summary", ""))):
            errors.append(f"{prefix}: feedback.summary is empty")
        website = item.get("website")
        if not isinstance(website, dict):
            errors.append(f"{prefix}: website must be an object")
    return errors


def render_report() -> Path:
    weeks = load_weeks()
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    total_products = sum(len(week.get("items", [])) for week in weeks)
    latest = weeks[-1] if weeks else None
    display_weeks = json.loads(json.dumps(weeks, ensure_ascii=False))
    for week in display_weeks:
        for item in week.get("items", []):
            website = item.get("website") or {}
            item["website"] = {
                "status": website.get("status", ""),
                "url": website.get("url", ""),
                "final_url": website.get("final_url", ""),
            }
            if isinstance(item.get("feedback"), dict):
                item["feedback"]["top_comments"] = []
            item["comments"] = []
            item["media"] = []
            item["description"] = ""
            item["tagline"] = ""
            item["product_links"] = []
            item["makers"] = []
    embedded = json.dumps({"weeks": display_weeks}, ensure_ascii=False).replace("</", "<\\/")
    updated = utc_now()
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Product Hunt 周榜 Top 50 报告</title>
  <style>
    :root {{
      --bg: #f7f4ef;
      --ink: #181614;
      --muted: #6c6259;
      --line: #ddd5ca;
      --panel: #fffdfa;
      --accent: #da552f;
      --accent-2: #087f8c;
      --soft: #efe8dd;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    a {{ color: var(--accent-2); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    header {{
      padding: 32px clamp(18px, 4vw, 56px) 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    h1 {{ margin: 0 0 8px; font-size: clamp(30px, 5vw, 54px); line-height: 1.02; letter-spacing: 0; }}
    .summary {{ color: var(--muted); max-width: 900px; }}
    .stats {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 20px; }}
    .stat {{ min-width: 150px; padding: 12px 14px; border: 1px solid var(--line); background: var(--soft); border-radius: 8px; }}
    .stat b {{ display: block; font-size: 22px; }}
    main {{ padding: 24px clamp(14px, 3vw, 48px) 56px; }}
    .toolbar {{
      display: grid;
      grid-template-columns: minmax(220px, 1fr) 180px 140px;
      gap: 12px;
      margin-bottom: 18px;
      position: sticky;
      top: 0;
      z-index: 2;
      padding: 12px 0;
      background: var(--bg);
    }}
    input, select {{
      width: 100%;
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--ink);
      border-radius: 8px;
      padding: 10px 12px;
      font: inherit;
    }}
    .week {{ margin: 24px 0 34px; }}
    .week h2 {{ font-size: 24px; margin: 0 0 14px; }}
    .grid {{ display: grid; gap: 14px; }}
    article {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}
    .topline {{ display: flex; gap: 12px; align-items: flex-start; justify-content: space-between; }}
    .rank {{
      flex: 0 0 auto;
      min-width: 44px;
      height: 44px;
      border-radius: 8px;
      display: grid;
      place-items: center;
      background: var(--accent);
      color: white;
      font-weight: 700;
    }}
    h3 {{ margin: 0; font-size: 20px; }}
    .tagline {{ margin: 4px 0 0; color: var(--muted); }}
    .metrics {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
    .pill {{ border: 1px solid var(--line); background: var(--soft); border-radius: 999px; padding: 3px 9px; font-size: 13px; color: var(--muted); }}
    .analysis {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 14px; }}
    .analysis section {{ border-top: 1px solid var(--line); padding-top: 10px; }}
    .analysis h4 {{ margin: 0 0 5px; font-size: 13px; color: var(--accent); text-transform: uppercase; }}
    .analysis p {{ margin: 0; }}
    .comments {{ margin-top: 10px; }}
    .comment {{ margin-top: 8px; padding-left: 10px; border-left: 3px solid var(--line); color: var(--muted); }}
    .pending {{ margin-top: 14px; border-top: 1px solid var(--line); padding-top: 12px; color: var(--muted); }}
    .empty {{ padding: 28px; border: 1px dashed var(--line); background: var(--panel); border-radius: 8px; color: var(--muted); }}
    footer {{ padding: 24px clamp(18px, 4vw, 56px); color: var(--muted); border-top: 1px solid var(--line); }}
    @media (max-width: 760px) {{
      .toolbar {{ grid-template-columns: 1fr; position: static; }}
      .analysis {{ grid-template-columns: 1fr; }}
      .topline {{ flex-direction: column; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Product Hunt 周榜 Top 50</h1>
    <p class="summary">从 2026 年第 1 周开始滚动沉淀 Product Hunt 周榜项目。每个项目保留 Product Hunt 排名、官方介绍、官网抓取信息、用户评论摘要和市场判断。</p>
    <div class="stats">
      <div class="stat"><b>{len(weeks)}</b>周数据</div>
      <div class="stat"><b>{total_products}</b>个项目</div>
      <div class="stat"><b>{html.escape(str(latest.get('year')) + '-W' + str(latest.get('week')).zfill(2)) if latest else '未生成'}</b>最新周</div>
      <div class="stat"><b>{html.escape(updated[:10])}</b>更新时间 UTC</div>
    </div>
  </header>
  <main>
    <div class="toolbar">
      <input id="search" placeholder="搜索项目、介绍、官网内容、评论主题">
      <select id="weekFilter"><option value="">全部周</option></select>
      <select id="rankFilter">
        <option value="50">Top 50</option>
        <option value="20">Top 20</option>
        <option value="10">Top 10</option>
      </select>
    </div>
    <div id="report"></div>
  </main>
  <footer>
    数据来源：Product Hunt 周榜页面、Product Hunt 详情页、项目官网。排名是抓取时刻快照；未进入详情页和官网的项目只展示榜单基础信息。
  </footer>
  <script id="report-data" type="application/json">{embedded}</script>
  <script>
    const DATA = JSON.parse(document.getElementById('report-data').textContent);
    const report = document.getElementById('report');
    const search = document.getElementById('search');
    const weekFilter = document.getElementById('weekFilter');
    const rankFilter = document.getElementById('rankFilter');

    for (const week of DATA.weeks) {{
      const option = document.createElement('option');
      option.value = `${{week.year}}-W${{String(week.week).padStart(2, '0')}}`;
      option.textContent = `${{option.value}} · ${{week.week_start}} → ${{week.week_end}}`;
      weekFilter.appendChild(option);
    }}

    function escapeHtml(value) {{
      return String(value || '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
    }}

    function textFor(item) {{
      return [
        item.name, item.tagline, item.description,
        item.website?.title, item.website?.description, item.website?.text_excerpt,
        ...Object.values(item.analysis || {{}}),
        item.feedback?.summary,
        ...(item.topics || []).map(t => t.name)
      ].join(' ').toLowerCase();
    }}

    function render() {{
      const q = search.value.trim().toLowerCase();
      const selectedWeek = weekFilter.value;
      const maxRank = Number(rankFilter.value);
      report.innerHTML = '';
      let shown = 0;
      for (const week of DATA.weeks) {{
        const weekId = `${{week.year}}-W${{String(week.week).padStart(2, '0')}}`;
        if (selectedWeek && selectedWeek !== weekId) continue;
        const items = (week.items || []).filter(item => {{
          const rank = Number(item.rank || item.fallback_rank || 999);
          if (rank > maxRank) return false;
          if (q && !textFor(item).includes(q)) return false;
          return true;
        }});
        if (!items.length) continue;
        shown += items.length;
        const section = document.createElement('section');
        section.className = 'week';
        section.innerHTML = `<h2>${{weekId}} · ${{escapeHtml(week.week_start)}} → ${{escapeHtml(week.week_end)}} <span class="pill">${{items.length}} 项</span></h2><div class="grid"></div>`;
        const grid = section.querySelector('.grid');
        for (const item of items) {{
          const analysis = item.analysis || {{}};
          const enriched = item.enrichment_status === 'enriched';
          const websiteUrl = item.website?.final_url || item.website_url || '';
          const feedbackThemes = (item.feedback?.themes || []).map(t => `<span class="pill">${{escapeHtml(t)}}</span>`).join('');
          const topics = (item.topics || []).slice(0, 6).map(t => `<span class="pill">${{escapeHtml(t.name)}}</span>`).join('');
          const article = document.createElement('article');
          article.innerHTML = `
            <div class="topline">
              <div style="display:flex; gap:12px; align-items:flex-start;">
                <div class="rank">#${{escapeHtml(item.rank || item.fallback_rank)}}</div>
                <div>
                  <h3>${{escapeHtml(item.name)}}</h3>
                  <div class="metrics">
                    <span class="pill">${{escapeHtml(item.votes_count)}} 票</span>
                    <span class="pill">${{escapeHtml(item.comments_count)}} 条评论</span>
                    <span class="pill">${{enriched ? '已富集' : '待富集'}}</span>
                    ${{topics}}
                  </div>
                </div>
              </div>
              <div>
                ${{item.product_url ? `<a href="${{escapeHtml(item.product_url)}}" target="_blank" rel="noreferrer">详情页</a>` : ''}}
                ${{websiteUrl ? ` · <a href="${{escapeHtml(websiteUrl)}}" target="_blank" rel="noreferrer">官网</a>` : ''}}
              </div>
            </div>
            ${{enriched ? `<div class="analysis">
              <section><h4>做什么</h4><p>${{escapeHtml(analysis.what_it_does)}}</p></section>
              <section><h4>目标用户</h4><p>${{escapeHtml(analysis.target_users)}}</p></section>
              <section><h4>官网补充</h4><p>${{escapeHtml(analysis.official_details)}}</p></section>
              <section><h4>技术栈/数据线索</h4><p>${{escapeHtml(analysis.technical_signals)}}</p></section>
              <section><h4>市场情况</h4><p>${{escapeHtml(analysis.market_context)}}</p></section>
              <section><h4>风险</h4><p>${{escapeHtml(analysis.risks)}}</p></section>
              <section><h4>用户反馈</h4><p>${{escapeHtml(analysis.feedback_summary || item.feedback?.summary)}}</p><div class="comments">${{feedbackThemes}}</div></section>
              <section><h4>判断</h4><p>${{escapeHtml(analysis.verdict)}}</p></section>
            </div>` : `<div class="pending"><span class="pill">待富集</span> 已完成周榜基础采集；尚未进入 Product Hunt 详情页、用户评论区和官网，因此不展示产品级风险、技术栈或反馈结论。</div>`}}`;
          grid.appendChild(article);
        }}
        report.appendChild(section);
      }}
      if (!shown) {{
        report.innerHTML = '<div class="empty">还没有匹配的数据。先让 Codex 按 docs/codex-research-playbook.md 采集周榜，并生成 data/weeks/*.json。</div>';
      }}
    }}
    search.addEventListener('input', render);
    weekFilter.addEventListener('change', render);
    rankFilter.addEventListener('change', render);
    render();
  </script>
</body>
</html>
"""
    REPORT_PATH.write_text(html_text, encoding="utf-8")
    print(f"[write] {REPORT_PATH.relative_to(ROOT)} ({len(weeks)} weeks, {total_products} products)")
    return REPORT_PATH


def cmd_update(args: argparse.Namespace) -> None:
    through_year, through_week = parse_through(args.through, args.tz)
    weeks = iter_weeks(args.year, args.from_week, through_year, through_week)
    if args.max_weeks:
        weeks = weeks[: args.max_weeks]
    for year, week in weeks:
        update_week(year, week, args)
    render_report()


def cmd_render(_: argparse.Namespace) -> None:
    render_report()


def cmd_plan(args: argparse.Namespace) -> None:
    through_year, through_week = parse_through(args.through, args.tz)
    for year, week in iter_weeks(args.year, args.from_week, through_year, through_week):
        start, end = week_bounds(year, week)
        print(f"{year}-W{week:02d}\t{start}\t{end}\t{PH_WEEKLY_URL.format(year=year, week=week)}")


def cmd_missing(args: argparse.Namespace) -> None:
    through_year, through_week = parse_through(args.through, args.tz)
    existing = {path.stem for path in DATA_DIR.glob("*.json")}
    for year, week in iter_weeks(args.year, args.from_week, through_year, through_week):
        week_id = f"{year}-W{week:02d}"
        if week_id not in existing:
            start, end = week_bounds(year, week)
            print(f"{week_id}\t{start}\t{end}\t{PH_WEEKLY_URL.format(year=year, week=week)}")


def cmd_scaffold(args: argparse.Namespace) -> None:
    path = week_file(args.year, args.week)
    if path.exists() and not args.force:
        raise SystemExit(f"{path.relative_to(ROOT)} already exists; pass --force to overwrite")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(empty_week_payload(args.year, args.week, args.top), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[write] {path.relative_to(ROOT)}")


def cmd_validate(args: argparse.Namespace) -> None:
    paths = [Path(p) for p in args.files] if args.files else sorted(DATA_DIR.glob("*.json"))
    errors: list[str] = []
    for path in paths:
        if not path.is_absolute():
            path = ROOT / path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{path}: cannot read JSON: {exc}")
            continue
        errors.extend(validate_week_payload(payload, path))
    if errors:
        print("\n".join(errors), file=sys.stderr)
        raise SystemExit(1)
    print(f"[ok] validated {len(paths)} week file(s)")


def cmd_codex_prompt(args: argparse.Namespace) -> None:
    start, end = week_bounds(args.year, args.week)
    week_id = f"{args.year}-W{args.week:02d}"
    path = week_file(args.year, args.week)
    print(
        f"""Research {week_id} Product Hunt weekly Top {args.top}.

Source URL: {PH_WEEKLY_URL.format(year=args.year, week=args.week)}
Date window: {start} to {end}
Output file: {path.relative_to(ROOT)}

Use Codex app web access/browser capabilities, not Product Hunt API tokens.
For each ranked product:
1. Confirm rank, name, tagline, votes/comments when visible, Product Hunt detail URL, and official website URL.
2. Open the Product Hunt detail page and summarize maker notes and user comments/feedback themes.
3. Open the official website and capture concrete product details, integrations, pricing hints, docs, technical stack only when publicly stated, and target users.
4. Fill every analysis field: {", ".join(ANALYSIS_FIELDS)}.
5. Do not include sponsored/promoted cards as ranked products.

After writing JSON, run:
python3 tools/ph_report.py validate {path.relative_to(ROOT)}
python3 tools/ph_report.py render
"""
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Product Hunt weekly Top 50 report builder")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--year", type=int, default=2026)
    common.add_argument("--from-week", type=int, default=1)
    common.add_argument("--through", default="latest-complete", help="latest-complete or YYYY-Www")
    common.add_argument("--tz", default="Asia/Shanghai")

    update = sub.add_parser("update", parents=[common], help="Optional API-backed fetch; Codex web research does not use this command")
    update.add_argument("--top", type=int, default=50)
    update.add_argument("--comments", type=int, default=20)
    update.add_argument("--overwrite", action="store_true")
    update.add_argument("--max-weeks", type=int, default=0, help="Safety valve for test runs")
    update.add_argument("--api-timeout", type=int, default=30)
    update.add_argument("--website-timeout", type=int, default=15)
    update.add_argument("--website-sleep", type=float, default=0.4)
    update.add_argument("--fetch-websites", action=argparse.BooleanOptionalAction, default=True)
    update.add_argument("--llm", action="store_true", help="Use OpenAI API for richer Chinese analysis when OPENAI_API_KEY is set")
    update.add_argument("--llm-timeout", type=int, default=60)
    update.set_defaults(func=cmd_update)

    render = sub.add_parser("render", help="Regenerate docs/index.html from existing JSON")
    render.set_defaults(func=cmd_render)

    plan = sub.add_parser("plan-weeks", parents=[common], help="Print the week range that update would process")
    plan.set_defaults(func=cmd_plan)

    missing = sub.add_parser("missing-weeks", parents=[common], help="Print week files that still need Codex research")
    missing.set_defaults(func=cmd_missing)

    scaffold = sub.add_parser("scaffold-week", help="Create an empty week JSON shell for Codex research")
    scaffold.add_argument("--year", type=int, required=True)
    scaffold.add_argument("--week", type=int, required=True)
    scaffold.add_argument("--top", type=int, default=50)
    scaffold.add_argument("--force", action="store_true")
    scaffold.set_defaults(func=cmd_scaffold)

    validate = sub.add_parser("validate", help="Validate researched week JSON files")
    validate.add_argument("files", nargs="*")
    validate.set_defaults(func=cmd_validate)

    codex_prompt = sub.add_parser("codex-prompt", help="Print the Codex web-research instructions for one week")
    codex_prompt.add_argument("--year", type=int, required=True)
    codex_prompt.add_argument("--week", type=int, required=True)
    codex_prompt.add_argument("--top", type=int, default=50)
    codex_prompt.set_defaults(func=cmd_codex_prompt)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
