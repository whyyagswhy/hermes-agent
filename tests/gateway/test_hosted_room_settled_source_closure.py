"""Retained non-pass settlements keep their source user in bounded projections.

Regression for #139347: a bounded hosted-room transcript can retain a
committed member reply and its non-pass turn.settled (via the committed row's
settled_seq reference) after the originating message.user ages out of the
transcript window. The next planner pass then fails deriving watermarks with
"turn.settled references no matching user discussion".
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from gateway import hosted_room_discussion as discussion
from gateway import hosted_room_policy_checkpoint as checkpoint_module
from gateway.hosted_room_policy_checkpoint import HostedRoomPolicyCheckpoint
from gateway import hosted_rooms


ROOM_ID = "room-1"
GATEWAY_ID = "gateway-a"
LOCAL_PROFILES = ("research", "build")
MEMBERS = [
    {
        "member_id": f"member-{profile}",
        "profile": profile,
        "handle": profile,
        "display_name": profile.title(),
    }
    for profile in LOCAL_PROFILES
]


@pytest.fixture
def room_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict]:
    monkeypatch.setattr(checkpoint_module, "MAX_THREAD_TRANSCRIPT_EVENTS", 3)
    db = tmp_path / "state.db"
    room = hosted_rooms.create_room(
        db,
        room_id=ROOM_ID,
        name="Release",
        members=MEMBERS,
        authority_gateway_id=GATEWAY_ID,
        now=1,
    )
    return db, room


def _events(db: Path) -> list[dict]:
    return hosted_rooms.read_events(
        db, room_id=ROOM_ID, since_seq=0, limit=hosted_rooms.MAX_LOG_LIMIT
    )["events"]


def _append_user(db: Path, *, event_id: str, text: str) -> dict:
    return hosted_rooms.append_event(
        db,
        room_id=ROOM_ID,
        event_id=event_id,
        kind="message.user",
        actor={"kind": "user", "id": "local-user"},
        authority_gateway_id=GATEWAY_ID,
        authority_epoch=1,
        payload={"text": text, "thread_id": "thread-1"},
        now=time.time(),
    )


def _settle_first_turn(db: Path, room: dict, *, text: str) -> None:
    decision = discussion.plan_next_task(room, _events(db), local_profiles=LOCAL_PROFILES)
    assert decision.status == "task", decision
    assert decision.task is not None
    publication = discussion.plan_publication(
        room, _events(db), decision.task, status="settled",
        result={"text": text}, local_profiles=LOCAL_PROFILES,
    )
    assert publication.terminal_kind == "turn.settled"
    for event in publication.events:
        hosted_rooms.append_event(db, **event.append_kwargs(ROOM_ID), now=time.time())


def _snapshot_events(db: Path) -> tuple[dict, tuple[dict, ...]]:
    checkpoint = HostedRoomPolicyCheckpoint(db)
    latest = max(event["seq"] for event in _events(db))
    snap = checkpoint.snapshot(room_id=ROOM_ID, latest_seq=latest)
    return checkpoint, snap.events


def test_retained_settlement_keeps_source_user(room_db: tuple[Path, dict]) -> None:
    db, room = room_db
    _append_user(db, event_id="user-1", text="First request.")
    _settle_first_turn(db, room, text="Answer one.")
    # Newer same-thread traffic ages user-1 out of the bounded transcript
    # window while the committed reply + settlement stay retained.
    _append_user(db, event_id="user-2", text="Second request.")
    _append_user(db, event_id="user-3", text="Third request.")

    _, events = _snapshot_events(db)
    kinds = [event["kind"] for event in events]
    assert "turn.settled" in kinds
    # The planner derives watermarks from this projection; it must not raise.
    watermarks = discussion.derive_member_watermarks(
        room, events, local_profiles=LOCAL_PROFILES
    )
    assert watermarks


def test_genuinely_absent_source_still_raises(room_db: tuple[Path, dict]) -> None:
    db, room = room_db
    _append_user(db, event_id="user-1", text="First request.")
    _settle_first_turn(db, room, text="Answer one.")
    _append_user(db, event_id="user-2", text="Second request.")
    _append_user(db, event_id="user-3", text="Third request.")

    _, events = _snapshot_events(db)
    assert any(event["kind"] == "turn.settled" for event in events)
    # Delete the durable source: closure must not invent it, validation raises.
    import sqlite3

    with sqlite3.connect(db) as conn:
        conn.execute(
            "DELETE FROM hosted_room_events WHERE room_id=? AND event_id=?",
            (ROOM_ID, "user-1"),
        )
        conn.commit()
    checkpoint = HostedRoomPolicyCheckpoint(db)
    latest = max(event["seq"] for event in _events(db))
    snap = checkpoint.snapshot(room_id=ROOM_ID, latest_seq=latest)
    with pytest.raises(
        discussion.DiscussionValidationError,
        match="no matching user discussion",
    ):
        discussion.derive_member_watermarks(
            room, snap.events, local_profiles=LOCAL_PROFILES
        )
