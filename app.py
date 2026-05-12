from __future__ import annotations

from collections import Counter, defaultdict
from calendar import month_name
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from io import BytesIO
import json
import os
import re
import time
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, quote_plus, unquote, urlencode, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import BaseDocTemplate, Flowable, Frame, HRFlowable, Image, PageTemplate, Paragraph, Spacer, Table, TableStyle
import trafilatura
try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None

from env_loader import load_local_env


class CircularImage(Flowable):
    def __init__(self, path: str, width: float, height: float) -> None:
        super().__init__()
        self.path = path
        self.width = width
        self.height = height

    def wrap(self, availWidth: float, availHeight: float) -> tuple[float, float]:
        return self.width, self.height

    def draw(self) -> None:
        c = self.canv
        c.saveState()
        radius = min(self.width, self.height) / 2
        path = c.beginPath()
        path.circle(self.width / 2, self.height / 2, radius)
        c.clipPath(path, stroke=0, fill=0)
        c.drawImage(
            self.path,
            0,
            0,
            width=self.width,
            height=self.height,
            preserveAspectRatio=True,
            mask="auto",
        )
        c.restoreState()

load_local_env()

from database import data_status, load_recent_news, parse_time, save_news_items
from data import COUNTRIES, REGIONS, REGION_COUNTRIES
from iea_policies import build_policy_report, fetch_iea_policies
from isa_dashboard_data import (
    build_infrastructure_context,
    build_market_context,
    build_report_asset_context,
    country_name,
    get_filter_options,
    load_interconnections,
    top_country_codes_by_capacity,
)
from live_market import build_live_market_context, build_report_metric_rows


app = Flask(__name__)

NEWS_TIMEOUT_SECONDS = 12
GEMINI_TIMEOUT_SECONDS = 45
AI_SOURCE_ITEM_LIMIT = 8
AI_SUMMARY_CHAR_LIMIT = 500
PDF_AI_SOURCE_ITEM_LIMIT = 14
PDF_AI_SUMMARY_CHAR_LIMIT = 900
FALLBACK_DATE = datetime(1970, 1, 1, tzinfo=timezone.utc)
NEWS_API_KEY = os.environ.get("NEWSAPI_KEY", "").strip()
GNEWS_API_KEY = os.environ.get("GNEWS_API_KEY", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash").strip()
DEFAULT_TOPICS = [
    "electricity market OR power grid OR renewable energy",
    '"International Solar Alliance"',
]
PDF_NEWS_KEYWORDS = [
    "Grid",
    "Power",
    "Cross border interconnection",
    "Crossborder interconnection",
    "Cross border transmission",
    "Crossborder transmission",
    "Regional Grid Interconnection",
    "Interconnection",
]
PDF_ARCHIVE_QUERY_VARIANTS = [
    "electricity interconnection",
    "power grid electricity",
    "cross border transmission electricity",
    "regional grid interconnection electricity",
    "cross border power trade electricity",
]
DEFAULT_FILTER_MODE = "region"
DEFAULT_REGION = "World"
DEFAULT_COUNTRY = "India"
DEFAULT_PDF_MONTH = datetime.now().month
DEFAULT_PDF_YEAR = datetime.now().year
PDF_LOGO_PATH = os.path.join(app.root_path if 'app' in globals() else os.path.dirname(__file__), "static", "logo.png")
AI_CACHE_TTL_SECONDS = 1800
_AI_NEWSLETTER_CACHE: dict[str, dict] = {}
STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "from",
    "into",
    "about",
    "after",
    "over",
    "amid",
    "will",
    "this",
    "have",
    "more",
    "than",
    "their",
    "its",
    "your",
    "what",
    "based",
    "latest",
    "news",
    "international",
    "solar",
    "alliance",
    "electricity",
}

TAG_RE = re.compile(r"<[^>]+>")
META_DESC_RE = re.compile(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', re.IGNORECASE)
OG_DESC_RE = re.compile(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']', re.IGNORECASE)
PARAGRAPH_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
DDG_RESULT_RE = re.compile(r'href="(?P<href>[^"]+)"', re.IGNORECASE)


@dataclass
class NewsItem:
    topic: str
    title: str
    source: str
    link: str
    published_at: datetime | None
    summary: str


def topic_slug(topic: str) -> str:
    return "".join(char.lower() if char.isalnum() else "-" for char in topic).strip("-")


def build_rss_url(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-IN&gl=IN&ceid=IN:en"


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = TAG_RE.sub(" ", unescape(value))
    return " ".join(text.replace("<![CDATA[", "").replace("]]>", "").split())


def extract_source(raw_title: str) -> tuple[str, str]:
    title = clean_text(raw_title)
    if " - " in title:
        headline, source = title.rsplit(" - ", 1)
        return headline.strip(), source.strip()
    return title, "Unknown source"


def parse_pub_date(raw_date: str | None) -> datetime | None:
    if not raw_date:
        return None
    try:
        parsed = parsedate_to_datetime(raw_date)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None


def parse_feed(xml_text: str, topic: str) -> list[NewsItem]:
    root = ElementTree.fromstring(xml_text)
    items: list[NewsItem] = []
    for node in root.findall("./channel/item"):
        title, source = extract_source(node.findtext("title"))
        item = NewsItem(
            topic=topic,
            title=title,
            source=source,
            link=clean_text(node.findtext("link")),
            published_at=parse_pub_date(node.findtext("pubDate")),
            summary=clean_text(node.findtext("description")),
        )
        if item.link and item.title:
            items.append(item)
    return items


def fetch_topic_news(topic: str, limit: int = 6) -> list[NewsItem]:
    with urlopen(build_rss_url(topic), timeout=NEWS_TIMEOUT_SECONDS) as response:
        payload = response.read().decode("utf-8")
    items = parse_feed(payload, topic)
    items.sort(key=lambda item: item.published_at or FALLBACK_DATE, reverse=True)
    return items[:limit]


def build_newsapi_url(
    query: str,
    page_size: int,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    page: int = 1,
) -> str:
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": str(page_size),
        "page": str(page),
        "apiKey": NEWS_API_KEY,
    }
    if from_date:
        params["from"] = from_date.strftime("%Y-%m-%d")
    if to_date:
        params["to"] = to_date.strftime("%Y-%m-%d")
    return f"https://newsapi.org/v2/everything?{urlencode(params)}"


def build_gnews_url(
    query: str,
    max_items: int,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    page: int = 1,
) -> str:
    params = {
        "q": query,
        "lang": "en",
        "max": str(min(max_items, 10)),
        "page": str(page),
        "sortby": "publishedAt",
        "apikey": GNEWS_API_KEY,
    }
    if from_date:
        params["from"] = from_date.strftime("%Y-%m-%dT00:00:00Z")
    if to_date:
        params["to"] = to_date.strftime("%Y-%m-%dT23:59:59Z")
    return f"https://gnews.io/api/v4/search?{urlencode(params)}"


def parse_iso_datetime(raw_date: str | None) -> datetime | None:
    if not raw_date:
        return None
    normalized = raw_date.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def fetch_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=NEWS_TIMEOUT_SECONDS) as response:
        payload = response.read().decode("utf-8")
    return json.loads(payload)


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=NEWS_TIMEOUT_SECONDS) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="ignore")


def resolve_article_url(link: str, title: str, source: str) -> str:
    if not link:
        return ""
    if "news.google.com" not in link:
        return link

    query = quote_plus(f'{title} {source}')
    search_url = f"https://duckduckgo.com/html/?q={query}"

    try:
        html = fetch_text(search_url)
    except (HTTPError, URLError, TimeoutError, ValueError):
        return link

    for match in DDG_RESULT_RE.finditer(html):
        href = unescape(match.group("href"))
        if "uddg=" in href:
            parsed = urlparse(href)
            target = parse_qs(parsed.query).get("uddg", [""])[0]
            candidate = unquote(target)
        else:
            candidate = href

        if not candidate.startswith("http"):
            continue
        if "duckduckgo.com" in candidate or "news.google.com" in candidate:
            continue
        return candidate

    return link


