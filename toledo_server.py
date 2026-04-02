#!/usr/bin/env python3
"""Toledo web server — shares the same task data as the CLI."""

import importlib.util
import json
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

# ── Load toledo module (no .py extension) ──────────────────────────────────────
import importlib.machinery
_path   = str(Path(__file__).parent / "toledo")
_loader = importlib.machinery.SourceFileLoader("toledo", _path)
_spec   = importlib.util.spec_from_loader("toledo", _loader, origin=_path)
t       = importlib.util.module_from_spec(_spec)
_loader.exec_module(t)

app = Flask(__name__, static_folder="static", static_url_path="/static")


# ── Helpers ───────────────────────────────────────────────────────────────────

def task_to_dict(folder, state, detail=False):
    info = t.parse_task_slug(folder.name)
    df = folder / "due.txt"
    rf = folder / "recurrence.txt"
    result = {
        "slug": folder.name,
        "state": state,
        "priority": info["priority"],
        "project": info["project"],
        "name": info["name"],
        "due": df.read_text().strip() if df.exists() else None,
        "recurrence": int(rf.read_text().strip()) if rf.exists() else None,
        "overdue": t.is_overdue(folder),
        "subtasks": {"active": [], "completed": []},
    }
    for ss in t.SUBTASK_STATES:
        sub_dir = folder / "subtasks" / ss
        if sub_dir.exists():
            for sub in sorted(sub_dir.iterdir()):
                if sub.is_dir():
                    si = t.parse_task_slug(sub.name)
                    result["subtasks"][ss].append({
                        "slug": sub.name,
                        "name": si["name"],
                        "priority": si["priority"],
                    })
    if detail:
        desc_f = folder / "description.md"
        wlog_f = folder / "worklog.md"
        log_f  = folder / "activity.log"
        result["description"] = desc_f.read_text() if desc_f.exists() else ""
        result["worklog"]     = wlog_f.read_text() if wlog_f.exists() else ""
        result["log"] = []
        if log_f.exists():
            for line in log_f.read_text().splitlines():
                try:
                    result["log"].append(json.loads(line))
                except Exception:
                    pass
    return result


def require_json(*fields):
    data = request.json or {}
    missing = [f for f in fields if not data.get(f)]
    if missing:
        return None, jsonify({"error": f"Required: {', '.join(missing)}"}), 400
    return data, None, None


# ── Static / PWA ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json")

@app.route("/sw.js")
def sw():
    resp = send_from_directory("static", "sw.js")
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


# ── Tasks ─────────────────────────────────────────────────────────────────────

@app.route("/api/tasks", methods=["GET"])
def list_tasks():
    project_filter = (request.args.get("project") or "").upper() or None
    state_filter   = request.args.get("state")
    include_all    = request.args.get("all", "false").lower() == "true"

    states = t.STATES if include_all else ["active", "completed"]
    if state_filter in t.STATES:
        states = [state_filter]

    tasks = []
    for state in states:
        sd = t.get_tasks_dir() / state
        if not sd.exists():
            continue
        for folder in sd.iterdir():
            if not folder.is_dir():
                continue
            if project_filter and t.parse_task_slug(folder.name)["project"] != project_filter:
                continue
            tasks.append(task_to_dict(folder, state))

    tasks.sort(key=lambda x: (x["priority"], x["due"] or "9999"))
    return jsonify(tasks)


@app.route("/api/tasks/<slug>", methods=["GET"])
def get_task(slug):
    r = t.find_task(slug)
    if not r:
        return jsonify({"error": "Not found"}), 404
    folder, state = r
    return jsonify(task_to_dict(folder, state, detail=True))


@app.route("/api/tasks", methods=["POST"])
def create_task():
    data, err, code = require_json("name")
    if err:
        return err, code

    t.init_tasks_dir()
    priority = int(data.get("priority") or 50)
    project  = (data.get("project") or "GEN").upper()
    slug     = t.make_task_slug(priority, project, data["name"])
    recur    = data.get("recur")
    state    = "active"
    folder   = t.get_tasks_dir() / state / slug

    if folder.exists():
        return jsonify({"error": f"Task '{slug}' already exists"}), 409

    t.ensure_dir(folder)
    name = data["name"]
    desc = data.get("description") or f"# {name}\n\n_No description provided._\n"
    if not desc.startswith("#"):
        desc = f"# {name}\n\n{desc}\n"
    (folder / "description.md").write_text(desc)
    if data.get("due"):
        (folder / "due.txt").write_text(data["due"])
    if recur:
        (folder / "recurrence.txt").write_text(str(recur))
    t.append_log(folder, "created", state=state, priority=priority, project=project)
    return jsonify(task_to_dict(folder, state)), 201


@app.route("/api/tasks/<slug>/done", methods=["POST"])
def task_done(slug):
    r = t.find_task(slug)
    if not r:
        return jsonify({"error": "Not found"}), 404
    folder, state = r
    rf = folder / "recurrence.txt"
    if rf.exists():
        days    = int(rf.read_text().strip())
        new_due = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        (folder / "due.txt").write_text(new_due)
        t.append_log(folder, "completed_recurring", next_due=new_due)
        return jsonify({"recurring": True, "next_due": new_due})
    nf = t.get_tasks_dir() / "completed" / folder.name
    folder.rename(nf)
    t.append_log(nf, "state_changed", from_state=state, to_state="completed")
    return jsonify({"state": "completed"})


