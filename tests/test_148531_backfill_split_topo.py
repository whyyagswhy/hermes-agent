"""Tests for #148531 — owner-backfill 409 on split topology.

The bug: backfill_session_owner_profiles calls _with_db(read_only=False) even when
a standalone gateway owns the store.  exclusive_maintenance raises OwnershipConflict
→ WARNING "drain and stop the gateway" on every desktop boot.

The fix: when _owner_db_for returns None, check discover_gateway_endpoint first.
If a live owner is found, skip the offline write (backfill is idempotent; the
gateway stamps its own rows).  Call _with_db only when genuinely owner-less.
"""
import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.web_routers import sessions as sessions_mod
from hermes_cli.web_routers.sessions import backfill_session_owner_profiles, manage_router


def _make_app(gateway_runner=None):
    app = FastAPI()
    app.state.gateway_runner = gateway_runner
    app.include_router(manage_router)
    return app


def _mock_discovery(endpoint_present: bool):
    ep = MagicMock() if endpoint_present else None
    return SimpleNamespace(
        endpoint=ep,
        state="present" if endpoint_present else "absent",
        reason_code=None,
        detail=None,
    )


_PROFILE = "maiduy"
_STAMP = "maiduy"
_HOME = "/home/maiduy/.hermes"


# ---------------------------------------------------------------------------
# RED (structural) — proves the fix is present in the source.
#
# On unpatched main, backfill_session_owner_profiles never calls
# discover_gateway_endpoint, so it always takes the offline exclusive-maintenance
# path in a split topology, which loses the ownership lock every desktop boot.
#
# This test fails on unpatched main and passes only after the fix.
# ---------------------------------------------------------------------------

def test_backfill_fix_consults_discovery_in_split_topology():
    """Structural RED: fix must add a discover_gateway_endpoint call to the else-branch."""
    src = inspect.getsource(backfill_session_owner_profiles)
    assert "discover_gateway_endpoint" in src, (
        "Fix absent: backfill_session_owner_profiles must call discover_gateway_endpoint "
        "before attempting the offline exclusive_maintenance reservation (#148531). "
        "On unpatched main the else-branch calls _with_db(read_only=False) unconditionally, "
        "which raises OwnershipConflict every time a standalone gateway holds the lock."
    )


# ---------------------------------------------------------------------------
# GREEN — split topology with live owner: return stamped=0, skip _with_db
# ---------------------------------------------------------------------------

def test_backfill_split_topo_live_owner_skips_offline_write():
    """Split topology + live gateway owner → stamped=0, _with_db never attempted."""
    app = _make_app(gateway_runner=None)   # no in-process runner = split topology

    with (
        patch.object(sessions_mod, "_owner_db_for", return_value=None),
        patch(
            "hermes_cli.gateway_runtime.discover_gateway_endpoint",
            return_value=_mock_discovery(endpoint_present=True),
        ),
        patch.object(sessions_mod, "_with_db", return_value=99),
        patch.object(sessions_mod, "_serving_profile", return_value=_STAMP),
        patch.object(sessions_mod, "_cron_profile_home",
                     return_value=(_PROFILE, _HOME)),
    ):
        client = TestClient(app)
        resp = client.post("/api/sessions/owner-backfill", json={"profile": _PROFILE})

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["stamped"] == 0, (
        f"live-owner path must skip the offline write; got stamped={data['stamped']!r}"
    )


# ---------------------------------------------------------------------------
# GREEN — no live owner: offline _with_db path runs (unchanged behaviour)
# ---------------------------------------------------------------------------

def test_backfill_split_topo_no_owner_runs_offline_write():
    """Split topology + no live owner → offline write runs as before."""
    app = _make_app(gateway_runner=None)

    with (
        patch.object(sessions_mod, "_owner_db_for", return_value=None),
        patch(
            "hermes_cli.gateway_runtime.discover_gateway_endpoint",
            return_value=_mock_discovery(endpoint_present=False),
        ),
        patch.object(sessions_mod, "_with_db", return_value=3),
        patch.object(sessions_mod, "_serving_profile", return_value=_STAMP),
        patch.object(sessions_mod, "_cron_profile_home",
                     return_value=(_PROFILE, _HOME)),
    ):
        client = TestClient(app)
        resp = client.post("/api/sessions/owner-backfill", json={"profile": _PROFILE})

    assert resp.status_code == 200, resp.text
    assert resp.json()["stamped"] == 3


# ---------------------------------------------------------------------------
# GREEN — composed topology: existing in-process path unchanged
# ---------------------------------------------------------------------------

def test_backfill_composed_topo_uses_owner_db():
    """Composed topology (runner in-process): uses owner_db directly, no discovery."""
    mock_db = MagicMock()
    mock_db.backfill_null_session_profiles.return_value = 0

    app = _make_app(gateway_runner=MagicMock())

    with (
        patch.object(sessions_mod, "_owner_db_for", return_value=mock_db),
        patch("hermes_cli.gateway_runtime.discover_gateway_endpoint") as mock_disc,
        patch.object(sessions_mod, "_serving_profile", return_value=_STAMP),
    ):
        client = TestClient(app)
        resp = client.post("/api/sessions/owner-backfill", json={"profile": None})

    assert resp.status_code == 200, resp.text
    mock_disc.assert_not_called()
    mock_db.backfill_null_session_profiles.assert_called_once_with(_STAMP)
