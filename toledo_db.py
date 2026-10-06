#!/usr/bin/env python3
"""
Toledo SQLite store — the single source of truth for tasks, projects, tags,
notes, activity, glossary and context. The web server and MCP server both go through
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
import subprocess
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
# Schema 3 repoints tasks whose project was stored as an unregistered string.
# Schema 4 turns activity into a global event log (see the table below).
# Schema 5 adds task tags.
SCHEMA_VERSION = 5

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
-- project holds a projects.code. It is not a foreign key so project codes
-- can be rewritten in bulk, but every write resolves it against the registry.
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

-- Free-form tags on top-level tasks: a tag exists while some task has it.
-- Subtasks carry none of their own and show their parent's.
CREATE TABLE IF NOT EXISTS task_tags (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    tag     TEXT NOT NULL,
    PRIMARY KEY (task_id, tag)
);
CREATE INDEX IF NOT EXISTS task_tags_tag ON task_tags(tag);

CREATE TABLE IF NOT EXISTS notes (
    id      INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    ts      TEXT NOT NULL,
    text    TEXT NOT NULL,
    source  TEXT
);
CREATE INDEX IF NOT EXISTS notes_task ON notes(task_id);

-- The event log. scope says what an event is about; task events carry task_id,
-- the rest name their target in ref (project code, glossary term, journal id).
-- History outlives its task: deleting one only clears task_id, and task events
-- keep a slug/name snapshot in data. Its indexes are made in _upgrade.
CREATE TABLE IF NOT EXISTS activity (
    id      INTEGER PRIMARY KEY,
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    ts      TEXT NOT NULL,
    scope   TEXT NOT NULL DEFAULT 'task',
    action  TEXT NOT NULL,
    ref     TEXT,
    data    TEXT NOT NULL DEFAULT '{}'
);

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

-- Journal entries: the raw dump as given and its revised summary. entry_date
-- is the submission date unless the entry was backfilled for another day.
CREATE TABLE IF NOT EXISTS journal (
    id         INTEGER PRIMARY KEY,
    entry_date TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    raw        TEXT NOT NULL DEFAULT '',
    summary    TEXT NOT NULL DEFAULT '',
    source     TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS journal_date ON journal(entry_date);
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
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row and int(row[0]) < 4:
        _backup(conn, path, int(row[0]))
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


def _backup(conn, path: Path, version: int) -> None:
    """Copy the database aside before an upgrade that rebuilds a table."""
    target = path.with_name(f"{path.name}.schema{version}-{datetime.now():%Y%m%d-%H%M%S}.bak")
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
        print(f"ℹ Backed up {path} to {target} before upgrading to schema {SCHEMA_VERSION}")
    except sqlite3.OperationalError as e:
        # The other server may have started the same upgrade this second.
        print(f"⚠ Backup to {target} skipped: {e}")


def _upgrade(conn) -> None:
    version = int(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0])
    if version < 4:
        # Runs first, since later steps log events. SQLite cannot relax
        # NOT NULL or change an FK action in place, so rebuild activity with
        # the schema-4 columns, keeping ids. Nothing references activity, so
        # this is safe with foreign keys on. Fresh databases already have
        # the new table.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(activity)")}
        if "scope" not in cols:
            conn.execute("""
                CREATE TABLE activity_v4 (
                    id      INTEGER PRIMARY KEY,
                    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
                    ts      TEXT NOT NULL,
                    scope   TEXT NOT NULL DEFAULT 'task',
                    action  TEXT NOT NULL,
                    ref     TEXT,
                    data    TEXT NOT NULL DEFAULT '{}'
                )""")
            conn.execute("INSERT INTO activity_v4 (id, task_id, ts, scope, action, data) "
                         "SELECT id, task_id, ts, 'task', action, data FROM activity")
            conn.execute("DROP TABLE activity")
            conn.execute("ALTER TABLE activity_v4 RENAME TO activity")
        conn.execute("CREATE INDEX IF NOT EXISTS activity_task ON activity(task_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS activity_ts ON activity(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS activity_scope ON activity(scope, ts)")
    if version < 2:
        # Flip priority so higher = more important, including the values
        # recorded in the activity log.
        conn.execute("UPDATE tasks SET priority = 100 - priority")
        for key in ("priority", "old_priority", "new_priority"):
            conn.execute(
                f"UPDATE activity SET data = json_set(data, '$.{key}', 100 - json_extract(data, '$.{key}')) "
                f"WHERE json_type(data, '$.{key}') = 'integer'"
            )
    if version < 3:
        # Writes used to store the project argument verbatim, so a project
        # given by name ('Travel') landed as an unregistered 'TRAVEL'. Point
        # those at the project they named, and register anything left over.
        orphans = [r[0] for r in conn.execute(
            "SELECT DISTINCT project FROM tasks WHERE project NOT IN (SELECT code FROM projects)"
        )]
        for orphan in orphans:
            row = conn.execute(
                "SELECT code FROM projects WHERE name = ? COLLATE NOCASE", (orphan,)
            ).fetchone()
            if row:
                _move_project_tasks(conn, orphan, row[0])
            else:
                conn.execute("INSERT INTO projects (code, name) VALUES (?, ?)",
                             (orphan, orphan.title()))
    if version < SCHEMA_VERSION:
        _log_event(conn, "system", "schema_upgraded", from_version=version, to_version=SCHEMA_VERSION)
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

def release_version() -> dict:
    """Code identity for this running process: the commit it was built from and
    when that build happened. Both the web server and MCP server call this so
    they agree on the same answer. Sourced from $TOLEDO_COMMIT / $TOLEDO_BUILD_TIME
    (set by the Docker build — the image has no .git to read them from), falling
    back to the local checkout's HEAD for a dev run outside Docker."""
    commit = os.environ.get("TOLEDO_COMMIT", "").strip()[:12]
    built = os.environ.get("TOLEDO_BUILD_TIME", "").strip()
    if not (commit and built):
        try:
            out = subprocess.run(
                ["git", "log", "-1", "--format=%h|%cI"], cwd=Path(__file__).parent,
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                git_commit, git_built = out.stdout.strip().split("|", 1)
                commit = commit or git_commit
                built = built or git_built
        except (OSError, subprocess.SubprocessError):
            pass
    return {"commit": commit or "unknown", "built": built or "unknown"}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "task"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


# Priority labels as shown to users and MCP clients, each mapped to a value
# that reads back as the same label. Writes accept them in any case.
PRIORITY_KEYWORDS = {
    "Ultra High": 90,
    "High":       75,
    "Med-High":   60,
    "Medium":     50,
    "Med-Low":    40,
    "Low":        25,
    "Very Low":   10,
}


def _priority_key(text: str) -> str:
    key = " ".join(text.lower().replace("-", " ").replace("_", " ").split())
    return key.replace("medium ", "med ").replace("mid ", "med ")


_PRIORITY_LOOKUP = {_priority_key(k): v for k, v in PRIORITY_KEYWORDS.items()}


def parse_priority(value) -> int:
    """Priority as an int 1–99, from a number, a numeric string, or a label
    like 'High' or 'med-low' (case-insensitive)."""
    if isinstance(value, bool):
        raise ToledoError(f"Invalid priority '{value}'")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        n = value
    else:
        text = str(value).strip()
        if _priority_key(text) in _PRIORITY_LOOKUP:
            return _PRIORITY_LOOKUP[_priority_key(text)]
        try:
            n = int(text)
        except ValueError:
            labels = ", ".join(PRIORITY_KEYWORDS)
            raise ToledoError(f"Invalid priority '{value}' (expected 1–99 or one of: {labels})")
    if not 1 <= n <= 99:
        raise ToledoError(f"Priority {n} is out of range (expected 1–99)")
    return n


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


# ── Event log ─────────────────────────────────────────────────────────────────

SCOPES = ["task", "project", "tag", "glossary", "journal", "context", "system"]

# Who made a change, when the caller doesn't say: each server sets its own
# ('web', 'mcp') at startup; the web chat passes 'chat' explicitly.
_default_source: str | None = None


def set_default_source(source: str | None) -> None:
    global _default_source
    _default_source = source


def _insert_event(conn, scope: str, action: str, task_id: int | None = None,
                  ref: str | None = None, ts: str | None = None, **data) -> str:
    """Append one event. An event about a task snapshots its slug and name
    (and its parent's slug, for a subtask) so it stays readable after the
    task is deleted. Returns the timestamp used."""
    ts = ts or now_iso()
    if data.get("source") is None:
        data["source"] = _default_source
    if task_id is not None:
        t = conn.execute(
            "SELECT t.slug, t.name, p.slug AS parent FROM tasks t "
            "LEFT JOIN tasks p ON p.id = t.parent_id WHERE t.id = ?", (task_id,)
        ).fetchone()
        if t:
            data = {"slug": t["slug"], "name": t["name"], "parent": t["parent"], **data}
    data = {k: v for k, v in data.items() if v is not None}
    conn.execute(
        "INSERT INTO activity (task_id, ts, scope, action, ref, data) VALUES (?, ?, ?, ?, ?, ?)",
        (task_id, ts, scope, action, None if ref is None else str(ref), json.dumps(data)),
    )
    return ts


def _log_task(conn, task_id: int, action: str, ts: str | None = None, **data) -> None:
    """Log a change to a task and bump its updated_at."""
    ts = _insert_event(conn, "task", action, task_id=task_id, ts=ts, **data)
    conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (ts, task_id))