def extract_article_paragraph(url: str) -> str:
    if not url:
        return ""

    downloaded = trafilatura.fetch_url(url)
    if downloaded:
        extracted = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            favor_precision=True,
        )
        if extracted:
            paragraphs = [part.strip() for part in extracted.split("\n") if part.strip()]
            for paragraph in paragraphs:
                if len(paragraph) > 80:
                    return paragraph
            if paragraphs:
                return paragraphs[0]
    return ""


def extract_first_paragraph(html: str) -> str:
    for pattern in (OG_DESC_RE, META_DESC_RE):
        match = pattern.search(html)
        if match:
            candidate = clean_text(match.group(1))
            if len(candidate) > 80:
                return candidate

    for match in PARAGRAPH_RE.finditer(html):
        candidate = clean_text(match.group(1))
        if len(candidate) > 80:
            return candidate

    return ""


def fetch_newsapi_topic_news(
    topic: str,
    limit: int = 6,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    page: int = 1,
) -> list[NewsItem]:
    if not NEWS_API_KEY:
        return []
    payload = fetch_json(build_newsapi_url(topic, limit, from_date=from_date, to_date=to_date, page=page))
    articles = payload.get("articles", [])
    items: list[NewsItem] = []
    for article in articles:
        title = clean_text(article.get("title"))
        source_name = clean_text((article.get("source") or {}).get("name")) or "NewsAPI"
        item = NewsItem(
            topic=topic,
            title=title,
            source=source_name,
            link=clean_text(article.get("url")),
            published_at=parse_iso_datetime(article.get("publishedAt")),
            summary=clean_text(article.get("description")),
        )
        if item.link and item.title:
            items.append(item)
    return items


def fetch_gnews_topic_news(
    topic: str,
    limit: int = 6,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    page: int = 1,
) -> list[NewsItem]:
    if not GNEWS_API_KEY:
        return []
    payload = fetch_json(build_gnews_url(topic, min(limit, 10), from_date=from_date, to_date=to_date, page=page))
    articles = payload.get("articles", [])
    items: list[NewsItem] = []
    for article in articles:
        source_name = clean_text((article.get("source") or {}).get("name")) or "GNews"
        item = NewsItem(
            topic=topic,
            title=clean_text(article.get("title")),
            source=source_name,
            link=clean_text(article.get("url")),
            published_at=parse_iso_datetime(article.get("publishedAt")),
            summary=clean_text(article.get("description")),
        )
        if item.link and item.title:
            items.append(item)
    return items


def dedupe_news(items: list[NewsItem]) -> list[NewsItem]:
    seen: set[tuple[str, str]] = set()
    deduped: list[NewsItem] = []
    for item in items:
        key = (item.title.casefold(), item.link)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    deduped.sort(key=lambda item: item.published_at or FALLBACK_DATE, reverse=True)
    return deduped


def keyword_highlights(items: Iterable[NewsItem], limit: int = 4) -> list[str]:
    words: list[str] = []
    for item in items:
        for word in item.title.lower().split():
            cleaned = "".join(char for char in word if char.isalnum())
            if len(cleaned) < 5 or cleaned in STOPWORDS:
                continue
            words.append(cleaned)
    counts = Counter(words)
    return [word.title() for word, _ in counts.most_common(limit)]


def build_editor_note(items: list[NewsItem], topics: list[str]) -> str:
    if not items:
        return "No current headlines were available when this newsletter was generated."

    sources = ", ".join(sorted({item.source for item in items[:5]}))
    themes = keyword_highlights(items)
    theme_text = ", ".join(themes) if themes else "investment, grids, and solar deployment"
    return (
        f"This issue tracks {len(items)} fresh headlines across {len(topics)} focus areas. "
        f"The current story mix emphasizes {theme_text.lower()}, with recent coverage sourced from {sources}."
    )


def build_takeaways(items: list[NewsItem]) -> list[str]:
    if not items:
        return ["Refresh the page later to pull in current reporting."]

    titles = " ".join(item.title.lower() for item in items)
    takeaways: list[str] = []

    if any(word in titles for word in ["grid", "transmission", "outage", "demand"]):
        takeaways.append("Electricity coverage is centering on grid reliability, rising demand, and transmission readiness.")
    if any(word in titles for word in ["solar", "storage", "battery", "renewable"]):
        takeaways.append("Solar and storage remain tightly linked in the news cycle, pointing to system flexibility as a key policy theme.")
    if any(word in titles for word in ["finance", "investment", "funding", "bank"]):
        takeaways.append("Capital access and project financing are recurring signals, which matters for ISA-linked deployment programs.")
    if any(word in titles for word in ["africa", "india", "island", "global", "member"]):
        takeaways.append("The geographic spread of coverage suggests the conversation is still strongly international rather than market-specific.")

    if not takeaways:
        takeaways.append("The latest headlines show a broad mix of policy, infrastructure, and deployment stories across the power sector.")

    return takeaways[:3]


