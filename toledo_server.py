#!/usr/bin/env python3
"""Toledo web server — serves the PWA and its JSON API over the SQLite store."""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, make_response, request, send_from_directory

import brief_collect
import brief_gcal
import brief_scheduler
import brief_state
import toledo_db as db

app = Flask(__name__, static_folder="static", static_url_path="/static")
db.set_default_source("web")
STARTED = datetime.now().isoformat(timespec="seconds")

# ── Morning brief ─────────────────────────────────────────────────────────────
# Vendored from the standalone morning-brief project (github.com/krets/morning). State lives
# alongside Toledo's own database in the shared ~/.toledo volume. Only the real server process
# (the `if __name__ == "__main__":` block below) starts the Scheduler thread — toledo_mcp.py
# shares this same data but only reads it, so the two containers don't both run it daily.
BRIEF_DB_PATH = str(db.TOLEDO_HOME / "brief.db")
BRIEF_DATA_DIR = db.TOLEDO_HOME / "brief"
BRIEF_DATA_DIR.mkdir(parents=True, exist_ok=True)
BRIEF_TZ_NAME = os.environ.get("TIMEZONE", "Europe/Berlin")
BRIEF_TZ = ZoneInfo(BRIEF_TZ_NAME)
BRIEF_EVENT_DAYS = int(os.environ.get("EVENT_DAYS", "90"))
BRIEF_SCHEDULE_SPEC = os.environ.get("SCHEDULE_WINDOW", "02:00-06:00")

_brief_conn = brief_state.connect(BRIEF_DB_PATH)
brief_state.mark_interrupted(_brief_conn)
brief_state.seed(_brief_conn, brief_collect.DEFAULT_KEYWORDS, brief_gcal.HOLIDAYS)
_brief_conn.close()

brief_runner = brief_scheduler.Runner(str(BRIEF_DATA_DIR), BRIEF_DB_PATH, BRIEF_TZ_NAME, BRIEF_EVENT_DAYS,
                                      llm_resolver=lambda: resolve_llm(db.load_config().get("llm", {})))
brief_schedule = brief_scheduler.Schedule(BRIEF_SCHEDULE_SPEC, BRIEF_TZ)

RELEASE = db.release_version()
COMMIT = RELEASE["commit"]
BUILT = RELEASE["built"]
# Changes whenever the web UI does. The page carries the value it was served
# with and compares it with /api/version to notice it is a stale copy.
UI_BUILD = hashlib.sha1(b"".join(
    (Path(app.static_folder) / f).read_bytes() for f in ("index.html", "sw.js")
)).hexdigest()[:10]


def _stamp(text: str) -> str:
    return (text.replace("__TOLEDO_COMMIT__", COMMIT)
                .replace("__TOLEDO_BUILD_TIME__", BUILT)
                .replace("__TOLEDO_UI_BUILD__", UI_BUILD))


# ── Helpers ───────────────────────────────────────────────────────────────────

def task_to_dict(d: dict) -> dict:
    """Store task dict → the shape the PWA expects."""
    parent = d.get("parent")
    result = {
        "id":         d["id"],
        "slug":       d["slug"],
        # What the PWA puts in API paths. A subtask goes by '#id': Flask
        # decodes %2F before routing, so 'parent/child' can't be a path part.
        "ref":        f"#{d['id']}" if parent else d["slug"],
        "parent":     parent,
        "state":      d["state"],
        "priority":   d["priority"],
        "project":    d["project"],
        "tags":       d.get("tags", []),
        "name":       d["name"],
        "due":        d["due"],
        "recurrence": d["recurrence"],
        "overdue":    d["overdue"],
        "updated":    d["updated"],
        "subtasks":   {"active": [], "completed": []},
    }
    for s in d.get("subtasks", []):
        result["subtasks"][s["state"]].append({
            "id": s["id"], "slug": s["slug"], "name": s["name"], "priority": s["priority"],
            "due": s["due"],
        })
    if "description" in d:
        result["description"] = d["description"]
        result["notes"]       = d["notes"]
        result["worklog"]     = db.render_worklog(d["notes"])
        result["log"]         = d["activity"]
    return result


def require_json(*fields):
    data = request.json or {}
    missing = [f for f in fields if not data.get(f)]
    if missing:
        return None, jsonify({"error": f"Required: {', '.join(missing)}"}), 400
    return data, None, None


@app.errorhandler(db.ToledoError)
def toledo_error(e):
    return jsonify({"error": str(e)}), e.status


# ── Static / PWA ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    resp = make_response(_stamp((Path(app.static_folder) / "index.html").read_text()))
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/api/version")
def version():
    return jsonify({"commit": COMMIT, "built": BUILT, "ui": UI_BUILD, "started": STARTED})

@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json")

@app.route("/sw.js")
def sw():
    sw_path = Path(app.static_folder) / "sw.js"
    if not sw_path.exists():
        return "Not found", 404
    # The stamped build gives each UI release its own cache name.
    resp = make_response(_stamp(sw_path.read_text()))
    resp.headers["Content-Type"] = "application/javascript"
    resp.headers["Service-Worker-Allowed"] = "/"
    # Ensure browsers don't cache sw.js itself too long
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp


