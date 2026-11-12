"""gateway/run_notifications.py: _completion_delivery_ready must call restore_completion_route
before adapter lookup for LOCAL-platform DM sessions (#154230).

For a gateway DM (agent:<profile>:local:dm:<id>), the LOCAL adapter is registered lazily by
``restore_local_session`` inside ``restore_completion_route`` — which ``_inject_watch_notification``
already calls. But ``_completion_delivery_ready`` runs FIRST (as a pre-flight check that must pass
before a durable claim is made), and it does NOT call ``restore_completion_route``. Without the
local adapter in the profile adapter map, ``_resolve_injection_adapter("local", source)`` returns
None → adapter is None → ``_completion_delivery_ready`` returns False → no claim → delivery_attempts
stays 0 → the row re-queues indefinitely (#154230).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def local_dm_runner(tmp_path, monkeypatch):
    from agent import secret_scope
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.pairing import PairingStore
    from gateway.run import GatewayRunner

    home = tmp_path / "hh"
    for name in ("researcher",):
        (home / "profiles" / name).mkdir(parents=True)
    (home / ".env").write_text("")
    (home / "profiles" / "researcher" / ".env").write_text("")
    monkeypatch.setenv("HERMES_HOME", str(home))
    prev = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner.config.platforms = {Platform.TELEGRAM: PlatformConfig(enabled=True, extra={})}
    runner.config.profile_routes = []
    runner.pairing_store = PairingStore(profile="default")
    runner.pairing_stores = {}
    runner.session_store = None
    runner._session_sources = {}
    runner.adapters = {}

    yield runner

    secret_scope.set_multiplex_active(prev)
    monkeypatch.undo()


def test_local_dm_completion_delivery_ready_calls_restore_route_before_adapter_lookup(local_dm_runner):
    """Without the fix: _completion_delivery_ready never calls restore_completion_route for a
    LOCAL DM event. The LOCAL adapter is therefore absent, adapter is None, and the method
    returns False — delivery_attempts stays 0 (#154230).

    With the fix: restore_completion_route is called in _completion_delivery_ready when the
    adapter is None for Platform.LOCAL, the LOCAL adapter is registered via the restore, the
    retry finds it, and the method returns True (delivery ready).
    """
    from gateway.config import Platform
    from gateway.platforms.base import BasePlatformAdapter

    runner = local_dm_runner
    evt = {
        "type": "async_delegation",
        "session_key": "agent:researcher:local:dm:local-e31d",
        "parent_session_id": "local-e31d",
        "delegation_id": "deleg-test-001",
    }

    restore_calls = []
    mock_local_adapter = MagicMock(spec=BasePlatformAdapter)
    # supports_async_delivery True means adapter_supports_push returns True → delivery ready.
    mock_local_adapter.supports_async_delivery = True

    def fake_restore(runner_arg, session_key_arg):
        restore_calls.append(session_key_arg)
        # Simulate what restore_local_session does: register the LOCAL adapter for researcher.
        profile_adapters = getattr(runner_arg, '_profile_adapters', None)
        if profile_adapters is None:
            runner_arg._profile_adapters = {}
        runner_arg._profile_adapters.setdefault('researcher', {})[Platform.LOCAL] = mock_local_adapter

    async def _run():
        with patch('gateway.session_local_recovery.restore_completion_route',
                   side_effect=fake_restore):
            with patch.object(runner, '_classify_completion_target',
                              new=AsyncMock(return_value='deliver')):
                return await runner._completion_delivery_ready(evt)

    result = asyncio.run(_run())

    assert 'agent:researcher:local:dm:local-e31d' in restore_calls, (
        f"_completion_delivery_ready must call restore_completion_route with the LOCAL "
        f"DM session key before aborting on a missing adapter. Got: {restore_calls!r}. "
        f"Without fix: adapter is None immediately → returns False → delivery_attempts "
        f"stays 0 (#154230)"
    )
    assert result is True, (
        f"After restore registers the LOCAL adapter, _completion_delivery_ready must "
        f"return True (delivery ready). Got: {result!r}"
    )


def test_non_local_platform_does_not_call_restore_route(local_dm_runner):
    """Non-LOCAL platforms (Telegram etc.) must not trigger the new restore path."""
    runner = local_dm_runner
    evt = {
        "type": "async_delegation",
        "session_key": "agent:researcher:telegram:dm:12345",
        "parent_session_id": "sess-tg",
        "delegation_id": "deleg-tg-001",
    }

    async def _run():
        with patch('gateway.session_local_recovery.restore_completion_route') as mock_rcr:
            with patch.object(runner, '_classify_completion_target',
                              new=AsyncMock(return_value='deliver')):
                await runner._completion_delivery_ready(evt)
            return mock_rcr.call_count

    call_count = asyncio.run(_run())
    assert call_count == 0, (
        f"restore_completion_route must NOT be called for non-LOCAL platforms; "
        f"got {call_count} call(s)"
    )
