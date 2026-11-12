"""#154418: _kill_process_group must sweep subprocess children spawned by user code.

RED case: a child the kernel spawned ignores SIGTERM (simulating a race where the child
was forking when killpg(SIGTERM) ran, or chose to ignore it). After the kernel leader
exits on SIGTERM, the current code does nothing more -- the child is orphaned with PPID 1.

GREEN case: after the fix, killpg(pgid, SIGKILL) is sent after the leader exits so
any remaining group members are force-killed.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time

import pytest


_SIGTERM_IGNORE_CHILD = (
    "import signal, time, sys, os;"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
    "open(sys.argv[1], 'w').write(str(os.getpid()));"
    "time.sleep(60)"
)

_KERNEL_TEMPLATE = (
    "import subprocess, signal, time, sys, os;"
    "g = subprocess.Popen([sys.executable, '-c', {child!r}, sys.argv[1]]);"
    "[time.sleep(0.05) for _ in range(100) if not (os.path.exists(sys.argv[1]) and os.path.getsize(sys.argv[1]) > 0)];"
    "time.sleep(60)"
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only process-group semantics")
def test_kill_process_group_sweeps_sigterm_ignoring_child():
    """_kill_process_group(escalate=True) must SIGKILL group stragglers.

    Scenario: kernel spawns a child that ignores SIGTERM.  On SIGTERM the kernel
    exits; the child is reparented to PID 1 but stays alive.  _kill_process_group
    must still kill it.  RED on the unpatched code; GREEN on the fix.
    """
    from tools.code_execution_tool import _kill_process_group

    with tempfile.NamedTemporaryFile(suffix=".pid", delete=False) as fh:
        pid_file = fh.name

    kernel_code = _KERNEL_TEMPLATE.format(child=_SIGTERM_IGNORE_CHILD)
    kernel = subprocess.Popen(
        [sys.executable, "-c", kernel_code, pid_file],
        start_new_session=True,
    )

    deadline = time.monotonic() + 5.0
    grandchild_pid: int | None = None
    while time.monotonic() < deadline:
        try:
            text = open(pid_file).read().strip()
            if text:
                grandchild_pid = int(text)
                break
        except (OSError, ValueError):
            pass
        time.sleep(0.05)

    assert grandchild_pid is not None, "grandchild did not start in time"

    try:
        os.kill(grandchild_pid, 0)
    except ProcessLookupError:
        pytest.fail("grandchild already dead before _kill_process_group was called")

    _kill_process_group(kernel, escalate=True)

    time.sleep(0.5)

    alive = True
    try:
        os.kill(grandchild_pid, 0)
    except ProcessLookupError:
        alive = False

    assert not alive, (
        f"grandchild PID {grandchild_pid} is still alive after _kill_process_group -- "
        "SIGTERM was ignored and no SIGKILL swept the process group"
    )
    try:
        os.unlink(pid_file)
    except OSError:
        pass
