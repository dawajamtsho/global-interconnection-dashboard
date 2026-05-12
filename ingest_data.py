from __future__ import annotations

import argparse
import sys

from database import (
    data_status,
    finish_run,
    init_db,
    save_infrastructure_assets,
    save_news_items,
    save_policy_documents,
    start_run,
)
from app import DEFAULT_TOPICS, fetch_gnews_topic_news, fetch_newsapi_topic_news, fetch_topic_news
from iea_policies import load_all_policies
from isa_dashboard_data import load_interconnections


def ingest_static_infrastructure() -> None:
    run_id = start_run("static_interconnections_csv", "static_infrastructure")
    try:
        rows = load_interconnections()
        saved = save_infrastructure_assets(rows)
        finish_run(run_id, "success", records_seen=len(rows), records_saved=saved)
        print(f"Saved {saved} infrastructure records.")
    except Exception as exc:
        finish_run(run_id, "failed", error=str(exc))
        raise


def ingest_policies() -> None:
    run_id = start_run("iea_policies", "policies")
    try:
        items = load_all_policies()
        saved = save_policy_documents(items)
        finish_run(run_id, "success", records_seen=len(items), records_saved=saved)
        print(f"Saved {saved} policy records.")
    except Exception as exc:
        finish_run(run_id, "failed", error=str(exc))
        raise


def ingest_news() -> None:
    source_seen = {"google_news_rss": 0, "newsapi": 0, "gnews": 0}
    source_saved = {"google_news_rss": 0, "newsapi": 0, "gnews": 0}
    run_ids = {key: start_run(key, "news") for key in source_seen}
    errors: dict[str, str] = {}

    providers = [
        ("google_news_rss", fetch_topic_news),
        ("newsapi", fetch_newsapi_topic_news),
        ("gnews", fetch_gnews_topic_news),
    ]
    for source_key, fetcher in providers:
        try:
            items = []
            for topic in DEFAULT_TOPICS:
                items.extend(fetcher(topic, 10))
            source_seen[source_key] = len(items)
            source_saved[source_key] = save_news_items(items, source_key=source_key)
        except Exception as exc:
            errors[source_key] = str(exc)

    for source_key, run_id in run_ids.items():
        if source_key in errors:
            finish_run(run_id, "failed", records_seen=source_seen[source_key], records_saved=source_saved[source_key], error=errors[source_key])
        else:
            finish_run(run_id, "success", records_seen=source_seen[source_key], records_saved=source_saved[source_key])
        print(f"{source_key}: saved {source_saved[source_key]} records.")


def placeholder(dataset_type: str) -> None:
    print(
        f"{dataset_type} database tables are ready, but no live provider ingestion has been enabled yet. "
        "Add provider-specific fetch code here or schedule this command after connecting a source."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest API/crawler/manual data into the dashboard database.")
    parser.add_argument(
        "command",
        choices=["init", "static", "news", "policies", "power-flow", "energy-flow", "market-prices", "all", "status"],
    )
    args = parser.parse_args()

    if args.command == "init":
        init_db()
        print("Database initialized.")
    elif args.command == "static":
        ingest_static_infrastructure()
    elif args.command == "news":
        ingest_news()
    elif args.command == "policies":
        ingest_policies()
    elif args.command == "power-flow":
        placeholder("Power flow")
    elif args.command == "energy-flow":
        placeholder("Energy flow")
    elif args.command == "market-prices":
        placeholder("Market prices")
    elif args.command == "all":
        init_db()
        ingest_static_infrastructure()
        ingest_policies()
        ingest_news()
    elif args.command == "status":
        status = data_status()
        print(f"Database: {status['database_path']}")
        for table, count in status["record_counts"].items():
            print(f"{table}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