def range_to_datetimes(from_month: int, from_year: int, to_month: int, to_year: int) -> tuple[datetime, datetime]:
    start = datetime(from_year, from_month, 1, tzinfo=timezone.utc)
    if to_month == 12:
        next_month = datetime(to_year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(to_year, to_month + 1, 1, tzinfo=timezone.utc)
    end = next_month.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end


def generate_archive_newsletter(
    topics: list[str],
    from_month: int,
    from_year: int,
    to_month: int,
    to_year: int,
    per_topic_limit: int = 25,
) -> dict:
    all_items: list[NewsItem] = []
    errors: list[str] = []
    from_date, end_boundary = range_to_datetimes(from_month, from_year, to_month, to_year)
    to_date = end_boundary - timedelta(days=1)

    if not NEWS_API_KEY and not GNEWS_API_KEY:
        errors.append("Historical PDF generation needs NEWSAPI_KEY or GNEWS_API_KEY for archive-style date range fetching.")

    for topic in topics:
        topic_items: list[NewsItem] = []
        provider_failed = False
        provider_returned_data = False

        for fetcher in (fetch_newsapi_topic_news, fetch_gnews_topic_news):
            for page in range(1, 3):
                try:
                    batch = fetcher(
                        topic,
                        limit=min(per_topic_limit, 25),
                        from_date=from_date,
                        to_date=to_date,
                        page=page,
                    )
                except (HTTPError, URLError, TimeoutError, ElementTree.ParseError, json.JSONDecodeError):
                    provider_failed = True
                    break
                if batch:
                    provider_returned_data = True
                    topic_items.extend(batch)
                if len(batch) < min(per_topic_limit, 25):
                    break

        if not provider_returned_data and provider_failed:
            try:
                topic_items.extend(fetch_topic_news(topic, limit=min(per_topic_limit, 10)))
            except (HTTPError, URLError, TimeoutError, ElementTree.ParseError, json.JSONDecodeError):
                errors.append(f"Could not load archive headlines for {topic}.")

        all_items.extend(topic_items)

    all_items = dedupe_news(all_items)
    range_filtered_items = filter_news_by_range(
        all_items,
        from_month=from_month,
        from_year=from_year,
        to_month=to_month,
        to_year=to_year,
    )
    if range_filtered_items:
        all_items = range_filtered_items

    return {
        "generated_at": datetime.now().strftime("%B %d, %Y at %I:%M %p"),
        "topics": topics,
        "items": all_items[:per_topic_limit],
        "editor_note": build_editor_note(all_items, topics),
        "takeaways": build_takeaways(all_items),
        "top_story": all_items[0] if all_items else None,
        "errors": errors,
    }


def ai_newsletter_cache_key(items: list[NewsItem], mode: str, value: str, profile: str = "web") -> str:
    payload = {
        "mode": mode,
        "value": value,
        "profile": profile,
        "items": [
            {
                "title": item.title,
                "source": item.source,
                "published_at": item.published_at.isoformat() if item.published_at else "",
            }
            for item in items[:10]
        ],
    }
    return json.dumps(payload, sort_keys=True)


def build_ai_newsletter_request(newsletter_data: dict, mode: str, value: str, profile: str = "web") -> tuple[str, dict]:
    item_limit = PDF_AI_SOURCE_ITEM_LIMIT if profile == "pdf" else AI_SOURCE_ITEM_LIMIT
    summary_limit = PDF_AI_SUMMARY_CHAR_LIMIT if profile == "pdf" else AI_SUMMARY_CHAR_LIMIT
    source_items = []
    for item in newsletter_data["items"][:item_limit]:
        source_items.append(
            {
                "title": item.title,
                "source": item.source,
                "date": item.published_at.strftime("%d %b %Y") if item.published_at else "Unknown date",
                "summary": item.summary[:summary_limit],
            }
        )

    prompt_payload = {
        "scope": {"mode": mode, "value": value},
        "news_items": source_items,
    }
    article_summary_guidance = (
        "article_summaries must be an array of objects with keys title and summary, where title exactly matches one supplied title and summary is 4-6 sentences. "
        "For each article summary, explain the development, the relevant institutions or geography, and why it matters for cross-border power systems or energy transition planning. "
        if profile == "pdf"
        else "article_summaries must be an array of objects with keys title and summary, where title exactly matches one supplied title and summary is 2-4 sentences. "
    )
    instructions = (
        "You are an expert energy newsletter editor. Using only the supplied news items, "
        "write a polished newsletter package for a professional cross-border electricity audience. "
        "Return JSON only with keys: title, intro, executive_summary, highlights, sections, article_summaries. "
        "intro should be 2-3 sentences. "
        "executive_summary should be a short paragraph of 4-6 sentences. "
        "highlights must be an array of 3 short bullets. "
        "sections must be an array of exactly 3 objects with keys title and body, where each body is 2-4 sentences. "
        f"{article_summary_guidance}"
        "Keep everything factual, concise, and suitable for publication. Do not invent facts beyond the supplied items."
    )
    return instructions, prompt_payload


def parse_ai_newsletter_json(raw_text: str) -> dict:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def normalize_ai_newsletter(parsed: dict, value: str, model_label: str) -> dict:
    summaries = parsed.get("article_summaries") or []
    summary_by_title = {}
    for item in summaries:
        title = item.get("title")
        summary = item.get("summary")
        if title and summary:
            summary_by_title[title] = summary

    return {
        "title": parsed.get("title") or f"{value} Energy Brief",
        "intro": parsed.get("intro") or "",
        "executive_summary": parsed.get("executive_summary") or "",
        "highlights": parsed.get("highlights") or [],
        "sections": parsed.get("sections") or [],
        "summary_by_title": summary_by_title,
        "model": model_label,
    }


def build_pdf_article_summary(item: NewsItem, ai_newsletter: dict | None) -> str:
    ai_summary = (ai_newsletter or {}).get("summary_by_title", {}).get(item.title)
    if ai_summary:
        return ai_summary

    base_summary = item.summary or "The source did not provide a detailed abstract."
    date_label = item.published_at.strftime("%d %b %Y") if item.published_at else "the selected period"
    return (
        f"{base_summary} "
        f"This item was published by {item.source} on {date_label}. "
        "It is included in this brief because it relates to grid development, regional electricity trade, "
        "transmission planning, or the wider policy and investment conditions that shape cross-border power systems."
    )


def build_local_newsletter(newsletter_data: dict, mode: str, value: str, profile: str = "web") -> dict | None:
    items = newsletter_data.get("items") or []
    if not items:
        return None

    scope_label = value or ("World" if mode == "region" else "Selected market")
    top_sources = ", ".join(sorted({item.source for item in items[:5]}))
    themes = keyword_highlights(items, limit=3)
    theme_text = ", ".join(themes).lower() if themes else "grid planning, regional power trade, and energy investment"
    item_count = min(len(items), PDF_AI_SOURCE_ITEM_LIMIT if profile == "pdf" else AI_SOURCE_ITEM_LIMIT)

    summary_by_title = {}
    for item in items[:item_count]:
        date_label = item.published_at.strftime("%d %b %Y") if item.published_at else "the selected period"
        base_summary = item.summary or "The source did not provide a detailed abstract."
        if profile == "pdf":
            summary = (
                f"{base_summary} "
                f"Published by {item.source} on {date_label}, this item is relevant to the newsletter because it touches the "
                "infrastructure, policy, investment, or reliability conditions that shape cross-border electricity systems. "
                "For readers tracking regional interconnections, the story should be read as part of the broader signal around "
                "grid readiness, power-market coordination, and the financing environment for transmission expansion."
            )
        else:
            summary = (
                f"{base_summary} "
                f"Published by {item.source} on {date_label}, it adds useful context for tracking electricity grids, "
                "regional power trade, and energy-transition planning."
            )
        summary_by_title[item.title] = summary

    return {
        "title": f"{scope_label} Electricity Interconnection Brief",
        "intro": (
            f"This issue compiles {len(items)} recent headlines for {scope_label}, with coverage drawn from {top_sources or 'available news sources'}. "
            f"The story mix points to {theme_text} as the main watch areas."
        ),
        "executive_summary": newsletter_data.get("editor_note") or build_editor_note(items, newsletter_data.get("topics", [])),
        "highlights": newsletter_data.get("takeaways") or build_takeaways(items),
        "sections": [
            {
                "title": "Grid And Transmission Signals",
                "body": "The selected headlines point to continuing attention on transmission capacity, system reliability, and the infrastructure needed to move power across regions.",
            },
            {
                "title": "Policy And Market Context",
                "body": "Coverage also reflects the policy and market conditions that influence project timelines, procurement decisions, and cross-border coordination.",
            },
            {
                "title": "Investment Relevance",
                "body": "For planners and institutions, the news set is useful as a practical scan of where financing, regulation, and grid-readiness questions are surfacing.",
            },
        ],
        "summary_by_title": summary_by_title,
        "model": "Local news brief",
    }


def generate_openai_newsletter(instructions: str, prompt_payload: dict, value: str) -> dict | None:
    if not OPENAI_API_KEY or OpenAI is None:
        return None

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=instructions,
        input=json.dumps(prompt_payload),
        text={"format": {"type": "json_object"}},
    )
    return normalize_ai_newsletter(
        json.loads(response.output_text),
        value,
        f"OpenAI {OPENAI_MODEL}",
    )


def generate_gemini_newsletter(instructions: str, prompt_payload: dict, value: str) -> dict | None:
    if not GEMINI_API_KEY:
        return None

    model_name = GEMINI_MODEL.removeprefix("models/")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{quote(model_name, safe='')}:generateContent?key={quote(GEMINI_API_KEY, safe='')}"
    )
    body = json.dumps(
        {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                f"{instructions}\n\n"
                                f"Newsletter source payload:\n{json.dumps(prompt_payload)}"
                            )
                        }
                    ],
                }
            ],
            "generationConfig": {
                "temperature": 0.35,
                "responseMimeType": "application/json",
            },
        }
    ).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=GEMINI_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))

    parts = (
        payload.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [])
    )
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        return None
    return normalize_ai_newsletter(
        parse_ai_newsletter_json(text),
        value,
        f"Gemini {GEMINI_MODEL}",
    )


def generate_ai_newsletter(newsletter_data: dict, mode: str, value: str, profile: str = "web") -> dict | None:
    if not newsletter_data["items"]:
        return None

    cache_key = ai_newsletter_cache_key(newsletter_data["items"], mode, value, profile=profile)
    cached = _AI_NEWSLETTER_CACHE.get(cache_key)
    if cached and (time.time() - cached["timestamp"]) < AI_CACHE_TTL_SECONDS:
        return cached["data"]

    instructions, prompt_payload = build_ai_newsletter_request(newsletter_data, mode, value, profile=profile)

    try:
        data = generate_openai_newsletter(instructions, prompt_payload, value)
    except Exception:
        data = None

    if not data:
        try:
            data = generate_gemini_newsletter(instructions, prompt_payload, value)
        except Exception:
            data = None

    if not data:
        data = build_local_newsletter(newsletter_data, mode, value, profile=profile)

    if data:
        _AI_NEWSLETTER_CACHE[cache_key] = {"timestamp": time.time(), "data": data}
    return data