def _log_event(conn, scope: str, action: str, ref=None, task_id: int | None = None, **data) -> None:
    """Log an event that is not a change to a task."""
    _insert_event(conn, scope, action, task_id=task_id, ref=ref, **data)


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


def _tags(conn, task_ids) -> dict[int, list[str]]:
    ids = sorted(set(task_ids))
    out: dict[int, list[str]] = {tid: [] for tid in ids}
    if ids:
        for r in conn.execute(
            f"SELECT task_id, tag FROM task_tags WHERE task_id IN ({','.join('?' * len(ids))}) "
            "ORDER BY tag", ids,
        ):
            out[r["task_id"]].append(r["tag"])
    return out


def _dicts(conn, rows) -> list[dict]:
    """Task dicts with their tags and subtasks, and a subtask's parent
    (slug, name). A subtask's tags are its parent's."""
    tasks = [_base_dict(r) for r in rows]
    tags = _tags(conn, [t["parent_id"] or t["id"] for t in tasks])
    for t in tasks:
        t["tags"] = tags[t["parent_id"] or t["id"]]
    subs = _subtasks(conn, [t["id"] for t in tasks])
    parent_ids = sorted({t["parent_id"] for t in tasks if t["parent_id"]})
    parents = {
        r["id"]: {"slug": r["slug"], "name": r["name"]}
        for r in conn.execute(
            f"SELECT id, slug, name FROM tasks WHERE id IN ({','.join('?' * len(parent_ids))})",
            parent_ids,
        )
    } if parent_ids else {}
    for t in tasks:
        t["subtasks"] = subs[t["id"]]
        if t["parent_id"]:
            t["parent"] = parents.get(t["parent_id"])
    return tasks


# Keys _insert_event adds to describe the task an event belongs to.
_SNAPSHOT_KEYS = {"slug", "name", "parent"}


def _detail(conn, row) -> dict:
    d = _dicts(conn, [row])[0]
    d["description"] = row["description"]
    d["notes"] = [
        {"id": r["id"], "ts": r["ts"], "text": r["text"], "source": r["source"]}
        for r in conn.execute(
            "SELECT id, ts, text, source FROM notes WHERE task_id = ? ORDER BY ts, id", (row["id"],)
        )
    ]
    d["activity"] = [
        {"ts": r["ts"], "action": r["action"],
         **{k: v for k, v in json.loads(r["data"] or "{}").items() if k not in _SNAPSHOT_KEYS}}
        for r in conn.execute(
            "SELECT ts, action, data FROM activity WHERE task_id = ? ORDER BY ts, id", (row["id"],)
        )
    ]
    return d


def render_worklog(notes: list[dict], ids: bool = False) -> str:
    """Notes as the legacy worklog.md text ('### ts' headed entries);
    ids=True appends each note's '#id' so it can be edited or deleted."""
    parts = []
    for n in notes:
        ts = (n["ts"] or "")[:16].replace("T", " ")
        if n.get("source") == "chat":
            ts += " (Chat)"
        if ids:
            ts += f" (#{n['id']})"
        parts.append(f"### {ts}\n\n{n['text'].strip()}\n")
    return "\n".join(parts)


# ── Task lookup ───────────────────────────────────────────────────────────────

_STATE_ORDER = "CASE state WHEN 'active' THEN 0 WHEN 'completed' THEN 1 ELSE 2 END"


