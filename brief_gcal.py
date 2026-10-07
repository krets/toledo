#!/usr/bin/env python3
"""Calendar events (today + next N days) from Google Calendar secret iCal feeds.

Vendored from the standalone morning-brief project (github.com/krets/morning) into Toledo.

Feeds normally come from brief_state's calendars table (seeded with the public DE/UK/US
holiday calendars; private feeds are added later through Toledo's Settings UI). Running this
module directly without --feeds-file falls back to .env / environment: every variable named
GCAL_ICS_<LABEL> is a feed URL and <LABEL> becomes the calendar's name in the output, with the
holiday calendars always added on top. Recurring events are expanded for the window.
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events
import requests

import brief_state as state

UA = "Mozilla/5.0 (X11; Linux x86_64) gcal-collector/0.1 (personal use)"

# Public, key-free Google holiday calendars. Seeded into brief_state on first start;
# the user adds private calendar URLs afterwards via Settings.
HOLIDAYS = {
    "holidays_de": "https://calendar.google.com/calendar/ical/en.german%23holiday%40group.v.calendar.google.com/public/basic.ics",
    "holidays_uk": "https://calendar.google.com/calendar/ical/en.uk%23holiday%40group.v.calendar.google.com/public/basic.ics",
    "holidays_us": "https://calendar.google.com/calendar/ical/en.usa%23holiday%40group.v.calendar.google.com/public/basic.ics",
}
ENV_PREFIX = "GCAL_ICS_"


def log(msg):
    print(msg, file=sys.stderr)


def load_dotenv(path):
    """Minimal KEY=value parser; real environment variables win over the file."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def feeds_from_env():
    feeds = {k[len(ENV_PREFIX):].lower(): v for k, v in os.environ.items()
             if k.startswith(ENV_PREFIX) and v}
    feeds.update(HOLIDAYS)
    return feeds


def to_local(value, tz):
    """iCal DATE or DATETIME -> (aware datetime in tz, is_all_day)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=tz)
        return value.astimezone(tz), False
    return datetime(value.year, value.month, value.day, tzinfo=tz), True


def text(component, key):
    v = component.get(key)
    return str(v).strip() if v is not None else ""


def fetch_calendar(sess, label, url, start, end, tz):
    r = sess.get(url, timeout=30)
    r.raise_for_status()
    if b"BEGIN:VCALENDAR" not in r.content[:200]:
        raise RuntimeError("response is not an iCal feed (bad or revoked URL?)")
    cal = icalendar.Calendar.from_ical(r.content)
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        if text(ev, "STATUS").upper() == "CANCELLED":
            continue
        s, all_day = to_local(ev.decoded("DTSTART"), tz)
        e = to_local(ev.decoded("DTEND"), tz)[0] if ev.get("DTEND") else None
        out.append({
            "calendar": label,
            "title": text(ev, "SUMMARY") or "(no title)",
            "start": s.isoformat(),
            "end": e.isoformat() if e else None,
            "all_day": all_day,
            "location": text(ev, "LOCATION"),
            "description": text(ev, "DESCRIPTION"),
        })
    return out


def safe_error(exc):
    """An error line that never contains the feed URL, which is a secret. Library errors (connection pools, redirects)
    quote the URL in their text, so only our own RuntimeError text and the HTTP status survive."""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTPError: {exc.response.status_code} {exc.response.reason}".strip()
    if isinstance(exc, RuntimeError):
        return f"RuntimeError: {exc}"
    return type(exc).__name__


def collect(sess, days, tz, feeds=None):
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start, end = today, today + timedelta(days=days)
    events, status = [], {}
    for label, url in (feeds if feeds is not None else feeds_from_env()).items():
        try:
            got = fetch_calendar(sess, label, url, start, end, tz)
            events += got
            status[label] = {"ok": True, "events": len(got)}
        except Exception as exc:
            status[label] = {"ok": False, "error": safe_error(exc)}
    events.sort(key=lambda e: (e["start"], e["title"]))
    return start, end, events, status


def refresh_cache(conn, days=90, tz_name="Europe/Berlin", force=False, session=None):
    """Re-fetch the feeds whose cache is stale (or all of them with force) into the database, then re-check which
    events the calendar covers. One GET per feed: an iCal URL cannot be date-bounded, so the window is applied after
    download. A feed that fails keeps its old entries and reports the error through state.calendar_status()."""
    labels = state.stale_feeds(conn, force=force)
    if labels:
        feeds = {c["label"]: c["url"] for c in state.get_calendars(conn) if c["label"] in labels}
        sess = session or requests.Session()
        if session is None:
            sess.headers.update({"User-Agent": UA})
        start, end, events, status = collect(sess, days, ZoneInfo(tz_name), feeds)
        for label in feeds:
            if status[label]["ok"]:
                state.store_calendar(conn, label, [e for e in events if e["calendar"] == label],
                                     start.isoformat(), end.isoformat())
            else:
                state.note_calendar_failure(conn, label, status[label]["error"])
        state.update_accepted(conn)
    return state.calendar_status(conn)


def main(argv=None, session=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--days", type=int, default=4, help="today plus the next N-1 days")
    p.add_argument("--timezone", default="Europe/Berlin")
    p.add_argument("--env-file", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    p.add_argument("--feeds-file", help="JSON {label: url}; used instead of .env/environment (no holidays added)")
    p.add_argument("--out", default="-", help="JSON path, '-' for stdout")
    args = p.parse_args(argv)

    feeds = None
    if args.feeds_file:
        with open(args.feeds_file, encoding="utf-8") as fh:
            feeds = json.load(fh)
    else:
        load_dotenv(args.env_file)
    sess = session or requests.Session()
    if session is None:
        sess.headers.update({"User-Agent": UA})
    tz = ZoneInfo(args.timezone)
    start, end, events, status = collect(sess, args.days, tz, feeds)

    result = {"timezone": args.timezone, "window_start": start.isoformat(), "window_end": end.isoformat(),
              "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "events": events}
    fh = sys.stdout if args.out == "-" else open(args.out, "w", encoding="utf-8")
    json.dump(result, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
    if fh is not sys.stdout:
        fh.close()
    log(json.dumps({"run_at": result["generated_at"], "sources": status}))
    return 0 if any(s["ok"] for s in status.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
