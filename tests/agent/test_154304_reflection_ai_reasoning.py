"""#154304 — Reflection AI (Beam) reasoning sanitization.

Bug A: `reasoning` carrier sent to Reflection AI → HTTP 400 "Unrecognized request argument".
       Fix: restrict carrier to {reasoning_content} only for api.reflection.ai host.
Bug B: tool-call assistant turns without reasoning_content → HTTP 400 "Missing required parameter".
       Fix: add reflection to the must-echo family so missing values are padded with " ".
"""
import pytest

REFLECT_URL = "https://api.reflection.ai/openai/v1"
REFLECT_MODEL = "Beam-1.0-501B-A23B"


# ── Bug A: `reasoning` carrier must be excluded for reflection.ai ──────────────

def test_reflection_carrier_excludes_reasoning():
    """Reflection AI rejects `reasoning` → route must be {reasoning_content} only."""
    from agent.message_sanitization import route_reasoning_carriers
    carriers = route_reasoning_carriers("custom", REFLECT_URL)
    assert "reasoning" not in carriers, (
        "BUG #154304-A: `reasoning` carrier present for reflection.ai; "
        "provider returns 400 'Unrecognized request argument supplied: messages[N].reasoning'"
    )
    assert "reasoning_content" in carriers, "reasoning_content must remain in carrier set"


def test_non_reflection_custom_keeps_local_carriers():
    """Other custom endpoints (ollama, upstage) must keep both local carriers."""
    from agent.message_sanitization import route_reasoning_carriers
    carriers = route_reasoning_carriers("custom", "http://localhost:11434/v1")
    assert "reasoning" in carriers, "ollama custom should still carry `reasoning`"


# ── Bug B: missing reasoning_content on tool-call turns must be padded ─────────

def test_reflection_echo_family_pads():
    """Reflection AI must be in the must-echo family → needs_reasoning_echo returns True."""
    from agent.message_sanitization import needs_reasoning_echo
    assert needs_reasoning_echo("custom", REFLECT_MODEL, REFLECT_URL) is True, (
        "BUG #154304-B: needs_reasoning_echo=False for reflection.ai; "
        "provider returns 400 'Missing required parameter: messages[N].reasoning_content'"
    )


def test_reflection_echo_family_matches_on_host():
    """Host match (api.reflection.ai) triggers echo even with generic provider/model."""
    from agent.message_sanitization import needs_reasoning_echo
    assert needs_reasoning_echo("custom", "unknown-model", REFLECT_URL) is True


def test_non_reflection_custom_no_pad_by_default():
    """Generic custom providers must NOT force-pad reasoning_content."""
    from agent.message_sanitization import needs_reasoning_echo
    assert needs_reasoning_echo("custom", "generic-model", "http://localhost:9000/v1") is False


# ── Integration: replay_route combines both fixes ──────────────────────────────

def test_reflection_replay_route():
    """reasoning_replay_route for reflection.ai: pad=True, carriers={reasoning_content}."""
    from agent.message_sanitization import reasoning_replay_route
    route = reasoning_replay_route("chat_completions", "custom", REFLECT_MODEL, REFLECT_URL)
    assert route.pad is True, "BUG #154304-B: pad should be True for reflection.ai"
    assert "reasoning_content" in route.carriers, "reasoning_content must be in carrier set"
    assert "reasoning" not in route.carriers, "BUG #154304-A: reasoning must not be in carrier set"


def test_reapply_echo_pads_missing_reasoning_content():
    """reapply_reasoning_echo pads empty tool-call turns for Reflection AI."""
    from agent.message_sanitization import reapply_reasoning_echo
    messages = [
        {"role": "user", "content": "do something"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "tc1", "type": "function",
                          "function": {"name": "fn", "arguments": "{}"}}]},
        {"role": "tool", "content": "result", "tool_call_id": "tc1"},
        {"role": "user", "content": "continue"},
    ]
    # needs_thinking_pad=True mirrors needs_reasoning_echo(reflection)
    reapply_reasoning_echo(messages, needs_thinking_pad=True,
                           carriers=frozenset({"reasoning_content"}))
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    for m in assistant_msgs:
        rc = m.get("reasoning_content")
        assert rc is not None and rc != "", (
            "BUG #154304-B: tool-call assistant turn has empty/absent reasoning_content "
            "after reapply_reasoning_echo"
        )
