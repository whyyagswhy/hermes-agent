"""Gateway wiring for generation-scoped Git metadata publication."""

from __future__ import annotations

from tui_gateway import server
from hermes_state import SessionDB


class _ImmediateThread:
    def __init__(self, *, target, **_kwargs):
        self._target = target

    def start(self):
        self._target()


def test_cwd_claim_precedes_probe_and_generation_reaches_publish(monkeypatch):
    events = []

    class DB:
        def update_session_cwd(self, session_id, cwd):
            events.append(("claim", session_id, cwd))
            return 17

        def publish_session_git_metadata(
            self, session_id, cwd, generation, branch, root
        ):
            events.append(
                ("publish", session_id, cwd, generation, branch, root)
            )
            return True

    monkeypatch.setattr(server, "_get_db", lambda: DB())
    monkeypatch.setattr(server.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(
        server.git_probe,
        "branch",
        lambda cwd: events.append(("probe", cwd)) or "feature",
    )
    monkeypatch.setattr(server.git_probe, "common_repo_root", lambda _cwd: "/repo")

    generation = server._persist_session_cwd_and_schedule_git_meta(
        {"session_key": "session"}, "/repo/worktree"
    )

    assert generation == 17
    assert events == [
        ("claim", "session", "/repo/worktree"),
        ("probe", "/repo/worktree"),
        (
            "publish",
            "session",
            "/repo/worktree",
            17,
            "feature",
            "/repo",
        ),
    ]


def test_missing_db_claim_never_starts_git_probe(monkeypatch):
    probed = []
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(
        server.git_probe, "branch", lambda cwd: probed.append(cwd)
    )

    generation = server._persist_session_cwd_and_schedule_git_meta(
        {"session_key": "session"}, "/repo"
    )

    assert generation is None
    assert probed == []


def test_persisted_row_missing_git_metadata_is_enriched_on_hydration(monkeypatch, tmp_path):
    """A row that already carries its cwd but no git metadata is healed on resume (#108784).

    The lazy-row path enriches on the first submit, so this covers the OTHER way a row reaches
    hydration with a cwd and no branch: one written before the enrichment existed, or by a writer
    that stamped only the cwd. The row is built directly rather than through
    ``_ensure_session_db_row`` so the two paths stay independently covered.
    """
    db = SessionDB(db_path=tmp_path / "state.db")
    repo = tmp_path / "repo"
    repo.mkdir()
    key = "desktop-session"
    db.create_session(key, source="desktop", model="test-model", cwd=str(repo))
    session = {
        "session_key": key,
        "source": "desktop",
        "cwd": str(repo),
        "explicit_cwd": True,
    }
    sid = "live-desktop-session"

    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(server.git_probe, "branch", lambda _cwd: "feature/session-metadata")
    monkeypatch.setattr(server.git_probe, "common_repo_root", lambda _cwd: str(repo))
    server._sessions[sid] = session
    try:
        assert db.get_session(key)["git_branch"] is None

        server._hydrate_session_cwd(sid, key, db, None)

        row = db.get_session(key)
        assert row["cwd"] == str(repo)
        assert row["git_branch"] == "feature/session-metadata"
        assert row["git_repo_root"] == str(repo)
    finally:
        server._sessions.pop(sid, None)
        db.close()


def test_dead_out_of_repo_worktree_heals_to_its_repo_root(tmp_path, monkeypatch):
    "#145910: a removed out-of-repo worktree heals to its repo, not the bare parent folder."
    import contextlib
    import subprocess

    repo = tmp_path / "repo"
    wt_parent = tmp_path / "_worktrees"
    wt_parent.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "t@t.t"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "t"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(wt_parent / "feat")],
        check=True,
        capture_output=True,
    )
    wt = wt_parent / "feat"
    assert wt.is_dir()
    key = "worktree-session"
    rows = {"cwd": str(wt), "git_branch": "feat", "git_repo_root": str(repo)}

    class DB:
        def update_session_cwd(self, session_id, cwd, git_branch=None, git_repo_root=None, **_kw):
            if rows["cwd"] != cwd:
                rows["git_branch"] = git_branch or None
                rows["git_repo_root"] = git_repo_root or None
            rows["cwd"] = cwd
            return 3

        def get_session(self, _key):
            return dict(rows)

        def publish_session_git_metadata(self, _sid, _cwd, _gen, branch, root):
            rows["git_branch"] = branch
            rows["git_repo_root"] = root
            return True

    @contextlib.contextmanager
    def fake_db(_session):
        yield DB()

    subprocess.run(
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], check=True
    )
    assert not wt.exists()
    session = {"session_key": key, "cwd": str(wt)}
    monkeypatch.setattr(server, "_session_is_local_backend", lambda _s: True)
    monkeypatch.setattr(server, "_session_db", fake_db)
    monkeypatch.setattr(server.threading, "Thread", _ImmediateThread)

    healed = server._display_session_cwd(session)

    assert healed == str(repo)
    assert session["cwd"] == str(repo)
    assert rows["git_repo_root"] == str(repo)
