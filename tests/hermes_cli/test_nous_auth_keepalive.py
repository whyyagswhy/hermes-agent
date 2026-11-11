import logging
from datetime import UTC, datetime, timedelta

from hermes_cli import nous_auth_keepalive as keepalive

# Both lifetimes have been observed on real installs.
OBSERVED_LIFETIMES_SECONDS = (3594, 899)


def test_refresh_always_fires_before_expiry_for_observed_lifetimes():
    """Simulate the tick schedule and assert no credential expires unrefreshed.

    This is the property that actually matters: for every lifetime, some tick
    must decide to refresh while the credential is still valid. Ticking faster
    alone does not guarantee it -- the refresh horizon has to cover the gap
    between ticks too.
    """
    for lifetime in OBSERVED_LIFETIMES_SECONDS:
        tick = keepalive._tick_seconds(
            keepalive.NOUS_AUTH_KEEPALIVE_INTERVAL_SECONDS, lifetime
        )
        horizon = keepalive._refresh_horizon_seconds(
            tick, keepalive.NOUS_INVOKE_JWT_MIN_TTL_SECONDS
        )

        # Walk the ticks and find the first one that refreshes.
        refreshed_at = None
        elapsed = 0
        while elapsed <= lifetime:
            if lifetime - elapsed <= horizon:
                refreshed_at = elapsed
                break
            elapsed += tick

        assert refreshed_at is not None, f"never refreshed for lifetime={lifetime}"
        assert refreshed_at < lifetime, (
            f"refresh at {refreshed_at}s came at/after expiry {lifetime}s "
            f"(tick={tick}, horizon={horizon})"
        )


def test_interval_precedence_and_disable(monkeypatch):
    def _config(section):
        monkeypatch.setattr(keepalive, "_nous_config", lambda: section)

    # An absent section leaves the module default in place.
    _config({})
    assert (
        keepalive._interval_seconds(None)
        == keepalive.NOUS_AUTH_KEEPALIVE_INTERVAL_SECONDS
    )

    _config({keepalive.NOUS_AUTH_KEEPALIVE_INTERVAL_CONFIG_KEY: 600})
    assert keepalive._interval_seconds(None) == 600
    # An explicit argument still outranks config.yaml.
    assert keepalive._interval_seconds(300) == 300

    # A malformed value falls back to the default rather than disabling.
    _config({keepalive.NOUS_AUTH_KEEPALIVE_INTERVAL_CONFIG_KEY: "not-a-number"})
    assert (
        keepalive._interval_seconds(None)
        == keepalive.NOUS_AUTH_KEEPALIVE_INTERVAL_SECONDS
    )

    # Zero remains the documented way to turn the keepalive off.
    _config({keepalive.NOUS_AUTH_KEEPALIVE_INTERVAL_CONFIG_KEY: 0})
    assert keepalive._interval_seconds(None) == 0
    assert keepalive.start_nous_auth_keepalive() is None


def test_keepalive_refreshes_stale_pool_entry(monkeypatch):
    class _Entry:
        access_token = "pooled-access-token"
        expires_at = "2000-01-01T00:00:00+00:00"
        agent_key = ""
        agent_key_expires_at = None
        scope = "inference:invoke"

    class _Pool:
        refreshed = False

        def has_credentials(self):
            return True

        def select(self):
            return _Entry()

        def try_refresh_current(self):
            self.refreshed = True
            return _Entry()

    pool = _Pool()
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pool)

    assert keepalive.refresh_nous_auth_keepalive_once() is True
    assert pool.refreshed is True


