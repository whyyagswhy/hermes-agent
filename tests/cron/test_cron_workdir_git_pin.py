"""Tests for cron workdir git-state pinning (issue #133097).

Covers:
  - jobs._git_state_of_workdir: repo snapshot shape, non-repo None,
    missing git / odd state None (best-effort)
  - jobs.create_job: pin recorded for repo workdirs, absent otherwise
  - jobs.update_job: workdir change re-pins, non-repo workdir drops the pin
  - jobs.workdir_git_mismatch: branch drift refuses, same-branch advance
    passes, detached pins commit, dirty only warns, enforce=0 skips,
    legacy records without a pin pass
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(not GIT, reason="git binary not available")


def make_repo(path, branch="main"):
    subprocess.run(
        [GIT, "init", "-b", branch, str(path)], check=True, capture_output=True
    )
    subprocess.run(
        [GIT, "-C", str(path), "config", "user.email", "t@t"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [GIT, "-C", str(path), "config", "user.name", "t"],
        check=True,
        capture_output=True,
    )
    (path / "f.txt").write_text("x")
    subprocess.run([GIT, "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        [GIT, "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True
    )
    return path


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Isolate cron job storage into a temp dir so tests don't stomp on real jobs."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def create_job(workdir):
    from cron.jobs import create_job

    return create_job(
        prompt="do the thing",
        schedule="30m",
        workdir=str(workdir),
    )


# ---------------------------------------------------------------------------
# jobs._git_state_of_workdir
# ---------------------------------------------------------------------------


class TestGitStateOfWorkdir:
    @needs_git
    def test_repo_snapshot_shape(self, tmp_path):
        from cron.jobs import _git_state_of_workdir

        make_repo(tmp_path / "repo")
        pin = _git_state_of_workdir(str(tmp_path / "repo"))
        assert pin is not None
        assert pin["branch"] == "main"
        assert len(pin["commit"]) == 40
        assert pin["dirty"] is False

    @needs_git
    def test_dirty_flag(self, tmp_path):
        from cron.jobs import _git_state_of_workdir

        make_repo(tmp_path / "repo")
        (tmp_path / "repo" / "f.txt").write_text("changed")
        pin = _git_state_of_workdir(str(tmp_path / "repo"))
        assert pin is not None
        assert pin["dirty"] is True

    def test_plain_dir_returns_none(self, tmp_path):
        from cron.jobs import _git_state_of_workdir

        assert _git_state_of_workdir(str(tmp_path)) is None

    def test_none_returns_none(self):
        from cron.jobs import _git_state_of_workdir

        assert _git_state_of_workdir(None) is None


# ---------------------------------------------------------------------------
# create_job / update_job pin plumbing
# ---------------------------------------------------------------------------


class TestPinPlumbing:
    @needs_git
    def test_create_records_pin_for_repo(self, tmp_cron_dir, tmp_path):
        make_repo(tmp_path / "repo")
        job = create_job(tmp_path / "repo")
        assert job["workdir_git"]["branch"] == "main"

    def test_create_without_pin_for_plain_dir(self, tmp_cron_dir, tmp_path):
        job = create_job(tmp_path)
        assert "workdir_git" not in job

    @needs_git
    def test_update_repins_on_workdir_change(self, tmp_cron_dir, tmp_path):
        from cron.jobs import update_job

        make_repo(tmp_path / "repo")
        job = create_job(tmp_path)
        assert "workdir_git" not in job
        updated = update_job(job["id"], {"workdir": str(tmp_path / "repo")})
        assert updated is not None
        assert updated["workdir_git"]["branch"] == "main"

    @needs_git
    def test_update_drops_pin_for_plain_dir(self, tmp_cron_dir, tmp_path):
        from cron.jobs import update_job

        make_repo(tmp_path / "repo")
        job = create_job(tmp_path / "repo")
        assert "workdir_git" in job
        updated = update_job(job["id"], {"workdir": str(tmp_path)})
        assert updated is not None
        assert "workdir_git" not in updated


# ---------------------------------------------------------------------------
# workdir_git_mismatch
# ---------------------------------------------------------------------------


class TestMismatch:
    @needs_git
    def test_branch_drift_refuses(self, tmp_cron_dir, tmp_path):
        from cron.jobs import workdir_git_mismatch

        make_repo(tmp_path / "repo")
        job = create_job(tmp_path / "repo")
        subprocess.run(
            [GIT, "-C", str(tmp_path / "repo"), "checkout", "-b", "feature"],
            check=True,
            capture_output=True,
        )
        err = workdir_git_mismatch(job)
        assert err is not None
        assert "feature" in err
        assert "main" in err

    @needs_git
    def test_same_branch_advance_passes(self, tmp_cron_dir, tmp_path):
        from cron.jobs import workdir_git_mismatch

        make_repo(tmp_path / "repo")
        job = create_job(tmp_path / "repo")
        (tmp_path / "repo" / "g.txt").write_text("y")
        subprocess.run(
            [GIT, "-C", str(tmp_path / "repo"), "add", "-A"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [GIT, "-C", str(tmp_path / "repo"), "commit", "-m", "second"],
            check=True,
            capture_output=True,
        )
        assert workdir_git_mismatch(job) is None

    @needs_git
    def test_detached_pin_enforces_commit(self, tmp_cron_dir, tmp_path):
        from cron.jobs import create_job, workdir_git_mismatch

        make_repo(tmp_path / "repo")
        subprocess.run(
            [GIT, "-C", str(tmp_path / "repo"), "checkout", "--detach", "HEAD"],
            check=True,
            capture_output=True,
        )
        job = create_job(
            prompt="do the thing", schedule="30m", workdir=str(tmp_path / "repo")
        )
        assert job["workdir_git"]["branch"] is None
        assert workdir_git_mismatch(job) is None
        (tmp_path / "repo" / "h.txt").write_text("z")
        subprocess.run(
            [GIT, "-C", str(tmp_path / "repo"), "add", "-A"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [GIT, "-C", str(tmp_path / "repo"), "commit", "-m", "third"],
            check=True,
            capture_output=True,
        )
        err = workdir_git_mismatch(job)
        assert err is not None
        assert "moved from pinned commit" in err

    @needs_git
    def test_enforce_zero_skips(self, tmp_cron_dir, tmp_path, monkeypatch):
        from cron.jobs import workdir_git_mismatch

        make_repo(tmp_path / "repo")
        job = create_job(tmp_path / "repo")
        subprocess.run(
            [GIT, "-C", str(tmp_path / "repo"), "checkout", "-b", "feature"],
            check=True,
            capture_output=True,
        )
        monkeypatch.setenv("HERMES_CRON_WORKDIR_GIT_ENFORCE", "0")
        assert workdir_git_mismatch(job) is None

    def test_legacy_record_without_pin_passes(self):
        from cron.jobs import workdir_git_mismatch

        assert workdir_git_mismatch({"id": "x", "workdir": "/tmp"}) is None
        assert workdir_git_mismatch({"id": "x"}) is None
