"""SQLite state for the morning brief: keywords, calendar feeds, per-event mute/dismiss
tracking, run history.

Vendored from the standalone morning-brief project (github.com/krets/morning) into Toledo.
Lives in its own database (~/.toledo/brief.db), separate from toledo.db.

Event semantics (keyed by source_event_id, change detected via the collector's content_hash):
  active     shown in the brief
  dismissed  hidden until the event's details change, then it comes back (active, flagged updated)
  muted      hidden for good, even if the details change
Independently of status, an event is accepted (accepted_at set) while a calendar entry matches it.
Accepted events leave the review list and the brief. Each event also has a small integer id,
shown as e<id>, so agents can refer to it without carrying the platform id or URL.
"""
import json
import math
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS keywords (keyword TEXT PRIMARY KEY COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS calendars (
    label TEXT PRIMARY KEY, url TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_event_id TEXT NOT NULL UNIQUE,
    source TEXT, title TEXT, url TEXT, start TEXT, end_at TEXT,
    location TEXT, description TEXT,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'dismissed', 'muted')),
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    changed_at TEXT, accepted_at TEXT);
CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, calendar TEXT NOT NULL,
    title TEXT, start TEXT, end_at TEXT, all_day INTEGER NOT NULL DEFAULT 0, location TEXT);
CREATE INDEX IF NOT EXISTS calendar_events_calendar ON calendar_events(calendar);
CREATE TABLE IF NOT EXISTS calendar_feeds (
    label TEXT PRIMARY KEY, fetched_at TEXT, attempted_at TEXT, error TEXT,
    window_start TEXT, window_end TEXT);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL, finished_at TEXT, trigger TEXT,
    ok INTEGER, log TEXT);
CREATE TABLE IF NOT EXISTS run_sources (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, name TEXT NOT NULL,
    ok INTEGER, error TEXT, detail TEXT,
    PRIMARY KEY (run_id, kind, name));
"""
KEEP_RUNS = 1000

STATUSES = ("active", "dismissed", "muted")
UPDATED_FLAG_DAYS = 2
KEEP_UNSEEN_DAYS = 60


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _legacy_events(conn):
    """True for an events table from before events had an integer id."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    return bool(cols) and "id" not in cols


def connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    legacy = _legacy_events(conn)
    if legacy:
        conn.execute("ALTER TABLE events RENAME TO events_old")
    conn.executescript(SCHEMA)
    if legacy:
        with conn:
            conn.execute("INSERT INTO events (source_event_id, title, url, start, content_hash, status,"
                         " first_seen, last_seen, changed_at)"
                         " SELECT source_event_id, title, url, start, content_hash, status, first_seen,"
                         " last_seen, changed_at FROM events_old ORDER BY first_seen, source_event_id")
            conn.execute("DROP TABLE events_old")
    if "color" not in {r["name"] for r in conn.execute("PRAGMA table_info(keywords)")}:
        with conn:
            conn.execute("ALTER TABLE keywords ADD COLUMN color INTEGER")
            for n, r in enumerate(conn.execute("SELECT rowid FROM keywords ORDER BY rowid").fetchall()):
                conn.execute("UPDATE keywords SET color=? WHERE rowid=?", (n, r["rowid"]))
    return conn


# ---------- settings ----------

def _assign_colors(keywords, used):
    """{keyword: slot}, giving each the lowest slot not in `used`. Slots are never reshuffled."""
    used, out, n = set(used), {}, 0
    for k in keywords:
        while n in used:
            n += 1
        out[k] = n
        used.add(n)
    return out


def seed(conn, keywords, feeds):
    """Fill keywords/calendars from defaults exactly once, so deleting them later sticks."""
    if conn.execute("SELECT 1 FROM meta WHERE key='seeded'").fetchone():
        return
    with conn:
        conn.executemany("INSERT OR IGNORE INTO keywords (keyword, color) VALUES (?, ?)",
                         list(_assign_colors(list(dict.fromkeys(keywords)), ()).items()))
        conn.executemany("INSERT OR IGNORE INTO calendars (label, url) VALUES (?, ?)", list(feeds.items()))
        conn.execute("INSERT INTO meta VALUES ('seeded', ?)", (now_iso(),))


