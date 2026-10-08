#!/usr/bin/env python3
"""
Toledo MCP Server
Exposes Toledo task management as MCP tools for Claude.

Run:   .venv/bin/python toledo_mcp.py [--port 8001]
Nginx: proxy /mcp/ → http://localhost:8001
Claude config:
  { "mcpServers": { "toledo": { "type": "sse",
      "url": "https://toledo.krets.com/mcp/sse" } } }
"""

import argparse
import asyncio
import contextlib
import json
import os
from datetime import datetime, timedelta

import mcp.types as types
import uvicorn
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Route

import brief_state
import toledo_db as db

RELEASE = db.release_version()

# Morning brief: the scheduler/collectors live in toledo_server.py (brief_scheduler.py); this
# process only reads their output from the shared ~/.toledo/brief/context.md.
BRIEF_CONTEXT_PATH = db.TOLEDO_HOME / "brief" / "context.md"
BRIEF_DB_PATH = db.TOLEDO_HOME / "brief.db"


def read_brief() -> str:
    try:
        return BRIEF_CONTEXT_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "No brief generated yet."


EVENT_IDS_SCHEMA = {
    "type": "object",
    "properties": {
        "ids": {"type": "array", "items": {"type": "string"}},
        "similar": {"type": "boolean", "default": False,
                    "description": "Also apply to every upcoming event with a near-identical title (a series)"},
    },
    "required": ["ids"],
}


def fmt_event_day(e: dict) -> str:
    return datetime.fromisoformat(e["start"]).strftime("%a %b %d").replace(" 0", " ")


def fmt_event_row(e: dict) -> str:
    """One compact line for list_events: date, title, internal id (no URL, to save tokens)."""
    flags = (" (updated)" if e["updated"] else "") + (" (on calendar)" if e["accepted"] else "")
    status = "" if e["status"] == "active" else f" [{e['status']}]"
    same = f" (+{len(e['same'])} same: {', '.join(e['same'])})" if e.get("same") else ""
    return f"{fmt_event_day(e)} · {e['title']} · {e['ref']}{flags}{status}{same}"


def fmt_event_detail(e: dict) -> str:
    loc = e["location"]
    where = ", ".join(x for x in (loc.get("name"), loc.get("address"), loc.get("city")) if x)
    if loc.get("is_online"):
        where = (where + " " if where else "") + f"(online{': ' + loc['online_url'] if loc.get('online_url') else ''})"
    start = datetime.fromisoformat(e["start"])
    when = start.strftime("%a %b %d %H:%M").replace(" 0", " ")
    if e["end"]:
        when += "–" + datetime.fromisoformat(e["end"]).strftime("%H:%M")
    lines = [f"{e['title']} ({e['ref']})", f"When:   {when}"]
    if where:
        lines.append(f"Where:  {where}")
    lines += [f"Link:   {e['url']}", f"Source: {e['source']}",
              f"State:  {e['status']}" + (", on the calendar" if e["accepted"] else "")
              + (", details changed recently" if e["updated"] else "")]
    if e.get("conflicts"):
        lines.append("Calendar that day: " + "; ".join(fmt_conflict(c) for c in e["conflicts"]))
    for d in e.get("same", []):
        lines.append(f"Also listed as {d['ref']} ({d['source'] or 'unknown source'}): {d['url']}")
    if e["description"]:
        lines += ["", e["description"]]
    return "\n".join(lines)


def fmt_conflict(c: dict) -> str:
    """One calendar entry on an event's day: time or all-day, title, and what is notable about it."""
    if c["all_day"]:
        when = "all-day"
    else:
        when = datetime.fromisoformat(c["start"]).strftime("%H:%M")
        if c["end"]:
            when += "–" + datetime.fromisoformat(c["end"]).strftime("%H:%M")
    notes = [n for n, on in (("overlaps", c["overlaps"]), ("holiday", c["holiday"]), ("this event", c["this_event"])) if on]
    return f"{when} {c['title']}" + (f" ({', '.join(notes)})" if notes else "")


def fmt_calendar_status(cal: dict, refresh_error: str | None = None) -> str:
    if refresh_error:
        return f"Calendar: could not refresh ({refresh_error}); conflicts may be out of date."
    if cal["feeds"] == 0:
        return "Calendar: no calendar feeds configured, so no conflicts are shown."
    why = "; ".join(f"{k}: {v}" for k, v in cal["errors"].items())
    if cal["age_minutes"] is None:
        return "Calendar: nothing cached yet, so no conflicts are shown" + (f" ({why})." if why else ".")
    if cal["fresh"]:
        return (f"Calendar: current (cached {cal['age_minutes']} min ago)."
                + (f" The latest refresh failed ({why}); this is the earlier copy." if why else ""))
    return (f"Calendar: STALE, cached {cal['age_minutes']} min ago" + (f" ({why})" if why else "")
            + ". Conflicts may be out of date; do not act on them without checking.")


def refresh_calendar(force: bool) -> str | None:
    """Re-fetch stale calendar feeds into the cache. Returns an error message, or None. Blocking: run in a thread."""
    try:
        import brief_gcal
        conn = brief_state.connect(str(BRIEF_DB_PATH))
        try:
            brief_gcal.refresh_cache(conn, int(os.environ.get("EVENT_DAYS", "90")),
                                     os.environ.get("TIMEZONE", "Europe/Berlin"), force=force)
        finally:
            conn.close()
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return None


def rerender_brief() -> None:
    """Refresh the brief's events section after a status change. Best effort: the next run fixes it."""
    try:
        import brief_scheduler
        brief_scheduler.rerender(str(BRIEF_CONTEXT_PATH.parent), str(BRIEF_DB_PATH),
                                 os.environ.get("TIMEZONE", "Europe/Berlin"),
                                 int(os.environ.get("EVENT_DAYS", "90")))
    except Exception:
        pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def pri_label(n: int) -> str:
    n = int(n)
    if n >= 76:  return "Ultra High"
    if n == 75:  return "High"
    if n >= 51:  return "Med-High"
    if n == 50:  return "Medium"
    if n >= 26:  return "Med-Low"
    if n == 25:  return "Low"
    return "Very Low"


def decorate(d: dict, projects: dict | None = None) -> dict:
    """Add display fields (priority label, project name) to a store task dict."""
    projects = projects if projects is not None else db.list_projects()
    d["pri_label"] = pri_label(d["priority"])
    d["project_name"] = projects.get(d["project"], {}).get("name", d["project"])
    return d


def fmt_task_line(d: dict) -> str:
    overdue = "⚠ " if d["overdue"] else ""
    due     = f"  due:{overdue}{d['due']}" if d["due"] else ""
    rec     = f"  ↻{d['recurrence']}d"    if d["recurrence"] else ""
    subs    = ""
    if d.get("subtasks"):
        done  = sum(1 for s in d["subtasks"] if s["state"] == "completed")
        total = len(d["subtasks"])
        subs  = f"  [{done}/{total} subtasks]"
    upd = f"  upd:{d['updated'][:16].replace('T', ' ')}" if d.get("updated") else ""
    tags = "".join(f" #{t}" for t in d.get("tags") or [])
    return (
        f"[{d['pri_label']:10s}] [{d['project_name']:12s}] {d['name']}{tags}"
        f"  ({d['slug']}){due}{rec}{subs}{upd}"
    )


# Activity fields that hold a project code, shown by name instead.
_PROJECT_KEYS = {"project", "old_project", "new_project"}
# Row ids and codes in activity data; useful to the web UI, noise to a model.
# (A merge's 'into' code comes with 'into_name'.)
_ID_KEYS = {"note_id", "history_id", "subtask_id", "into", "old_text"}


def fmt_event_fields(fields: dict) -> str:
    rest = {k: db.project_name(v) if k in _PROJECT_KEYS else ",".join(v) if isinstance(v, list) else v
            for k, v in fields.items() if k not in _ID_KEYS}
    return "  " + "  ".join(f"{k}={v}" for k, v in rest.items()) if rest else ""


def fmt_event_line(e: dict) -> str:
    """One list_activity event: when, what, and the task or other target."""
    ts = e["ts"][:16].replace("T", " ")
    if e["slug"]:
        target = f"{e['name']} [{e['parent'] + '/' if e['parent'] else ''}{e['slug']}]"
        if e["deleted"]:
            target += " (deleted)"
    elif e["scope"] == "project":
        # Events on a project that is gone carry its name.
        target = e["data"].pop("name", None) or db.project_name(e["ref"])
    elif e["scope"] == "journal":
        target = f"journal #{e['ref']}"
    elif e["scope"] == "tag":
        target = f"tag #{e['ref']}"
    else:
        target = e["ref"] or e["scope"]
    return f"{ts}  {e['action']:<20} {target}{fmt_event_fields(e['data'])}"


def fmt_task_detail(d: dict) -> str:
    lines = [
        f"# {d['name']}",
        f"Slug:     {task_ref(d)}  (#{d['id']})",
        f"State:    {d['state']}",
        f"Priority: {d['priority']} — {d['pri_label']}",
        f"Project:  {d['project_name']}",
    ]
    if d.get("tags"):
        lines.append(f"Tags:     {' '.join('#' + t for t in d['tags'])}")
    if d.get("parent"):
        lines.insert(2, f"Parent:   {d['parent']['name']}  ({d['parent']['slug']})")
    if d.get("updated"):
        lines.append(f"Updated:  {d['updated'][:16].replace('T', ' ')}")
    if d["due"]:
        lines.append(f"Due:      {'⚠ OVERDUE — ' if d['overdue'] else ''}{d['due']}")
    if d["recurrence"]:
        lines.append(f"Recurs:   every {d['recurrence']} days")

    if d.get("subtasks"):
        lines.append("\n## Subtasks")
        for s in d["subtasks"]:
            mark = "✓" if s["state"] == "completed" else "○"
            lines.append(f"  {mark} {s['name']}  ({d['slug']}/{s['slug']}, #{s['id']})")

    if d.get("description", "").strip():
        lines.append("\n## Description")
        lines.append(d["description"].strip())

    if d.get("notes"):
        lines.append("\n## Notes")
        lines.append(db.render_worklog(d["notes"], ids=True).strip())

    if d.get("activity"):
        lines.append("\n## Recent Activity")
        for e in reversed(d["activity"][-10:]):
            ts     = e.get("ts", "")[:16].replace("T", " ")
            action = e.get("action", "")
            extra  = fmt_event_fields({k: v for k, v in e.items() if k not in ("ts", "action")})
            lines.append(f"  {ts}  {action}{extra}")

    return "\n".join(lines)


