from __future__ import annotations

import json
import os
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from io import StringIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree


REQUEST_TIMEOUT = 20
EIA_BASE = "https://api.eia.gov/v2/electricity/rto/region-data/data/"
ENTSOE_BASE = "https://web-api.tp.entsoe.eu/api"
EIA_LINK = "https://www.eia.gov/opendata/"
ENTSOE_LINK = "https://transparency.entsoe.eu/"
OWID_URLS = [
    "https://raw.githubusercontent.com/owid/energy-data/master/owid-energy-data.csv",
    "https://nyc3.digitaloceanspaces.com/owid-public/data/energy/owid-energy-data.csv",
]

EIA_RESPONDENTS = {
    "USA": {"code": "US", "label": "United States"},
    "CAN": {"code": "BCHA", "label": "BC Hydro / Canada"},
}

ENTSOE_DOMAINS = {
    "DEU": {"code": "10Y1001A1001A83F", "label": "Germany"},
    "FRA": {"code": "10YFR-RTE------C", "label": "France"},
    "GBR": {"code": "10YGB----------A", "label": "United Kingdom"},
    "ESP": {"code": "10YES-REE------0", "label": "Spain"},
    "ITA": {"code": "10YIT-GRTN-----B", "label": "Italy"},
    "POL": {"code": "10YPL-AREA-----S", "label": "Poland"},
    "NLD": {"code": "10YNL----------L", "label": "Netherlands"},
    "BEL": {"code": "10YBE----------2", "label": "Belgium"},
    "CHE": {"code": "10YCH-SWISSGRIDZ", "label": "Switzerland"},
    "AUT": {"code": "10YAT-APG------L", "label": "Austria"},
    "CZE": {"code": "10YCZ-CEPS-----N", "label": "Czech Republic"},
    "SVK": {"code": "10YSK-SEPS-----K", "label": "Slovakia"},
    "HUN": {"code": "10YHU-MAVIR----U", "label": "Hungary"},
    "ROU": {"code": "10YRO-TEL------P", "label": "Romania"},
    "BGR": {"code": "10YCA-BULGARIA-R", "label": "Bulgaria"},
    "GRC": {"code": "10YGR-HTSO-----Y", "label": "Greece"},
    "HRV": {"code": "10YHR-HEP------M", "label": "Croatia"},
    "SRB": {"code": "10YCS-SERBIATSOV", "label": "Serbia"},
    "SVN": {"code": "10YSI-ELES-----O", "label": "Slovenia"},
    "NOR": {"code": "10YNO-0--------C", "label": "Norway"},
    "SWE": {"code": "10YSE-1--------K", "label": "Sweden"},
    "FIN": {"code": "10YFI-1--------U", "label": "Finland"},
    "DNK": {"code": "10Y1001A1001A65H", "label": "Denmark"},
    "PRT": {"code": "10YPT-REN------W", "label": "Portugal"},
    "EST": {"code": "10Y1001A1001A39I", "label": "Estonia"},
    "LVA": {"code": "10YLV-1001A00074", "label": "Latvia"},
    "LTU": {"code": "10YLT-1001A0008Q", "label": "Lithuania"},
    "TUR": {"code": "10YTR-TEIAS----W", "label": "Turkey"},
    "UKR": {"code": "10YUA-WEPS-----0", "label": "Ukraine"},
    "IRL": {"code": "10YIE-1001A00010", "label": "Ireland"},
}

POOL_DEFAULTS = {
    "ENTSO-E": ("entsoe", "DEU"),
    "NERC": ("eia", "USA"),
}

REGION_DEFAULTS = {
    "Europe": ("entsoe", "DEU"),
    "North America": ("eia", "USA"),
}

