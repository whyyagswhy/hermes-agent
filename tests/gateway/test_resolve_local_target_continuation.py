"""
#150614 — resolve_local_target returns parent chat_id for session_reset continuations.

A continuation row has: id=local-child, chat_id=local-parent, source=local.
resolve_local_target(sid=local-child) must return a SessionRef pointing at
local-child, not local-parent, because they are different conversations.
"""
import pytest
from unittest.mock import MagicMock, patch, call


def _make_authority(session_rows, lineage_owner=None):
    """Build a minimal authority stub with db.get_session and db._read_ctx."""
    authority = MagicMock()
    authority.profile_id = "prof-1"

    conn = MagicMock()
    # state_meta lookup: return None (not bound via POLICY/LEGACY prefix)
    conn.execute.return_value.fetchone.return_value = None

    db = MagicMock()
    db.get_session = lambda sid: session_rows.get(sid)
    db._read_ctx.return_value.__enter__ = lambda s: conn
    db._read_ctx.return_value.__exit__ = MagicMock(return_value=False)
    authority.db = db
    return authority, conn


class TestResolveLocalTargetContinuation:

    def test_continuation_returns_child_sid_not_parent_chat_id(self):
        """The core bug: child.chat_id == parent.id but ref must point at child."""
        from gateway.session_local_migration import resolve_local_target
        from gateway.session_contract import SessionRef

        rows = {
            "local-child": {
                "id": "local-child",
                "chat_id": "local-parent",
                "source": "local",
                "session_key": None,
                "origin_json": None,
            }
        }
        authority, _conn = _make_authority(rows)
        actor = MagicMock()

        ref = resolve_local_target(authority, actor, "local-child")

        assert isinstance(ref, SessionRef)
        assert ref.session_id == "local-child", (
            f"Expected local-child but got {ref.session_id} — "
            "resolve_local_target returned the parent chat_id"
        )

    def test_plain_local_row_unchanged(self):
        """Non-continuation local rows still return chat_id (backward compat)."""
        from gateway.session_local_migration import resolve_local_target
        from gateway.session_contract import SessionRef

        rows = {
            "local-abc": {
                "id": "local-abc",
                "chat_id": "local-abc",   # same — not a continuation
                "source": "local",
                "session_key": None,
                "origin_json": None,
            }
        }
        authority, _conn = _make_authority(rows)
        actor = MagicMock()

        ref = resolve_local_target(authority, actor, "local-abc")

        assert isinstance(ref, SessionRef)
        assert ref.session_id == "local-abc"

    def test_legacy_row_unchanged(self):
        """Rows with no chat_id (legacy 2026… ids) still hit adopt_legacy_session."""
        from gateway.session_local_migration import resolve_local_target

        rows = {
            "2026-01-01T00:00:00-abc": {
                "id": "2026-01-01T00:00:00-abc",
                "chat_id": None,
                "source": "local",
                "session_key": None,
                "origin_json": None,
            }
        }
        authority, _conn = _make_authority(rows)
        actor = MagicMock()

        with patch(
            "gateway.session_local_migration.adopt_legacy_session",
            return_value="legacy-result",
        ) as mock_adopt:
            result = resolve_local_target(authority, actor, "2026-01-01T00:00:00-abc")

        mock_adopt.assert_called_once()
        assert result == "legacy-result"