def get_keywords(conn):
    return [r["keyword"] for r in conn.execute("SELECT keyword FROM keywords ORDER BY rowid")]


def set_keywords(conn, keywords):
    clean = []
    for k in keywords:
        k = " ".join(k.split())
        if k and k.lower() not in {c.lower() for c in clean}:
            clean.append(k)
    with conn:
        old = {r["keyword"].lower(): r["color"] for r in conn.execute("SELECT keyword, color FROM keywords")}
        kept = {k: old[k.lower()] for k in clean if old.get(k.lower()) is not None}
        fresh = _assign_colors([k for k in clean if k not in kept], kept.values())
        conn.execute("DELETE FROM keywords")
        conn.executemany("INSERT INTO keywords (keyword, color) VALUES (?, ?)",
                         [(k, kept.get(k, fresh.get(k))) for k in clean])
    return clean


def keyword_colors(conn):
    """{keyword.lower(): color slot}. Slots map to a stable hue (see hue()/tier() helpers)."""
    return {r["keyword"].lower(): r["color"] for r in conn.execute("SELECT keyword, color FROM keywords")}


def get_calendars(conn):
    return [dict(r) for r in conn.execute("SELECT label, url, enabled FROM calendars ORDER BY label")]


def upsert_calendar(conn, label, url=None, enabled=None):
    """Add or update a feed. url=None keeps the stored URL (the UI never shows it back in full)."""
    label = "_".join(label.strip().lower().split())
    if not label:
        raise ValueError("calendar label is required")
    row = conn.execute("SELECT url, enabled FROM calendars WHERE label=?", (label,)).fetchone()
    if row is None and not url:
        raise ValueError("a new calendar needs a URL")
    if url and not url.startswith(("http://", "https://")):
        raise ValueError("calendar URL must be http(s)")
    with conn:
        conn.execute("INSERT OR REPLACE INTO calendars (label, url, enabled) VALUES (?, ?, ?)", (
            label, url or row["url"], int(enabled if enabled is not None else (row["enabled"] if row else 1))))
    return label


def delete_calendar(conn, label):
    with conn:
        conn.execute("DELETE FROM calendars WHERE label=?", (label,))


# ---------- events ----------

DESCRIPTION_CHARS = 4000


# ---------- title matching (same event listed twice, or a series of them) ----------

MATCH_STOPWORDS = {"berlin", "germany", "deutschland", "gmbh", "the", "and", "und", "der", "die", "das", "str",
                   "strasse", "straße", "street", "platz", "event", "events", "meetup", "berlins"}


def tokens(text):
    """Distinctive lowercase words: no short words, bare numbers (house numbers, postcodes) or filler."""
    words = re.findall(r"[^\W_]+", (text or "").lower())
    return {w for w in words if len(w) >= 3 and not w.isdigit() and w not in MATCH_STOPWORDS}


def same_title(ev, other):
    """Two titles that name the same thing: two or more shared words, half of all words or one title inside the other."""
    a, b = tokens(ev["title"]), tokens(other["title"])
    if not a or not b:
        return False
    common = a & b
    return len(common) >= 2 and (len(common) / len(a | b) >= 0.5 or common in (a, b))


def same_series(ev, other):
    """Looser than same_title: most of the shorter title's words are shared, so 'Tech Mixer' matches 'Tech Mixer Berlin Oct'."""
    a, b = tokens(ev["title"]), tokens(other["title"])
    common = a & b
    return len(common) >= 2 and len(common) / min(len(a), len(b)) >= 0.6


def same_day(ev, other):
    return bool(ev["start"] and other["start"]) and ev["start"][:10] == other["start"][:10]


DEFAULT_HOURS = 2
SOURCE_HOURS = {"meetup": 3}  # meetups run long and rarely publish an end time
VENUE_METERS = 150


def _source(ev):
    src = ev.get("source")
    if not src:  # rows stored before the source column existed
        prefix = (ev.get("source_event_id") or "").split(":")[0]
        src = prefix if prefix in ("meetup", "luma", "eventbrite") else None
    return src


def event_span(ev):
    """(start, end) of an event; a missing end is guessed from the source."""
    start = datetime.fromisoformat(ev["start"])
    if ev.get("end"):
        return start, datetime.fromisoformat(ev["end"])
    return start, start + timedelta(hours=SOURCE_HOURS.get(_source(ev), DEFAULT_HOURS))