REFERENCE_SOURCES = {
    "CIER": {
        "name": "CIER / South America Source",
        "source": "CIER / regional operators",
        "metric": "Regional interconnection reference",
        "detail": "Primary South American cross-border coverage is through CIER and national operators rather than one shared public real-time API.",
        "link": "https://www.cier.org/",
    },
    "SIEPAC": {
        "name": "SIEPAC / Central America Source",
        "source": "SIEPAC / CRIE",
        "metric": "Regional market source",
        "detail": "Central American market and cross-border information is published through SIEPAC and CRIE portals.",
        "link": "https://www.eie.or.cr/",
    },
    "APG": {
        "name": "APG / ASEAN Source",
        "source": "ASEAN Centre for Energy",
        "metric": "Regional interconnection tracker",
        "detail": "ASEAN and APG data is primarily available through ACE project tracking and country reports.",
        "link": "https://aseanenergy.org/asean-power-grid/",
    },
    "ASEAN": {
        "name": "ASEAN Power Grid Source",
        "source": "ASEAN Centre for Energy",
        "metric": "Regional interconnection tracker",
        "detail": "ASEAN power integration data is available through ACE publications and APG project tracking.",
        "link": "https://aseanenergy.org/asean-power-grid/",
    },
    "SAARC": {
        "name": "South Asia Cross-Border Source",
        "source": "CTU / CEA / regional utilities",
        "metric": "Regional exchange source",
        "detail": "South Asia cross-border information is typically published through national utilities and regional reports rather than one unified API.",
        "link": "https://cea.nic.in/",
    },
    "CAREN": {
        "name": "Central Asia Source",
        "source": "CAREN / regional operators",
        "metric": "Regional interconnection source",
        "detail": "Central Asia interconnection data is mostly available through regional operator reports and project documentation.",
        "link": "https://www.carenet.org/",
    },
    "SAPP": {
        "name": "SAPP Market Source",
        "source": "Southern African Power Pool",
        "metric": "Regional market and DAM source",
        "detail": "SAPP publishes day-ahead market results and regional reports through its portal.",
        "link": "https://www.sapp.co.zw/",
    },
    "EAPP": {
        "name": "EAPP Operational Source",
        "source": "Eastern Africa Power Pool",
        "metric": "Operational report source",
        "detail": "EAPP cross-border information is published through monthly and annual operational reports.",
        "link": "https://eappool.org/",
    },
    "WAPP": {
        "name": "WAPP Market Source",
        "source": "West African Power Pool",
        "metric": "Regional market bulletin source",
        "detail": "WAPP publishes interconnection and market information mainly through reports and bulletins.",
        "link": "https://www.ecowapp.org/",
    },
    "GCC": {
        "name": "GCC Interconnection Source",
        "source": "GCCIA",
        "metric": "Regional interconnection source",
        "detail": "GCC interconnection information is published by GCCIA through annual and operational reports.",
        "link": "https://www.gccia.com.sa/",
    },
    "GCCIA": {
        "name": "GCCIA Source",
        "source": "GCCIA",
        "metric": "Regional interconnection source",
        "detail": "GCCIA provides public interconnection reference data and annual system statistics.",
        "link": "https://www.gccia.com.sa/",
    },
}

_OWID_CACHE: dict[str, object] = {"loaded": False, "rows": {}}

PSR_TYPE_LABELS = {
    "B01": "Biomass",
    "B02": "Lignite",
    "B04": "Gas",
    "B05": "Hard coal",
    "B06": "Oil",
    "B09": "Geothermal",
    "B10": "Pumped hydro",
    "B11": "Hydro",
    "B12": "Hydro reservoir",
    "B14": "Nuclear",
    "B15": "Other renewables",
    "B16": "Solar",
    "B17": "Waste",
    "B18": "Wind offshore",
    "B19": "Wind onshore",
    "B20": "Other",
}


def _fetch_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.read().decode("utf-8", errors="ignore")


def _to_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _eia_api_key() -> str:
    return os.environ.get("EIA_API_KEY", "").strip()


def _entsoe_token() -> str:
    return os.environ.get("ENTSOE_TOKEN", "").strip() or os.environ.get("ENTSOE_API_KEY", "").strip()


def _eia_fetch_rows(endpoint: str, params: dict, windows: list[tuple[int, int]] | None = None) -> tuple[list[dict], str | None]:
    api_key = _eia_api_key()
    if not api_key:
        return [], "EIA_API_KEY is not configured."

    request_params = dict(params)
    request_params["api_key"] = api_key

    if windows is None:
        windows = [(5, 0), (24, 0), (7 * 24, 0), (30 * 24, 0)]

    last_error: str | None = None
    now_dt = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    for hours_back, lag_hours in windows:
        start_dt = now_dt - timedelta(hours=hours_back + lag_hours)
        end_dt = now_dt - timedelta(hours=lag_hours)
        query_params = dict(request_params)
        query_params["start"] = start_dt.strftime("%Y-%m-%dT%H")
        query_params["end"] = end_dt.strftime("%Y-%m-%dT%H")
        try:
            payload = _fetch_json(f"{endpoint}?{urlencode(query_params)}")
        except Exception as exc:
            last_error = str(exc)
            continue
        rows = ((payload.get("response") or {}).get("data") or [])
        if rows:
            return rows, None

    fallback_params = dict(request_params)
    fallback_params.pop("start", None)
    fallback_params.pop("end", None)
    try:
        payload = _fetch_json(f"{endpoint}?{urlencode(fallback_params)}")
        rows = ((payload.get("response") or {}).get("data") or [])
        if rows:
            return rows, None
    except Exception as exc:
        last_error = str(exc)

    return [], last_error


def _load_owid_rows() -> dict[str, dict]:
    if _OWID_CACHE["loaded"]:
        return _OWID_CACHE["rows"]  # type: ignore[return-value]

    latest_by_iso: dict[str, dict] = {}
    for url in OWID_URLS:
        try:
            text = _fetch_text(url)
            reader = csv.DictReader(StringIO(text))
            for row in reader:
                iso = (row.get("iso_code") or "").strip().upper()
                year = row.get("year")
                if not iso or len(iso) != 3 or not year or not year.isdigit():
                    continue
                current = latest_by_iso.get(iso)
                if current is None or int(year) > int(current["year"]):
                    latest_by_iso[iso] = row
            if latest_by_iso:
                break
        except (HTTPError, URLError, TimeoutError, ValueError):
            continue

    _OWID_CACHE["loaded"] = True
    _OWID_CACHE["rows"] = latest_by_iso
    return latest_by_iso