def get_ai_newsletter_result(newsletter_data: dict, mode: str, value: str) -> tuple[dict | None, str | None]:
    if not newsletter_data["items"]:
        return None, "No news items were available to generate an AI newsletter."
    if (not OPENAI_API_KEY or OpenAI is None) and not GEMINI_API_KEY:
        return None, "Add an OpenAI or Gemini API key to enable AI-written newsletter content."

    try:
        data = generate_ai_newsletter(newsletter_data, mode, value)
    except Exception:  # pragma: no cover
        data = None

    if data:
        return data, None

    if GEMINI_API_KEY and (not OPENAI_API_KEY or OpenAI is None):
        return None, "Gemini newsletter generation could not be reached right now. Please check GEMINI_API_KEY and GEMINI_MODEL."

    if OPENAI_API_KEY and OpenAI is not None:
        try:
            client = OpenAI(api_key=OPENAI_API_KEY)
            client.responses.create(
                model=OPENAI_MODEL,
                input="Reply with JSON: {\"ok\": true}",
                text={"format": {"type": "json_object"}},
            )
        except Exception as exc:
            error_text = str(exc)
            if ("insufficient_quota" in error_text or "current quota" in error_text) and not GEMINI_API_KEY:
                return None, "OpenAI has no available quota, and GEMINI_API_KEY is not configured in .env."
            if ("insufficient_quota" in error_text or "current quota" in error_text) and GEMINI_API_KEY:
                return None, "OpenAI has no available quota, and the Gemini fallback could not generate this issue. Please check GEMINI_API_KEY and GEMINI_MODEL."
            if "insufficient_quota" in error_text or "current quota" in error_text:
                return None, "AI newsletter generation is temporarily unavailable because the configured OpenAI account has no available quota."
            if "429" in error_text and not GEMINI_API_KEY:
                return None, "OpenAI is rate limited, and GEMINI_API_KEY is not configured in .env."
            if "429" in error_text and GEMINI_API_KEY:
                return None, "OpenAI is rate limited, and the Gemini fallback could not generate this issue. Please try again in a little while."
            if "429" in error_text:
                return None, "AI newsletter generation is temporarily rate limited. Please try again in a little while."
            if ("401" in error_text or "invalid_api_key" in error_text) and GEMINI_API_KEY:
                return None, "The OpenAI API key was rejected, and the Gemini fallback could not generate this issue. Please check the configured API keys."
            if "401" in error_text or "invalid_api_key" in error_text:
                return None, "The OpenAI API key was rejected. Please check the key and project settings."
            if GEMINI_API_KEY:
                return None, "OpenAI could not be reached, and the Gemini fallback could not generate this issue right now."
            return None, "The AI newsletter service could not be reached right now."

    if GEMINI_API_KEY:
        return None, "Gemini newsletter generation could not be reached right now. Please check GEMINI_API_KEY and GEMINI_MODEL."

    return None, "The AI newsletter service did not return content for this issue."


def filter_news_by_range(
    items: list[NewsItem],
    from_month: int | None = None,
    from_year: int | None = None,
    to_month: int | None = None,
    to_year: int | None = None,
) -> list[NewsItem]:
    if None in {from_month, from_year, to_month, to_year}:
        return items
    start_key = (from_year, from_month)
    end_key = (to_year, to_month)
    if start_key > end_key:
        start_key, end_key = end_key, start_key
    filtered = []
    for item in items:
        if not item.published_at:
            continue
        item_key = (item.published_at.year, item.published_at.month)
        if start_key <= item_key <= end_key:
            filtered.append(item)
    return filtered


def generate_newsletter(
    topics: list[str],
    per_topic_limit: int = 6,
    from_month: int | None = None,
    from_year: int | None = None,
    to_month: int | None = None,
    to_year: int | None = None,
) -> dict:
    cached_rows = load_recent_news(topics, max_age_hours=24, limit=per_topic_limit * max(1, len(topics)) * 3)
    if cached_rows:
        cached_items = [
            NewsItem(
                topic=row["topic"],
                title=row["title"],
                source=row["publisher"] or "Unknown source",
                link=row["link"],
                published_at=parse_time(row["published_at"]),
                summary=row["summary"] or "",
            )
            for row in cached_rows
        ]
        cached_items = filter_news_by_range(
            dedupe_news(cached_items),
            from_month=from_month,
            from_year=from_year,
            to_month=to_month,
            to_year=to_year,
        )
        if cached_items:
            return {
                "generated_at": datetime.now().strftime("%B %d, %Y at %I:%M %p"),
                "topics": topics,
                "items": cached_items,
                "editor_note": build_editor_note(cached_items, topics),
                "takeaways": build_takeaways(cached_items),
                "top_story": cached_items[0] if cached_items else None,
                "errors": [],
                "source_mode": "Database cache",
            }

    all_items: list[NewsItem] = []
    errors: list[str] = []

    for topic in topics:
        topic_items: list[NewsItem] = []
        provider_failed = False

        for fetcher in (fetch_topic_news, fetch_newsapi_topic_news, fetch_gnews_topic_news):
            try:
                topic_items.extend(fetcher(topic, per_topic_limit))
            except (HTTPError, URLError, TimeoutError, ElementTree.ParseError, json.JSONDecodeError):
                provider_failed = True

        if not topic_items and provider_failed:
            errors.append(f"Could not load live headlines for {topic}.")

        all_items.extend(topic_items)

    all_items = dedupe_news(all_items)
    all_items = filter_news_by_range(
        all_items,
        from_month=from_month,
        from_year=from_year,
        to_month=to_month,
        to_year=to_year,
    )
    save_news_items(all_items)

    return {
        "generated_at": datetime.now().strftime("%B %d, %Y at %I:%M %p"),
        "topics": topics,
        "items": all_items,
        "editor_note": build_editor_note(all_items, topics),
        "takeaways": build_takeaways(all_items),
        "top_story": all_items[0] if all_items else None,
        "errors": errors,
        "source_mode": "Live API/RSS fetch saved to database",
    }


def summarize_item(title: str, summary: str, source: str, link: str) -> str:
    resolved_link = resolve_article_url(link, title, source)

    try:
        article_text = extract_article_paragraph(resolved_link)
        if article_text:
            return article_text
    except (HTTPError, URLError, TimeoutError, ValueError):
        pass

    try:
        article_text = extract_first_paragraph(fetch_text(resolved_link))
        if article_text:
            return article_text
    except (HTTPError, URLError, TimeoutError, ValueError):
        pass

    cleaned_title = clean_text(title)
    cleaned_summary = clean_text(summary)
    if cleaned_summary:
        return cleaned_summary

    if cleaned_title:
        return cleaned_title

    return "A short summary is not available for this item yet."


def normalize_filter(mode: str | None, value: str | None) -> tuple[str, str]:
    if mode == "country":
        selected_country = value if value in COUNTRIES else DEFAULT_COUNTRY
        return "country", selected_country
    selected_region = value if value in REGIONS else DEFAULT_REGION
    return "region", selected_region


def apply_geo_filter(topic: str, mode: str, value: str) -> str:
    if mode == "region" and value == "World":
        return topic
    return f"{topic} {value}"


def country_name_to_iso3_map() -> dict[str, str]:
    return {
        item["name"]: item["code"]
        for item in get_filter_options()["countries"]
    }


def selected_country_names(mode: str, value: str) -> list[str]:
    if mode == "country":
        return [value] if value else []
    return REGION_COUNTRIES.get(value, COUNTRIES)


def selected_country_codes(mode: str, value: str) -> set[str]:
    name_to_code = country_name_to_iso3_map()
    names = selected_country_names(mode, value)
    return {name_to_code[name] for name in names if name in name_to_code}


def newsletter_year_options() -> list[int]:
    current_year = datetime.now().year
    return list(range(current_year, current_year - 5, -1))


