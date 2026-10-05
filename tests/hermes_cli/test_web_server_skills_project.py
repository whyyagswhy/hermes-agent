"""Regression tests: GET /api/skills resolves project-tier skills (#133321).

The desktop backend serves from a fixed process cwd (wherever Electron
spawned it), never inside the user's checkout — so project skills that
``hermes skills`` lists from a repo cwd were invisible to the Skills tab.
The endpoint accepts an optional ``?project=`` hint (a path inside the
checkout); when it walks up to a trusted project root the scan runs scoped
to that root. Untrusted/missing hints fall back to ambient behavior.
"""
import pytest


SKILL_MD = """---
name: {name}
description: a project skill
---

# {name}

Project-local instructions.
"""


def _write_skill(skills_dir, name):
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(SKILL_MD.format(name=name), encoding="utf-8")


@pytest.fixture
def project_env(tmp_path, monkeypatch):
    """Isolated home trusting one repo; cwd parked outside it (desktop-like)."""
    import agent.skill_utils as su

    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    _write_skill(repo / ".hermes" / "skills", "proj-skill")

    other = tmp_path / "untrusted-proj"
    (other / ".git").mkdir(parents=True)
    _write_skill(other / ".hermes" / "skills", "stranger-skill")

    (tmp_path / "elsewhere").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.chdir(tmp_path / "elsewhere")
    (home / "config.yaml").write_text(
        "skills:\n  trusted_project_dirs:\n    - '" + str(repo) + "'\n",
        encoding="utf-8",
    )
    su._external_dirs_cache_clear()
    from tools.skills_tool import clear_skills_cache

    clear_skills_cache()
    yield {"home": home, "repo": repo, "other": other}
    su._external_dirs_cache_clear()
    clear_skills_cache()


@pytest.fixture
def client(monkeypatch, project_env):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    import hermes_state
    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")
    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


def _names(resp):
    assert resp.status_code == 200
    return {s["name"] for s in resp.json()}


class TestProjectScopedSkills:
    def test_ambient_scope_misses_project_skills(self, client, project_env):
        # The bug context: a backend parked outside the checkout (desktop's
        # spawned `serve`) sees no project tier without a hint.
        assert "proj-skill" not in _names(client.get("/api/skills"))

    def test_project_hint_includes_trusted_project_skills(self, client, project_env):
        names = _names(client.get("/api/skills", params={"project": str(project_env["repo"])}))
        assert "proj-skill" in names

    def test_project_hint_accepts_subdir(self, client, project_env):
        sub = project_env["repo"] / "a" / "b"
        sub.mkdir(parents=True)
        names = _names(client.get("/api/skills", params={"project": str(sub)}))
        assert "proj-skill" in names

    def test_untrusted_project_hint_ignored(self, client, project_env):
        names = _names(client.get("/api/skills", params={"project": str(project_env["other"])}))
        assert "stranger-skill" not in names
        assert "proj-skill" not in names

    def test_missing_project_hint_ignored(self, client, project_env):
        names = _names(client.get("/api/skills", params={"project": "/no/such/dir"}))
        assert "proj-skill" not in names