def _owid_query(country_code: str) -> dict:
    rows = _load_owid_rows()
    row = rows.get(country_code)
    if not row:
        return _unsupported_card(
            "OWID Electricity Snapshot",
            f"No OWID electricity snapshot was found for {country_code}.",
        )

    total_generation = _to_float(row.get("electricity_generation"))
    renewables = _to_float(row.get("renewables_electricity"))
    renewables_share = None
    if total_generation and renewables is not None and total_generation > 0:
        renewables_share = round((renewables / total_generation) * 100, 1)

    country_name = row.get("country") or country_code
    year = row.get("year") or "Latest"

    return {
        "name": f"OWID Electricity Snapshot · {country_name}",
        "source": "Our World in Data",
        "status": "annual",
        "region": country_name,
        "metric": f"Annual electricity generation ({year})",
        "latest_value": round(total_generation, 2) if total_generation is not None else None,
        "latest_unit": "TWh",
        "latest_period": year,
        "average_value": None,
        "detail": (
            f"Renewables share: {renewables_share}%"
            if renewables_share is not None
            else "Renewables share not available."
        ),
        "link": "https://ourworldindata.org/energy",
    }


def _unsupported_card(title: str, detail: str) -> dict:
    return {
        "name": title,
        "source": "Live market APIs",
        "status": "unsupported",
        "error": detail,
        "link": "#",
    }


def _reference_card(pool_key: str) -> dict:
    source = REFERENCE_SOURCES[pool_key]
    return {
        "name": source["name"],
        "source": source["source"],
        "status": "reference",
        "region": pool_key,
        "metric": source["metric"],
        "detail": source["detail"],
        "link": source["link"],
    }


def _eia_query(country_code: str) -> dict:
    if not _eia_api_key():
        return {
            "name": "EIA Demand Snapshot",
            "source": "EIA Open Data",
            "status": "missing_key",
            "error": "EIA_API_KEY is not configured.",
            "link": EIA_LINK,
        }

    target = EIA_RESPONDENTS.get(country_code)
    if not target:
        return _unsupported_card(
            "EIA Demand Snapshot",
            f"No EIA live mapping is configured for {country_code}.",
        )

    params = {
        "frequency": "hourly",
        "data[0]": "value",
        "facets[respondent][]": target["code"],
        "facets[type][]": "D",
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "offset": 0,
        "length": 6,
    }
    rows, error = _eia_fetch_rows(EIA_BASE, params)
    if error and not rows:
        return {
            "name": f"EIA Demand Snapshot · {target['label']}",
            "source": "EIA Open Data",
            "status": "error",
            "error": error,
            "link": EIA_LINK,
        }
    if not rows:
        return {
            "name": f"EIA Demand Snapshot · {target['label']}",
            "source": "EIA Open Data",
            "status": "empty",
            "error": "No EIA data was returned for this geography.",
            "link": EIA_LINK,
        }

    latest = rows[0]
    values = [_to_float(row.get("value")) for row in rows]
    clean_values = [value for value in values if value is not None]
    average_value = sum(clean_values) / len(clean_values) if clean_values else None
    return {
        "name": f"EIA Demand Snapshot · {target['label']}",
        "source": "EIA Open Data",
        "status": "ok",
        "region": latest.get("respondent-name", target["label"]),
        "metric": latest.get("type-name", "Demand"),
        "latest_value": _to_float(latest.get("value")),
        "latest_unit": latest.get("value-units", "megawatthours"),
        "latest_period": latest.get("period", ""),
        "average_value": round(average_value, 2) if average_value is not None else None,
        "link": EIA_LINK,
    }


def _entsoe_namespace(root: ElementTree.Element) -> dict[str, str]:
    if root.tag.startswith("{"):
        namespace = root.tag.split("}", 1)[0][1:]
        return {"ns": namespace}
    return {}


def _entsoe_find_text(root: ElementTree.Element, path: str, namespaces: dict[str, str]) -> str:
    node = root.find(path, namespaces) if namespaces else root.find(path.replace("ns:", ""))
    return node.text.strip() if node is not None and node.text else ""


def _parse_entsoe_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_entsoe_resolution(value: str) -> timedelta:
    if value == "PT15M":
        return timedelta(minutes=15)
    if value == "PT30M":
        return timedelta(minutes=30)
    if value == "PT60M":
        return timedelta(hours=1)
    if value.startswith("PT") and value.endswith("M"):
        minutes = _to_float(value[2:-1])
        if minutes:
            return timedelta(minutes=minutes)
    return timedelta(hours=1)


