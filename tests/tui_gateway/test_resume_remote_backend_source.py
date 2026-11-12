"""session.resume cold-mount path: stored source and user_id must survive reconnect.

Bug class (issue #154216, bugs 2 and 3):

Bug 2 — ``_Resume.mint()`` ignores ``self.found["source"]``
   When a Desktop session persisted with ``source='desktop'`` is resumed by a
   reconnect call that omits ``source`` (the internal ``attachForRequest`` /
   ``recoverCanonicalPrompt`` paths in the Desktop client), ``_new_runtime_ids``
   resolves from the env instead of from the stored row. On a remote backend
   where ``HERMES_DESKTOP`` is unset (the most common remote-Linux split-topology
   case), that gives ``source='tui'``, which triggers the TUI platform hint in
   ``agent/prompt_builder.py`` and causes the model to refuse MEDIA:/ delivery
   inside the Desktop app.

Bug 3 — ``_resume_materialize_minted()`` omits ``user_id``
   When an empty Desktop draft tab (a server-minted session key with no DB row
   yet) is resumed after a backend restart, ``create_session`` is called without
   ``user_id``. Because ``create_session`` is an ``INSERT OR IGNORE`` upsert, the
   row is born with ``user_id=NULL`` and no later write can fill it in — the
   session is permanently anonymous in the Desktop sidebar and ``@session:``
   deep-link routing.

Both are complementary to #112910 (which covers the live-reuse / session.activate
paths). These tests pin the cold-mount / materialize-minted paths that #112910
does not reach.
"""

from __future__ import annotations

import threading

import pytest

from gateway.owner_identity import host_owner_subject
from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.transport import bind_transport, reset_transport

# Shape that _new_session_key() produces (the materialization gate checks this).
MINTED_KEY = "20261112_060000_ab1234"
# A session already in the DB with source='desktop'.
STORED_DESKTOP_KEY = "20261112_070000_cd5678"


class _DesktopSocket:
    """Minimal WS peer whose auth_identity satisfies is_authenticated_identity."""

    def __init__(self):
        # host_owner_subject() is what a real WS-upgrade login records.
        self.auth_identity = {"provider": "basic", "user_id": "alice"}

    def write(self, frame):
        return True


@pytest.fixture()
def real_db(monkeypatch, tmp_path):
    """Real SessionDB; gateway-level side effects patched out."""
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_resolve_model", lambda: "test-model")
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda _key, _home=None: None)
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda *a, **k: None)
    monkeypatch.setattr(server, "_maybe_schedule_auto_continue", lambda *a, **k: None)
    monkeypatch.setattr(server, "_default_session_cwd", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(server, "_profile_configured_cwd", lambda _home: str(tmp_path))
    monkeypatch.setattr(server, "_child_run_active", lambda _key, _home=None: False)
    known = set(server._sessions)
    yield db
    with server._sessions_lock:
        for sid in [s for s in server._sessions if s not in known]:
            server._sessions.pop(sid, None)
    db.close()


@pytest.fixture()
def remote_env(monkeypatch):
    """Simulate a remote Linux backend: HERMES_DESKTOP unset → env gives 'tui'."""
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    monkeypatch.delenv("HERMES_DESKTOP_TERMINAL", raising=False)


def _resume(**params):
    return server.handle_request({"id": "1", "method": "session.resume", "params": params})


def _assert_resumed_ok(resp, target):
    assert "error" not in resp, resp
    assert resp["result"]["resumed"] == target
    sid = resp["result"]["session_id"]
    assert sid, "a live runtime must be registered"
    return sid


# ---------------------------------------------------------------------------
# Bug 2: mint() must carry the stored source, not re-resolve from env
# ---------------------------------------------------------------------------

def test_mint_carries_stored_desktop_source_when_params_omit_source(real_db, remote_env):
    """RED on main: mint() calls _new_runtime_ids(self.params) which ignores self.found['source'].

    When a Desktop client's reconnect path omits source from session.resume,
    _new_runtime_ids falls back to env-resolution → 'tui' on a remote backend.
    After the fix, mint() must check self.found['source'] first.
    """
    # Persist the session as a Desktop session (the first prompt would have done this).
    real_db.create_session(STORED_DESKTOP_KEY, source="desktop", model="test-model")

    # Resume without source (what internal Desktop reconnect calls do).
    resp = _resume(session_id=STORED_DESKTOP_KEY)
    sid = _assert_resumed_ok(resp, STORED_DESKTOP_KEY)

    record = server._sessions.get(sid, {})
    assert record.get("source") == "desktop", (
        f"mint() must carry stored 'desktop' source; got {record.get('source')!r}. "
        "On a remote backend without HERMES_DESKTOP, env-resolution gives 'tui', "
        "which triggers the TUI platform hint and breaks MEDIA:/ delivery (#154216 bug 2)."
    )


def test_mint_respects_explicit_source_override(real_db, remote_env):
    """When params DO include source, that value wins (not stored row). Sanity fence."""
    real_db.create_session(STORED_DESKTOP_KEY, source="desktop", model="test-model")

    resp = _resume(session_id=STORED_DESKTOP_KEY, source="tui")
    sid = _assert_resumed_ok(resp, STORED_DESKTOP_KEY)

    # Explicit override must still work (this path was already correct on main).
    record = server._sessions.get(sid, {})
    assert record.get("source") == "tui"


# ---------------------------------------------------------------------------
# Bug 3: materialize_minted() must stamp user_id from the live transport
# ---------------------------------------------------------------------------

def test_materialize_minted_stamps_user_id_from_transport(real_db, remote_env):
    """RED on main: create_session() in _resume_materialize_minted omits user_id.

    After the fix, the materialized row must carry the same user_id that
    bind_transport provides — the same contract session.create satisfies at
    _ensure_session_db_row time, now also applied for the backend-restart path.
    """
    socket = _DesktopSocket()
    token = bind_transport(socket)
    try:
        resp = _resume(session_id=MINTED_KEY)
    finally:
        reset_transport(token)

    _assert_resumed_ok(resp, MINTED_KEY)
    row = real_db.get_session(MINTED_KEY)
    assert row is not None, "resume must materialize the DB row"

    expected_uid = host_owner_subject()
    assert row.get("user_id") == expected_uid, (
        f"materialized row must carry user_id={expected_uid!r}; "
        f"got {row.get('user_id')!r}. Without this, the session is permanently "
        "anonymous in the Desktop sidebar (#154216 bug 3)."
    )


def test_materialize_minted_anonymous_transport_leaves_user_id_null(real_db, remote_env):
    """An unauthenticated (stdio/PTY) transport must leave user_id as NULL — no guess."""
    # No bind_transport call → current_transport() returns None → user_id=None.
    resp = _resume(session_id=MINTED_KEY)
    _assert_resumed_ok(resp, MINTED_KEY)

    row = real_db.get_session(MINTED_KEY)
    assert row is not None
    assert row.get("user_id") is None, (
        "anonymous transport must not stamp a user_id on a materialized row"
    )