def times_overlap(a, b):
    try:
        (a0, a1), (b0, b1) = event_span(a), event_span(b)
        return a0 < b1 and b0 < a1
    except (TypeError, ValueError):  # missing start, or naive vs aware datetimes
        return False


def _meters(la, lb):
    (lat1, lon1), (lat2, lon2) = [(math.radians(float(x["lat"])), math.radians(float(x["lon"]))) for x in (la, lb)]
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 12742000 * math.asin(math.sqrt(h))


def same_venue(a, b):
    """Both events are at the same physical place: coordinates a stone's throw apart, the same venue name, or the
    same street with a shared house number. Online events have no venue."""
    la, lb = a.get("location") or {}, b.get("location") or {}
    if la.get("is_online") or lb.get("is_online"):
        return False
    if all(x.get("lat") is not None and x.get("lon") is not None for x in (la, lb)):
        return _meters(la, lb) <= VENUE_METERS
    na, nb = tokens(la.get("name")), tokens(lb.get("name"))
    if na and na == nb:
        return True
    sa, sb = tokens(la.get("address")), tokens(lb.get("address"))
    numa = set(re.findall(r"\d+", la.get("address") or ""))
    numb = set(re.findall(r"\d+", lb.get("address") or ""))
    return bool(sa & sb) and bool(numa & numb)


def is_duplicate(a, b):
    """The same event listed twice (typically on two platforms): same day with the same title, or overlapping
    times at the same venue. Venue matching can fold two real events held at one place at once."""
    if same_day(a, b) and same_title(a, b):
        return True
    return same_day(a, b) and times_overlap(a, b) and same_venue(a, b)


# ---------- matching against calendar entries ----------

MATCH_WINDOW = timedelta(minutes=60)


def same_place(ev, cal):
    """Calendar location text names the event's venue (its name, or two or more address words)."""
    loc = ev.get("location") or {}
    cal_tokens = tokens(cal["location"])
    name = tokens(loc.get("name"))
    if name and name <= cal_tokens:
        return True
    return len((name | tokens(loc.get("address"))) & cal_tokens) >= 2


def entry_matches(ev, c):
    """A timed calendar entry starts within an hour of the event and shares its venue or title."""
    if c["all_day"]:
        return False
    if abs(datetime.fromisoformat(c["start"]) - datetime.fromisoformat(ev["start"])) > MATCH_WINDOW:
        return False
    return same_place(ev, c) or same_title(ev, c)


def calendar_matches(ev, cal_events):
    return any(entry_matches(ev, c) for c in cal_events)


def collapse(items, get=lambda e: e):
    """Fold duplicates into the first of each group. Returns [(item, [its duplicates])] in the input order."""
    groups = []
    for it in items:
        for g in groups:
            if is_duplicate(get(g[0]), get(it)):
                g[1].append(it)
                break
        else:
            groups.append((it, []))
    return groups


def short_id(n):
    return f"e{n}"


def parse_ref(ref):
    """'e42', '#e42' or '42' -> 42; None for anything else."""
    ref = str(ref).strip().lstrip("#").lower()
    ref = ref[1:] if ref.startswith("e") else ref
    return int(ref) if ref.isdigit() else None


def _details(e):
    """The stored columns that hold what get_event shows."""
    return (e.get("source"), e.get("end"), json.dumps(e.get("location") or {}, ensure_ascii=False),
            (e.get("description") or "")[:DESCRIPTION_CHARS])


def _ev(r):
    """A stored event row as the dict the matching helpers expect (location decoded, end under 'end')."""
    d = dict(r)
    loc = d.get("location")
    d["location"] = json.loads(loc) if isinstance(loc, str) and loc else (loc if isinstance(loc, dict) else {})
    d["end"] = d.get("end_at")
    return d


def _inherited_status(conn, e):
    """A new listing of an event the user already hid arrives hidden too ('muted' beats 'dismissed')."""
    seen = {r["status"] for r in map(_ev, conn.execute(
        "SELECT * FROM events WHERE substr(start, 1, 10)=substr(?, 1, 10)", (e["start"] or "",)))
        if is_duplicate(e, r)}
    return "muted" if "muted" in seen else "dismissed" if "dismissed" in seen else "active"


