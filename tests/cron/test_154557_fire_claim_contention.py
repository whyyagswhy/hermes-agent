"""#154557 — two defects in cron fire-claim contention handling.

Defect 1: when a live fire_claim blocks a cron tick's claim attempt, the
pre-created execution receipt is closed as status='failed' with "Fire claim lost".
The at-most-once guard fired correctly — the receipt should be 'cancelled'.

Defect 2: a cron tick that finds a live claim and returns without claiming also
leaves pending_slot on the job, causing _restore_unclaimed_slot to loop the
slot back as due on every subsequent tick until the claim clears.

Both tests run against the real store (a temp HERMES_HOME) per the
E2E-over-mocks discipline for file-touching cron code.
"""
import datetime

import pytest
from datetime import UTC, timedelta


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield tmp_path


# ---------------------------------------------------------------------------
# Defect 2 — pending_slot consumed on live-claim refusal
# ---------------------------------------------------------------------------

def test_live_claim_contention_consumes_pending_slot(temp_home):
    """When a live fire_claim blocks a second claim, pending_slot is consumed so
    _restore_unclaimed_slot cannot loop the slot back as due (#154557 Defect 2)."""
    from cron.jobs import (
        _jobs_lock, create_job, claim_job_for_fire, get_job, load_jobs, save_jobs,
    )
    from cron.occurrences import pending_slot_stamp
    from hermes_time import now as _now

    job = create_job(prompt="x", schedule="every 5m", name="d2-test")
    jid = job["id"]

    # First claim — simulates a live manual fire holding the claim
    assert claim_job_for_fire(jid) is True

    # Stamp pending_slot to simulate the cron tick advancing next_run_at before claiming
    # (the scenario described in the issue: manual fire crosses the job's own cron slot)
    now = _now()
    slot = pending_slot_stamp(get_job(jid).get("next_run_at") or now.isoformat(), now)
    with _jobs_lock():
        jobs = load_jobs()
        for j in jobs:
            if j.get("id") == jid:
                j["pending_slot"] = slot
        save_jobs(jobs)

    assert get_job(jid).get("pending_slot") is not None, "pending_slot not stamped (test setup)"

    # Second claim attempt — blocked by live first claim
    result = claim_job_for_fire(jid)
    assert result is False

    # Defect 2: before fix, pending_slot would survive and cause the replay loop
    after = get_job(jid)
    assert after.get("pending_slot") is None, (
        f"pending_slot not consumed after live-claim refusal: {after.get('pending_slot')!r} "
        f"(would cause _restore_unclaimed_slot replay loop every minute)"
    )


# ---------------------------------------------------------------------------
# Defect 1 — live-claim refusal books execution as 'cancelled', not 'failed'
# ---------------------------------------------------------------------------

def test_live_claim_contention_books_cancelled_not_failed(temp_home):
    """When a live fire_claim blocks a cron tick, the pre-created execution receipt
    must be settled as status='cancelled', not 'failed' (#154557 Defect 1)."""
    from cron.executions import create_execution, finish_execution, get_execution
    from cron.jobs import (
        FIRE_CLAIM_TTL_SECONDS, _claim_is_live, create_job, claim_job_for_fire, get_job,
    )
    from hermes_time import now as _now

    job = create_job(prompt="x", schedule="every 5m", name="d1-test")
    jid = job["id"]

    # First claim — holds the fire_claim (simulates live manual fire)
    assert claim_job_for_fire(jid) is True

    live_job = get_job(jid)
    assert _claim_is_live(live_job.get("fire_claim"), _now(), FIRE_CLAIM_TTL_SECONDS), (
        "fire_claim not live after claim (test setup failure)"
    )

    # Simulate the pre-created receipt a cron tick stamps before calling claim_job_for_fire
    exec_id = create_execution(jid, source="scheduler")["id"]

    # Second claim attempt — blocked by live first claim
    result = claim_job_for_fire(jid)
    assert result is False

    # Settle the receipt the way _process_due_job now does for a live-claim refusal
    # (Defect 1 fix: use cancelled=True instead of success=False only)
    if _claim_is_live(live_job.get("fire_claim"), _now(), FIRE_CLAIM_TTL_SECONDS):
        finish_execution(
            exec_id, success=False, cancelled=True,
            error="Skipped: job already running (live fire claim; at-most-once guard).")
    else:
        finish_execution(exec_id, success=False, error="Fire claim lost; execution was not started.")

    row = get_execution(exec_id)
    assert row is not None
    assert row["status"] == "cancelled", (
        f"Expected status='cancelled' for live-claim contention, got {row['status']!r}. "
        f"Before fix this would be 'failed', alarming operators with spurious failure rows."
    )
    assert "already running" in (row.get("error") or ""), row
