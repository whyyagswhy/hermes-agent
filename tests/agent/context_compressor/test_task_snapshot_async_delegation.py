"""Task-snapshot anchoring must look past runtime delegation notices."""

from unittest.mock import patch

from agent.context_compressor import ContextCompressor
from agent.prompt_builder import steer_user_row

NOTICE = (
    "[ASYNC DELEGATION BATCH COMPLETE \u2014 synthetic-review]\n"
    "A review of an older, unrelated task has finished."
)


def _compressor():
    with patch("agent.context_compressor.get_model_context_length", return_value=100000):
        return ContextCompressor(model="test", quiet_mode=True)


def test_task_snapshot_skips_async_delegation_notices():
    """A delegation completion notice is runtime traffic, never the human's ask.

    The registry emits ``[ASYNC DELEGATION ...]`` rows in the user role; the
    anchor must look past them to the real request, and return None when no
    request exists at all.
    """
    c = _compressor()

    request = {"role": "user", "content": "Perform the current document task."}
    notice = {"role": "user", "content": NOTICE}

    # Notice after a request must not displace the request.
    snapshot = c._latest_user_task_snapshot([request, notice])
    assert snapshot is not None
    assert "Perform the current document task." in snapshot
    assert "ASYNC DELEGATION" not in snapshot

    # Notice-only history must not invent a human task.
    assert c._latest_user_task_snapshot([notice]) is None

    # A later genuine request still wins over an earlier notice.
    later = {"role": "user", "content": "Now summarize the second section."}
    snapshot = c._latest_user_task_snapshot([notice, later])
    assert snapshot is not None
    assert "Now summarize the second section." in snapshot
    assert "ASYNC DELEGATION" not in snapshot

    # A person quoting a notice mid-body is still making a real ask.
    quoted = {
        "role": "user",
        "content": "Why did this fire? [ASYNC DELEGATION BATCH COMPLETE \u2014 synthetic-review]",
    }
    snapshot = c._latest_user_task_snapshot([quoted])
    assert snapshot is not None
    assert "Why did this fire?" in snapshot


def test_notice_only_transcript_has_no_real_user_turn():
    """The provenance gate must agree with the anchor: no notice licenses "User asked"."""
    c = _compressor()
    notice = {"role": "user", "content": NOTICE}

    # A notice-only history has no human turn, so the summary may not claim one.
    assert c._transcript_has_real_user_turn([notice]) is False

    assert c._transcript_has_real_user_turn([{"role": "user", "content": "Do the thing."}]) is True

    snapshot = c._latest_user_task_snapshot([{"role": "user", "content": "Do the thing."}])
    assert snapshot is not None
    assert "Do the thing." in snapshot


def test_steer_row_still_anchors_and_counts_as_a_user_turn():
    """``display_kind="steer"`` is the person's own mid-turn message, never machinery."""
    c = _compressor()
    steer = steer_user_row("also check the second file")

    snapshot = c._latest_user_task_snapshot([{"role": "user", "content": "first ask"}, steer])
    assert snapshot is not None
    assert "also check the second file" in snapshot
    assert c._transcript_has_real_user_turn([steer]) is True


def test_machinery_display_rows_do_not_anchor():
    """A background-process completion card is operational traffic, even without a marker prefix."""
    c = _compressor()
    card = {"role": "user", "content": "Background process finished.", "display_kind": "process_complete"}

    assert c._latest_user_task_snapshot([card]) is None
    assert c._transcript_has_real_user_turn([card]) is False


def test_machinery_rows_are_synthetic_on_every_predicate():
    """All three user-row predicates must agree: a stamped card is not a human turn on any of them.

    ``_is_real_user_message`` reads ``_is_synthetic_compression_user_turn``, so a card that only
    the anchor skipped would still be a "real user message" to the reply anchor and focus picker.
    """
    from agent.conversation_compression import _is_real_user_message
    from session_rules.turns import _is_synthetic_compression_user_turn as session_rules_copy

    c = _compressor()
    for kind in ("internal_notification", "process_complete", "async_delegation_complete"):
        card = {"role": "user", "content": "runtime traffic", "display_kind": kind}
        assert c._is_synthetic_compression_user_turn(card) is True, kind
        assert _is_real_user_message(card) is False, kind
        assert session_rules_copy(card) is True, kind


def test_human_quoting_the_marker_mid_text_still_anchors():
    """The marker match is ``startswith``, so quoting it mid-sentence stays human."""
    c = _compressor()
    quoted = {"role": "user", "content": "Why did this fire? [ASYNC DELEGATION BATCH COMPLETE — x]"}

    snapshot = c._latest_user_task_snapshot([quoted])
    assert snapshot is not None
    assert "Why did this fire?" in snapshot


def test_later_request_wins_over_an_earlier_notice():
    c = _compressor()
    snapshot = c._latest_user_task_snapshot([
        {"role": "user", "content": NOTICE},
        {"role": "user", "content": "Now summarize the second section."},
    ])
    assert snapshot is not None
    assert "Now summarize the second section." in snapshot


def test_steer_row_stays_an_auto_focus_candidate():
    """Auto-focus reads intent from real turns. ``steer`` is the person's own message, not machinery."""
    from agent.prompt_builder import steer_user_row

    c = _compressor()
    messages = [
        {"role": "user", "content": "please refactor the parser"},
        {"role": "assistant", "content": "working on it"},
        steer_user_row("actually, keep the old signature"),
    ]
    assert "old signature" in (c._derive_auto_focus_topic(messages) or "")


def test_machinery_row_is_not_an_auto_focus_candidate():
    """A stamped card must never become the compaction focus hint."""
    c = _compressor()
    messages = [
        {"role": "user", "content": "please refactor the parser"},
        {"role": "assistant", "content": "working on it"},
        {"role": "user", "content": "runtime traffic", "display_kind": "process_complete"},
    ]
    focus = c._derive_auto_focus_topic(messages) or ""
    assert "runtime traffic" not in focus
