from __future__ import annotations

import csv
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from binascii import unhexlify

from shapely import wkb

from live_market import build_entsoe_corridor_flow_context


DATASET_PATH = Path(__file__).resolve().parent / "datasets" / "interconnections.csv"

ISO3_NAMES = {
    "USA": "United States", "CAN": "Canada", "MEX": "Mexico",
    "DEU": "Germany", "FRA": "France", "GBR": "United Kingdom",
    "ESP": "Spain", "ITA": "Italy", "POL": "Poland", "ROU": "Romania",
    "NOR": "Norway", "SWE": "Sweden", "FIN": "Finland", "DNK": "Denmark",
    "NLD": "Netherlands", "BEL": "Belgium", "CHE": "Switzerland",
    "AUT": "Austria", "CZE": "Czech Republic", "SVK": "Slovakia",
    "HUN": "Hungary", "HRV": "Croatia", "SRB": "Serbia",
    "BGR": "Bulgaria", "GRC": "Greece", "TUR": "Türkiye",
    "RUS": "Russia", "UKR": "Ukraine", "BLR": "Belarus",
    "LTU": "Lithuania", "LVA": "Latvia", "EST": "Estonia",
    "KAZ": "Kazakhstan", "AZE": "Azerbaijan", "GEO": "Georgia",
    "ARM": "Armenia", "PRT": "Portugal", "IRL": "Ireland",
    "ZAF": "South Africa", "ZWE": "Zimbabwe", "ZMB": "Zambia",
    "MOZ": "Mozambique", "NAM": "Namibia", "BWA": "Botswana",
    "MWI": "Malawi", "TZA": "Tanzania", "COD": "DR Congo",
    "AGO": "Angola", "SWZ": "Eswatini", "LSO": "Lesotho",
    "NGA": "Nigeria", "GHA": "Ghana", "CIV": "Cote d'Ivoire",
    "SEN": "Senegal", "MLI": "Mali", "BFA": "Burkina Faso",
    "NER": "Niger", "BEN": "Benin", "TGO": "Togo",
    "GIN": "Guinea", "SLE": "Sierra Leone", "LBR": "Liberia",
    "GMB": "Gambia", "GNB": "Guinea-Bissau", "MRT": "Mauritania",
    "ETH": "Ethiopia", "KEN": "Kenya", "UGA": "Uganda",
    "RWA": "Rwanda", "BDI": "Burundi", "SDN": "Sudan", "SSD": "South Sudan",
    "THA": "Thailand", "MYS": "Malaysia", "IDN": "Indonesia",
    "VNM": "Vietnam", "PHL": "Philippines", "SGP": "Singapore",
    "MMR": "Myanmar", "KHM": "Cambodia", "LAO": "Laos",
    "BRN": "Brunei", "IND": "India", "BGD": "Bangladesh",
    "NPL": "Nepal", "BTN": "Bhutan", "LKA": "Sri Lanka",
    "CHN": "China", "JPN": "Japan", "KOR": "South Korea", "MNG": "Mongolia",
    "ARG": "Argentina", "BRA": "Brazil", "CHL": "Chile",
    "COL": "Colombia", "PER": "Peru", "VEN": "Venezuela",
    "ECU": "Ecuador", "BOL": "Bolivia", "PRY": "Paraguay",
    "URY": "Uruguay", "PAN": "Panama", "CRI": "Costa Rica",
    "GTM": "Guatemala", "HND": "Honduras", "NIC": "Nicaragua",
    "SLV": "El Salvador", "UZB": "Uzbekistan", "KGZ": "Kyrgyzstan",
    "TJK": "Tajikistan", "TKM": "Turkmenistan", "AFG": "Afghanistan",
    "SAU": "Saudi Arabia", "ARE": "United Arab Emirates", "EGY": "Egypt",
    "IRN": "Iran", "IRQ": "Iraq", "JOR": "Jordan", "ISR": "Israel",
    "LBN": "Lebanon", "SYR": "Syria", "TUN": "Tunisia", "MAR": "Morocco",
    "DZA": "Algeria", "LBY": "Libya", "KWT": "Kuwait", "QAT": "Qatar",
    "BHR": "Bahrain", "OMN": "Oman", "YEM": "Yemen", "PAK": "Pakistan",
}