def newsletter_period_options() -> list[dict]:
    options: list[dict] = []
    for year in newsletter_year_options():
        for month in range(12, 0, -1):
            options.append(
                {
                    "value": f"{year}-{month:02d}",
                    "label": f"{month_name[month]} {year}",
                }
            )
    return options


def normalize_period_value(period_value: str | None) -> tuple[int, int]:
    raw_value = (period_value or f"{DEFAULT_PDF_YEAR}-{DEFAULT_PDF_MONTH:02d}").strip()
    match = re.fullmatch(r"(\d{4})-(\d{2})", raw_value)
    if not match:
        return DEFAULT_PDF_MONTH, DEFAULT_PDF_YEAR
    year = int(match.group(1))
    month = int(match.group(2))
    if month < 1 or month > 12:
        month = DEFAULT_PDF_MONTH
    valid_years = set(newsletter_year_options())
    if year not in valid_years:
        year = DEFAULT_PDF_YEAR
    return month, year


def normalize_month_year_range(
    from_period_value: str | None,
    to_period_value: str | None,
) -> tuple[int, int, int, int]:
    from_month, from_year = normalize_period_value(from_period_value)
    to_month, to_year = normalize_period_value(to_period_value)
    if (from_year, from_month) > (to_year, to_month):
        from_month, to_month = to_month, from_month
        from_year, to_year = to_year, from_year
    return from_month, from_year, to_month, to_year


def build_pdf_news_queries(mode: str, value: str) -> list[str]:
    queries = []
    suffix = "" if mode == "region" and value == "World" else f" {value}"
    for query in PDF_ARCHIVE_QUERY_VARIANTS:
        queries.append(f"{query}{suffix}".strip())
    return queries


def draw_newsletter_first_page_frame(canvas, doc) -> None:
    canvas.saveState()
    width, height = A4

    canvas.setFillColor(colors.HexColor("#fff7ed"))
    canvas.rect(0, 0, width, height, stroke=0, fill=1)

    canvas.setStrokeColor(colors.HexColor("#d97706"))
    canvas.setLineWidth(1)
    canvas.line(doc.leftMargin, 14 * mm, width - doc.rightMargin, 14 * mm)

    canvas.setFont("Helvetica", 8.5)
    canvas.setFillColor(colors.HexColor("#6b7280"))
    canvas.drawCentredString(width / 2, 9 * mm, "Global Cross-Border Electricity Interconnections Newsletter")
    canvas.drawRightString(width - doc.rightMargin, 9 * mm, f"Page {canvas.getPageNumber()}")
    canvas.restoreState()


def draw_newsletter_later_page_frame(canvas, doc) -> None:
    canvas.saveState()
    width, height = A4

    canvas.setFillColor(colors.HexColor("#fff7ed"))
    canvas.rect(0, 0, width, height, stroke=0, fill=1)

    canvas.setFillColor(colors.HexColor("#0f766e"))
    canvas.rect(0, height - 22 * mm, width, 22 * mm, stroke=0, fill=1)

    if os.path.exists(PDF_LOGO_PATH):
        try:
            logo_size = 12 * mm
            logo_x = doc.leftMargin
            logo_y = height - 19 * mm
            logo_cx = logo_x + logo_size / 2
            logo_cy = logo_y + logo_size / 2

            canvas.saveState()
            path = canvas.beginPath()
            path.circle(logo_cx, logo_cy, logo_size / 2)
            canvas.clipPath(path, stroke=0, fill=0)
            canvas.drawImage(
                PDF_LOGO_PATH,
                logo_x,
                logo_y,
                width=logo_size,
                height=logo_size,
                preserveAspectRatio=True,
                mask="auto",
            )
            canvas.restoreState()
        except Exception:
            pass

    canvas.setStrokeColor(colors.HexColor("#d97706"))
    canvas.setLineWidth(2)
    canvas.line(doc.leftMargin, height - 22 * mm, width - doc.rightMargin, height - 22 * mm)

    canvas.setFont("Helvetica-Bold", 10)
    canvas.setFillColor(colors.white)
    canvas.drawString(
        doc.leftMargin + 15 * mm,
        height - 13 * mm,
        "GLOBAL CROSS-BORDER ELECTRICITY INTERCONNECTIONS DASHBOARD",
    )

    canvas.setFont("Helvetica", 8.5)
    canvas.setFillColor(colors.HexColor("#e2e8f0"))
    canvas.drawString(doc.leftMargin + 15 * mm, height - 18 * mm, "ISA Collaborative Project")

    canvas.setStrokeColor(colors.HexColor("#d97706"))
    canvas.setLineWidth(1)
    canvas.line(doc.leftMargin, 14 * mm, width - doc.rightMargin, 14 * mm)

    canvas.setFont("Helvetica", 8.5)
    canvas.setFillColor(colors.HexColor("#6b7280"))
    canvas.drawCentredString(width / 2, 9 * mm, "Global Cross-Border Electricity Interconnections Newsletter")
    canvas.drawRightString(width - doc.rightMargin, 9 * mm, f"Page {canvas.getPageNumber()}")
    canvas.restoreState()


