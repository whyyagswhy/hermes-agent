"""Regression coverage for Kanban CLI process exit status propagation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[2]


def _run_hermes(home: Path, *args: str, marker: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    for name in (
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if marker:
        env["HERMES_DELEGATED_CHILD_CONTEXT"] = "1"
    else:
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_delegated_child_kanban_cli_refusal_returns_nonzero_exit_status(tmp_path):
    """A printed Kanban mutation refusal must not look like CLI success."""
    home = tmp_path / "hermes"
    home.mkdir()

    created = _run_hermes(home, "kanban", "create", "exit status probe", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]

    refused = _run_hermes(
        home,
        "kanban",
        "comment",
        task_id,
        "must be refused",
        marker=True,
    )

    assert refused.returncode == 1
    assert "delegate_task" in refused.stderr


def _finalize_with_failover_reasons(reasons, exit_reason="rebuilt_restart_limit_exceeded"):
    """Finalize a restart-limit turn whose fallback chain saw reasons (#133361)."""
    from unittest.mock import MagicMock

    from run_agent import AIAgent
    from agent.turn_finalizer import finalize_turn

    agent = AIAgent(
        model="openai/gpt-4o-mini",
        provider="openrouter",
        api_key="sk-dummy",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        skip_background_review=True,
        platform="cli",
    )
    agent._spawn_background_review = MagicMock()
    agent._save_trajectory = MagicMock()
    agent._cleanup_task_resources = MagicMock()
    agent._persist_session = MagicMock()
    agent._session_messages = []
    agent._file_mutation_verifier_enabled = lambda: False
    agent.clear_interrupt = MagicMock()
    agent._stream_callback = None
    agent._sync_external_memory_for_turn = MagicMock()
    agent._skill_nudge_interval = 0
    agent._iters_since_skill = 0
    agent.valid_tool_names = set()
    agent.iteration_budget = MagicMock()
    agent.iteration_budget.remaining = 100
    agent.iteration_budget.used = 5
    agent.iteration_budget.max_total = 100
    agent.max_iterations = 50
    agent._emit_status = MagicMock()
    agent._safe_print = MagicMock()
    agent.context_compressor = None
    agent._turn_preflight_display_snapshot = None
    agent._turn_received_provider_response = False
    agent.model = "test-model"
    agent.session_id = "test-session"
    agent.quiet_mode = True
    agent._turn_failed_file_mutations = {}
    agent._db_flush_scan_prefix = None
    agent._turn_failover_reasons = list(reasons)
    return finalize_turn(
        agent,
        final_response=None,
        api_call_count=3,
        interrupted=False,
        failed=False,
        messages=[{"role": "user", "content": "hi"}],
        conversation_history=[],
        effective_task_id="test",
        turn_id="test-turn",
        user_message="hi",
        original_user_message="hi",
        _should_review_memory=False,
        _turn_exit_reason=exit_reason,
    )


def test_restart_limit_all_transient_exits_75(monkeypatch):
    """Two-provider chain, both 429: restart-limit exit maps to EX_TEMPFAIL (#133361)."""
    from hermes_cli.cli_single_query import _single_query_exit_code

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    result = _finalize_with_failover_reasons(["rate_limit", "rate_limit"])
    assert result["failure_reason"] == "rate_limit"
    assert _single_query_exit_code(result) == 75


def test_restart_limit_all_terminal_exits_78(monkeypatch):
    """All-terminal failovers stamp the dominant terminal reason (EX_CONFIG, #133361)."""
    from hermes_cli.cli_single_query import _single_query_exit_code

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    result = _finalize_with_failover_reasons(["auth", "auth"])
    assert result["failure_reason"] == "auth"
    assert _single_query_exit_code(result) == 78


def test_restart_limit_mixed_failovers_keep_loop_error(monkeypatch):
    """Mixed transient+terminal failovers keep the generic loop_error (exit 1, #133361)."""
    from hermes_cli.cli_single_query import _single_query_exit_code

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-123")
    result = _finalize_with_failover_reasons(["rate_limit", "auth"])
    assert result["failure_reason"] == "loop_error"
    assert _single_query_exit_code(result) == 1
