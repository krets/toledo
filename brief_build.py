#!/usr/bin/env python3
"""Run every collector and write one Markdown context file for a morning summary.

Vendored from the standalone morning-brief project (github.com/krets/morning) into Toledo,
as brief_build.py (the original file was named brief.py).

    python brief_build.py                 # writes context.md (raw JSON goes to out/)
    python brief_build.py --skip-events    # fast run (events scrape takes a few minutes)
    python brief_build.py --from-raw       # re-render context.md from out/ without collecting

Sections: calendar (brief_gcal.py), weather (brief_weather.py), nearby tech events (brief_collect.py).
A failed source is reported inside the file; the rest still render.
"""
import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import brief_collect as collect
import brief_gcal as gcal
import brief_state as state
import brief_weather as weather

HERE = os.path.dirname(os.path.abspath(__file__))


def run(fn, argv):
    """Call a collector's main(); echo its stderr live and return (rc, last JSON summary line)."""
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = fn(argv)
    text = buf.getvalue()
    sys.stderr.write(text)
    summary = {}
    for line in reversed(text.strip().splitlines()):
        if line.startswith("{"):
            summary = json.loads(line).get("sources", {})
            break
    return rc, summary


def fmt_day(d):
    return d.strftime("%A, %B %d").replace(" 0", " ")


def fmt_time(iso):
    return datetime.fromisoformat(iso).strftime("%H:%M")


def one_line(s, n=200):
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ---------- sections ----------

def calendar_section(data, status, tz):
    lines = ["## Calendar", ""]
    if data is None:
        return lines + [f"Unavailable: {status}", ""]
    start = datetime.fromisoformat(data["window_start"])
    end = datetime.fromisoformat(data["window_end"])
    by_day = defaultdict(list)
    for e in data["events"]:
        by_day[datetime.fromisoformat(e["start"]).date()].append(e)
    day = start.date()
    while day < end.date():
        label = "Today" if day == start.date() else ("Tomorrow" if (day - start.date()).days == 1 else fmt_day(day))
        lines.append(f"### {label} ({fmt_day(day)})" if label in ("Today", "Tomorrow") else f"### {label}")
        evs = by_day.get(day, [])
        if not evs:
            lines.append("Nothing scheduled.")
        for e in sorted(evs, key=lambda e: (not e["all_day"], e["start"])):
            when = "all day" if e["all_day"] else f"{fmt_time(e['start'])}" + (f"–{fmt_time(e['end'])}" if e["end"] else "")
            row = f"- **{when}** {e['title']} _({e['calendar']})_"
            if e["location"]:
                row += f" — {one_line(e['location'], 80)}"
            lines.append(row)
            if e["description"]:
                lines.append(f"  - {one_line(e['description'])}")
        lines.append("")
        day += timedelta(days=1)
    return lines


def fmt(v, unit="", nd=1):
    return "—" if v is None else f"{v:.{nd}f}{unit}"


def weather_section(data, status):
    lines = [f"## Weather — {data['location'] if data else 'Berlin'}", ""]
    if data is None or "previous_24h" not in data or "next_24h" not in data:
        return lines + [f"Unavailable or partial: {status}", ""]
    prev, nxt = data["previous_24h"]["summary"], data["next_24h"]["summary"]
    rows = [  # label, stat group, stat, decimals
        ("Temperature low (°C)", "temp_c", "min", 1),
        ("Temperature high (°C)", "temp_c", "max", 1),
        ("Temperature avg (°C)", "temp_c", "avg", 1),
        ("Precipitation total (mm)", "precip_mm", "sum", 1),
        ("Wind avg (m/s)", "wind_speed_ms", "avg", 1),
        ("Wind max (m/s)", "wind_speed_ms", "max", 1),
        ("Cloud cover avg (%)", "cloud_pct", "avg", 0),
        ("Humidity avg (%)", "humidity_pct", "avg", 0),
        ("Pressure avg (hPa)", "pressure_hpa", "avg", 0),
    ]
    lines += ["| | Previous 24 h | Next 24 h | Change |", "|---|---|---|---|"]
    for label, group, stat, nd in rows:
        a, b = prev[group][stat], nxt[group][stat]
        lines.append(f"| {label} | {fmt(a, nd=nd)} | {fmt(b, nd=nd)} | {round(b - a, nd) + 0:+.{nd}f} |")
    lines += ["", "Previous 24 h: observed conditions (yr.no hours=0). Next 24 h: latest forecast (hours 0–23).", ""]

    lines += ["| Time | °C | Precip mm | Cloud % | Wind m/s |", "|---|---|---|---|---|"]
    for h in data["next_24h"]["hourly"]:
        if h["hours_ahead"] % 3 == 0:
            when = datetime.fromisoformat(h["time"])
            label = when.strftime("%a %H:%M")
            lines.append(f"| {label} | {fmt(h.get('temp_c'))} | {fmt(h.get('precip_mm'))} | "
                         f"{fmt(h.get('cloud_pct'), nd=0)} | {fmt(h.get('wind_speed_ms'))} |")
    return lines + [""]