@app.route("/api/tasks/<slug>/cancel", methods=["POST"])
def task_cancel(slug):
    r = t.find_task(slug)
    if not r:
        return jsonify({"error": "Not found"}), 404
    folder, state = r
    if not (folder / "recurrence.txt").exists():
        return jsonify({"error": "Not a recurring task"}), 400
    nf = t.get_tasks_dir() / "completed" / folder.name
    folder.rename(nf)
    t.append_log(nf, "recurring_cancelled")
    return jsonify({"state": "completed"})


@app.route("/api/tasks/<slug>/archive", methods=["POST"])
def task_archive(slug):
    r = t.find_task(slug)
    if not r:
        return jsonify({"error": "Not found"}), 404
    folder, state = r
    if state == "archive":
        return jsonify({"error": "Already archived"}), 400
    nf = t.get_tasks_dir() / "archive" / folder.name
    folder.rename(nf)
    t.append_log(nf, "state_changed", from_state=state, to_state="archive")
    return jsonify({"state": "archive"})


@app.route("/api/tasks/<slug>", methods=["DELETE"])
def task_delete(slug):
    r = t.find_task(slug)
    if not r:
        return jsonify({"error": "Not found"}), 404
    folder, _ = r
    shutil.rmtree(folder)
    return jsonify({"deleted": True})


@app.route("/api/tasks/<slug>/note", methods=["POST"])
def task_note(slug):
    r = t.find_task(slug)
    if not r:
        return jsonify({"error": "Not found"}), 404
    folder, _ = r
    data, err, code = require_json("text")
    if err:
        return err, code
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(folder / "worklog.md", "a") as f:
        f.write(f"\n### {ts}\n\n{data['text']}\n")
    t.append_log(folder, "note_added")
    return jsonify({"ok": True})


@app.route("/api/tasks/<slug>/due", methods=["POST"])
def task_due(slug):
    r = t.find_task(slug)
    if not r:
        return jsonify({"error": "Not found"}), 404
    folder, _ = r
    data, err, code = require_json("date")
    if err:
        return err, code
    (folder / "due.txt").write_text(data["date"])
    t.append_log(folder, "due_date_set", date=data["date"])
    return jsonify({"due": data["date"]})


@app.route("/api/tasks/<slug>/move", methods=["POST"])
def task_move(slug):
    r = t.find_task(slug)
    if not r:
        return jsonify({"error": "Not found"}), 404
    folder, cur = r
    data = request.json or {}
    to_state = data.get("state")
    if to_state not in t.STATES:
        return jsonify({"error": f"state must be one of {t.STATES}"}), 400
    nf = t.get_tasks_dir() / to_state / folder.name
    folder.rename(nf)
    t.append_log(nf, "state_changed", from_state=cur, to_state=to_state)
    return jsonify({"state": to_state})


@app.route("/api/tasks/<slug>/reproject", methods=["POST"])
def task_reproject(slug):
    r = t.find_task(slug)
    if not r:
        return jsonify({"error": "Not found"}), 404
    folder, state = r
    data = request.json or {}
    new_proj = (data.get("project") or "").upper()
    if not new_proj:
        return jsonify({"error": "project required"}), 400
    old_info = t.parse_task_slug(folder.name)
    old_proj = old_info["project"]
    if old_proj == new_proj:
        return jsonify({"error": "Already that project"}), 400
    new_slug   = f"{old_info['priority']:02d}-{new_proj}-{old_info['name'].replace(' ', '-')}"
    new_folder = t.get_tasks_dir() / state / new_slug
    if new_folder.exists():
        return jsonify({"error": f"Slug '{new_slug}' already exists"}), 409
    folder.rename(new_folder)
    if t.get_context() == folder.name:
        t.set_context(new_slug)
    t.append_log(new_folder, "reprojected", old_project=old_proj, new_project=new_proj)
    return jsonify(task_to_dict(new_folder, state))


@app.route("/api/tasks/<slug>/reprioritize", methods=["POST"])
def task_reprioritize(slug):
    r = t.find_task(slug)
    if not r:
        return jsonify({"error": "Not found"}), 404
    folder, state = r
    data = request.json or {}
    new_pri = data.get("priority")
    if new_pri is None:
        return jsonify({"error": "priority required"}), 400
    new_pri  = int(new_pri)
    old_info = t.parse_task_slug(folder.name)
    old_pri  = old_info["priority"]
    if old_pri == new_pri:
        return jsonify({"error": "Already that priority"}), 400
    new_slug   = f"{new_pri:02d}-{old_info['project']}-{old_info['name'].replace(' ', '-')}"
    new_folder = t.get_tasks_dir() / state / new_slug
    if new_folder.exists():
        return jsonify({"error": f"Slug '{new_slug}' already exists"}), 409
    folder.rename(new_folder)
    if t.get_context() == folder.name:
        t.set_context(new_slug)
    t.append_log(new_folder, "reprioritized", old_priority=old_pri, new_priority=new_pri)
    return jsonify(task_to_dict(new_folder, state))