def test_keepalive_falls_back_to_singleton_state(monkeypatch):
    calls = []

    class _Pool:
        def has_credentials(self):
            return False

    def _resolve_nous_runtime_credentials(**kwargs):
        calls.append(kwargs)
        return {
            "provider": "nous",
            "api_key": "fresh-agent-key",
            "base_url": "https://inference-api.nousresearch.com/v1",
        }

    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: _Pool())
    monkeypatch.setattr(
        keepalive,
        "get_provider_auth_state",
        lambda provider: {"access_token": "stored-access-token"},
    )
    monkeypatch.setattr(
        keepalive,
        "resolve_nous_runtime_credentials",
        _resolve_nous_runtime_credentials,
    )

    assert keepalive.refresh_nous_auth_keepalive_once(timeout_seconds=15.0) is True
    assert calls == [{"timeout_seconds": 15.0}]


def test_keepalive_binds_launch_scope_for_multiplexed_singleton_refresh(tmp_path, monkeypatch, caplog):
    """The daemon tick must not lose the launch profile's routing secrets."""
    from agent.secret_scope import is_multiplex_active, set_multiplex_active
    from hermes_cli.auth_nous import _nous_inference_env_override, _nous_portal_env_override

    launch_home = tmp_path / ".hermes"
    launch_home.mkdir()
    (launch_home / ".env").write_text(
        "NOUS_INFERENCE_BASE_URL=https://inference.example/v1\n"
        "HERMES_PORTAL_BASE_URL=https://portal.example\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: None)
    monkeypatch.setattr(
        keepalive, "get_provider_auth_state", lambda provider: {"access_token": "stored-token"}
    )
    seen = []

    def _resolve(**kwargs):
        seen.append((_nous_inference_env_override(), _nous_portal_env_override(), kwargs))
        return {"provider": "nous"}

    monkeypatch.setattr(keepalive, "resolve_nous_runtime_credentials", _resolve)
    previous_multiplex = is_multiplex_active()
    set_multiplex_active(True)
    try:
        with caplog.at_level(logging.WARNING):
            assert keepalive.refresh_nous_auth_keepalive_once(timeout_seconds=15.0) is True
    finally:
        set_multiplex_active(previous_multiplex)

    assert seen == [(
        "https://inference.example/v1", "https://portal.example", {"timeout_seconds": 15.0}
    )]
    assert not [record for record in caplog.records if "no profile secret scope" in record.getMessage()]


def test_keepalive_binds_launch_scope_for_multiplexed_pool_refresh(tmp_path, monkeypatch):
    """Pool refreshes run under the same launch scope as singleton refreshes."""
    from agent.secret_scope import current_secret_scope, is_multiplex_active, set_multiplex_active

    launch_home = tmp_path / ".hermes"
    launch_home.mkdir()
    (launch_home / ".env").write_text(
        "NOUS_INFERENCE_BASE_URL=https://inference.example/v1\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    seen = []

    class _Entry:
        access_token = "pooled-access-token"
        expires_at = "2000-01-01T00:00:00+00:00"
        agent_key = ""
        agent_key_expires_at = None
        scope = "inference:invoke"

    class _Pool:
        def has_credentials(self):
            return True

        def select(self):
            seen.append((current_secret_scope() or {}).get("NOUS_INFERENCE_BASE_URL"))
            return _Entry()

        def try_refresh_current(self):
            return _Entry()

    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: _Pool())
    previous_multiplex = is_multiplex_active()
    set_multiplex_active(True)
    try:
        assert keepalive.refresh_nous_auth_keepalive_once() is True
    finally:
        set_multiplex_active(previous_multiplex)

    assert seen == ["https://inference.example/v1"]


class _FakeGatewayServer:
    """Stand-in for the imported ``tui_gateway.server`` module the announcement probes.

    The class attributes satisfy tests/conftest.py's autouse teardown, which probes
    whatever currently sits at ``sys.modules["tui_gateway.server"]`` and snapshots/resets
    these globals on it.
    """

    _sessions = {}
    _methods = {}
    _cfg_cache = None
    _cfg_sig = None
    _cfg_path = None
    _db = None
    _db_error = None
    _real_stdout = None

    def __init__(self, transports):
        self._live_transports = transports
        self.events = []

    def _broadcast_global_event(self, event, payload=None):
        self.events.append((event, payload))


def _singleton_relogin_state(monkeypatch, outcomes):
    """Pool-less singleton state whose refresh raises AuthError until ``outcomes`` is exhausted;
    every call after that succeeds. ``outcomes`` entries: True = fail with relogin_required.
    """
    from hermes_cli.auth_constants import AuthError

    pending = list(outcomes)

    def _resolve(**kwargs):
        if pending and pending.pop(0):
            raise AuthError(
                "No access token found for Nous Portal login.",
                provider="nous", code="auth_missing_access_token", relogin_required=True,
            )
        return {"provider": "nous", "access_token": "fresh"}

    class _Pool:
        def has_credentials(self):
            return False

    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: _Pool())
    monkeypatch.setattr(
        keepalive, "get_provider_auth_state", lambda provider: {"access_token": "stored"}
    )
    monkeypatch.setattr(keepalive, "resolve_nous_runtime_credentials", _resolve)


