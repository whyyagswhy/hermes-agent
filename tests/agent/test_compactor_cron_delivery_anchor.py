"""#154252 — mid-turn compaction must anchor on the cron/bot-chat delivery row.

A turn started by a ``--deliver bot-chat`` cron notification has
``display_kind="internal_notification"`` on its user row.
``_is_actionable_user_turn`` correctly rejects display_kind rows in general
(so they do not seed auto-focus); but during an *in-flight* turn the notification
row IS the active request and must stay in the tail.

The fix: ``_run_conversation_turn`` stamps ``_active_turn_user_idx`` on the agent
at turn start; ``_find_last_user_message_idx`` uses it as a fallback when the
active-turn row is a display_kind row that the normal scan would skip.
"""

import pytest
from agent.context_compressor import ContextCompressor


def _make_compressor():
    cc = ContextCompressor(
        model="test-model",
        threshold_percent=0.75,
        protect_first_n=5,
        protect_last_n=20,
        quiet_mode=True,
        config_context_length=40960,
        provider="test",
    )
    return cc


def _build_inflight_transcript():
    """
    Simulates a mid-turn compaction on a Bot Chat fed by cron.

    Layout:
      [0]  system
      [1]  user (human, old turn)
      [2]  assistant
      [3]  user (internal_notification, THIS turn started here)   ← must be in tail
      [4]  assistant (first response in this turn)
      [5]  user  (async_delegation_complete, still this turn)
      [6]  assistant
    """
    return [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "earlier human message"},
        {"role": "assistant", "content": "done"},
        # Cron delivery that started the in-flight turn:
        {"role": "user", "content": "cron: run weekly digest", "display_kind": "internal_notification"},
        {"role": "assistant", "content": "Starting weekly digest…"},
        # Async delegation complete (also display_kind, mid-turn):
        {"role": "user", "content": "subagent done", "display_kind": "async_delegation_complete"},
        {"role": "assistant", "content": "Processing subagent result…"},
    ]


CRON_DELIVERY_IDX = 3  # index in the transcript above


class TestCronDeliveryAnchor:
    """RED: without _active_turn_user_idx, the notification row is invisible to
    the tail anchor; GREEN: with it set, the row is protected."""

    def test_red_notification_invisible_without_active_turn_idx(self):
        """Without the fix, _find_last_user_message_idx returns the OLD human turn
        (index 1), not the cron delivery at index 3.  The delivery row would be
        summarized away, dropping the in-flight task."""
        cc = _make_compressor()
        msgs = _build_inflight_transcript()
        # No _active_turn_user_idx set (simulates pre-fix behaviour)
        assert not hasattr(cc, "_active_turn_user_idx") or cc._active_turn_user_idx == -1
        idx = cc._find_last_user_message_idx(msgs, head_end=0)
        # Should land on the old human row (index 1), not the notification row (index 3)
        assert idx == 1, (
            f"expected old human turn at 1, got {idx} — "
            "meaning the notification row already anchors the tail on this build"
        )

    def test_green_notification_anchored_with_active_turn_idx(self):
        """With _active_turn_user_idx set to the cron delivery row, the row is
        returned by _find_last_user_message_idx and the cut is adjusted to keep
        the delivery in the tail."""
        cc = _make_compressor()
        msgs = _build_inflight_transcript()
        cc._active_turn_user_idx = CRON_DELIVERY_IDX
        idx = cc._find_last_user_message_idx(msgs, head_end=0)
        assert idx == CRON_DELIVERY_IDX, (
            f"expected cron delivery row at {CRON_DELIVERY_IDX}, got {idx}"
        )

    def test_ensure_tail_cut_protects_delivery_row(self):
        """_ensure_last_user_message_in_tail adjusts cut_idx back to the delivery
        row when _active_turn_user_idx is set."""
        cc = _make_compressor()
        msgs = _build_inflight_transcript()
        cc._active_turn_user_idx = CRON_DELIVERY_IDX
        # Simulate a cut that would summarise the delivery row (cut is AFTER it)
        adjusted = cc._ensure_last_user_message_in_tail(msgs, cut_idx=5, head_end=0)
        assert adjusted <= CRON_DELIVERY_IDX, (
            f"cut not pulled back to protect delivery row: cut={adjusted}, delivery={CRON_DELIVERY_IDX}"
        )

    def test_existing_human_turn_unaffected(self):
        """When the last user row IS actionable (human), _active_turn_user_idx does
        not override it — existing behaviour is preserved."""
        cc = _make_compressor()
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "cron", "display_kind": "internal_notification"},
            {"role": "assistant", "content": "noted"},
            {"role": "user", "content": "deploy the fix"},   # real human row ← index 3
            {"role": "assistant", "content": "deploying"},
        ]
        # _active_turn_user_idx points to the human row
        cc._active_turn_user_idx = 3
        idx = cc._find_last_user_message_idx(msgs, head_end=0)
        # Normal scan already finds it; _active_turn_user_idx adds no change
        assert idx == 3
        assert msgs[3]["content"] == "deploy the fix"
