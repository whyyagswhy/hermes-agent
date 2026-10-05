"""Gateway-only multiplex host mode (#133086).

The multiplex host may be a gateway-only process instead of an agent profile:

- the host home skips agent skeleton/SOUL.md seeding,
- ``profiles_to_serve`` excludes ``default`` as an agent target,
- the shared listener owner is the ``multiplex_default_profile`` pointer
  (or the first served profile), enabled from that profile's own key,
- per-profile ``/p/<profile>/`` auth works without a host key.
"""
from __future__ import annotations

from pathlib import Path

import hermes_yaml as yaml
import pytest

from hermes_cli import profiles
from hermes_cli.profiles import (
    _get_default_hermes_home,
    create_profile,
    get_profile_dir,
    profiles_to_serve,
)


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    profiles._STANDALONE_MEMO.clear()
    getattr(profiles, "_HOST_ONLY_MEMO", {}).clear()
    profiles._parked_default_warned.clear()
    return tmp_path


def _enable_host_only(default_home: Path, **gateway_keys) -> None:
    cfg = {"gateway": {"multiplex_profiles": True, "multiplex_host_only": True, **gateway_keys}}
    (default_home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


class TestHostOnlyHomeInit:
    def test_host_only_home_skips_agent_skeleton(self, profile_env):
        """Host-only home has no SOUL.md/skills seed; gateway dirs still exist."""
        from hermes_cli.config import _HERMES_HOME_ENSURED, ensure_hermes_home

        default_home = _get_default_hermes_home()
        _enable_host_only(default_home)
        _HERMES_HOME_ENSURED.clear()
        ensure_hermes_home()
        assert not (default_home / "SOUL.md").exists()
        assert not (default_home / "skills").exists()
        for subdir in ("cron", "sessions", "logs", "memories"):
            assert (default_home / subdir).is_dir()

    def test_named_profile_home_still_seeded(self, profile_env):
        """Host-only affects the host home only; named profiles seed normally."""
        from hermes_cli.config import _HERMES_HOME_ENSURED, ensure_hermes_home
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override

        default_home = _get_default_hermes_home()
        _enable_host_only(default_home)
        create_profile("coder", no_alias=True)
        token = set_hermes_home_override(str(get_profile_dir("coder")))
        try:
            _HERMES_HOME_ENSURED.clear()
            ensure_hermes_home()
        finally:
            reset_hermes_home_override(token)
        assert (get_profile_dir("coder") / "SOUL.md").exists()

    def test_existing_home_not_migrated(self, profile_env):
        """Enabling host-only never deletes a pre-existing SOUL.md/skills."""
        from hermes_cli.config import _HERMES_HOME_ENSURED, ensure_hermes_home

        default_home = _get_default_hermes_home()
        (default_home / "SOUL.md").write_text("# soul\n", encoding="utf-8")
        (default_home / "skills").mkdir(exist_ok=True)
        _enable_host_only(default_home)
        _HERMES_HOME_ENSURED.clear()
        ensure_hermes_home()
        assert (default_home / "SOUL.md").exists()
        assert (default_home / "skills").exists()


class TestHostOnlyServeList:
    def test_default_excluded_as_agent_target(self, profile_env):
        create_profile("coder", no_alias=True)
        create_profile("writer", no_alias=True)
        _enable_host_only(_get_default_hermes_home())
        serve = dict(profiles_to_serve(multiplex=True))
        assert set(serve) == {"coder", "writer"}

    def test_host_default_opt_in(self, profile_env):
        create_profile("coder", no_alias=True)
        _enable_host_only(_get_default_hermes_home())
        serve = dict(profiles_to_serve(multiplex=True, include_host_default=True))
        assert set(serve) == {"default", "coder"}

    def test_off_without_flag(self, profile_env):
        create_profile("coder", no_alias=True)
        serve = dict(profiles_to_serve(multiplex=True))
        assert set(serve) == {"default", "coder"}


class TestListenerOwner:
    def test_pointer_selects_owner(self, profile_env):
        create_profile("coder", no_alias=True)
        create_profile("writer", no_alias=True)
        _enable_host_only(_get_default_hermes_home(), multiplex_default_profile="writer")
        assert profiles.multiplex_listener_owner() == "writer"

    def test_fallback_is_first_served(self, profile_env):
        create_profile("coder", no_alias=True)
        create_profile("writer", no_alias=True)
        _enable_host_only(_get_default_hermes_home())
        assert profiles.multiplex_listener_owner() == "coder"

    def test_dangling_pointer_falls_back(self, profile_env):
        create_profile("coder", no_alias=True)
        _enable_host_only(_get_default_hermes_home(), multiplex_default_profile="ghost")
        assert profiles.multiplex_listener_owner() == "coder"

    def test_not_host_only_owner_is_default(self, profile_env):
        create_profile("coder", no_alias=True)
        assert profiles.multiplex_listener_owner() == "default"


class TestOwnerListenerEnable:
    def test_owner_env_enables_mirror_listener(self, profile_env, monkeypatch):
        """The owner's API_SERVER_KEY enables its listener even under the multiplexer."""
        from gateway.config import GatewayConfig, Platform, PlatformConfig
        from gateway import config_env

        create_profile("coder", no_alias=True)
        _enable_host_only(_get_default_hermes_home())
        monkeypatch.setattr(config_env, "_loading_secondary_under_multiplexer", lambda: True)
        monkeypatch.setattr(
            config_env, "_loading_listener_owner_under_multiplexer", lambda: True)
        cfg = GatewayConfig(platforms={Platform.API_SERVER: PlatformConfig(enabled=False)})
        out = config_env._enable_from_env(cfg, Platform.API_SERVER)
        assert out.enabled is True

    def test_non_owner_secondary_stays_mirrored(self, profile_env, monkeypatch):
        from gateway.config import GatewayConfig, Platform, PlatformConfig
        from gateway import config_env

        create_profile("coder", no_alias=True)
        create_profile("writer", no_alias=True)
        _enable_host_only(_get_default_hermes_home())
        monkeypatch.setattr(config_env, "_loading_secondary_under_multiplexer", lambda: True)
        monkeypatch.setattr(
            config_env, "_loading_listener_owner_under_multiplexer", lambda: False)
        cfg = GatewayConfig(platforms={Platform.API_SERVER: PlatformConfig(enabled=False)})
        out = config_env._enable_from_env(cfg, Platform.API_SERVER)
        assert out.enabled is False


class TestCronTickHomes:
    def test_tick_skips_host_default(self, profile_env):
        from gateway.run import _cron_tick_profile_homes
        from gateway.config import GatewayConfig

        create_profile("coder", no_alias=True)
        _enable_host_only(_get_default_hermes_home())
        homes = dict(_cron_tick_profile_homes(GatewayConfig(multiplex_profiles=True)))
        assert set(homes) == {"coder"}


class TestProfileAuthWithoutHostKey:
    def test_named_profile_key_resolves_with_empty_host_key(self, profile_env, monkeypatch):
        """Per-profile /p/<profile>/ auth must not need a host key."""
        from agent import secret_scope as ss
        from gateway.platforms.api_server import APIServerAdapter, _api_request_profile
        from gateway.config import PlatformConfig

        create_profile("coder", no_alias=True)
        profile_key = "profile-secret-key-1234567890"
        (get_profile_dir("coder") / ".env").write_text(
            f"API_SERVER_KEY={profile_key}\n", encoding="utf-8")
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        adapter._api_key = ""
        ss.set_multiplex_active(True)
        req_token = _api_request_profile.set("coder")
        try:
            with adapter._profile_scope("coder"):
                assert adapter._expected_api_key() == profile_key
        finally:
            _api_request_profile.reset(req_token)
            ss.set_multiplex_active(False)