def jtag(j: dict) -> str:
    title = f" '{j['title']}'" if j["title"] else ""
    return f"#{j['id']}{title} [{j['date']}]"


def fmt_journal_line(j: dict) -> str:
    day = datetime.strptime(j["date"], "%Y-%m-%d").strftime("%a")
    title = f"{j['title']} — " if j["title"] else ""
    return f"#{j['id']:<4} {j['date']} {day}  {title}{j['excerpt']}"


def fmt_journal_detail(j: dict, include_raw: bool = True) -> str:
    day = datetime.strptime(j["date"], "%Y-%m-%d").strftime("%A %Y-%m-%d")
    lines = [f"# Journal #{j['id']} — {day}" + (f": {j['title']}" if j["title"] else "")]
    lines.append(f"Submitted: {j['created'][:16].replace('T', ' ')}")
    if j["updated"] != j["created"]:
        lines.append(f"Revised:   {j['updated'][:16].replace('T', ' ')}")
    lines.append("\n## Summary\n" + (j["summary"] or "_No summary yet._"))
    if include_raw:
        lines.append("\n## Raw\n" + (j["raw"] or "_No raw text._"))
    return "\n".join(lines)


def glossary_warning(j: dict) -> str:
    """Flag misheard glossary terms left in a saved journal entry."""
    hits = db.glossary_hits(f"{j['title']}\n{j['raw']}\n{j['summary']}")
    if not hits:
        return ""
    listed = "; ".join(f"'{term}' → {canonical}" for term, canonical in hits)
    return (f"\n  ⚠ Glossary terms still in the entry: {listed}. "
            f"Correct them with update_journal (entry #{j['id']}).")


def tag(d: dict) -> str:
    """Identify a task in write results so the caller can check the target."""
    project = d.get("project_name") or db.project_name(d["project"])
    return f"'{d['name']}' [{task_ref(d)}, {project}]"


def task_ref(d: dict) -> str:
    """A ref that names d: its slug, or 'parent/child' for a subtask."""
    parent = d.get("parent")
    return f"{parent['slug']}/{d['slug']}" if parent else d["slug"]


def ok(text: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=text)]


def err(text: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=f"Error: {text}")]


# ── MCP Server ────────────────────────────────────────────────────────────────

# Sent to the client on connect. Keep it short: it lands in the model's context
# every session, and the prompts themselves are fetched on demand.
SERVER_INSTRUCTIONS = """\
Toledo is the user's task manager. Tasks are addressed by partial name or slug, and projects by name.
Every task tool also works on a subtask, addressed as 'parent/child' or by the '#id' get_task shows.
Priority is 1–99 and higher is more important (75 high, 50 medium, 25 low). Writes also accept the
labels Ultra High, High, Med-High, Medium, Med-Low, Low and Very Low, in any case.
Besides its one project, a task can carry any number of free-form tags (lowercase, shown as #tag),
set with tag_task / untag_task and filtered with list_tasks. Tags are not priority labels.

For more than one write, send them together in a single apply_changes call. Every \
write result names the task it touched and warns when a partial name matched several \
tasks, so there is no need to re-read a task to check a write.

Toledo ships guided-session prompts:
- morning_planning: start-of-day "what should I work on" session
- end_of_day_dump: end-of-day brain dump reconciled against tasks and the glossary
- periodic_audit: infrequent deep audit of tasks, categories, and goals
- event_review: triage and review upcoming tech events/meetups, a page at a time
- self_diagnostic: review the session so far for prompt or tool misbehavior and propose fixes

When the user asks for one of these sessions (e.g. "let's plan my day", "end of day \
dump"), fetch its full instructions first and follow them. The fetched prompt ends with \
a snapshot of the current tasks, projects, tags, and glossary, so the session needs no \
further reads to get started.

Claude.ai's web interface does not support MCP prompts natively, so fetch them with \
the get_prompt tool (name = the prompt name; list_prompts shows what exists). Clients \
that do surface MCP prompts can use them directly.

Toledo also keeps the user's journal: dated entries holding the raw dump as given and \
a revised summary. Save with add_journal (it can ride along in apply_changes), and read \
back with list_journal / get_journal. The newest entries come first.

Resources (status, projects, tags, active tasks, glossary, recent journal) are likewise \
available through the list_resources and get_resource tools.

Running build {commit}, built {built}. If something described elsewhere (a task, a \
conversation) doesn't match what the tools actually do, this build may be older than that \
description — check this line against the latest commit.
""".format(**RELEASE)

server = Server("toledo", version=RELEASE["commit"], instructions=SERVER_INSTRUCTIONS)
db.set_default_source("mcp")

# Write tools that apply_changes accepts as ops.
BATCH_OPS = {
    "create_task", "done_task", "move_task", "delete_task", "rename_task",
    "reprioritize_task", "reproject_task", "set_due", "set_recurrence", "add_note",
    "update_description", "add_subtask", "done_subtask", "delete_subtask",
    "update_glossary", "remove_glossary", "add_project", "remove_project", "rename_project", "merge_projects",
    "tag_task", "untag_task", "rename_tag", "add_journal", "update_journal",
}

# ── Tool definitions ──────────────────────────────────────────────────────────

TASK_REF = {"type": "string", "description": (
    "Task: partial name or slug. A subtask: 'parent/child' (e.g. 'garage/buy paint'), "
    "or '#id' as shown in get_task")}

