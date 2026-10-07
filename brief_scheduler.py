"""Background scheduler for the morning brief: one run per local day at a random time in a
configured window, retried on failure, run on demand via Runner.start("manual").

Extracted from the standalone morning-brief project's server.py (github.com/krets/morning)
into Toledo. toledo_server.py is the only process that should construct and start a
Scheduler — toledo_mcp.py shares the same brief.db/context.md over the data volume but only
reads them, so two containers don't both kick off daily runs.
"""
import json
import logging
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta

import brief_build
import brief_state as state

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("toledo.brief")
RUN_TIMEOUT_S = 30 * 60
RETRY_AFTER_S = 30 * 60


class Runner:
    """Runs brief_build.py as a subprocess, one at a time, and records each run in the database."""

    def __init__(self, data_dir, db_path, tz_name, event_days, llm_resolver=None):
        """llm_resolver() -> (model, api_key, base_url) for the weather summary, or None to skip the LLM."""
        self.llm_resolver = llm_resolver
        self.data_dir, self.db_path, self.tz_name, self.event_days = data_dir, db_path, tz_name, event_days
        self.context_path = os.path.join(data_dir, "context.md")
        self.raw_dir = os.path.join(data_dir, "out")
        self._lock = threading.Lock()

    @property
    def running(self):
        return self._lock.locked()

    def start(self, trigger):
        """Kick off a run in the background; False if one is already in progress."""
        if not self._lock.acquire(blocking=False):
            return False
        threading.Thread(target=self._run, args=(trigger,), daemon=True).start()
        return True

    def _run(self, trigger):
        conn = state.connect(self.db_path)
        run_id = state.start_run(conn, trigger)
        ok, output = False, ""
        llm_file = None
        try:
            cmd = [sys.executable, os.path.join(HERE, "brief_build.py"), "--db", self.db_path, "--out", self.context_path,
                   "--raw-dir", self.raw_dir, "--timezone", self.tz_name, "--event-days", str(self.event_days),
                   "--run-id", str(run_id)]
            llm_file = self._write_llm_file()
            if llm_file:
                cmd += ["--llm-file", llm_file]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=RUN_TIMEOUT_S)
            ok, output = proc.returncode == 0, proc.stderr + proc.stdout
        except subprocess.TimeoutExpired as exc:
            output = f"timed out after {RUN_TIMEOUT_S}s\n{exc.stderr or ''}"
        except Exception as exc:  # keep the scheduler alive no matter what
            output = f"{type(exc).__name__}: {exc}"
        finally:
            if llm_file:
                os.remove(llm_file)
            state.finish_run(conn, run_id, ok, output)
            conn.close()
            self._lock.release()
            log.info("run %s (%s) %s", run_id, trigger, "ok" if ok else "FAILED")

    def _write_llm_file(self):
        """Private temp file with the LLM settings (it holds the API key), or None if unconfigured."""
        try:
            model, api_key, base_url = self.llm_resolver() if self.llm_resolver else (None, None, None)
        except Exception:
            log.exception("could not resolve LLM settings for the weather summary")
            return None
        if not model or model.endswith("/"):
            return None
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"model": model, "api_key": api_key, "base_url": base_url}, fh)
        return path

    def rerender(self):
        """Rebuild context.md from the saved raw output (fast; applies mute/dismiss immediately)."""
        if self.running:  # the running job renders with fresh state when it finishes
            return
        rerender(self.data_dir, self.db_path, self.tz_name, self.event_days)


def rerender(data_dir, db_path, tz_name, event_days):
    """Re-render context.md from the saved raw output. Also used by toledo_mcp.py, which has no Runner."""
    brief_build.main(["--from-raw", "--db", db_path, "--out", os.path.join(data_dir, "context.md"),
                      "--raw-dir", os.path.join(data_dir, "out"), "--timezone", tz_name,
                      "--event-days", str(event_days)])


def parse_hhmm(s):
    h, m = s.strip().split(":")
    return int(h) * 60 + int(m)  # minutes after midnight


class Schedule:
    """One run per local day at a random minute inside a window, e.g. "02:00-06:00".

    The minute is picked the first time a day's plan is asked for and stored in the database,
    so restarts neither re-roll it nor hand out a second chance.
    """

    def __init__(self, spec, tz):
        start, _, end = spec.partition("-")
        self.start, self.end = parse_hhmm(start), parse_hhmm(end or start)
        if not (0 <= self.start <= self.end < 24 * 60):
            raise ValueError(f"bad schedule window {spec!r}: need HH:MM-HH:MM within one day")
        self.tz, self.spec = tz, spec

    def planned(self, conn, day):
        key = f"planned:{day.isoformat()}"
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row:
            return datetime.fromisoformat(row["value"])
        minute = random.randint(self.start, self.end)
        at = datetime(day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=self.tz)
        with conn:
            conn.execute("INSERT OR IGNORE INTO meta VALUES (?, ?)", (key, at.isoformat()))
            conn.execute("DELETE FROM meta WHERE key LIKE 'planned:%' AND key < ?",
                         (f"planned:{(day - timedelta(days=7)).isoformat()}",))
        return at

    def due(self, conn, now, last_success, last_attempt_age):
        if last_attempt_age <= RETRY_AFTER_S:
            return False
        if last_success is None:
            return True  # never ran: collect now rather than wait for tomorrow
        return last_success < now.date() and now >= self.planned(conn, now.date())

    def describe(self, conn, now, last_success):
        """(datetime or None, human text) for the next run."""
        if last_success is None:
            return None, "due now"
        if last_success >= now.date():
            window = self.spec.replace("-", " and ")
            return None, f"tomorrow, at a random time between {window}" if self.start != self.end else \
                f"tomorrow at {self.spec}"
        at = self.planned(conn, now.date())
        return (at, at.strftime("%a %Y-%m-%d %H:%M")) if now < at else (None, "due now")


class Scheduler(threading.Thread):
    """Starts a run once per local day per Schedule (or at boot if today's was missed)."""

    def __init__(self, runner, schedule):
        super().__init__(daemon=True, name="brief-scheduler")
        self.runner, self.schedule = runner, schedule

    def run(self):
        conn = state.connect(self.runner.db_path)
        while True:
            try:
                now = datetime.now(self.schedule.tz)
                if not self.runner.running and self.schedule.due(
                        conn, now, state.last_success_date(conn, self.schedule.tz), state.last_attempt_age(conn)):
                    self.runner.start("schedule")
            except Exception:
                log.exception("brief scheduler tick failed")
            time.sleep(30)
