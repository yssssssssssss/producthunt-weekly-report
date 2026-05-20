#!/usr/bin/env python3
"""
Enrich Product Hunt weekly report items without Product Hunt API tokens.

The script uses agent-browser on producthunt.com to fetch Product Hunt product
detail pages, then uses the local website extractor from ph_report.py for
official websites. It generates conservative Chinese analysis from observed
page evidence. Unknown technical stack remains explicit.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import ph_report


ROOT = Path(__file__).resolve().parents[1]
PH_ORIGIN = "https://www.producthunt.com"


def compact(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def has_token(text: str, token: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(token.lower())}(?![a-z0-9])", text.lower()) is not None


def has_any_token(text: str, tokens: tuple[str, ...]) -> bool:
    return any(has_token(text, token) for token in tokens)


def run_agent_browser(args: list[str], timeout: int = 120, retries: int = 2) -> str:
    command = ["agent-browser", "--session", "ph2026", *args]
    last_output = ""
    for attempt in range(retries + 1):
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
        if result.returncode == 0:
            return result.stdout.strip()
        last_output = result.stderr or result.stdout
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{' '.join(command)}\n{last_output}")


def ensure_ph_origin() -> None:
    run_agent_browser(["open", PH_ORIGIN], timeout=60)
    time.sleep(0.5)


def js_fetch_details(items: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        [{"id": item.get("id"), "rank": item.get("rank"), "name": item.get("name"), "url": item.get("product_url")} for item in items],
        ensure_ascii=False,
    )
    return f"""
