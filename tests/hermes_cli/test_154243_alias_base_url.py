"""#154243: model.base_url must be honoured for provider alias spellings.

_same_registered_provider must resolve aliases (lm-studio -> lmstudio) before
the PROVIDER_REGISTRY lookup; otherwise an alias always returns None, the
function returns False, and _config_base_url_for_provider discards base_url.
"""
import pytest
from hermes_cli.runtime_provider import _same_registered_provider, _config_base_url_for_provider

LAN_URL = "http://192.168.1.50:1234/v1"

def test_canonical_matches_alias_lm_studio():
    """lmstudio == lm-studio (alias) -> same provider, must return True."""
    assert _same_registered_provider("lmstudio", "lm-studio") is True

def test_canonical_matches_alias_lm_underscore():
    """lmstudio == lm_studio (alias) -> same provider, must return True."""
    assert _same_registered_provider("lmstudio", "lm_studio") is True

def test_alias_both_sides():
    """lm-studio == lm_studio (both aliases of lmstudio) -> same provider."""
    assert _same_registered_provider("lm-studio", "lm_studio") is True

def test_different_providers_still_false():
    """Unrelated providers must still return False."""
    assert _same_registered_provider("lmstudio", "openai") is False

def test_base_url_honoured_for_lm_dash_studio_alias():
    """Alias lm-studio: base_url must be returned, not discarded."""
    cfg = {"provider": "lm-studio", "base_url": LAN_URL}
    assert _config_base_url_for_provider(cfg, "lmstudio") == LAN_URL

def test_base_url_honoured_for_lm_underscore_studio_alias():
    """Alias lm_studio: base_url must be returned, not discarded."""
    cfg = {"provider": "lm_studio", "base_url": LAN_URL}
    assert _config_base_url_for_provider(cfg, "lmstudio") == LAN_URL

def test_base_url_still_honoured_for_canonical():
    """Canonical spelling still works after the fix."""
    cfg = {"provider": "lmstudio", "base_url": LAN_URL}
    assert _config_base_url_for_provider(cfg, "lmstudio") == LAN_URL
