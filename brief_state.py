"""SQLite state for the morning brief: keywords, calendar feeds, per-event mute/dismiss
tracking, run history.

Vendored from the standalone morning-brief project (github.com/krets/morning) into Toledo.
Lives in its own database (~/.toledo/brief.db), separate from toledo.db.

Event semantics (keyed by source_event_id, change detected via the collector's content_hash):
  active     shown in the brief
  dismissed  hidden until the event's details change, then it comes back (active, flagged updated)
  muted      hidden for good, even if the details change
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
    source_event_id TEXT PRIMARY KEY,
    title TEXT, url TEXT, start TEXT,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'dismissed', 'muted')),
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
    changed_at TEXT);
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


def connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
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

def sync_events(conn, rows, now=None):
    """Record this run's events; a changed content_hash re-surfaces dismissed events."""
    now = now or now_iso()
    with conn:
        for e in rows:
            old = conn.execute("SELECT content_hash, status FROM events WHERE source_event_id=?",
                               (e["source_event_id"],)).fetchone()
            if old is None:
                conn.execute("INSERT INTO events (source_event_id, title, url, start, content_hash, first_seen, last_seen)"
                             " VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (e["source_event_id"], e["title"], e["url"], e["start"], e["content_hash"], now, now))
            elif old["content_hash"] != e["content_hash"]:
                conn.execute("UPDATE events SET title=?, url=?, start=?, content_hash=?, last_seen=?, changed_at=?,"
                             " status=CASE status WHEN 'dismissed' THEN 'active' ELSE status END"
                             " WHERE source_event_id=?",
                             (e["title"], e["url"], e["start"], e["content_hash"], now, now, e["source_event_id"]))
            else:
                conn.execute("UPDATE events SET last_seen=? WHERE source_event_id=?", (now, e["source_event_id"]))
        cutoff = (datetime.fromisoformat(now) - timedelta(days=KEEP_UNSEEN_DAYS)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM events WHERE last_seen < ?", (cutoff,))


def set_status(conn, source_event_id, status):
    if status not in STATUSES:
        raise ValueError(f"bad status {status!r}")
    with conn:
        cur = conn.execute("UPDATE events SET status=? WHERE source_event_id=?", (status, source_event_id))
    return cur.rowcount > 0


def set_statuses(conn, ids, status):
    """Bulk set_status. Returns (number updated, ids that matched no known event)."""
    if status not in STATUSES:
        raise ValueError(f"bad status {status!r}")
    ids = list(dict.fromkeys(ids))
    unknown = []
    with conn:
        for i in ids:
            if not conn.execute("UPDATE events SET status=? WHERE source_event_id=?", (status, i)).rowcount:
                unknown.append(i)
    return len(ids) - len(unknown), unknown


def annotate(conn, rows, now=None):
    """Attach _status and _updated to each event dict. Unknown events count as active."""
    now = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
    known = {r["source_event_id"]: r for r in conn.execute("SELECT source_event_id, status, changed_at FROM events")}
    out = []
    for e in rows:
        s = known.get(e["source_event_id"])
        e = dict(e)
        e["_status"] = s["status"] if s else "active"
        e["_updated"] = bool(s and s["changed_at"] and
                             now - datetime.fromisoformat(s["changed_at"]) < timedelta(days=UPDATED_FLAG_DAYS))
        out.append(e)
    return out


def visible(conn, rows, now=None):
    """Events to show in the brief; the second value is how many were hidden."""
    ann = annotate(conn, rows, now)
    shown = [e for e in ann if e["_status"] == "active"]
    return shown, len(ann) - len(shown)


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