(async () => {{
  const inputs = {payload};
  const out = [];
  const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
  for (const input of inputs) {{
    try {{
      const url = new URL(input.url, location.origin);
      const html = await fetch(url.pathname + url.search, {{ credentials: "include" }}).then(r => r.text());
      const doc = new DOMParser().parseFromString(html, "text/html");
      const text = (doc.body?.innerText || "").replace(/\\s+/g, " ").trim();
      const visit = [...doc.querySelectorAll("a")].find(a => a.textContent.trim() === "Visit website")?.href || "";
      const headings = [...doc.querySelectorAll("h1,h2,h3")].map(h => h.textContent.replace(/\\s+/g, " ").trim()).filter(Boolean).slice(0, 20);
      const paragraphs = [...doc.querySelectorAll("p")]
        .map(p => p.textContent.replace(/\\s+/g, " ").trim())
        .filter(t => t.length >= 35)
        .filter((t, i, arr) => arr.indexOf(t) === i)
        .slice(0, 80);
      const richTextSections = [...doc.querySelectorAll("div.prose")]
        .map(el => {{
          const text = el.textContent.replace(/\\s+/g, " ").trim();
          let cur = el;
          let ancestorText = "";
          for (let i = 0; i < 8 && cur; i += 1) {{
            ancestorText = `${{ancestorText}} ${{cur.textContent || ""}}`;
            cur = cur.parentElement;
          }}
          ancestorText = ancestorText.replace(/\\s+/g, " ");
          const isComment = /\\bUpvote\\b/i.test(ancestorText) && /\\bReport\\b/i.test(ancestorText) && /\\bShare\\b/i.test(ancestorText);
          return {{ text, is_comment: isComment }};
        }})
        .filter(section => section.text.length >= 25)
        .filter((section, i, arr) => arr.findIndex(other => other.text === section.text) === i)
        .slice(0, 80);
      const links = [...doc.querySelectorAll("a")]
        .map(a => ({{ text: a.textContent.replace(/\\s+/g, " ").trim(), href: a.href || "" }}))
        .filter(a => a.href)
        .slice(0, 80);
      const buttons = [...doc.querySelectorAll("button")]
        .map(b => b.textContent.replace(/\\s+/g, " ").trim())
        .filter(Boolean)
        .filter((t, i, arr) => arr.indexOf(t) === i)
        .slice(0, 50);
      out.push({{
        id: input.id,
        rank: input.rank,
        name: input.name,
        product_url: url.href,
        status: "ok",
        title: doc.title || "",
        headings,
        visit_url: visit,
        paragraphs,
        rich_text_sections: richTextSections,
        links,
        buttons,
        text_excerpt: text.slice(0, 5000)
      }});
      await sleep(250);
    }} catch (error) {{
      out.push({{
        id: input.id,
        rank: input.rank,
        name: input.name,
        product_url: input.url,
        status: "failed",
        error: String(error && error.message || error)
      }});
    }}
  }}
  return JSON.stringify(out);
}})()
""".strip()


def chunks(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def fetch_product_details(items: list[dict[str, Any]], batch_size: int) -> list[dict[str, Any]]:
    if not items:
        return []
    details: list[dict[str, Any]] = []
    for batch in chunks(items, max(1, batch_size)):
        ranks = ",".join(str(item.get("rank")) for item in batch)
        print(f"[detail-batch] ranks={ranks}", flush=True)
        raw = run_agent_browser(["eval", js_fetch_details(batch)], timeout=max(90, len(batch) * 12), retries=3)
        details.extend(json.loads(json.loads(raw)))
    return details


def website_allowed(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.netloc and "producthunt.com" not in parsed.netloc


def fetch_websites(details: list[dict[str, Any]], workers: int, timeout: int) -> dict[str, dict[str, Any]]:
    urls = sorted({detail.get("visit_url", "") for detail in details if website_allowed(detail.get("visit_url", ""))})
    results: dict[str, dict[str, Any]] = {}
    def one(url: str) -> tuple[str, dict[str, Any]]:
        return url, ph_report.extract_website(url, timeout=timeout, max_bytes=220_000)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        for url, website in executor.map(one, urls):
            results[url] = website
    return results


def reuse_existing_websites(items: list[dict[str, Any]], details: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_id = {item.get("id"): item for item in items}
    reused: dict[str, dict[str, Any]] = {}
    for detail in details:
        url = detail.get("visit_url", "")
        item = by_id.get(detail.get("id")) or {}
        website = item.get("website") or {}
        if website_allowed(url) and website.get("status") and website.get("status") != "pending_detail_enrichment":
            reused[url] = website
    return reused


def comments_from_paragraphs(paragraphs: list[str]) -> list[str]:
    comments: list[str] = []
    markers = (
        "@",
        "Congrats",
        "congrats",
        "Congratulations",
        "This is",
        "This would",
        "Love",
        "love",
        "How do",
        "Any plans",
        "have you",
        "Would",
        "What",
        "Great",
        "great",
    )
    for text in paragraphs:
        if len(text) < 45:
            continue
        if text.startswith(markers) or "?" in text:
            comments.append(text)
    if not comments and len(paragraphs) > 8:
        comments = paragraphs[8:14]
    return comments[:8]


def split_detail_rich_text(detail: dict[str, Any]) -> tuple[str, list[str]]:
    maker_pattern = re.compile(
        r"\b(hi product hunt|hello product hunt|hey product hunt|hey hunters|today we(?:'re| are) launching|we built|i built)\b",
        flags=re.I,
    )
    sections = [
        {"text": compact(section.get("text", "")), "is_comment": bool(section.get("is_comment"))}
        for section in detail.get("rich_text_sections", [])
        if isinstance(section, dict) and compact(section.get("text", ""))
    ]
    if sections:
        maker_note = ""
        comments = [section["text"] for section in sections]
        maker_index = next((index for index, section in enumerate(sections[:3]) if maker_pattern.search(section["text"])), None)
        if maker_index is not None:
            maker_note = compact(" ".join(section["text"] for section in sections[: maker_index + 1] if not section["text"].startswith("@")))
            comments = [section["text"] for section in sections[maker_index + 1 :]]
        elif any(section["is_comment"] for section in sections):
            comments = [section["text"] for section in sections if section["is_comment"]]
        return maker_note[:1400], comments[:12]

    blocks = [compact(text) for text in detail.get("rich_text_blocks", []) if compact(text)]
    if blocks:
        maker_note = ""
        comments = blocks
        first = blocks[0]
        if not first.startswith("@") and not re.search(r"\b(upvote|report|share)\b", first, flags=re.I):
            maker_note = first
            comments = blocks[1:]
        return maker_note[:1400], comments[:12]

    paragraphs = detail.get("paragraphs", [])
    selected: list[str] = []
    for text in paragraphs[:14]:
        if text.startswith("@"):
            break
        selected.append(text)
    return compact(" ".join(selected))[:1400], comments_from_paragraphs(paragraphs)


def topic_names(item: dict[str, Any]) -> list[str]:
    return [topic.get("name", "") for topic in item.get("topics", []) if topic.get("name")]


def product_type_focus(product_type: str) -> str:
    mapping = {
        "人工智能音频/语音工具": "语音生成、音频处理、声音交互或内容校验",
        "硬件/智能设备": "线下设备、随身硬件或实体交互",
        "健康/心理健康工具": "睡眠、压力、冥想、健康记录或心理支持",
        "游戏/娱乐产品": "游戏体验、娱乐内容或长期进度",
        "营销/增长工具": "内容生产、渠道分发、品牌增长或获客转化",
        "销售/获客工具": "线索发现、客户沟通、销售流程或成交效率",
        "招聘/人才工具": "候选人筛选、面试协作、岗位匹配或人才运营",
        "金融/支付工具": "支付、账单、税务、财务分析或交易流程",
        "效率工具": "个人或团队工作流、信息整理、提醒、会议或自动化执行",
        "开发者工具": "代码、接口、工程协作、开发工作流或技术集成",
        "设计/创意工具": "设计协作、创意生成、原型制作或视觉生产",
        "社交/社区产品": "社交连接、社区互动、身份展示或关系维护",
        "人工智能工作流工具": "智能体、自动化流程、知识检索或模型驱动任务",
    }
    return mapping.get(product_type, "特定垂直场景中的用户任务")


def confirmed_signal_labels(evidence: str) -> list[str]:
    evidence = evidence.lower()
    signals: list[str] = []
    known = [
        (("e-ink", "eink"), "电子墨水屏"),
        (("nfc",), "近场通信"),
        (("ios",), "苹果移动端"),
        (("android",), "安卓端"),
        (("app store",), "苹果应用商店入口"),
        (("google play",), "安卓应用商店入口"),
        (("chrome extension", "add to chrome", "browser extension"), "浏览器扩展"),
        (("slack",), "团队协作平台集成"),
        (("github",), "代码托管平台集成"),
        (("figma",), "设计工具集成"),
        (("notion",), "知识库工具集成"),
        (("api",), "接口能力"),
        (("sdk",), "开发套件"),
        (("mcp",), "模型上下文协议"),
        (("open source", "open-source"), "开源"),
        (("cuda",), "图形计算加速栈"),
        (("tensorrt",), "推理加速栈"),
        (("cosmos",), "英伟达生成式世界模型"),
        (("end-to-end encryption",), "端到端加密"),
        (("local-first",), "本地优先"),
        (("self-host", "self-hosted"), "自托管"),
        (("stripe",), "支付平台集成"),
        (("shopify",), "电商平台集成"),
        (("wordpress",), "内容管理系统集成"),
        (("ai",), "人工智能能力"),
    ]
    for needles, label in known:
        if has_any_token(evidence, needles) and label not in signals:
            signals.append(label)
    return signals


def infer_product_type(item: dict[str, Any], evidence: str) -> str:
    text = f"{item.get('name','')} {item.get('tagline','')} {' '.join(topic_names(item))} {evidence}".lower()
    rules = [
        (("tts", "voice", "audio", "watermarking", "deepfake"), "人工智能音频/语音工具"),
        (("nfc", "e-ink", "wearable", "physical device"), "硬件/智能设备"),
        (("meditation", "mental health", "anxiety", "sleep", "health & fitness"), "健康/心理健康工具"),
        (("game", "games", "pokemon", "tower defense"), "游戏/娱乐产品"),
        (("marketing", "seo", "content", "social media", "creator"), "营销/增长工具"),
        (("sales", "lead", "crm", "outreach"), "销售/获客工具"),
        (("hiring", "recruit", "candidate", "jobs"), "招聘/人才工具"),
        (("finance", "payment", "billing", "invoice", "tax"), "金融/支付工具"),
        (("meeting", "calendar", "slack", "notion"), "效率工具"),
        (("developer", "github", "api", "code", "sdk", "mcp"), "开发者工具"),
        (("productivity", "focus", "routine"), "效率工具"),
        (("design", "figma", "prototype", "ui"), "设计/创意工具"),
        (("social", "networking", "community"), "社交/社区产品"),
        (("ai", "agent", "automation", "workflow"), "人工智能工作流工具"),
    ]
    for keys, label in rules:
        if any(key in text for key in keys if key != "ai") or ("ai" in keys and has_token(text, "ai")):
            return label
    return "垂直场景产品"


def infer_users(item: dict[str, Any], evidence: str) -> str:
    text = f"{item.get('tagline','')} {' '.join(topic_names(item))} {evidence}".lower()
    users: list[str] = []
    if any(k in text for k in ("procrastination", "routine", "alarm", "app blocking", "focus app")):
        users.append("希望减少拖延、强化自律、管理学习/工作专注时间的个人用户")
    if any(k in text for k in ("conference", "meetup", "network", "business card", "sales")):
        users.append("高频参加会议、展会、商务社交和销售拜访的人")
    if any(k in text for k in ("developer", "github", "code", "mcp")) or has_token(text, "api"):
        users.append("开发者、工程团队和技术负责人")
    if any(k in text for k in ("marketing", "seo", "content", "brand", "social")):
        users.append("市场、内容、品牌和增长团队")
    if any(k in text for k in ("design", "figma", "prototype")) or has_token(text, "ui"):
        users.append("设计师、产品经理和前端团队")
    if any(k in text for k in ("hiring", "candidate", "recruit", "job")):
        users.append("招聘团队、HR、候选人或求职者")
    if any(k in text for k in ("payment", "billing", "invoice", "tax", "finance")):
        users.append("SaaS 创始人、财务/支付工程团队")
    if any(k in text for k in ("workflow", "automation", "agent")) or "ai agent" in text:
        users.append("希望把 AI agent 或自动化接入工作流的团队")
    if not users:
        users.append("该垂直场景的早期采用者、创业团队或专业用户")
    return "；".join(dict.fromkeys(users))


def technical_signals(item: dict[str, Any], detail: dict[str, Any], website: dict[str, Any]) -> str:
    maker_note, _ = split_detail_rich_text(detail)
    evidence = " ".join(
        [
            item.get("tagline", ""),
            maker_note,
            website.get("title", ""),
            website.get("description", ""),
            " ".join(website.get("headings", [])),
        ]
    ).lower()
    signals = confirmed_signal_labels(evidence)
    parts: list[str] = []
    if signals:
        parts.append("公开页面可确认的技术/数据线索：" + "、".join(signals))
    else:
        parts.append("公开页面未披露明确技术栈")
    parts.append("分类标签只作为主题参考，不等同于技术栈。")
    parts.append("未公开信息不做推断：后端框架、数据库、云厂商、模型供应商、安全架构等只有在官网、文档、源码或招聘页明确出现时才记录。")
    return "；".join(parts)


def feedback_summary(item: dict[str, Any], comments: list[str], count: int) -> tuple[dict[str, Any], str]:
    if not comments:
        summary = "详情页可解析评论样本有限；当前仅确认该项目有评论互动，尚不足以归纳稳定用户反馈主题。"
        return {"sample_size": 0, "summary": summary, "themes": [], "top_comments": []}, summary
    joined = " ".join(comments).lower()
    themes: list[str] = []
    if any(k in joined for k in ("congrats", "great", "love", "awesome", "must-have", "useful")):
        themes.append("正面认可和发布支持")
    if "?" in " ".join(comments):
        themes.append("功能、集成或落地方式追问")
    if any(k in joined for k in ("pricing", "price", "free", "cost")):
        themes.append("价格/套餐关注")
    if any(k in joined for k in ("privacy", "security", "data", "sync", "analytics")):
        themes.append("数据、隐私或分析能力关注")
    if any(k in joined for k in ("conference", "meetup", "team", "company", "enterprise")):
        themes.append("团队/活动/企业场景讨论")
    if not themes:
        themes.append("早期使用场景讨论")
    summary = f"详情页解析到 {len(comments)} 条评论样本，总评论数显示为 {count}。主要反馈集中在：" + "；".join(themes) + "。"
    top_comments = [
        {"body": comment[:320], "votes_count": 0, "user": "", "url": item.get("product_url", "") + "#comments"}
        for comment in comments[:5]
    ]
    return {"sample_size": len(comments), "summary": summary, "themes": themes, "top_comments": top_comments}, summary


def market_context(item: dict[str, Any], product_type: str) -> str:
    rank = item.get("rank")
    votes = item.get("votes_count", 0)
    comments = item.get("comments_count", 0)
    return f"它属于{product_type}方向，周榜排名第 {rank}，获得 {votes} 票、{comments} 条评论。这个数据说明早期受众愿意讨论和投票，但仍只是发布热度信号；真正市场情况还需要继续验证付费转化、留存、获客渠道和替代方案强度。分类标签已保留在项目卡片上方，本栏不把标签当作市场结论。"


def risk_text(product_type: str, item: dict[str, Any], tech: str, comments: list[str]) -> str:
    topics = " ".join(topic_names(item)).lower()
    risks: list[str] = []
    if "硬件" in product_type or "hardware" in topics:
        risks.append("硬件交付、供应链、售后和库存会显著增加执行难度")
    if "人工智能" in tech or has_token(item.get("tagline", "") + " ".join(topic_names(item)), "ai"):
        risks.append("人工智能能力需要证明稳定性、成本可控和相对普通自动化的差异")
    if any(k in topics for k in ("social", "networking", "community")):
        risks.append("社交/网络效应产品需要解决冷启动和持续使用频率")
    if any(k in topics for k in ("finance", "health", "legal")):
        risks.append("高敏感领域要额外验证合规、隐私和信任成本")
    if comments and any("?" in c for c in comments):
        risks.append("评论区已有用户追问关键落地细节，需要官方给出更清楚的实现或路线图")
    risks.append("榜单热度不能直接证明收入、留存或规模化获客")
    return "；".join(dict.fromkeys(risks)) + "。"


def chinese_what_it_does(item: dict[str, Any], product_type: str, users: str, website: dict[str, Any]) -> str:
    focus = product_type_focus(product_type)
    site_status = website.get("status", "")
    site_note = "官网已可解析" if site_status == "ok" else "官网信息可解析度有限"
    return (
        f"{item.get('name')} 是一个{product_type}，核心围绕{focus}展开。"
        f"它面向{users}，目标是把相关流程做得更清晰、更自动或更容易持续执行。"
        f"当前已核验榜单详情页和官网入口，{site_note}；具体价格、交付成熟度和长期留存仍需要继续跟踪。"
    )


def chinese_official_details(product_type: str, website: dict[str, Any], tech: str) -> str:
    if website.get("status") == "ok":
        base = f"官网可访问，公开页面显示该项目围绕{product_type}提供产品入口和功能说明。"
    elif website.get("status") == "missing":
        base = "详情页没有提供可用官网入口，因此官网补充有限。"
    else:
        base = "已从详情页取得官网入口，但自动抓取没有解析到足够正文，官网补充需要后续人工复核。"
    if "公开页面可确认的技术/数据线索：" in tech:
        signal_part = tech.split("；", 1)[0].replace("公开页面可确认的技术/数据线索：", "")
        base += f"可确认的公开线索包括：{signal_part}。"
    else:
        base += "公开页面暂未披露明确技术栈或集成细节。"
    base += "未看到明确披露的价格、模型供应商、云厂商、数据库或后端框架时，统一按未公开处理。"
    return base


def enrich_item(item: dict[str, Any], detail: dict[str, Any], website: dict[str, Any]) -> dict[str, Any]:
    maker_note, comments = split_detail_rich_text(detail)
    feedback, feedback_text = feedback_summary(item, comments, item.get("comments_count", 0))
    item["website_url"] = detail.get("visit_url") or item.get("website_url", "")
    item["website"] = website or {"status": "missing", "url": item.get("website_url", ""), "final_url": item.get("website_url", ""), "title": "", "description": "", "headings": [], "text_excerpt": ""}
    item["feedback"] = feedback
    product_type = infer_product_type(item, maker_note + " " + website.get("text_excerpt", ""))
    users = infer_users(item, maker_note + " " + website.get("text_excerpt", ""))
    tech = technical_signals(item, detail, website)
    item["analysis"] = {
        "what_it_does": compact(chinese_what_it_does(item, product_type, users, website)),
        "target_users": users,
        "official_details": chinese_official_details(product_type, website, tech),
        "technical_signals": tech,
        "market_context": market_context(item, product_type),
        "feedback_summary": feedback_text,
        "risks": risk_text(product_type, item, tech, comments),
        "verdict": f"已完成详情页和官网级别的基础富集。{item.get('name')} 值得继续观察，但下一步应验证真实用户留存、付费意愿、核心功能是否如发布页承诺，以及与现有替代方案相比的差异是否足够强。",
    }
    item["comments"] = [{"body": c, "votes_count": 0, "url": item.get("product_url", "") + "#comments"} for c in comments[:8]]
    item["enrichment_status"] = "enriched"
    item["enriched_at"] = ph_report.utc_now()
    sources = [
        {"type": "producthunt", "url": item.get("product_url", "")},
        {"type": "website", "url": item.get("website_url", "")},
    ]
    item["sources"] = [source for source in sources if source["url"]]
    return item


def select_items(week: dict[str, Any], ranks: set[int] | None, only_pending: bool, limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in week.get("items", []):
        if ranks and item.get("rank") not in ranks:
            continue
        if only_pending and item.get("enrichment_status") == "enriched":
            continue
        items.append(item)
        if limit and len(items) >= limit:
            break
    return items


def enrich_week(path: Path, args: argparse.Namespace) -> int:
    week = json.loads(path.read_text(encoding="utf-8"))
    ranks = {int(r) for r in args.ranks.split(",") if r.strip()} if args.ranks else None
    items = select_items(week, ranks, args.only_pending, args.limit)
    if not items:
        print(f"[skip] {path.name}: no matching items")
        return 0
    print(f"[detail] {path.name}: {len(items)} item(s)", flush=True)
    details = fetch_product_details(items, args.detail_batch_size)
    detail_by_id = {detail.get("id"): detail for detail in details}
    websites = reuse_existing_websites(items, details) if args.reuse_websites else {}
    missing_details = [detail for detail in details if website_allowed(detail.get("visit_url", "")) and detail.get("visit_url", "") not in websites]
    websites.update(fetch_websites(missing_details, args.website_workers, args.website_timeout))
    changed = 0
    for item in week.get("items", []):
        if item not in items:
            continue
        detail = detail_by_id.get(item.get("id")) or {}
        website = websites.get(detail.get("visit_url", "")) or {}
        enrich_item(item, detail, website)
        changed += 1
    week["generated_at"] = ph_report.utc_now()
    note = f"Deep-enriched {changed} item(s) via Product Hunt detail pages and official websites."
    notes = week.setdefault("collection_notes", [])
    if note not in notes:
        notes.append(note)
    path.write_text(json.dumps(week, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[write] {path.relative_to(ROOT)} enriched={changed}", flush=True)
    return changed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deep-enrich Product Hunt weekly JSON files")
    parser.add_argument("--week-file", action="append", help="Specific data/weeks/YYYY-Www.json file; may repeat")
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--from-week", type=int, default=1)
    parser.add_argument("--through", default="latest-complete")
    parser.add_argument("--tz", default="Asia/Shanghai")
    parser.add_argument("--ranks", help="Comma-separated ranks to enrich, e.g. 1,2,3")
    parser.add_argument("--limit", type=int, default=0, help="Per-week item limit")
    parser.add_argument("--only-pending", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--website-workers", type=int, default=6)
    parser.add_argument("--website-timeout", type=int, default=6)
    parser.add_argument("--detail-batch-size", type=int, default=8)
    parser.add_argument("--reuse-websites", action="store_true", help="Reuse already captured website objects when refreshing Product Hunt detail analysis")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    ensure_ph_origin()
    if args.week_file:
        paths = [Path(p) if Path(p).is_absolute() else ROOT / p for p in args.week_file]
    else:
        through_year, through_week = ph_report.parse_through(args.through, args.tz)
        paths = [ph_report.week_file(year, week) for year, week in ph_report.iter_weeks(args.year, args.from_week, through_year, through_week)]
    total = 0
    for path in paths:
        if not path.exists():
            print(f"[warn] missing {path.relative_to(ROOT)}", file=sys.stderr)
            continue
        total += enrich_week(path, args)
    ph_report.render_report()
    print(f"[done] enriched {total} item(s)")


if __name__ == "__main__":
    main()
