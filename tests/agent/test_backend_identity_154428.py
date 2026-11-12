"""Regression tests for #154428: same_deployment must not skip a named-provider fallback
that shares the host but carries a distinct credential source."""

from unittest.mock import patch

import pytest

from agent.backend_identity import (
    BackendIdentity,
    FailureScope,
    same_deployment,
    should_skip_candidate,
)


def _id(provider="", model="", base_url=""):
    return BackendIdentity.build(provider=provider, model=model, base_url=base_url)


_ANTHROPIC = "https://api.anthropic.com"
_MODEL = "claude-opus-5-5"


def _cfg_distinct_key_cmd():
    return {
        "providers": {
            "account-a": {"api": _ANTHROPIC, "transport": "anthropic_messages", "key_cmd": "get-key-a"},
            "account-b": {"api": _ANTHROPIC, "transport": "anthropic_messages", "key_cmd": "get-key-b"},
        }
    }


def _cfg_shared_api_key():
    """Two alias labels sharing one literal api_key — shim-alias pattern."""
    return {
        "providers": {
            "my-alias":  {"api_key": "sk-shared", "api": "http://localhost:7891/v1"},
            "my-alias2": {"api_key": "sk-shared", "api": "http://localhost:7891/v1"},
        }
    }


class TestIncident154428NamedProvidersDistinctCredentials:
    """Bug: same_deployment skipped account-b after account-a 429'd even though
    they carry different key_cmd / api_key / key_env credential sources."""

    def test_distinct_key_cmd_same_host_is_not_same_deployment(self):
        """account-a and account-b both on api.anthropic.com with different key_cmd
        → NOT the same deployment (the fix case)."""
        failed = _id("account-a", _MODEL, _ANTHROPIC)
        cand   = _id("account-b", _MODEL, _ANTHROPIC)
        with patch("hermes_cli.config.load_config_readonly", return_value=_cfg_distinct_key_cmd()):
            assert not same_deployment(cand, failed), (
                "account-b should not be treated as the same deployment as account-a "
                "when they carry different key_cmd credentials"
            )

    def test_should_not_skip_distinct_credential_fallback_after_429(self):
        """The public predicate: a 429 on account-a must NOT cause account-b to be skipped."""
        failed = _id("account-a", _MODEL, _ANTHROPIC)
        cand   = _id("account-b", _MODEL, _ANTHROPIC)
        with patch("hermes_cli.config.load_config_readonly", return_value=_cfg_distinct_key_cmd()):
            assert not should_skip_candidate(cand, failed, FailureScope.MODEL), (
                "account-b should be tried after account-a 429 — it has its own rate-limit quota"
            )

    def test_shim_alias_shared_api_key_still_treated_as_same_deployment(self):
        """Two alias labels sharing one literal api_key ARE the same deployment (#22548 preserved)."""
        a = _id("my-alias",  _MODEL, "http://localhost:7891/v1")
        b = _id("my-alias2", _MODEL, "http://localhost:7891/v1")
        with patch("hermes_cli.config.load_config_readonly", return_value=_cfg_shared_api_key()):
            assert same_deployment(a, b), (
                "Aliases sharing a credential are still one deployment"
            )

    def test_provider_not_in_config_falls_back_to_shim_alias_rule(self):
        """When a label is not in the providers config, conservatively keep the
        same-deployment default (unknown labels → old shim-alias behaviour)."""
        failed = _id("unknown-a", _MODEL, _ANTHROPIC)
        cand   = _id("unknown-b", _MODEL, _ANTHROPIC)
        with patch("hermes_cli.config.load_config_readonly", return_value={}):
            assert same_deployment(cand, failed), (
                "Without config evidence, treat same-host aliases as same deployment (conservative)"
            )

    def test_distinct_key_env_same_host_is_not_same_deployment(self):
        """key_env variant: ANTHROPIC_KEY_A vs ANTHROPIC_KEY_B → different deployments."""
        cfg = {
            "providers": {
                "acct-env-a": {"api": _ANTHROPIC, "key_env": "ANTHROPIC_KEY_A"},
                "acct-env-b": {"api": _ANTHROPIC, "key_env": "ANTHROPIC_KEY_B"},
            }
        }
        failed = _id("acct-env-a", _MODEL, _ANTHROPIC)
        cand   = _id("acct-env-b", _MODEL, _ANTHROPIC)
        with patch("hermes_cli.config.load_config_readonly", return_value=cfg):
            assert not same_deployment(cand, failed)
            assert not should_skip_candidate(cand, failed, FailureScope.MODEL)

    def test_same_label_always_same_deployment_regardless_of_config(self):
        """The same provider label is always the same deployment (no config lookup needed)."""
        a = _id("account-a", _MODEL, _ANTHROPIC)
        b = _id("account-a", _MODEL, _ANTHROPIC)
        with patch("hermes_cli.config.load_config_readonly", return_value=_cfg_distinct_key_cmd()):
            assert same_deployment(a, b)
            assert should_skip_candidate(b, a, FailureScope.MODEL)