@app.route("/api/tasks/<slug>/edit", methods=["POST"])
def task_edit(slug):
    r = t.find_task(slug)
    if not r:
        return jsonify({"error": "Not found"}), 404
    folder, _ = r
    data, err, code = require_json("text")
    if err:
        return err, code
    df = folder / "description.md"
    if df.exists():
        ad = folder / "description_archive"
        t.ensure_dir(ad)
        df.rename(ad / f"description_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
    df.write_text(data["text"])
    t.append_log(folder, "description_updated")
    return jsonify({"ok": True})


@app.route("/api/tasks/<slug>/sub", methods=["POST"])
def task_sub(slug):
    r = t.find_task(slug)
    if not r:
        return jsonify({"error": "Not found"}), 404
    folder, _ = r
    data, err, code = require_json("name")
    if err:
        return err, code
    p        = int(data.get("priority") or 50)
    sub_slug = f"{p:02d}-{re.sub(r'[^a-z0-9]+', '-', data['name'].lower()).strip('-')}"
    sf       = folder / "subtasks" / "active" / sub_slug
    t.ensure_dir(sf)
    (sf / "description.md").write_text(
        data.get("description") or f"# {data['name']}\n\n_No description._\n"
    )
    if data.get("due"):
        (sf / "due.txt").write_text(data["due"])
    t.append_log(folder, "subtask_created", subtask=sub_slug)
    return jsonify({"slug": sub_slug}), 201


@app.route("/api/tasks/<slug>/subdone", methods=["POST"])
def task_subdone(slug):
    r = t.find_task(slug)
    if not r:
        return jsonify({"error": "Not found"}), 404
    folder, _ = r
    data = request.json or {}
    sub_slug = data.get("slug")
    if not sub_slug:
        return jsonify({"error": "slug required"}), 400
    src = folder / "subtasks" / "active" / sub_slug
    if not src.exists():
        return jsonify({"error": "Subtask not found"}), 404
    dst = folder / "subtasks" / "completed"
    t.ensure_dir(dst)
    src.rename(dst / src.name)
    t.append_log(folder, "subtask_completed", subtask=sub_slug)
    return jsonify({"ok": True})


# ── Upcoming & Search ─────────────────────────────────────────────────────────

@app.route("/api/upcoming", methods=["GET"])
def upcoming():
    days   = int(request.args.get("days", 7))
    today  = datetime.now().date()
    cutoff = (today + timedelta(days=days)).strftime("%Y-%m-%d")
    tasks  = []
    for state in ["active"]:
        sd = t.get_tasks_dir() / state
        if not sd.exists():
            continue
        for folder in sd.iterdir():
            if not folder.is_dir():
                continue
            df = folder / "due.txt"
            if df.exists() and df.read_text().strip() <= cutoff:
                tasks.append(task_to_dict(folder, state))
    tasks.sort(key=lambda x: (x["due"] or "9999", x["priority"]))
    return jsonify(tasks)


@app.route("/api/search", methods=["GET"])
def search():
    query = (request.args.get("q") or "").lower()
    if not query:
        return jsonify([])
    results = []
    for state in t.STATES:
        sd = t.get_tasks_dir() / state
        if not sd.exists():
            continue
        for folder in sd.iterdir():
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
                d["hits"] = hits
                results.append(d)
    return jsonify(results)


# ── Projects ──────────────────────────────────────────────────────────────────

@app.route("/api/projects", methods=["GET"])
def list_projects():
    return jsonify(t.load_projects())


@app.route("/api/projects", methods=["POST"])
def add_project():
    data, err, code = require_json("code", "name")
    if err:
        return err, code
    projects = t.load_projects()
    projects[data["code"].upper()] = data["name"]
    t.save_projects(projects)
    return jsonify(projects)


@app.route("/api/projects/<code>", methods=["DELETE"])
def remove_project(code):
    projects = t.load_projects()
    code = code.upper()
    if code not in projects:
        return jsonify({"error": "Not found"}), 404
    del projects[code]
    t.save_projects(projects)
    return jsonify({"deleted": True})


# ── Context ───────────────────────────────────────────────────────────────────

@app.route("/api/ctx", methods=["GET"])
def get_ctx():
    return jsonify({"context": t.get_context()})


@app.route("/api/ctx", methods=["POST"])
def set_ctx():
    data = request.json or {}
    slug = data.get("slug")
    if not slug:
        return jsonify({"error": "slug required"}), 400
    r = t.find_task(slug)
    if not r:
        return jsonify({"error": "Not found"}), 404
    folder, _ = r
    t.set_context(folder.name)
    return jsonify({"context": folder.name})


@app.route("/api/ctx", methods=["DELETE"])
def clear_ctx():
    t.clear_context()
    return jsonify({"context": None})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Toledo web server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    print(f"Toledo server running on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
