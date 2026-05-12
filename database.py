from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "interconnection_data.sqlite3"
SCHEMA_VERSION = 1


def db_path() -> Path:
    configured = os.environ.get("DATA_DB_PATH", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_DB_PATH


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat()


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@contextmanager
def connect() -> Iterable[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS data_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                url TEXT,
                update_frequency TEXT NOT NULL,
                access_method TEXT NOT NULL DEFAULT 'api',
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ingestion_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL,
                dataset_type TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                records_seen INTEGER NOT NULL DEFAULT 0,
                records_saved INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                FOREIGN KEY (source_key) REFERENCES data_sources(source_key)
            );

            CREATE TABLE IF NOT EXISTS power_flow_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL,
                corridor_key TEXT NOT NULL,
                from_country TEXT,
                to_country TEXT,
                observed_at TEXT NOT NULL,
                actual_flow_mw REAL,
                atc_mw REAL,
                ntc_mw REAL,
                utilization_pct REAL,
                direction TEXT,
                quality_flag TEXT DEFAULT 'raw',
                raw_payload TEXT,
                fetched_at TEXT NOT NULL,
                UNIQUE(source_key, corridor_key, observed_at),
                FOREIGN KEY (source_key) REFERENCES data_sources(source_key)
            );

            CREATE TABLE IF NOT EXISTS energy_flow_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL,
                corridor_key TEXT NOT NULL,
                from_country TEXT,
                to_country TEXT,
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                energy_mwh REAL,
                net_import_mwh REAL,
                net_export_mwh REAL,
                quality_flag TEXT DEFAULT 'raw',
                raw_payload TEXT,
                fetched_at TEXT NOT NULL,
                UNIQUE(source_key, corridor_key, period_start, period_end),
                FOREIGN KEY (source_key) REFERENCES data_sources(source_key)
            );

            CREATE TABLE IF NOT EXISTS market_price_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL,
                market_area TEXT NOT NULL,
                price_type TEXT NOT NULL,
                currency TEXT,
                unit TEXT,
                observed_at TEXT NOT NULL,
                price REAL,
                raw_payload TEXT,
                fetched_at TEXT NOT NULL,
                UNIQUE(source_key, market_area, price_type, observed_at),
                FOREIGN KEY (source_key) REFERENCES data_sources(source_key)
            );

            CREATE TABLE IF NOT EXISTS policy_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL,
                external_id TEXT NOT NULL,
                title TEXT NOT NULL,
                country_label TEXT,
                topic_label TEXT,
                status TEXT,
                jurisdiction TEXT,
                year TEXT,
                source_url TEXT,
                learn_more_url TEXT,
                description TEXT,
                raw_payload TEXT,
                fetched_at TEXT NOT NULL,
                UNIQUE(source_key, external_id),
                FOREIGN KEY (source_key) REFERENCES data_sources(source_key)
            );

            CREATE TABLE IF NOT EXISTS news_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL,
                topic TEXT NOT NULL,
                title TEXT NOT NULL,
                publisher TEXT,
                link TEXT NOT NULL,
                summary TEXT,
                published_at TEXT,
                raw_payload TEXT,
                fetched_at TEXT NOT NULL,
                UNIQUE(source_key, link),
                FOREIGN KEY (source_key) REFERENCES data_sources(source_key)
            );

            CREATE TABLE IF NOT EXISTS infrastructure_assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL,
                asset_id TEXT NOT NULL,
                name TEXT,
                country_a TEXT,
                country_b TEXT,
                status TEXT,
                voltage_kv REAL,
                voltage_class TEXT,
                capacity_mw REAL,
                ac_dc TEXT,
                power_pool TEXT,
                length_km REAL,
                mode TEXT,
                operator TEXT,
                source_dataset TEXT,
                raw_payload TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(source_key, asset_id),
                FOREIGN KEY (source_key) REFERENCES data_sources(source_key)
            );

            CREATE INDEX IF NOT EXISTS idx_power_flow_latest ON power_flow_observations(corridor_key, observed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_energy_flow_latest ON energy_flow_observations(corridor_key, period_start DESC);
            CREATE INDEX IF NOT EXISTS idx_prices_latest ON market_price_observations(market_area, observed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_news_published ON news_items(published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_policy_year ON policy_documents(year DESC);
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES(?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        seed_sources(conn)


def seed_sources(conn: sqlite3.Connection) -> None:
    now = isoformat()
    sources = [
        ("google_news_rss", "Google News RSS", "Policies/news", "https://news.google.com/rss", "daily", "rss", "Current headline feed."),
        ("newsapi", "NewsAPI", "Policies/news", "https://newsapi.org/", "daily", "api", "Optional archive/news provider."),
        ("gnews", "GNews", "Policies/news", "https://gnews.io/", "daily", "api", "Optional archive/news provider."),
        ("iea_policies", "IEA Policies Database", "Policies/news", "https://api.iea.org/v3/policies", "daily", "api", "Policy source used by the Policies page."),
        ("static_interconnections_csv", "Static Interconnections CSV", "Static infrastructure", "datasets/interconnections.csv", "manual", "manual_csv", "Static infrastructure dataset maintained manually."),
        ("entsoe_transparency", "ENTSO-E Transparency Platform", "Power flow / Energy flow / Market prices", "https://transparency.entsoe.eu/", "15 min / hourly / daily", "api", "Future live Europe power-flow, exchange, and price source."),
        ("eia_open_data", "EIA Open Data", "Power flow / Energy flow / Market prices", "https://www.eia.gov/opendata/", "15 min / hourly / daily", "api", "Future North America operating data source."),
        ("web_crawl", "Curated Web Crawl", "Policies/news", "", "daily", "crawler", "Reserved for future crawler outputs."),
    ]
    conn.executemany(
        """
        INSERT INTO data_sources(source_key, name, category, url, update_frequency, access_method, notes, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            name=excluded.name,
            category=excluded.category,
            url=excluded.url,
            update_frequency=excluded.update_frequency,
            access_method=excluded.access_method,
            notes=excluded.notes,
            updated_at=excluded.updated_at
        """,
        [(*source, now, now) for source in sources],
    )


def start_run(source_key: str, dataset_type: str) -> int:
    init_db()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO ingestion_runs(source_key, dataset_type, status, started_at)
            VALUES(?, ?, 'running', ?)
            """,
            (source_key, dataset_type, isoformat()),
        )
        return int(cursor.lastrowid)


def finish_run(run_id: int, status: str, records_seen: int = 0, records_saved: int = 0, error: str = "") -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE ingestion_runs
            SET status=?, finished_at=?, records_seen=?, records_saved=?, error=?
            WHERE id=?
            """,
            (status, isoformat(), records_seen, records_saved, error, run_id),
        )


def save_news_items(items: list[object], source_key: str = "google_news_rss") -> int:
    init_db()
    now = isoformat()
    rows = []
    for item in items:
        published_at = getattr(item, "published_at", None)
        rows.append(
            (
                source_key,
                getattr(item, "topic", ""),
                getattr(item, "title", ""),
                getattr(item, "source", ""),
                getattr(item, "link", ""),
                getattr(item, "summary", ""),
                published_at.astimezone(timezone.utc).isoformat() if published_at else None,
                json.dumps(
                    {
                        "topic": getattr(item, "topic", ""),
                        "title": getattr(item, "title", ""),
                        "source": getattr(item, "source", ""),
                        "link": getattr(item, "link", ""),
                    }
                ),
                now,
            )
        )
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO news_items(source_key, topic, title, publisher, link, summary, published_at, raw_payload, fetched_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key, link) DO UPDATE SET
                topic=excluded.topic,
                title=excluded.title,
                publisher=excluded.publisher,
                summary=excluded.summary,
                published_at=excluded.published_at,
                raw_payload=excluded.raw_payload,
                fetched_at=excluded.fetched_at
            """,
            rows,
        )
    return len(rows)


def load_recent_news(topics: list[str], max_age_hours: int = 24, limit: int = 30) -> list[dict]:
    init_db()
    threshold = utc_now() - timedelta(hours=max_age_hours)
    placeholders = ",".join("?" for _ in topics)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM news_items
            WHERE topic IN ({placeholders}) AND fetched_at >= ?
            ORDER BY COALESCE(published_at, fetched_at) DESC
            LIMIT ?
            """,
            [*topics, threshold.isoformat(), limit],
        ).fetchall()
    return [dict(row) for row in rows]


def save_policy_documents(items: list[dict], source_key: str = "iea_policies") -> int:
    init_db()
    now = isoformat()
    rows = []
    for item in items:
        external_id = str(item.get("id") or item.get("title") or "")
        if not external_id:
            continue
        rows.append(
            (
                source_key,
                external_id,
                item.get("title", ""),
                item.get("country_label", ""),
                item.get("topic_label", ""),
                item.get("status", ""),
                item.get("jurisdiction", ""),
                item.get("year", ""),
                item.get("source", ""),
                item.get("learn_more", ""),
                item.get("description", ""),
                json.dumps(item, default=str),
                now,
            )
        )
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO policy_documents(source_key, external_id, title, country_label, topic_label, status, jurisdiction, year, source_url, learn_more_url, description, raw_payload, fetched_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key, external_id) DO UPDATE SET
                title=excluded.title,
                country_label=excluded.country_label,
                topic_label=excluded.topic_label,
                status=excluded.status,
                jurisdiction=excluded.jurisdiction,
                year=excluded.year,
                source_url=excluded.source_url,
                learn_more_url=excluded.learn_more_url,
                description=excluded.description,
                raw_payload=excluded.raw_payload,
                fetched_at=excluded.fetched_at
            """,
            rows,
        )
    return len(rows)


def load_policy_documents(max_age_hours: int = 24) -> list[dict]:
    init_db()
    threshold = utc_now() - timedelta(hours=max_age_hours)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM policy_documents
            WHERE fetched_at >= ?
            ORDER BY CAST(year AS INTEGER) DESC, title ASC
            """,
            (threshold.isoformat(),),
        ).fetchall()
    items = []
    for row in rows:
        payload = json.loads(row["raw_payload"] or "{}")
        if payload:
            items.append(payload)
    return items


def save_infrastructure_assets(rows: list[dict], source_key: str = "static_interconnections_csv") -> int:
    init_db()
    now = isoformat()
    values = []
    for row in rows:
        asset_id = str(row.get("id") or row.get("name") or "")
        if not asset_id:
            continue
        values.append(
            (
                source_key,
                asset_id,
                row.get("name", ""),
                row.get("country_a", ""),
                row.get("country_b", ""),
                row.get("status", ""),
                row.get("voltage_kv"),
                row.get("voltage_class", ""),
                row.get("capacity_mw"),
                row.get("ac_dc", ""),
                row.get("power_pool", ""),
                row.get("length_km"),
                row.get("mode", ""),
                row.get("operator", ""),
                row.get("source_dataset", ""),
                json.dumps(row, default=str),
                now,
            )
        )
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO infrastructure_assets(source_key, asset_id, name, country_a, country_b, status, voltage_kv, voltage_class, capacity_mw, ac_dc, power_pool, length_km, mode, operator, source_dataset, raw_payload, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key, asset_id) DO UPDATE SET
                name=excluded.name,
                country_a=excluded.country_a,
                country_b=excluded.country_b,
                status=excluded.status,
                voltage_kv=excluded.voltage_kv,
                voltage_class=excluded.voltage_class,
                capacity_mw=excluded.capacity_mw,
                ac_dc=excluded.ac_dc,
                power_pool=excluded.power_pool,
                length_km=excluded.length_km,
                mode=excluded.mode,
                operator=excluded.operator,
                source_dataset=excluded.source_dataset,
                raw_payload=excluded.raw_payload,
                updated_at=excluded.updated_at
            """,
            values,
        )
    return len(values)


def data_status() -> dict:
    init_db()
    with connect() as conn:
        sources = [dict(row) for row in conn.execute("SELECT * FROM data_sources ORDER BY category, name").fetchall()]
        runs = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM ingestion_runs
                ORDER BY started_at DESC
                LIMIT 20
                """
            ).fetchall()
        ]
        counts = {}
        for table in (
            "power_flow_observations",
            "energy_flow_observations",
            "market_price_observations",
            "policy_documents",
            "news_items",
            "infrastructure_assets",
        ):
            counts[table] = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
    return {
        "database_path": str(db_path()),
        "sources": sources,
        "recent_runs": runs,
        "record_counts": counts,
    }
