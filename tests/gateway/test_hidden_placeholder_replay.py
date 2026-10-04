"""Regression tests for #132949: hidden interrupt placeholders must not replay.

An interrupted turn leaves an on-screen-empty assistant placeholder
(content="", display_kind="hidden") with a neutral api_content
sidecar ("[response interrupted]") so the pre-call sanitizer does not
re-heal it every call. That sidecar is transcript bookkeeping, not a reply —
but _has_replayable_sidecar replayed ANY empty-content assistant row with
a non-empty api_content, and substitute_api_content then swapped the
sidecar back into content on the wire, so the model echoed the
placeholder as its whole answer to an ordinary follow-up instruction.

Contract: hidden rows never replay; the reasoning-promotion row
(content="" + real api_content, no display_kind) still replays.
"""

from __future__ import annotations


from gateway.run import _build_gateway_agent_history, _has_replayable_sidecar

INTERRUPTED_PLACEHOLDER = "[response interrupted]"


def _hidden_placeholder_row() -> dict:
    return {
        "role": "assistant",
        "content": "",
        "display_kind": "hidden",
        "api_content": INTERRUPTED_PLACEHOLDER,
    }


def test_hidden_placeholder_row_is_not_replayable():
    assert _has_replayable_sidecar("assistant", "", _hidden_placeholder_row()) is False


def test_hidden_placeholder_absent_from_replayed_history():
    history = [
        {"role": "user", "content": "summarize the doc"},
        _hidden_placeholder_row(),
        {"role": "user", "content": "now do the ordinary follow-up"},
    ]
    agent_history, _ = _build_gateway_agent_history(history)
    assert [m["role"] for m in agent_history] == ["user", "user"]
    for msg in agent_history:
        assert INTERRUPTED_PLACEHOLDER not in str(msg.get("content", ""))
        assert msg.get("api_content") != INTERRUPTED_PLACEHOLDER


def test_reasoning_promotion_row_still_replayed():
    """A reasoning-only clean stop persists content="" with the promoted
    reply in api_content (no display_kind) — that row must replay."""
    history = [
        {"role": "user", "content": "2+2?"},
        {
            "role": "assistant",
            "content": "",
            "reasoning": "The answer is 4.",
            "api_content": "The answer is 4.",
        },
        {"role": "user", "content": "thanks"},
    ]
    agent_history, _ = _build_gateway_agent_history(history)
    assert [m["role"] for m in agent_history] == ["user", "assistant", "user"]
    assert agent_history[1]["api_content"] == "The answer is 4."
