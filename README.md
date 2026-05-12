# Electricity and ISA Newsletter Generator

Small Flask website that pulls the latest headlines for electricity and International Solar Alliance topics, then formats them into a newsletter-style brief.

## Run locally

```bash
cd /Users/dawajamtsho/Documents/newsletter_site
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run_newsletter.py
```

This version also tries to open the browser for you automatically and will use the first free port from `5000` to `5010`.

If it does not open by itself, try either of these addresses:

- `http://127.0.0.1:5000`
- `http://localhost:5000`

If `python app.py` does not work, use:

```bash
/Users/dawajamtsho/Documents/newsletter_site/.venv/bin/python /Users/dawajamtsho/Documents/newsletter_site/run_newsletter.py
```

## Notes

- The app uses Google News RSS by default.
- You can also add API providers by setting `NEWSAPI_KEY` and/or `GNEWS_API_KEY` before starting the app.
- When those API keys are set, the app merges API results with the RSS feed and de-duplicates matching stories.
- The `Market` page can also use `EIA_API_KEY` and `ENTSOE_TOKEN` for live market snapshots.

## Optional API setup

Create a local `.env` file in the project folder and add any keys you want the app to use:

```bash
cp .env.example .env
```

Then fill in values like:

```bash
EIA_API_KEY="your_eia_key"
ENTSOE_TOKEN="your_entsoe_token"
NEWSAPI_KEY="your_newsapi_key"
GNEWS_API_KEY="your_gnews_key"
```

The app loads `.env` automatically on startup.

You can still use shell exports if you prefer:

```bash
export NEWSAPI_KEY="your_newsapi_key"
export GNEWS_API_KEY="your_gnews_key"
export EIA_API_KEY="your_eia_key"
export ENTSOE_TOKEN="your_entsoe_token"
python run_newsletter.py
```

## Database and ingestion

The app now has a database layer in `database.py`. By default it creates a local SQLite database at:

```bash
data/interconnection_data.sqlite3
```

Use `DATA_DB_PATH` to place the database somewhere persistent on a hosted server:

```bash
export DATA_DB_PATH=/var/data/interconnection_data.sqlite3
```

Initialize and load static infrastructure:

```bash
.venv/bin/python ingest_data.py init
.venv/bin/python ingest_data.py static
```

Run data updates:

```bash
.venv/bin/python ingest_data.py news
.venv/bin/python ingest_data.py policies
```

Planned update schedule:

- Power flow: every 15 minutes
- Energy flow: hourly
- Policies/news: daily
- Market prices: daily
- Static infrastructure: manual update

The database schema already includes source-attributed tables for power flow, energy flow, market prices, policies, news, and static infrastructure. Use `/data/status` to inspect configured sources, record counts, and recent ingestion runs.
