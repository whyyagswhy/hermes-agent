"""Windows turn-lock for bot delivery (tools/bot_relay.py). Regression for #133153.

On native Windows there is no ``fcntl``, so ``acquire_turn_lock`` used to be a
no-op there — and two deliveries into one profile could run their recipient
turns concurrently. The lock now has a real Windows implementation
(``msvcrt.locking`` on the same lockfile, never deleted: a dead holder's
region lock is released by the kernel, so acquisition alone reaps it).

The ``windows``-marked tests below exercise real file locks across real
processes on native Windows. The unmarked tests force the ``msvcrt`` branch on
any host (``fcntl`` hidden, ``msvcrt`` shimmed over real OS locks) so the same
contract — contention raises ``TurnBusyError``, a dead owner releases without
the file being deleted — is proven on every platform.
"""

import concurrent.futures
import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

import tools.bot_relay as bot_relay
from tools.bot_relay import TurnBusyError, acquire_turn_lock

WINDOWS_HOLDER = """
import sys
from tools.bot_relay import acquire_turn_lock
with acquire_turn_lock(sys.argv[1], 'recipient', timeout_seconds=1):
    print('locked', flush=True)
    sys.stdin.readline()
"""


@pytest.fixture
def holder(tmp_path):
    """A live lock holder in a real subprocess (native Windows locks)."""
    proc = subprocess.Popen([sys.executable, "-c", WINDOWS_HOLDER, str(tmp_path)],
                            cwd=Path(__file__).resolve().parents[2], env=os.environ.copy(),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    # Bound startup without timing assertions; a dead/hung helper cannot hang pytest.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        assert pool.submit(proc.stdout.readline).result(timeout=30).strip() == "locked"
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)
        pool.shutdown(wait=True)


@pytest.mark.platforms("windows")
def test_same_profile_waits_but_other_profile_runs(tmp_path, holder):
    with pytest.raises(TurnBusyError):
        with acquire_turn_lock(tmp_path, "recipient", timeout_seconds=0.15):
            pytest.fail("two processes acquired the same recipient")
    with acquire_turn_lock(tmp_path, "different-recipient", timeout_seconds=0):
        pass


@pytest.mark.platforms("windows")
def test_dead_owner_releases_lock_without_deleting_file(tmp_path, holder):
    holder.kill()
    holder.wait(timeout=10)
    with acquire_turn_lock(tmp_path, "recipient", timeout_seconds=1) as path:
        assert path.is_file()


# Portable forcing of the Windows (``msvcrt``) branch: hide ``fcntl`` so the
# backend probe falls through, and shim ``msvcrt.locking`` over real OS-level
# locks. Subprocesses get real cross-process file locks; the branch, retry
# loop, ``TurnBusyError`` and release semantics under test are the real ones.

_FORCED_MSVCRT_PRELUDE = """
import fcntl as _real_fcntl
import sys as _sys
import types as _types
_shim = _types.ModuleType("msvcrt")
_shim.LK_NBLCK = 1
_shim.LK_UNLCK = 0
def _locking(fd, mode, nbytes):
    if mode == 1:
        _real_fcntl.flock(fd, _real_fcntl.LOCK_EX | _real_fcntl.LOCK_NB)
    else:
        _real_fcntl.flock(fd, _real_fcntl.LOCK_UN)
_shim.locking = _locking
_sys.modules["msvcrt"] = _shim
_sys.modules["fcntl"] = None
"""

_FORCED_HOLDER = (
    "import sys\n"
    "sys.path.insert(0, %r)\n"
    "import tools.bot_relay  # import while fcntl is still real\n"
    + _FORCED_MSVCRT_PRELUDE
    + """
from tools.bot_relay import acquire_turn_lock
with acquire_turn_lock(sys.argv[1], sys.argv[2], timeout_seconds=float(sys.argv[3])):
    print("locked", flush=True)
    sys.stdin.readline()
"""
)


def _start_forced_holder(tmp_path, profile="recipient", timeout="30"):
    repo = str(Path(__file__).resolve().parents[2])
    driver = tmp_path / "forced_msvcrt_holder.py"
    driver.write_text(_FORCED_HOLDER % repo, encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(driver), str(tmp_path), profile, timeout],
                            cwd=repo, env=os.environ.copy(),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        assert pool.submit(proc.stdout.readline).result(timeout=30).strip() == "locked"
    except Exception:
        err = proc.stderr.read() if proc.poll() is not None else ""
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)
        raise AssertionError(f"forced-msvcrt holder never locked: {err!r}")
    return proc, pool


def test_windows_branch_serializes_across_processes(tmp_path):
    """The ``msvcrt`` branch holds off a second process on the same profile.

    The holder runs the Windows branch (forced) in a real subprocess with real
    file locks; this process contends through the native backend. On the
    pre-fix no-op both sides acquire, so the ``TurnBusyError`` never fires.
    """
    proc, pool = _start_forced_holder(tmp_path)
    try:
        with pytest.raises(TurnBusyError):
            with acquire_turn_lock(tmp_path, "recipient", timeout_seconds=0.15):
                pytest.fail("two processes acquired the same recipient")
        with acquire_turn_lock(tmp_path, "different-recipient", timeout_seconds=0):
            pass
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)
        pool.shutdown(wait=True)


def test_windows_branch_dead_owner_releases_without_deleting_file(tmp_path):
    proc, pool = _start_forced_holder(tmp_path)
    pool.shutdown(wait=True)
    proc.kill()
    proc.wait(timeout=10)
    with acquire_turn_lock(tmp_path, "recipient", timeout_seconds=5) as path:
        assert path.is_file()


class _ContendedMsvcrt:
    """Faithful ``msvcrt`` stand-in: mandatory per-file locks, ``OSError`` on contention."""

    LK_NBLCK = 1
    LK_UNLCK = 0

    def __init__(self):
        self.held = set()

    @staticmethod
    def _key(fd):
        st = os.fstat(fd)
        return (st.st_dev, st.st_ino)

    def locking(self, fd, mode, nbytes):
        key = self._key(fd)
        if mode == self.LK_NBLCK:
            if key in self.held:
                raise OSError(errno.EACCES, "Lock violation")
            self.held.add(key)
        else:
            self.held.discard(key)


def test_windows_branch_contended_lock_raises_busy(tmp_path, monkeypatch):
    """Two fds on one lockfile through the ``msvcrt`` branch: the second waits
    out its budget, then fails with a typed ``target_busy`` refusal — and the
    lock is usable again once the first holder leaves."""
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setitem(sys.modules, "msvcrt", _ContendedMsvcrt())
    with bot_relay.acquire_turn_lock(tmp_path, "recipient", timeout_seconds=0):
        with pytest.raises(TurnBusyError) as excinfo:
            with bot_relay.acquire_turn_lock(tmp_path, "recipient", timeout_seconds=0):
                pytest.fail("re-acquired a held Windows turn lock")
    assert excinfo.value.reason == "target_busy"
    assert excinfo.value.profile == "recipient"
    with bot_relay.acquire_turn_lock(tmp_path, "recipient", timeout_seconds=0):
        pass
