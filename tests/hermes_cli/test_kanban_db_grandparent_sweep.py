"""Regression tests for kanban grandparent workspace sweep (#135782).

Kept in a separate file so test_kanban_db.py stays under its FILE_LINES cap.
"""
from __future__ import annotations

import pytest
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_workspace as kbw


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_deferred_child_completion_sweeps_grandparent_scratch(kanban_home):
    """Middle task completing with an active child must still sweep its grandparent.

    Chain: A (scratch) -> B (scratch) -> C (leaf).
    Without #135782 fix: B completing with C active deferred B's workspace
    cleanup and returned early, never calling _try_cleanup_parent_workspaces.
    A was stranded because its only child B was terminal but the sweep never ran.
    With fix: the deferral branch calls _try_cleanup_parent_workspaces before
    returning, so A is swept as soon as B becomes terminal.
    """
    with kbc.connect() as conn:
        a = kb.create_task(conn, title="grandparent-scratch")
        b = kb.create_task(conn, title="parent-scratch")
        c = kb.create_task(conn, title="leaf-child")
        kb.link_tasks(conn, a, b)
        kb.link_tasks(conn, b, c)

        a_task = kb.get_task(conn, a)
        a_ws = kbw.resolve_workspace(a_task)
        kbw.set_workspace_path(conn, a, a_ws)

        b_task = kb.get_task(conn, b)
        b_ws = kbw.resolve_workspace(b_task)
        kbw.set_workspace_path(conn, b, b_ws)

        # A completes while B (and C) are active — deferred.
        kb.complete_task(conn, a, result="done")
        assert a_ws.exists(), "A must be deferred while B is active"

        # B completes while C is still active — B is deferred.
        # The fix ensures A is swept here: A is terminal, its only child B is
        # now terminal, so _try_cleanup_parent_workspaces must remove A's dir.
        kb.complete_task(conn, b, result="done")
        assert b_ws.exists(), "B must be deferred while C is active"
        assert not a_ws.exists(), (
            "A must be swept when B becomes terminal, even though B is itself deferred (#135782)"
        )

        # C completes — B's deferred cleanup now runs.
        kb.complete_task(conn, c, result="done")
        assert not b_ws.exists(), "B must be swept after C completes"