TAGS = {"type": ["array", "string"], "items": {"type": "string"},
        "description": "Tags, e.g. [\"errands\", \"weekend\"] or \"errands, weekend\"; "
                       "stored lowercase, a leading # is dropped"}

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_tasks",
            description=(
                "List tasks. By default returns active tasks. "
                "Filter by state (active/completed/archive/all), project, and/or tags. "
                "Each task shows its last-updated timestamp; sort or filter by recency "
                "with 'sort' and 'updated_within_days'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "state":   {"type": "string", "enum": ["active","completed","archive","all"],
                                "description": "Filter by task state (default: active)"},
                    "project": {"type": "string",
                                "description": "Filter by project name (e.g. Chores)"},
                    "tags":    {**TAGS, "description": "Only tasks with any of these tags"},
                    "match":   {"type": "string", "enum": ["any", "all"],
                                "description": "'all' keeps only tasks with every tag given (default: any)"},
                    "sort":    {"type": "string", "enum": ["default", "recent"],
                                "description": "'recent' sorts by last-updated, newest first (default: creation order)"},
                    "updated_within_days": {"type": "integer",
                                "description": "Only include tasks updated within the last N days"},
                },
            },
        ),
        types.Tool(
            name="get_task",
            description="Get full details of a task including description, subtasks, notes, and activity log.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task": TASK_REF,
                },
                "required": ["task"],
            },
        ),
        types.Tool(
            name="create_task",
            description=(
                "Create a new task in the active state, optionally with an initial "
                "worklog note and subtasks."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name":        {"type": "string", "description": "Task name"},
                    "project":     {"type": "string", "description": "Project name (e.g. Chores). Defaults to General"},
                    "tags":        TAGS,
                    "priority":    {"type": ["integer", "string"], "description": "Priority 1–99 (higher = more important) or a label like High or Med-Low. Default 50"},
                    "due":         {"type": "string", "description": "Due date YYYY-MM-DD"},
                    "recurrence":  {"type": "integer", "description": "Repeat every N days"},
                    "description": {"type": "string", "description": "Task description (Markdown)"},
                    "note":        {"type": "string", "description": "Initial note for the worklog"},
                    "subtasks":    {"type": "array", "description": "Subtasks to add: names, or {name, priority, due} objects",
                                    "items": {"anyOf": [
                                        {"type": "string"},
                                        {"type": "object", "properties": {
                                            "name":     {"type": "string"},
                                            "priority": {"type": ["integer", "string"]},
                                            "due":      {"type": "string"},
                                        }, "required": ["name"]},
                                    ]}},
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="done_task",
            description=(
                "Mark a task as completed. "
                "For recurring tasks this advances the due date instead of completing it. "
                "Optionally logs a closing note to the task's worklog."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task": TASK_REF,
                    "note": {"type": "string", "description": "Optional closing note to add to the worklog"},
                },
                "required": ["task"],
            },
        ),
        types.Tool(
            name="move_task",
            description="Move a task to a different state (active/completed/archive).",
            inputSchema={
                "type": "object",
                "properties": {
                    "task":  TASK_REF,
                    "state": {"type": "string", "enum": ["active","completed","archive"]},
                },
                "required": ["task", "state"],
            },
        ),
        types.Tool(
            name="delete_task",
            description="Permanently delete a task and all its contents. Cannot be undone.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task": TASK_REF,
                },
                "required": ["task"],
            },
        ),
        types.Tool(
            name="rename_task",
            description="Rename a task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task": TASK_REF,
                    "name": {"type": "string", "description": "New name"},
                },
                "required": ["task", "name"],
            },
        ),
        types.Tool(
            name="reprioritize_task",
            description="Change a task's priority (1–99, higher = more important, or a label like High or Med-Low).",
            inputSchema={
                "type": "object",
                "properties": {
                    "task":     TASK_REF,
                    "priority": {"type": ["integer", "string"], "minimum": 1, "maximum": 99},
                },
                "required": ["task", "priority"],
            },
        ),
        types.Tool(
            name="reproject_task",
            description="Move a task to a different project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task":    TASK_REF,
                    "project": {"type": "string", "description": "Target project name"},
                },
                "required": ["task", "project"],
            },
        ),
        types.Tool(
            name="tag_task",
            description="Add tags to a task. Tags it already has are left alone. Subtasks share their parent's tags.",
            inputSchema={
                "type": "object",
                "properties": {"task": TASK_REF, "tags": TAGS},
                "required": ["task", "tags"],
            },
        ),
        types.Tool(
            name="untag_task",
            description="Remove tags from a task.",
            inputSchema={
                "type": "object",
                "properties": {"task": TASK_REF, "tags": TAGS},
                "required": ["task", "tags"],
            },
        ),
        types.Tool(
            name="set_due",
            description="Set or update a task's due date.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task": TASK_REF,
                    "due":  {"type": "string", "description": "YYYY-MM-DD, or empty string to clear"},
                },
                "required": ["task", "due"],
            },
        ),
        types.Tool(
            name="set_recurrence",
            description=(
                "Set, update, or clear a task's recurrence interval (days). "
                "Recurring tasks advance their due date instead of completing when done_task is called."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task":     TASK_REF,
                    "interval": {"type": "integer", "description": "Repeat every N days, or 0 to clear recurrence"},
                },
                "required": ["task", "interval"],
            },
        ),
        types.Tool(
            name="add_note",
            description=(
                "Append a timestamped note to a task's worklog. To change an existing note, "
                "pass its note_id (the '#id' get_task shows in the note's heading) with the "
                "replacement text in note, or with delete=true to remove it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task": TASK_REF,
                    "note": {"type": "string", "description": "Note text; the new text when note_id is given"},
                    "note_id": {"type": "integer", "description": "Existing note to edit or delete"},
                    "delete": {"type": "boolean", "description": "With note_id, delete that note (note not needed)"},
                },
                "required": ["task"],
            },
        ),
        types.Tool(
            name="update_description",
            description="Replace a task's description (Markdown).",
            inputSchema={
                "type": "object",
                "properties": {
                    "task":        TASK_REF,
                    "description": {"type": "string"},
                },
                "required": ["task", "description"],
            },
        ),
        types.Tool(
            name="add_subtask",
            description="Add a subtask to a task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task":     {"type": "string", "description": "Parent task (partial name or slug)"},
                    "name":     {"type": "string"},
                    "priority": {"type": ["integer", "string"], "default": 50, "description": "1–99, higher = more important, or a label like High"},
                    "due":      {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["task", "name"],
            },
        ),
        types.Tool(
            name="done_subtask",
            description="Mark a subtask as completed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task":    {"type": "string", "description": "Parent task"},
                    "subtask": {"type": "string", "description": "Partial subtask name or slug"},
                },
                "required": ["task", "subtask"],
            },
        ),
        types.Tool(
            name="delete_subtask",
            description=(
                "Permanently delete a subtask (active or completed). Cannot be undone. "
                "Fails if the name matches more than one subtask."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task":    {"type": "string", "description": "Parent task"},
                    "subtask": {"type": "string", "description": "Partial subtask name or slug"},
                },
                "required": ["task", "subtask"],
            },
        ),
        types.Tool(
            name="search_tasks",
            description="Search tasks by keyword across names, descriptions, and notes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="upcoming_tasks",
            description="List active tasks with due dates within the next N days (includes overdue).",
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "default": 7,
                             "description": "Look-ahead window in days (0 = overdue only)"},
                },
            },
        ),
        types.Tool(
            name="get_status",
            description=(
                "Get a summary of all tasks grouped by state and project. "
                "Good for a quick overview of what's on the plate."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_brief",
            description=(
                "Fetch the latest morning brief as Markdown: calendar (including German/UK/US "
                "public holidays), weather, and a short list of nearby tech events (date, title, id). Generated once a day by a "
                "background job; this just reads the last run's output. Already included in "
                "the morning_planning prompt, so call this only when you need it outside that flow."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="list_events",
            description=(
                "Upcoming tech events/meetups to review, soonest first, in pages of about 10 "
                "(continue with after=<last id>). Each is date · title · id (e.g. e42), followed by "
                "every calendar entry on that day (flagged: overlaps, all-day, holiday, this event). "
                "Conflicts are computed in code, but the calendar is shared, so an entry may not be "
                "the user's. Hides events already dismissed, muted, or on the calendar (accepted). "
                "Use get_event for the link and details, dismiss_events/mute_events to drop events."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["active", "dismissed", "muted", "all"],
                               "default": "active"},
                    "include_accepted": {"type": "boolean", "default": False,
                                         "description": "Also list events that match a calendar entry"},
                    "limit": {"type": "integer", "default": 10, "description": "Events per page"},
                    "after": {"type": "string", "description": "Continue after this event id (the previous page's last)"},
                    "refresh": {"type": "boolean", "default": False,
                                "description": "Re-fetch the calendar feeds now instead of using the 15-minute cache"},
                },
            },
        ),
        types.Tool(
            name="get_event",
            description="Full details for one event by its id (e.g. e42): date/time, venue, link, description.",
            inputSchema={"type": "object", "properties": {"id": {"type": "string", "description": "e.g. e42"}},
                         "required": ["id"]},
        ),
        types.Tool(
            name="dismiss_events",
            description=(
                "Hide events from the brief and list_events until their details change. For 'not interested "
                "right now'. Takes one or more ids (e.g. e42); duplicate listings of the same event "
                "are hidden with it. Pass similar=true to also hide every upcoming event with a "
                "near-identical title (a series)."
            ),
            inputSchema=EVENT_IDS_SCHEMA,
        ),
        types.Tool(
            name="mute_events",
            description=(
                "Hide events for good, even if their details change later. Takes one or more ids "
                "(e.g. e42); duplicate listings go with it. similar=true also mutes every upcoming "
                "event with a near-identical title (a series)."
            ),
            inputSchema=EVENT_IDS_SCHEMA,
        ),
        types.Tool(
            name="restore_events",
            description=(
                "Make dismissed or muted events active again. Takes one or more ids (e.g. e42); "
                "duplicates and, with similar=true, series matches come back too."
            ),
            inputSchema=EVENT_IDS_SCHEMA,
        ),
        types.Tool(
            name="list_projects",
            description="List all projects with their names, colors, and active task counts.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="list_tags",
            description="List every tag in use with its active and total task counts.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="rename_tag",
            description=(
                "Rename a tag on every task that has it. Renaming to a tag already in use "
                "merges the two."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "tag":  {"type": "string", "description": "Current tag"},
                    "name": {"type": "string", "description": "New tag"},
                },
                "required": ["tag", "name"],
            },
        ),
        types.Tool(
            name="add_project",
            description="Add a new project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name":  {"type": "string", "description": "Project name"},
                    "color": {"type": "string", "description": "Hex color e.g. #3498db"},
                },
                "required": ["name"],
            },
        ),
        types.Tool(
            name="remove_project",
            description="Remove an empty project. Use merge_projects for one that still has tasks.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name"},
                },
                "required": ["project"],
            },
        ),
        types.Tool(
            name="rename_project",
            description="Rename a project. Its tasks stay in it.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Current project name"},
                    "name":    {"type": "string", "description": "New project name"},
                },
                "required": ["project", "name"],
            },
        ),
        types.Tool(
            name="merge_projects",
            description=(
                "Merge one project into another: moves every task and subtask from "
                "from_project to into_project, then removes from_project."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "from_project": {"type": "string", "description": "Project to merge away"},
                    "into_project": {"type": "string", "description": "Project to merge into"},
                },
                "required": ["from_project", "into_project"],
            },
        ),
        types.Tool(
            name="remove_glossary",
            description=(
                "Delete a glossary entry by its exact term (as listed in the toledo://glossary "
                "resource, e.g. 'paragard / perigard' for a combined key). Use it to drop "
                "duplicate or wrong entries."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "term": {"type": "string", "description": "The entry's term, exactly as listed"},
                },
                "required": ["term"],
            },
        ),
        types.Tool(
            name="update_glossary",
            description=(
                "Add or update a glossary entry mapping a raw/garbled term to its canonical "
                "form (e.g. a misheard proper noun). Used to make the glossary self-healing "
                "so the same term is never asked about twice. Read the current glossary via "
                "the toledo://glossary resource."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "term":      {"type": "string", "description": "Raw or garbled term as it appeared"},
                    "canonical": {"type": "string", "description": "Confirmed canonical form/meaning"},
                },
                "required": ["term", "canonical"],
            },
        ),
        types.Tool(
            name="add_journal",
            description=(
                "Save a journal entry: the user's raw dump as they gave it, plus a revised "
                "Markdown summary of it. Correct misheard terms in both against the glossary "
                "first; the result flags any glossary terms left in. Dated today unless "
                "'date' backfills another day. A day may hold several entries."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "raw":     {"type": "string", "description": "The dump as the user gave it, with only misheard glossary terms corrected"},
                    "summary": {"type": "string", "description": "Revised/summarized version (Markdown)"},
                    "title":   {"type": "string", "description": "Optional short title"},
                    "date":    {"type": "string", "description": "YYYY-MM-DD the entry belongs to (default: today)"},
                },
                "required": ["raw"],
            },
        ),
        types.Tool(
            name="list_journal",
            description=(
                "List journal entries newest first: id, date, title, and a one-line excerpt. "
                "Filter by a keyword across title/raw/summary, or by date range. "
                "Read an entry in full with get_journal."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 10, "description": "Max entries (default 10)"},
                    "query": {"type": "string", "description": "Keyword to search for"},
                    "since": {"type": "string", "description": "Earliest date, YYYY-MM-DD"},
                    "until": {"type": "string", "description": "Latest date, YYYY-MM-DD"},
                },
            },
        ),
        types.Tool(
            name="get_journal",
            description=(
                "Read journal entries in full (summary and raw). 'entry' is an id, a date "
                "(YYYY-MM-DD, 'today', 'yesterday': every entry that day), or 'latest' (default)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entry": {"type": "string", "description": "Id, date, or 'latest'"},
                    "include_raw": {"type": "boolean", "default": True,
                                    "description": "Include the raw text (default true)"},
                },
            },
        ),
        types.Tool(
            name="update_journal",
            description=(
                "Revise a journal entry. Only the fields given change. 'entry' is an id, or a "
                "date/'latest' that names exactly one entry."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "entry":   {"type": "string", "description": "Id, date, or 'latest'"},
                    "summary": {"type": "string", "description": "New summary (Markdown)"},
                    "raw":     {"type": "string", "description": "New raw text"},
                    "title":   {"type": "string"},
                    "date":    {"type": "string", "description": "Move the entry to another day, YYYY-MM-DD"},
                },
                "required": ["entry"],
            },
        ),
        types.Tool(
            name="list_activity",
            description=(
                "The event log, newest first: every change to tasks, subtasks, projects, the "
                "glossary, the journal, and the current-task context, with who made it (source). "
                "Use it to answer \"what changed since yesterday\" or to trace a task's history, "
                "including deleted tasks."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "since":   {"type": "string", "description": "Earliest date (YYYY-MM-DD) or timestamp"},
                    "until":   {"type": "string", "description": "Latest date (inclusive) or timestamp"},
                    "task":    {"type": "string", "description": "Only this task and its subtasks"},
                    "project": {"type": "string", "description": "Only this project's tasks and events"},
                    "scope":   {"type": "string", "description": "task, project, tag, glossary, journal, "
                                                                 "context, or system; comma-separate several"},
                    "action":  {"type": "string", "description": "e.g. state_changed, deleted; comma-separate several"},
                    "limit":   {"type": "integer", "default": 50, "description": "Max events (default 50)"},
                },
            },
        ),
        types.Tool(
            name="apply_changes",
            description=(
                "Apply several writes in one call. Each change is {\"op\": <write tool name>, "
                "...that tool's arguments}, e.g. {\"op\": \"set_due\", \"task\": \"dentist\", "
                "\"due\": \"2026-10-02\"}. Ops: " + ", ".join(sorted(BATCH_OPS)) + ". "
                "Changes run in order and each commits on its own: a failure does not roll "
                "back earlier changes or stop later ones. A create_task change may carry "
                "\"as\": \"<label>\", and later changes can then use \"$<label>\" as their "
                "task; they are skipped if that create failed. Returns one ✓/✗/skipped line "
                "per change."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "changes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {"type": "string", "enum": sorted(BATCH_OPS)},
                                "as": {"type": "string", "description": "create_task only: label for later changes"},
                            },
                            "required": ["op"],
                        },
                    },
                },
                "required": ["changes"],
            },
        ),
        types.Tool(
            name="list_resources",
            description=(
                "List Toledo's MCP resources (uri, name, description) — status, projects, "
                "active tasks, glossary, recent journal. Exists for clients that only surface "
                "MCP tools, not the resources capability; fetch a resource's contents with "
                "get_resource."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_resource",
            description="Fetch the contents of a Toledo resource by uri (see list_resources).",
            inputSchema={
                "type": "object",
                "properties": {
                    "uri": {"type": "string", "description": "Resource uri, e.g. 'toledo://status'"},
                },
                "required": ["uri"],
            },
        ),
        types.Tool(
            name="list_prompts",
            description=(
                "List Toledo's MCP prompts (name, description) — end_of_day_dump, "
                "morning_planning, periodic_audit, event_review, self_diagnostic. Exists for clients that only surface MCP "
                "tools, not the prompts capability; fetch a prompt's text with get_prompt."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_prompt",
            description=(
                "Fetch the full instructions for a Toledo prompt by name (see list_prompts). "
                "Follow the returned instructions as if the user had invoked that prompt."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Prompt name, e.g. 'morning_planning'"},
                },
                "required": ["name"],
            },
        ),
    ]


