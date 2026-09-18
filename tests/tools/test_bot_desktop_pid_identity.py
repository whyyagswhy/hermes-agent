"""screen stop must never signal a stranger: the recorded launcher pid is verified by process identity (start time, session, command line) before any signal."""

import json
import os
import subprocess
import threading

from tools.bot_desktop import runtime


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, 'state_dir', lambda: tmp_path)
    return tmp_path


def test_symlinked_pid_file_is_rejected(monkeypatch, tmp_path):
    sd = _isolate(monkeypatch, tmp_path)
    (sd / 'real.pid').write_text(str(os.getpid()))
    (sd / 'launcher.pid').symlink_to(sd / 'real.pid')
    assert runtime._launcher_pid() is None


def test_non_regular_pid_file_is_rejected(monkeypatch, tmp_path):
    sd = _isolate(monkeypatch, tmp_path)
    (sd / 'launcher.pid').mkdir()
    assert runtime._launcher_pid() is None


def test_live_pid_without_identity_is_stale(monkeypatch, tmp_path):
    sd = _isolate(monkeypatch, tmp_path)
    (sd / 'launcher.pid').write_text(str(os.getpid()))
    assert runtime._launcher_pid() is None


def _record(tmp_path, pid, create_time, sid, cmdline):
    import json as _json
    rec = {'pid': pid, 'create_time': create_time, 'sid': sid, 'cmdline': cmdline}
    (tmp_path / 'launcher.pid').write_text(str(pid))
    (tmp_path / 'launcher.identity.json').write_text(_json.dumps(rec))


def test_live_reused_pid_with_wrong_start_time_is_stale(monkeypatch, tmp_path):
    sd = _isolate(monkeypatch, tmp_path)
    me = os.getpid()
    sig = runtime._proc_signature(me)
    assert sig is not None
    _record(sd, me, sig['create_time'] + 100.0, sig['sid'], sig['cmdline'])
    assert runtime._launcher_pid() is None


def _forbidden_kill(*args, **kwargs):
    raise AssertionError('killpg must not fire for a stale pid')


def test_stop_never_signals_a_reused_pid(monkeypatch, tmp_path):
    sd = _isolate(monkeypatch, tmp_path)
    me = os.getpid()
    sig = runtime._proc_signature(me)
    assert sig is not None
    _record(sd, me, sig['create_time'] + 100.0, sig['sid'], sig['cmdline'])
    monkeypatch.setattr(os, 'killpg', _forbidden_kill)
    assert runtime.stop() is False
    assert not (sd / 'launcher.pid').exists()
    assert not (sd / 'launcher.identity.json').exists()


def test_matching_launcher_is_recognized_and_stopped(monkeypatch, tmp_path):
    sd = _isolate(monkeypatch, tmp_path)
    proc = subprocess.Popen(['sleep', '120'], start_new_session=True)
    reaper = threading.Thread(target=proc.wait, kwargs={'timeout': 30}, daemon=True)
    reaper.start()
    try:
        runtime._record_launcher_identity(proc.pid)
        assert runtime._launcher_pid() == proc.pid
        assert runtime.stop() is True
        proc.wait(timeout=10)
        assert proc.poll() is not None
        assert not (sd / 'launcher.pid').exists()
        assert not (sd / 'launcher.identity.json').exists()
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