def test_keepalive_announces_relogin_once_per_outage(monkeypatch):
    """A dead login reaches the user once, not once per tick; recovery re-arms the notice."""
    import sys

    server = _FakeGatewayServer(transports=[object()])
    monkeypatch.setitem(sys.modules, "tui_gateway.server", server)
    # fail, fail (same outage), succeed (recovered), fail (a later outage).
    _singleton_relogin_state(monkeypatch, outcomes=[True, True, False, True])
    keepalive._clear_relogin_announcement()

    # Two failing ticks on the same outage: one broadcast, the second stays log-only.
    assert keepalive.refresh_nous_auth_keepalive_once() is False
    assert keepalive.refresh_nous_auth_keepalive_once() is False
    assert [(event, payload["provider"]) for event, payload in server.events] == [
        (keepalive.NOUS_RELOGIN_EVENT, "nous")
    ]

    # A successful refresh clears the dead state and re-arms the notice...
    assert keepalive.refresh_nous_auth_keepalive_once() is True
    # ...so a later outage announces again instead of staying silent forever.
    assert keepalive.refresh_nous_auth_keepalive_once() is False
    assert [(event, payload["provider"]) for event, payload in server.events] == [
        (keepalive.NOUS_RELOGIN_EVENT, "nous"),
        (keepalive.NOUS_RELOGIN_EVENT, "nous"),
    ]


def test_keepalive_stays_silent_without_connected_clients(monkeypatch):
    """No gateway clients: nothing to announce to, and the tick still fails cleanly."""
    import sys

    server = _FakeGatewayServer(transports=[])
    monkeypatch.setitem(sys.modules, "tui_gateway.server", server)
    _singleton_relogin_state(monkeypatch, outcomes=[True, True])
    keepalive._clear_relogin_announcement()

    assert keepalive.refresh_nous_auth_keepalive_once() is False
    assert server.events == []

    # The latch is still unset: clients that connect later get the notice on the next tick.
    server._live_transports = [object()]
    assert keepalive.refresh_nous_auth_keepalive_once() is False
    assert [(event, payload["provider"]) for event, payload in server.events] == [
        (keepalive.NOUS_RELOGIN_EVENT, "nous")
    ]


# --- The one-notice latch must re-arm once the login works again -------------
#
# ``_relogin_announced`` keeps a flapping outage to one toast, which is right, but it is cleared
# only from a successful *refresh*. A re-login writes fresh tokens to the auth state, and the pool
# can still answer "no usable entry" for it -- the row the outage left DEAD shadows it -- so a
# falsy pool result returns before anything consults the credential the user just repaired. The
# latch then outlives the recovery and the next terminal death is silent.


