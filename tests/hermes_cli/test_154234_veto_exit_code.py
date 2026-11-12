"""Exit code for a vetoed turn (#154234).

A ``pre_llm_call`` hook that returns ``{"action": "block", ...}`` is a veto:
the model never ran and the task was not done.  ``hermes -z`` must exit non-zero
so an unattended caller (cron, CI, wrapper script) can distinguish it from a
successful answer.
"""

from __future__ import annotations

import pytest

from agent.turn_context import PRE_LLM_BLOCK_EXIT_REASON
from hermes_cli.oneshot import _PRE_LLM_BLOCK_EXIT_REASON, _oneshot_exit_code


# ---------------------------------------------------------------------------
# Red proof (documents the bug on main before the fix)
# ---------------------------------------------------------------------------


def test_veto_exit_code_is_nonzero():
    """A vetoed turn exits 2, not 0.

    Without the fix, ``completed=True`` and ``failed=False`` cause
    ``_oneshot_exit_code`` to fall through to ``return 0``; with the fix it
    checks ``turn_exit_reason`` and returns 2 instead.
    """
    result = {
        "completed": True,
        "failed": False,
        "interrupted": False,
        "partial": False,
        "turn_exit_reason": PRE_LLM_BLOCK_EXIT_REASON,
        "final_response": "BLOCKED: policy",
    }
    assert _oneshot_exit_code("BLOCKED: policy", result) == 2


# ---------------------------------------------------------------------------
# Regression guards: other exit-code paths must be unaffected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result,response,want",
    [
        # Normal completed turn → 0
        ({"completed": True, "failed": False, "interrupted": False, "partial": False,
          "turn_exit_reason": "completed"},
         "some answer", 0),
        # Failed turn → 2
        ({"completed": False, "failed": True, "interrupted": False, "partial": False,
          "turn_exit_reason": "all_retries_exhausted_no_response"},
         "", 2),
        # Partial turn → 2
        ({"completed": True, "failed": False, "interrupted": False, "partial": True,
          "turn_exit_reason": "max_iterations_reached(4/4)"},
         "partial", 2),
        # Interrupted → 130
        ({"completed": False, "failed": False, "interrupted": True, "partial": False,
          "turn_exit_reason": "interrupted"},
         None, 130),
        # Completed but empty response → 1
        ({"completed": True, "failed": False, "interrupted": False, "partial": False,
          "turn_exit_reason": "completed"},
         "   ", 1),
    ],
)
def test_other_exit_codes_unchanged(result, response, want):
    assert _oneshot_exit_code(response, result) == want


def test_pre_llm_block_exit_reason_constant_matches_turn_context():
    """The local constant in oneshot.py must stay in sync with turn_context.py."""
    assert _PRE_LLM_BLOCK_EXIT_REASON == PRE_LLM_BLOCK_EXIT_REASON
