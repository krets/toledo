#!/usr/bin/env python3
"""Collect Berlin tech events from Luma, Meetup, Eventbrite into one normalized JSONL stream.

Vendored from the standalone morning-brief project (github.com/krets/morning) into Toledo.
Collection only: no state, no dedupe, no cache in this pass (see brief_state.py for that).
Env: MEETUP_TOKEN, EVENTBRITE_TOKEN (both optional), LUMA_PLACE_ID, LUMA_API,
     MEETUP_GQL, EVENTBRITE_HOST.
"""
import argparse
import hashlib
import html
import json
import math
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BERLIN = (52.5200, 13.4050)
UA = "Mozilla/5.0 (X11; Linux x86_64) event-collector/0.1 (personal use)"

DEFAULT_KEYWORDS = [
    "python", "rust", "golang", "typescript", "programming languages",
    "software engineering", "software architecture", "platform engineering",
    "developer tooling", "devops", "site reliability", "kubernetes",
    "distributed systems", "backend", "machine learning", "deep learning",
    "generative ai", "llm", "computer vision", "virtual reality",
    "augmented reality", "xr", "ux", "user experience", "game development",
    "graphics programming", "open source",
]

LUMA_API = os.environ.get("LUMA_API", "https://api.lu.ma")
MEETUP_GQL = os.environ.get("MEETUP_GQL", "https://www.meetup.com/gql2")
EVENTBRITE_HOST = os.environ.get("EVENTBRITE_HOST", "www.eventbrite.de")
EVENTBRITE_API = "https://www.eventbriteapi.com/v3"

MEETUP_QUERY = """
query($filter: EventSearchFilter!, $first: Int, $after: String) {
  eventSearch(filter: $filter, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    edges { node {
      id title description dateTime endTime eventUrl eventType isOnline
      venue { name address city state country postalCode lat lon }
      group { id name urlname }
    } }
  }
}
"""


def log(msg):
    print(msg, file=sys.stderr)


# ---------- helpers ----------

class BudgetExceeded(RuntimeError):
    pass


class GuardedSession(requests.Session):
    """Counts requests and refuses to go past `budget` (reset per source by main)."""
    budget = None
    used = 0

    def request(self, *a, **kw):
        if self.budget is not None and self.used >= self.budget:
            raise BudgetExceeded(f"request budget of {self.budget} reached")
        self.used += 1
        return super().request(*a, **kw)


def pause(delay):
    """Sleep around `delay` seconds, jittered so requests don't arrive on a metronome."""
    time.sleep(delay * random.uniform(0.75, 1.25))


def make_session():
    s = GuardedSession()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en"})
    retry = Retry(total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=None, respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def make_id(raw):
    """Numeric ids get a platform prefix elsewhere; non-numeric ids (uuid, evt-xxx) are kept."""
    return str(raw)


def prefixed_id(source, raw):
    raw = str(raw)
    return f"{source}:{raw}" if raw.isdigit() else raw


def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def prosemirror_text(node):
    if not isinstance(node, dict):
        return ""
    if "text" in node:
        return node["text"]
    parts = [prosemirror_text(c) for c in node.get("content", [])]
    sep = "\n" if node.get("type") in ("doc", "bullet_list", "ordered_list") else ""
    out = sep.join(parts) if node.get("type") in ("doc", "bullet_list", "ordered_list") else "".join(parts)
    if node.get("type") in ("paragraph", "heading", "list_item", "listItem"):
        out += "\n"
    return out


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def haversine_km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(d))


def parse_dt(s, tz=None):
    """ISO string to aware datetime. Naive input is interpreted in tz."""
    if not s:
        return None
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz or "Europe/Berlin"))
    elif tz:
        try:
            dt = dt.astimezone(ZoneInfo(tz))
        except Exception:
            pass
    return dt