# ── Tool handlers ─────────────────────────────────────────────────────────────

# Argument names agents commonly guess, mapped to the canonical schema name.
# Applied only when the canonical key is absent, so real params (e.g. the
# 'due' on create_task/add_subtask) are never overridden.
_ARG_ALIASES_ALL = {
    "task_id": "task", "task_name": "task", "slug": "task", "id": "task",
}
_ARG_ALIASES = {
    "rename_task": {"new_name": "name", "title": "name"},
    "add_subtask": {"title": "name"},
    "set_due":     {"date": "due", "due_date": "due"},
    "add_note":    {"text": "note", "content": "note"},
    "add_journal": {"text": "raw", "content": "raw", "dump": "raw", "revised": "summary"},
    "get_journal": {"id": "entry", "entry_id": "entry", "date": "entry"},
    "update_journal": {"id": "entry", "entry_id": "entry", "revised": "summary"},
    "remove_project": {"code": "project", "name": "project"},
    "rename_project": {"old_code": "project", "old_name": "project",
                       "new_code": "name", "new_name": "name"},
    "merge_projects": {"from_code": "from_project", "from": "from_project",
                       "into_code": "into_project", "into": "into_project"},
    "list_tasks":     {"tag": "tags"},
    "create_task":    {"title": "name", "tag": "tags"},
    "tag_task":       {"tag": "tags"},
    "untag_task":     {"tag": "tags"},
    "rename_tag":     {"old": "tag", "from": "tag", "new": "name", "to": "name", "new_name": "name"},
}

# These take event ids, not task references, so the task aliases above don't apply.
_EVENT_TOOLS = {"list_events", "get_event", "dismiss_events", "mute_events", "restore_events"}

_required_args: dict[str, list[str]] | None = None


async def _required_for(name: str) -> list[str]:
    global _required_args
    if _required_args is None:
        _required_args = {
            tool.name: tool.inputSchema.get("required", []) for tool in await list_tools()
        }
    return _required_args.get(name, [])


def _normalize_args(name: str, args: dict) -> dict:
    out = dict(args)
    aliases = {**({} if name in _EVENT_TOOLS else _ARG_ALIASES_ALL), **_ARG_ALIASES.get(name, {})}
    for alias, canonical in aliases.items():
        if alias in out and canonical not in out:
            out[canonical] = out.pop(alias)
    # A bare integer task reference is a task id.
    if isinstance(out.get("task"), int) and not isinstance(out["task"], bool):
        out["task"] = f"#{out['task']}"
    return out


# validate_input=False: the library's schema check would reject aliased names
# before we could normalize them, so required args are checked in call_tool.
@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    try:
        return ok(await _run(name, arguments or {}))
    except ValueError as e:
        return err(str(e))
    except Exception as e:
        return err(f"Unexpected error in '{name}': {e}")


async def _prepare(name: str, arguments: dict) -> dict:
    args = _normalize_args(name, arguments)
    missing = [k for k in await _required_for(name) if k not in args]
    if missing:
        raise ValueError(
            f"Missing required argument(s) for '{name}': {', '.join(missing)}. "
            f"Got: {', '.join(sorted(args)) or '(none)'}"
        )
    return args


async def _run(name: str, arguments: dict) -> str:
    """Run one tool call and return its text, raising on failure."""
    args = await _prepare(name, arguments)
    # Look for other partial matches before the write, which may rename the task.
    others = db.other_matches(args["task"]) if name in BATCH_OPS and "task" in args else []
    text = (await _dispatch(name, args))[0].text
    if others:
        listed = ", ".join(f"{tag(o)} ({o['state']})" for o in others[:3])
        more = f" and {len(others) - 3} more" if len(others) > 3 else ""
        text += (f"\n  ⚠ '{args['task']}' also matches {listed}{more}. "
                 f"Use a slug if you meant one of those.")
    return text


def _create_task(args: dict) -> tuple[dict, str]:
    task_name = args["name"].strip()
    d = db.create_task(
        task_name,
        project=args.get("project"),
        priority=args.get("priority"),
        due=args.get("due") or None,
        recurrence=args.get("recurrence") or None,
        description=args.get("description") or f"# {task_name}\n\n_No description._\n",
        tags=args.get("tags"),
    )
    lines = [f"Created: {tag(d)}"]
    # The task exists from here on, so extras report their own failures
    # rather than failing the create.
    if (args.get("note") or "").strip():
        try:
            db.add_note(d["slug"], args["note"])
            lines.append("  + note")
        except ValueError as e:
            lines.append(f"  ✗ note: {e}")
    for sub in args.get("subtasks") or []:
        sub = {"name": sub} if isinstance(sub, str) else sub
        try:
            s = db.add_subtask(d["slug"], sub.get("name", ""),
                               priority=sub.get("priority"), due=sub.get("due"))
            lines.append(f"  + subtask {s['name']} [{d['slug']}/{s['slug']}]")
        except ValueError as e:
            lines.append(f"  ✗ subtask {sub.get('name', '?')}: {e}")
    return d, "\n".join(lines)


async def _apply_changes(changes) -> str:
    if isinstance(changes, str):
        changes = json.loads(changes)
    if not isinstance(changes, list) or not changes:
        raise ValueError("changes must be a non-empty list of {op, ...args} objects")
    labels: dict[str, str | None] = {}   # label → slug, or None if its create failed
    lines, failed = [], 0
    for i, change in enumerate(changes, 1):
        change = dict(change) if isinstance(change, dict) else {}
        op = change.pop("op", None) or change.pop("tool", None)
        label = change.pop("as", None)
        head = f"{i}. {op or '?'}"
        try:
            if op not in BATCH_OPS:
                raise ValueError(f"unknown op '{op}'")
            ref = change.get("task")
            if isinstance(ref, str) and ref.startswith("$"):
                if ref[1:] not in labels:
                    raise ValueError(f"'{ref}' is not the label of an earlier create_task")
                if labels[ref[1:]] is None:
                    lines.append(f"– {head}: skipped, its create_task ({ref}) failed")
                    failed += 1
                    continue
                change["task"] = labels[ref[1:]]
            if op == "create_task":
                d, text = _create_task(await _prepare(op, change))
                if label:
                    labels[label] = d["slug"]
            else:
                text = await _run(op, change)
            lines.append(f"✓ {head}: {text}")
        except Exception as e:
            if label:
                labels[label] = None
            lines.append(f"✗ {head}: {e}")
            failed += 1
    summary = f"{len(changes) - failed} of {len(changes)} changes applied"
    return summary + ("" if not failed else f", {failed} failed or skipped") + ":\n" + "\n".join(lines)