# ── Tasks ─────────────────────────────────────────────────────────────────────

@app.route("/api/tasks", methods=["GET"])
def list_tasks():
    project_filter = (request.args.get("project") or "").upper() or None
    state_filter   = request.args.get("state")
    include_all    = request.args.get("all", "false").lower() == "true"

    states = db.STATES if include_all else ["active", "completed"]
    if state_filter in db.STATES:
        states = [state_filter]

    tags = request.args.get("tag")
    match_all = request.args.get("match") == "all"
    tasks = [task_to_dict(d) for d in db.list_tasks(states, project_filter, tags, match_all)]
    tasks.sort(key=lambda x: (-x["priority"], x["due"] or "9999"))
    return jsonify(tasks)


@app.route("/api/tasks/<slug>", methods=["GET"])
def get_task(slug):
    return jsonify(task_to_dict(db.get_task(slug)))


@app.route("/api/tasks", methods=["POST"])
def create_task():
    data, err, code = require_json("name")
    if err:
        return err, code
    d = db.create_task(
        data["name"],
        project=data.get("project"),
        priority=data.get("priority"),
        due=data.get("due"),
        recurrence=data.get("recur"),
        description=data.get("description"),
        tags=data.get("tags"),
    )
    return jsonify(task_to_dict(d)), 201


@app.route("/api/tasks/<slug>/done", methods=["POST"])
def task_done(slug):
    r = db.complete_task(slug)
    if r["recurring"]:
        return jsonify({"recurring": True, "next_due": r["next_due"]})
    return jsonify({"state": "completed"})


@app.route("/api/tasks/<slug>/cancel", methods=["POST"])
def task_cancel(slug):
    db.cancel_recurrence(slug)
    return jsonify({"state": "completed"})


@app.route("/api/tasks/<slug>/archive", methods=["POST"])
def task_archive(slug):
    if db.resolve_task(slug)["state"] == "archive":
        return jsonify({"error": "Already archived"}), 400
    db.move_task(slug, "archive")
    return jsonify({"state": "archive"})


@app.route("/api/tasks/<slug>", methods=["DELETE"])
def task_delete(slug):
    db.delete_task(slug)
    return jsonify({"deleted": True})


@app.route("/api/tasks/<slug>/note", methods=["POST"])
def task_note(slug):
    data, err, code = require_json("text")
    if err:
        return err, code
    db.add_note(slug, data["text"])
    return jsonify({"ok": True})


@app.route("/api/tasks/<slug>/notes/<int:note_id>", methods=["PATCH"])
def task_note_update(slug, note_id):
    data, err, code = require_json("text")
    if err:
        return err, code
    db.update_note(slug, note_id, data["text"])
    return jsonify({"ok": True})


@app.route("/api/tasks/<slug>/notes/<int:note_id>", methods=["DELETE"])
def task_note_delete(slug, note_id):
    db.delete_note(slug, note_id)
    return jsonify({"deleted": True})


@app.route("/api/tasks/<slug>/due", methods=["POST"])
def task_due(slug):
    data = request.json or {}
    d = db.set_due(slug, data.get("date"))
    return jsonify({"due": d["due"]})


@app.route("/api/tasks/<slug>/move", methods=["POST"])
def task_move(slug):
    data = request.json or {}
    d = db.move_task(slug, data.get("state"))
    return jsonify({"state": d["state"]})


@app.route("/api/tasks/<slug>/reproject", methods=["POST"])
def task_reproject(slug):
    data = request.json or {}
    return jsonify(task_to_dict(db.set_project(slug, data.get("project"))))


@app.route("/api/tasks/<slug>/tags", methods=["POST"])
def task_tag(slug):
    return jsonify(task_to_dict(db.tag_task(slug, (request.json or {}).get("tags"))))


@app.route("/api/tasks/<slug>/tags", methods=["DELETE"])
def task_untag(slug):
    return jsonify(task_to_dict(db.untag_task(slug, (request.json or {}).get("tags"))))


@app.route("/api/tasks/<slug>/reprioritize", methods=["POST"])
def task_reprioritize(slug):
    data = request.json or {}
    if data.get("priority") is None:
        return jsonify({"error": "priority required"}), 400
    return jsonify(task_to_dict(db.set_priority(slug, data["priority"])))


@app.route("/api/tasks/<slug>/edit", methods=["POST"])
def task_edit(slug):
    data, err, code = require_json("text")
    if err:
        return err, code
    db.set_description(slug, data["text"])
    return jsonify({"ok": True})


@app.route("/api/tasks/<slug>/sub", methods=["POST"])
def task_sub(slug):
    data, err, code = require_json("name")
    if err:
        return err, code
    s = db.add_subtask(slug, data["name"], priority=data.get("priority"),
                       due=data.get("due"), description=data.get("description"))
    return jsonify({"slug": s["slug"]}), 201


@app.route("/api/tasks/<slug>/subdone", methods=["POST"])
def task_subdone(slug):
    sub_slug = (request.json or {}).get("slug")
    if not sub_slug:
        return jsonify({"error": "slug required"}), 400
    db.set_subtask_state(slug, sub_slug, "completed")
    return jsonify({"ok": True})