def _match(conn, ref: str):
    """Return (row, partial): the task ref names, plus every row it partially
    matched with the chosen one first (empty when ref named a task exactly by
    id, slug, or former slug).

    '#id' names any task or subtask by id and 'parent/child' names a subtask;
    any other ref names a top-level task. A 'parent/child' ref that names no
    subtask is tried as a whole, since task names can contain '/'."""
    ref = (ref or "").strip()
    if not ref:
        return None, []
    if ref.startswith("#") and ref[1:].isdigit():
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (int(ref[1:]),)).fetchone(), []
    if "/" in ref:
        head, tail = ref.split("/", 1)
        parent, partial = _match_top(conn, head)
        if parent and tail.strip():
            subs = _find_subtasks(conn, parent["id"], tail, STATES)
            if len(subs) > 1:
                names = ", ".join(f"{parent['slug']}/{r['slug']} ({r['state']})" for r in subs)
                raise ToledoError(f"'{ref}' is ambiguous — matches: {names}")
            if subs:
                # Other top-level tasks the parent part also matched.
                return subs[0], (subs + partial[1:]) if len(partial) > 1 else []
    return _match_top(conn, ref)


def _match_top(conn, ref: str):
    """_match for a plain ref: top-level tasks only."""
    ref = (ref or "").strip()
    if not ref:
        return None, []
    if ref.startswith("#") and ref[1:].isdigit():
        return conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND parent_id IS NULL", (int(ref[1:]),)
        ).fetchone(), []
    row = conn.execute(
        "SELECT * FROM tasks WHERE parent_id IS NULL AND slug = ?", (ref.lower(),)
    ).fetchone()
    if row:
        return row, []
    row = conn.execute(
        "SELECT t.* FROM task_aliases a JOIN tasks t ON t.id = a.task_id "
        "WHERE t.parent_id IS NULL AND a.slug = ? "
        f"ORDER BY {_STATE_ORDER}, t.id DESC LIMIT 1",
        (ref.lower(),),
    ).fetchone()
    if row:
        return row, []
    # Partial match on slug, name, or former slugs; active first, then highest priority.
    needle = _norm(ref)
    if not needle:
        return None, []
    rows = conn.execute(
        f"SELECT * FROM tasks WHERE parent_id IS NULL ORDER BY {_STATE_ORDER}, priority DESC, id"
    ).fetchall()
    matches = [r for r in rows if needle in _norm(r["slug"]) or needle in _norm(r["name"])]
    if matches:
        return matches[0], matches
    alias = conn.execute(
        "SELECT t.* FROM task_aliases a JOIN tasks t ON t.id = a.task_id "
        "WHERE t.parent_id IS NULL AND a.slug LIKE ? "
        f"ORDER BY {_STATE_ORDER}, t.priority DESC, t.id LIMIT 1",
        (f"%{needle.replace(' ', '-')}%",),
    ).fetchone()
    return alias, [alias] if alias else []


def _find_row(conn, ref: str):
    return _match(conn, ref)[0]


def _resolve(conn, ref: str):
    row = _find_row(conn, ref)
    if not row:
        raise NotFound(f"No task matching '{ref}'")
    return row


def _resolve_parent(conn, ref: str):
    """_resolve for a ref that must name a top-level task: a subtask's parent."""
    row = _resolve(conn, ref)
    if row["parent_id"]:
        raise ToledoError(f"'{row['slug']}' is a subtask; subtasks can't have subtasks")
    return row


def _reject_subtask(row, why: str) -> None:
    if row["parent_id"]:
        raise ToledoError(f"'{row['slug']}' is a subtask; {why}")


def find_task(ref: str) -> dict | None:
    with connect() as c:
        row = _find_row(c, ref)
        return _dicts(c, [row])[0] if row else None


def other_matches(ref: str) -> list[dict]:
    """Tasks a partial ref also matched besides the one it resolves to."""
    with connect() as c:
        return [_base_dict(r) for r in _match(c, ref)[1][1:]]


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

def list_tasks(states=None, project: str | None = None, tags=None,
               match_all: bool = False) -> list[dict]:
    """Top-level tasks in states, optionally only those in project and
    those with any of tags (every one of them, with match_all)."""
    states = list(states or STATES)
    marks = ",".join("?" * len(states))
    sql = f"SELECT * FROM tasks WHERE parent_id IS NULL AND state IN ({marks})"
    params: list = list(states)
    tags = parse_tags(tags)
    with connect() as c:
        if project:
            sql += " AND project = ?"
            params.append(_project_code(c, project))
        if tags:
            sql += (f" AND id IN (SELECT task_id FROM task_tags WHERE tag IN ({','.join('?' * len(tags))})"
                    " GROUP BY task_id HAVING COUNT(*) >= ?)")
            params += [*tags, len(tags) if match_all else 1]
        sql += f" ORDER BY {_STATE_ORDER}, priority DESC, id"
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
    """Tasks whose name/slug, tags, description, or notes contain query.
    Each result carries 'hits': which of name/tags/description/notes matched."""
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
        qt = "-".join(q.lstrip("#").split())
        tagged = {
            r["task_id"] for r in c.execute(
                "SELECT DISTINCT task_id FROM task_tags WHERE instr(tag, ?) > 0", (qt,)
            )
        } if qt else set()
        hits_by_id = {}
        for r in rows:
            hits = []
            if q in r["slug"] or q in r["name"].lower() or (qn and qn in _norm(r["name"])):
                hits.append("name")
            if r["id"] in tagged:
                hits.append("tags")
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


def _split(value) -> list[str]:
    """A filter given as a list or a comma-separated string."""
    if not value:
        return []
    if isinstance(value, str) and value.lstrip().startswith("["):
        try:  # a JSON array that reached us as a string, e.g. '["a", "b"]'
            value = json.loads(value)
        except ValueError:
            pass
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return [str(v).strip() for v in items if str(v).strip()]


