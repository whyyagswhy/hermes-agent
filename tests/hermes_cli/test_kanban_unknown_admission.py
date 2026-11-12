"""#154265 — unknown owner-admission must not spend the card's failure budget.

RED on main: exit 69 → kind="nonzero_exit" → rate_limited=False → _account_crashes
increments consecutive_failures → task blocked after two occurrences.

GREEN on fix: exit 69 → kind="unknown_admission" → rate_limited=True →
task skips _account_crashes entirely → consecutive_failures stays 0.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

_UNKNOWN_ADMISSION_EXIT = 69


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _make_running_task(conn) -> tuple[str, int]:
    tid = kb.create_task(conn, title="design task t_c16662b9")
    kb.claim_task(conn, tid)
    fake_pid = 999999
    with kbc.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
            (fake_pid, int(time.time()) - 300, tid),
        )
    return tid, fake_pid


def test_exit_code_unknown_receipt_returns_sentinel_not_raises() -> None:
    from hermes_cli.kanban_worker_client import _exit_code
    result = _exit_code({"status": "unknown"}, params={}, board_db=None)
    assert result == _UNKNOWN_ADMISSION_EXIT, (
        f"expected {_UNKNOWN_ADMISSION_EXIT}, got {result!r}"
    )


def test_exit_code_kind_maps_unknown_admission() -> None:
    from hermes_cli.kanban_db_dispatch import _exit_code_kind, _KANBAN_UNKNOWN_ADMISSION_EXIT
    assert _KANBAN_UNKNOWN_ADMISSION_EXIT == _UNKNOWN_ADMISSION_EXIT
    kind, code = _exit_code_kind(_UNKNOWN_ADMISSION_EXIT)
    assert kind == "unknown_admission", f"got kind={kind!r}"
    assert code == _UNKNOWN_ADMISSION_EXIT


def test_classify_dead_worker_exit_unknown_admission() -> None:
    from hermes_cli.kanban_db_dispatch import _classify_dead_worker_exit
    dead = _classify_dead_worker_exit(
        pid=12345, claimer="local-claim-abc", exit_code=_UNKNOWN_ADMISSION_EXIT,
    )
    assert dead.rate_limited is True, (
        f"rate_limited={dead.rate_limited!r}; unknown admission must reuse no-budget path"
    )
    assert "race" in dead.error_text or "unknown" in dead.error_text, dead.error_text


def test_unknown_admission_does_not_spend_failure_budget(kanban_home: Path) -> None:
    """Core regression: exit 69 must NOT increment consecutive_failures.

    On main (unfixed): exit 69 → nonzero_exit → _account_crashes increments counter.
    On fix: exit 69 → unknown_admission → rate_limited=True → _account_crashes skipped.
    """
    from hermes_cli.kanban_db_dispatch import _classify_dead_worker_exit, _account_crashes

    with kbc.connect() as conn:
        tid, fake_pid = _make_running_task(conn)
        dead = _classify_dead_worker_exit(
            pid=fake_pid, claimer="local-host-claim", exit_code=_UNKNOWN_ADMISSION_EXIT,
        )

        if not dead.rate_limited:
            # On main: would call _account_crashes (the failing path)
            _account_crashes(conn, [(tid, fake_pid, "local-host-claim", dead)])

        task = kb.get_task(conn, tid)
        assert task.consecutive_failures == 0, (
            f"consecutive_failures={task.consecutive_failures}, want 0 — "
            "unknown admission must not spend the failure budget"
        )