@app.route("/api/tasks/<slug>/subundo", methods=["POST"])
def task_subundo(slug):
    sub_slug = (request.json or {}).get("slug")
    if not sub_slug:
        return jsonify({"error": "slug required"}), 400
    db.set_subtask_state(slug, sub_slug, "active")
    return jsonify({"ok": True})


@app.route("/api/tasks/<slug>/sub/<sub_slug>", methods=["DELETE"])
def task_subdelete(slug, sub_slug):
    db.delete_subtask(slug, sub_slug)
    return jsonify({"deleted": True})


@app.route("/api/tasks/<slug>/rename", methods=["POST"])
def task_rename(slug):
    data, err, code = require_json("name")
    if err:
        return err, code
    return jsonify(task_to_dict(db.rename_task(slug, data["name"])))


@app.route("/api/tasks/<slug>/subrename", methods=["POST"])
def task_subrename(slug):
    data = request.json or {}
    if not data.get("slug") or not (data.get("name") or "").strip():
        return jsonify({"error": "slug and name required"}), 400
    s = db.rename_subtask(slug, data["slug"], data["name"])
    return jsonify({"slug": s["slug"]})


# ── Upcoming & Search ─────────────────────────────────────────────────────────

@app.route("/api/upcoming", methods=["GET"])
def upcoming():
    days = int(request.args.get("days", 7))
    return jsonify([task_to_dict(d) for d in db.upcoming(days)])


@app.route("/api/search", methods=["GET"])
def search():
    results = []
    for d in db.search(request.args.get("q") or ""):
        r = task_to_dict(d)
        r["hits"] = d["hits"]
        results.append(r)
    return jsonify(results)


# ── Projects ──────────────────────────────────────────────────────────────────

@app.route("/api/projects", methods=["GET"])
def list_projects():
    return jsonify(db.list_projects())


@app.route("/api/projects", methods=["POST"])
def add_project():
    data, err, code = require_json("code", "name")
    if err:
        return err, code
    db.save_project(data["code"], data["name"], data.get("color") or "#6b9ce8")
    return jsonify(db.list_projects())


@app.route("/api/projects/<code>", methods=["PATCH"])
def update_project(code):
    data = request.json or {}
    db.update_project(code, name=data.get("name"), color=data.get("color"))
    return jsonify(db.list_projects())


@app.route("/api/projects/<code>", methods=["DELETE"])
def remove_project(code):
    db.remove_project(code)
    return jsonify({"deleted": True})


# ── Tags ──────────────────────────────────────────────────────────────────────

@app.route("/api/tags", methods=["GET"])
def list_tags():
    return jsonify(db.list_tags())


@app.route("/api/tags/<tag>", methods=["PATCH"])
def rename_tag(tag):
    data, err, code = require_json("name")
    if err:
        return err, code
    db.rename_tag(tag, data["name"])
    return jsonify(db.list_tags())


# ── Journal ───────────────────────────────────────────────────────────────────

@app.route("/api/journal", methods=["GET"])
def list_journal():
    """Entries newest first, without full text. ?q= searches, ?since=/&until=
    bound the date, ?limit=/&offset= page through."""
    a = request.args
    return jsonify(db.list_journal(limit=int(a.get("limit", 50)), offset=int(a.get("offset", 0)),
                                   query=a.get("q"), since=a.get("since"), until=a.get("until")))


@app.route("/api/journal/<int:entry_id>", methods=["GET"])
def get_journal(entry_id):
    return jsonify(db.get_journal(entry_id)[0])


@app.route("/api/journal", methods=["POST"])
def add_journal():
    data = request.json or {}
    j = db.add_journal(data.get("raw"), summary=data.get("summary"), title=data.get("title"),
                       date=data.get("date"), source="web")
    return jsonify(j), 201


@app.route("/api/journal/<int:entry_id>", methods=["PATCH"])
def update_journal(entry_id):
    data = request.json or {}
    return jsonify(db.update_journal(entry_id, raw=data.get("raw"), summary=data.get("summary"),
                                     title=data.get("title"), date=data.get("date")))


@app.route("/api/journal/<int:entry_id>", methods=["DELETE"])
def delete_journal(entry_id):
    db.delete_journal(entry_id)
    return jsonify({"deleted": True})


# ── Activity ──────────────────────────────────────────────────────────────────

@app.route("/api/activity", methods=["GET"])
def list_activity():
    """The event log, newest first. ?scope=, ?action= take comma-separated
    values; ?task= includes subtasks; ?project=; ?since=/&until= take a date
    or timestamp; ?limit=/&offset= page through."""
    a = request.args
    return jsonify(db.list_activity(
        limit=int(a.get("limit", 100)), offset=int(a.get("offset", 0)),
        since=a.get("since"), until=a.get("until"), scope=a.get("scope"),
        task=a.get("task"), action=a.get("action"), project=a.get("project"),
    ))


# ── Context ───────────────────────────────────────────────────────────────────

@app.route("/api/ctx", methods=["GET"])
def get_ctx():
    return jsonify({"context": db.get_context()})