@lru_cache(maxsize=512)
def _entsoe_directional_flow(
    from_domain: str,
    to_domain: str,
    period_start: str,
    period_end: str,
) -> dict:
    token = _entsoe_token()
    if not token:
        return {"status": "missing_key", "error": "ENTSOE_TOKEN is not configured."}

    params = {
        "securityToken": token,
        "documentType": "A11",
        "in_Domain": to_domain,
        "out_Domain": from_domain,
        "periodStart": period_start,
        "periodEnd": period_end,
    }
    try:
        root = ElementTree.fromstring(_fetch_text(f"{ENTSOE_BASE}?{urlencode(params)}"))
    except (HTTPError, URLError, TimeoutError, ValueError, ElementTree.ParseError) as exc:
        return {"status": "error", "error": str(exc)}

    namespaces = _entsoe_namespace(root)
    points = root.findall(".//ns:Point", namespaces) if namespaces else root.findall(".//Point")
    if not points:
        reason = _entsoe_find_text(root, ".//ns:text", namespaces)
        return {"status": "empty", "error": reason or "No cross-border flow data returned."}

    period_start_text = _entsoe_find_text(root, ".//ns:timeInterval/ns:start", namespaces)
    resolution_text = _entsoe_find_text(root, ".//ns:resolution", namespaces)
    interval_start = _parse_entsoe_timestamp(period_start_text)
    resolution = _parse_entsoe_resolution(resolution_text)
    parsed_points: list[tuple[int, float]] = []
    for point in points:
        quantity_node = point.find("ns:quantity", namespaces) if namespaces else point.find("quantity")
        position_node = point.find("ns:position", namespaces) if namespaces else point.find("position")
        quantity = _to_float(quantity_node.text if quantity_node is not None else None)
        position = int(position_node.text) if position_node is not None and position_node.text else None
        if quantity is not None and position is not None:
            parsed_points.append((position, quantity))

    if not parsed_points:
        return {"status": "empty", "error": "No usable cross-border flow points returned."}

    parsed_points.sort(key=lambda item: item[0])
    latest_position, latest_value = parsed_points[-1]
    latest_at = None
    if interval_start is not None:
        latest_at = interval_start + (latest_position - 1) * resolution
    resolution_hours = resolution.total_seconds() / 3600

    return {
        "status": "ok",
        "latest_value": round(latest_value, 2),
        "energy_mwh": round(sum(value * resolution_hours for _, value in parsed_points), 2),
        "latest_period": latest_at.isoformat() if latest_at else f"Point {latest_position}",
        "points": len(parsed_points),
    }


def _flow_time_window() -> tuple[str, str]:
    end_dt = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start_dt = end_dt - timedelta(hours=24)
    return start_dt.strftime("%Y%m%d%H%M"), end_dt.strftime("%Y%m%d%H%M")


def build_entsoe_corridor_flow_context(rows: list[dict], max_corridors: int = 30) -> dict[tuple[str, str], dict]:
    if not _entsoe_token():
        return {}

    candidates: dict[tuple[str, str], float] = {}
    for row in rows:
        country_a = row.get("country_a", "")
        country_b = row.get("country_b", "")
        if country_a not in ENTSOE_DOMAINS or country_b not in ENTSOE_DOMAINS:
            continue
        key = tuple(sorted((country_a, country_b)))
        candidates[key] = candidates.get(key, 0.0) + (row.get("capacity_mw") or 0.0)

    ordered_pairs = sorted(candidates, key=lambda key: candidates[key], reverse=True)[:max_corridors]
    period_start, period_end = _flow_time_window()
    directional_results: dict[tuple[str, str], dict] = {}

    with ThreadPoolExecutor(max_workers=8) as executor:
        future_map = {}
        for country_a, country_b in ordered_pairs:
            domain_a = ENTSOE_DOMAINS[country_a]["code"]
            domain_b = ENTSOE_DOMAINS[country_b]["code"]
            future_map[executor.submit(_entsoe_directional_flow, domain_a, domain_b, period_start, period_end)] = (country_a, country_b)
            future_map[executor.submit(_entsoe_directional_flow, domain_b, domain_a, period_start, period_end)] = (country_b, country_a)

        for future in as_completed(future_map):
            directional_results[future_map[future]] = future.result()

    flow_context: dict[tuple[str, str], dict] = {}
    for country_a, country_b in ordered_pairs:
        a_to_b = directional_results.get((country_a, country_b), {"status": "empty"})
        b_to_a = directional_results.get((country_b, country_a), {"status": "empty"})

        ok_flows = [flow for flow in (a_to_b, b_to_a) if flow.get("status") == "ok"]
        if not ok_flows:
            error = a_to_b.get("error") or b_to_a.get("error") or "No recent ENTSO-E flow data returned."
            flow_context[(country_a, country_b)] = {
                "status": "unavailable",
                "source": "ENTSO-E Transparency",
                "error": error,
            }
            continue

        a_value = a_to_b.get("latest_value") if a_to_b.get("status") == "ok" else 0
        b_value = b_to_a.get("latest_value") if b_to_a.get("status") == "ok" else 0
        a_energy = a_to_b.get("energy_mwh") if a_to_b.get("status") == "ok" else 0
        b_energy = b_to_a.get("energy_mwh") if b_to_a.get("status") == "ok" else 0
        net_value = (a_value or 0) - (b_value or 0)
        net_energy = (a_energy or 0) - (b_energy or 0)
        if net_value >= 0:
            from_code, to_code, latest_value = country_a, country_b, net_value
        else:
            from_code, to_code, latest_value = country_b, country_a, abs(net_value)
        if net_energy >= 0:
            energy_from_code, energy_to_code = country_a, country_b
        else:
            energy_from_code, energy_to_code = country_b, country_a
        latest_energy_mwh = abs(net_energy)

        latest_period = a_to_b.get("latest_period") or b_to_a.get("latest_period") or ""
        flow_context[(country_a, country_b)] = {
            "status": "ok",
            "source": "ENTSO-E Transparency",
            "metric": "Actual cross-border physical flow",
            "from": _entsoe_country_name(from_code),
            "to": _entsoe_country_name(to_code),
            "latest_value": round(latest_value, 2),
            "latest_unit": "MW",
            "energy_value": round(latest_energy_mwh, 2),
            "energy_unit": "MWh",
            "energy_from": _entsoe_country_name(energy_from_code),
            "energy_to": _entsoe_country_name(energy_to_code),
            "latest_period": latest_period,
            "window": "Last 24 hours",
            "link": ENTSOE_LINK,
        }
    return flow_context


