"""Regression tests for #150103: session.create must not run inline on the WS reader.

Without the fix, a slow session.create (full AIAgent build, memory-provider init,
network round-trips) blocks the WS read loop for the duration of the call. Under load
this freezes the Desktop app — observed 3 min 47 s in the field (#150103).

The fix is a one-line addition: include "session.create" in _LONG_HANDLERS so
dispatch() returns None immediately (pool submission) instead of blocking.

_LONG_HANDLERS is the control surface; dispatch() reads it at call time, so
testing membership is an exact proxy for the dispatch path taken.
"""

from __future__ import annotations

import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

_original_stdout = sys.stdout


@pytest.fixture(autouse=True)
def _restore_stdout():
    yield
    sys.stdout = _original_stdout


@pytest.fixture()
def server():
    with patch.dict("sys.modules", {
        "hermes_constants": MagicMock(get_hermes_home=MagicMock(return_value="/tmp/hermes_test_150103")),
        "hermes_cli.env_loader": MagicMock(),
        "hermes_cli.banner": MagicMock(),
        "hermes_state": MagicMock(),
    }):
        import importlib
        mod = importlib.import_module("tui_gateway.server")

    methods = dict(mod._methods)
    real_stdout = mod._real_stdout
    yield mod
    mod._methods.clear()
    mod._methods.update(methods)
    mod._real_stdout = real_stdout
    mod._sessions.clear()
    __import__("tui_gateway.server_requests", fromlist=["x"]).reset_for_tests()


def test_session_create_in_long_handlers(server):
    """session.create must be in _LONG_HANDLERS.

    RED on unpatched main: 'session.create' not in _LONG_HANDLERS.
    GREEN after fix: assertion passes.
    """
    assert "session.create" in server._LONG_HANDLERS, (
        "session.create is not in _LONG_HANDLERS — it runs inline on the WS reader. "
        "A slow agent build (~3 min 47 s under load) blocks all queued RPCs, "
        "freezing the Desktop app. Fix: add 'session.create' to _LONG_HANDLERS "
        "in tui_gateway/server.py (#150103)."
    )


def test_session_create_dispatches_to_pool_not_inline(server):
    """dispatch() must return None for session.create (pool path), not block inline.

    Mirrors test_dispatch_inline_rpc_does_not_block_under_gil_pressure from
    test_inline_rpc_gil_starvation.py: a slow session.create must not prevent a
    concurrent fast RPC from completing on the reader.
    """
    released = threading.Event()

    def slow_create(rid, params):
        released.wait(timeout=5)
        return server._ok(rid, {"session_id": "s-test", "stored_session_id": "k", "messages": []})

    server._methods["session.create"] = slow_create
    server._methods["fast.check"] = lambda rid, params: server._ok(rid, {"ok": True})

    # session.create in _LONG_HANDLERS → dispatch returns None immediately (pool submission)
    result = server.dispatch({"id": "c1", "method": "session.create", "params": {}})
    assert result is None, (
        f"dispatch() returned {result!r} instead of None for session.create — "
        "it ran inline, blocking the WS reader (#150103)."
    )

    # A fast RPC after a dispatched session.create must not wait for it
    t0 = time.monotonic()
    fast_resp = server.dispatch({"id": "fast", "method": "fast.check", "params": {}})
    elapsed = time.monotonic() - t0

    assert fast_resp["result"] == {"ok": True}
    assert elapsed < 2.0, (
        f"fast.check blocked for {elapsed:.2f}s behind session.create inline run — "
        f"the WS read loop would stall, freezing Desktop (#150103)."
    )

    released.set()
