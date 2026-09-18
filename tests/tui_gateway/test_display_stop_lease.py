"""display.stop releases the human-exclusion lease only after the runtime confirms the stop."""
from __future__ import annotations


def _stop():
    import tui_gateway.server as server
    return server.handle_request({'jsonrpc': '2.0', 'id': 1, 'method': 'display.stop', 'params': {}})


def test_display_stop_keeps_lease_when_runtime_stop_raises(monkeypatch):
    from tools.bot_desktop import lease, runtime
    lease._reset_for_tests()
    lease.acquire('disconnected-viewer')
    def _boom():
        raise PermissionError('killpg denied')
    monkeypatch.setattr(runtime, 'stop', _boom)
    resp = _stop()
    assert 'error' in resp, resp
    assert lease.get().holder == 'human', lease.get()
    lease._reset_for_tests()


def test_display_stop_releases_lease_when_already_exited(monkeypatch):
    from tools.bot_desktop import lease, runtime
    lease._reset_for_tests()
    lease.acquire('disconnected-viewer')
    monkeypatch.setattr(runtime, 'stop', lambda: False)
    resp = _stop()
    assert 'result' in resp, resp
    assert lease.get().holder == lease.AGENT, lease.get()
    lease._reset_for_tests()