def list_activity(limit: int | None = 100, offset: int = 0, since: str | None = None,
                  until: str | None = None, scope=None, task: str | None = None,
                  action=None, project: str | None = None) -> list[dict]:
    """Events newest first. since/until take a date (until inclusive) or a
    full timestamp; scope and action take one value or several (list or
    comma-separated); task includes its subtasks; project matches tasks now
    in it, deleted tasks that were, and the project's own events."""
    sql = ("SELECT a.*, t.slug AS t_slug, t.name AS t_name, t.project AS t_project, "
           "t.state AS t_state, p.slug AS p_slug, p.name AS p_name "
           "FROM activity a LEFT JOIN tasks t ON t.id = a.task_id "
           "LEFT JOIN tasks p ON p.id = t.parent_id WHERE 1 = 1")
    params: list = []
    if since:
        sql += " AND a.ts >= ?"
        params.append(since.strip())
    if until:
        until = until.strip()
        if len(until) == 10:
            until = (datetime.strptime(validate_date(until), "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            sql += " AND a.ts < ?"
        else:
            sql += " AND a.ts <= ?"
        params.append(until)
    scopes = _split(scope)
    if scopes:
        bad = [s for s in scopes if s not in SCOPES]
        if bad:
            raise ToledoError(f"Unknown scope '{bad[0]}' (expected one of: {', '.join(SCOPES)})")
        sql += f" AND a.scope IN ({','.join('?' * len(scopes))})"
        params += scopes
    actions = _split(action)
    if actions:
        sql += f" AND a.action IN ({','.join('?' * len(actions))})"
        params += actions
    with connect() as c:
        if task:
            tid = _resolve(c, task)["id"]
            sql += " AND (a.task_id = ? OR t.parent_id = ?)"
            params += [tid, tid]
        if project:
            code = _project_code(c, project)
            sql += (" AND (t.project = ? OR (a.task_id IS NULL AND json_extract(a.data, '$.project') = ?)"
                    " OR (a.scope = 'project' AND (a.ref = ? OR json_extract(a.data, '$.into') = ?)))")
            params += [code] * 4
        sql += " ORDER BY a.ts DESC, a.id DESC LIMIT ? OFFSET ?"
        params += [int(limit) if limit else -1, int(offset or 0)]
        out = []
        for r in c.execute(sql, params):
            data = json.loads(r["data"] or "{}")
            # Only events about a task carry a snapshot; a project event's
            # 'name' is the project's.
            about_task = r["scope"] in ("task", "context")
            snap = {k: data.pop(k, None) if about_task else None for k in _SNAPSHOT_KEYS}
            exists = r["t_slug"] is not None
            out.append({
                "id":      r["id"],
                "ts":      r["ts"],
                "scope":   r["scope"],
                "action":  r["action"],
                "ref":     r["ref"],
                "task_id": r["task_id"],
                # The task as it is now, or as it was when logged if it is gone.
                "slug":    r["t_slug"] if exists else snap["slug"],
                "name":    r["t_name"] if exists else snap["name"],
                "parent":  r["p_slug"] if exists else snap["parent"],
                "parent_name": r["p_name"],
                "project": r["t_project"],
                "state":   r["t_state"],
                # Slug of the top-level task to open, while it exists.
                "open":    (r["p_slug"] or r["t_slug"]) if exists else None,
                "deleted": r["task_id"] is None and snap["slug"] is not None,
                "data":    data,
            })
        return out


# ── Task mutations ────────────────────────────────────────────────────────────

def create_task(name: str, project: str | None = None, priority: int | str | None = None,
                due: str | None = None, recurrence: int | None = None,
                description: str | None = None, tags=None, source: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ToledoError("name is required")
    priority = DEFAULT_PRIORITY if priority in (None, "") else parse_priority(priority)
    if due:
        validate_date(due)
    recurrence = int(recurrence) if recurrence else None
    tags = parse_tags(tags)
    ts = now_iso()
    with connect() as c:
        project = _project_code(c, project) if (project or "").strip() else _default_project(c)
        slug = _unique_slug(c, name, None)
        cur = c.execute(
            "INSERT INTO tasks (slug, name, state, priority, project, due, recurrence, "
            "description, created_at, updated_at) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)",
            (slug, name, priority, project, due or None, recurrence,
             description or "", ts, ts),
        )
        tid = cur.lastrowid
        c.executemany("INSERT INTO task_tags (task_id, tag) VALUES (?, ?)", [(tid, t) for t in tags])
        _log_task(c, tid, "created", ts, state="active", priority=priority, project=project,
                  tags=tags or None, source=source)
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
            _log_task(c, tid, "completed_recurring", next_due=next_due, source=source)
            result.update(recurring=True, next_due=next_due)
        else:
            _touch(c, tid, state="completed")
            _log_task(c, tid, "state_changed", from_state=row["state"], to_state="completed", source=source)
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
        if state == "archive":
            _reject_subtask(row, "archive its parent instead")
        if row["state"] != state:
            _touch(c, row["id"], state=state)
            _log_task(c, row["id"], "state_changed", from_state=row["state"], to_state=state, source=source)
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
        _log_task(c, row["id"], "recurring_cancelled", source=source)
        _clear_context_if(c, row["id"])
        return _dicts(c, [c.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()])[0]


def _snapshot_history(conn, task_id: int) -> None:
    """Stamp slug/name onto a task's (and its subtasks') events that predate
    snapshots, before a delete leaves them without a task_id."""
    for t in conn.execute(
        "SELECT t.id, t.slug, t.name, p.slug AS parent FROM tasks t "
        "LEFT JOIN tasks p ON p.id = t.parent_id WHERE t.id = ? OR t.parent_id = ?", (task_id, task_id)
    ).fetchall():
        conn.execute(
            "UPDATE activity SET data = json_set(data, '$.slug', ?, '$.name', ?) "
            "WHERE task_id = ? AND json_extract(data, '$.slug') IS NULL",
            (t["slug"], t["name"], t["id"]),
        )
        if t["parent"]:
            conn.execute(
                "UPDATE activity SET data = json_set(data, '$.parent', ?) "
                "WHERE task_id = ? AND json_extract(data, '$.parent') IS NULL", (t["parent"], t["id"]),
            )


def delete_task(ref: str, source: str | None = None) -> dict:
    with connect() as c:
        row = _resolve(c, ref)
        d = _dicts(c, [row])[0]
        subtasks = c.execute("SELECT count(*) FROM tasks WHERE parent_id = ?", (row["id"],)).fetchone()[0]
        # Logged first: the delete then clears task_id on this and every
        # earlier event, leaving the snapshot to say which task it was.
        _log_task(c, row["id"], "deleted", project=row["project"], state=row["state"],
                  priority=row["priority"], tags=d["tags"] or None, subtasks=subtasks or None,
                  source=source)
        _clear_context_if(c, row["id"])
        _snapshot_history(c, row["id"])
        c.execute("DELETE FROM tasks WHERE id = ?", (row["id"],))
        return d


def _set_field(ref: str, field: str, value, action: str, source=None, **log) -> dict:
    with connect() as c:
        row = _resolve(c, ref)
        _touch(c, row["id"], **{field: value})
        _log_task(c, row["id"], action, source=source, **log)
        return _dicts(c, [c.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()])[0]


def rename_task(ref: str, name: str, source: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ToledoError("name is required")
    with connect() as c:
        row = _resolve(c, ref)
        slug = _unique_slug(c, name, row["parent_id"], exclude_id=row["id"])
        if slug != row["slug"]:
            c.execute("INSERT INTO task_aliases (task_id, slug) VALUES (?, ?)", (row["id"], row["slug"]))
        _touch(c, row["id"], name=name, slug=slug)
        _log_task(c, row["id"], "renamed", old=row["name"], new=name, source=source)
        return _dicts(c, [c.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()])[0]


def set_priority(ref: str, priority: int | str, source: str | None = None) -> dict:
    priority = parse_priority(priority)
    with connect() as c:
        row = _resolve(c, ref)
        if row["priority"] == priority:
            raise ToledoError(f"Task already has priority {priority}")
        _touch(c, row["id"], priority=priority)
        _log_task(c, row["id"], "reprioritized", old_priority=row["priority"],
             new_priority=priority, source=source)
        return _dicts(c, [c.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()])[0]


def set_project(ref: str, project: str, source: str | None = None) -> dict:
    with connect() as c:
        project = _project_code(c, project)
        row = _resolve(c, ref)
        _reject_subtask(row, "subtasks inherit the parent's project")
        if row["project"] == project:
            raise ToledoError(f"Task is already in project {_project_name(c, project)}")
        _touch(c, row["id"], project=project)
        _log_task(c, row["id"], "reprojected", old_project=row["project"], new_project=project, source=source)
        for sub in c.execute("SELECT id, project FROM tasks WHERE parent_id = ?", (row["id"],)).fetchall():
            _touch(c, sub["id"], project=project)
            _log_task(c, sub["id"], "reprojected", old_project=sub["project"], new_project=project,
                      source=source)
        return _dicts(c, [c.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()])[0]


def tag_task(ref: str, tags, source: str | None = None) -> dict:
    """Add tags to a task; ones it already has are ignored."""
    tags = parse_tags(tags)
    if not tags:
        raise ToledoError("at least one tag is required")
    with connect() as c:
        row = _resolve(c, ref)
        _reject_subtask(row, "subtasks share the parent's tags")
        have = set(_tags(c, [row["id"]])[row["id"]])
        added = [t for t in tags if t not in have]
        if not added:
            raise ToledoError(f"Task already has {_tag_list(tags)}")
        c.executemany("INSERT INTO task_tags (task_id, tag) VALUES (?, ?)", [(row["id"], t) for t in added])
        _log_task(c, row["id"], "tagged", tags=added, source=source)
        return _dicts(c, [c.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()])[0]


def untag_task(ref: str, tags, source: str | None = None) -> dict:
    """Remove tags from a task; ones it doesn't have are ignored."""
    tags = parse_tags(tags)
    if not tags:
        raise ToledoError("at least one tag is required")
    with connect() as c:
        row = _resolve(c, ref)
        _reject_subtask(row, "subtasks share the parent's tags")
        have = set(_tags(c, [row["id"]])[row["id"]])
        removed = [t for t in tags if t in have]
        if not removed:
            raise ToledoError(f"Task doesn't have {_tag_list(tags)}")
        c.executemany("DELETE FROM task_tags WHERE task_id = ? AND tag = ?", [(row["id"], t) for t in removed])
        _log_task(c, row["id"], "untagged", tags=removed, source=source)
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
        history_id = None
        if row["description"]:
            history_id = c.execute(
                "INSERT INTO description_history (task_id, ts, text) VALUES (?, ?, ?)",
                (row["id"], now_iso(), row["description"]),
            ).lastrowid
        _touch(c, row["id"], description=text or "")
        _log_task(c, row["id"], "description_updated", history_id=history_id, source=source)
        return _dicts(c, [c.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()])[0]


def _add_note(conn, task_id: int, text: str, source: str | None) -> None:
    ts = now_iso()
    note_id = conn.execute(
        "INSERT INTO notes (task_id, ts, text, source) VALUES (?, ?, ?, ?)",
        (task_id, ts, text, source),
    ).lastrowid
    _log_task(conn, task_id, "note_added", ts, note_id=note_id, source=source)


def add_note(ref: str, text: str, source: str | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise ToledoError("note text is required")
    with connect() as c:
        row = _resolve(c, ref)
        _add_note(c, row["id"], text, source)
        return _dicts(c, [row])[0]


def _find_note(conn, task_id: int, note_id: int):
    note = conn.execute(
        "SELECT id, text FROM notes WHERE id = ? AND task_id = ?", (note_id, task_id)
    ).fetchone()
    if not note:
        raise NotFound(f"No note #{note_id} on that task")
    return note


def update_note(ref: str, note_id: int, text: str, source: str | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise ToledoError("note text is required")
    with connect() as c:
        row = _resolve(c, ref)
        note = _find_note(c, row["id"], note_id)
        c.execute("UPDATE notes SET text = ? WHERE id = ?", (text, note["id"]))
        _log_task(c, row["id"], "note_edited", note_id=note["id"], old_text=note["text"],
                  source=source)
        return _dicts(c, [row])[0]


def delete_note(ref: str, note_id: int, source: str | None = None) -> dict:
    with connect() as c:
        row = _resolve(c, ref)
        note = _find_note(c, row["id"], note_id)
        c.execute("DELETE FROM notes WHERE id = ?", (note["id"],))
        _log_task(c, row["id"], "note_deleted", note_id=note["id"], old_text=note["text"],
                  source=source)
        return _dicts(c, [row])[0]


# ── Subtasks ──────────────────────────────────────────────────────────────────

def add_subtask(ref: str, name: str, priority: int | str | None = None, due: str | None = None,
                description: str | None = None, source: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ToledoError("subtask name is required")
    priority = DEFAULT_PRIORITY if priority in (None, "") else parse_priority(priority)
    if due:
        validate_date(due)
    ts = now_iso()
    with connect() as c:
        parent = _resolve_parent(c, ref)
        slug = _unique_slug(c, name, parent["id"])
        cur = c.execute(
            "INSERT INTO tasks (parent_id, slug, name, state, priority, project, due, "
            "description, created_at, updated_at) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
            (parent["id"], slug, name, priority, parent["project"],
             due or None, description or "", ts, ts),
        )
        _log_task(c, cur.lastrowid, "created", ts, state="active", priority=priority, due=due or None,
                  source=source)
        _log_task(c, parent["id"], "subtask_created", ts, subtask=slug, subtask_id=cur.lastrowid,
                  source=source)
        return {"parent": _base_dict(parent),
                **_base_dict(c.execute("SELECT * FROM tasks WHERE id = ?", (cur.lastrowid,)).fetchone())}


def set_subtask_state(ref: str, sub_ref: str, state: str, source: str | None = None,
                      first_match: bool = False) -> dict:
    """Complete (state='completed') or reopen (state='active') a subtask."""
    if state not in SUBTASK_STATES:
        raise ToledoError(f"state must be one of {SUBTASK_STATES}")
    other = "active" if state == "completed" else "completed"
    with connect() as c:
        parent = _resolve_parent(c, ref)
        sub = _resolve_subtask(c, parent["id"], sub_ref, [other], first=first_match)
        _touch(c, sub["id"], state=state)
        _log_task(c, sub["id"], "state_changed", from_state=other, to_state=state, source=source)
        action = "subtask_completed" if state == "completed" else "subtask_reopened"
        _log_task(c, parent["id"], action, subtask=sub["slug"], subtask_id=sub["id"], source=source)
        return {"parent": _base_dict(parent), **_base_dict(sub), "state": state}


def rename_subtask(ref: str, sub_ref: str, name: str, source: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ToledoError("name is required")
    with connect() as c:
        parent = _resolve_parent(c, ref)
        sub = _resolve_subtask(c, parent["id"], sub_ref)
        slug = _unique_slug(c, name, parent["id"], exclude_id=sub["id"])
        if slug != sub["slug"]:
            c.execute("INSERT INTO task_aliases (task_id, slug) VALUES (?, ?)", (sub["id"], sub["slug"]))
        _touch(c, sub["id"], name=name, slug=slug)
        _log_task(c, sub["id"], "renamed", old=sub["name"], new=name, source=source)
        _log_task(c, parent["id"], "subtask_renamed", old=sub["slug"], new=slug, subtask_id=sub["id"],
                  source=source)
        return {"parent": _base_dict(parent), "slug": slug, "name": name}


def delete_subtask(ref: str, sub_ref: str, source: str | None = None) -> dict:
    with connect() as c:
        parent = _resolve_parent(c, ref)
        sub = _resolve_subtask(c, parent["id"], sub_ref)
        _log_task(c, sub["id"], "deleted", state=sub["state"], source=source)
        _snapshot_history(c, sub["id"])
        c.execute("DELETE FROM tasks WHERE id = ?", (sub["id"],))
        _log_task(c, parent["id"], "subtask_deleted", subtask=sub["slug"], subtask_id=sub["id"],
                  state=sub["state"], source=source)
        return {"parent": _base_dict(parent), **_base_dict(sub)}


# ── Projects ──────────────────────────────────────────────────────────────────

def list_projects() -> dict[str, dict]:
    with connect() as c:
        return {
            r["code"]: {"name": r["name"], "color": r["color"]}
            for r in c.execute("SELECT * FROM projects ORDER BY code")
        }


def _project_code(conn, ref: str | None) -> str:
    """Resolve a project given by code or name, in any case, to its code."""
    ref = (ref or "").strip()
    if not ref:
        raise ToledoError("project is required")
    row = conn.execute(
        "SELECT code FROM projects WHERE code = ? COLLATE NOCASE OR name = ? COLLATE NOCASE "
        "ORDER BY code = ? COLLATE NOCASE DESC LIMIT 1",
        (ref, ref, ref),
    ).fetchone()
    if row:
        return row["code"]
    names = ", ".join(r["name"] for r in conn.execute("SELECT name FROM projects ORDER BY name"))
    raise NotFound(f"Unknown project '{ref}'. Projects: {names or '(none)'}")


def _default_project(conn) -> str:
    if conn.execute("INSERT OR IGNORE INTO projects (code, name) VALUES (?, 'General')",
                    (DEFAULT_PROJECT,)).rowcount:
        _log_event(conn, "project", "project_created", DEFAULT_PROJECT, name="General")
    return DEFAULT_PROJECT


def _project_name(conn, code: str) -> str:
    row = conn.execute("SELECT name FROM projects WHERE code = ?", (code,)).fetchone()
    return row["name"] if row else code


def find_project(ref: str) -> str:
    """The code of the project given by code or name."""
    with connect() as c:
        return _project_code(c, ref)


def project_name(code: str) -> str:
    with connect() as c:
        return _project_name(c, code)


def _check_name_free(conn, name: str, code: str | None = None) -> None:
    row = conn.execute(
        "SELECT code FROM projects WHERE name = ? COLLATE NOCASE AND code IS NOT ?", (name, code)
    ).fetchone()
    if row:
        raise Conflict(f"A project named '{name}' already exists")


def _new_code(conn, name: str) -> str:
    base = re.sub(r"[^A-Z0-9]", "", name.upper())[:3] or "PRJ"
    code, n = base, 2
    while conn.execute("SELECT 1 FROM projects WHERE code = ?", (code,)).fetchone():
        code, n = f"{base}{n}", n + 1
    return code


def _log_project_changes(conn, code: str, old, name: str | None, color: str | None,
                         source: str | None) -> None:
    """Log what changed on project code, given its row from before the write."""
    if old is None:
        _log_event(conn, "project", "project_created", code, name=name, color=color or None,
                   source=source)
        return
    if name is not None and name != old["name"]:
        _log_event(conn, "project", "project_renamed", code, old=old["name"], new=name, source=source)
    if color is not None and color != old["color"]:
        _log_event(conn, "project", "project_recolored", code, old=old["color"] or None, new=color,
                   source=source)


def save_project(code: str | None, name: str, color: str | None = None,
                 source: str | None = None) -> str:
    """Add a project, or update the name (and color, if given) of an existing
    one. A new project without a code gets one derived from its name.
    Returns the project's code."""
    code, name = (code or "").upper().strip(), (name or "").strip()
    if not name:
        raise ToledoError("name is required")
    with connect() as c:
        code = code or _new_code(c, name)
        _check_name_free(c, name, code)
        old = c.execute("SELECT * FROM projects WHERE code = ?", (code,)).fetchone()
        c.execute(
            "INSERT INTO projects (code, name, color) VALUES (?, ?, ?) "
            "ON CONFLICT(code) DO UPDATE SET name = excluded.name, "
            "color = CASE WHEN ? IS NULL THEN projects.color ELSE excluded.color END",
            (code, name, color or "", color),
        )
        _log_project_changes(c, code, old, name, color, source)
        return code


def update_project(ref: str, name: str | None = None, color: str | None = None,
                   source: str | None = None) -> str:
    """Rename or recolor a project given by code or name. Returns its code."""
    with connect() as c:
        code = _project_code(c, ref)
        old = c.execute("SELECT * FROM projects WHERE code = ?", (code,)).fetchone()
        if name is not None:
            name = name.strip()
            if not name:
                raise ToledoError("name is required")
            _check_name_free(c, name, code)
            c.execute("UPDATE projects SET name = ? WHERE code = ?", (name, code))
        if color is not None:
            c.execute("UPDATE projects SET color = ? WHERE code = ?", (color, code))
        _log_project_changes(c, code, old, name, color, source)
        return code


def remove_project(ref: str, source: str | None = None) -> str:
    """Remove an empty project. Returns its name."""
    with connect() as c:
        code = _project_code(c, ref)
        name = _project_name(c, code)
        count = c.execute("SELECT COUNT(*) FROM tasks WHERE project = ?", (code,)).fetchone()[0]
        if count:
            raise Conflict(f"Project '{name}' still has {count} task(s) — "
                           f"merge it into another project instead")
        c.execute("DELETE FROM projects WHERE code = ?", (code,))
        _log_event(c, "project", "project_removed", code, name=name, source=source)
        return name


def _move_project_tasks(conn, old: str, new: str, source: str | None = None) -> int:
    """Move every task and subtask in project old to new. Returns how many
    top-level tasks moved."""
    rows = conn.execute("SELECT id, parent_id FROM tasks WHERE project = ?", (old,)).fetchall()
    conn.execute("UPDATE tasks SET project = ? WHERE project = ?", (new, old))
    for r in rows:
        _log_task(conn, r["id"], "reprojected", old_project=old, new_project=new, via="merge",
                  source=source)
    return sum(1 for r in rows if r["parent_id"] is None)


def merge_projects(from_ref: str, into_ref: str, source: str | None = None) -> tuple[str, str, int]:
    """Move every task in one project into another and drop the first.
    Returns (from name, into name, tasks moved)."""
    with connect() as c:
        src, dst = _project_code(c, from_ref), _project_code(c, into_ref)
        if src == dst:
            raise ToledoError("Cannot merge a project into itself")
        names = _project_name(c, src), _project_name(c, dst)
        count = _move_project_tasks(c, src, dst, source)
        c.execute("DELETE FROM projects WHERE code = ?", (src,))
        _log_event(c, "project", "project_merged", src, name=names[0], into=dst, into_name=names[1],
                   count=count, source=source)
        return (*names, count)



# ── Tags ──────────────────────────────────────────────────────────────────────

def normalize_tag(value) -> str:
    """A tag as stored: lowercase, no leading '#', inner spaces as '-'."""
    tag = "-".join(str(value or "").strip().lstrip("#").lower().split())
    if not tag:
        raise ToledoError("tag is empty")
    if "," in tag:
        raise ToledoError(f"Invalid tag '{value}' (tags cannot contain commas)")
    return tag


def parse_tags(value) -> list[str]:
    """Tags given as a list or a comma-separated string, normalized and
    without duplicates."""
    return list(dict.fromkeys(normalize_tag(t) for t in _split(value)))


def _tag_list(tags) -> str:
    return ", ".join(f"#{t}" for t in tags)


def list_tags() -> list[dict]:
    """Every tag in use with how many tasks, and active tasks, have it."""
    with connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT g.tag, COUNT(*) AS tasks, SUM(t.state = 'active') AS active "
            "FROM task_tags g JOIN tasks t ON t.id = g.task_id GROUP BY g.tag ORDER BY g.tag"
        )]


def rename_tag(old, new, source: str | None = None) -> tuple[str, str, int]:
    """Rename a tag on every task that has it, merging it into new if that
    tag is already in use. Returns (old, new, number of tasks)."""
    old, new = normalize_tag(old), normalize_tag(new)
    if old == new:
        raise ToledoError(f"Tag is already #{new}")
    with connect() as c:
        ids = [r[0] for r in c.execute("SELECT task_id FROM task_tags WHERE tag = ?", (old,))]
        if not ids:
            raise NotFound(f"No task has tag #{old}")
        merged = c.execute("SELECT 1 FROM task_tags WHERE tag = ? LIMIT 1", (new,)).fetchone()
        c.execute("UPDATE OR IGNORE task_tags SET tag = ? WHERE tag = ?", (new, old))
        c.execute("DELETE FROM task_tags WHERE tag = ?", (old,))
        _log_event(c, "tag", "tag_merged" if merged else "tag_renamed", old, new=new,
                   tasks=len(ids), source=source)
        return old, new, len(ids)

# ── Glossary ──────────────────────────────────────────────────────────────────

def load_glossary() -> dict[str, str]:
    with connect() as c:
        return {r["term"]: r["canonical"] for r in c.execute("SELECT * FROM glossary ORDER BY term")}


def set_glossary_term(term: str, canonical: str, source: str | None = None) -> None:
    term, canonical = (term or "").strip().lower(), (canonical or "").strip()
    if not term or not canonical:
        raise ToledoError("term and canonical are required")
    with connect() as c:
        old = c.execute("SELECT canonical FROM glossary WHERE term = ?", (term,)).fetchone()
        if old and old["canonical"] == canonical:
            return
        c.execute("INSERT OR REPLACE INTO glossary VALUES (?, ?)", (term, canonical))
        _log_event(c, "glossary", "glossary_set", term, old=old["canonical"] if old else None,
                   new=canonical, source=source)


def glossary_hits(text: str) -> list[tuple[str, str]]:
    """Glossary terms still present in text, as (term, canonical) pairs.
    A glossary key may list variants ('paragard / perigard'). A variant that
    also appears in its canonical text is the correct spelling, not a
    mishearing, so it is not reported."""
    hits = []
    for term, canonical in load_glossary().items():
        for variant in (v.strip() for v in term.split("/")):
            if not variant:
                continue
            pattern = re.compile(rf"(?<!\w){re.escape(variant)}(?!\w)", re.IGNORECASE)
            if pattern.search(text or "") and not pattern.search(canonical):
                hits.append((variant, canonical))
    return hits


# ── Journal ───────────────────────────────────────────────────────────────────

_JOURNAL_ORDER = "ORDER BY entry_date DESC, created_at DESC, id DESC"


def _excerpt(text: str, length: int = 160) -> str:
    """First few lines of Markdown as one plain line, for list views."""
    lines = [re.sub(r"^(#+|[-*]|>)\s*", "", ln).strip() for ln in (text or "").splitlines()]
    flat = re.sub(r"[*_`]+", "", " · ".join(ln for ln in lines if ln))
    return flat if len(flat) <= length else flat[:length - 1].rstrip() + "…"


def _journal_dict(row, full: bool = True) -> dict:
    d = {
        "id":      row["id"],
        "date":    row["entry_date"],
        "title":   row["title"],
        "source":  row["source"],
        "created": row["created_at"],
        "updated": row["updated_at"],
        "excerpt": _excerpt(row["summary"] or row["raw"]),
        "has_summary": bool(row["summary"].strip()),
    }
    if full:
        d["raw"] = row["raw"]
        d["summary"] = row["summary"]
    return d


def _journal_rows(conn, ref) -> list:
    """Entries a ref names: an id ('12' or '#12'), a date (every entry that
    day), or 'latest'/empty for the newest entry."""
    ref = str(ref if ref is not None else "").strip().lstrip("#").lower()
    if ref in ("", "latest", "last"):
        rows = conn.execute(f"SELECT * FROM journal {_JOURNAL_ORDER} LIMIT 1").fetchall()
    elif ref.isdigit():
        rows = conn.execute("SELECT * FROM journal WHERE id = ?", (int(ref),)).fetchall()
    elif ref in ("today", "yesterday"):
        day = datetime.now() - timedelta(days=ref == "yesterday")
        rows = conn.execute(f"SELECT * FROM journal WHERE entry_date = ? {_JOURNAL_ORDER}",
                            (day.strftime("%Y-%m-%d"),)).fetchall()
    else:
        try:
            validate_date(ref)
        except ToledoError:
            raise ToledoError(f"'{ref}' is not a journal ref: use an id, a YYYY-MM-DD date, "
                              "'today', 'yesterday', or 'latest'")
        rows = conn.execute(f"SELECT * FROM journal WHERE entry_date = ? {_JOURNAL_ORDER}",
                            (ref,)).fetchall()
    if not rows:
        raise NotFound(f"No journal entry matching '{ref or 'latest'}'")
    return rows


def _resolve_journal(conn, ref):
    rows = _journal_rows(conn, ref)
    if len(rows) > 1:
        ids = ", ".join(f"#{r['id']}" for r in rows)
        raise ToledoError(f"{rows[0]['entry_date']} has several journal entries ({ids}); use an id")
    return rows[0]


def add_journal(raw: str, summary: str | None = None, title: str | None = None,
                date: str | None = None, source: str | None = None) -> dict:
    raw, summary = (raw or "").strip(), (summary or "").strip()
    if not raw and not summary:
        raise ToledoError("raw or summary is required")
    date = validate_date(date.strip()) if (date or "").strip() else today()
    ts = now_iso()
    with connect() as c:
        cur = c.execute(
            "INSERT INTO journal (entry_date, title, raw, summary, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date, (title or "").strip(), raw, summary, source, ts, ts),
        )
        _log_event(c, "journal", "journal_added", cur.lastrowid, date=date,
                   title=(title or "").strip() or None, source=source)
        return _journal_dict(c.execute("SELECT * FROM journal WHERE id = ?", (cur.lastrowid,)).fetchone())


def list_journal(limit: int | None = 50, offset: int = 0, query: str | None = None,
                 since: str | None = None, until: str | None = None) -> list[dict]:
    """Entries newest first, without their full text. query matches title,
    raw, or summary; since/until bound entry_date (inclusive)."""
    sql, params = "SELECT * FROM journal WHERE 1 = 1", []
    q = (query or "").strip().lower()
    if q:
        sql += " AND (lower(title) LIKE ? OR lower(raw) LIKE ? OR lower(summary) LIKE ?)"
        params += [f"%{q}%"] * 3
    if since:
        sql += " AND entry_date >= ?"
        params.append(validate_date(since))
    if until:
        sql += " AND entry_date <= ?"
        params.append(validate_date(until))
    sql += f" {_JOURNAL_ORDER} LIMIT ? OFFSET ?"
    params += [int(limit) if limit else -1, int(offset or 0)]
    with connect() as c:
        return [_journal_dict(r, full=False) for r in c.execute(sql, params)]


def get_journal(ref=None) -> list[dict]:
    """Full entries for a ref (see _journal_rows); a date may name several."""
    with connect() as c:
        return [_journal_dict(r) for r in _journal_rows(c, ref)]


def update_journal(ref, raw: str | None = None, summary: str | None = None,
                   title: str | None = None, date: str | None = None,
                   source: str | None = None) -> dict:
    """Change the given fields of one entry; None leaves a field as it is."""
    fields = {}
    if raw is not None:
        fields["raw"] = raw.strip()
    if summary is not None:
        fields["summary"] = summary.strip()
    if title is not None:
        fields["title"] = title.strip()
    if date is not None:
        fields["entry_date"] = validate_date(date.strip())
    if not fields:
        raise ToledoError("nothing to update: give raw, summary, title, or date")
    with connect() as c:
        row = _resolve_journal(c, ref)
        if not (fields.get("raw", row["raw"]) or fields.get("summary", row["summary"])):
            raise ToledoError("an entry needs a raw or summary text")
        changed = [k for k, v in fields.items() if v != row[k]]
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        c.execute(f"UPDATE journal SET {cols} WHERE id = ?", (*fields.values(), row["id"]))
        if changed:
            _log_event(c, "journal", "journal_updated", row["id"], date=row["entry_date"],
                       fields=changed, new_date=fields.get("entry_date") if "entry_date" in changed else None,
                       source=source)
        return _journal_dict(c.execute("SELECT * FROM journal WHERE id = ?", (row["id"],)).fetchone())


def delete_journal(entry_id: int, source: str | None = None) -> dict:
    with connect() as c:
        row = c.execute("SELECT * FROM journal WHERE id = ?", (int(entry_id),)).fetchone()
        if not row:
            raise NotFound(f"No journal entry #{entry_id}")
        c.execute("DELETE FROM journal WHERE id = ?", (row["id"],))
        _log_event(c, "journal", "journal_deleted", row["id"], date=row["entry_date"],
                   title=row["title"] or None, source=source)
        return _journal_dict(row, full=False)


# ── Context (the "current task" pointer) ──────────────────────────────────────

def get_context() -> str | None:
    with connect() as c:
        row = c.execute("SELECT value FROM meta WHERE key = 'context'").fetchone()
        if not row or not row["value"]:
            return None
        t = c.execute("SELECT slug FROM tasks WHERE id = ?", (int(row["value"]),)).fetchone()
        return t["slug"] if t else None


def set_context(ref: str | None, source: str | None = None) -> str | None:
    with connect() as c:
        old = c.execute("SELECT value FROM meta WHERE key = 'context'").fetchone()
        old_id = int(old["value"]) if old and old["value"] else None
        if not ref:
            c.execute("DELETE FROM meta WHERE key = 'context'")
            if old_id:
                _log_context(c, "context_cleared", old_id, source=source)
            return None
        row = _resolve(c, ref)
        _reject_subtask(row, "set the context to its parent instead")
        c.execute("INSERT OR REPLACE INTO meta VALUES ('context', ?)", (str(row["id"]),))
        if row["id"] != old_id:
            _log_context(c, "context_set", row["id"], source=source)
        return row["slug"]


def _log_context(conn, action: str, task_id: int, **data) -> None:
    # The pointer may outlive its task (a deleted task's id); log without it.
    exists = conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
    _log_event(conn, "context", action, task_id=task_id if exists else None, **data)


def _clear_context_if(conn, task_id: int) -> None:
    """Drop the context pointer when its task is closed or deleted."""
    if conn.execute("DELETE FROM meta WHERE key = 'context' AND value = ?", (str(task_id),)).rowcount:
        _log_context(conn, "context_cleared", task_id, reason="task closed")


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