async def _dispatch(name: str, args: dict) -> list[types.TextContent]:

    # ── list_tasks ────────────────────────────────────────────────────────────
    if name == "list_tasks":
        state_filter   = args.get("state", "active")
        project_filter = args.get("project") or None
        sort           = args.get("sort", "default")
        updated_within = args.get("updated_within_days")
        if state_filter != "all" and state_filter not in db.STATES:
            raise ValueError(f"Invalid state '{state_filter}'")
        states   = db.STATES if state_filter == "all" else [state_filter]
        projects = db.list_projects()
        all_tasks = [decorate(d, projects) for d in db.list_tasks(
            states, project_filter, args.get("tags"), match_all=args.get("match") == "all")]
        lines  = []
        for state in states:
            dicts = [d for d in all_tasks if d["state"] == state]
            if updated_within is not None:
                cutoff = (datetime.now() - timedelta(days=int(updated_within))).isoformat()
                dicts = [d for d in dicts if d["updated"] and d["updated"] >= cutoff]
            if sort == "recent":
                dicts.sort(key=lambda d: d["updated"] or "", reverse=True)
            if not dicts:
                continue
            if state_filter == "all":
                lines.append(f"\n── {state.upper()} ──")
            for d in dicts:
                lines.append(fmt_task_line(d))
        if not lines:
            return ok("No tasks found.")
        return ok("\n".join(lines))

    # ── get_task ──────────────────────────────────────────────────────────────
    if name == "get_task":
        return ok(fmt_task_detail(decorate(db.get_task(args["task"]))))

    # ── create_task ───────────────────────────────────────────────────────────
    if name == "create_task":
        return ok(_create_task(args)[1])

    # ── done_task ─────────────────────────────────────────────────────────────
    if name == "done_task":
        r = db.complete_task(args["task"], note=args.get("note"))
        if r["recurring"]:
            return ok(f"↻ Recurring task advanced: {tag(r['task'])}. Next due: {r['next_due']}")
        return ok(f"✓ Completed: {tag(r['task'])}")

    # ── move_task ─────────────────────────────────────────────────────────────
    if name == "move_task":
        to_state = args["state"]
        d = db.resolve_task(args["task"])
        if d["state"] == to_state:
            return ok(f"{tag(d)} is already in '{to_state}'")
        d = db.move_task(args["task"], to_state)
        return ok(f"→ Moved {tag(d)} to {to_state}")

    # ── delete_task ───────────────────────────────────────────────────────────
    if name == "delete_task":
        d = db.delete_task(args["task"])
        return ok(f"🗑 Deleted: {tag(d)}")

    # ── rename_task ───────────────────────────────────────────────────────────
    if name == "rename_task":
        d = db.rename_task(args["task"], args["name"])
        return ok(f"Renamed → {tag(d)}")

    # ── reprioritize_task ─────────────────────────────────────────────────────
    if name == "reprioritize_task":
        d = db.set_priority(args["task"], args["priority"])
        return ok(f"Priority → {d['priority']} ({pri_label(d['priority'])}): {tag(d)}")

    # ── reproject_task ────────────────────────────────────────────────────────
    if name == "reproject_task":
        d = db.set_project(args["task"], args["project"])
        return ok(f"Project → {tag(d)}")

    # ── tag_task / untag_task ─────────────────────────────────────────────────
    if name in ("tag_task", "untag_task"):
        write = db.tag_task if name == "tag_task" else db.untag_task
        d = write(args["task"], args["tags"])
        return ok(f"Tags → {' '.join('#' + t for t in d['tags']) or '(none)'}: {tag(d)}")

    # ── set_due ───────────────────────────────────────────────────────────────
    if name == "set_due":
        d = db.set_due(args["task"], args.get("due"))
        return ok(f"Due {d['due']}: {tag(d)}" if d["due"] else f"Due date cleared: {tag(d)}")

    # ── set_recurrence ────────────────────────────────────────────────────────
    if name == "set_recurrence":
        d = db.set_recurrence(args["task"], args.get("interval"))
        if d["recurrence"]:
            return ok(f"Recurs every {d['recurrence']} days: {tag(d)}")
        return ok(f"Recurrence cleared: {tag(d)}")

    # ── add_note ──────────────────────────────────────────────────────────────
    if name == "add_note":
        if args.get("note_id") is None:
            if args.get("delete"):
                raise ValueError("delete needs a note_id")
            if "note" not in args:
                raise ValueError("Missing required argument(s) for 'add_note': note")
            d = db.add_note(args["task"], args["note"])
            return ok(f"Note added to {tag(d)}")
        try:
            note_id = int(str(args["note_id"]).lstrip("#"))
        except ValueError:
            raise ValueError(f"note_id must be a number, got '{args['note_id']}'")
        if args.get("delete"):
            d = db.delete_note(args["task"], note_id)
            return ok(f"Note #{note_id} deleted from {tag(d)}")
        if "note" not in args:
            raise ValueError("Pass note with the new text, or delete=true, along with note_id")
        d = db.update_note(args["task"], note_id, args["note"])
        return ok(f"Note #{note_id} updated on {tag(d)}")

    # ── update_description ────────────────────────────────────────────────────
    if name == "update_description":
        d = db.set_description(args["task"], args.get("description", ""))
        return ok(f"Description updated for {tag(d)}")

    # ── add_subtask ───────────────────────────────────────────────────────────
    if name == "add_subtask":
        s = db.add_subtask(args["task"], args["name"],
                           priority=args.get("priority"), due=args.get("due"))
        return ok(f"Subtask created: {s['name']} [{s['parent']['slug']}/{s['slug']}] under {tag(s['parent'])}")

    # ── done_subtask ──────────────────────────────────────────────────────────
    if name == "done_subtask":
        s = db.set_subtask_state(args["task"], args["subtask"], "completed", first_match=True)
        return ok(f"✓ Subtask done: {s['name']} [{s['slug']}] under {tag(s['parent'])}")

    # ── delete_subtask ────────────────────────────────────────────────────────
    if name == "delete_subtask":
        s = db.delete_subtask(args["task"], args["subtask"])
        return ok(f"🗑 Deleted subtask: {s['name']} [{s['slug']}, {s['state']}] under {tag(s['parent'])}")

    # ── search_tasks ──────────────────────────────────────────────────────────
    if name == "search_tasks":
        query = args["query"]
        projects = db.list_projects()
        results = [
            f"[{d['state']}] {fmt_task_line(decorate(d, projects))}  (matched: {', '.join(d['hits'])})"
            for d in db.search(query)
        ]
        if not results:
            return ok(f"No tasks match '{query}'")
        return ok(f"Results for '{query}':\n\n" + "\n".join(results))

    # ── upcoming_tasks ────────────────────────────────────────────────────────
    if name == "upcoming_tasks":
        days = int(args.get("days") if args.get("days") is not None else 7)
        projects = db.list_projects()
        results = [decorate(d, projects) for d in db.upcoming(days)]
        if not results:
            return ok(f"No tasks due within {days} days.")
        lines = [fmt_task_line(d) for d in results]
        label = "overdue" if days == 0 else f"due within {days} days"
        return ok(f"Tasks {label}:\n\n" + "\n".join(lines))

    # ── get_status ────────────────────────────────────────────────────────────
    if name == "get_status":
        projects = db.list_projects()
        all_tasks = [decorate(d, projects) for d in db.list_tasks()]
        lines    = [f"Toledo Status — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
        for state in db.STATES:
            tasks = [d for d in all_tasks if d["state"] == state]
            if not tasks:
                continue
            lines.append(f"── {state.upper()} ({len(tasks)}) ──")
            # Group by project
            by_proj: dict[str, list] = {}
            for d in tasks:
                by_proj.setdefault(d["project"], []).append(d)
            for proj_tasks in sorted(by_proj.values(), key=lambda t: t[0]["project_name"].lower()):
                lines.append(f"  {proj_tasks[0]['project_name']} — {len(proj_tasks)} task(s)")
                for d in proj_tasks:
                    due = f"  due:{('⚠' if d['overdue'] else '')}{d['due']}" if d["due"] else ""
                    lines.append(f"    • {d['name']}{due}")
            lines.append("")
        return ok("\n".join(lines))

    # ── get_brief ─────────────────────────────────────────────────────────────
    if name == "get_brief":
        return ok(read_brief())

    # ── events ────────────────────────────────────────────────────────────────
    if name in ("list_events", "get_event", "dismiss_events", "mute_events", "restore_events"):
        conn = brief_state.connect(str(BRIEF_DB_PATH))
        try:
            if name == "list_events":
                status = args.get("status") or "active"
                if status != "all" and status not in brief_state.STATUSES:
                    return err(f"status must be all or one of {list(brief_state.STATUSES)}")
                refresh_error = await asyncio.to_thread(refresh_calendar, bool(args.get("refresh")))
                page = brief_state.event_page(conn, after=args.get("after"), limit=args.get("limit") or 10,
                                              status=status, include_accepted=bool(args.get("include_accepted")))
                if not page["events"]:
                    return ok("No upcoming events to review." if not args.get("after") else "No more events.")
                lo, hi = (datetime.fromisoformat(d).strftime("%b %d").replace(" 0", " ") for d in page["window"])
                head = f"{len(page['events'])} event(s), {lo} – {hi}"
                if page["next"]:
                    head += f"; {page['remaining']} more (continue with after={page['next']})"
                lines = [head, fmt_calendar_status(page["calendar"], refresh_error), ""]
                for e in page["events"]:
                    lines.append(fmt_event_row(e))
                    if e["conflicts"]:
                        lines.append("    calendar: " + "; ".join(fmt_conflict(c) for c in e["conflicts"]))
                return ok("\n".join(lines))
            if name == "get_event":
                refresh_error = await asyncio.to_thread(refresh_calendar, False)
                e = brief_state.get_event(conn, args.get("id") or "")
                if not e:
                    return err(f"no event {args.get('id')!r}")
                return ok(fmt_event_detail(e) + "\n\n" + fmt_calendar_status(brief_state.calendar_status(conn), refresh_error))
            ids = args.get("ids") or []
            if isinstance(ids, str):
                ids = [ids]
            if not ids:
                return err("ids is required")
            status = {"dismiss_events": "dismissed", "mute_events": "muted", "restore_events": "active"}[name]
            changed, unknown = brief_state.apply_status(conn, [str(i) for i in ids], status,
                                                        series=bool(args.get("similar")))
        finally:
            conn.close()
        rerender_brief()
        asked = {str(i).lstrip("#").lower() for i in ids}
        lines = [f"{len(changed)} event(s) now {status}."]
        # Show what the duplicate/series expansion touched beyond the ids that were asked for.
        lines += [f"  also {c['ref']} · {fmt_event_day(c)} · {c['title']}" for c in changed if c["ref"] not in asked]
        return ok("\n".join(lines) + (f"\nUnknown ids: {', '.join(unknown)}." if unknown else ""))

    # ── list_projects ─────────────────────────────────────────────────────────
    if name == "list_projects":
        projects = db.list_projects()
        if not projects:
            return ok("No projects defined.")
        active: dict[str, int] = {}
        for d in db.list_tasks(["active"]):
            active[d["project"]] = active.get(d["project"], 0) + 1
        lines = [f"{'NAME':<20}  {'ACTIVE':>6}  COLOR", "-" * 40]
        for code, val in sorted(projects.items(), key=lambda p: p[1]["name"].lower()):
            lines.append(f"{val['name']:<20}  {active.get(code, 0):>6}  {val['color']}")
        return ok("\n".join(lines))

    # ── list_tags / rename_tag ────────────────────────────────────────────────
    if name == "list_tags":
        tags = db.list_tags()
        if not tags:
            return ok("No tags in use.")
        lines = [f"{'TAG':<20}  {'ACTIVE':>6}  {'TOTAL':>5}", "-" * 35]
        lines += [f"#{t['tag']:<19}  {t['active']:>6}  {t['tasks']:>5}" for t in tags]
        return ok("\n".join(lines))

    if name == "rename_tag":
        old, new, count = db.rename_tag(args["tag"], args["name"])
        return ok(f"Renamed #{old} → #{new} on {count} task(s)")

    # ── add_project ───────────────────────────────────────────────────────────
    if name == "add_project":
        pname = args["name"].strip()
        color = args.get("color") or ""
        db.save_project(None, pname, color)
        return ok(f"✓ Project '{pname}'" + (f"  {color}" if color else ""))

    # ── remove_project ────────────────────────────────────────────────────────
    if name == "remove_project":
        return ok(f"Removed project '{db.remove_project(args['project'])}'")

    # ── rename_project ────────────────────────────────────────────────────────
    if name == "rename_project":
        code = db.find_project(args["project"])
        old = db.project_name(code)
        db.update_project(code, name=args["name"])
        return ok(f"Renamed project '{old}' → '{args['name'].strip()}'")

    # ── merge_projects ────────────────────────────────────────────────────────
    if name == "merge_projects":
        src, dst, count = db.merge_projects(args["from_project"], args["into_project"])
        return ok(f"Merged '{src}' into '{dst}' ({count} task(s) moved), removed '{src}'")

    # ── update_glossary ───────────────────────────────────────────────────────
    if name == "update_glossary":
        term      = args["term"].strip()
        canonical = args["canonical"].strip()
        db.set_glossary_term(term, canonical)
        return ok(f"Glossary: '{term}' → '{canonical}'")

    # ── remove_glossary ───────────────────────────────────────────────────────
    if name == "remove_glossary":
        term = args["term"].strip()
        db.remove_glossary_term(term)
        return ok(f"Glossary: removed '{term}'")

    # ── Journal ───────────────────────────────────────────────────────────────
    if name == "add_journal":
        j = db.add_journal(args["raw"], summary=args.get("summary"),
                           title=args.get("title"), date=args.get("date"), source="mcp")
        return ok(f"Journal saved: {jtag(j)}" + glossary_warning(j))

    if name == "list_journal":
        entries = db.list_journal(limit=args.get("limit") or 10, query=args.get("query"),
                                  since=args.get("since"), until=args.get("until"))
        if not entries:
            return ok("No journal entries found.")
        return ok("\n".join(fmt_journal_line(j) for j in entries))

    if name == "get_journal":
        include_raw = args.get("include_raw", True) not in (False, "false", "0")
        entries = db.get_journal(args.get("entry"))
        return ok("\n\n---\n\n".join(fmt_journal_detail(j, include_raw) for j in entries))

    if name == "update_journal":
        j = db.update_journal(args["entry"], raw=args.get("raw"), summary=args.get("summary"),
                              title=args.get("title"), date=args.get("date"))
        return ok(f"Journal updated: {jtag(j)}" + glossary_warning(j))

    # ── Activity ──────────────────────────────────────────────────────────────
    if name == "list_activity":
        events = db.list_activity(limit=args.get("limit") or 50, since=args.get("since"),
                                  until=args.get("until"), scope=args.get("scope"),
                                  task=args.get("task"), action=args.get("action"),
                                  project=args.get("project"))
        if not events:
            return ok("No activity found.")
        return ok("\n".join(fmt_event_line(e) for e in events))

    # ── apply_changes ─────────────────────────────────────────────────────────
    if name == "apply_changes":
        return ok(await _apply_changes(args["changes"]))

    # ── list_resources / get_resource (tool mirrors of the resources capability) ─
    if name == "list_resources":
        resources = await list_resources()
        return ok("\n".join(f"{r.uri} — {r.name}: {r.description}" for r in resources))

    if name == "get_resource":
        uri = args.get("uri", "").strip()
        if not uri:
            raise ValueError("uri is required")
        text = await read_resource(types.AnyUrl(uri))
        return ok(text)

    # ── list_prompts / get_prompt (tool mirrors of the prompts capability) ──────
    if name == "list_prompts":
        prompts = await list_prompts()
        return ok("\n\n".join(f"{p.name}: {p.description}" for p in prompts))

    if name == "get_prompt":
        prompt_name = args.get("name", "").strip()
        if not prompt_name:
            raise ValueError("name is required")
        result = await get_prompt(prompt_name, None)
        return ok(result.messages[0].content.text)

    return err(f"Unknown tool: {name}")


# ── Resources ─────────────────────────────────────────────────────────────────

@server.list_resources()
async def list_resources() -> list[types.Resource]:
    return [
        types.Resource(
            uri="toledo://status",
            name="Toledo Status",
            description="Live task summary grouped by state and project",
            mimeType="text/plain",
        ),
        types.Resource(
            uri="toledo://projects",
            name="Toledo Projects",
            description="Projects with their names, colors, and active task counts",
            mimeType="text/plain",
        ),
        types.Resource(
            uri="toledo://tags",
            name="Toledo Tags",
            description="Tags in use with their active and total task counts",
            mimeType="text/plain",
        ),
        types.Resource(
            uri="toledo://tasks/active",
            name="Active Tasks",
            description="All currently active tasks",
            mimeType="text/plain",
        ),
        types.Resource(
            uri="toledo://glossary",
            name="Toledo Glossary",
            description="Self-healing glossary of proper nouns/terms, mutated via update_glossary",
            mimeType="text/plain",
        ),
        types.Resource(
            uri="toledo://journal/recent",
            name="Recent Journal",
            description="The ten newest journal entries (id, date, title, excerpt)",
            mimeType="text/plain",
        ),
        types.Resource(
            uri="toledo://brief",
            name="Morning Brief",
            description="Latest morning brief (calendar, weather, nearby tech events) as Markdown",
            mimeType="text/markdown",
        ),
    ]


@server.read_resource()
async def read_resource(uri: types.AnyUrl) -> str:
    uri_str = str(uri)

    if uri_str == "toledo://status":
        result = await _dispatch("get_status", {})
        return result[0].text

    if uri_str == "toledo://projects":
        result = await _dispatch("list_projects", {})
        return result[0].text

    if uri_str == "toledo://tags":
        result = await _dispatch("list_tags", {})
        return result[0].text

    if uri_str == "toledo://tasks/active":
        result = await _dispatch("list_tasks", {"state": "active"})
        return result[0].text

    if uri_str == "toledo://glossary":
        glossary = db.load_glossary()
        if not glossary:
            return "No glossary entries yet."
        return "\n".join(f"{term} → {canonical}" for term, canonical in sorted(glossary.items()))

    if uri_str == "toledo://journal/recent":
        result = await _dispatch("list_journal", {"limit": 10})
        return result[0].text

    if uri_str == "toledo://brief":
        result = await _dispatch("get_brief", {})
        return result[0].text

    raise ValueError(f"Unknown resource: {uri_str}")


# ── Prompts ───────────────────────────────────────────────────────────────────

def _prompt_message(text: str) -> types.PromptMessage:
    return types.PromptMessage(role="user", content=types.TextContent(type="text", text=text))


END_OF_DAY_DUMP_PROMPT = """\
You are running Toledo's end-of-day brain dump ("end of day dump" / "daily brain dump").

The Goals category holds quarter-level intent, not work items. Never surface its entries as \
things to do, never suggest completing them, never include them in priority lists. Use them \
only to weigh what matters among real tasks.

When creating a task, apply an existing tag from the snapshot's tag list only when the \
task's own subject is that tag; do not invent new tags — a new tag with a single task on it is noise. A genuinely new tag is a \
structural decision that belongs in the periodic audit.

Follow this sequence:

1. Open with exactly this, and nothing else: "Ready for your daily dump. Start whenever you \
like. I'll stand by until you say end dump." Then enter collection mode and stay in it until \
the user says the exit phrase, end dump or dump complete. While in collection mode, every \
reply is at most three words of bare acknowledgment, for example okay, still here, go on. \
Never ask a question, offer a summary, reflect content back, or clarify anything, no matter \
how long the user pauses or how finished they sound. Silence, a trailing sentence, or an \
apparent conclusion is not the end. Only the exit phrase ends collection mode and advances to \
step 2.

2. Once they're done, check the dump against the glossary in the snapshot below. Scan it for proper nouns, \
project names, and terms that don't clearly match a glossary entry or an existing Toledo \
task/project name. List every proper noun in the dump that is absent from the glossary \
and Toledo, and ask about all of them in ONE message. Do not ask again. State no fact that is \
not in the dump or the snapshot. For each term the user resolves, record an update_glossary \
change (term → canonical form) for step 4 so it is never asked about again. The glossary is \
healed via that tool, not by editing this prompt. This round happens right after the exit \
phrase and before any write: ask about every unrecognized proper noun, name spelling, or place, \
never skipping it because the dump seemed clear. update_glossary is for misheard or misspelled terms and stable \
identities; skip correctly spelled names. When adding an alias to an existing entry, keep its \
canonical form and description and add only the new variant. On a correction, edit only the \
contradicted clause. Store only facts the user stated, nothing inferred or time-bound. If the \
user asked for a calendar entry, also ask which calendar in this round.

3. Cross-reference what the user mentioned against the active tasks in the snapshot. Where it's ambiguous whether something is done, still in \
progress, or abandoned, batch those into one more round of status questions.

4. Write back to Toledo from the answers in ONE apply_changes call:
   - update_glossary for each resolved term.
   - done_task for anything completed.
   - add_note on tasks that progressed but aren't done, summarizing what happened.
   - create_task only for work the user stated as something to do, with a concrete \
deliverable or deadline (its note and subtasks can ride along on the create). Leads, "maybe I \
should" and small follow-ups go in the journal only; if unsure, skip.
   - add_journal as the last change, saving today's journal (see step 5). Never call \
add_journal directly in this flow; it is the final op of the single apply_changes call.
   Use slugs from the snapshot for existing tasks and do not re-read what the snapshot \
already holds. Report any ✗ or ⚠ lines in the result.

5. The journal entry saved in step 4 has two parts, and both are run against the glossary: \
the snapshot's entries plus the terms resolved in step 2. Replace every misheard term, \
including near variants of a listed one, with its canonical form (the name itself, not the \
explanation that follows it). raw is otherwise the collection-mode messages verbatim and in \
order, fillers included, not cleaned up; post-dump answers go in summary only. summary is your revised write-up in Markdown: what happened, decisions and ideas \
worth keeping, and the task changes this session made. Leave the title empty unless the day \
has an obvious theme, and leave the date to default to today. The save result flags glossary \
terms left in the entry. Apply every correction to raw and summary before the first \
add_journal so the warning comes back clean; if it still appears, test the cause rather than \
dismissing it, then correct with update_journal. If a change in step 4 failed, likewise fix the summary with update_journal after dealing with the failure. Do not render \
the journal as an artifact unless the user asks; it lives in Toledo.

6. If the user asked for a calendar entry, create it once now with one calendar tool. Never \
re-create while a call is pending. Verify with one event search and report the stored status.

7. Close with a next-day priority list, built after all writes from the snapshot with this \
session's changes applied, so there is no need to re-read Toledo. Order: overdue first, then \
due within 7 days, then High. Show the weekday next to each due date. Output it as a list, \
weighted by urgency, not a fixed count.
"""

EVENT_REVIEW_PROMPT = """\
You are running Toledo's event review: a short, repeatable pass over upcoming tech events and \
meetups. The user invokes it whenever they have time; there is no fixed cadence. You work \
through one page of events at a time using list_events, get_event, dismiss_events, \
mute_events and restore_events. Events that already match a calendar entry are accepted \
automatically and never appear in the list.

Who the events are for: a Berlin-based senior technologist (about 20 years: VFX pipelines, \
infrastructure architecture, CTO and technical leadership, AI and generative systems) who is \
looking for a staff or senior individual-contributor role in platform engineering, developer \
tooling, backend systems or AI infrastructure, and who also freelances on AI integrations, \
generative workflows and cloud infrastructure. Interests: programming languages, software \
engineering, reliability, machine learning, UX, VR/AR, AI infrastructure and open models, \
developer tooling, and networking with engineers and technical leaders where it could help \
a job search. Anything that does not fit this is a non-fit. Exceptions: an event tied to a \
company the user has an active application with (see the active job tasks at the end) is \
kept active even when its format is not to their taste.

Follow this sequence:

1. Call list_events with no arguments. It returns about 10 upcoming events, soonest first, \
each with every calendar entry on its day. Read the calendar status line: if it says the \
calendar is stale or missing, do no conflict-based triage this pass.

2. Triage without asking, using dismiss_events with the ids:
   - Obvious non-fits per the profile above: dismiss.
   - A hard conflict: the event is flagged "overlaps" with a timed calendar entry, and the \
calendar status is current: dismiss.
   - An all-day entry is only a possible conflict, since it may really be at a specific time. \
The calendar is shared and some entries belong to someone else, so an overlapping entry is \
not proof either. Do not dismiss on all-day entries or holiday entries. Keep the event and \
mention the clash when you raise it.
   - A series that is clearly a non-fit: dismiss_events with similar=true on one of its ids.
   Everything else stays for step 3.

3. Open with one terse line naming what you dismissed and why, as ids and a few words each, \
so the user can reverse any of it. Then walk through the remainder, two or three at a time. \
Call get_event for each one you raise, since the list has no descriptions. Raise events \
relevant to career development and the job search first, and prefer sooner events, because \
registration is still open for them. For each give the date, title, one sentence on what it \
is, and any calendar clash.

4. Act on the user's decision for each event: dismiss_events to drop it for now, mute_events \
to hide it for good, or leave it active. If they ask to undo something, use restore_events.

5. When the page is done, continue with list_events after=<id of the last event on the \
page> whenever the user wants more. Do not offer to continue; wait to be told.

Interaction rules: be terse and direct. Ask no follow-up questions and add no affirmations \
or process explanations. Keep batches short. The user drives the conversation and you \
execute. Speak only in response to what they say, apart from the opening triage line and \
the first batch.
"""

SELF_DIAGNOSTIC_PROMPT = """\
You are running Toledo's self diagnostic ("self diagnostic" / "review this session").

Purpose: evaluate the conversation so far, find where a Toledo prompt or MCP tool did not \
behave as intended, and propose fixes. Do not change anything in Toledo until the user approves.

1. Identify the prompt or workflow under review (for example end_of_day_dump, \
morning_planning). Fetch its text with get_prompt so you compare behavior against the actual \
instructions, not memory.

2. Scan the conversation for issues:
   - Steps skipped, reordered, or done twice.
   - Tool calls that failed, returned warnings, or were misused (wrong arguments, redundant \
writes, overwritten data).
   - Questions asked that the prompt forbade, or questions that should have been asked and \
were not.
   - User corrections, repeated clarifications, or deleted/recreated items.
   - Facts stored that the user did not state (inferred or time-bound).
   - Warnings or errors dismissed without verifying the cause.

3. For each issue, decide the cause: (a) the base prompt was ambiguous, missing, or \
contradictory; (b) the prompt was clear and was not followed; (c) an MCP tool behaved \
unexpectedly or its description was misleading. State which, and quote the relevant prompt \
line if one exists.

4. Present a numbered list of proposed fixes. Each item: the issue, the cause category, and \
the concrete change (exact replacement prompt wording, a tool description change, or a code \
change). One line of wording per fix where possible. No preamble, no praise, no follow-up \
questions.

5. Wait for the user to select items ("all", "1,3", "none"). For each approved item, create \
one Toledo task: project Projects, tag toledo (existing tag only), description containing the \
issue, cause, and exact proposed change. Create them in one apply_changes call, then list the \
created task names. Create nothing for unapproved items.
"""

MORNING_PLANNING_PROMPT = """\
You are running Toledo's morning planning session ("what should I work on"). Its job is to \
support the user's own planning, not to interview them or steer them.

The Goals category holds quarter-level intent, not work items. Never surface its entries as \
things to do, never suggest completing them, never include them in priority lists. Use them \
only to weigh what matters among real tasks.

When creating a task, apply an existing tag from the snapshot's tag list if one fits; do not \
invent new tags — a new tag with a single task on it is noise. A genuinely new tag is a \
structural decision that belongs in the periodic audit.

Follow this sequence:

1. Before responding, read the most recent journal entry. It is included with the snapshot \
below; if it is not, fetch it. Treat the snapshot header's date and time as the current moment \
and compare it against the entry's submitted timestamp to judge how recent it is. Hold the \
entry and the snapshot as silent context for the whole session.

   The snapshot may end with a morning brief. Its calendar covers the next few days and \
its weather is one line; both are silent context like the rest. Its tech events/meetups are \
low priority: mention one only if the user asks or there is genuinely nothing else to discuss.

   Your entire opening message is "Ready." Then wait for the user to speak. No recap, no \
brief, no question, no agenda, no menu.

   Throughout the session, speak only in response to what the user says. Never volunteer \
information, never ask follow-up questions, never add affirmations or process explanations. \
The user drives; you execute.

   Wrap-up flag: when the user signals they are finished (in any phrasing, for example "that \
wraps it up for me"), reply with a terse flag as its own dedicated reply, after applying any \
pending writes. Never attach it to another reply, and never give it on a turn where the user \
has not signaled they are finished. If the user keeps talking instead, continue normally and \
flag at the next wrap-up signal.

   Choose flag items from the snapshot and the journal entry:
      - High priority tasks that are overdue, due within the next few days, or aligned with \
the Goals category.
      - High priority items from the journal entry that are not otherwise tracked.
      - Tasks that look stale by their upd: timestamp. Do not hard-filter out undated tasks.
   Exclude anything the user already addressed this session, including updates they gave that \
morning, and account for your own writes so nothing already done or moved is flagged. Keep it \
to a few items at most, one short line each, in generic shorthand ("the visa paperwork", "two \
chores") rather than full stored titles. Refer to Goals only as framing for weighing, never as \
items to do.

   If nothing is worth flagging, give a brief summary of the task list instead: how many \
tasks are overdue and roughly where, which areas are hot, and anything with a deadline in the \
next few days, grouped by the projects actually present in the snapshot.

2. Let the user steer. Do not offer a menu of categories or otherwise script the conversation. \
Follow where they take it, surfacing the relevant tasks weighted by urgency when asked.

3. Apply changes as they come up. When the user says something is done, a date should move, \
a task needs a note, a priority, project or tag should change, or something new should be tracked, \
write it straight away without asking for confirmation and without narrating the write. \
When several changes come up together, send them in one apply_changes call.
   - Refer to tasks by partial name or slug. Only rename changes a task's slug, and the old \
slug keeps resolving afterwards.
   - For a recurring task, done only advances its cycle.
   - The snapshot shows the state before the session; track your own writes on top of it \
so you do not suggest a task already called done or show a moved date as unmoved.
   - Mention a write only if it failed or matched more than one task (a ✗ or ⚠ line in \
the result), and ask how to resolve it.

4. Save a short morning journal entry when the user signals they are finished, silently and \
in the same apply_changes call as any pending writes (add_journal last), before the wrap-up \
flag. This is not the evening dump: keep it brief and capture only what the user said this \
session about their focus and alignment for the day, plus anything outstanding that is not \
already tracked in Toledo. Run both parts against the glossary in the snapshot, replacing every \
misheard term with its canonical form. If a name, spelling or place is unrecognized, ask about \
it in one batched question before writing, and when aliasing an existing glossary entry keep its \
canonical form and add only the new variant, storing only facts the user stated. raw is those statements as the user gave them; summary \
is a few lines of Markdown. Set the title to "Morning focus" so it is distinguishable from the \
evening entry, and leave the date to default to today. Skip the entry if the session produced \
no focus or outstanding items. Mention the save only if it failed or the result flags glossary \
terms, which you correct with update_journal. Do not render the journal as an artifact unless \
the user asks.
"""

PERIODIC_AUDIT_PROMPT = """\
You are running Toledo's periodic audit and goals refinement (roughly every 3–6 months). \
This is a structural review, not daily triage — day-to-day drift is already handled by the \
morning planning prompt, which applies its changes as they come up.

The Goals category holds quarter-level intent, not work items — elsewhere it should never be \
surfaced as things to do or included in priority lists. Step 5 below is the exception: this \
audit is where goal entries themselves get reviewed and revised.

Follow this sequence:

1. Review every task in the snapshot below for staleness: tasks that no longer matter, \
duplicates, or things quietly superseded. Confirm with the user before archiving (move_task \
to archive) or permanently deleting (delete_task) anything.

2. Review the category structure itself (the snapshot's project list): rename, split, merge, or \
otherwise refine categories to make daily use easier. The current categories are not \
guaranteed to be optimal long-term — don't assume they are. Use rename_project to rename a \
category (its tasks stay in it), merge_projects to fold one category into another, and \
add_project/remove_project for brand-new or now-empty categories.

3. Reassign individually miscategorized tasks to better-fitting categories via reproject_task.
Review the tags too (the snapshot's tag list): fold near-duplicates together with rename_tag, \
and add or drop tags on tasks with tag_task / untag_task where that makes filtering easier. \
Tags cut across projects, so a theme that spans several categories may be better as a tag \
than as a new project.

Apply the changes confirmed in each of the steps above with one apply_changes call per step, \
not one call per change.

4. Open a capture window: ask the user for anything new — tasks, ideas, projects — that \
surfaced during this review. Similar in spirit to the evening brain dump, but focused on \
structure rather than daily narrative. Create tasks via create_task for anything raised.

5. Sanity-check the goals category (quarter-level overarching targets): does it exist, is it \
stale, does it need updating? If no goals project/category exists yet, offer to create one. \
Week-to-week goal adjustments happen in the morning planning prompt, not here — this is \
just a staleness check.

6. Before finishing, make sure a recurring Toledo task exists that reminds the user to re-run \
this audit in 3–6 months (self-referential: it should surface again through the morning \
prompt). Create or update one via create_task / set_due if it's missing or overdue.
"""


@server.list_prompts()
async def list_prompts() -> list[types.Prompt]:
    return [
        types.Prompt(
            name="end_of_day_dump",
            description=(
                "End-of-day / daily brain dump: freeform talk, reconcile against Toledo "
                "tasks and a self-healing glossary, write back updates, save the day's "
                "journal to Toledo, and close with next-day priorities. "
                "Trigger phrases: 'end of day dump', 'daily brain dump'."
            ),
        ),
        types.Prompt(
            name="morning_planning",
            description=(
                "Morning 'what should I work on' session: opens with a bare 'Ready.', "
                "then an open conversation the user steers, with changes applied as "
                "they come up and a terse flag only at wrap-up."
            ),
        ),
        types.Prompt(
            name="event_review",
            description=(
                "Review upcoming tech events/meetups a page at a time: auto-dismiss non-fits and "
                "clear calendar conflicts, then walk through the rest and dismiss, mute or keep. "
                "Trigger phrases: 'event review', 'review events'."
            ),
        ),
        types.Prompt(
            name="self_diagnostic",
            description=(
                "Review the session so far for places a Toledo prompt or MCP tool misbehaved, "
                "propose fixes, and file approved ones as Toledo tasks. "
                "Trigger phrases: 'self diagnostic', 'review this session'."
            ),
        ),
        types.Prompt(
            name="periodic_audit",
            description=(
                "Periodic (3-6 month) deep audit: prune stale tasks, refine categories, "
                "recategorize, open a capture window, and sanity-check quarter-level goals."
            ),
        ),
    ]


@server.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    prompts = {
        "end_of_day_dump":   END_OF_DAY_DUMP_PROMPT,
        "morning_planning":  MORNING_PLANNING_PROMPT,
        "periodic_audit":    PERIODIC_AUDIT_PROMPT,
        "event_review":      EVENT_REVIEW_PROMPT,
        "self_diagnostic":   SELF_DIAGNOSTIC_PROMPT,
    }
    if name not in prompts:
        raise ValueError(f"Unknown prompt: {name}")
    if name == "event_review":  # no full snapshot: events and a few job tasks are all it needs
        return types.GetPromptResult(messages=[_prompt_message(prompts[name] + await _event_review_context())])
    if name == "self_diagnostic":  # works from the conversation and get_prompt, so no snapshot
        return types.GetPromptResult(messages=[_prompt_message(prompts[name])])
    text = prompts[name] + await _snapshot("active")
    if name == "morning_planning":
        text += _latest_journal_section()
        text += _latest_brief_section()
    return types.GetPromptResult(messages=[_prompt_message(text)])


async def _event_review_context() -> str:
    """What event_review needs beyond list_events: today's date and the active job-search tasks, so events tied to
    a company with an open application are kept."""
    try:
        jobs = (await _dispatch("list_tasks", {"state": "active", "project": "Job/Work"}))[0].text
    except Exception:
        jobs = "(could not load)"
    return f"\n\n---\n# Today\n{datetime.now():%A %Y-%m-%d %H:%M}\n\n# Active job tasks\n{jobs}\n"


def _latest_journal_section() -> str:
    """Newest journal entry, appended for morning_planning so step 1 needs no fetch."""
    try:
        entry = db.get_journal()[0]
    except db.NotFound:
        return "\n\n---\n# Latest journal entry\nNone saved yet.\n"
    return f"\n\n---\n# Latest journal entry\n{fmt_journal_detail(entry, include_raw=False)}\n"


def _latest_brief_section() -> str:
    """Latest morning brief (calendar/weather/events), appended for morning_planning so a
    claude.ai session doesn't need a separate get_brief call to see it."""
    return f"\n\n---\n# Morning brief\n{read_brief()}\n"


async def _snapshot(state: str) -> str:
    """Current Toledo state appended to a prompt, so a session starts without reads."""
    now = datetime.now()
    tasks    = (await _dispatch("list_tasks", {"state": state}))[0].text
    projects = (await _dispatch("list_projects", {}))[0].text
    tags     = (await _dispatch("list_tags", {}))[0].text
    glossary = await read_resource(types.AnyUrl("toledo://glossary"))
    return (
        f"\n\n---\n# Toledo snapshot — {now:%A %Y-%m-%d %H:%M}\n"
        "Fetched along with these instructions. It stands in for list_tasks, "
        "list_projects, list_tags, and the glossary resource at the start of the session; reuse it "
        "instead of re-reading, and track your own writes on top of it.\n\n"
        f"## Tasks ({state})\n{tasks}\n\n"
        f"## Projects\n{projects}\n\n"
        f"## Tags\n{tags}\n\n"
        f"## Glossary\n{glossary}\n"
    )


# ── Starlette / Streamable HTTP transport ──────────────────────────────────────

session_manager = StreamableHTTPSessionManager(app=server)


class StreamableHTTPASGIApp:
    """Plain ASGI callable so Starlette routes to it directly instead of
    treating it as a request/response endpoint function."""

    async def __call__(self, scope, receive, send):
        await session_manager.handle_request(scope, receive, send)


@contextlib.asynccontextmanager
async def lifespan(app):
    async with session_manager.run():
        yield


starlette_app = Starlette(
    routes=[
        Route("/mcp/sse", endpoint=StreamableHTTPASGIApp(), methods=["GET", "POST", "DELETE"]),
    ],
    lifespan=lifespan,
)

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Toledo MCP Server")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8001)
    a = parser.parse_args()
    print(f"Toledo MCP server on http://{a.host}:{a.port}/mcp/sse")
    uvicorn.run(starlette_app, host=a.host, port=a.port)
