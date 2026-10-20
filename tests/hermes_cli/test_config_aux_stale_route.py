"""`hermes config set auxiliary.<task>.provider` drops the stale route (#141832).

Mirrors the ``model.provider`` guard (#113719, #40862): a provider switch
re-points the block at a new route, so a ``base_url``/``api_mode`` left by the
old provider must go (or at minimum warn) instead of silently overriding the
new provider.
"""

from hermes_cli import config as cfg


def _read(tmp_path, *path):
    """Read a nested value straight from the on-disk config.yaml."""
    import hermes_yaml as yaml
    data = yaml.safe_load((tmp_path / "config.yaml").read_text()) or {}
    node = data
    for seg in path:
        node = node[seg]
    return node


def _seed_aux_route(tmp_path, monkeypatch, base_url="https://api.openai.com/v1"):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg.set_config_value("auxiliary.vision.provider", "openai-api")
    cfg.set_config_value("auxiliary.vision.base_url", base_url)


class TestAuxProviderSwitchClearsStaleRoute:
    def test_stale_base_url_cleared_with_notice(self, tmp_path, monkeypatch, capsys):
        _seed_aux_route(tmp_path, monkeypatch)
        cfg.set_config_value("auxiliary.vision.provider", "openai-codex")
        assert _read(tmp_path, "auxiliary", "vision").get("base_url") in (None, "")
        assert "Cleared auxiliary.vision.base_url" in capsys.readouterr().out

    def test_same_provider_keeps_base_url(self, tmp_path, monkeypatch, capsys):
        _seed_aux_route(tmp_path, monkeypatch)
        cfg.set_config_value("auxiliary.vision.provider", "openai-api")
        assert _read(tmp_path, "auxiliary", "vision")["base_url"] == "https://api.openai.com/v1"
        assert "Cleared auxiliary.vision.base_url" not in capsys.readouterr().out

    def test_unknown_host_base_url_warns_and_stays(self, tmp_path, monkeypatch, capsys):
        _seed_aux_route(tmp_path, monkeypatch, base_url="https://lan.local/v1")
        cfg.set_config_value("auxiliary.vision.provider", "openai-codex")
        assert _read(tmp_path, "auxiliary", "vision")["base_url"] == "https://lan.local/v1"
        assert "still applies to openai-codex" in capsys.readouterr().out
