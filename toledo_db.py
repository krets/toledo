#!/usr/bin/env python3
"""
Toledo SQLite store — the single source of truth for tasks, projects, notes,
activity, glossary and context. The web server and MCP server both go through
this module; nothing else touches the database directly.

DB:      $TOLEDO_DB, or ~/.toledo/toledo.db
Config:  ~/.toledo/config.json  (LLM settings etc.; not task data)

A fresh database is populated automatically from the legacy file tree
(~/.toledo/tasks/) the first time it is opened. The legacy files are left in
place. To run the migration by hand:

    python toledo_db.py migrate [--tasks-dir DIR] [--db PATH]
"""

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

TOLEDO_HOME = Path.home() / ".toledo"
CONFIG_PATH = TOLEDO_HOME / "config.json"
LEGACY_TASKS_DIR = TOLEDO_HOME / "tasks"
LEGACY_CONTEXT_FILE = TOLEDO_HOME / ".context"

STATES = ["active", "completed", "archive"]
SUBTASK_STATES = ["active", "completed"]
DEFAULT_PROJECT = "GEN"
DEFAULT_PRIORITY = 50

# Priority is 1–99, higher = more important. Schema 1 stored it the other
# way round (lower = more important); _upgrade flips old databases.
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS projects (
    code  TEXT PRIMARY KEY,
    name  TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT ''
);

-- Top-level tasks have parent_id NULL; subtasks point at their parent.
-- project is deliberately not a foreign key: tasks may reference a code
-- that has no registry entry (as they could with the file backend).
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY,
    parent_id   INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    slug        TEXT NOT NULL,
    name        TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'active',
    priority    INTEGER NOT NULL DEFAULT 50,
    project     TEXT NOT NULL DEFAULT 'GEN',
    due         TEXT,
    recurrence  INTEGER,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_slug_top ON tasks(slug) WHERE parent_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS tasks_slug_sub ON tasks(parent_id, slug) WHERE parent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state);

