"""
Slack native replay must not conflict on arrival-only deltas.

A Slack gateway restart, a message_changed event, or a Relay buffer replay can
deliver the same message with:
  - a fresh receive timestamp (MessageEvent.timestamp defaults to datetime.now())
  - refreshed channel_context (Slack thread history re-fetched on reconnect)
  - a refreshed display name for the same user_id (Slack profile updates)

None of these are content changes.  Before this fix, reconcile_native_retry
returned the new payload unchanged when ingress_sha256 didn't match and there
was no media to compare, causing admit_session_input to raise
RuntimeStoreError('admission_conflict').

Negative controls: genuinely changed content and a different user_id still
produce a non-matching result so the conflict guard stays intact.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from gateway.session_ingress_media import ingress_fingerprint, reconcile_native_retry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

T1 = datetime(2026, 11, 12, 6, 54, 49, tzinfo=UTC).isoformat()
T2 = datetime(2026, 11, 12, 6, 55, 1, tzinfo=UTC).isoformat()   # 12 s later (replay)


class _FakeDB:
    """Minimal DB double: a single in-memory SQLite table."""

    def __init__(self, payload):
        self._conn = sqlite3.connect(':memory:')
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            'CREATE TABLE session_admissions'
            '(principal_id TEXT, target_session_id TEXT, request_id TEXT,'
            ' status TEXT, payload_json TEXT)'
        )
        self._conn.execute(
            'INSERT INTO session_admissions VALUES (?,?,?,?,?)',
            ('p1', 's1', 'r1', 'terminal', json.dumps(payload)),
        )
        self._conn.commit()

    @contextmanager
    def _read_ctx(self):
        yield self._conn


def _make_payload(ts, *, channel_context=None, user_name='Alice', user_id='U12345',
                  text='hello', with_fingerprint=True):
    """Construct a minimal native_text_v1 payload."""
    ntv1: dict = {
        'source': {
            'chat_id': 'C99',
            'user_id': user_id,
            'user_name': user_name,
            'platform': 'slack',
        },
        'route': 'slack:C99',
        'event': {'user_id': user_id, 'user_name': user_name},
        'timestamp': ts,
    }
    if channel_context is not None:
        ntv1['event']['channel_context'] = channel_context
    base = {'text': text, 'native_text_v1': ntv1}
    if with_fingerprint:
        ntv1['ingress_sha256'] = ingress_fingerprint(base)
    return base


def _retry(db, payload):
    return reconcile_native_retry(
        db, principal_id='p1', session_id='s1', request_id='r1', payload=payload
    )


# ---------------------------------------------------------------------------
# RED-on-main tests (the three arrival-only delta cases)
# ---------------------------------------------------------------------------

def test_replay_new_timestamp_resolves_to_retained_fingerprint():
    """A replay with a later receive timestamp must not be treated as changed content."""
    original = _make_payload(T1)
    db = _FakeDB(original)
    replay = _make_payload(T2, with_fingerprint=False)         # T2 differs, no sha256
    result = _retry(db, replay)
    stored_fp = original['native_text_v1']['ingress_sha256']
    assert ingress_fingerprint(result) == stored_fp, (
        f"Fingerprint mismatch: replay timestamp change should be absorbed; "
        f"got {ingress_fingerprint(result)!r} want {stored_fp!r}"
    )


def test_replay_refreshed_channel_context_resolves_to_retained_fingerprint():
    """A replay with new channel_context (Slack thread re-fetched) must not conflict."""
    original = _make_payload(T1, channel_context='thread history v1')
    db = _FakeDB(original)
    replay = _make_payload(T2, channel_context='thread history v2 (refreshed)',
                           with_fingerprint=False)
    result = _retry(db, replay)
    stored_fp = original['native_text_v1']['ingress_sha256']
    assert ingress_fingerprint(result) == stored_fp


def test_replay_refreshed_display_name_same_user_id_resolves_to_retained_fingerprint():
    """A display-name change with the same Slack user_id must not conflict."""
    original = _make_payload(T1, user_name='Alice', user_id='U12345')
    db = _FakeDB(original)
    replay = _make_payload(T2, user_name='Alice Smith', user_id='U12345',
                           with_fingerprint=False)
    result = _retry(db, replay)
    stored_fp = original['native_text_v1']['ingress_sha256']
    assert ingress_fingerprint(result) == stored_fp


# ---------------------------------------------------------------------------
# Negative controls — must stay fail-closed after the fix
# ---------------------------------------------------------------------------

def test_changed_text_still_returns_new_payload():
    """Different message text must still produce a non-matching fingerprint."""
    original = _make_payload(T1, text='hello')
    db = _FakeDB(original)
    replay = _make_payload(T2, text='goodbye', with_fingerprint=False)
    result = _retry(db, replay)
    stored_fp = original['native_text_v1']['ingress_sha256']
    assert ingress_fingerprint(result) != stored_fp, (
        "Changed text must NOT be normalised to the retained payload"
    )
    assert result['text'] == 'goodbye'


def test_different_user_id_does_not_inherit_username():
    """A different user_id must not receive the retained user_name."""
    original = _make_payload(T1, user_name='Alice', user_id='U12345')
    db = _FakeDB(original)
    replay = _make_payload(T2, user_name='Bob', user_id='U99999',
                           with_fingerprint=False)
    result = _retry(db, replay)
    # user_id mismatch → no user_name normalization; fingerprint must differ
    stored_fp = original['native_text_v1']['ingress_sha256']
    assert ingress_fingerprint(result) != stored_fp
    # And the returned source must NOT carry Alice's user_id
    assert result['native_text_v1']['source']['user_id'] == 'U99999'