def _entsoe_query(country_code: str) -> dict:
    token = _entsoe_token()
    if not token:
        return {
            "name": "ENTSO-E Load Snapshot",
            "source": "ENTSO-E Transparency",
            "status": "missing_key",
            "error": "ENTSOE_TOKEN is not configured.",
            "link": ENTSOE_LINK,
        }

    target = ENTSOE_DOMAINS.get(country_code)
    if not target:
        return _unsupported_card(
            "ENTSO-E Load Snapshot",
            f"No ENTSO-E live mapping is configured for {country_code}.",
        )

    end_dt = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=1)
    params = {
        "securityToken": token,
        "documentType": "A65",
        "processType": "A16",
        "outBiddingZone_Domain": target["code"],
        "periodStart": start_dt.strftime("%Y%m%d%H%M"),
        "periodEnd": end_dt.strftime("%Y%m%d%H%M"),
    }
    url = f"{ENTSOE_BASE}?{urlencode(params)}"
    try:
        xml_text = _fetch_text(url)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return {
            "name": f"ENTSO-E Load Snapshot · {target['label']}",
            "source": "ENTSO-E Transparency",
            "status": "error",
            "error": str(exc),
            "link": ENTSOE_LINK,
        }

    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        return {
            "name": f"ENTSO-E Load Snapshot · {target['label']}",
            "source": "ENTSO-E Transparency",
            "status": "error",
            "error": str(exc),
            "link": ENTSOE_LINK,
        }

    namespaces = _entsoe_namespace(root)
    points = root.findall(".//ns:Point", namespaces) if namespaces else root.findall(".//Point")
    if not points:
        reason = root.find(".//ns:text", namespaces) if namespaces else root.find(".//text")
        return {
            "name": f"ENTSO-E Load Snapshot · {target['label']}",
            "source": "ENTSO-E Transparency",
            "status": "empty",
            "error": reason.text.strip() if reason is not None and reason.text else "No recent data returned for this geography.",
            "link": ENTSOE_LINK,
        }

    quantity_tag = "ns:quantity" if namespaces else "quantity"
    position_tag = "ns:position" if namespaces else "position"
    parsed_points: list[tuple[int, float]] = []
    for point in points:
        quantity_node = point.find(quantity_tag, namespaces) if namespaces else point.find(quantity_tag)
        position_node = point.find(position_tag, namespaces) if namespaces else point.find(position_tag)
        quantity = _to_float(quantity_node.text if quantity_node is not None else None)
        position = int(position_node.text) if position_node is not None and position_node.text else None
        if quantity is not None and position is not None:
            parsed_points.append((position, quantity))

    if not parsed_points:
        return {
            "name": f"ENTSO-E Load Snapshot · {target['label']}",
            "source": "ENTSO-E Transparency",
            "status": "empty",
            "error": "No usable live points were returned for this geography.",
            "link": ENTSOE_LINK,
        }

    parsed_points.sort(key=lambda item: item[0])
    latest_position, latest_value = parsed_points[-1]
    average_value = sum(value for _, value in parsed_points) / len(parsed_points)
    return {
        "name": f"ENTSO-E Load Snapshot · {target['label']}",
        "source": "ENTSO-E Transparency",
        "status": "ok",
        "region": target["label"],
        "metric": "Actual total load",
        "latest_value": round(latest_value, 2),
        "latest_unit": "MW",
        "latest_period": f"Point {latest_position}",
        "average_value": round(average_value, 2),
        "link": ENTSOE_LINK,
    }


def _entsoe_country_name(country_code: str) -> str:
    target = ENTSOE_DOMAINS.get(country_code)
    return target["label"] if target else country_code