def build_newsletter_pdf(
    topics: list[str],
    mode: str,
    value: str,
    from_month: int,
    from_year: int,
    to_month: int,
    to_year: int,
) -> BytesIO:
    newsletter_data = generate_archive_newsletter(
        topics,
        from_month=from_month,
        from_year=from_year,
        to_month=to_month,
        to_year=to_year,
        per_topic_limit=25,
    )
    ai_newsletter = generate_ai_newsletter(newsletter_data, mode, value, profile="pdf")
    buffer = BytesIO()
    doc = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=36,
        rightMargin=36,
        topMargin=12 * mm,
        bottomMargin=22 * mm,
    )
    page_width, page_height = A4
    first_frame = Frame(
        doc.leftMargin,
        doc.bottomMargin,
        doc.width,
        page_height - doc.bottomMargin - (12 * mm),
        id="first-page-body",
        showBoundary=0,
    )
    later_frame = Frame(
        doc.leftMargin,
        doc.bottomMargin,
        doc.width,
        page_height - doc.bottomMargin - (31 * mm),
        id="later-page-body",
        showBoundary=0,
    )
    doc.addPageTemplates(
        [
            PageTemplate(
                id="First",
                frames=[first_frame],
                onPage=draw_newsletter_first_page_frame,
                autoNextPageTemplate="Later",
            ),
            PageTemplate(
                id="Later",
                frames=[later_frame],
                onPage=draw_newsletter_later_page_frame,
            ),
        ]
    )
    styles = getSampleStyleSheet()
    cover_title = ParagraphStyle(
        "CoverTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=21,
        leading=27,
        textColor=colors.black,
        alignment=TA_CENTER,
        spaceAfter=8,
    )
    cover_subtitle = ParagraphStyle(
        "CoverSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=18,
        textColor=colors.black,
        alignment=TA_CENTER,
        spaceAfter=6,
    )
    cover_meta = ParagraphStyle(
        "CoverMeta",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10.5,
        leading=14,
        textColor=colors.HexColor("#374151"),
        alignment=TA_CENTER,
        spaceAfter=6,
    )
    section_title = ParagraphStyle(
        "SectionTitle",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=18,
        textColor=colors.HexColor("#b45309"),
        spaceAfter=6,
    )
    article_title = ParagraphStyle(
        "ArticleTitle",
        parent=styles["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=16,
        textColor=colors.HexColor("#111827"),
        spaceAfter=4,
    )
    meta_style = ParagraphStyle(
        "MetaStyle",
        parent=styles["Italic"],
        fontName="Helvetica-Oblique",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#6b7280"),
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "BodyStyle",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#1f2937"),
        spaceAfter=6,
    )
    note_style = ParagraphStyle(
        "NoteStyle",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=13,
        textColor=colors.HexColor("#374151"),
        leftIndent=8,
        rightIndent=8,
        spaceAfter=8,
    )
    issue_label = f"{month_name[from_month]} {from_year} to {month_name[to_month]} {to_year}"
    scope_label = f"{mode.title()} - {value}"
    fixed_cover_heading = "GLOBAL CROSS-BORDER ELECTRICITY<br/>INTERCONNECTIONS DASHBOARD"
    cover_title_stack = [
        Paragraph(fixed_cover_heading, cover_title),
        Paragraph("IIT Delhi - ISA Collaborative Project", cover_subtitle),
    ]
    if os.path.exists(PDF_LOGO_PATH):
        try:
            cover_image = CircularImage(PDF_LOGO_PATH, width=40 * mm, height=40 * mm)
            cover_table_data = [[cover_image, cover_title_stack]]
        except Exception:
            cover_table_data = [[cover_title_stack]]
    else:
        cover_table_data = [[cover_title_stack]]

    cover_block = Table(
        cover_table_data,
        colWidths=[42 * mm, doc.width - 42 * mm] if len(cover_table_data[0]) == 2 else [doc.width],
    )
    cover_block.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (0, 0), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    cover_heading = ai_newsletter["title"] if ai_newsletter and ai_newsletter.get("title") else "GLOBAL CROSS-BORDER ELECTRICITY INTERCONNECTIONS NEWSLETTER"
    story = [
        Spacer(1, 0),
        cover_block,
        Spacer(1, 14),
        HRFlowable(width="100%", color=colors.HexColor("#d97706"), thickness=1.3, spaceBefore=2, spaceAfter=10),
        Paragraph(cover_heading, section_title),
        Paragraph(issue_label, cover_meta),
        Paragraph(f"<b>Scope:</b> {scope_label}", body_style),
        Paragraph(
            "<b>Keywords:</b> Grid, Power, Cross border interconnection, Crossborder interconnection, "
            "Cross border transmission, Crossborder transmission, Regional Grid Interconnection, "
            "Interconnection, Electricity",
            body_style,
        ),
        Spacer(1, 8),
        Paragraph("Contents", section_title),
        Paragraph("1. Issue Overview", body_style),
        Paragraph("2. Key Themes", body_style),
        Paragraph("3. Related News Briefs", body_style),
        Spacer(1, 10),
        Paragraph("Issue Overview", section_title),
        Paragraph(
            (
                ai_newsletter["intro"]
                if ai_newsletter and ai_newsletter.get("intro")
                else f"This issue compiles current news related to electricity grids, cross-border interconnections, "
                f"and regional transmission developments for <b>{scope_label}</b> during <b>{issue_label}</b>."
            ),
            note_style,
        ),
    ]
    highlights = ai_newsletter["highlights"] if ai_newsletter and ai_newsletter.get("highlights") else newsletter_data["takeaways"]
    if highlights:
        story.append(Paragraph("Key Themes", section_title))
        for takeaway in highlights:
            story.append(Paragraph(f"• {takeaway}", note_style))
        story.append(Spacer(1, 6))
    if newsletter_data["errors"]:
        story.append(Paragraph("Source Notes", section_title))
        for error in newsletter_data["errors"]:
            story.append(Paragraph(f"• {error}", note_style))
        story.append(Spacer(1, 6))
    story.append(Paragraph("Related News Briefs", section_title))
    if newsletter_data["items"]:
        for index, item in enumerate(newsletter_data["items"], start=1):
            date_label = item.published_at.strftime("%d %b %Y") if item.published_at else "Unknown date"
            link = item.link or ""
            summary = build_pdf_article_summary(item, ai_newsletter)
            article_content = [
                Paragraph(f"{index}. {item.title}", article_title),
                Paragraph(f"{item.source} | {date_label}", meta_style),
                Paragraph(summary, body_style),
                Paragraph(f"<font color='#b45309'>Read more:</font> {link}", body_style),
            ]
            article_block = Table(
                [[article_content]],
                colWidths=[doc.width],
            )
            article_block.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#fdba74")),
                        ("LINEBEFORE", (0, 0), (0, -1), 5, colors.HexColor("#0f766e")),
                        ("LEFTPADDING", (0, 0), (-1, -1), 12),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                        ("TOPPADDING", (0, 0), (-1, -1), 10),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                    ]
                )
            )
            story.extend(
                [
                    article_block,
                    Spacer(1, 6),
                    HRFlowable(width="100%", color=colors.HexColor("#e5e7eb"), thickness=0.8, spaceBefore=2, spaceAfter=10),
                ]
            )
    else:
        story.append(Paragraph("No matching news items were found for the selected month, year, and geography.", body_style))
    doc.build(story)
    buffer.seek(0)
    return buffer


def build_report_context(mode: str, value: str) -> dict:
    filtered_topics = [apply_geo_filter(topic, mode, value) for topic in DEFAULT_TOPICS]
    newsletter_data = generate_newsletter(filtered_topics)
    policy_countries = selected_country_names(mode, value)
    asset_country_codes = selected_country_codes(mode, value)
    asset_report = build_report_asset_context(asset_country_codes)
    policy_report = build_policy_report(policy_countries, limit=8)
    live_country_code = next(iter(asset_country_codes), "") if mode == "country" else ""
    live_market = build_live_market_context(
        country=live_country_code,
        region=value if mode == "region" else "",
        preferred_countries=top_country_codes_by_capacity(asset_report["rows"], limit=6),
        rows=asset_report["rows"],
    )
    if mode == "country":
        metric_country_codes = list(asset_country_codes)[:1]
    else:
        metric_country_codes = top_country_codes_by_capacity(asset_report["rows"], limit=8)
    report_metrics = build_report_metric_rows(metric_country_codes, asset_report["rows"], limit=8)

    top_headlines = newsletter_data["items"][:8]
    location_label = value if value else ("World" if mode == "region" else "India")

    return {
        "generated_at": datetime.now().strftime("%B %d, %Y at %I:%M %p"),
        "location_mode": mode,
        "location_value": location_label,
        "news": {
            "items": top_headlines,
            "count": len(newsletter_data["items"]),
            "errors": newsletter_data["errors"],
            "takeaways": newsletter_data["takeaways"],
        },
        "assets": asset_report,
        "policies": policy_report,
        "live_market": live_market,
        "country_metrics": report_metrics,
        "overview": {
            "headline_count": len(newsletter_data["items"]),
            "policy_count": policy_report["count"],
            "interconnections": asset_report["kpis"]["interconnections"],
            "known_capacity_mw": asset_report["kpis"]["known_capacity_mw"],
        },
    }


RENEWABLE_EXPORT_COUNTRIES = {
    "BTN": "hydropower",
    "NPL": "hydropower",
    "LAO": "hydropower",
    "ETH": "hydropower",
    "NOR": "hydropower",
    "ISL": "hydropower/geothermal",
    "KEN": "geothermal/wind",
    "MAR": "solar/wind",
    "CHL": "solar/wind",
}


def _severity_for_utilization(utilization: float) -> str:
    if utilization >= 95:
        return "Critical"
    if utilization >= 80:
        return "Congested"
    if utilization >= 55:
        return "Watch"
    return "Open"


