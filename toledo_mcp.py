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
import importlib.machinery
import importlib.util
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import mcp.types as types
import uvicorn
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.routing import Route

# ── Load toledo module (no .py extension) ─────────────────────────────────────
_path   = str(Path(__file__).parent / "toledo")
_loader = importlib.machinery.SourceFileLoader("toledo", _path)
_spec   = importlib.util.spec_from_loader("toledo", _loader, origin=_path)
t       = importlib.util.module_from_spec(_spec)
_loader.exec_module(t)

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


def proj_name(code: str) -> str:
    projects = t.load_projects()
    val = projects.get(code)
    if isinstance(val, dict):
        return val.get("name", code)
    return str(val) if val else code


def task_to_dict(folder: Path, state: str, detail: bool = False) -> dict:
    info = t.parse_task_slug(folder.name)
    df = folder / "due.txt"
    rf = folder / "recurrence.txt"
    result = {
        "slug":        folder.name,
        "state":       state,
        "priority":    info["priority"],
        "pri_label":   pri_label(info["priority"]),
        "project":     info["project"],
        "project_name": proj_name(info["project"]),
        "name":        info["name"],
        "due":         df.read_text().strip() if df.exists() else None,
        "recurrence":  int(rf.read_text().strip()) if rf.exists() else None,
        "overdue":     t.is_overdue(folder),
    }
    if detail:
        desc_f = folder / "description.md"
        wlog_f = folder / "worklog.md"
        log_f  = folder / "activity.log"
        result["description"] = desc_f.read_text() if desc_f.exists() else ""
        result["worklog"]     = wlog_f.read_text()  if wlog_f.exists() else ""
        subtasks = []
        for ss in t.SUBTASK_STATES:
            sub_dir = folder / "subtasks" / ss
            if sub_dir.exists():
                for sub in sorted(sub_dir.iterdir()):
                    if sub.is_dir():
                        si = t.parse_task_slug(sub.name)
                        subtasks.append({
                            "slug":  sub.name,
                            "name":  si["name"],
                            "state": ss,
                        })
        result["subtasks"] = subtasks
        if log_f.exists():
            entries = []
            for line in log_f.read_text().splitlines():
                try:    entries.append(json.loads(line))
                except: pass
            result["activity"] = entries
    return result


def fmt_task_line(d: dict) -> str:
    overdue = "⚠ " if d["overdue"] else ""
    due     = f"  due:{overdue}{d['due']}" if d["due"] else ""
    rec     = f"  ↻{d['recurrence']}d"    if d["recurrence"] else ""
    subs    = ""
    if d.get("subtasks"):
        done  = sum(1 for s in d["subtasks"] if s["state"] == "completed")
        total = len(d["subtasks"])
        subs  = f"  [{done}/{total} subtasks]"
    return (
        f"[{d['pri_label']:10s}] [{d['project_name']:12s}] {d['name']}"
        f"  ({d['slug']}){due}{rec}{subs}"
    )


def fmt_task_detail(d: dict) -> str:
    lines = [
        f"# {d['name']}",
        f"Slug:     {d['slug']}",
        f"State:    {d['state']}",
        f"Priority: {d['priority']} — {d['pri_label']}",
        f"Project:  {d['project_name']} ({d['project']})",
    ]
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

    if d.get("worklog", "").strip():
        lines.append("\n## Notes")
        lines.append(d["worklog"].strip())

    if d.get("activity"):
        lines.append("\n## Recent Activity")
        for e in reversed(d["activity"][-10:]):
            ts     = e.get("ts", "")[:16].replace("T", " ")
            action = e.get("action", "")
            rest   = {k: v for k, v in e.items() if k not in ("ts", "action")}
            extra  = "  " + "  ".join(f"{k}={v}" for k, v in rest.items()) if rest else ""
            lines.append(f"  {ts}  {action}{extra}")

    return "\n".join(lines)


