"""Secondary hatch for #133183: custom-prefixed catalog rows.

providers['custom:<name>'] must win over the bare-name fallback, and the
custom: path must not early-return just because the stripped name is a
known Hermes provider id. Primary model_metadata.py gate lives in PR
#133187 and is untouched here.
"""
from unittest.mock import patch

from agent.models_dev import _configured_catalog_provider


def _cfg(config):
    def _cfg_get(*keys, default=None, **kwargs):
        node = config
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node
    return patch("agent.models_dev._cfg_get", side_effect=_cfg_get)


def test_custom_prefixed_row_beats_bare_fallback():
    config = {"providers": {
        "custom:mygw": {"catalog_provider": "deepseek"},
        "mygw": {"catalog_provider": "anthropic"},
    }}
    with _cfg(config):
        assert _configured_catalog_provider("custom:mygw", config=config) == "deepseek"


def test_custom_path_skips_known_name_early_return():
    config = {"providers": {"custom:openai": {"catalog_provider": "deepseek"}}}
    with _cfg(config):
        assert _configured_catalog_provider("custom:openai", config=config) == "deepseek"


def test_bare_known_provider_still_returns_none():
    config = {"providers": {"openai": {"catalog_provider": "deepseek"}}}
    with _cfg(config):
        assert _configured_catalog_provider("openai", config=config) is None
