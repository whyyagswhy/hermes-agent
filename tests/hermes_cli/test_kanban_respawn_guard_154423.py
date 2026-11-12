"""Regression tests for three check_respawn_guard false-positive bugs (#154423).

Bug A: bare \bauth\b in _RESPAWN_BLOCKER_RE matches worker prose (gh auth status).
Bug B: stale task-level last_failure_error checked instead of latest run's error.
Bug C: lane=='review' exemption ordered AFTER the blocker check; review cards blocked.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _add_task(conn, task_id: str, *, last_failure_error=None, lane: str = "ready") -> str:
    """Create a task via the public API, then optionally stamp last_failure_error."""
    created_id = kb.create_task(conn, title=f"task-{task_id}", assignee="agent")
    # Move to the requested lane and rename to our predictable id.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET id = ?, status = ? WHERE id = ?",
                     (task_id, lane, created_id))
        if last_failure_error is not None:
            conn.execute("UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                         (last_failure_error, task_id))
    return task_id


def _add_run(conn, task_id: str, *, outcome: str, ended_at=None, error=None) -> None:
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at, error) "
            "VALUES (?, 'done', ?, ?, ?, ?)",
            (task_id, outcome, int(time.time()) - 60, ended_at or int(time.time()), error),
        )


# ---------------------------------------------------------------------------
# Bug A: bare \bauth\b matches prose
# ---------------------------------------------------------------------------

def test_bug_a_bare_auth_in_prose_returns_false_positive(kanban_home):
    """Bare \\bauth\\b matches 'gh auth status' in a timed-out run's captured output.

    Expected after fix: None (not a real auth error).
    Current broken behaviour: 'blocker_auth'.
    """
    task_id = "t_bug_a"
    prose = (
        "Worker output: gh auth status shows token is valid. "
        "gh auth setup-git succeeded. Continuing with task."
    )
    with kbc.connect() as conn:
        _add_task(conn=conn, task_id=task_id, last_failure_error=prose)
        _add_run(conn=conn, task_id=task_id, outcome="timed_out", error=prose)
        result = kbd.check_respawn_guard(conn, task_id)

    assert result is None, (
        f"Bug A: prose 'gh auth status' should not trigger blocker_auth, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Bug B: stale last_failure_error checked after a successful run supersedes it
# ---------------------------------------------------------------------------

def test_bug_b_stale_last_failure_error_after_success(kanban_home):
    """A later successful run should not leave the guard tripped on the stale field.

    The task's last_failure_error was stamped from an earlier run that
    mentioned 'unauthorized'. The latest run completed successfully and its
    task_runs.error is NULL. check_respawn_guard should return None or
    'recent_success', not 'blocker_auth' from the stale field.
    """
    task_id = "t_bug_b"
    stale_error = "HTTP 401 Unauthorized: invalid API key"
    with kbc.connect() as conn:
        _add_task(conn=conn, task_id=task_id, last_failure_error=stale_error)
        _add_run(conn=conn, task_id=task_id, outcome="completed", error=None)
        result = kbd.check_respawn_guard(conn, task_id)

    assert result != "blocker_auth", (
        f"Bug B: stale last_failure_error should not block after a successful run, got {result!r}"
    )


# ---------------------------------------------------------------------------
# Bug C: review-lane exemption ordered after blocker check
# ---------------------------------------------------------------------------

def test_bug_c_review_lane_blocked_before_exemption(kanban_home):
    """A review-lane card whose last_failure_error matches the regex should be exempt.

    The review-lane exemption ('if lane == "review": return None') sits BELOW the
    blocker_auth check in the current code, so it is never reached when the error
    matches. Expected: None. Current broken: 'blocker_auth'.
    """
    task_id = "t_bug_c"
    stale_error = "401 Unauthorized: bearer token expired"
    with kbc.connect() as conn:
        _add_task(conn=conn, task_id=task_id, last_failure_error=stale_error, lane="review")
        _add_run(conn=conn, task_id=task_id, outcome="completed", error=None)
        result = kbd.check_respawn_guard(conn, task_id, lane="review")

    assert result is None, (
        f"Bug C: review-lane card should not be blocked by blocker_auth, got {result!r}"
    )
