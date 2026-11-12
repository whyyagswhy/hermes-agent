"""reconcile_pending must install the fire secret scope before calling _save_compose_deliver.

Regression for #146428: when the gateway multiplexes secondary profiles, the
reconciliation path called _save_compose_deliver without a secret scope, causing
_home_env_lookup -> get_secret to raise UnscopedSecretError and leaving the run
status=running forever.
"""
import hashlib
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _journal_path(tmp_home: Path, job_id: str, request_id: str) -> Path:
    key = hashlib.sha256(json.dumps([job_id, request_id]).encode()).hexdigest()
    admissions = tmp_home / 'cron' / 'admissions'
    admissions.mkdir(parents=True, exist_ok=True)
    return admissions / (key + '.json')


@pytest.fixture()
def multiplex_active():
    from agent.secret_scope import reset_multiplex_context, set_multiplex_context
    token = set_multiplex_context(True)
    yield
    reset_multiplex_context(token)


def test_reconcile_installs_scope_before_save_compose_deliver(tmp_path, multiplex_active):
    """_save_compose_deliver must run with an active secret scope.

    Before fix: reconcile_pending called _save_compose_deliver without a scope while
    multiplex_context was True → get_secret raised UnscopedSecretError, the exception
    was swallowed, and the run stayed status=running forever.
    After fix: _fire_secret_scope() wraps the call → a scope is active.
    """
    from agent.secret_scope import UnscopedSecretError, get_secret

    job_id = f"job-{uuid.uuid4().hex[:8]}"
    request_id = str(uuid.uuid4())
    journal = _journal_path(tmp_path, job_id, request_id)
    journal.write_text(json.dumps({
        "params": {"job_id": job_id, "request_id": request_id},
        "job": {"id": job_id, "deliver": "home"},
    }), encoding="utf-8")

    scope_was_active = []

    def _capture_save(*args, **kwargs):
        try:
            get_secret("_PROBE_", "")
            scope_was_active.append(True)
        except UnscopedSecretError:
            scope_was_active.append(False)

    terminal_state = {
        "status": "terminal",
        "result": [True, "output", "answer", None],
        "job": {"id": job_id, "deliver": "home"},
    }

    async def _mock_rpc(*_args, **_kwargs):
        return terminal_state

    mock_client = MagicMock()
    mock_client.rpc = _mock_rpc
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_client)
    cm.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("hermes_constants.get_hermes_home", return_value=tmp_path),
        patch("cron.executions.execution_owner_live", return_value=False),
        patch("cron.executions.get_execution", return_value=None),
        patch("cron.jobs.get_job", return_value={"id": job_id, "deliver": "home"}),
        patch("gateway.session_cron.owner_for_home", return_value=None),
        patch("hermes_cli.gateway_client.connect_gateway", return_value=cm),
        # Patch at the source module so reconcile_pending's local import picks it up
        patch("cron.scheduler._save_compose_deliver", side_effect=_capture_save),
        patch("cron.scheduler_bookkeeping.fail_empty_response"),
        patch("cron.scheduler_bookkeeping.finish_completed_run", return_value=True),
        patch("cron.scheduler_authority._lift_pause"),
        patch("cron.jobs.pause_job", return_value={}),
    ):
        from cron.scheduler_authority import reconcile_pending
        reconcile_pending(allow_connect=True)

    assert scope_was_active, "_save_compose_deliver was never called"
    assert scope_was_active[0], (
        "Secret scope NOT active inside _save_compose_deliver — "
        "UnscopedSecretError would have wedged the run as status=running (#146428)"
    )