def events_section(rows, status, days):
    lines = [f"## Tech events near Berlin (next {days} days)", ""]
    if rows is None:
        return lines + [f"Unavailable: {status}", ""]
    if not rows:
        return lines + ["No matching events found.", ""]
    by_day = defaultdict(list)
    for e in rows:
        by_day[datetime.fromisoformat(e["start"]).date()].append(e)
    for day in sorted(by_day):
        lines.append(f"### {fmt_day(day)}")
        for e in by_day[day]:
            loc = e["location"]
            where = loc["name"] or loc["address"] or loc["city"] or ""
            lines.append(f"- **{fmt_time(e['start'])}** [{e['title']}]({e['url']}) _({e['source']})_"
                         + (" **[updated]**" if e.get("_updated") else "") + (f" — {where}" if where else ""))
            lines.append(f"  - id: `{e['source_event_id']}` · matched: {' '.join(f'`{k}`' for k in e['matched_queries'])}")
        lines.append("")
    return lines


def status_section(statuses):
    lines = ["## Data sources", ""]
    for group, st in statuses.items():
        for name, s in st.items():
            ok = "ok" if s.get("ok") else f"FAILED ({s.get('error', 'unknown')})"
            extra = ", ".join(f"{k}={v}" for k, v in s.items() if k not in ("ok", "error"))
            lines.append(f"- {group}/{name}: {ok}" + (f" ({extra})" if extra else ""))
    return lines + [""]


# ---------- main ----------

def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default=os.path.join(HERE, "context.md"))
    p.add_argument("--raw-dir", default=os.path.join(HERE, "out"), help="where raw collector JSON is kept")
    p.add_argument("--timezone", default="Europe/Berlin")
    p.add_argument("--calendar-days", type=int, default=4, help="today plus the next N-1 days")
    p.add_argument("--event-days", type=int, default=90, help="how far ahead to look for events")
    p.add_argument("--skip-events", action="store_true", help="skip the slow events scrape")
    p.add_argument("--skip-weather", action="store_true")
    p.add_argument("--skip-calendar", action="store_true")
    p.add_argument("--db", help="SQLite state file: keywords and calendar feeds come from it, and events are "
                                "synced to it and filtered by mute/dismiss status")
    p.add_argument("--run-id", type=int, help="with --db: record each source's result against this run")
    p.add_argument("--from-raw", action="store_true",
                   help="re-render from the saved raw files in --raw-dir without running any collector")
    args = p.parse_args(argv)

    tz = ZoneInfo(args.timezone)
    os.makedirs(args.raw_dir, exist_ok=True)
    statuses, sections_data, errors = {}, {}, {}

    def attempt(name, fn, cli, path, loader):
        try:
            if args.from_raw:
                statuses[name] = {}
                sections_data[name] = loader(path) if os.path.exists(path) else None
                if sections_data[name] is None:
                    errors[name] = f"no saved file at {path}"
                return
            rc, st = run(fn, cli)
            statuses[name] = st
            sections_data[name] = loader(path) if os.path.exists(path) else None
            if sections_data[name] is None:
                errors[name] = json.dumps(st)
        except Exception as exc:
            statuses[name] = {"collector": {"ok": False, "error": f"{type(exc).__name__}: {exc}"}}
            sections_data[name] = None
            errors[name] = str(exc)

    conn = state.connect(args.db) if args.db else None
    tmp_files = []

    def temp_json(text_):
        """Private temp file (feed URLs are secrets); removed in the finally below."""
        fd, path_ = tempfile.mkstemp(suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text_)
        tmp_files.append(path_)
        return path_

    try:
        if not args.skip_calendar:
            path = os.path.join(args.raw_dir, "calendar.json")
            cli = ["--days", str(args.calendar_days), "--timezone", args.timezone, "--out", path]
            if conn and not args.from_raw:
                feeds = {c["label"]: c["url"] for c in state.get_calendars(conn) if c["enabled"]}
                cli += ["--feeds-file", temp_json(json.dumps(feeds))]
            attempt("calendar", gcal.main, cli, path, load_json)
        if not args.skip_weather:
            path = os.path.join(args.raw_dir, "weather.json")
            attempt("weather", weather.main, ["--timezone", args.timezone, "--out", path], path, load_json)
        if not args.skip_events:
            path = os.path.join(args.raw_dir, "events.jsonl")
            cli = ["--days", str(args.event_days), "--out", path]
            if conn and not args.from_raw:
                cli += ["--keywords-file", temp_json("\n".join(state.get_keywords(conn)) + "\n")]
            attempt("events", collect.main, cli, path,
                    lambda p_: [json.loads(l) for l in open(p_, encoding="utf-8")])
    finally:
        for f in tmp_files:
            os.remove(f)

    if conn and args.run_id and not args.from_raw:
        state.record_sources(conn, args.run_id, statuses)

    if conn and sections_data.get("events") is not None:
        if not args.from_raw:
            state.sync_events(conn, sections_data["events"])
        sections_data["events"] = state.visible(conn, sections_data["events"])[0]

    now = datetime.now(tz)
    md = [f"# Morning context — {now.strftime('%A, %B %d, %Y').replace(' 0', ' ')}", "",
          f"Generated {now.strftime('%H:%M')} {args.timezone}.", ""]
    if not args.skip_calendar:
        md += calendar_section(sections_data["calendar"], errors.get("calendar"), tz)
    if not args.skip_weather:
        md += weather_section(sections_data["weather"], errors.get("weather"))
    if not args.skip_events:
        md += events_section(sections_data["events"], errors.get("events"), args.event_days)
    md += ["## Data sources", "", "Re-rendered from saved raw output; collector status not available.", ""]         if args.from_raw else status_section(statuses)

    tmp_out = args.out + ".tmp"  # atomic swap: the web server may be reading args.out
    with open(tmp_out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))
    os.replace(tmp_out, args.out)
    print(f"wrote {args.out}", file=sys.stderr)
    ok = [d is not None for d in sections_data.values()]
    return 0 if any(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
