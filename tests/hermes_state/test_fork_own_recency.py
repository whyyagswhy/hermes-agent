"""A fork sorts by when it was made, not by the parent history it copied (#146712).

Both fork paths copy the parent's message rows, so the child's freshest message
carries the parent's timestamp: ``_clone_message_rows`` for the canonical
``session.mutate branch`` verb the Desktop uses, and ``replace_messages`` for the
API ``/api/sessions/{id}/fork`` handler. Recency is
``COALESCE(last_activity_at, MAX(messages.timestamp), started_at)``
(hermes_state_common._sql_freshest_of) and neither INSERT path stamps
``last_activity_at`` — so a fresh fork is ranked and displayed by the time of the
conversation it forked FROM, and can sort below conversations the user never
touched.

Compression continuations are the opposite case and must keep the parent's
recency: the child IS the same conversation, and the listing walks the
compression chain to its tip.
"""

from __future__ import annotations

import time

import pytest

from hermes_state import SessionDB


OLD = time.time() - 3 * 86400  # the parent's last message: three days ago


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "test_state.db")
    yield session_db
    session_db.close()


def _parent_with_old_history(db: SessionDB, session_id: str = "parent") -> None:
    db.create_session(session_id, "local")
    db.append_message(session_id, role="user", content="old question", timestamp=OLD)
    db.append_message(session_id, role="assistant", content="old answer", timestamp=OLD + 30)


def _branch_through_publish(db: SessionDB, *, child: str = "branch", model_config=None) -> float:
    """Publish a child row as the canonical branch verb does, then clone the tail."""
    made_at = time.time()
    if model_config is None:
        model_config = {"_branched_from": "parent"}

    def write(conn):
        parent = conn.execute("SELECT * FROM sessions WHERE id=?", ("parent",)).fetchone()
        db._publish_child_session_row(
            conn, parent, parent_session_id="parent", child_session_id=child, source="local",
            model=None, model_config=model_config, system_prompt=None, cwd=None,
            profile_name=parent["profile_name"])
        ids = [row[0] for row in conn.execute(
            "SELECT id FROM messages WHERE session_id=? AND active=1 ORDER BY id", ("parent",))]
        db._clone_message_rows(conn, ids, session_id=child)

    db._execute_write(write)
    return made_at


def _assert_sorts_by_creation(db: SessionDB, child: str, made_at: float) -> None:
    assert db.get_session(child) is not None
    listed = {entry["id"]: entry for entry in db.list_sessions_rich(order_by_last_active=True)}

    assert listed[child]["last_active"] >= made_at - 1, (
        f"fork listed at last_active={listed[child]['last_active']}, the copied history's "
        f"{OLD}, not the fork's own creation {made_at}")
    assert listed[child]["last_active"] > listed["parent"]["last_active"]


def test_canonical_branch_sorts_by_its_own_creation(db):
    """Desktop/slash ``session.mutate branch`` publishes through _publish_child_session_row."""
    _parent_with_old_history(db)
    made_at = _branch_through_publish(db)

    _assert_sorts_by_creation(db, "branch", made_at)


def test_api_fork_sorts_by_its_own_creation(db):
    """``POST /api/sessions/{id}/fork`` creates the row, ends the parent, copies messages."""
    _parent_with_old_history(db)
    made_at = time.time()
    db.create_session("fork", "api_server", parent_session_id="parent",
                      model_config={"_branched_from": "parent"})
    db.end_session("parent", "branched")
    db.replace_messages("fork", db.get_messages("parent"))

    assert [m["timestamp"] for m in db.get_messages("fork")] == [OLD, OLD + 30], (
        "the fork no longer copies the parent's timestamps, so this test reaches nothing")

    _assert_sorts_by_creation(db, "fork", made_at)


def test_compression_continuation_keeps_the_parents_recency(db):
    """A continuation is the same conversation: stamping it would cut the chain tip off."""
    _parent_with_old_history(db)
    _branch_through_publish(db, child="continuation", model_config={})

    assert db.get_session("continuation")["last_activity_at"] is None