def _live_state(**overrides):
    state = {
        "provider": "nous",
        "access_token": "fresh-access-token",
        "refresh_token": "fresh-refresh-token",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    state.update(overrides)
    return state


def _state_after_terminal_death():
    """What a dead login looks like on disk: the tokens are stripped (``_quarantine_flat_oauth_state``)."""
    return {
        "provider": "nous",
        "last_auth_error": {
            "provider": "nous",
            "message": "No access token found for Nous Portal login.",
            "reason": "runtime",
            "relogin_required": True,
            "code": "auth_missing_access_token",
            "at": datetime.now(UTC).isoformat(),
        },
    }


class _PoolWithoutUsableEntry:
    """A pool with rows but no usable one: the DEAD row shadows a freshly written login."""

    def has_credentials(self):
        return True

    def select(self, **_kwargs):
        return None

    def try_refresh_current(self):
        return None


def _announce_outage(monkeypatch, server):
    """Drive the announcing path once, so the latch sits where a real outage leaves it."""
    _singleton_relogin_state(monkeypatch, outcomes=[True])
    assert keepalive.refresh_nous_auth_keepalive_once() is False
    return len(server.events)


def test_latch_rearms_when_the_credential_is_live_again(monkeypatch):
    """After a re-login the next terminal death must be heard, not swallowed by the old latch."""
    import sys

    server = _FakeGatewayServer(transports=[object()])
    monkeypatch.setitem(sys.modules, "tui_gateway.server", server)
    monkeypatch.setattr(keepalive, "_relogin_announced", False)

    assert _announce_outage(monkeypatch, server) == 1, "one outage, one notice"

    # The user signs in again: the auth state holds a fresh, unexpired token, while the pool still
    # carries the row the outage left behind and cannot select it.
    monkeypatch.setattr(keepalive, "get_provider_auth_state", lambda provider: _live_state())
    monkeypatch.setattr(
        "agent.credential_pool.load_pool", lambda provider: _PoolWithoutUsableEntry()
    )

    assert keepalive.refresh_nous_auth_keepalive_once() is False
    assert keepalive._relogin_announced is False, (
        "a live credential must retire the one-notice latch, or the next death is silent"
    )

    # A later terminal death reached the same way is announced again.
    from hermes_cli.auth_constants import AuthError

    keepalive._announce_relogin_required(
        AuthError(
            "refresh token rejected",
            provider="nous",
            code="auth_refresh_rejected",
            relogin_required=True,
        )
    )
    assert [(event, payload["provider"]) for event, payload in server.events] == [
        (keepalive.NOUS_RELOGIN_EVENT, "nous"),
        (keepalive.NOUS_RELOGIN_EVENT, "nous"),
    ]


def test_latch_survives_while_the_credential_is_still_dead(monkeypatch):
    """A dead login must not clear the latch: one outage stays one notice, however long it lasts."""
    import sys

    server = _FakeGatewayServer(transports=[object()])
    monkeypatch.setitem(sys.modules, "tui_gateway.server", server)
    monkeypatch.setattr(keepalive, "_relogin_announced", False)

    assert _announce_outage(monkeypatch, server) == 1

    from hermes_cli.auth_constants import AuthError

    still_dead = AuthError(
        "No access token found for Nous Portal login.",
        provider="nous",
        code="auth_missing_access_token",
        relogin_required=True,
    )

    # (a) the tokens were stripped by the terminal death, (b) they are there but already expired.
    for state in (
        _state_after_terminal_death(),
        _live_state(expires_at=(datetime.now(UTC) - timedelta(minutes=5)).isoformat()),
    ):
        monkeypatch.setattr(keepalive, "get_provider_auth_state", lambda provider, s=state: s)
        monkeypatch.setattr(
            "agent.credential_pool.load_pool", lambda provider: _PoolWithoutUsableEntry()
        )
        assert keepalive.refresh_nous_auth_keepalive_once() is False
        assert keepalive._relogin_announced is True, (
            f"a dead credential cleared the latch: {state!r}"
        )

        keepalive._announce_relogin_required(still_dead)

    assert len(server.events) == 1, "one outage must stay one notice"