def _entsoe_load_metric(country_code: str, process_type: str) -> str:
    token = _entsoe_token()
    target = ENTSOE_DOMAINS.get(country_code)
    if not token or not target:
        return "N/A"

    end_dt = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=1)
    params = {
        "securityToken": token,
        "documentType": "A65",
        "processType": process_type,
        "businessType": "A04",
        "outBiddingZone_Domain": target["code"],
        "periodStart": start_dt.strftime("%Y%m%d%H%M"),
        "periodEnd": end_dt.strftime("%Y%m%d%H%M"),
    }
    try:
        root = ElementTree.fromstring(_fetch_text(f"{ENTSOE_BASE}?{urlencode(params)}"))
    except Exception:
        return "N/A"

    namespaces = _entsoe_namespace(root)
    points = root.findall(".//ns:Point", namespaces) if namespaces else root.findall(".//Point")
    quantities = []
    for point in points:
        node = point.find("ns:quantity", namespaces) if namespaces else point.find("quantity")
        value = _to_float(node.text if node is not None else None)
        if value is not None:
            quantities.append(value)
    if not quantities:
        return "N/A"
    return f"{quantities[-1]:,.0f} MW"


def _entsoe_generation_summary(country_code: str) -> str:
    token = _entsoe_token()
    target = ENTSOE_DOMAINS.get(country_code)
    if not token or not target:
        return "N/A"

    end_dt = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=1)
    params = {
        "securityToken": token,
        "documentType": "A75",
        "processType": "A16",
        "in_Domain": target["code"],
        "periodStart": start_dt.strftime("%Y%m%d%H%M"),
        "periodEnd": end_dt.strftime("%Y%m%d%H%M"),
    }
    try:
        root = ElementTree.fromstring(_fetch_text(f"{ENTSOE_BASE}?{urlencode(params)}"))
    except Exception:
        return "N/A"

    namespaces = _entsoe_namespace(root)
    series = root.findall(".//ns:TimeSeries", namespaces) if namespaces else root.findall(".//TimeSeries")
    best_label = ""
    best_value = None
    for item in series:
        psr_node = item.find(".//ns:psrType", namespaces) if namespaces else item.find(".//psrType")
        label = PSR_TYPE_LABELS.get(psr_node.text if psr_node is not None else "", "Other")
        points = item.findall(".//ns:Point", namespaces) if namespaces else item.findall(".//Point")
        values = []
        for point in points:
            node = point.find("ns:quantity", namespaces) if namespaces else point.find("quantity")
            value = _to_float(node.text if node is not None else None)
            if value is not None:
                values.append(value)
        if not values:
            continue
        latest = values[-1]
        if best_value is None or latest > best_value:
            best_value = latest
            best_label = label
    if best_value is None:
        return "N/A"
    return f"{best_label}: {best_value:,.0f} MW"


def _top_partner(country_code: str, rows: list[dict], candidates: set[str] | None = None) -> str:
    totals: dict[str, float] = {}
    for row in rows:
        if row["country_a"] == country_code:
            partner = row["country_b"]
        elif row["country_b"] == country_code:
            partner = row["country_a"]
        else:
            continue
        if candidates and partner not in candidates:
            continue
        totals[partner] = totals.get(partner, 0.0) + (row.get("capacity_mw") or 0.0)
    if not totals:
        return ""
    return max(totals.items(), key=lambda item: item[1])[0]


def _entsoe_cross_border_summary(country_code: str, rows: list[dict]) -> str:
    token = _entsoe_token()
    target = ENTSOE_DOMAINS.get(country_code)
    partner_code = _top_partner(country_code, rows, set(ENTSOE_DOMAINS))
    partner = ENTSOE_DOMAINS.get(partner_code)
    if not token or not target or not partner:
        return "N/A"

    end_dt = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start_dt = end_dt - timedelta(days=1)
    for from_code, to_code in ((target["code"], partner["code"]), (partner["code"], target["code"])):
        params = {
            "securityToken": token,
            "documentType": "A11",
            "in_Domain": from_code,
            "out_Domain": to_code,
            "periodStart": start_dt.strftime("%Y%m%d%H%M"),
            "periodEnd": end_dt.strftime("%Y%m%d%H%M"),
        }
        try:
            root = ElementTree.fromstring(_fetch_text(f"{ENTSOE_BASE}?{urlencode(params)}"))
        except Exception:
            continue
        namespaces = _entsoe_namespace(root)
        points = root.findall(".//ns:Point", namespaces) if namespaces else root.findall(".//Point")
        values = []
        for point in points:
            node = point.find("ns:quantity", namespaces) if namespaces else point.find("quantity")
            value = _to_float(node.text if node is not None else None)
            if value is not None:
                values.append(value)
        if values:
            return f"{partner['label']}: {values[-1]:,.0f} MW"
    return "N/A"


def _eia_region_metric(region_code: str, type_code: str) -> str:
    if not _eia_api_key():
        return "N/A"
    params = {
        "frequency": "hourly",
        "data[0]": "value",
        "facets[respondent][]": region_code,
        "facets[type][]": type_code,
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "offset": 0,
        "length": 1,
    }
    rows, _ = _eia_fetch_rows(EIA_BASE, params)
    if not rows:
        return "N/A"
    value = _to_float(rows[0].get("value"))
    if value is None:
        return "N/A"
    unit = rows[0].get("value-units", "MW")
    return f"{value:,.0f} {unit}"


