"""Plausibility gate for model.base_url in the registry api_key rung (#133179).

A stale base_url kept across a provider switch must not redirect the newly selected
named-vendor provider's credential onto another vendor's endpoint. The gate honours the
configured URL only when it plausibly belongs to the provider (canonical
inference_base_url or a documented same-vendor mirror); a URL naming another
registered provider's endpoint falls back to the credential/env resolution.
"""

from hermes_cli import runtime_provider as rp


def _run_api_key_rung(monkeypatch, provider, model_cfg, creds_base_url, api_key="sk-test-key"):
    pconfig = rp.PROVIDER_REGISTRY[provider]
    monkeypatch.setattr(
        rp,
        "resolve_api_key_provider_credentials",
        lambda _p: {
            "provider": provider,
            "api_key": api_key,
            "base_url": creds_base_url,
            "source": "env",
        },
    )
    return rp._api_key_provider_runtime(provider, pconfig, provider, model_cfg, None)


def test_stale_foreign_base_url_falls_back_to_credential_endpoint(monkeypatch):
    """provider deepseek + a stale OpenAI endpoint must not receive the DeepSeek key."""
    resolved = _run_api_key_rung(
        monkeypatch,
        "deepseek",
        {"provider": "deepseek", "base_url": "https://api.openai.com/v1"},
        "https://api.deepseek.com/v1",
    )
    assert resolved["base_url"] == "https://api.deepseek.com/v1"


def test_own_canonical_base_url_honoured(monkeypatch):
    resolved = _run_api_key_rung(
        monkeypatch,
        "deepseek",
        {"provider": "deepseek", "base_url": "https://api.deepseek.com/v1/"},
        "https://api.deepseek.com/v1",
    )
    assert resolved["base_url"] == "https://api.deepseek.com/v1"


def test_documented_minimax_cn_mirror_honoured(monkeypatch):
    """The api.minimaxi.com China endpoint stays honoured under provider minimax."""
    resolved = _run_api_key_rung(
        monkeypatch,
        "minimax",
        {"provider": "minimax", "base_url": "https://api.minimaxi.com/anthropic"},
        "https://api.minimax.io/anthropic",
    )
    assert resolved["base_url"] == "https://api.minimaxi.com/anthropic"


def test_sibling_mirror_is_foreign_for_unrelated_provider(monkeypatch):
    resolved = _run_api_key_rung(
        monkeypatch,
        "deepseek",
        {"provider": "deepseek", "base_url": "https://api.minimaxi.com/anthropic"},
        "https://api.deepseek.com/v1",
    )
    assert resolved["base_url"] == "https://api.deepseek.com/v1"


def test_zai_coding_plan_mirror_honoured(monkeypatch):
    resolved = _run_api_key_rung(
        monkeypatch,
        "zai",
        {"provider": "zai", "base_url": "https://api.z.ai/api/coding/paas/v4"},
        "https://api.z.ai/api/paas/v4",
    )
    assert resolved["base_url"] == "https://api.z.ai/api/coding/paas/v4"


def test_kimi_code_mirror_honoured(monkeypatch):
    resolved = _run_api_key_rung(
        monkeypatch,
        "kimi-coding",
        {"provider": "kimi-coding", "base_url": "https://api.kimi.com/coding"},
        "https://api.moonshot.ai/v1",
    )
    assert resolved["base_url"] == "https://api.kimi.com/coding"


def test_unrecognized_custom_base_url_still_honoured(monkeypatch):
    """Unknown hosts are the user's own proxy/loopback/LAN box (cf. the lmstudio LAN
    neighbour tests): only *known* cross-vendor endpoints are rejected."""
    resolved = _run_api_key_rung(
        monkeypatch,
        "deepseek",
        {"provider": "deepseek", "base_url": "https://proxy.example.com/v1"},
        "https://api.deepseek.com/v1",
    )
    assert resolved["base_url"] == "https://proxy.example.com/v1"


def test_openrouter_host_never_treated_as_foreign(monkeypatch):
    """An openrouter.ai config URL is a deliberate mirror/proxy (#10622), not stale."""
    resolved = _run_api_key_rung(
        monkeypatch,
        "deepseek",
        {"provider": "deepseek", "base_url": "https://openrouter.ai/api/v1"},
        "https://api.deepseek.com/v1",
    )
    assert resolved["base_url"] == "https://openrouter.ai/api/v1"


def test_explicit_base_url_rung_unaffected(monkeypatch):
    """The explicit --base-url rung keeps caller intent verbatim, even cross-vendor."""
    pconfig = rp.PROVIDER_REGISTRY["deepseek"]
    resolved = rp._explicit_api_key_provider(
        "deepseek",
        pconfig,
        "deepseek",
        {"provider": "deepseek"},
        "sk-test-key",
        "https://api.openai.com/v1",
        None,
    )
    assert resolved["base_url"] == "https://api.openai.com/v1"
    assert resolved["source"] == "explicit"
