from __future__ import annotations

import json
import math
import time
from html import unescape
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from database import load_policy_documents, save_policy_documents


IEA_POLICIES_API_URL = "https://api.iea.org/v3/policies"
REQUEST_TIMEOUT = 30
CACHE_TTL_SECONDS = 3600
PAGE_SIZE = 50

_CACHE: dict[str, object] = {"timestamp": 0.0, "items": []}


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(unescape(str(value)).split())


def normalize_filter_value(value: str | None) -> str:
    return clean_text(value).casefold()


def fetch_json(url: str) -> list[dict]:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_names(values: list[dict] | None) -> list[str]:
    if not values:
        return []
    names = []
    for item in values:
        if isinstance(item, dict):
            name = clean_text(item.get("name"))
        else:
            name = clean_text(item)
        if name:
            names.append(name)
    return names


def load_all_policies() -> list[dict]:
    now = time.time()
    cached_items = _CACHE.get("items", [])
    cached_at = float(_CACHE.get("timestamp", 0.0))
    if cached_items and (now - cached_at) < CACHE_TTL_SECONDS:
        return cached_items  # type: ignore[return-value]

    db_items = load_policy_documents(max_age_hours=24)
    if db_items:
        _CACHE["timestamp"] = now
        _CACHE["items"] = db_items
        return db_items

    raw_items = fetch_json(IEA_POLICIES_API_URL)
    normalized = []
    for item in raw_items:
        countries = normalize_names(item.get("countries"))
        topics = normalize_names(item.get("topics"))
        technologies = normalize_names(item.get("technologies"))
        policy_types = normalize_names(item.get("policyTypes"))
        normalized.append(
            {
                "id": clean_text(item.get("policyId")),
                "title": clean_text(item.get("title")),
                "description": clean_text(item.get("description")),
                "country_names": countries,
                "country_label": ", ".join(countries) if countries else "",
                "topics": topics,
                "topic_label": ", ".join(topics) if topics else "",
                "technologies": technologies,
                "technology_label": ", ".join(technologies) if technologies else "",
                "policy_types": policy_types,
                "policy_type_label": ", ".join(policy_types) if policy_types else "",
                "status": clean_text(item.get("status")),
                "jurisdiction": clean_text(item.get("jurisdiction")),
                "year": clean_text(item.get("year")),
                "learn_more": clean_text(item.get("learnMore")),
                "source": clean_text(item.get("source")),
            }
        )

    _CACHE["timestamp"] = now
    _CACHE["items"] = normalized
    save_policy_documents(normalized)
    return normalized


def unique_sorted_values(items: list[dict], key: str) -> list[str]:
    values = sorted({value for item in items for value in item.get(key, []) if value})
    return values


def unique_sorted_scalar(items: list[dict], key: str) -> list[str]:
    values = sorted({item.get(key, "") for item in items if item.get(key, "")})
    return values


def filter_policies(
    items: list[dict],
    country: str = "",
    topic: str = "",
    status: str = "",
    jurisdiction: str = "",
    query: str = "",
) -> list[dict]:
    query_text = query.casefold().strip()
    normalized_country = normalize_filter_value(country)
    normalized_topic = normalize_filter_value(topic)
    normalized_status = normalize_filter_value(status)
    normalized_jurisdiction = normalize_filter_value(jurisdiction)
    filtered = []

    for item in items:
        country_names = {normalize_filter_value(name) for name in item["country_names"]}
        topics = {normalize_filter_value(name) for name in item["topics"]}
        item_status = normalize_filter_value(item["status"])
        item_jurisdiction = normalize_filter_value(item["jurisdiction"])

        if normalized_country and normalized_country not in country_names:
            continue
        if normalized_topic and normalized_topic not in topics:
            continue
        if normalized_status and normalized_status != item_status:
            continue
        if normalized_jurisdiction and normalized_jurisdiction != item_jurisdiction:
            continue
        if query_text:
            haystack = " ".join(
                [
                    item["title"],
                    item["description"],
                    item["country_label"],
                    item["topic_label"],
                    item["technology_label"],
                    item["policy_type_label"],
                ]
            ).casefold()
            if query_text not in haystack:
                continue
        filtered.append(item)

    filtered.sort(
        key=lambda item: (
            int(item["year"]) if str(item["year"]).isdigit() else 0,
            item["title"].casefold(),
        ),
        reverse=True,
    )
    return filtered