@app.route("/api/ctx", methods=["POST"])
def set_ctx():
    slug = (request.json or {}).get("slug")
    if not slug:
        return jsonify({"error": "slug required"}), 400
    return jsonify({"context": db.set_context(slug)})


@app.route("/api/ctx", methods=["DELETE"])
def clear_ctx():
    db.set_context(None)
    return jsonify({"context": None})


@app.route("/api/status", methods=["GET"])
def get_status():
    projects = db.list_projects()
    tasks = db.list_tasks(["active"])

    # Format Projects as Markdown
    projects_md = "\n".join(f"- **{code}**: {p['name']}" for code, p in projects.items())

    # Format Tasks as Markdown
    tasks_md = ""
    if not tasks:
        tasks_md = "_No active tasks._\n"
    else:
        for tk in tasks:
            due_str = f", Due: {tk['due']}" if tk.get('due') else ""
            recur_str = f", Recur: {tk['recurrence']}d" if tk.get('recurrence') else ""
            tags_str = f", Tags: {' '.join('#' + t for t in tk['tags'])}" if tk.get('tags') else ""
            tasks_md += f"- **{tk['name']}** (`{tk['slug']}`)\n  - Project: {tk['project']}, Priority: {tk['priority']}{due_str}{recur_str}{tags_str}\n"

    status_text = f"""# TOLEDO SYSTEM STATUS
**Current Time:** {datetime.now().strftime('%Y-%m-%d %H:%M')}

## PROJECT CONTEXT
{projects_md}

## ACTIVE TASKS
{tasks_md}

## PRIORITY SYSTEM
Priority is a number from 1 to 99; higher is more important:
- **76-99**: Ultra High
- **75**: High
- **50**: Medium
- **25**: Low
- **1-24**: Very Low

## AVAILABLE OPERATIONS
You can ask the Toledo AI to perform the following actions. When responding, provide a concise list of these operations that the user can paste into the Toledo chat bubble:

- **Create Task**: `create_task(name, project="GEN", priority=50, due="YYYY-MM-DD", recur=0)`
- **Complete Task**: `done_task(task_slug_or_name)`
- **Add Note**: `add_note(task, text)`
- **Set Due Date**: `set_due(task, due="YYYY-MM-DD")`
- **Change Priority**: `reprioritize(task, priority)`
- **Change Project**: `reproject(task, project_code)`
- **Tag Task**: `tag_task(task, tags)`
- **Untag Task**: `untag_task(task, tags)`
- **Edit Description**: `edit_desc(task, text)`
- **Rename Task**: `rename_task(task, new_name)`
- **Add Subtask**: `add_subtask(parent_task, name, priority=50, due=None)`
- **Complete Subtask**: `done_subtask(parent_task, subtask_slug)`
- **Undo Subtask**: `undo_subtask(parent_task, subtask_slug)`
- **Rename Subtask**: `rename_subtask(parent_task, subtask_slug, new_name)`
- **Delete Subtask**: `delete_subtask(parent_task, subtask_slug)`
- **Move Task State**: `move_task(task, state="active|completed|archive")`
- **Cancel Recurrence**: `cancel_recurrence(task)`
- **Archive Task**: `archive_task(task)`
- **Delete Task**: `delete_task(task)`

**Instruction for Assistant:**
Please take these details and remember them, just acknowldge that you are ready to start. At some point I will ask for a plan update. Only then, provide the final set of instructions as a clear list of actions (e.g., "Create a task for...", "Mark 'x' as done", etc.) so that I can copy them into my other todo system.
"""
    return jsonify({"status": status_text})


# ── LLM settings ──────────────────────────────────────────────────────────────
#
# config.json "llm" block:
#   provider   key of LLM_PROVIDERS (absent in legacy configs → model used verbatim)
#   model      model name without the litellm prefix, e.g. "gpt-4o-mini"
#   base_url   optional endpoint override
#   api_keys   {provider: key}, so switching providers doesn't lose keys
#   api_key    legacy single key, folded into api_keys on the next save

LLM_PROVIDERS = {
    "openai":     {"label": "OpenAI",                     "prefix": "openai",      "needs_key": True},
    "anthropic":  {"label": "Anthropic",                  "prefix": "anthropic",   "needs_key": True},
    "gemini":     {"label": "Google Gemini",              "prefix": "gemini",      "needs_key": True},
    "openrouter": {"label": "OpenRouter",                 "prefix": "openrouter",  "needs_key": True},
    "groq":       {"label": "Groq",                       "prefix": "groq",        "needs_key": True},
    "mistral":    {"label": "Mistral",                    "prefix": "mistral",     "needs_key": True},
    "deepseek":   {"label": "DeepSeek",                   "prefix": "deepseek",    "needs_key": True},
    "xai":        {"label": "xAI (Grok)",                 "prefix": "xai",         "needs_key": True},
    "ollama":     {"label": "Ollama",                     "prefix": "ollama_chat", "needs_key": False,
                   "base_url": "http://localhost:11434"},
    "lm_studio":  {"label": "LM Studio",                  "prefix": "lm_studio",   "needs_key": False,
                   "base_url": "http://localhost:1234/v1"},
    "custom":     {"label": "Custom (OpenAI-compatible)", "prefix": "openai",      "needs_key": False,
                   "base_url": ""},
}


