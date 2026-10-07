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
import os
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


def sync_events(conn, rows, now=None):
    """Record this run's events; a changed content_hash re-surfaces dismissed events."""
    now = now or now_iso()
    with conn:
        for e in rows:
            old = conn.execute("SELECT content_hash, status FROM events WHERE source_event_id=?",
                               (e["source_event_id"],)).fetchone()
            if old is None:
                conn.execute("INSERT INTO events (source_event_id, title, url, start, content_hash, first_seen, last_seen,"
                             " source, end_at, location, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (e["source_event_id"], e["title"], e["url"], e["start"], e["content_hash"], now, now,
                              *_details(e)))
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


def set_statuses(conn, refs, status):
    """Set status on events given as short ids (e42) or platform ids. Returns (number updated, refs that matched none)."""
    if status not in STATUSES:
        raise ValueError(f"bad status {status!r}")
    refs = list(dict.fromkeys(refs))
    unknown = []
    with conn:
        for ref in refs:
            sid = _resolve(conn, ref)
            if sid is None:
                unknown.append(ref)
            else:
                conn.execute("UPDATE events SET status=? WHERE source_event_id=?", (status, sid))
    return len(refs) - len(unknown), unknown


def set_status(conn, ref, status):
    return set_statuses(conn, [ref], status)[0] > 0


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
        "updated": bool(changed and now - datetime.fromisoformat(changed) < timedelta(days=UPDATED_FLAG_DAYS)),
    }


def get_event(conn, ref, now=None):
    """One stored event by short id (e42) or platform id, or None."""
    sid = _resolve(conn, ref)
    r = conn.execute("SELECT * FROM events WHERE source_event_id=?", (sid,)).fetchone() if sid else None
    return _event_dict(r, datetime.now(timezone.utc)) if r else None


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
    return out[:limit] if limit else out


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
