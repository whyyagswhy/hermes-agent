"""Stale gateway active-session lease after runtime reload is reclaimed by the orphan sweep.

#154162: after ``hermes plugins update`` the gateway re-creates its runtime while the process
stays alive. The old active-session claim stays in the registry (its pid is still live), and
every new turn for that session returns SESSION_NOT_OWNED until a gateway restart.

The fix: the gateway's housekeeping loop periodically calls ``release_orphaned_leases`` with the
set of its currently-live session lease IDs, mirroring what ``tui_gateway``'s idle reaper already
does. A stale lease from a replaced runtime (not in the live set) is then reclaimed on the next
housekeeping tick rather than lasting until restart.
"""

import json
import time

from hermes_cli.active_sessions import (
    SESSION_NOT_OWNED,
    _read_entries,
    _state_path,
    _write_entries,
    release_orphaned_leases,
    try_acquire_active_session,
)

_GRACE = 31.0  # just past _SELF_ORPHAN_GRACE_SECONDS (30 s)


def _backdate_entries(state_path, delta_s=_GRACE):
    """Rewrite every entry's started_at to be delta_s seconds in the past."""
    entries = _read_entries(state_path)
    now = time.time()
    for entry in entries:
        entry['started_at'] = now - delta_s
    _write_entries(state_path, entries)


def test_stale_lease_blocks_next_turn_without_sweep(tmp_path, monkeypatch):
    """Without the orphan sweep a stale same-pid same-session-key claim blocks the next turn."""
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))

    lease_a, refusal = try_acquire_active_session(
        session_id='chat-1',
        surface='gateway:local',
        config={},
        registry_home=tmp_path,
        metadata={'live_session_id': 'runtime-A'},
    )
    assert refusal is None and lease_a is not None

    # Simulate: runtime re-created (plugin update) — lease_a never released.
    _backdate_entries(_state_path(tmp_path))  # past the 30-s grace
    lease_b, refusal_b = try_acquire_active_session(
        session_id='chat-1',
        surface='gateway:local',
        config={},
        registry_home=tmp_path,
        metadata={'live_session_id': 'runtime-B'},
    )
    # Without the sweep the stale claim blocks the new runtime.
    assert refusal_b is not None and refusal_b.reason == SESSION_NOT_OWNED, (
        f'Expected SESSION_NOT_OWNED; got refusal={refusal_b}, lease={lease_b}'
    )


def test_orphan_sweep_reclaims_stale_lease_and_allows_next_turn(tmp_path, monkeypatch):
    """With the orphan sweep the stale claim is dropped and the new runtime acquires the slot."""
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))

    lease_a, refusal = try_acquire_active_session(
        session_id='chat-1',
        surface='gateway:local',
        config={},
        registry_home=tmp_path,
        metadata={'live_session_id': 'runtime-A'},
    )
    assert refusal is None and lease_a is not None

    _backdate_entries(_state_path(tmp_path))  # past the 30-s grace

    # Housekeeping sweep: live set is empty (old lease was not formally released).
    dropped = release_orphaned_leases(live_lease_ids=set())
    assert dropped == 1, f'Expected 1 orphan dropped; got {dropped}'

    # Now the new runtime acquires the slot.
    lease_b, refusal_b = try_acquire_active_session(
        session_id='chat-1',
        surface='gateway:local',
        config={},
        registry_home=tmp_path,
        metadata={'live_session_id': 'runtime-B'},
    )
    assert refusal_b is None and lease_b is not None, (
        f'Expected success after sweep; got refusal={refusal_b}'
    )