def _eia_generation_summary(country_code: str) -> str:
    target = EIA_RESPONDENTS.get(country_code)
    if not _eia_api_key() or not target:
        return "N/A"
    params = {
        "frequency": "hourly",
        "data[0]": "value",
        "facets[respondent][]": target["code"],
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "offset": 0,
        "length": 200,
    }
    rows, _ = _eia_fetch_rows("https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/", params, windows=[(24, 0), (7 * 24, 0), (30 * 24, 0)])
    if not rows:
        return "N/A"
    totals: dict[str, float] = {}
    fuel_map = {
        "COL": "Coal",
        "NG": "Gas",
        "NUC": "Nuclear",
        "SUN": "Solar",
        "WND": "Wind",
        "WAT": "Hydro",
        "OIL": "Oil",
        "OTH": "Other",
        "GEO": "Geothermal",
        "BIO": "Biomass",
    }
    for row in rows:
        fuel = fuel_map.get((row.get("fueltype") or "").upper(), row.get("fueltype") or "Other")
        value = _to_float(row.get("value"))
        if value is not None:
            totals[fuel] = totals.get(fuel, 0.0) + value
    if not totals:
        return "N/A"
    fuel, value = max(totals.items(), key=lambda item: item[1])
    return f"{fuel}: {value:,.0f} MWh"


def _eia_cross_border_summary(country_code: str, rows: list[dict]) -> str:
    target = EIA_RESPONDENTS.get(country_code)
    partner_code = _top_partner(country_code, rows, set(EIA_RESPONDENTS))
    partner = EIA_RESPONDENTS.get(partner_code)
    if not _eia_api_key() or not target or not partner:
        return "N/A"
    params = {
        "frequency": "hourly",
        "data[0]": "value",
        "facets[fromba][]": target["code"],
        "facets[toba][]": partner["code"],
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "offset": 0,
        "length": 1,
    }
    data, _ = _eia_fetch_rows("https://api.eia.gov/v2/electricity/rto/interchange-data/data/", params)
    if not data:
        return "N/A"
    value = _to_float(data[0].get("value"))
    return f"{partner['label']}: {value:,.0f} MW" if value is not None else "N/A"


def _eia_capacity_summary(country_code: str) -> str:
    api_key = _eia_api_key()
    if not api_key or country_code != "USA":
        return "N/A"
    params = {
        "api_key": api_key,
        "frequency": "annual",
        "data[0]": "nameplate-capacity-mw",
        "facets[stateid][]": "US",
        "start": str(datetime.now().year - 1),
        "end": str(datetime.now().year - 1),
    }
    try:
        payload = _fetch_json("https://api.eia.gov/v2/electricity/operating-generator-capacity/data/?" + urlencode(params))
    except Exception:
        return "N/A"
    rows = ((payload.get("response") or {}).get("data") or [])
    if not rows:
        return "N/A"
    total = 0.0
    found = False
    for row in rows:
        value = _to_float(row.get("nameplate-capacity-mw"))
        if value is not None:
            total += value
            found = True
    return f"{total:,.0f} MW" if found else "N/A"


def _owid_generation_by_type(country_code: str) -> str:
    row = _load_owid_rows().get(country_code)
    if not row:
        return "N/A"
    fuels = {
        "Coal": _to_float(row.get("coal_electricity")),
        "Gas": _to_float(row.get("gas_electricity")),
        "Oil": _to_float(row.get("oil_electricity")),
        "Nuclear": _to_float(row.get("nuclear_electricity")),
        "Hydro": _to_float(row.get("hydro_electricity")),
        "Solar": _to_float(row.get("solar_electricity")),
        "Wind": _to_float(row.get("wind_electricity")),
        "Bioenergy": _to_float(row.get("biofuel_electricity")),
    }
    valid = {key: value for key, value in fuels.items() if value is not None}
    if not valid:
        return "N/A"
    fuel, value = max(valid.items(), key=lambda item: item[1])
    return f"{fuel}: {value:,.0f} TWh"


def _owid_net_generation(country_code: str) -> str:
    row = _load_owid_rows().get(country_code)
    if not row:
        return "N/A"
    value = _to_float(row.get("electricity_generation"))
    year = row.get("year") or ""
    return f"{value:,.0f} TWh ({year})" if value is not None else "N/A"


def build_report_metric_rows(country_codes: list[str], rows: list[dict], limit: int = 8) -> list[dict]:
    output = []
    for country_code in country_codes[:limit]:
        provider = _provider_for_country(country_code)
        country_label = _entsoe_country_name(country_code)
        if country_code in EIA_RESPONDENTS:
            country_label = EIA_RESPONDENTS[country_code]["label"]
        elif country_code in _load_owid_rows():
            country_label = _load_owid_rows()[country_code].get("country") or country_code

        metric_row = {
            "country": country_label,
            "provider": provider[0].upper() if provider else "N/A",
            "actual_total_load": "N/A",
            "load_forecasts": "N/A",
            "generation_by_type": "N/A",
            "cross_border_flows": "N/A",
            "hourly_demand": "N/A",
            "demand_forecasts": "N/A",
            "net_generation": "N/A",
            "operating_generator_capacity": "N/A",
        }

        if provider:
            provider_name, code = provider
            if provider_name == "entsoe":
                metric_row["actual_total_load"] = _entsoe_load_metric(code, "A16")
                metric_row["load_forecasts"] = _entsoe_load_metric(code, "A01")
                metric_row["generation_by_type"] = _entsoe_generation_summary(code)
                metric_row["cross_border_flows"] = _entsoe_cross_border_summary(code, rows)
            elif provider_name == "eia":
                region_code = EIA_RESPONDENTS[code]["code"]
                metric_row["hourly_demand"] = _eia_region_metric(region_code, "D")
                metric_row["demand_forecasts"] = _eia_region_metric(region_code, "DF")
                metric_row["net_generation"] = _eia_region_metric(region_code, "NG")
                metric_row["generation_by_type"] = _eia_generation_summary(code)
                metric_row["cross_border_flows"] = _eia_cross_border_summary(code, rows)
                metric_row["operating_generator_capacity"] = _eia_capacity_summary(code)
            elif provider_name == "owid":
                metric_row["generation_by_type"] = _owid_generation_by_type(code)
                metric_row["net_generation"] = _owid_net_generation(code)

        output.append(metric_row)

    return output