def infer_provider(llm: dict) -> str:
    """Best guess at the provider for a legacy config that only has a model string."""
    if llm.get("provider") in LLM_PROVIDERS:
        return llm["provider"]
    model = llm.get("model") or ""
    head = model.split("/", 1)[0] if "/" in model else None
    for name, p in LLM_PROVIDERS.items():
        if head in (name, p["prefix"]):
            return name
    if "claude" in model:
        return "anthropic"
    if "gemini" in model:
        return "gemini"
    if llm.get("base_url"):
        return "custom"
    return "openai"


def strip_prefix(provider: str, model: str) -> str:
    for p in {provider, LLM_PROVIDERS[provider]["prefix"]}:
        if model.startswith(p + "/"):
            return model[len(p) + 1:]
    return model


def provider_key(llm: dict, provider: str):
    keys = llm.get("api_keys") or {}
    if provider in keys:
        return keys[provider]
    # Legacy single key belongs to whatever provider the old config implied
    if llm.get("api_key") and infer_provider(llm) == provider:
        return llm["api_key"]
    return None


def resolve_llm(llm: dict, provider=None, model=None, base_url=None, api_key=None):
    """(litellm model string, api_key, api_base) from config, with optional overrides."""
    if provider is None and "provider" not in llm:
        # Legacy config: pass the model string straight to litellm
        return (model or llm.get("model") or "gpt-4o-mini",
                api_key or llm.get("api_key"), base_url or llm.get("base_url"))
    provider = provider or llm["provider"]
    p = LLM_PROVIDERS[provider]
    model = strip_prefix(provider, model or llm.get("model") or "")
    if base_url is None:
        base_url = llm.get("base_url")
    return (f"{p['prefix']}/{model}",
            api_key or provider_key(llm, provider),
            base_url or p.get("base_url") or None)


def settings_payload(config: dict) -> dict:
    llm = config.get("llm", {})
    provider = infer_provider(llm)
    keys = {name: provider_key(llm, name) for name in LLM_PROVIDERS}
    return {
        "providers": [
            {"id": name, "label": p["label"], "needs_key": p["needs_key"],
             "default_base_url": p.get("base_url"),
             # Never send keys back to the browser, just enough to recognise them
             "key_hint": ("…" + keys[name][-4:]) if keys[name] else None}
            for name, p in LLM_PROVIDERS.items()
        ],
        "llm": {
            "provider": provider,
            "model":    strip_prefix(provider, llm.get("model") or ""),
            "base_url": llm.get("base_url") or "",
        },
        "chat": {"token_limit": config.get("chat", {}).get("token_limit", 2000)},
    }


def settings_request():
    """Validated provider/model/base_url/api_key from a settings form body."""
    data = request.json or {}
    provider = data.get("provider")
    if provider not in LLM_PROVIDERS:
        raise db.ToledoError(f"Unknown provider: {provider}")
    base_url = (data.get("base_url") or "").strip()
    return data, provider, (data.get("model") or "").strip(), base_url, (data.get("api_key") or "").strip()


@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify(settings_payload(db.load_config()))


@app.route("/api/settings", methods=["PUT"])
def put_settings():
    data, provider, model, base_url, api_key = settings_request()
    if not model:
        return jsonify({"error": "Model is required"}), 400

    config = db.load_config()
    llm = config.setdefault("llm", {})
    keys = llm.setdefault("api_keys", {})
    if llm.get("api_key"):
        keys.setdefault(infer_provider(llm), llm["api_key"])
        del llm["api_key"]

    if data.get("clear_api_key"):
        keys.pop(provider, None)
    elif api_key:
        keys[provider] = api_key

    llm["provider"] = provider
    llm["model"] = strip_prefix(provider, model)
    if base_url:
        llm["base_url"] = base_url
    else:
        llm.pop("base_url", None)

    token_limit = data.get("token_limit")
    if token_limit is not None:
        try:
            config.setdefault("chat", {})["token_limit"] = max(500, int(token_limit))
        except (TypeError, ValueError):
            return jsonify({"error": "Token limit must be a number"}), 400

    db.save_config(config)
    return jsonify(settings_payload(config))


@app.route("/api/settings/models", methods=["POST"])
def list_provider_models():
    """Ask the provider which models it has, using the form's (unsaved) values."""
    import litellm

    _, provider, _, base_url, api_key = settings_request()
    llm = db.load_config().get("llm", {})
    api_key = api_key or provider_key(llm, provider)
    api_base = base_url or LLM_PROVIDERS[provider].get("base_url") or None
    try:
        if provider == "ollama":
            # litellm falls back to a canned list when Ollama is unreachable, so ask it directly
            from urllib.request import urlopen
            with urlopen(api_base.rstrip("/") + "/api/tags", timeout=10) as resp:
                models = [m["name"] for m in json.load(resp).get("models", [])]
        else:
            models = litellm.get_valid_models(check_provider_endpoint=True,
                                              custom_llm_provider=LLM_PROVIDERS[provider]["prefix"],
                                              api_key=api_key, api_base=api_base)
    except Exception as e:
        return jsonify({"error": f"Couldn't list models: {e}"}), 502
    if not models:
        # litellm logs the failure and returns [] for a bad key or unreachable endpoint
        return jsonify({"error": "No models returned. Check the API key and base URL."}), 502
    return jsonify({"models": sorted({strip_prefix(provider, m) for m in models})})