CENTROIDS = {
    "ALB": (41.153, 20.168), "AND": (42.547, 1.602), "AUT": (47.516, 14.550),
    "BLR": (53.710, 27.953), "BEL": (50.503, 4.469), "BIH": (43.916, 17.679),
    "BGR": (42.734, 25.486), "HRV": (45.100, 15.201), "CYP": (35.126, 33.430),
    "CZE": (49.818, 15.473), "DNK": (56.263, 9.502), "EST": (58.596, 25.014),
    "FIN": (61.924, 25.748), "FRA": (46.228, 2.214), "DEU": (51.166, 10.452),
    "GRC": (39.075, 21.824), "HUN": (47.163, 19.504), "ISL": (64.963, -19.021),
    "IRL": (53.413, -8.244), "ITA": (41.872, 12.567), "LVA": (56.880, 24.604),
    "LTU": (55.170, 23.881), "LUX": (49.816, 6.130), "MDA": (47.412, 28.370),
    "MNE": (42.708, 19.374), "NLD": (52.133, 5.291), "MKD": (41.609, 21.746),
    "NOR": (60.472, 8.469), "POL": (51.920, 19.145), "PRT": (39.400, -8.225),
    "ROU": (45.943, 24.967), "RUS": (61.524, 105.319), "SRB": (44.017, 21.006),
    "SVK": (48.669, 19.699), "SVN": (46.151, 14.996), "ESP": (40.464, -3.750),
    "SWE": (60.128, 18.644), "CHE": (46.818, 8.228), "TUR": (38.964, 35.243),
    "UKR": (48.379, 31.166), "GBR": (55.378, -3.436), "ARM": (40.069, 45.039),
    "AZE": (40.143, 47.577), "BHR": (26.006, 50.548), "GEO": (42.315, 43.357),
    "IRN": (32.427, 53.688), "IRQ": (33.224, 43.679), "ISR": (31.047, 34.852),
    "JOR": (30.585, 36.239), "KAZ": (48.020, 66.924), "KWT": (29.313, 47.482),
    "KGZ": (41.204, 74.767), "LBN": (33.855, 35.862), "OMN": (21.513, 55.923),
    "PAK": (30.375, 69.346), "QAT": (25.355, 51.184), "SAU": (23.886, 45.079),
    "SYR": (34.802, 38.997), "TJK": (38.861, 71.276), "TKM": (38.970, 59.557),
    "ARE": (23.425, 53.848), "UZB": (41.377, 64.586), "YEM": (15.553, 48.516),
    "AFG": (33.939, 67.710), "BGD": (23.685, 90.357), "BTN": (27.515, 90.434),
    "IND": (20.594, 78.963), "MMR": (19.165, 95.956), "NPL": (28.394, 84.124),
    "LKA": (7.873, 80.772), "BRN": (4.536, 114.728), "KHM": (12.566, 104.991),
    "CHN": (35.862, 104.195), "IDN": (-0.789, 113.921), "JPN": (36.205, 138.253),
    "LAO": (19.857, 102.496), "MYS": (4.211, 101.976), "MNG": (46.863, 103.847),
    "PHL": (12.880, 121.774), "SGP": (1.353, 103.820), "KOR": (35.908, 127.767),
    "THA": (15.870, 100.993), "VNM": (14.059, 108.278), "DZA": (28.034, 1.660),
    "AGO": (-11.203, 17.874), "BEN": (9.308, 2.316), "BWA": (-22.329, 24.685),
    "BFA": (12.364, -1.535), "BDI": (-3.374, 29.919), "CMR": (3.848, 11.502),
    "CAF": (6.612, 20.940), "TCD": (15.455, 18.733), "COD": (-4.038, 21.759),
    "COG": (-0.228, 15.827), "DJI": (11.826, 42.590), "EGY": (26.821, 30.802),
    "ERI": (15.180, 39.782), "SWZ": (-26.522, 31.466), "ETH": (9.145, 40.490),
    "GAB": (-0.804, 11.610), "GMB": (13.444, -15.311), "GHA": (7.947, -1.024),
    "GIN": (9.946, -9.697), "GNB": (11.804, -15.180), "CIV": (7.540, -5.547),
    "KEN": (-0.024, 37.907), "LSO": (-29.610, 28.234), "LBR": (6.428, -9.430),
    "LBY": (26.336, 17.229), "MDG": (-18.767, 46.869), "MWI": (-13.255, 34.302),
    "MLI": (17.571, -3.996), "MRT": (21.009, -10.941), "MAR": (31.792, -7.093),
    "MOZ": (-18.666, 35.530), "NAM": (-22.959, 18.490), "NER": (17.608, 8.082),
    "NGA": (9.082, 8.676), "RWA": (-1.940, 29.874), "SEN": (14.497, -14.452),
    "SLE": (8.461, -11.780), "SOM": (5.152, 46.200), "ZAF": (-30.560, 22.938),
    "SSD": (6.877, 31.307), "SDN": (12.863, 30.218), "TZA": (-6.369, 34.889),
    "TGO": (8.620, 0.825), "TUN": (33.887, 9.538), "UGA": (1.374, 32.290),
    "ZMB": (-13.134, 27.849), "ZWE": (-19.016, 29.155), "ARG": (-38.416, -63.617),
    "BOL": (-16.291, -63.589), "BRA": (-14.235, -51.926), "CAN": (56.131, -106.347),
    "CHL": (-35.676, -71.543), "COL": (4.571, -74.298), "CRI": (9.748, -83.753),
    "ECU": (-1.832, -78.184), "SLV": (13.794, -88.897), "GTM": (15.784, -90.231),
    "GUY": (4.861, -58.930), "HND": (15.200, -86.242), "MEX": (23.635, -102.553),
    "NIC": (12.866, -85.208), "PAN": (8.538, -80.783), "PRY": (-23.443, -58.444),
    "PER": (-9.190, -75.015), "SUR": (3.920, -56.028), "USA": (37.090, -95.713),
    "URY": (-32.523, -55.766), "VEN": (6.424, -66.590), "AUS": (-25.275, 133.776),
    "NZL": (-40.901, 174.886),
}