def _resolve_target(country: str = "", power_pool: str = "", region: str = "") -> tuple[str, str] | None:
    if country:
        return _provider_for_country(country)

    normalized_pool = power_pool.strip().upper()
    for pool_name, target in POOL_DEFAULTS.items():
        if pool_name.upper() in normalized_pool:
            return target

    if region in REGION_DEFAULTS:
        return REGION_DEFAULTS[region]

    return None


def _provider_for_country(country_code: str) -> tuple[str, str] | None:
    if country_code in EIA_RESPONDENTS:
        return ("eia", country_code)
    if country_code in ENTSOE_DOMAINS:
        return ("entsoe", country_code)
    rows = _load_owid_rows()
    if country_code in rows:
        return ("owid", country_code)
    return None


def _match_reference_pool(pool_name: str) -> str | None:
    normalized = pool_name.strip().upper()
    for key in REFERENCE_SOURCES:
        if key in normalized:
            return key
    return None


def _reference_cards_from_rows(rows: list[dict], power_pool: str = "") -> list[dict]:
    ordered_keys: list[str] = []
    if power_pool:
        match = _match_reference_pool(power_pool)
        if match:
            ordered_keys.append(match)

    seen = set(ordered_keys)
    pool_totals: dict[str, float] = {}
    for row in rows:
        pool = row.get("power_pool", "")
        match = _match_reference_pool(pool)
        if not match:
            continue
        pool_totals[match] = pool_totals.get(match, 0.0) + (row.get("capacity_mw") or 0.0)

    for key, _ in sorted(pool_totals.items(), key=lambda item: item[1], reverse=True):
        if key in seen:
            continue
        seen.add(key)
        ordered_keys.append(key)
        if len(ordered_keys) >= 3:
            break

    return [_reference_card(key) for key in ordered_keys]


def _build_card(provider: str, country_code: str) -> dict:
    if provider == "eia":
        return _eia_query(country_code)
    if provider == "entsoe":
        return _entsoe_query(country_code)
    if provider == "owid":
        return _owid_query(country_code)
    return _unsupported_card("Live Market Snapshot", "No live provider was resolved.")


def build_live_market_context(
    country: str = "",
    power_pool: str = "",
    region: str = "",
    preferred_countries: list[str] | None = None,
    rows: list[dict] | None = None,
) -> dict:
    if country:
        target = _resolve_target(country=country, power_pool=power_pool, region=region)
        if not target:
            reference_cards = _reference_cards_from_rows(rows or [], power_pool=power_pool)
            if reference_cards:
                return {"cards": reference_cards, "available": True}
            cards = [_unsupported_card("Live Market Snapshot", "No live API provider is configured for the selected geography yet.")]
            return {"cards": cards, "available": False}
        provider, country_code = target
        cards = [_build_card(provider, country_code)]
        return {
            "cards": cards,
            "available": any(card.get("status") == "ok" for card in cards),
            "provider": provider,
            "target": country_code,
        }

    supported_targets: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for country_code in preferred_countries or []:
        target = _provider_for_country(country_code)
        if not target or target in seen:
            continue
        seen.add(target)
        supported_targets.append(target)
        if len(supported_targets) >= 3:
            break

    if supported_targets:
        cards = [_build_card(provider, country_code) for provider, country_code in supported_targets]
        return {
            "cards": cards,
            "available": any(card.get("status") == "ok" for card in cards),
            "provider": "mixed",
            "target": ",".join(country_code for _, country_code in supported_targets),
        }

    target = _resolve_target(country=country, power_pool=power_pool, region=region)

    if not target:
        reference_cards = _reference_cards_from_rows(rows or [], power_pool=power_pool)
        if reference_cards:
            return {"cards": reference_cards, "available": True}
        cards = [
            _unsupported_card(
                "Live Market Snapshot",
                "No live API provider is configured for the selected geography yet.",
            )
        ]
        return {"cards": cards, "available": False}

    provider, country_code = target
    cards = [_build_card(provider, country_code)]

    return {
        "cards": cards,
        "available": any(card.get("status") == "ok" for card in cards),
        "provider": provider,
        "target": country_code,
    }
