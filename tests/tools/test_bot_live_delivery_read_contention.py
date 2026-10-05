"""Exact-id mailbox reads ride out a transient sharing violation. Regression for #133153.

Tickets are read without the dir lock while other processes atomically replace
them. On Windows such a read can fail with a sharing violation (winerror 32),
which aborted receipt recovery and could fail a sender's ``await_delivery``
wait mid-turn. A read colliding with a replace succeeds once the replace
lands; a hold that outlasts the bounded retry still fails closed.

The ``windows``-marked test holds the ticket with a real no-share handle. The
unmarked tests prove the same retry contract on every host with transient vs.
persistent ``PermissionError`` faults.
"""

import ctypes
import json
import threading
import time
from pathlib import Path

import pytest

from tools import bot_live_delivery as bld

DELIVERY_ID = "ab" * 32


def _hold_without_sharing(path, seconds, ready):
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)  # GENERIC_READ, share none
    if handle in (None, ctypes.c_void_p(-1).value):
        return  # ``ready`` stays unset; the test fails on its wait instead of hanging.
    try:
        ready.set()
        time.sleep(seconds)
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def _ticket(tmp_path):
    root = tmp_path / "runtime" / "bot_live_delivery"
    root.mkdir(parents=True)
    record = {"delivery_id": DELIVERY_ID, "status": "settled", "reply": "DONE"}
    (root / f"{DELIVERY_ID}.json").write_text(json.dumps(record), encoding="utf-8")
    return root / f"{DELIVERY_ID}.json", record


@pytest.mark.platforms("windows")
@pytest.mark.parametrize("hold_s, readable", [(0.3, True), (2.0, False)])
def test_read_delivery_result_rides_out_a_transient_sharing_violation(tmp_path, hold_s, readable):
    path, record = _ticket(tmp_path)
    ready = threading.Event()
    holder = threading.Thread(target=_hold_without_sharing, args=(path, hold_s, ready), daemon=True)
    holder.start()
    try:
        assert ready.wait(10), "could not open the ticket without sharing"
        if readable:
            assert bld.read_delivery_result(tmp_path, DELIVERY_ID) == record
        else:
            with pytest.raises(PermissionError):
                bld.read_delivery_result(tmp_path, DELIVERY_ID)
    finally:
        holder.join(10)


def _flaky_read_text(path, failures, exc):
    real_read_text = Path.read_text

    def flaky(self, *args, **kwargs):
        if self == path and failures["left"] > 0:
            failures["left"] -= 1
            raise exc
        return real_read_text(self, *args, **kwargs)

    return flaky


def test_exact_read_retries_a_transient_permission_error(tmp_path, monkeypatch):
    """A replace-sized burst of ``PermissionError`` faults is ridden out: the
    read succeeds once the replacement lands. Without the retry the first
    fault aborts the caller."""
    path, record = _ticket(tmp_path)
    monkeypatch.setattr(Path, "read_text",
                        _flaky_read_text(path, {"left": 3}, PermissionError(13, "Permission denied")))
    assert bld.read_delivery_result(tmp_path, DELIVERY_ID) == record


def test_exact_read_fails_closed_after_the_retry_budget(tmp_path, monkeypatch):
    """A hold that outlasts the bounded retry still raises ``PermissionError``
    instead of licensing an overwrite of a possibly-live receipt — and it
    raises on a bounded budget, not after a whole stuck wait."""
    path, _record = _ticket(tmp_path)
    monkeypatch.setattr(Path, "read_text",
                        _flaky_read_text(path, {"left": 10 ** 6}, PermissionError(13, "Permission denied")))
    start = time.monotonic()
    with pytest.raises(PermissionError):
        bld.read_delivery_result(tmp_path, DELIVERY_ID)
    assert time.monotonic() - start < 2.0