def empty_location():
    return {"name": None, "address": None, "city": None, "region": None,
            "country": None, "postal_code": None, "lat": None, "lon": None,
            "is_online": False, "online_url": None}


def finish(ev, raw, include_raw):
    ev["content_hash"] = hashlib.sha256(json.dumps(
        [ev["title"], ev["description"], ev["start"], ev["end"], ev["location"]],
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    ev["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ev["matched_queries"] = []
    if include_raw:
        ev["raw"] = raw
    return ev


# ---------- Luma ----------

def luma_normalize(entry, detail=None, include_raw=False):
    ev = dict(entry.get("event") or entry)
    hosts = entry.get("hosts") or []
    cal = entry.get("calendar") or {}
    if detail:
        ev.update(detail.get("event") or {})
        hosts = detail.get("hosts") or hosts
        cal = detail.get("calendar") or cal
    geo = ev.get("geo_address_info") or {}
    coord = ev.get("coordinate") or {}
    loc = empty_location()
    loc.update(
        name=geo.get("address") or geo.get("description"),
        address=geo.get("full_address") or geo.get("short_address"),
        city=geo.get("city"), region=geo.get("region"), country=geo.get("country"),
        lat=to_float(coord.get("latitude")), lon=to_float(coord.get("longitude")),
    )
    if str(ev.get("location_type", "")).lower() in ("online", "zoom"):
        loc["is_online"] = True
        loc["online_url"] = ev.get("zoom_meeting_url") or ev.get("meeting_url")
    tz = ev.get("timezone")
    desc = ""
    if detail:
        desc = prosemirror_text(detail.get("description_mirror")).strip() or detail.get("description", "") or ""
    organizers = [{"name": h.get("name"), "id": h.get("api_id"),
                   "url": f"https://luma.com/user/{h['username']}" if h.get("username") else None}
                  for h in hosts]
    if not organizers and cal.get("name"):
        organizers.append({"name": cal["name"], "id": cal.get("api_id"),
                           "url": f"https://luma.com/{cal['slug']}" if cal.get("slug") else None})
    out = {
        "source": "luma",
        "source_event_id": make_id(ev.get("api_id")),
        "url": f"https://luma.com/{ev['url']}" if ev.get("url") else None,
        "title": ev.get("name"),
        "description": desc,
        "description_kind": "full" if detail else "none",
        "start": parse_dt(ev.get("start_at"), tz).isoformat() if ev.get("start_at") else None,
        "end": parse_dt(ev.get("end_at"), tz).isoformat() if ev.get("end_at") else None,
        "timezone": tz,
        "location": loc,
        "organizers": organizers,
    }
    return finish(out, entry, include_raw)


def luma_discover_place_id(sess, delay):
    pid = os.environ.get("LUMA_PLACE_ID")
    if pid:
        return pid
    for host in ("https://luma.com/berlin", "https://lu.ma/berlin"):
        try:
            r = sess.get(host, timeout=30)
        except requests.RequestException as exc:
            log(f"luma: {host} unreachable ({type(exc).__name__}), trying next host")
            continue
        pause(delay)
        m = re.search(r"discplace-[A-Za-z0-9]+", r.text) if r.ok else None
        if m:
            return m.group(0)
    raise RuntimeError("could not find Luma Berlin place id; set LUMA_PLACE_ID (discplace-...)")


def collect_luma(sess, args, keywords):
    place = luma_discover_place_id(sess, args.delay)
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=args.days)
    entries, cursor = [], None
    for _ in range(args.max_pages):
        params = {"discover_place_api_id": place, "pagination_limit": 50}
        if cursor:
            params["pagination_cursor"] = cursor
        r = sess.get(f"{LUMA_API}/discover/get-paginated-events", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        got = data.get("entries", [])
        entries += got
        pause(args.delay)
        if not data.get("has_more") or not data.get("next_cursor"):
            break
        # the feed runs in start order: once a page ends past the horizon, later pages are all out of range
        last = parse_dt(((got[-1].get("event") or got[-1]).get("start_at")) if got else None)
        if last and last > horizon:
            break
        cursor = data["next_cursor"]
    log(f"luma: {len(entries)} list entries for {place}")
    out = []
    for e in entries:
        ev = luma_normalize(e, None, args.include_raw)
        if not keep_basic(ev, args, now):  # don't spend a detail request on events we'd drop anyway
            continue
        api_id = (e.get("event") or {}).get("api_id") or e.get("api_id")
        detail = None
        if api_id:
            try:
                r = sess.get(f"{LUMA_API}/event/get", params={"event_api_id": api_id}, timeout=30)
                detail = r.json() if r.ok else None
            except BudgetExceeded:
                raise
            except Exception as exc:  # detail is best effort
                log(f"luma: detail failed for {api_id}: {exc}")
            pause(args.delay)
        out.append(luma_normalize(e, detail, args.include_raw))
    return out


# ---------- Meetup ----------

def meetup_normalize(ev, include_raw=False):
    v = ev.get("venue") or {}
    loc = empty_location()
    loc.update(name=v.get("name"), address=v.get("address"), city=v.get("city"),
               region=v.get("state"), country=(v.get("country") or "").upper() or None,
               postal_code=v.get("postalCode"), lat=to_float(v.get("lat")), lon=to_float(v.get("lon")))
    if ev.get("isOnline") or str(ev.get("eventType", "")).upper() == "ONLINE":
        loc["is_online"] = True
        loc["online_url"] = ev.get("eventUrl")
    g = ev.get("group") or {}
    start = parse_dt(ev.get("dateTime"))
    end = parse_dt(ev.get("endTime")) if ev.get("endTime") else None
    out = {
        "source": "meetup",
        "source_event_id": prefixed_id("meetup", ev.get("id")),
        "url": ev.get("eventUrl"),
        "title": ev.get("title"),
        "description": (ev.get("description") or "").strip(),
        "description_kind": "full",
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "timezone": None,
        "location": loc,
        "organizers": [{"name": g.get("name"), "id": g.get("id"),
                        "url": f"https://www.meetup.com/{g['urlname']}/" if g.get("urlname") else None}] if g else [],
    }
    return finish(out, ev, include_raw)


def collect_meetup(sess, args, keywords):
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("MEETUP_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    radius_mi = max(1, math.ceil(args.radius_km * 0.621371))
    out = {}
    now = datetime.now(timezone.utc)
    window = {"startDateRange": now.isoformat(timespec="seconds"),
              "endDateRange": (now + timedelta(days=args.days)).isoformat(timespec="seconds")}
    for kw in keywords:
        after = None
        for _ in range(args.max_pages):
            variables = {"first": 50, "after": after, "filter": {
                "query": kw, "lat": args.lat, "lon": args.lon, "radius": radius_mi, **window}}
            r = sess.post(MEETUP_GQL, headers=headers, timeout=30,
                          json={"query": MEETUP_QUERY, "variables": variables})
            r.raise_for_status()
            body = r.json()
            if body.get("errors"):
                raise RuntimeError(f"meetup graphql errors: {json.dumps(body['errors'])[:500]}")
            ks = body["data"]["eventSearch"]
            for edge in ks["edges"]:
                if not (edge["node"] or {}).get("id"):
                    continue
                e = meetup_normalize(edge["node"], args.include_raw)
                cur = out.setdefault(e["source_event_id"], e)
                cur["matched_queries"].append(kw)
            pause(args.delay)
            if not ks["pageInfo"]["hasNextPage"]:
                break
            after = ks["pageInfo"]["endCursor"]
        log(f"meetup: '{kw}' done, {len(out)} unique so far")
    return list(out.values())


# ---------- Eventbrite ----------

SERVER_DATA_RE = re.compile(r"window\.__SERVER_DATA__\s*=\s*")


def eventbrite_parse_page(page_html):
    m = SERVER_DATA_RE.search(page_html)
    if not m:
        raise RuntimeError("eventbrite: __SERVER_DATA__ not found (page structure changed or blocked)")
    data, _ = json.JSONDecoder().raw_decode(page_html[m.end():])
    evs = data["search_data"]["events"]
    return evs.get("results", []), evs.get("pagination", {})


def eventbrite_normalize(r, description=None, include_raw=False):
    v = r.get("primary_venue") or {}
    a = v.get("address") or {}
    loc = empty_location()
    loc.update(name=v.get("name"),
               address=a.get("localized_address_display") or a.get("address_1"),
               city=a.get("city"), region=a.get("region"), country=a.get("country"),
               postal_code=a.get("postal_code"),
               lat=to_float(a.get("latitude")), lon=to_float(a.get("longitude")))
    if r.get("is_online_event"):
        loc["is_online"] = True
    tz = r.get("timezone") or "Europe/Berlin"

    def mk(d, t):
        if not d:
            return None
        return parse_dt(f"{d}T{t or '00:00'}", tz).isoformat()

    org = r.get("primary_organizer") or {}
    full = description is not None
    out = {
        "source": "eventbrite",
        "source_event_id": prefixed_id("eventbrite", r.get("id")),
        "url": r.get("url"),
        "title": r.get("name"),
        "description": (description if full else r.get("summary")) or "",
        "description_kind": "full" if full else "summary",
        "start": mk(r.get("start_date"), r.get("start_time")),
        "end": mk(r.get("end_date"), r.get("end_time")),
        "timezone": tz,
        "location": loc,
        "organizers": [{"name": org.get("name"), "id": str(org["id"]) if org.get("id") else None,
                        "url": org.get("url")}] if org else [],
    }
    return finish(out, r, include_raw)


def collect_eventbrite(sess, args, keywords):
    token = os.environ.get("EVENTBRITE_TOKEN")
    out = {}
    today = datetime.now(ZoneInfo("Europe/Berlin")).date()
    window = {"start_date": today.isoformat(), "end_date": (today + timedelta(days=args.days)).isoformat()}
    for kw in keywords:
        slug = re.sub(r"[^a-z0-9]+", "-", kw.lower()).strip("-")
        for page in range(1, args.eventbrite_pages + 1):
            url = f"https://{EVENTBRITE_HOST}/d/germany--berlin/{slug}/"
            r = sess.get(url, params={**window, **({"page": page} if page > 1 else {})}, timeout=30)
            r.raise_for_status()
            results, pg = eventbrite_parse_page(r.text)
            for res in results:
                e = eventbrite_normalize(res, include_raw=args.include_raw)
                cur = out.setdefault(e["source_event_id"], e)
                cur["matched_queries"].append(kw)
            pause(args.delay)
            if page >= int(pg.get("page_count") or 1):
                break
        log(f"eventbrite: '{kw}' done, {len(out)} unique so far")
    if token:
        for e in out.values():
            eid = e["source_event_id"].split(":")[-1]
            try:
                r = sess.get(f"{EVENTBRITE_API}/events/{eid}/description/",
                             headers={"Authorization": f"Bearer {token}"}, timeout=30)
                if r.ok:
                    e["description"] = strip_html(r.json().get("description"))
                    e["description_kind"] = "full"
            except Exception as exc:
                log(f"eventbrite: description failed for {eid}: {exc}")
            pause(args.delay)
    return list(out.values())


# ---------- filtering ----------

def kw_regex(kw):
    return re.compile(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", re.I)


URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.I)
# Google Plus Codes ("G95X+XR Berlin") appear in location text and look like words around a '+'
PLUS_CODE_RE = re.compile(r"(?<![A-Za-z0-9])[23456789CFGHJMPQRVWX]{4,8}\+[23456789CFGHJMPQRVWX]{2,3}(?![A-Za-z0-9])")


def match_text(ev):
    """Title + description with the parts that cause false keyword hits (URLs, Plus Codes) removed."""
    text = f"{ev['title']}\n{ev['description']}"
    return PLUS_CODE_RE.sub(" ", URL_RE.sub(" ", text))


def keep_basic(ev, args, now):
    """Window, online and distance checks (everything except keywords)."""
    if not ev["start"]:
        return False
    start = datetime.fromisoformat(ev["start"])
    if not (now <= start <= now + timedelta(days=args.days)):
        return False
    loc = ev["location"]
    if loc["is_online"] and not args.include_online:
        return False
    if loc["lat"] is not None and loc["lon"] is not None:
        if haversine_km((loc["lat"], loc["lon"]), (args.lat, args.lon)) > args.radius_km:
            return False
    return True


def keep(ev, args, now, keywords, need_kw_match):
    if not keep_basic(ev, args, now):
        return False
    if need_kw_match:
        text = match_text(ev)
        hits = [k for k in keywords if kw_regex(k).search(text)]
        if not hits:
            return False
        ev["matched_queries"] = hits
    return True


COLLECTORS = {"luma": collect_luma, "meetup": collect_meetup, "eventbrite": collect_eventbrite}


def main(argv=None, session=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sources", default="luma,meetup,eventbrite")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--lat", type=float, default=BERLIN[0])
    p.add_argument("--lon", type=float, default=BERLIN[1])
    p.add_argument("--radius-km", type=float, default=25)
    p.add_argument("--keywords-file", help="one keyword per line, replaces defaults")
    p.add_argument("--max-pages", type=int, default=3, help="result pages per keyword (Meetup) / per feed (Luma)")
    p.add_argument("--eventbrite-pages", type=int, default=2,
                   help="result pages per keyword on Eventbrite, whose search is mostly noise")
    p.add_argument("--delay", type=float, default=2.0, help="average seconds between requests (jittered +/-25%%)")
    p.add_argument("--max-requests", type=int, default=250, help="hard cap on HTTP requests per source per run")
    p.add_argument("--include-online", action="store_true")
    p.add_argument("--include-raw", action="store_true", help="embed raw source payload under 'raw'")
    p.add_argument("--no-keyword-filter", action="store_true",
                    help="skip client-side keyword matching; keep whatever each source's own search returns")
    p.add_argument("--out", default="-", help="JSONL path, '-' for stdout")
    args = p.parse_args(argv)

    keywords = DEFAULT_KEYWORDS
    if args.keywords_file:
        keywords = [l.strip() for l in open(args.keywords_file) if l.strip() and not l.startswith("#")]
    sess = session or make_session()
    now = datetime.now(timezone.utc)
    events, status = [], {}
    for name in [s.strip() for s in args.sources.split(",") if s.strip()]:
        sess.used, sess.budget = 0, args.max_requests
        t0 = time.time()
        try:
            raw = COLLECTORS[name](sess, args, keywords)
            # Each source's own "keyword search" is unreliable (seen live: Meetup and Eventbrite
            # results overwhelmingly don't mention the matched keyword at all), so every source
            # gets the same client-side title/description check rather than trusting server-side search.
            need = not args.no_keyword_filter
            kept = [e for e in raw if keep(e, args, now, keywords, need)]
            events += kept
            status[name] = {"ok": True, "fetched": len(raw), "kept": len(kept)}
        except Exception as exc:
            status[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        status[name].update(requests=sess.used, seconds=round(time.time() - t0))
    events.sort(key=lambda e: e["start"])
    fh = sys.stdout if args.out == "-" else open(args.out, "w", encoding="utf-8")
    for e in events:
        fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    if fh is not sys.stdout:
        fh.close()
    log(json.dumps({"run_at": now.isoformat(timespec="seconds"), "sources": status}))
    return 0 if any(s["ok"] for s in status.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