def build_source_url() -> str:
    return IEA_POLICIES_API_URL


def fetch_iea_policies(
    country: str = "",
    topic: str = "",
    status: str = "",
    jurisdiction: str = "",
    query: str = "",
    page: int = 1,
) -> dict:
    try:
        all_items = load_all_policies()
    except URLError:
        return {
            "items": [],
            "count": "0",
            "countries": [],
            "topics": [],
            "statuses": [],
            "jurisdictions": [],
            "selected_country": country,
            "selected_topic": topic,
            "selected_status": status,
            "selected_jurisdiction": jurisdiction,
            "selected_query": query,
            "selected_page": 1,
            "pages": [],
            "total_pages": 0,
            "source_url": build_source_url(),
            "error": "The IEA policies source is not reachable right now.",
        }
    normalized_country_lookup = {normalize_filter_value(name): name for name in unique_sorted_values(all_items, "country_names")}
    normalized_topic_lookup = {normalize_filter_value(name): name for name in unique_sorted_values(all_items, "topics")}
    normalized_status_lookup = {normalize_filter_value(name): name for name in unique_sorted_scalar(all_items, "status")}
    normalized_jurisdiction_lookup = {normalize_filter_value(name): name for name in unique_sorted_scalar(all_items, "jurisdiction")}

    selected_country = normalized_country_lookup.get(normalize_filter_value(country), clean_text(country))
    selected_topic = normalized_topic_lookup.get(normalize_filter_value(topic), clean_text(topic))
    selected_status = normalized_status_lookup.get(normalize_filter_value(status), clean_text(status))
    selected_jurisdiction = normalized_jurisdiction_lookup.get(normalize_filter_value(jurisdiction), clean_text(jurisdiction))

    filtered = filter_policies(
        all_items,
        country=selected_country,
        topic=selected_topic,
        status=selected_status,
        jurisdiction=selected_jurisdiction,
        query=query,
    )

    total_count = len(filtered)
    total_pages = max(1, math.ceil(total_count / PAGE_SIZE))
    page = min(max(1, page), total_pages)
    start = (page - 1) * PAGE_SIZE
    end = start + PAGE_SIZE
    items = filtered[start:end]

    page_numbers = list(range(max(1, page - 3), min(total_pages, page + 4) + 1))

    return {
        "items": items,
        "count": f"{total_count:,}",
        "countries": list(normalized_country_lookup.values()),
        "topics": list(normalized_topic_lookup.values()),
        "statuses": list(normalized_status_lookup.values()),
        "jurisdictions": list(normalized_jurisdiction_lookup.values()),
        "selected_country": selected_country,
        "selected_topic": selected_topic,
        "selected_status": selected_status,
        "selected_jurisdiction": selected_jurisdiction,
        "selected_query": query,
        "selected_page": page,
        "pages": page_numbers,
        "total_pages": total_pages,
        "source_url": build_source_url(),
        "error": "",
    }


def build_policy_report(country_names: list[str] | None = None, limit: int = 8) -> dict:
    try:
        items = load_all_policies()
    except URLError:
        return {
            "count": 0,
            "items": [],
            "top_topics": [],
            "top_statuses": [],
            "source_url": build_source_url(),
            "error": "The IEA policies source is not reachable right now.",
        }
    selected_names = {name for name in (country_names or []) if name}

    if selected_names:
        filtered = [
            item for item in items
            if selected_names.intersection(item["country_names"])
        ]
    else:
        filtered = items

    filtered.sort(
        key=lambda item: (
            int(item["year"]) if str(item["year"]).isdigit() else 0,
            item["title"].casefold(),
        ),
        reverse=True,
    )

    topic_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    for item in filtered:
        for topic in item["topics"]:
            topic_counts[topic] = topic_counts.get(topic, 0) + 1
        if item["status"]:
            status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1

    top_topics = sorted(topic_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:5]
    top_statuses = sorted(status_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:5]

    return {
        "count": len(filtered),
        "items": filtered[:limit],
        "top_topics": [{"label": label, "count": count} for label, count in top_topics],
        "top_statuses": [{"label": label, "count": count} for label, count in top_statuses],
        "source_url": build_source_url(),
        "error": "",
    }
