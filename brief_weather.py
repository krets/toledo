#!/usr/bin/env python3
"""Berlin weather: previous 24 hours vs next 24 hours, from Prometheus (yr.no exporter).

Vendored from the standalone morning-brief project (github.com/krets/morning) into Toledo.
Observed conditions are the hours=0 series of the yr.no exporter, read back from Prometheus
history; the forecast is hours=0..23 of the latest sample.

Env: PROMETHEUS_URL (default http://underlord.krets.com:9090)
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PROMETHEUS_URL = "http://underlord.krets.com:9090"
UA = "Mozilla/5.0 (X11; Linux x86_64) weather-collector/0.1 (personal use)"

# normalized field -> yr.no exporter metric name
METRICS = {
    "temp_c": "yrno_air_temperature",
    "apparent_temp_c": "yrno_apparent_air_temperature",
    "humidity_pct": "yrno_relative_humidity",
    "wind_speed_ms": "yrno_wind_speed",
    "wind_dir_deg": "yrno_wind_from_direction",
    "cloud_pct": "yrno_cloud_area_fraction",
    "precip_mm": "yrno_precipitation_amount",
    "pressure_hpa": "yrno_air_pressure_at_sea_level",
}
# Compass angles cannot be min/max/averaged meaningfully: hourly rows only, no summary.
NO_SUMMARY = {"wind_dir_deg"}
FORECAST_HOURS = r"[0-9]|1[0-9]|2[0-3]"


def log(msg):
    print(msg, file=sys.stderr)


def make_session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    retry = Retry(total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=None, respect_retry_after_header=True)
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def prom_query(sess, base_url, query):
    r = sess.get(f"{base_url}/api/v1/query", params={"query": query}, timeout=30)
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"prometheus query failed: {json.dumps(body)[:500]}")
    return body["data"]["result"]


def to_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def scalar(sess, base_url, query):
    """Single-value query. An empty result means the data is missing, which must not pass silently."""
    result = prom_query(sess, base_url, query)
    if not result:
        raise RuntimeError(f"no data for query: {query}")
    return round(to_num(result[0]["value"][1]), 2)


def stat_names(field):
    return ("sum", "max") if field == "precip_mm" else ("min", "max", "avg")


def previous_24h(sess, base_url, location):
    """min/max/avg (precip: sum/max) of the hours=0 series, hourly samples over the last 24h."""
    summary = {}
    for field, metric in METRICS.items():
        if field in NO_SUMMARY:
            continue
        sel = f'{metric}{{location="{location}",hours="0"}}'
        summary[field] = {s: scalar(sess, base_url, f"{s}_over_time({sel}[24h:1h])") for s in stat_names(field)}
    return summary


def next_24h(sess, base_url, location, tz):
    """Latest forecast, hours=0..23: aggregates across those series, plus the hourly rows."""
    summary, hourly = {}, {}
    now = datetime.now(tz).replace(minute=0, second=0, microsecond=0)
    for field, metric in METRICS.items():
        sel = f'{metric}{{location="{location}",hours=~"{FORECAST_HOURS}"}}'
        if field not in NO_SUMMARY:
            summary[field] = {s: scalar(sess, base_url, f"{s}({sel})") for s in stat_names(field)}
        for row in prom_query(sess, base_url, sel):
            h = int(float(row["metric"]["hours"]))
            hourly.setdefault(h, {"time": (now + timedelta(hours=h)).isoformat(), "hours_ahead": h})[field] = \
                to_num(row["value"][1])
    return summary, [hourly[h] for h in sorted(hourly)]


def main(argv=None, session=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prometheus-url", default=None, help="default: $PROMETHEUS_URL or the built-in default")
    p.add_argument("--location", default="Berlin")
    p.add_argument("--timezone", default="Europe/Berlin")
    p.add_argument("--out", default="-", help="JSON path, '-' for stdout")
    args = p.parse_args(argv)

    base_url = args.prometheus_url or os.environ.get("PROMETHEUS_URL", PROMETHEUS_URL)
    tz = ZoneInfo(args.timezone)
    sess = session or make_session()
    now = datetime.now(tz)

    out = {
        "location": args.location,
        "timezone": args.timezone,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    status = {}
    try:
        out["previous_24h"] = {"start": (now - timedelta(hours=24)).isoformat(timespec="minutes"),
                               "end": now.isoformat(timespec="minutes"), "source": "yrno_hours0",
                               "summary": previous_24h(sess, base_url, args.location)}
        status["previous_24h"] = {"ok": True}
    except Exception as exc:
        status["previous_24h"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        summary, hourly = next_24h(sess, base_url, args.location, tz)
        out["next_24h"] = {"start": now.replace(minute=0, second=0, microsecond=0).isoformat(timespec="minutes"),
                           "source": "yrno_forecast", "summary": summary, "hourly": hourly}
        status["next_24h"] = {"ok": True, "hours": len(hourly)}
    except Exception as exc:
        status["next_24h"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    fh = sys.stdout if args.out == "-" else open(args.out, "w", encoding="utf-8")
    json.dump(out, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
    if fh is not sys.stdout:
        fh.close()
    log(json.dumps({"run_at": out["generated_at"], "sources": status}))
    return 0 if all(s["ok"] for s in status.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