STATUS_LABELS = {
    "operational": "Operational",
    "planned": "Planned",
    "construction": "Under Construction",
    "under construction": "Under Construction",
    "underconstruction": "Under Construction",
    "maintenance": "Maintenance",
    "decommissioned": "Decommissioned",
    "disconnected": "Decommissioned",
}


def _to_float(value: str | None) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: str | None) -> int | None:
    number = _to_float(value)
    return int(number) if number is not None else None


def _clean_text(value: str | None, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def country_name(iso3: str) -> str:
    return ISO3_NAMES.get(iso3, iso3)


def _is_close_point(point: list[float], centroid: tuple[float, float], tolerance: float = 1.5) -> bool:
    lon, lat = point[0], point[1]
    centroid_lat, centroid_lon = centroid
    return abs(lon - centroid_lon) <= tolerance and abs(lat - centroid_lat) <= tolerance


def _parse_geom_points(value: str | None) -> list[list[float]]:
    if not value:
        return []
    geom_hex = str(value).strip()
    if geom_hex.startswith("\\x"):
        geom_hex = geom_hex[2:]
    try:
        geom = wkb.loads(unhexlify(geom_hex))
    except Exception:
        return []

    if geom.geom_type == "LineString":
        return [[float(lon), float(lat)] for lon, lat in geom.coords]
    if geom.geom_type == "MultiLineString":
        points: list[list[float]] = []
        for line in geom.geoms:
            points.extend([[float(lon), float(lat)] for lon, lat in line.coords])
        return points
    if geom.geom_type == "Point":
        return [[float(geom.x), float(geom.y)]]
    return []


@lru_cache(maxsize=1)
def load_interconnections() -> list[dict]:
    rows: list[dict] = []
    with DATASET_PATH.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            country_a = _clean_text(raw.get("country_a"))
            country_b = _clean_text(raw.get("country_b"))
            status_key = _clean_text(raw.get("status")).lower()
            rows.append(
                {
                    "id": _clean_text(raw.get("id")),
                    "country_a": country_a,
                    "country_b": country_b,
                    "country_a_name": country_name(country_a),
                    "country_b_name": country_name(country_b),
                    "status": STATUS_LABELS.get(status_key, status_key.title() or "Unknown"),
                    "ac_dc": _clean_text(raw.get("ac_dc"), "Unknown").upper(),
                    "power_pool": _clean_text(raw.get("power_pool"), "Unknown"),
                    "name": _clean_text(raw.get("name"), f"{country_a} - {country_b}"),
                    "operator": _clean_text(raw.get("operator"), "Unknown"),
                    "mode": _clean_text(raw.get("mode"), "Unknown"),
                    "voltage_class": _clean_text(raw.get("voltage_class"), "Unknown"),
                    "length_km": _to_float(raw.get("length_km")),
                    "voltage_kv": _to_float(raw.get("voltage_kv")),
                    "capacity_mw": _to_float(raw.get("capacity_mw")),
                    "commissioned_year": _to_int(raw.get("commissioned_year")),
                    "trade_flow_mwh": _to_float(raw.get("trade_flow_mwh")),
                    "source_dataset": _clean_text(raw.get("source_dataset"), "Unknown"),
                    "confidence_level": _clean_text(raw.get("confidence_level"), "Unknown"),
                    "route_points": _parse_geom_points(raw.get("geom")),
                    "geom_kind": _clean_text(raw.get("geom"))[:16],
                }
            )
    return rows


def filter_interconnections(
    country: str = "",
    status: str = "",
    power_pool: str = "",
    query: str = "",
) -> list[dict]:
    query_text = query.strip().casefold()
    filtered: list[dict] = []
    for row in load_interconnections():
        if country and country not in (row["country_a"], row["country_b"]):
            continue
        if status and row["status"] != status:
            continue
        if power_pool and row["power_pool"] != power_pool:
            continue
        if query_text:
            haystack = " ".join(
                [
                    row["name"],
                    row["country_a_name"],
                    row["country_b_name"],
                    row["power_pool"],
                    row["operator"],
                ]
            ).casefold()
            if query_text not in haystack:
                continue
        filtered.append(row)
    return filtered


def rows_for_country_codes(country_codes: set[str] | None = None) -> list[dict]:
    rows = load_interconnections()
    if not country_codes:
        return rows
    return [
        row for row in rows
        if row["country_a"] in country_codes or row["country_b"] in country_codes
    ]


def get_filter_options() -> dict:
    rows = load_interconnections()
    countries = sorted({row["country_a"] for row in rows} | {row["country_b"] for row in rows})
    return {
        "countries": [{"code": code, "name": country_name(code)} for code in countries],
        "statuses": sorted({row["status"] for row in rows}),
        "power_pools": sorted({row["power_pool"] for row in rows}),
    }


def _country_counts(rows: list[dict]) -> list[dict]:
    counts: Counter[str] = Counter()
    for row in rows:
        counts[row["country_a"]] += 1
        counts[row["country_b"]] += 1
    top = counts.most_common(12)
    return [{"country": country_name(code), "count": count} for code, count in top]


def top_country_codes_by_capacity(rows: list[dict], limit: int = 8) -> list[str]:
    totals: defaultdict[str, float] = defaultdict(float)
    counts: Counter[str] = Counter()
    for row in rows:
        value = row["capacity_mw"] or 0
        totals[row["country_a"]] += value
        totals[row["country_b"]] += value
        counts[row["country_a"]] += 1
        counts[row["country_b"]] += 1
    ordered = sorted(
        totals.items(),
        key=lambda item: (item[1], counts[item[0]]),
        reverse=True,
    )
    return [code for code, _ in ordered[:limit]]


def _power_pool_counts(rows: list[dict]) -> list[dict]:
    counts = Counter(row["power_pool"] for row in rows)
    return [{"pool": pool, "count": count} for pool, count in counts.most_common(12)]


def _capacity_by_status(rows: list[dict]) -> list[dict]:
    totals: defaultdict[str, float] = defaultdict(float)
    for row in rows:
        if row["capacity_mw"] is not None:
            totals[row["status"]] += row["capacity_mw"]
    return [{"status": status, "capacity_mw": round(value, 2)} for status, value in totals.items()]


def _corridor_summary(rows: list[dict]) -> list[dict]:
    corridors: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = tuple(sorted((row["country_a"], row["country_b"])))
        item = corridors.setdefault(
            key,
            {
                "country_a": key[0],
                "country_b": key[1],
                "connections": 0,
                "capacity_mw": 0.0,
                "planned": 0,
                "operational": 0,
            },
        )
        item["connections"] += 1
        if row["capacity_mw"] is not None:
            item["capacity_mw"] += row["capacity_mw"]
        if row["status"] == "Operational":
            item["operational"] += 1
        if row["status"] == "Planned":
            item["planned"] += 1
    results = []
    for item in corridors.values():
        item["label"] = f"{country_name(item['country_a'])} ↔ {country_name(item['country_b'])}"
        item["capacity_mw"] = round(item["capacity_mw"], 2)
        results.append(item)
    results.sort(key=lambda item: (item["capacity_mw"], item["connections"]), reverse=True)
    return results[:20]


def build_infrastructure_context(
    country: str = "",
    status: str = "",
    power_pool: str = "",
    query: str = "",
    map_mode: str = "separate",
) -> dict:
    rows = filter_interconnections(country=country, status=status, power_pool=power_pool, query=query)
    total_capacity = sum(row["capacity_mw"] or 0 for row in rows)
    operational_count = sum(1 for row in rows if row["status"] == "Operational")
    country_codes = sorted({row["country_a"] for row in rows} | {row["country_b"] for row in rows})

    pair_members: defaultdict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        pair_key = tuple(sorted((row["country_a"], row["country_b"])))
        pair_members[pair_key].append(row)
    live_flows = build_entsoe_corridor_flow_context(rows)

    line_features = []
    marker_counts: Counter[str] = Counter()
    for pair_key, pair_rows in pair_members.items():
        total_for_pair = len(pair_rows)
        ordered_rows = sorted(
            pair_rows,
            key=lambda row: (
                row["commissioned_year"] or 0,
                row["capacity_mw"] or 0,
                row["name"],
            ),
            reverse=True,
        )
        if map_mode == "consolidated":
            start_code, end_code = pair_key
            start = CENTROIDS.get(start_code)
            end = CENTROIDS.get(end_code)
            capacity_total = sum(row["capacity_mw"] or 0 for row in ordered_rows)
            status_counts = Counter(row["status"] for row in ordered_rows)
            dominant_status = status_counts.most_common(1)[0][0] if status_counts else "Unknown"
            if start and end:
                feature = {
                    "from": country_name(start_code),
                    "to": country_name(end_code),
                    "lat": [start[0], end[0]],
                    "lon": [start[1], end[1]],
                    "status": dominant_status,
                    "capacity_mw": round(capacity_total, 2),
                    "label": f"{country_name(start_code)} ↔ {country_name(end_code)}",
                    "route_index": 0,
                    "route_total": 1,
                }
                if pair_key in live_flows:
                    feature["live_flow"] = live_flows[pair_key]
                line_features.append(feature)
                marker_counts[start_code] += total_for_pair
                marker_counts[end_code] += total_for_pair
            continue
        for index, row in enumerate(ordered_rows):
            start = CENTROIDS.get(row["country_a"])
            end = CENTROIDS.get(row["country_b"])
            route_points = row["route_points"]
            is_centroid_like = False
            if len(route_points) == 2 and start and end:
                is_centroid_like = _is_close_point(route_points[0], start) and _is_close_point(route_points[1], end)
            if route_points:
                feature = {
                    "from": row["country_a_name"],
                    "to": row["country_b_name"],
                    "lat": [point[1] for point in route_points],
                    "lon": [point[0] for point in route_points],
                    "status": row["status"],
                    "capacity_mw": row["capacity_mw"] or 0,
                    "label": row["name"],
                    "route_index": index,
                    "route_total": total_for_pair,
                    "curve_hint": is_centroid_like,
                }
                if pair_key in live_flows:
                    feature["live_flow"] = live_flows[pair_key]
                line_features.append(feature)
                marker_counts[row["country_a"]] += 1
                marker_counts[row["country_b"]] += 1
            elif start and end:
                feature = {
                    "from": row["country_a_name"],
                    "to": row["country_b_name"],
                    "lat": [start[0], end[0]],
                    "lon": [start[1], end[1]],
                    "status": row["status"],
                    "capacity_mw": row["capacity_mw"] or 0,
                    "label": row["name"],
                    "route_index": index,
                    "route_total": total_for_pair,
                    "curve_hint": True,
                }
                if pair_key in live_flows:
                    feature["live_flow"] = live_flows[pair_key]
                line_features.append(feature)
                marker_counts[row["country_a"]] += 1
                marker_counts[row["country_b"]] += 1

    markers = []
    for code, count in marker_counts.items():
        lat_lon = CENTROIDS.get(code)
        if not lat_lon:
            continue
        markers.append(
            {
                "country": country_name(code),
                "code": code,
                "lat": lat_lon[0],
                "lon": lat_lon[1],
                "count": count,
            }
        )

    return {
        "rows": sorted(rows, key=lambda row: ((row["commissioned_year"] or 0), row["name"]), reverse=True),
        "map_mode": map_mode,
        "kpis": {
            "interconnections": len(rows),
            "countries": len(country_codes),
            "capacity_mw": round(total_capacity, 2),
            "operational_share": round((operational_count / len(rows) * 100), 1) if rows else 0,
        },
        "line_features": line_features,
        "live_flow_count": sum(1 for item in live_flows.values() if item.get("status") == "ok"),
        "live_flow_total": len(live_flows),
        "markers": markers,
        "country_counts": _country_counts(rows),
        "power_pool_counts": _power_pool_counts(rows),
        "capacity_by_status": _capacity_by_status(rows),
    }


def build_market_context(country: str = "", power_pool: str = "") -> dict:
    rows = filter_interconnections(country=country, power_pool=power_pool)
    corridors = _corridor_summary(rows)
    by_voltage = Counter(row["voltage_class"] for row in rows)
    by_mode = Counter(row["mode"] for row in rows)
    status_counts = Counter(row["status"] or "Unknown" for row in rows)
    power_pool_counts = Counter(row["power_pool"] or "Unknown" for row in rows)
    avg_capacity = round(
        sum(row["capacity_mw"] or 0 for row in rows if row["capacity_mw"] is not None)
        / max(1, sum(1 for row in rows if row["capacity_mw"] is not None)),
        2,
    )
    total_capacity = sum(row["capacity_mw"] or 0 for row in rows)
    return {
        "rows": rows,
        "dataset_path": str(DATASET_PATH),
        "kpis": {
            "corridors": len(corridors),
            "known_capacity_assets": sum(1 for row in rows if row["capacity_mw"] is not None),
            "known_capacity_mw": round(total_capacity, 2),
            "average_capacity_mw": avg_capacity,
            "planned_assets": sum(1 for row in rows if row["status"] == "Planned"),
        },
        "corridors": corridors,
        "voltage_breakdown": [{"label": label, "count": count} for label, count in by_voltage.most_common()],
        "mode_breakdown": [{"label": label, "count": count} for label, count in by_mode.most_common()],
        "status_breakdown": [{"label": label, "count": count} for label, count in status_counts.most_common()],
        "power_pool_breakdown": [{"label": label, "count": count} for label, count in power_pool_counts.most_common()],
        "country_counts": _country_counts(rows),
    }


def build_report_asset_context(country_codes: set[str] | None = None) -> dict:
    rows = rows_for_country_codes(country_codes)
    total_capacity = sum(row["capacity_mw"] or 0 for row in rows)
    operational_rows = sum(1 for row in rows if row["status"] == "Operational")
    known_capacity_assets = sum(1 for row in rows if row["capacity_mw"] is not None)

    return {
        "kpis": {
            "interconnections": len(rows),
            "countries": len({row["country_a"] for row in rows} | {row["country_b"] for row in rows}),
            "known_capacity_mw": round(total_capacity, 2),
            "operational_share": round((operational_rows / len(rows) * 100), 1) if rows else 0,
            "known_capacity_assets": known_capacity_assets,
        },
        "top_corridors": _corridor_summary(rows)[:8],
        "top_countries": _country_counts(rows)[:6],
        "capacity_by_status": _capacity_by_status(rows),
        "power_pools": _power_pool_counts(rows)[:6],
        "rows": sorted(rows, key=lambda row: ((row["commissioned_year"] or 0), row["name"]), reverse=True)[:8],
    }
