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
import contextlib
from datetime import datetime, timedelta

import mcp.types as types
import uvicorn
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Route

import toledo_db as db

# ── Helpers ───────────────────────────────────────────────────────────────────

def pri_label(n: int) -> str:
    n = int(n)
    if n <= 24:  return "Ultra High"
    if n == 25:  return "High"
    if n <= 49:  return "Med-High"
    if n == 50:  return "Medium"
    if n <= 74:  return "Med-Low"
    if n == 75:  return "Low"
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
    return (
        f"[{d['pri_label']:10s}] [{d['project_name']:12s}] {d['name']}"
        f"  ({d['slug']}){due}{rec}{subs}{upd}"
    )


def fmt_task_detail(d: dict) -> str:
    lines = [
        f"# {d['name']}",
        f"Slug:     {d['slug']}",
        f"State:    {d['state']}",
        f"Priority: {d['priority']} — {d['pri_label']}",
        f"Project:  {d['project_name']} ({d['project']})",
    ]
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
            lines.append(f"  {mark} {s['name']}  ({s['slug']})")

    if d.get("description", "").strip():
        lines.append("\n## Description")
        lines.append(d["description"].strip())

    if d.get("notes"):
        lines.append("\n## Notes")
        lines.append(db.render_worklog(d["notes"]).strip())

    if d.get("activity"):
        lines.append("\n## Recent Activity")
        for e in reversed(d["activity"][-10:]):
            ts     = e.get("ts", "")[:16].replace("T", " ")
            action = e.get("action", "")
            rest   = {k: v for k, v in e.items() if k not in ("ts", "action")}
            extra  = "  " + "  ".join(f"{k}={v}" for k, v in rest.items()) if rest else ""
            lines.append(f"  {ts}  {action}{extra}")

    return "\n".join(lines)


def ok(text: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=text)]


def err(text: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=f"Error: {text}")]


# ── MCP Server ────────────────────────────────────────────────────────────────

# Sent to the client on connect. Keep it short: it lands in the model's context
# every session, and the prompts themselves are fetched on demand.
SERVER_INSTRUCTIONS = """\
Toledo is the user's task manager. Tasks are addressed by partial name or slug.

Toledo ships guided-session prompts:
- morning_planning: start-of-day "what should I work on" session
- end_of_day_dump: end-of-day brain dump reconciled against tasks and the glossary
- periodic_audit: infrequent deep audit of tasks, categories, and goals

When the user asks for one of these sessions (e.g. "let's plan my day", "end of day \
dump"), fetch its full instructions first and follow them.

Claude.ai's web interface does not support MCP prompts natively, so fetch them with \
the get_prompt tool (name = the prompt name; list_prompts shows what exists). Clients \
that do surface MCP prompts can use them directly.

Resources (status, projects, active tasks, glossary) are likewise available through \
the list_resources and get_resource tools.
"""

server = Server("toledo", instructions=SERVER_INSTRUCTIONS)