def resolve_task(partial: str):
    """Return (folder, state) or raise ValueError."""
    r = t.find_task(partial)
    if not r:
        raise ValueError(f"No task matching '{partial}'")
    return r


def ok(text: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=text)]


def err(text: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=f"Error: {text}")]


# ── MCP Server ────────────────────────────────────────────────────────────────

server = Server("toledo")

# ── Tool definitions ──────────────────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_tasks",
            description=(
                "List tasks. By default returns active tasks. "
                "Filter by state (active/completed/archive/all) and/or project code."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "state":   {"type": "string", "enum": ["active","completed","archive","all"],
                                "description": "Filter by task state (default: active)"},
                    "project": {"type": "string",
                                "description": "Filter by project code (e.g. JOB, HLT)"},
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
                "For recurring tasks this advances the due date instead of completing it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Partial task name or slug"},
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
                    "date": {"type": "string", "description": "YYYY-MM-DD, or empty string to clear"},
                },
                "required": ["task", "date"],
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
    ]


# ── Tool handlers ─────────────────────────────────────────────────────────────

@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    args = arguments or {}
    try:
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
        states = t.STATES if state_filter == "all" else [state_filter]
        lines  = []
        for state in states:
            sd = t.get_tasks_dir() / state
            if not sd.exists():
                continue
            tasks = [f for f in sorted(sd.iterdir()) if f.is_dir()]
            if project_filter:
                tasks = [f for f in tasks if t.parse_task_slug(f.name)["project"] == project_filter]
            if not tasks:
                continue
            if state_filter == "all":
                lines.append(f"\n── {state.upper()} ──")
            for f in tasks:
                lines.append(fmt_task_line(task_to_dict(f, state)))
        if not lines:
            return ok("No tasks found.")
        return ok("\n".join(lines))

    # ── get_task ──────────────────────────────────────────────────────────────
    if name == "get_task":
        folder, state = resolve_task(args["task"])
        return ok(fmt_task_detail(task_to_dict(folder, state, detail=True)))

    # ── create_task ───────────────────────────────────────────────────────────
    if name == "create_task":
        task_name = args["name"].strip()
        if not task_name:
            raise ValueError("name is required")
        priority = int(args.get("priority") or 50)
        project  = (args.get("project") or "GEN").upper()
        due      = args.get("due") or None
        recur    = args.get("recurrence") or None
        desc     = args.get("description") or None
        slug     = t.make_task_slug(priority, project, task_name)
        folder   = t.get_tasks_dir() / "active" / slug
        if folder.exists():
            raise ValueError(f"Task '{slug}' already exists")
        t.ensure_dir(folder)
        (folder / "description.md").write_text(
            desc if desc else f"# {task_name}\n\n_No description._\n"
        )
        if due:
            (folder / "due.txt").write_text(due)
        if recur:
            (folder / "recurrence.txt").write_text(str(recur))
        t.append_log(folder, "created", state="active", priority=priority, project=project)
        return ok(f"Created: {slug}")

    # ── done_task ─────────────────────────────────────────────────────────────
    if name == "done_task":
        folder, state = resolve_task(args["task"])
        rf = folder / "recurrence.txt"
        if rf.exists():
            # Recurring: advance due date
            period = int(rf.read_text().strip())
            df = folder / "due.txt"
            if df.exists():
                from_date = datetime.strptime(df.read_text().strip(), "%Y-%m-%d")
            else:
                from_date = datetime.now()
            next_due = (from_date + timedelta(days=period)).strftime("%Y-%m-%d")
            df.write_text(next_due)
            t.append_log(folder, "done_recurring", next_due=next_due)
            return ok(f"↻ Recurring task advanced. Next due: {next_due}")
        else:
            dst = t.get_tasks_dir() / "completed" / folder.name
            t.ensure_dir(dst.parent)
            folder.rename(dst)
            if t.get_context() == folder.name:
                t.set_context("")
            t.append_log(dst, "completed")
            return ok(f"✓ Completed: {folder.name}")

    # ── move_task ─────────────────────────────────────────────────────────────
    if name == "move_task":
        folder, cur_state = resolve_task(args["task"])
        to_state = args["state"]
        if to_state not in t.STATES:
            raise ValueError(f"Invalid state '{to_state}'")
        if cur_state == to_state:
            return ok(f"Already in '{to_state}'")
        dst = t.get_tasks_dir() / to_state / folder.name
        t.ensure_dir(dst.parent)
        folder.rename(dst)
        if t.get_context() == folder.name:
            t.set_context("")
        t.append_log(dst, "state_changed", from_state=cur_state, to_state=to_state)
        return ok(f"→ Moved '{folder.name}' to {to_state}")

    # ── delete_task ───────────────────────────────────────────────────────────
    if name == "delete_task":
        import shutil
        folder, _ = resolve_task(args["task"])
        name_str = folder.name
        shutil.rmtree(folder)
        if t.get_context() == name_str:
            t.set_context("")
        return ok(f"🗑 Deleted: {name_str}")

    # ── rename_task ───────────────────────────────────────────────────────────
    if name == "rename_task":
        folder, state = resolve_task(args["task"])
        new_name = args["name"].strip()
        old_info = t.parse_task_slug(folder.name)
        new_slug = re.sub(r"[^a-z0-9]+", "-", new_name.lower()).strip("-")
        new_slug = f"{old_info['priority']:02d}-{old_info['project']}-{new_slug}"
        new_folder = t.get_tasks_dir() / state / new_slug
        if new_folder.exists():
            raise ValueError(f"Slug '{new_slug}' already exists")
        folder.rename(new_folder)
        if t.get_context() == folder.name:
            t.set_context(new_slug)
        t.append_log(new_folder, "renamed", old=old_info["name"], new=new_name)
        return ok(f"Renamed → {new_slug}")

    # ── reprioritize_task ─────────────────────────────────────────────────────
    if name == "reprioritize_task":
        folder, state = resolve_task(args["task"])
        new_pri  = int(args["priority"])
        old_info = t.parse_task_slug(folder.name)
        new_slug = f"{new_pri:02d}-{old_info['project']}-{old_info['name'].replace(' ', '-')}"
        new_folder = t.get_tasks_dir() / state / new_slug
        if new_folder.exists():
            raise ValueError(f"Slug '{new_slug}' already exists")
        folder.rename(new_folder)
        if t.get_context() == folder.name:
            t.set_context(new_slug)
        t.append_log(new_folder, "reprioritized",
                     old_priority=old_info["priority"], new_priority=new_pri)
        return ok(f"Priority → {new_pri} ({pri_label(new_pri)})  [{new_slug}]")

    # ── reproject_task ────────────────────────────────────────────────────────
    if name == "reproject_task":
        folder, state = resolve_task(args["task"])
        new_proj = args["project"].upper()
        old_info = t.parse_task_slug(folder.name)
        new_slug = f"{old_info['priority']:02d}-{new_proj}-{old_info['name'].replace(' ', '-')}"
        new_folder = t.get_tasks_dir() / state / new_slug
        if new_folder.exists():
            raise ValueError(f"Slug '{new_slug}' already exists")
        folder.rename(new_folder)
        if t.get_context() == folder.name:
            t.set_context(new_slug)
        t.append_log(new_folder, "reprojected",
                     old_project=old_info["project"], new_project=new_proj)
        return ok(f"Project → {new_proj} ({proj_name(new_proj)})  [{new_slug}]")

    # ── set_due ───────────────────────────────────────────────────────────────
    if name == "set_due":
        folder, _ = resolve_task(args["task"])
        date = (args.get("date") or "").strip()
        df = folder / "due.txt"
        if date:
            df.write_text(date)
            t.append_log(folder, "due_set", date=date)
            return ok(f"Due date set to {date}")
        else:
            if df.exists():
                df.unlink()
                t.append_log(folder, "due_cleared")
            return ok("Due date cleared")

    # ── add_note ──────────────────────────────────────────────────────────────
    if name == "add_note":
        folder, _ = resolve_task(args["task"])
        note = args["note"].strip()
        if not note:
            raise ValueError("note text is required")
        wf = folder / "worklog.md"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n### {ts}\n{note}\n"
        with open(wf, "a") as f:
            f.write(entry)
        t.append_log(folder, "note_added")
        return ok(f"Note added to {folder.name}")

    # ── update_description ────────────────────────────────────────────────────
    if name == "update_description":
        folder, _ = resolve_task(args["task"])
        text = args.get("description", "")
        df = folder / "description.md"
        # Archive old version
        if df.exists():
            ad = folder / "description_archive"
            t.ensure_dir(ad)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            df.rename(ad / f"description_{ts}.md")
        df.write_text(text)
        t.append_log(folder, "description_updated")
        return ok(f"Description updated for {folder.name}")

    # ── add_subtask ───────────────────────────────────────────────────────────
    if name == "add_subtask":
        folder, _ = resolve_task(args["task"])
        sub_name = args["name"].strip()
        if not sub_name:
            raise ValueError("subtask name is required")
        priority    = int(args.get("priority") or 50)
        parent_proj = t.parse_task_slug(folder.name)["project"]
        name_slug   = re.sub(r"[^a-z0-9]+", "-", sub_name.lower()).strip("-")
        sub_slug    = f"{priority:02d}-{parent_proj}-{name_slug}"
        sf = folder / "subtasks" / "active" / sub_slug
        t.ensure_dir(sf)
        (sf / "description.md").write_text(f"# {sub_name}\n\n_No description._\n")
        if args.get("due"):
            (sf / "due.txt").write_text(args["due"])
        t.append_log(folder, "subtask_created", subtask=sub_slug)
        return ok(f"Subtask created: {sub_slug}")

    # ── done_subtask ──────────────────────────────────────────────────────────
    if name == "done_subtask":
        folder, _ = resolve_task(args["task"])
        partial   = args["subtask"].lower()
        active_dir = folder / "subtasks" / "active"
        if not active_dir.exists():
            raise ValueError("No active subtasks")
        match = None
        for sub in active_dir.iterdir():
            if sub.is_dir() and partial in sub.name.lower():
                match = sub; break
        if not match:
            raise ValueError(f"No active subtask matching '{partial}'")
        dst_dir = folder / "subtasks" / "completed"
        t.ensure_dir(dst_dir)
        dst = dst_dir / match.name
        if dst.exists():
            n = 2
            while (dst_dir / f"{match.name}-{n}").exists():
                n += 1
            dst = dst_dir / f"{match.name}-{n}"
        match.rename(dst)
        t.append_log(folder, "subtask_completed", subtask=match.name)
        return ok(f"✓ Subtask done: {match.name}")

    # ── search_tasks ──────────────────────────────────────────────────────────
    if name == "search_tasks":
        query = args["query"].lower()
        results = []
        for state in t.STATES:
            sd = t.get_tasks_dir() / state
            if not sd.exists():
                continue
            for folder in sorted(sd.iterdir()):
                if not folder.is_dir():
                    continue
                hits = []
                if query in folder.name.lower():
                    hits.append("name")
                for fname in ("description.md", "worklog.md"):
                    f = folder / fname
                    if f.exists() and query in f.read_text().lower():
                        hits.append(fname)
                if hits:
                    d = task_to_dict(folder, state)
                    results.append(f"[{state}] {fmt_task_line(d)}  (matched: {', '.join(hits)})")
        if not results:
            return ok(f"No tasks match '{query}'")
        return ok(f"Results for '{query}':\n\n" + "\n".join(results))

    # ── upcoming_tasks ────────────────────────────────────────────────────────
    if name == "upcoming_tasks":
        days   = int(args.get("days") or 7)
        cutoff = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        results = []
        sd = t.get_tasks_dir() / "active"
        if sd.exists():
            for folder in sorted(sd.iterdir()):
                if not folder.is_dir():
                    continue
                df = folder / "due.txt"
                if not df.exists():
                    continue
                if df.read_text().strip() <= cutoff:
                    results.append(task_to_dict(folder, "active"))
        results.sort(key=lambda x: x["due"] or "9999")
        if not results:
            return ok(f"No tasks due within {days} days.")
        lines = [fmt_task_line(d) for d in results]
        label = "overdue" if days == 0 else f"due within {days} days"
        return ok(f"Tasks {label}:\n\n" + "\n".join(lines))

    # ── get_status ────────────────────────────────────────────────────────────
    if name == "get_status":
        projects = t.load_projects()
        lines    = [f"Toledo Status — {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
        for state in ["active", "completed", "archive"]:
            sd = t.get_tasks_dir() / state
            if not sd.exists():
                continue
            tasks = [f for f in sorted(sd.iterdir()) if f.is_dir()]
            if not tasks:
                continue
            lines.append(f"── {state.upper()} ({len(tasks)}) ──")
            # Group by project
            by_proj: dict[str, list] = {}
            for f in tasks:
                info = t.parse_task_slug(f.name)
                by_proj.setdefault(info["project"], []).append(f)
            for proj_code, proj_tasks in sorted(by_proj.items()):
                pname = proj_name(proj_code)
                lines.append(f"  {pname} ({proj_code}) — {len(proj_tasks)} task(s)")
                for f in proj_tasks:
                    d   = task_to_dict(f, state)
                    due = f"  due:{('⚠' if d['overdue'] else '')}{d['due']}" if d["due"] else ""
                    lines.append(f"    • {d['name']}{due}")
            lines.append("")
        return ok("\n".join(lines))

    # ── list_projects ─────────────────────────────────────────────────────────
    if name == "list_projects":
        projects = t.load_projects()
        if not projects:
            return ok("No projects defined.")
        lines = [f"{'CODE':<8}  {'NAME':<20}  COLOR"]
        lines.append("-" * 40)
        for code, val in sorted(projects.items()):
            if isinstance(val, dict):
                pname  = val.get("name", "")
                pcolor = val.get("color", "(none)")
            else:
                pname, pcolor = str(val), "(none)"
            lines.append(f"{code:<8}  {pname:<20}  {pcolor}")
        return ok("\n".join(lines))

    # ── add_project ───────────────────────────────────────────────────────────
    if name == "add_project":
        code  = args["code"].upper().strip()
        pname = args["name"].strip()
        color = args.get("color") or ""
        if not code or not pname:
            raise ValueError("code and name are required")
        projects = t.load_projects()
        projects[code] = {"name": pname, "color": color}
        t.save_projects(projects)
        return ok(f"✓ Project '{code}' = '{pname}'" + (f"  {color}" if color else ""))

    # ── remove_project ────────────────────────────────────────────────────────
    if name == "remove_project":
        code = args["code"].upper().strip()
        projects = t.load_projects()
        if code not in projects:
            raise ValueError(f"Project '{code}' not found")
        del projects[code]
        t.save_projects(projects)
        return ok(f"Removed project '{code}'")

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

    raise ValueError(f"Unknown resource: {uri_str}")


# ── Starlette / SSE transport ─────────────────────────────────────────────────

sse_transport = SseServerTransport("/mcp/messages")


async def handle_sse(request):
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await server.run(
            streams[0], streams[1],
            server.create_initialization_options(),
        )


async def handle_messages(request):
    await sse_transport.handle_post_message(
        request.scope, request.receive, request._send
    )


starlette_app = Starlette(
    routes=[
        Route("/mcp/sse",      endpoint=handle_sse),
        Route("/mcp/messages", endpoint=handle_messages, methods=["POST"]),
    ]
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
