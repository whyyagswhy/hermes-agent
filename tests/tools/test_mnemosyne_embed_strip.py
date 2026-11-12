"""#151247 — MNEMOSYNE_EMBEDDING_* vars must survive strip_launch_profile_env.

mnemosyne reads these at module-level import, before load_hermes_dotenv() runs in the child,
so they must arrive via the Popen env or the child freezes at built-in defaults
(384-dim bge-small, no API backend).  These are deployment-level config shared by all
profiles on the host — stripping them is a bug.
"""
from pathlib import Path

import pytest

from agent.secret_scope import (
    build_profile_secret_scope, reset_secret_scope, set_multiplex_active, set_secret_scope,
)
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture
def embed_homes(tmp_path, monkeypatch):
    """Two-profile setup where the LAUNCH profile's .env contains MNEMOSYNE_EMBEDDING_* vars."""
    root = tmp_path / "hermes"
    target = root / "profiles" / "worker"
    for home in (root, target):
        (home / ".env").parent.mkdir(parents=True, exist_ok=True)

    # Launch profile has embedding backend config in its .env
    (root / ".env").write_text(
        "HERMES_MODEL=default-model\n"
        "MNEMOSYNE_EMBEDDING_MODEL=@cf/baai/bge-m3\n"
        "MNEMOSYNE_EMBEDDING_DIM=1024\n"
        "MNEMOSYNE_EMBEDDINGS_VIA_API=true\n"
        "MNEMOSYNE_EMBEDDING_API_URL=https://embed.example.com/v1\n"
        "MNEMOSYNE_EMBEDDING_API_KEY=sk-embed-secret\n"
        "SOME_LAUNCH_ONLY_SECRET=should-be-stripped\n",
        encoding="utf-8",
    )
    # Target profile has no embedding config of its own
    (target / ".env").write_text("HERMES_LANGUAGE=zh\n", encoding="utf-8")

    # Simulate the launch process having loaded the .env into os.environ
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_MODEL", "default-model")
    monkeypatch.setenv("MNEMOSYNE_EMBEDDING_MODEL", "@cf/baai/bge-m3")
    monkeypatch.setenv("MNEMOSYNE_EMBEDDING_DIM", "1024")
    monkeypatch.setenv("MNEMOSYNE_EMBEDDINGS_VIA_API", "true")
    monkeypatch.setenv("MNEMOSYNE_EMBEDDING_API_URL", "https://embed.example.com/v1")
    monkeypatch.setenv("MNEMOSYNE_EMBEDDING_API_KEY", "sk-embed-secret")
    monkeypatch.setenv("SOME_LAUNCH_ONLY_SECRET", "should-be-stripped")

    set_multiplex_active(True)
    home_token = set_hermes_home_override(str(target))
    try:
        yield root, target
    finally:
        reset_hermes_home_override(home_token)
        set_multiplex_active(False)


def _stripped_env(embed_homes):
    """Call strip_launch_profile_env on a copy of os.environ and return the result."""
    import os
    from tools.environments.local import strip_launch_profile_env
    _root, target = embed_homes
    env = dict(__import__("os").environ)
    strip_launch_profile_env(env, target)
    return env


def test_mnemosyne_embedding_model_survives_strip(embed_homes):
    """MNEMOSYNE_EMBEDDING_MODEL from launch .env must NOT be stripped — it is
    read at mnemosyne module import time before load_hermes_dotenv() runs. (#151247)"""
    env = _stripped_env(embed_homes)
    assert env.get("MNEMOSYNE_EMBEDDING_MODEL") == "@cf/baai/bge-m3", (
        "MNEMOSYNE_EMBEDDING_MODEL was stripped; child will freeze at built-in default"
    )


def test_mnemosyne_embedding_dim_and_via_api_survive_strip(embed_homes):
    """DIM and the plural MNEMOSYNE_EMBEDDINGS_VIA_API must also survive."""
    env = _stripped_env(embed_homes)
    assert env.get("MNEMOSYNE_EMBEDDING_DIM") == "1024"
    assert env.get("MNEMOSYNE_EMBEDDINGS_VIA_API") == "true"


def test_mnemosyne_embedding_api_url_and_key_survive_strip(embed_homes):
    """API_URL and API_KEY are deployment-level config (one backend serves all profiles)."""
    env = _stripped_env(embed_homes)
    assert env.get("MNEMOSYNE_EMBEDDING_API_URL") == "https://embed.example.com/v1"
    assert env.get("MNEMOSYNE_EMBEDDING_API_KEY") == "sk-embed-secret"


def test_non_mnemosyne_launch_vars_still_stripped(embed_homes):
    """SOME_LAUNCH_ONLY_SECRET is still stripped — only MNEMOSYNE_EMBED* gets the exception."""
    env = _stripped_env(embed_homes)
    assert "SOME_LAUNCH_ONLY_SECRET" not in env
    assert "HERMES_MODEL" not in env