# ── Tool definitions ──────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_tasks",
            description=(
                "List tasks. By default returns active tasks. "
                "Filter by state (active/completed/archive/all) and/or project code. "
                "Each task shows its last-updated timestamp; sort or filter by recency "
                "with 'sort' and 'updated_within_days'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "state":   {"type": "string", "enum": ["active","completed","archive","all"],
                                "description": "Filter by task state (default: active)"},
                    "project": {"type": "string",
                                "description": "Filter by project code (e.g. JOB, HLT)"},
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
                    "task": {"type": "string", "description": "Partial task name or full slug"},
                },
                "required": ["task"],
            },
        ),
        types.Tool(
            name="create_task",
            description="Create a new task in the active state.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name":        {"type": "string", "description": "Task name"},
                    "project":     {"type": "string", "description": "Project code (e.g. JOB). Defaults to GEN"},
                    "priority":    {"type": "integer", "description": "Priority 1–99 (lower = higher priority). Default 50"},
                    "due":         {"type": "string", "description": "Due date YYYY-MM-DD"},
                    "recurrence":  {"type": "integer", "description": "Repeat every N days"},
                    "description": {"type": "string", "description": "Task description (Markdown)"},
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
                    "task": {"type": "string", "description": "Partial task name or slug"},
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
                    "task":  {"type": "string"},
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
                    "task": {"type": "string", "description": "Partial task name or slug"},
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
                    "task": {"type": "string"},
                    "name": {"type": "string", "description": "New name"},
                },
                "required": ["task", "name"],
            },
        ),
        types.Tool(
            name="reprioritize_task",
            description="Change a task's priority (1–99, lower = more urgent).",
            inputSchema={
                "type": "object",
                "properties": {
                    "task":     {"type": "string"},
                    "priority": {"type": "integer", "minimum": 1, "maximum": 99},
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
                    "task":    {"type": "string"},
                    "project": {"type": "string", "description": "Target project code"},
                },
                "required": ["task", "project"],
            },
        ),
        types.Tool(
            name="set_due",
            description="Set or update a task's due date.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
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
                    "task":     {"type": "string"},
                    "interval": {"type": "integer", "description": "Repeat every N days, or 0 to clear recurrence"},
                },
                "required": ["task", "interval"],
            },
        ),
        types.Tool(
            name="add_note",
            description="Append a timestamped note to a task's worklog.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["task", "note"],
            },
        ),
        types.Tool(
            name="update_description",
            description="Replace a task's description (Markdown).",
            inputSchema={
                "type": "object",
                "properties": {
                    "task":        {"type": "string"},
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
                    "priority": {"type": "integer", "default": 50},
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
            name="list_projects",
            description="List all projects with their codes, names, and colors.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="add_project",
            description="Add a new project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "code":  {"type": "string", "description": "Short code (e.g. WEB), max 8 chars"},
                    "name":  {"type": "string", "description": "Display name"},
                    "color": {"type": "string", "description": "Hex color e.g. #3498db"},
                },
                "required": ["code", "name"],
            },
        ),
        types.Tool(
            name="remove_project",
            description="Remove a project by code.",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                },
                "required": ["code"],
            },
        ),
        types.Tool(
            name="rename_project",
            description=(
                "Rename a project's code (e.g. PRJ -> WORK), carrying every task and "
                "subtask in it along in one operation. Fails without "
                "changing anything if the new code already exists as a distinct project "
                "— use merge_projects for that case instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "old_code": {"type": "string", "description": "Existing project code"},
                    "new_code": {"type": "string", "description": "New project code"},
                },
                "required": ["old_code", "new_code"],
            },
        ),
        types.Tool(
            name="merge_projects",
            description=(
                "Merge one project into another: moves every task and subtask from "
                "from_code to into_code, then removes from_code from the project "
                "registry. into_code must already exist."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "from_code": {"type": "string", "description": "Project code to merge away"},
                    "into_code": {"type": "string", "description": "Project code to merge into (must already exist)"},
                },
                "required": ["from_code", "into_code"],
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
            name="list_resources",
            description=(
                "List Toledo's MCP resources (uri, name, description) — status, projects, "
                "active tasks, glossary. Exists for clients that only surface MCP tools, not "
                "the resources capability; fetch a resource's contents with get_resource."
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
                "morning_planning, periodic_audit. Exists for clients that only surface MCP "
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
    "create_task": {"title": "name"},
    "rename_task": {"new_name": "name", "title": "name"},
    "add_subtask": {"title": "name"},
    "set_due":     {"date": "due", "due_date": "due"},
    "add_note":    {"text": "note", "content": "note"},
}

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
    aliases = {**_ARG_ALIASES_ALL, **_ARG_ALIASES.get(name, {})}
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
    args = _normalize_args(name, arguments or {})
    try:
        missing = [k for k in await _required_for(name) if k not in args]
        if missing:
            raise ValueError(
                f"Missing required argument(s) for '{name}': {', '.join(missing)}. "
                f"Got: {', '.join(sorted(args)) or '(none)'}"
            )
        return await _dispatch(name, args)
    except ValueError as e:
        return err(str(e))
    except Exception as e:
        return err(f"Unexpected error in '{name}': {e}")


async def _dispatch(name: str, args: dict) -> list[types.TextContent]:

    # ── list_tasks ────────────────────────────────────────────────────────────
    if name == "list_tasks":
        state_filter   = args.get("state", "active")
        project_filter = (args.get("project") or "").upper() or None
        sort           = args.get("sort", "default")
        updated_within = args.get("updated_within_days")
        if state_filter != "all" and state_filter not in db.STATES:
            raise ValueError(f"Invalid state '{state_filter}'")
        states   = db.STATES if state_filter == "all" else [state_filter]
        projects = db.list_projects()
        all_tasks = [decorate(d, projects) for d in db.list_tasks(states, project_filter)]
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
        task_name = args["name"].strip()
        d = db.create_task(
            task_name,
            project=args.get("project"),
            priority=args.get("priority"),
            due=args.get("due") or None,
            recurrence=args.get("recurrence") or None,
            description=args.get("description") or f"# {task_name}\n\n_No description._\n",
        )
        return ok(f"Created: {d['slug']}")

    # ── done_task ─────────────────────────────────────────────────────────────
    if name == "done_task":
        r = db.complete_task(args["task"], note=args.get("note"))
        if r["recurring"]:
            return ok(f"↻ Recurring task advanced. Next due: {r['next_due']}")
        return ok(f"✓ Completed: {r['task']['slug']}")

    # ── move_task ─────────────────────────────────────────────────────────────
    if name == "move_task":
        to_state = args["state"]
        if db.resolve_task(args["task"])["state"] == to_state:
            return ok(f"Already in '{to_state}'")
        d = db.move_task(args["task"], to_state)
        return ok(f"→ Moved '{d['slug']}' to {to_state}")

    # ── delete_task ───────────────────────────────────────────────────────────
    if name == "delete_task":
        d = db.delete_task(args["task"])
        return ok(f"🗑 Deleted: {d['slug']}")

    # ── rename_task ───────────────────────────────────────────────────────────
    if name == "rename_task":
        d = db.rename_task(args["task"], args["name"])
        return ok(f"Renamed → {d['name']}  [{d['slug']}]")

    # ── reprioritize_task ─────────────────────────────────────────────────────
    if name == "reprioritize_task":
        d = db.set_priority(args["task"], args["priority"])
        return ok(f"Priority → {d['priority']} ({pri_label(d['priority'])})  [{d['slug']}]")

    # ── reproject_task ────────────────────────────────────────────────────────
    if name == "reproject_task":
        d = db.set_project(args["task"], args["project"])
        return ok(f"Project → {d['project']} ({db.project_name(d['project'])})  [{d['slug']}]")

    # ── set_due ───────────────────────────────────────────────────────────────
    if name == "set_due":
        d = db.set_due(args["task"], args.get("due"))
        return ok(f"Due date set to {d['due']}" if d["due"] else "Due date cleared")

    # ── set_recurrence ────────────────────────────────────────────────────────
    if name == "set_recurrence":
        d = db.set_recurrence(args["task"], args.get("interval"))
        if d["recurrence"]:
            return ok(f"Recurrence set to every {d['recurrence']} days")
        return ok("Recurrence cleared")

    # ── add_note ──────────────────────────────────────────────────────────────
    if name == "add_note":
        d = db.add_note(args["task"], args["note"])
        return ok(f"Note added to {d['slug']}")

    # ── update_description ────────────────────────────────────────────────────
    if name == "update_description":
        d = db.set_description(args["task"], args.get("description", ""))
        return ok(f"Description updated for {d['slug']}")

    # ── add_subtask ───────────────────────────────────────────────────────────
    if name == "add_subtask":
        s = db.add_subtask(args["task"], args["name"],
                           priority=args.get("priority"), due=args.get("due"))
        return ok(f"Subtask created: {s['slug']}")

    # ── done_subtask ──────────────────────────────────────────────────────────
    if name == "done_subtask":
        s = db.set_subtask_state(args["task"], args["subtask"], "completed", first_match=True)
        return ok(f"✓ Subtask done: {s['slug']}")

    # ── delete_subtask ────────────────────────────────────────────────────────
    if name == "delete_subtask":
        s = db.delete_subtask(args["task"], args["subtask"])
        return ok(f"🗑 Deleted subtask: {s['slug']} ({s['state']})")

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
            for proj_code, proj_tasks in sorted(by_proj.items()):
                lines.append(f"  {proj_tasks[0]['project_name']} ({proj_code}) — {len(proj_tasks)} task(s)")
                for d in proj_tasks:
                    due = f"  due:{('⚠' if d['overdue'] else '')}{d['due']}" if d["due"] else ""
                    lines.append(f"    • {d['name']}{due}")
            lines.append("")
        return ok("\n".join(lines))

    # ── list_projects ─────────────────────────────────────────────────────────
    if name == "list_projects":
        projects = db.list_projects()
        if not projects:
            return ok("No projects defined.")
        lines = [f"{'CODE':<8}  {'NAME':<20}  COLOR"]
        lines.append("-" * 40)
        for code, val in sorted(projects.items()):
            lines.append(f"{code:<8}  {val['name']:<20}  {val['color']}")
        return ok("\n".join(lines))

    # ── add_project ───────────────────────────────────────────────────────────
    if name == "add_project":
        code  = args["code"].upper().strip()
        pname = args["name"].strip()
        color = args.get("color") or ""
        db.save_project(code, pname, color)
        return ok(f"✓ Project '{code}' = '{pname}'" + (f"  {color}" if color else ""))

    # ── remove_project ────────────────────────────────────────────────────────
    if name == "remove_project":
        code = args["code"].upper().strip()
        db.remove_project(code)
        return ok(f"Removed project '{code}'")

    # ── rename_project ────────────────────────────────────────────────────────
    if name == "rename_project":
        old_code = args["old_code"].upper().strip()
        new_code = args["new_code"].upper().strip()
        count = db.rename_project(old_code, new_code)
        return ok(f"Renamed project '{old_code}' → '{new_code}' ({count} task(s) updated)")

    # ── merge_projects ────────────────────────────────────────────────────────
    if name == "merge_projects":
        from_code = args["from_code"].upper().strip()
        into_code = args["into_code"].upper().strip()
        count = db.merge_projects(from_code, into_code)
        return ok(f"Merged '{from_code}' into '{into_code}' ({count} task(s) moved), removed '{from_code}'")

    # ── update_glossary ───────────────────────────────────────────────────────
    if name == "update_glossary":
        term      = args["term"].strip()
        canonical = args["canonical"].strip()
        db.set_glossary_term(term, canonical)
        return ok(f"Glossary: '{term}' → '{canonical}'")

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
            description="Project registry with codes and names",
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

    if uri_str == "toledo://tasks/active":
        result = await _dispatch("list_tasks", {"state": "active"})
        return result[0].text

    if uri_str == "toledo://glossary":
        glossary = db.load_glossary()
        if not glossary:
            return "No glossary entries yet."
        return "\n".join(f"{term} → {canonical}" for term, canonical in sorted(glossary.items()))

    raise ValueError(f"Unknown resource: {uri_str}")


# ── Prompts ───────────────────────────────────────────────────────────────────

def _prompt_message(text: str) -> types.PromptMessage:
    return types.PromptMessage(role="user", content=types.TextContent(type="text", text=text))


END_OF_DAY_DUMP_PROMPT = """\
You are running Toledo's end-of-day brain dump ("end of day dump" / "daily brain dump"). \
Follow this sequence:

1. Invite the user to talk freely about their day — no structure imposed, no questions yet. \
Let them dump everything: what they did, what came up, half-formed ideas, names, decisions. \
Do not interrupt to ask clarifying questions during this phase.

2. Once they're done, read the toledo://glossary resource. Scan their dump for proper nouns, \
project names, and terms that don't clearly match a glossary entry or an existing Toledo \
task/project name. Collect every ambiguous term into ONE batched round of clarifying \
questions — never one at a time. For each term the user resolves, call update_glossary to \
persist the mapping (term → canonical form) so it is never asked about again. The glossary is \
healed via that tool, not by editing this prompt.

3. Call list_tasks (state=active) to get current open tasks. Cross-reference what the user \
mentioned against that list. Where it's ambiguous whether something is done, still in \
progress, or abandoned, batch those into one more round of status questions.

4. Write back to Toledo from the answers:
   - done_task for anything completed.
   - add_note on tasks that progressed but aren't done, summarizing what happened.
   - create_task for anything mentioned that isn't already tracked.

5. Generate a dated Markdown artifact ("Toledo Journal — YYYY-MM-DD") summarizing the raw dump \
and the outcomes of this session, as a journal stub until an Obsidian integration replaces this \
step. Rendering that artifact is on you, the calling agent — the Toledo server has no part in it.

6. Close by surfacing a short next-day priority list pulled from the now-updated Toledo state \
(list_tasks and/or upcoming_tasks). Weight it by urgency, not a fixed count — a few Ultra High \
or overdue items beats padding out a round number.
"""

MORNING_PLANNING_PROMPT = """\
You are running Toledo's morning planning session ("what should I work on"). Changes that \
come up during the session are held and committed together at the end, not applied live. Tell \
the user this at the start, in one line. Follow this sequence:

1. Call list_tasks (state=active) and derive the distinct categories/projects actually \
present — do not hard-code a category list, since categories get renamed, split, or merged \
during the periodic audit. Use list_projects for display names.

2. Ask the user which category/lane to focus on today (for example freelance income, \
household, personal projects). If a dedicated goals project/category exists (quarter-level \
targets set during the periodic audit), you may surface relevant goals to help them choose.

3. Within the chosen category, call list_tasks filtered to that project — use sort=recent \
where it helps — and surface tasks weighted by urgency: approaching deadlines and recurring \
tasks nearing their cycle date first. Do NOT hard-filter out undated tasks; many chores and \
goals have no due date and are still worth surfacing.

4. Stay interactive throughout the conversation, but HOLD changes instead of writing them. \
If the user mentions in passing that something is already done, a date should move, a task \
needs a note, a priority or project should change, or something new should be tracked, do NOT \
call any write tool yet (done_task, set_due, add_note, reprioritize_task, reproject_task, \
rename_task, create_task, add_subtask, ...). Record it in a running pending-changes list \
kept in the conversation, and acknowledge it in a few words.
   - Refer to tasks by partial name or slug. Only rename changes a task's slug, and the old \
slug keeps resolving afterwards.
   - Coalesce as you go: the latest value wins per field on a task (two due dates become \
one). A done supersedes earlier due/priority edits on the same task but keeps its notes. \
For a recurring task, done only advances its cycle, so say that in the summary.
   - Overlay the pending list on anything you surface. list_tasks still shows the stored \
state, so do not suggest a task the user already called done, and show moved dates as moved.
   - Restate the pending list briefly when it grows, or when the user switches category, so \
it survives a long conversation.

5. Where useful, check recency (sort=recent / updated_within_days) within the selected \
category so a stale-looking task doesn't get silently skipped.

6. Commit when the user marks the session done ("done", "wrap up", "that's it"). Show the \
coalesced pending list and ask once for confirmation, letting them drop or edit items. On \
confirmation, apply it as ordinary tool calls:
   - Create new tasks first, then apply edits to existing ones, and apply any rename last \
for each task.
   - Do not roll back and do not stop on a failure. Skip only changes that depended on the \
failed one (for example a note for a task whose create_task failed), and keep going.
   - Finish with a short succeeded / failed list, and offer to retry the failures.
   If the user signals they are leaving (thanks, bye, going quiet) while changes are still \
pending, ask whether to commit them before they go. With nothing pending, there is nothing to \
do. Uncommitted changes are lost when the conversation ends, and the evening dump reads \
Toledo's stored state.
"""

PERIODIC_AUDIT_PROMPT = """\
You are running Toledo's periodic audit and goals refinement (roughly every 3–6 months). \
This is a structural review, not daily triage — day-to-day drift is already handled by the \
morning planning prompt, which commits its changes at the end of each session. Follow this sequence:

1. Call list_tasks (state=all) and review for staleness: tasks that no longer matter, \
duplicates, or things quietly superseded. Confirm with the user before archiving (move_task \
to archive) or permanently deleting (delete_task) anything.

2. Call list_projects and review the category structure itself: rename, split, merge, or \
otherwise refine categories to make daily use easier. The current categories are not \
guaranteed to be optimal long-term — don't assume they are. Use rename_project to rename a \
code in place (carries every task/subtask in it along), merge_projects to fold one category \
into another, and add_project/remove_project for brand-new or now-empty categories.

3. Reassign individually miscategorized tasks to better-fitting categories via reproject_task.

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
                "tasks and a self-healing glossary, write back updates, and close with a "
                "journal summary and next-day priorities. "
                "Trigger phrases: 'end of day dump', 'daily brain dump'."
            ),
        ),
        types.Prompt(
            name="morning_planning",
            description=(
                "Morning 'what should I work on' session: pick a category, surface "
                "urgency-weighted tasks in it, and hold changes that come up in "
                "conversation, committing them together once you mark the session done."
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
    }
    if name not in prompts:
        raise ValueError(f"Unknown prompt: {name}")
    return types.GetPromptResult(messages=[_prompt_message(prompts[name])])


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