-- Former slugs (renames, legacy file-backend names) so old references resolve.
CREATE TABLE IF NOT EXISTS task_aliases (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    slug    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS task_aliases_slug ON task_aliases(slug);

CREATE TABLE IF NOT EXISTS notes (
    id      INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    ts      TEXT NOT NULL,
    text    TEXT NOT NULL,
    source  TEXT
);
CREATE INDEX IF NOT EXISTS notes_task ON notes(task_id);

CREATE TABLE IF NOT EXISTS activity (
    id      INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    ts      TEXT NOT NULL,
    action  TEXT NOT NULL,
    data    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS activity_task ON activity(task_id);

CREATE TABLE IF NOT EXISTS description_history (
    id      INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    ts      TEXT NOT NULL,
    text    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS glossary (
    term      TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);
"""


class ToledoError(ValueError):
    status = 400


class NotFound(ToledoError):
    status = 404


class Conflict(ToledoError):
    status = 409


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {}


def save_config(config: dict) -> None:
    """Write config.json atomically; it can hold API keys, so keep it private."""
    TOLEDO_HOME.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


def db_path() -> Path:
    env = os.environ.get("TOLEDO_DB")
    if env:
        return Path(env)
    return Path(load_config().get("db_path") or TOLEDO_HOME / "toledo.db")


# ── Connection ────────────────────────────────────────────────────────────────

_initialized: set[str] = set()


def _open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _init(conn: sqlite3.Connection, path: Path) -> None:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    # The web and MCP servers share one database; the write lock makes sure
    # only the first of them to start runs the legacy migration.
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if not row:
        # Legacy files use the schema-1 priority scale; _upgrade converts them.
        conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
        legacy = Path(load_config().get("tasks_dir") or LEGACY_TASKS_DIR)
        if legacy.is_dir() and not os.environ.get("TOLEDO_NO_MIGRATE"):
            counts = migrate_from_files(conn, legacy, LEGACY_CONTEXT_FILE)
            print(f"ℹ Migrated legacy tasks from {legacy} into {path}: {counts}")
    _upgrade(conn)
    conn.commit()


def _upgrade(conn) -> None:
    version = int(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0])
    if version < 2:
        # Flip priority so higher = more important, including the values
        # recorded in the activity log.
        conn.execute("UPDATE tasks SET priority = 100 - priority")
        for key in ("priority", "old_priority", "new_priority"):
            conn.execute(
                f"UPDATE activity SET data = json_set(data, '$.{key}', 100 - json_extract(data, '$.{key}')) "
                f"WHERE json_type(data, '$.{key}') = 'integer'"
            )
    conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),))


@contextmanager
def connect():
    """Yield a connection; commits on success, rolls back on error."""
    path = db_path()
    conn = _open(path)
    try:
        key = str(path.resolve())
        if key not in _initialized:
            _init(conn, path)
            _initialized.add(key)
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Small helpers ─────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "task"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def validate_date(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ToledoError(f"Invalid date '{value}' (expected YYYY-MM-DD)")
    return value


def _unique_slug(conn, name: str, parent_id: int | None, exclude_id: int | None = None) -> str:
    base = slugify(name)
    slug, n = base, 2
    while True:
        if parent_id is None:
            row = conn.execute(
                "SELECT id FROM tasks WHERE parent_id IS NULL AND slug = ?", (slug,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM tasks WHERE parent_id = ? AND slug = ?", (parent_id, slug)
            ).fetchone()
        if not row or row["id"] == exclude_id:
            return slug
        slug, n = f"{base}-{n}", n + 1


def _log(conn, task_id: int, action: str, ts: str | None = None, **data) -> None:
    ts = ts or now_iso()
    data = {k: v for k, v in data.items() if v is not None}
    conn.execute(
        "INSERT INTO activity (task_id, ts, action, data) VALUES (?, ?, ?, ?)",
        (task_id, ts, action, json.dumps(data)),
    )
    conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (ts, task_id))


def _touch(conn, task_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id))


# ── Row → dict ────────────────────────────────────────────────────────────────

def _base_dict(row) -> dict:
    due = row["due"]
    return {
        "id":         row["id"],
        "parent_id":  row["parent_id"],
        "slug":       row["slug"],
        "name":       row["name"],
        "state":      row["state"],
        "priority":   row["priority"],
        "project":    row["project"],
        "due":        due,
        "recurrence": row["recurrence"],
        "overdue":    bool(due and due < today()),
        "created":    row["created_at"],
        "updated":    row["updated_at"],
    }


def _subtasks(conn, parent_ids: list[int]) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = {pid: [] for pid in parent_ids}
    if not parent_ids:
        return out
    marks = ",".join("?" * len(parent_ids))
    rows = conn.execute(
        f"SELECT * FROM tasks WHERE parent_id IN ({marks}) ORDER BY priority DESC, id",
        parent_ids,
    ).fetchall()
    for r in rows:
        out[r["parent_id"]].append(_base_dict(r))
    return out


def _dicts(conn, rows) -> list[dict]:
    tasks = [_base_dict(r) for r in rows]
    subs = _subtasks(conn, [t["id"] for t in tasks])
    for t in tasks:
        t["subtasks"] = subs[t["id"]]
    return tasks


def _detail(conn, row) -> dict:
    d = _dicts(conn, [row])[0]
    d["description"] = row["description"]
    d["notes"] = [
        {"ts": r["ts"], "text": r["text"], "source": r["source"]}
        for r in conn.execute(
            "SELECT ts, text, source FROM notes WHERE task_id = ? ORDER BY ts, id", (row["id"],)
        )
    ]
    d["activity"] = [
        {"ts": r["ts"], "action": r["action"], **json.loads(r["data"] or "{}")}
        for r in conn.execute(
            "SELECT ts, action, data FROM activity WHERE task_id = ? ORDER BY ts, id", (row["id"],)
        )
    ]
    return d


def render_worklog(notes: list[dict]) -> str:
    """Notes as the legacy worklog.md text ('### ts' headed entries)."""
    parts = []
    for n in notes:
        ts = (n["ts"] or "")[:16].replace("T", " ")
        if n.get("source") == "chat":
            ts += " (Chat)"
        parts.append(f"### {ts}\n\n{n['text'].strip()}\n")
    return "\n".join(parts)


# ── Task lookup ───────────────────────────────────────────────────────────────

_STATE_ORDER = "CASE state WHEN 'active' THEN 0 WHEN 'completed' THEN 1 ELSE 2 END"


def _find_row(conn, ref: str):
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref.startswith("#") and ref[1:].isdigit():
        return conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND parent_id IS NULL", (int(ref[1:]),)
        ).fetchone()
    row = conn.execute(
        "SELECT * FROM tasks WHERE parent_id IS NULL AND slug = ?", (ref.lower(),)
    ).fetchone()
    if row:
        return row
    row = conn.execute(
        "SELECT t.* FROM task_aliases a JOIN tasks t ON t.id = a.task_id "
        "WHERE t.parent_id IS NULL AND a.slug = ? "
        f"ORDER BY {_STATE_ORDER}, t.id DESC LIMIT 1",
        (ref.lower(),),
    ).fetchone()
    if row:
        return row
    # Partial match on slug, name, or former slugs; active first, then highest priority.
    needle = _norm(ref)
    if not needle:
        return None
    rows = conn.execute(
        f"SELECT * FROM tasks WHERE parent_id IS NULL ORDER BY {_STATE_ORDER}, priority DESC, id"
    ).fetchall()
    for r in rows:
        if needle in _norm(r["slug"]) or needle in _norm(r["name"]):
            return r
    alias = conn.execute(
        "SELECT t.* FROM task_aliases a JOIN tasks t ON t.id = a.task_id "
        "WHERE t.parent_id IS NULL AND a.slug LIKE ? "
        f"ORDER BY {_STATE_ORDER}, t.priority DESC, t.id LIMIT 1",
        (f"%{needle.replace(' ', '-')}%",),
    ).fetchone()
    return alias


def _resolve(conn, ref: str):
    row = _find_row(conn, ref)
    if not row:
        raise NotFound(f"No task matching '{ref}'")
    return row


def find_task(ref: str) -> dict | None:
    with connect() as c:
        row = _find_row(c, ref)
        return _dicts(c, [row])[0] if row else None


def resolve_task(ref: str) -> dict:
    with connect() as c:
        return _dicts(c, [_resolve(c, ref)])[0]


def get_task(ref: str) -> dict:
    with connect() as c:
        return _detail(c, _resolve(c, ref))


def _find_subtasks(conn, parent_id: int, ref: str, states=SUBTASK_STATES) -> list:
    """Exact slug matches if any, otherwise partial matches on slug/name/alias."""
    ref = (ref or "").strip()
    if not ref:
        raise ToledoError("subtask is required")
    marks = ",".join("?" * len(states))
    rows = conn.execute(
        f"SELECT * FROM tasks WHERE parent_id = ? AND state IN ({marks}) ORDER BY priority DESC, id",
        (parent_id, *states),
    ).fetchall()
    exact = [r for r in rows if r["slug"] == ref.lower()]
    if exact:
        return exact
    ids = {r["id"] for r in rows}
    aliased = {
        a["task_id"] for a in conn.execute(
            "SELECT a.task_id FROM task_aliases a JOIN tasks t ON t.id = a.task_id "
            "WHERE t.parent_id = ? AND a.slug = ?", (parent_id, ref.lower()),
        )
    } & ids
    if aliased:
        return [r for r in rows if r["id"] in aliased]
    needle = _norm(ref)
    return [r for r in rows if needle in _norm(r["slug"]) or needle in _norm(r["name"])]


def _resolve_subtask(conn, parent_id: int, ref: str, states=SUBTASK_STATES, first=False):
    matches = _find_subtasks(conn, parent_id, ref, states)
    if not matches:
        scope = "active subtask" if list(states) == ["active"] else "subtask"
        raise NotFound(f"No {scope} matching '{ref}'")
    if len(matches) > 1 and not first:
        names = ", ".join(f"{r['slug']} ({r['state']})" for r in matches)
        raise ToledoError(f"'{ref}' is ambiguous — matches: {names}")
    return matches[0]


# ── Queries ───────────────────────────────────────────────────────────────────

def list_tasks(states=None, project: str | None = None) -> list[dict]:
    states = list(states or STATES)
    marks = ",".join("?" * len(states))
    sql = f"SELECT * FROM tasks WHERE parent_id IS NULL AND state IN ({marks})"
    params: list = list(states)
    if project:
        sql += " AND project = ?"
        params.append(project.upper())
    sql += f" ORDER BY {_STATE_ORDER}, priority DESC, id"
    with connect() as c:
        return _dicts(c, c.execute(sql, params).fetchall())


def upcoming(days: int = 7) -> list[dict]:
    cutoff = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM tasks WHERE parent_id IS NULL AND state = 'active' "
            "AND due IS NOT NULL AND due <= ? ORDER BY due, priority DESC",
            (cutoff,),
        ).fetchall()
        return _dicts(c, rows)


def search(query: str) -> list[dict]:
    """Tasks whose name/slug, description, or notes contain query.
    Each result carries 'hits': which of name/description/notes matched."""
    q = (query or "").strip().lower()
    if not q:
        return []
    qn = _norm(q)
    like = f"%{q}%"
    with connect() as c:
        rows = c.execute(
            f"SELECT * FROM tasks WHERE parent_id IS NULL ORDER BY {_STATE_ORDER}, priority DESC, id"
        ).fetchall()
        noted = {
            r["task_id"] for r in c.execute(
                "SELECT DISTINCT task_id FROM notes WHERE lower(text) LIKE ?", (like,)
            )
        }
        hits_by_id = {}
        for r in rows:
            hits = []
            if q in r["slug"] or q in r["name"].lower() or (qn and qn in _norm(r["name"])):
                hits.append("name")
            if q in (r["description"] or "").lower():
                hits.append("description")
            if r["id"] in noted:
                hits.append("notes")
            if hits:
                hits_by_id[r["id"]] = (r, hits)
        results = _dicts(c, [r for r, _ in hits_by_id.values()])
        for d in results:
            d["hits"] = hits_by_id[d["id"]][1]
        return results


# ── Task mutations ────────────────────────────────────────────────────────────

def create_task(name: str, project: str | None = None, priority: int | None = None,
                due: str | None = None, recurrence: int | None = None,
                description: str | None = None, source: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ToledoError("name is required")
    project = (project or DEFAULT_PROJECT).upper().strip()
    priority = int(priority or DEFAULT_PRIORITY)
    if due:
        validate_date(due)
    recurrence = int(recurrence) if recurrence else None
    ts = now_iso()
    with connect() as c:
        slug = _unique_slug(c, name, None)
        cur = c.execute(
            "INSERT INTO tasks (slug, name, state, priority, project, due, recurrence, "
            "description, created_at, updated_at) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)",
            (slug, name, priority, project, due or None, recurrence,
             description or "", ts, ts),
        )
        tid = cur.lastrowid
        _log(c, tid, "created", ts, state="active", priority=priority, project=project, source=source)
        return _dicts(c, [c.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()])[0]


def complete_task(ref: str, note: str | None = None, source: str | None = None) -> dict:
    """Complete a task. A recurring task instead advances its due date by its
    interval (from the current due date, or today if it has none).
    Returns {"task": dict, "recurring": bool, "next_due": str|None}."""
    with connect() as c:
        row = _resolve(c, ref)
        tid = row["id"]
        result = {"recurring": False, "next_due": None}
        if row["recurrence"]:
            base = datetime.strptime(row["due"], "%Y-%m-%d") if row["due"] else datetime.now()
            next_due = (base + timedelta(days=row["recurrence"])).strftime("%Y-%m-%d")
            _touch(c, tid, due=next_due)
            _log(c, tid, "completed_recurring", next_due=next_due, source=source)
            result.update(recurring=True, next_due=next_due)
        else:
            _touch(c, tid, state="completed")
            _log(c, tid, "state_changed", from_state=row["state"], to_state="completed", source=source)
            _clear_context_if(c, tid)
        if note and note.strip():
            _add_note(c, tid, note.strip(), source)
        result["task"] = _dicts(c, [c.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()])[0]
        return result


def move_task(ref: str, state: str, source: str | None = None) -> dict:
    if state not in STATES:
        raise ToledoError(f"state must be one of {STATES}")
    with connect() as c:
        row = _resolve(c, ref)
        if row["state"] != state:
            _touch(c, row["id"], state=state)
            _log(c, row["id"], "state_changed", from_state=row["state"], to_state=state, source=source)
            if state != "active":
                _clear_context_if(c, row["id"])
        return _dicts(c, [c.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()])[0]


def cancel_recurrence(ref: str, source: str | None = None) -> dict:
    """Stop a recurring task for good: clears recurrence and completes it."""
    with connect() as c:
        row = _resolve(c, ref)
        if not row["recurrence"]:
            raise ToledoError(f"'{row['slug']}' is not a recurring task")
        _touch(c, row["id"], recurrence=None, state="completed")
        _log(c, row["id"], "recurring_cancelled", source=source)
        _clear_context_if(c, row["id"])
        return _dicts(c, [c.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()])[0]


def delete_task(ref: str) -> dict:
    with connect() as c:
        row = _resolve(c, ref)
        d = _base_dict(row)
        _clear_context_if(c, row["id"])
        c.execute("DELETE FROM tasks WHERE id = ?", (row["id"],))
        return d


def _set_field(ref: str, field: str, value, action: str, source=None, **log) -> dict:
    with connect() as c:
        row = _resolve(c, ref)
        _touch(c, row["id"], **{field: value})
        _log(c, row["id"], action, source=source, **log)
        return _dicts(c, [c.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()])[0]


def rename_task(ref: str, name: str, source: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ToledoError("name is required")
    with connect() as c:
        row = _resolve(c, ref)
        slug = _unique_slug(c, name, None, exclude_id=row["id"])
        if slug != row["slug"]:
            c.execute("INSERT INTO task_aliases (task_id, slug) VALUES (?, ?)", (row["id"], row["slug"]))
        _touch(c, row["id"], name=name, slug=slug)
        _log(c, row["id"], "renamed", old=row["name"], new=name, source=source)
        return _dicts(c, [c.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()])[0]


def set_priority(ref: str, priority: int, source: str | None = None) -> dict:
    priority = int(priority)
    with connect() as c:
        row = _resolve(c, ref)
        if row["priority"] == priority:
            raise ToledoError(f"Task already has priority {priority}")
        _touch(c, row["id"], priority=priority)
        _log(c, row["id"], "reprioritized", old_priority=row["priority"],
             new_priority=priority, source=source)
        return _dicts(c, [c.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()])[0]


def set_project(ref: str, project: str, source: str | None = None) -> dict:
    project = (project or "").upper().strip()
    if not project:
        raise ToledoError("project is required")
    with connect() as c:
        row = _resolve(c, ref)
        if row["project"] == project:
            raise ToledoError(f"Task is already in project {project}")
        _touch(c, row["id"], project=project)
        c.execute("UPDATE tasks SET project = ? WHERE parent_id = ?", (project, row["id"]))
        _log(c, row["id"], "reprojected", old_project=row["project"], new_project=project, source=source)
        return _dicts(c, [c.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()])[0]


def set_due(ref: str, due: str | None, source: str | None = None) -> dict:
    due = (due or "").strip() or None
    if due:
        validate_date(due)
        return _set_field(ref, "due", due, "due_date_set", source, date=due)
    return _set_field(ref, "due", None, "due_cleared", source)


def set_recurrence(ref: str, interval: int | None, source: str | None = None) -> dict:
    interval = int(interval or 0)
    if interval < 0:
        raise ToledoError("interval must be 0 or a positive number of days")
    if interval:
        return _set_field(ref, "recurrence", interval, "recurrence_set", source, interval=interval)
    return _set_field(ref, "recurrence", None, "recurrence_cleared", source)


def set_description(ref: str, text: str, source: str | None = None) -> dict:
    with connect() as c:
        row = _resolve(c, ref)
        if row["description"]:
            c.execute(
                "INSERT INTO description_history (task_id, ts, text) VALUES (?, ?, ?)",
                (row["id"], now_iso(), row["description"]),
            )
        _touch(c, row["id"], description=text or "")
        _log(c, row["id"], "description_updated", source=source)
        return _dicts(c, [c.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()])[0]


def _add_note(conn, task_id: int, text: str, source: str | None) -> None:
    ts = now_iso()
    conn.execute(
        "INSERT INTO notes (task_id, ts, text, source) VALUES (?, ?, ?, ?)",
        (task_id, ts, text, source),
    )
    _log(conn, task_id, "note_added", ts, source=source)


def add_note(ref: str, text: str, source: str | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise ToledoError("note text is required")
    with connect() as c:
        row = _resolve(c, ref)
        _add_note(c, row["id"], text, source)
        return _base_dict(row)


# ── Subtasks ──────────────────────────────────────────────────────────────────

def add_subtask(ref: str, name: str, priority: int | None = None, due: str | None = None,
                description: str | None = None, source: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ToledoError("subtask name is required")
    if due:
        validate_date(due)
    ts = now_iso()
    with connect() as c:
        parent = _resolve(c, ref)
        slug = _unique_slug(c, name, parent["id"])
        cur = c.execute(
            "INSERT INTO tasks (parent_id, slug, name, state, priority, project, due, "
            "description, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
            (parent["id"], slug, name, int(priority or DEFAULT_PRIORITY), parent["project"],
             due or None, description or "", ts, ts),
        )
        _log(c, parent["id"], "subtask_created", ts, subtask=slug, source=source)
        return {"parent": _base_dict(parent),
                **_base_dict(c.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone())}


def set_subtask_state(ref: str, sub_ref: str, state: str, source: str | None = None,
                      first_match: bool = False) -> dict:
    """Complete (state='completed') or reopen (state='active') a subtask."""
    if state not in SUBTASK_STATES:
        raise ToledoError(f"state must be one of {SUBTASK_STATES}")
    other = "active" if state == "completed" else "completed"
    with connect() as c:
        parent = _resolve(c, ref)
        sub = _resolve_subtask(c, parent["id"], sub_ref, [other], first=first_match)
        _touch(c, sub["id"], state=state, updated_at=now_iso())
        action = "subtask_completed" if state == "completed" else "subtask_reopened"
        _log(c, parent["id"], action, subtask=sub["slug"], source=source)
        return {"parent": _base_dict(parent), **_base_dict(sub), "state": state}


def rename_subtask(ref: str, sub_ref: str, name: str, source: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ToledoError("name is required")
    with connect() as c:
        parent = _resolve(c, ref)
        sub = _resolve_subtask(c, parent["id"], sub_ref)
        slug = _unique_slug(c, name, parent["id"], exclude_id=sub["id"])
        if slug != sub["slug"]:
            c.execute("INSERT INTO task_aliases (task_id, slug) VALUES (?, ?)", (sub["id"], sub["slug"]))
        _touch(c, sub["id"], name=name, slug=slug, updated_at=now_iso())
        _log(c, parent["id"], "subtask_renamed", old=sub["slug"], new=slug, source=source)
        return {"parent": _base_dict(parent), "slug": slug, "name": name}


def delete_subtask(ref: str, sub_ref: str, source: str | None = None) -> dict:
    with connect() as c:
        parent = _resolve(c, ref)
        sub = _resolve_subtask(c, parent["id"], sub_ref)
        c.execute("DELETE FROM tasks WHERE id = ?", (sub["id"],))
        _log(c, parent["id"], "subtask_deleted", subtask=sub["slug"], state=sub["state"], source=source)
        return {"parent": _base_dict(parent), **_base_dict(sub)}


# ── Projects ──────────────────────────────────────────────────────────────────

def list_projects() -> dict[str, dict]:
    with connect() as c:
        return {
            r["code"]: {"name": r["name"], "color": r["color"]}
            for r in c.execute("SELECT * FROM projects ORDER BY code")
        }


def project_name(code: str) -> str:
    with connect() as c:
        row = c.execute("SELECT name FROM projects WHERE code = ?", (code,)).fetchone()
        return row["name"] if row else code


def save_project(code: str, name: str, color: str | None = None) -> None:
    """Add a project, or update the name (and color, if given) of an existing one."""
    code, name = (code or "").upper().strip(), (name or "").strip()
    if not code or not name:
        raise ToledoError("code and name are required")
    with connect() as c:
        c.execute(
            "INSERT INTO projects (code, name, color) VALUES (?, ?, ?) "
            "ON CONFLICT(code) DO UPDATE SET name = excluded.name, "
            "color = CASE WHEN ? IS NULL THEN projects.color ELSE excluded.color END",
            (code, name, color or "", color),
        )


def update_project(code: str, name: str | None = None, color: str | None = None) -> None:
    code = code.upper()
    with connect() as c:
        if not c.execute("SELECT 1 FROM projects WHERE code = ?", (code,)).fetchone():
            raise NotFound(f"Project '{code}' not found")
        if name is not None:
            c.execute("UPDATE projects SET name = ? WHERE code = ?", (name, code))
        if color is not None:
            c.execute("UPDATE projects SET color = ? WHERE code = ?", (color, code))


def remove_project(code: str) -> None:
    code = code.upper().strip()
    with connect() as c:
        if not c.execute("DELETE FROM projects WHERE code = ?", (code,)).rowcount:
            raise NotFound(f"Project '{code}' not found")


def _move_project_tasks(conn, old: str, new: str) -> int:
    top = conn.execute(
        "SELECT id FROM tasks WHERE parent_id IS NULL AND project = ?", (old,)
    ).fetchall()
    conn.execute("UPDATE tasks SET project = ? WHERE project = ?", (new, old))
    for r in top:
        _log(conn, r["id"], "project_renamed", old_project=old, new_project=new)
    return len(top)


def rename_project(old: str, new: str) -> int:
    """Change a project's code, carrying its tasks along. Returns tasks moved."""
    old, new = old.upper().strip(), new.upper().strip()
    if not old or not new:
        raise ToledoError("old_code and new_code are required")
    if old == new:
        raise ToledoError("old_code and new_code are the same")
    with connect() as c:
        if not c.execute("SELECT 1 FROM projects WHERE code = ?", (old,)).fetchone():
            raise NotFound(f"Project '{old}' not found")
        if c.execute("SELECT 1 FROM projects WHERE code = ?", (new,)).fetchone():
            raise Conflict(f"Project '{new}' already exists — use merge_projects instead")
        c.execute("UPDATE projects SET code = ? WHERE code = ?", (new, old))
        return _move_project_tasks(c, old, new)


def merge_projects(from_code: str, into_code: str) -> int:
    """Move every task in from_code into into_code and drop from_code."""
    src, dst = from_code.upper().strip(), into_code.upper().strip()
    if not src or not dst:
        raise ToledoError("from_code and into_code are required")
    if src == dst:
        raise ToledoError("from_code and into_code are the same")
    with connect() as c:
        if not c.execute("SELECT 1 FROM projects WHERE code = ?", (src,)).fetchone():
            raise NotFound(f"Project '{src}' not found")
        if not c.execute("SELECT 1 FROM projects WHERE code = ?", (dst,)).fetchone():
            raise NotFound(f"Project '{dst}' not found — create it first with add_project")
        count = _move_project_tasks(c, src, dst)
        c.execute("DELETE FROM projects WHERE code = ?", (src,))
        return count


# ── Glossary ──────────────────────────────────────────────────────────────────

def load_glossary() -> dict[str, str]:
    with connect() as c:
        return {r["term"]: r["canonical"] for r in c.execute("SELECT * FROM glossary ORDER BY term")}


def set_glossary_term(term: str, canonical: str) -> None:
    term, canonical = (term or "").strip(), (canonical or "").strip()
    if not term or not canonical:
        raise ToledoError("term and canonical are required")
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO glossary VALUES (?, ?)", (term.lower(), canonical))


# ── Context (the "current task" pointer) ──────────────────────────────────────

def get_context() -> str | None:
    with connect() as c:
        row = c.execute("SELECT value FROM meta WHERE key = 'context'").fetchone()
        if not row or not row["value"]:
            return None
        t = c.execute("SELECT slug FROM tasks WHERE id = ?", (int(row["value"]),)).fetchone()
        return t["slug"] if t else None


def set_context(ref: str | None) -> str | None:
    with connect() as c:
        if not ref:
            c.execute("DELETE FROM meta WHERE key = 'context'")
            return None
        row = _resolve(c, ref)
        c.execute("INSERT OR REPLACE INTO meta VALUES ('context', ?)", (str(row["id"]),))
        return row["slug"]


def _clear_context_if(conn, task_id: int) -> None:
    conn.execute("DELETE FROM meta WHERE key = 'context' AND value = ?", (str(task_id),))


# ── Legacy file-tree migration ────────────────────────────────────────────────

def _parse_legacy_slug(slug: str) -> dict:
    """'50-PRJ-some-name' → priority/project/name-slug. Legacy CLI subtasks
    were '50-some-name' (no project), recognised by a non-uppercase 2nd part."""
    parts = slug.split("-", 2)
    pri = int(parts[0]) if parts and parts[0].isdigit() else DEFAULT_PRIORITY
    if len(parts) >= 3 and parts[1].isupper() and parts[1].isalpha():
        return {"priority": pri, "project": parts[1], "name_slug": parts[2]}
    rest = slug.split("-", 1)[1] if parts[0].isdigit() and "-" in slug else slug
    return {"priority": pri, "project": None, "name_slug": rest}


def _legacy_name(name_slug: str, description: str) -> str:
    """Recover the original casing from the description's '# Title' when it
    matches the slug; otherwise fall back to the slug with spaces."""
    m = re.match(r"#\s+(.+)", description or "")
    if m and slugify(m.group(1)) == name_slug:
        return m.group(1).strip()
    return name_slug.replace("-", " ")


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except (FileNotFoundError, IsADirectoryError):
        return None


def _parse_worklog(text: str) -> list[tuple[str | None, str, str | None]]:
    """worklog.md → [(ts_iso|None, text, source)]."""
    out = []
    for chunk in re.split(r"(?:^|\n)(?=### )", text or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"### (\d{4}-\d{2}-\d{2} \d{2}:\d{2})( \(Chat\))?[^\n]*\n?([\s\S]*)", chunk)
        if m:
            out.append((m.group(1).replace(" ", "T") + ":00", m.group(3).strip(),
                        "chat" if m.group(2) else None))
        else:
            out.append((None, chunk, None))
    return [(ts, body, src) for ts, body, src in out if body]


def _migrate_task(conn, folder: Path, state: str, parent: dict | None) -> int:
    info = _parse_legacy_slug(folder.name)
    desc = _read(folder / "description.md") or ""
    name = _legacy_name(info["name_slug"], desc)
    project = (parent["project"] if parent else None) or info["project"] or DEFAULT_PROJECT
    due = (_read(folder / "due.txt") or "").strip() or None
    rec = (_read(folder / "recurrence.txt") or "").strip()
    recurrence = int(rec) if rec.isdigit() and int(rec) > 0 else None

    activity = []
    for line in (_read(folder / "activity.log") or "").splitlines():
        try:
            activity.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    stamps = [e["ts"] for e in activity if e.get("ts")]
    mtime = datetime.fromtimestamp(folder.stat().st_mtime).isoformat(timespec="seconds")
    created = min(stamps) if stamps else mtime
    updated = max(stamps) if stamps else mtime

    parent_id = parent["id"] if parent else None
    slug = _unique_slug(conn, name, parent_id)
    cur = conn.execute(
        "INSERT INTO tasks (parent_id, slug, name, state, priority, project, due, recurrence, "
        "description, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (parent_id, slug, name, state, info["priority"], project, due, recurrence,
         desc, created, updated),
    )
    tid = cur.lastrowid
    conn.execute("INSERT INTO task_aliases (task_id, slug) VALUES (?, ?)", (tid, folder.name.lower()))

    for e in activity:
        e = dict(e)
        ts, action = e.pop("ts", created), e.pop("action", "unknown")
        conn.execute("INSERT INTO activity (task_id, ts, action, data) VALUES (?, ?, ?, ?)",
                     (tid, ts, action, json.dumps(e)))

    for ts, body, src in _parse_worklog(_read(folder / "worklog.md") or ""):
        conn.execute("INSERT INTO notes (task_id, ts, text, source) VALUES (?, ?, ?, ?)",
                     (tid, ts or created, body, src))

    archive = folder / "description_archive"
    if archive.is_dir():
        for f in sorted(archive.iterdir()):
            m = re.search(r"(\d{8})_(\d{6})", f.name)
            ts = (datetime.strptime("".join(m.groups()), "%Y%m%d%H%M%S").isoformat()
                  if m else created)
            conn.execute("INSERT INTO description_history (task_id, ts, text) VALUES (?, ?, ?)",
                         (tid, ts, f.read_text()))

    for ss in SUBTASK_STATES:
        sub_dir = folder / "subtasks" / ss
        if sub_dir.is_dir():
            for sub in sorted(sub_dir.iterdir()):
                if sub.is_dir():
                    _migrate_task(conn, sub, ss, {"id": tid, "project": project})
    return tid


def migrate_from_files(conn, tasks_dir: Path, context_file: Path | None = None) -> dict:
    """Load a legacy file tree into an (empty) database. Returns counts."""
    tasks_dir = Path(tasks_dir)
    counts = {"projects": 0, "tasks": 0, "subtasks": 0, "notes": 0, "glossary": 0}

    pf = tasks_dir / "projects.json"
    if pf.exists():
        for code, val in json.loads(pf.read_text()).items():
            if isinstance(val, str):
                val = {"name": val, "color": ""}
            conn.execute("INSERT OR REPLACE INTO projects VALUES (?, ?, ?)",
                         (code.upper(), val.get("name") or code, val.get("color") or ""))
            counts["projects"] += 1

    gf = tasks_dir / "glossary.json"
    if gf.exists():
        for term, canonical in json.loads(gf.read_text()).items():
            conn.execute("INSERT OR REPLACE INTO glossary VALUES (?, ?)", (term.lower(), canonical))
            counts["glossary"] += 1

    slug_to_id = {}
    for state in STATES:
        sd = tasks_dir / state
        if not sd.is_dir():
            continue
        # Oldest first, so where two legacy tasks share a name the older one
        # keeps the bare slug and the newer one gets the -2 suffix.
        folders = sorted((f for f in sd.iterdir() if f.is_dir()), key=lambda f: f.stat().st_mtime)
        for folder in folders:
            slug_to_id[folder.name] = _migrate_task(conn, folder, state, None)

    counts["tasks"] = conn.execute("SELECT count(*) FROM tasks WHERE parent_id IS NULL").fetchone()[0]
    counts["subtasks"] = conn.execute("SELECT count(*) FROM tasks WHERE parent_id IS NOT NULL").fetchone()[0]
    counts["notes"] = conn.execute("SELECT count(*) FROM notes").fetchone()[0]

    ctx = (_read(context_file) or "").strip() if context_file else ""
    if ctx in slug_to_id:
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('context', ?)", (str(slug_to_id[ctx]),))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('migrated_from', ?)", (str(tasks_dir),))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('migrated_at', ?)", (now_iso(),))
    return counts


# ── CLI (maintenance only) ────────────────────────────────────────────────────

def main():
    import argparse
    p = argparse.ArgumentParser(description="Toledo database maintenance")
    s = p.add_subparsers(dest="cmd", required=True)
    m = s.add_parser("migrate", help="Import a legacy file tree into a new database")
    m.add_argument("--tasks-dir", default=str(LEGACY_TASKS_DIR))
    m.add_argument("--context-file", default=str(LEGACY_CONTEXT_FILE))
    m.add_argument("--db", help="Target database (default: configured db path)")
    args = p.parse_args()

    if args.cmd == "migrate":
        target = Path(args.db) if args.db else db_path()
        if target.exists():
            p.error(f"{target} already exists; migrate only into a new database")
        conn = _open(target)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', '1')")
        counts = migrate_from_files(conn, Path(args.tasks_dir), Path(args.context_file))
        _upgrade(conn)
        conn.commit()
        conn.close()
        print(f"✓ Migrated {args.tasks_dir} → {target}: {counts}")


if __name__ == "__main__":
    main()