@app.route("/api/settings/test", methods=["POST"])
def test_settings():
    """One tiny completion with the form's (unsaved) values."""
    from litellm import completion

    _, provider, model, base_url, api_key = settings_request()
    if not model:
        return jsonify({"error": "Model is required"}), 400
    llm = db.load_config().get("llm", {})
    model, api_key, api_base = resolve_llm(llm, provider, model, base_url, api_key or None)
    try:
        resp = completion(model=model, api_key=api_key, api_base=api_base, timeout=30,
                          messages=[{"role": "user", "content": "Reply with just the word OK."}])
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": True, "model": model, "reply": (resp.choices[0].message.content or "").strip()})


# ── Chat / LLM ────────────────────────────────────────────────────────────────

CHATTABLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Create a new task",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "project": {"type": "string", "description": "Project code, default GEN"},
                    "priority": {"type": ["integer", "string"], "description": "1-99, higher = more important, or a label like High; default 50"},
                    "due": {"type": "string", "description": "YYYY-MM-DD"},
                    "recur": {"type": "integer", "description": "Days for recurrence"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Free-form tags"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "done_task",
            "description": "Mark a task as completed (or advance if recurring)",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Partial name or slug"}
                },
                "required": ["task"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_note",
            "description": "Add a note to a task's worklog",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "text": {"type": "string"}
                },
                "required": ["task", "text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_due",
            "description": "Set or update a task's due date",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "due": {"type": "string", "description": "YYYY-MM-DD"}
                },
                "required": ["task", "due"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_description",
            "description": "Update the Markdown description of a task",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "text": {"type": "string", "description": "Full new Markdown content"}
                },
                "required": ["task", "text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_subtask",
            "description": "Add a subtask to an existing task",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Parent task name/slug"},
                    "name": {"type": "string", "description": "Subtask name"},
                    "priority": {"type": ["integer", "string"], "default": 50, "description": "1-99, higher = more important, or a label like High"},
                    "due": {"type": "string", "description": "YYYY-MM-DD"}
                },
                "required": ["task", "name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "done_subtask",
            "description": "Mark a subtask as completed",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Parent task name/slug"},
                    "subtask": {"type": "string", "description": "Subtask name or slug"}
                },
                "required": ["task", "subtask"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_subtask",
            "description": "Permanently delete a subtask (active or completed). Cannot be undone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Parent task name/slug"},
                    "subtask": {"type": "string", "description": "Subtask name or slug"}
                },
                "required": ["task", "subtask"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "tag_task",
            "description": "Add free-form tags to a task (subtasks share their parent's tags)",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["task", "tags"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "untag_task",
            "description": "Remove tags from a task",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["task", "tags"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "move_task",
            "description": "Move a task to a different state (active, completed, archive)",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "state": {"type": "string", "enum": ["active", "completed", "archive"]}
                },
                "required": ["task", "state"]
            }
        }
    }
]

def execute_chat_tool(name, args):
    """Executes a tool call and returns a string result."""
    src = "chat"
    try:
        if name == "create_task":
            d = db.create_task(args["name"], project=args.get("project"),
                               priority=args.get("priority"), due=args.get("due"),
                               recurrence=args.get("recur"),
                               description=f"# {args['name']}\n\n_Created via Chat._\n",
                               tags=args.get("tags"), source=src)
            return f"Success: Created task {d['slug']}"

        if name == "done_task":
            r = db.complete_task(args["task"], source=src)
            if r["recurring"]:
                return f"Success: Recurring task {r['task']['slug']} advanced to {r['next_due']}"
            return f"Success: Completed {r['task']['slug']}"

        if name == "add_note":
            d = db.add_note(args["task"], args["text"], source=src)
            return f"Success: Added note to {d['slug']}"

        if name == "set_due":
            due = args.get("due") or args.get("date")  # 'date' kept as a legacy alias
            if not due: return "Error: due is required (YYYY-MM-DD)"
            d = db.set_due(args["task"], due, source=src)
            return f"Success: Set due date for {d['slug']} to {due}"

        if name == "update_description":
            d = db.set_description(args["task"], args["text"], source=src)
            return f"Success: Updated description for {d['slug']}"

        if name == "add_subtask":
            s = db.add_subtask(args["task"], args["name"], priority=args.get("priority"),
                               due=args.get("due"), source=src)
            return f"Success: Added subtask {s['slug']} to {s['parent']['slug']}"

        if name == "done_subtask":
            s = db.set_subtask_state(args["task"], args["subtask"], "completed",
                                     source=src, first_match=True)
            return f"Success: Subtask {s['slug']} marked complete"

        if name == "delete_subtask":
            s = db.delete_subtask(args["task"], args["subtask"], source=src)
            return f"Success: Deleted subtask {s['slug']} ({s['state']})"

        if name in ("tag_task", "untag_task"):
            write = db.tag_task if name == "tag_task" else db.untag_task
            d = write(args["task"], args.get("tags"), source=src)
            return f"Success: {d['slug']} now has tags {', '.join(d['tags']) or '(none)'}"

        if name == "move_task":
            d = db.move_task(args["task"], args["state"], source=src)
            return f"Success: Moved {d['slug']} to {d['state']}"

    except Exception as e:
        return f"Error executing {name}: {e}"
    return f"Unknown tool: {name}"