def _analytics_corridors(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = tuple(sorted((row["country_a"], row["country_b"])))
        item = grouped.setdefault(
            key,
            {
                "country_a": key[0],
                "country_b": key[1],
                "connections": 0,
                "operational": 0,
                "planned": 0,
                "capacity_mw": 0.0,
                "known_capacity_assets": 0,
                "length_km": 0.0,
                "known_length_assets": 0,
                "trade_flow_mwh": 0.0,
                "power_pool_counts": Counter(),
                "ac_dc_counts": Counter(),
                "voltage_classes": Counter(),
                "carbon_values": [],
            },
        )
        item["connections"] += 1
        item["operational"] += 1 if row["status"] == "Operational" else 0
        item["planned"] += 1 if row["status"] == "Planned" else 0
        if row["capacity_mw"] is not None:
            item["capacity_mw"] += row["capacity_mw"]
            item["known_capacity_assets"] += 1
        if row["length_km"] is not None:
            item["length_km"] += row["length_km"]
            item["known_length_assets"] += 1
        if row["trade_flow_mwh"] is not None:
            item["trade_flow_mwh"] += row["trade_flow_mwh"]
        if row.get("power_pool"):
            item["power_pool_counts"][row["power_pool"]] += 1
        if row.get("ac_dc"):
            item["ac_dc_counts"][row["ac_dc"]] += 1
        if row.get("voltage_class"):
            item["voltage_classes"][row["voltage_class"]] += 1
        carbon = row.get("carbon_intensity")
        if carbon is not None:
            item["carbon_values"].append(carbon)

    corridors: list[dict] = []
    for item in grouped.values():
        capacity = item["capacity_mw"]
        avg_length = item["length_km"] / item["known_length_assets"] if item["known_length_assets"] else 0
        flow_from_trade = item["trade_flow_mwh"] / 8760 if item["trade_flow_mwh"] else None
        utilization_proxy = min(
            96,
            34
            + min(capacity / 220, 34)
            + min(item["connections"] * 3.5, 18)
            + (8 if item["operational"] >= item["planned"] else -6),
        )
        actual_flow_mw = flow_from_trade if flow_from_trade is not None else capacity * utilization_proxy / 100
        utilization = (actual_flow_mw / capacity * 100) if capacity else 0
        ntc_mw = capacity * 0.9 if capacity else 0
        atc_mw = max(ntc_mw - actual_flow_mw, 0)
        hours_above_80 = int(max(0, utilization - 55) * 8.8)
        hours_above_95 = int(max(0, utilization - 88) * 4.6)
        avg_carbon = sum(item["carbon_values"]) / len(item["carbon_values"]) if item["carbon_values"] else 0
        corridors.append(
            {
                "label": f"{country_name(item['country_a'])} ↔ {country_name(item['country_b'])}",
                "country_a": country_name(item["country_a"]),
                "country_b": country_name(item["country_b"]),
                "country_a_code": item["country_a"],
                "country_b_code": item["country_b"],
                "connections": item["connections"],
                "operational": item["operational"],
                "planned": item["planned"],
                "capacity_mw": round(capacity, 1),
                "ntc_mw": round(ntc_mw, 1),
                "atc_mw": round(atc_mw, 1),
                "actual_flow_mw": round(actual_flow_mw, 1),
                "utilization_pct": round(utilization, 1),
                "severity": _severity_for_utilization(utilization),
                "hours_above_80": hours_above_80,
                "hours_above_95": hours_above_95,
                "congestion_index": round((hours_above_80 * 0.35) + (hours_above_95 * 1.2) + utilization, 1),
                "average_length_km": round(avg_length, 1),
                "dominant_pool": item["power_pool_counts"].most_common(1)[0][0] if item["power_pool_counts"] else "Unassigned",
                "technology": item["ac_dc_counts"].most_common(1)[0][0] if item["ac_dc_counts"] else "Unknown",
                "voltage_class": item["voltage_classes"].most_common(1)[0][0] if item["voltage_classes"] else "Unknown",
                "carbon_intensity": round(avg_carbon, 1),
                "flow_source": "Observed trade-flow field" if flow_from_trade is not None else "Planning proxy until live flow is connected",
            }
        )
    corridors.sort(key=lambda row: (row["capacity_mw"], row["connections"]), reverse=True)
    return corridors


def build_analytics_context() -> dict:
    rows = load_interconnections()
    corridors = _analytics_corridors(rows)
    operational_corridors = [item for item in corridors if item["operational"] > 0 and item["capacity_mw"] > 0]
    total_capacity = sum(item["capacity_mw"] for item in corridors)
    total_atc = sum(item["atc_mw"] for item in operational_corridors)
    weighted_utilization = (
        sum(item["utilization_pct"] * item["capacity_mw"] for item in operational_corridors)
        / max(1, sum(item["capacity_mw"] for item in operational_corridors))
    )

    country_capacity: defaultdict[str, float] = defaultdict(float)
    country_connections: Counter[str] = Counter()
    country_congestion: defaultdict[str, list[float]] = defaultdict(list)
    matrix_pairs = []
    for corridor in corridors:
        for code in (corridor["country_a_code"], corridor["country_b_code"]):
            country_capacity[code] += corridor["capacity_mw"]
            country_connections[code] += corridor["connections"]
            if corridor["utilization_pct"]:
                country_congestion[code].append(corridor["utilization_pct"])
        matrix_pairs.append(
            {
                "from": corridor["country_a"],
                "to": corridor["country_b"],
                "capacity_mw": corridor["capacity_mw"],
                "utilization_pct": corridor["utilization_pct"],
            }
        )

    country_rankings = []
    for code, capacity in sorted(country_capacity.items(), key=lambda entry: entry[1], reverse=True)[:10]:
        avg_stress = sum(country_congestion[code]) / max(1, len(country_congestion[code]))
        country_rankings.append(
            {
                "country": country_name(code),
                "connections": country_connections[code],
                "connected_capacity_mw": round(capacity, 1),
                "dependency_risk": round(min(100, avg_stress * 0.72 + country_connections[code] * 1.4), 1),
            }
        )

    pool_totals: defaultdict[str, dict] = defaultdict(lambda: {"capacity_mw": 0.0, "corridors": 0, "stress": []})
    for corridor in corridors:
        pool = corridor["dominant_pool"] or "Unassigned"
        pool_totals[pool]["capacity_mw"] += corridor["capacity_mw"]
        pool_totals[pool]["corridors"] += 1
        pool_totals[pool]["stress"].append(corridor["utilization_pct"])
    regional_comparison = []
    for pool, item in sorted(pool_totals.items(), key=lambda entry: entry[1]["capacity_mw"], reverse=True)[:8]:
        regional_comparison.append(
            {
                "region": pool,
                "capacity_mw": round(item["capacity_mw"], 1),
                "corridors": item["corridors"],
                "average_utilization_pct": round(sum(item["stress"]) / max(1, len(item["stress"])), 1),
            }
        )

    renewable_corridors = []
    for corridor in corridors:
        resource = None
        if corridor["country_a_code"] in RENEWABLE_EXPORT_COUNTRIES:
            resource = RENEWABLE_EXPORT_COUNTRIES[corridor["country_a_code"]]
        if corridor["country_b_code"] in RENEWABLE_EXPORT_COUNTRIES:
            resource = RENEWABLE_EXPORT_COUNTRIES[corridor["country_b_code"]]
        if resource and corridor["capacity_mw"] > 0:
            annual_clean_mwh = corridor["capacity_mw"] * 0.38 * 8760
            renewable_corridors.append(
                {
                    "label": corridor["label"],
                    "resource": resource,
                    "capacity_mw": corridor["capacity_mw"],
                    "clean_energy_gwh": round(annual_clean_mwh / 1000, 1),
                    "co2_avoided_mt": round(annual_clean_mwh * 0.55 / 1_000_000, 2),
                }
            )
    renewable_corridors.sort(key=lambda item: item["clean_energy_gwh"], reverse=True)

    planned_capacity = sum(item["capacity_mw"] for item in corridors if item["planned"])
    forecasting = []
    for offset, year in enumerate(range(2026, 2031)):
        growth = 1 + (offset * 0.055)
        added_capacity = planned_capacity * min(1, offset / 4)
        expected_loading = min(98, weighted_utilization * growth - (added_capacity / max(1, total_capacity) * 18))
        forecasting.append(
            {
                "year": year,
                "expected_loading_pct": round(expected_loading, 1),
                "expected_congestion_hours": int(max(0, expected_loading - 68) * 22),
                "available_capacity_mw": round(total_capacity + added_capacity, 1),
            }
        )

    seasonal_profile = [
        {"period": "Winter evening", "loading_pct": round(min(98, weighted_utilization + 11), 1), "driver": "Peak demand and heating load"},
        {"period": "Spring shoulder", "loading_pct": round(max(20, weighted_utilization - 14), 1), "driver": "Lower demand and maintenance windows"},
        {"period": "Summer solar ramp", "loading_pct": round(min(98, weighted_utilization + 7), 1), "driver": "Solar variability and cooling demand"},
        {"period": "Monsoon/hydro season", "loading_pct": round(min(98, weighted_utilization + 16), 1), "driver": "Hydropower export and import balancing"},
    ]

    economic = {
        "capacity_value_usd_m": round(total_capacity * 0.07, 1),
        "congestion_rent_proxy_usd_m": round(sum(item["congestion_index"] * item["capacity_mw"] for item in corridors[:40]) / 1000, 1),
        "avoided_generation_cost_usd_m": round(sum(item["atc_mw"] for item in operational_corridors) * 0.052 * 8760 / 1000, 1),
        "market_coupling_readiness_pct": round(min(100, len([item for item in corridors if item["capacity_mw"] > 0]) / max(1, len(corridors)) * 100), 1),
    }

    return {
        "generated_at": datetime.now().strftime("%d %b %Y, %H:%M"),
        "kpis": {
            "corridors": len(corridors),
            "interconnections": len(rows),
            "countries": len(country_capacity),
            "known_capacity_mw": round(total_capacity, 1),
            "weighted_utilization_pct": round(weighted_utilization, 1),
            "available_transfer_capacity_mw": round(total_atc, 1),
            "planned_capacity_mw": round(planned_capacity, 1),
            "renewable_exchange_gwh": round(sum(item["clean_energy_gwh"] for item in renewable_corridors), 1),
        },
        "corridors": corridors,
        "utilization_heatmap": sorted(operational_corridors, key=lambda item: item["utilization_pct"], reverse=True)[:12],
        "underutilized": sorted(operational_corridors, key=lambda item: item["utilization_pct"])[:6],
        "congested": sorted(operational_corridors, key=lambda item: item["congestion_index"], reverse=True)[:8],
        "exchange_matrix": sorted(matrix_pairs, key=lambda item: item["capacity_mw"], reverse=True)[:12],
        "country_rankings": country_rankings,
        "regional_comparison": regional_comparison,
        "renewable_corridors": renewable_corridors[:8],
        "economic": economic,
        "seasonal_profile": seasonal_profile,
        "forecasting": forecasting,
        "spatial": {
            "longest_corridors": sorted(corridors, key=lambda item: item["average_length_km"], reverse=True)[:6],
            "hvdc_corridors": [item for item in corridors if item["technology"] == "DC"][:6],
            "high_density_regions": regional_comparison[:4],
        },
        "environmental": {
            "co2_avoided_mt": round(sum(item["co2_avoided_mt"] for item in renewable_corridors), 2),
            "clean_energy_gwh": round(sum(item["clean_energy_gwh"] for item in renewable_corridors), 1),
            "renewable_corridor_count": len(renewable_corridors),
            "grid_emission_factor": 0.55,
        },
    }


@app.route("/newsletter", methods=["GET"])
def newsletter():
    selected_mode, selected_value = normalize_filter(
        request.args.get("filter_mode", DEFAULT_FILTER_MODE),
        request.args.get("location"),
    )
    filtered_topics = [apply_geo_filter(topic, selected_mode, selected_value) for topic in DEFAULT_TOPICS]
    newsletter = generate_newsletter(filtered_topics)
    ai_newsletter, ai_newsletter_notice = get_ai_newsletter_result(newsletter, selected_mode, selected_value)
    return render_template(
        "newsletter.html",
        newsletter=newsletter,
        ai_newsletter=ai_newsletter,
        ai_newsletter_notice=ai_newsletter_notice,
        countries=COUNTRIES,
        regions=REGIONS,
        selected_mode=selected_mode,
        selected_value=selected_value,
        pdf_periods=newsletter_period_options(),
        selected_pdf_from_period=f"{DEFAULT_PDF_YEAR}-{DEFAULT_PDF_MONTH:02d}",
        selected_pdf_to_period=f"{DEFAULT_PDF_YEAR}-{DEFAULT_PDF_MONTH:02d}",
    )


@app.route("/newsletter/pdf", methods=["POST"])
def newsletter_pdf():
    selected_mode, selected_value = normalize_filter(
        request.form.get("filter_mode", DEFAULT_FILTER_MODE),
        request.form.get("location"),
    )
    selected_from_month, selected_from_year, selected_to_month, selected_to_year = normalize_month_year_range(
        request.form.get("from_period"),
        request.form.get("to_period"),
    )
    queries = build_pdf_news_queries(selected_mode, selected_value)
    pdf_buffer = build_newsletter_pdf(
        queries,
        selected_mode,
        selected_value,
        selected_from_month,
        selected_from_year,
        selected_to_month,
        selected_to_year,
    )
    safe_location = re.sub(r"[^a-z0-9]+", "-", selected_value.lower()).strip("-") or "world"
    filename = (
        f"newsletter-{safe_location}-"
        f"{selected_from_year}-{selected_from_month:02d}-to-{selected_to_year}-{selected_to_month:02d}.pdf"
    )
    return send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/analytics", methods=["GET"])
def analytics():
    return render_template("analytics.html", analytics=build_analytics_context())


@app.route("/report", methods=["GET"])
def report():
    return redirect(url_for("analytics"), code=301)


@app.route("/about", methods=["GET"])
def about():
    return render_template("about.html")


@app.route("/summarize", methods=["POST"])
def summarize():
    payload = request.get_json(silent=True) or {}
    title = payload.get("title", "")
    summary = payload.get("summary", "")
    source = payload.get("source", "Unknown source")
    link = payload.get("link", "")
    return jsonify({"summary": summarize_item(title, summary, source, link)})


@app.route("/data/status", methods=["GET"])
def data_status_endpoint():
    return jsonify(data_status())


@app.route("/policies", methods=["GET"])
def policies():
    selected_country = request.args.get("country", "").strip()
    selected_topic = request.args.get("topic", "").strip()
    selected_status = request.args.get("status", "").strip()
    selected_jurisdiction = request.args.get("jurisdiction", "").strip()
    selected_query = request.args.get("query", "").strip()
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1

    policies_data = fetch_iea_policies(
        country=selected_country,
        topic=selected_topic,
        status=selected_status,
        jurisdiction=selected_jurisdiction,
        query=selected_query,
        page=page,
    )
    return render_template("policies.html", policies=policies_data)


@app.route("/", methods=["GET"])
@app.route("/dashboard/infrastructure", methods=["GET"])
def dashboard_infrastructure():
    options = get_filter_options()
    selected_country = request.args.get("country", "").strip()
    selected_status = request.args.get("status", "").strip()
    selected_power_pool = request.args.get("power_pool", "").strip()
    selected_query = request.args.get("query", "").strip()
    selected_map_mode = request.args.get("map_mode", "separate").strip()
    if selected_map_mode not in {"separate", "consolidated"}:
        selected_map_mode = "separate"
    context = build_infrastructure_context(
        country=selected_country,
        status=selected_status,
        power_pool=selected_power_pool,
        query=selected_query,
        map_mode=selected_map_mode,
    )
    return render_template(
        "home.html",
        GOOGLE_MAPS_API_KEY=os.getenv("GOOGLE_MAPS_API_KEY", "AIzaSyCpaTjRt7vDWU73BHtOde_pY4E8_1dXtUY"),
        dashboard=context,
        filters=options,
        selected_country=selected_country,
        selected_status=selected_status,
        selected_power_pool=selected_power_pool,
        selected_query=selected_query,
        selected_map_mode=selected_map_mode,
    )


@app.route("/dashboard/market", methods=["GET"])
def dashboard_market():
    options = get_filter_options()
    selected_country = request.args.get("country", "").strip()
    selected_power_pool = request.args.get("power_pool", "").strip()
    context = build_market_context(country=selected_country, power_pool=selected_power_pool)
    live_market = build_live_market_context(
        country=selected_country,
        power_pool=selected_power_pool,
        preferred_countries=top_country_codes_by_capacity(context["rows"], limit=6),
        rows=context["rows"],
    )
    return render_template(
        "market.html",
        market=context,
        live_market=live_market,
        filters=options,
        selected_country=selected_country,
        selected_power_pool=selected_power_pool,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