def sync_events(conn, rows, now=None):
    """Record this run's events; a changed content_hash re-surfaces dismissed events."""
    now = now or now_iso()
    with conn:
        for e in rows:
            old = conn.execute("SELECT content_hash, status FROM events WHERE source_event_id=?",
                               (e["source_event_id"],)).fetchone()
            if old is None:
                conn.execute("INSERT INTO events (source_event_id, title, url, start, content_hash, first_seen, last_seen,"
                             " source, end_at, location, description, status)"
                             " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (e["source_event_id"], e["title"], e["url"], e["start"], e["content_hash"], now, now,
                              *_details(e), _inherited_status(conn, e)))
            elif old["content_hash"] != e["content_hash"]:
                conn.execute("UPDATE events SET title=?, url=?, start=?, content_hash=?, last_seen=?, changed_at=?,"
                             " source=?, end_at=?, location=?, description=?,"
                             " status=CASE status WHEN 'dismissed' THEN 'active' ELSE status END"
                             " WHERE source_event_id=?",
                             (e["title"], e["url"], e["start"], e["content_hash"], now, now, *_details(e),
                              e["source_event_id"]))
            else:  # also refreshes the details of rows stored before those columns existed
                conn.execute("UPDATE events SET last_seen=?, source=?, end_at=?, location=?, description=?"
                             " WHERE source_event_id=?", (now, *_details(e), e["source_event_id"]))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_event_sync', ?)", (now,))
        cutoff = (datetime.fromisoformat(now) - timedelta(days=KEEP_UNSEEN_DAYS)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM events WHERE last_seen < ?", (cutoff,))


def mark_accepted(conn, checked, accepted, now=None):
    """Set accepted_at for the `accepted` source ids and clear it for the rest of `checked`
    (the events the calendar could have matched), so cancelling a calendar entry un-accepts."""
    now = now or now_iso()
    accepted = set(accepted)
    with conn:
        for i in checked:
            if i in accepted:
                conn.execute("UPDATE events SET accepted_at=? WHERE source_event_id=? AND accepted_at IS NULL", (now, i))
            else:
                conn.execute("UPDATE events SET accepted_at=NULL WHERE source_event_id=?", (i,))


def _resolve(conn, ref):
    """Source event id for a short ref or a platform id, or None."""
    n = parse_ref(ref)
    r = conn.execute("SELECT source_event_id FROM events WHERE id=?", (n,)).fetchone() if n is not None else None
    if r is None:
        r = conn.execute("SELECT source_event_id FROM events WHERE source_event_id=?", (str(ref),)).fetchone()
    return r["source_event_id"] if r else None


def apply_status(conn, refs, status, series=False, now=None):
    """Set status on events given as short ids (e42) or platform ids, and on their duplicates. With series, also on
    every upcoming event whose title matches loosely. Returns ([{ref, title, start}] changed, refs that matched none)."""
    if status not in STATUSES:
        raise ValueError(f"bad status {status!r}")
    now = now or datetime.now(timezone.utc)
    rows = [_ev(r) for r in conn.execute("SELECT * FROM events")]
    by_sid = {r["source_event_id"]: r for r in rows}
    hit, unknown = {}, []
    for ref in dict.fromkeys(refs):
        sid = _resolve(conn, ref)
        if sid is None:
            unknown.append(ref)
            continue
        base = by_sid[sid]
        for r in rows:
            if r is base or is_duplicate(base, r) or (
                    series and r["start"] and datetime.fromisoformat(r["start"]) >= now and same_series(base, r)):
                hit[r["id"]] = r
    with conn:
        conn.executemany("UPDATE events SET status=? WHERE id=?", [(status, i) for i in hit])
    changed = sorted(hit.values(), key=lambda r: r["id"])
    return [{"ref": short_id(r["id"]), "title": r["title"], "start": r["start"]} for r in changed], unknown


def set_statuses(conn, refs, status, series=False):
    """apply_status, as (number of events changed, refs that matched none)."""
    changed, unknown = apply_status(conn, refs, status, series)
    return len(changed), unknown


def annotate(conn, rows, now=None):
    """Attach _id, _status, _updated and _accepted to each event dict. Unknown events count as active."""
    now = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    known = {r["source_event_id"]: r for r in conn.execute(
        "SELECT id, source_event_id, status, changed_at, accepted_at FROM events")}
    out = []
    for e in rows:
        s = known.get(e["source_event_id"])
        e = dict(e)
        e["_id"] = s["id"] if s else None
        e["_status"] = s["status"] if s else "active"
        e["_accepted"] = bool(s and s["accepted_at"])
        e["_updated"] = bool(s and s["changed_at"] and
                             now - datetime.fromisoformat(s["changed_at"]) < timedelta(days=UPDATED_FLAG_DAYS))
        out.append(e)
    return out


def visible(conn, rows, now=None):
    """Events to show in the brief; the second value is how many were hidden."""
    ann = annotate(conn, rows, now)
    shown = [e for e in ann if e["_status"] == "active" and not e["_accepted"]]
    return shown, len(ann) - len(shown)


def _event_dict(r, now):
    changed = r["changed_at"]
    return {
        "id": r["id"], "ref": short_id(r["id"]), "source_event_id": r["source_event_id"],
        "source": r["source"], "title": r["title"], "url": r["url"], "start": r["start"], "end": r["end_at"],
        "location": json.loads(r["location"]) if r["location"] else {}, "description": r["description"] or "",
        "status": r["status"], "accepted": bool(r["accepted_at"]),
        # only worth flagging while the event is on the review list
        "updated": r["status"] == "active" and bool(
            changed and now - datetime.fromisoformat(changed) < timedelta(days=UPDATED_FLAG_DAYS)),
    }


def get_event(conn, ref, now=None):
    """One stored event by short id (e42) or platform id, or None."""
    sid = _resolve(conn, ref)
    r = conn.execute("SELECT * FROM events WHERE source_event_id=?", (sid,)).fetchone() if sid else None
    if not r:
        return None
    ev = _event_dict(r, datetime.now(timezone.utc))
    me = _ev(r)
    ev["same"] = [{"ref": short_id(o["id"]), "source": o["source"], "url": o["url"]} for o in map(_ev, conn.execute(
        "SELECT * FROM events WHERE id!=? AND substr(start, 1, 10)=?", (r["id"], (r["start"] or "")[:10])))
        if is_duplicate(me, o)]
    ev["conflicts"] = conflicts_for(ev, calendar_entries(conn))
    return ev


def list_events(conn, status="active", include_accepted=False, now=None, limit=None):
    """Upcoming events still on the platforms as of the last collection, soonest first.
    status: one of STATUSES or 'all'. Accepted (on the calendar) events are left out unless asked for."""
    if status != "all" and status not in STATUSES:
        raise ValueError(f"bad status {status!r}")
    now = now or datetime.now(timezone.utc)
    sync = conn.execute("SELECT value FROM meta WHERE key='last_event_sync'").fetchone()
    out = []
    for r in conn.execute("SELECT * FROM events ORDER BY start, id"):
        if sync and r["last_seen"] < sync["value"]:
            continue  # gone from every platform at the last collection
        if not r["start"] or datetime.fromisoformat(r["start"]) < now:
            continue
        if status != "all" and r["status"] != status:
            continue
        if r["accepted_at"] and not include_accepted:
            continue
        out.append(_event_dict(r, now))
    out.sort(key=lambda e: (datetime.fromisoformat(e["start"]), e["id"]))
    out = [dict(e, same=[d["ref"] for d in dups]) for e, dups in collapse(out)]
    return out[:limit] if limit else out


# ---------- calendar cache ----------

CALENDAR_TTL = timedelta(minutes=15)
CALENDAR_RETRY = timedelta(minutes=2)  # a feed that just failed is not hammered by every call
HOLIDAY_PREFIX = "holidays_"


def store_calendar(conn, label, events, window_start, window_end, now=None):
    """Replace one feed's cached entries with a fresh fetch."""
    now = now or now_iso()
    with conn:
        conn.execute("DELETE FROM calendar_events WHERE calendar=?", (label,))
        conn.executemany(
            "INSERT INTO calendar_events (calendar, title, start, end_at, all_day, location) VALUES (?, ?, ?, ?, ?, ?)",
            [(label, e["title"], e["start"], e["end"], int(bool(e["all_day"])), e.get("location") or "") for e in events])
        conn.execute("INSERT OR REPLACE INTO calendar_feeds (label, fetched_at, attempted_at, error, window_start,"
                     " window_end) VALUES (?, ?, ?, NULL, ?, ?)", (label, now, now, window_start, window_end))


def note_calendar_failure(conn, label, error, now=None):
    """A failed fetch keeps the feed's previous entries; only the attempt and its error are recorded."""
    now = now or now_iso()
    with conn:
        conn.execute("INSERT OR IGNORE INTO calendar_feeds (label) VALUES (?)", (label,))
        conn.execute("UPDATE calendar_feeds SET attempted_at=?, error=? WHERE label=?", (now, str(error)[:300], label))


def calendar_entries(conn):
    """Every cached calendar entry, in the shape the collector produced."""
    return [dict(r, all_day=bool(r["all_day"]), end=r["end_at"], calendar=r["calendar"])
            for r in conn.execute("SELECT * FROM calendar_events ORDER BY start, id")]


def stale_feeds(conn, ttl=CALENDAR_TTL, retry=CALENDAR_RETRY, now=None, force=False):
    """Enabled feed labels that are due a fetch: never fetched or older than ttl, and not tried within `retry`."""
    now = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    seen = {r["label"]: r for r in conn.execute("SELECT * FROM calendar_feeds")}
    out = []
    for c in get_calendars(conn):
        if not c["enabled"]:
            continue
        f = seen.get(c["label"])
        tried = f and f["attempted_at"] and now - datetime.fromisoformat(f["attempted_at"]) < retry
        fresh = f and f["fetched_at"] and now - datetime.fromisoformat(f["fetched_at"]) < ttl
        if force or not (fresh or tried):
            out.append(c["label"])
    return out


def calendar_status(conn, ttl=CALENDAR_TTL, now=None):
    """{fresh, age_minutes, errors, feeds}: whether conflict data can be trusted as current."""
    now = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    seen = {r["label"]: r for r in conn.execute("SELECT * FROM calendar_feeds")}
    enabled = [c["label"] for c in get_calendars(conn) if c["enabled"]]
    ages, errors, missing = [], {}, False
    for label in enabled:
        f = seen.get(label)
        if not f or not f["fetched_at"]:
            missing = True
        else:
            ages.append(now - datetime.fromisoformat(f["fetched_at"]))
        if f and f["error"] and (not f["fetched_at"] or f["attempted_at"] > f["fetched_at"]):
            errors[label] = f["error"]
    oldest = max(ages) if ages else None
    return {"fresh": bool(enabled) and not missing and oldest <= ttl,
            "age_minutes": int(oldest.total_seconds() // 60) if oldest is not None else None,
            "errors": errors, "feeds": len(enabled)}


def update_accepted(conn, now=None):
    """Recompute which events a cached calendar entry matches. A match sets accepted_at, and a lost match clears it
    only while every enabled feed has been fetched at least once and the event lies inside the cached window."""
    feeds = [dict(r) for r in conn.execute("SELECT * FROM calendar_feeds WHERE fetched_at IS NOT NULL")]
    if not feeds:
        return
    complete = {c["label"] for c in get_calendars(conn) if c["enabled"]} <= {f["label"] for f in feeds}
    lo = max(datetime.fromisoformat(f["window_start"]) for f in feeds)
    hi = min(datetime.fromisoformat(f["window_end"]) for f in feeds)
    entries = calendar_entries(conn)
    checked, accepted = [], []
    for r in map(_ev, conn.execute("SELECT * FROM events WHERE start IS NOT NULL")):
        start = datetime.fromisoformat(r["start"])
        hit = calendar_matches(r, entries)
        if hit:
            accepted.append(r["source_event_id"])
        if complete and lo <= start < hi:
            checked.append(r["source_event_id"])
    mark_accepted(conn, checked or accepted, accepted, now)


def conflicts_for(ev, entries):
    """Every cached calendar entry on the event's day, flagged: overlaps in time, all-day, a holiday feed, or this
    event's own calendar entry. Several may be someone else's; the reader judges."""
    day = datetime.fromisoformat(ev["start"]).date()
    out = []
    for c in entries:
        first = datetime.fromisoformat(c["start"]).date()
        end = datetime.fromisoformat(c["end"]) if c["end"] else None
        last = ((end.date() - timedelta(days=1)) if c["all_day"] else end.date()) if end else first
        if not (first <= day <= max(first, last)):
            continue
        overlaps = (not c["all_day"]) and times_overlap(ev, {"start": c["start"], "end": c["end"]})
        out.append({"title": c["title"], "start": c["start"], "end": c["end"], "all_day": c["all_day"],
                    "overlaps": overlaps, "holiday": c["calendar"].startswith(HOLIDAY_PREFIX),
                    "this_event": entry_matches(ev, c)})
    out.sort(key=lambda x: (not x["all_day"], x["start"]))
    return out


def event_page(conn, after=None, limit=10, status="active", include_accepted=False, now=None):
    """One page of upcoming events, soonest first, each with its calendar conflicts. `after` is the ref of the last
    event of the previous page, so paging holds still while events are dismissed in between."""
    evs = list_events(conn, status, include_accepted, now)
    if after:
        r = conn.execute("SELECT id, start FROM events WHERE id=?", (parse_ref(after),)).fetchone() if parse_ref(after) else None
        if r is None:
            raise ValueError(f"no event {after!r} to continue after")
        key = (datetime.fromisoformat(r["start"]), r["id"])
        evs = [e for e in evs if (datetime.fromisoformat(e["start"]), e["id"]) > key]
    page = evs[:limit]
    entries = calendar_entries(conn)
    for e in page:
        e["conflicts"] = conflicts_for(e, entries)
    return {"events": page, "remaining": len(evs) - len(page),
            "next": page[-1]["ref"] if len(evs) > len(page) else None,
            "window": [page[0]["start"][:10], page[-1]["start"][:10]] if page else None,
            "calendar": calendar_status(conn)}


# ---------- runs ----------

def start_run(conn, trigger):
    with conn:
        cur = conn.execute("INSERT INTO runs (started_at, trigger) VALUES (?, ?)", (now_iso(), trigger))
    return cur.lastrowid


def finish_run(conn, run_id, ok, log):
    with conn:
        conn.execute("UPDATE runs SET finished_at=?, ok=?, log=? WHERE id=?", (now_iso(), int(ok), log[-20000:], run_id))
        conn.execute("DELETE FROM runs WHERE id <= (SELECT MAX(id) FROM runs) - ?", (KEEP_RUNS,))


def record_sources(conn, run_id, statuses):
    """statuses: {kind: {name: {ok, error?, ...counts}}} as printed by the collectors."""
    rows = []
    for kind, group in statuses.items():
        for name, st in group.items():
            detail = {k: v for k, v in st.items() if k not in ("ok", "error")}
            rows.append((run_id, kind, name, int(bool(st.get("ok"))), st.get("error"), json.dumps(detail)))
    with conn:
        conn.executemany("INSERT OR REPLACE INTO run_sources VALUES (?, ?, ?, ?, ?, ?)", rows)


def _duration(r):
    if not r["finished_at"]:
        return None
    return int((datetime.fromisoformat(r["finished_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds())


def last_runs(conn, n=10):
    """Newest first; each run carries its per-source results and duration in seconds."""
    runs = []
    for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)):
        r = dict(r)
        r["seconds"] = _duration(r)
        r["sources"] = [dict(x, detail=json.loads(x["detail"] or "{}")) for x in conn.execute(
            "SELECT kind, name, ok, error, detail FROM run_sources WHERE run_id=? ORDER BY kind, name", (r["id"],))]
        runs.append(r)
    return runs


def last_success(conn):
    r = conn.execute("SELECT id, finished_at FROM runs WHERE ok=1 ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def last_success_date(conn, tz):
    """Local date of the most recent successful run, or None."""
    r = last_success(conn)
    return datetime.fromisoformat(r["finished_at"]).astimezone(tz).date() if r else None


def last_attempt_age(conn):
    """Seconds since the latest run started (any outcome), or a large number if none."""
    r = conn.execute("SELECT started_at FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return (datetime.now(timezone.utc) - datetime.fromisoformat(r["started_at"])).total_seconds() if r else 1e12


def mark_interrupted(conn):
    """Runs left unfinished by a crash/restart would otherwise show as running forever."""
    with conn:
        conn.execute("UPDATE runs SET finished_at=?, ok=0, log='interrupted by restart' WHERE finished_at IS NULL", (now_iso(),))