def estimate_tokens(messages):
    """Rough heuristic for token count."""
    return sum(len(m.get("content") or "") for m in messages) // 4

def compact_history_via_llm(model, messages, api_key, base_url):
    """Uses the LLM to summarize older history to save context space."""
    from litellm import completion

    # Keep the last 4 messages (2 rounds) untouched
    to_summarize = messages[:-4]
    keep = messages[-4:]

    if len(to_summarize) < 4: return messages

    prompt = (
        "Summarize the following conversation history into a single concise paragraph. "
        "Focus on key facts, user preferences, and task changes. "
        "Deduplicate information and be extremely brief.\n\n"
        + json.dumps(to_summarize)
    )

    try:
        # Use a system-like call for summary
        resp = completion(
            model=model,
            messages=[{"role": "system", "content": prompt}],
            api_key=api_key,
            api_base=base_url
        )
        summary = resp.choices[0].message.content
        return [
            {"role": "system", "content": f"Previous conversation summary: {summary}"}
        ] + keep
    except Exception as e:
        print(f"Compaction failed: {e}")
        return messages

@app.route("/api/chat", methods=["POST"])
def chat():
    from litellm import completion

    data, err, code = require_json("messages")
    if err:
        return err, code

    config = db.load_config()
    model, api_key, base_url = resolve_llm(config.get("llm", {}))

    chat_config = config.get("chat", {})
    # Default to 2000 estimated tokens before compaction
    token_limit = chat_config.get("token_limit", 2000)

    user_msgs = data["messages"]

    # Auto-reduce: Summarize if token estimate is high
    if estimate_tokens(user_msgs) > token_limit:
        user_msgs = compact_history_via_llm(model, user_msgs, api_key, base_url)

    # Build context
    projects = db.list_projects()
    tasks = [task_to_dict(d) for d in db.list_tasks()]

    system_prompt = f"""You are Toledo AI, a task management assistant.
Current Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}

PROJECT CONTEXT:
The following project codes and their display names are available:
{json.dumps(projects, indent=2)}

TASK CONTEXT (current state):
{json.dumps(tasks, indent=2)}

INSTRUCTIONS:
1. Help the user manage their tasks using the provided tools.
2. PRIORITY SYSTEM: priority is 1-99, and higher numbers are more important.
   - 76-99: Ultra High
   - 75: High
   - 50: Medium
   - 25: Low
   - 1-24: Very Low
3. When creating a task, use the PROJECT CONTEXT to find the most appropriate project code.
   - If the user mentions a project by name (e.g., "General", "Work"), map it to the corresponding code (e.g., "GEN", "JOB").
   - If the user's request implies a project (e.g., "misc", "random"), use your best judgment to map it to an existing project like "GEN".
   - Default to "GEN" if no project is specified or inferred.
4. Besides its one project, a task can carry any number of free-form tags (see each task's "tags").
   Use tag_task / untag_task to change them; reuse existing tags rather than near-duplicates.
3. Always confirm the details of the action you performed (e.g., "I've created the task 'Fly a kite' in the General (GEN) project").
"""

    messages = [{"role": "system", "content": system_prompt}] + user_msgs

    try:
        # Loop to handle tool calls
        for _ in range(5):
            response = completion(
                model=model,
                messages=messages,
                api_key=api_key,
                api_base=base_url,
                tools=CHATTABLE_TOOLS,
                tool_choice="auto"
            )

            message = response.choices[0].message
            # Convert Message object to a serializable dict (handles tool_calls)
            msg_dict = message.model_dump() if hasattr(message, "model_dump") else message.dict() if hasattr(message, "dict") else dict(message)
            messages.append(msg_dict)

            if not message.tool_calls:
                # Success! Return the AI message AND the history
                return jsonify({
                    "message": msg_dict.get("content"),
                    "model": model,
                    "history": messages[1:]
                })

            # Handle tool calls
            for tool_call in message.tool_calls:
                tool_name = tool_call.function.name
                tool_args = json.loads(tool_call.function.arguments)
                result = execute_chat_tool(tool_name, tool_args)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_name,
                    "content": result
                })

        return jsonify({
            "message": msg_dict.get("content") or "I performed several actions for you.",
            "model": model,
            "history": messages[1:]
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Morning brief ─────────────────────────────────────────────────────────────

def _read_brief_context():
    try:
        with open(brief_runner.context_path, encoding="utf-8") as fh:
            return fh.read(), os.path.getmtime(brief_runner.context_path)
    except FileNotFoundError:
        return None, None


def _load_brief_events(conn):
    """Events from the last collection with their state, in stable date order."""
    rows = []
    events_path = os.path.join(brief_runner.raw_dir, "events.jsonl")
    if os.path.exists(events_path):
        with open(events_path, encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh if l.strip()]
    return sorted(brief_state.annotate(conn, rows), key=lambda e: (e["start"], e["source_event_id"]))


@app.route("/brief.md")
def brief_markdown():
    text, _ = _read_brief_context()
    if text is None:
        return Response("No brief generated yet.\n", mimetype="text/markdown"), 503
    return Response(text, headers={"Content-Type": "text/markdown; charset=utf-8"})


@app.route("/api/brief", methods=["GET"])
def get_brief():
    text, mtime = _read_brief_context()
    c = brief_state.connect(BRIEF_DB_PATH)
    try:
        last_success = brief_state.last_success_date(c, BRIEF_TZ)
        last = (brief_state.last_runs(c, 1) or [None])[0]
        next_at, next_note = brief_schedule.describe(c, datetime.now(BRIEF_TZ), last_success)
    finally:
        c.close()
    return jsonify({
        "markdown": text or "",
        "updated": None if mtime is None else datetime.fromtimestamp(mtime, BRIEF_TZ).isoformat(timespec="minutes"),
        "running": brief_runner.running,
        "next_run": {"at": next_at.isoformat(timespec="minutes") if next_at else None, "note": next_note},
        "last_run": None if last is None else {
            "id": last["id"], "trigger": last["trigger"], "started_at": last["started_at"],
            "finished_at": last["finished_at"], "seconds": last["seconds"],
            "ok": None if last["finished_at"] is None else bool(last["ok"]),
        },
    })


@app.route("/api/brief/run", methods=["POST"])
def run_brief_now():
    return jsonify({"started": brief_runner.start("manual")})


@app.route("/api/brief/events", methods=["GET"])
def brief_events():
    status = request.args.get("status", "all")
    if status != "all" and status not in brief_state.STATUSES:
        return jsonify({"error": f"status must be all or one of {list(brief_state.STATUSES)}"}), 400
    c = brief_state.connect(BRIEF_DB_PATH)
    try:
        evs = [e for e in _load_brief_events(c) if status in ("all", e["_status"])]
    finally:
        c.close()
    return jsonify({"count": len(evs), "events": [
        {"id": e["source_event_id"], "title": e["title"], "start": e["start"], "url": e["url"],
         "source": e["source"], "status": e["_status"], "updated": e["_updated"]} for e in evs]})


@app.route("/api/brief/events/status", methods=["POST"])
def brief_event_status():
    data = request.json or {}
    ids, status = data.get("ids"), data.get("status")
    if (status not in brief_state.STATUSES or not isinstance(ids, list)
            or not all(isinstance(i, str) for i in ids) or len(ids) > 2000):
        return jsonify({"error": 'send JSON {"ids": ["meetup:123", ...], "status": "active|dismissed|muted"}'}), 400
    c = brief_state.connect(BRIEF_DB_PATH)
    try:
        updated, unknown = brief_state.set_statuses(c, ids, status)
    finally:
        c.close()
    brief_runner.rerender()
    return jsonify({"status": status, "updated": updated, "unknown": unknown})


@app.route("/api/brief/keywords", methods=["GET"])
def get_brief_keywords():
    c = brief_state.connect(BRIEF_DB_PATH)
    try:
        return jsonify({"keywords": brief_state.get_keywords(c)})
    finally:
        c.close()


@app.route("/api/brief/keywords", methods=["PUT"])
def put_brief_keywords():
    data = request.json or {}
    if not isinstance(data.get("keywords"), list):
        return jsonify({"error": "keywords must be a list of strings"}), 400
    c = brief_state.connect(BRIEF_DB_PATH)
    try:
        clean = brief_state.set_keywords(c, data["keywords"])
    finally:
        c.close()
    return jsonify({"keywords": clean})


@app.route("/api/brief/calendars", methods=["GET"])
def get_brief_calendars():
    c = brief_state.connect(BRIEF_DB_PATH)
    try:
        cals = brief_state.get_calendars(c)
    finally:
        c.close()
    # the URL is a secret (often a private iCal feed); never echo it back in full
    return jsonify({"calendars": [{"label": c["label"], "enabled": bool(c["enabled"]), "has_url": bool(c["url"])}
                                   for c in cals]})


@app.route("/api/brief/calendars", methods=["PUT"])
def put_brief_calendar():
    data = request.json or {}
    c = brief_state.connect(BRIEF_DB_PATH)
    try:
        label = brief_state.upsert_calendar(c, data.get("label", ""), (data.get("url") or "").strip() or None,
                                             enabled=data.get("enabled"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        c.close()
    return jsonify({"label": label})


@app.route("/api/brief/calendars/<label>", methods=["DELETE"])
def delete_brief_calendar(label):
    c = brief_state.connect(BRIEF_DB_PATH)
    try:
        brief_state.delete_calendar(c, label)
    finally:
        c.close()
    return jsonify({"deleted": True})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Toledo web server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    brief_scheduler.Scheduler(brief_runner, brief_schedule).start()
    print(f"Toledo server running on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
