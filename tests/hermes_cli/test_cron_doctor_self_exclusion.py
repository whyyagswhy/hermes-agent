"""Tests for cron doctor self-exclusion (issue #133135).

Covers:
  - the invoking job is excluded via HERMES_CRON_DOCTOR_SELF_ID
  - --exclude flag values exclude while other failed jobs still report
  - long/multiline last_error text is capped to a single short line
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    """Isolate cron job storage into a temp dir so tests don't stomp on real jobs."""
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def make_failed_job(workdir, name):
    from cron.jobs import create_job, update_job
    job = create_job(prompt="do the thing", schedule="30m", name=name)
    return update_job(job["id"], {"last_status": "error",
                                  "last_error": "boom " * 200 + "\nsecond line"})


def test_self_excluded_via_env(tmp_cron_dir, monkeypatch, capsys):
    from hermes_cli.cron import cron_doctor
    job = make_failed_job(tmp_cron_dir, "self-job")
    monkeypatch.setenv("HERMES_CRON_DOCTOR_SELF_ID", job["id"])
    assert cron_doctor() == 0
    assert job["id"] not in capsys.readouterr().out


def test_flag_excludes_self_but_reports_others(tmp_cron_dir, capsys):
    from hermes_cli.cron import cron_doctor
    self_job = make_failed_job(tmp_cron_dir, "self-job")
    other = make_failed_job(tmp_cron_dir, "other-job")
    assert cron_doctor(exclude_job_ids=[self_job["id"]]) == 1
    out = capsys.readouterr().out
    assert self_job["id"] not in out
    assert other["id"] in out


def test_last_error_line_capped(tmp_cron_dir, capsys):
    from hermes_cli.cron import cron_doctor
    job = make_failed_job(tmp_cron_dir, "long-job")
    assert cron_doctor() == 1
    lines = [line for line in capsys.readouterr().out.splitlines()
             if job["id"] in line or "last run failed" in line]
    assert lines
    assert all(len(line) <= 200 for line in lines)
    assert all("\n" not in line for line in lines)
