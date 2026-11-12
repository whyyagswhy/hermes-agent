"""#152674 — Nous/OAuth dashboard logins must leave a per-login audit trail
(auth_attribution_v1) in session_admissions.payload without changing
actor.subject (host_owner_subject() per PR #145598).
"""
import asyncio
import json
import pytest
import pytest_asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


@pytest_asyncio.fixture
async def simple_authority(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    monkeypatch.setattr(run, '_load_gateway_config',
                        lambda: {'platform_toolsets': {'cli': []}})
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = SimpleNamespace(
        session_store=store, _session_db=store._db,
        adapters={}, _draining=False,
    )
    authority = await initialize_session_authority(
        runner, profile_id='test-profile', instance_id='fixture-inst')
    monkeypatch.setattr(authority, '_schedule', lambda ref: None)
    yield authority
    store._db.close()


def _owner_actor(profile_id='test-profile'):
    from gateway.owner_identity import host_owner_subject
    from gateway.session_contract import SessionActor
    caps = frozenset({'session:create', 'session:read', 'session:submit',
                      'session:control', 'session:approve'})
    return SessionActor(host_owner_subject(), profile_id, caps, 'fixture-transport')


def _submission(ref, text='hello', request_id='req-1'):
    from gateway.session_contract import Submission
    return Submission(request_id=request_id, ref=ref,
                      payload={'text': text}, intent='queue')


def _mock_nous_transport(user_id='nas_user:67a3ff6f', provider='nous'):
    t = MagicMock()
    t.auth_identity = {'user_id': user_id, 'provider': provider}
    return t


def _mock_local_transport(uid='uid:1000'):
    t = MagicMock()
    t.auth_identity = {'user_id': uid, 'provider': 'local'}
    return t


def _admission_payload(authority, session_id, request_id, actor_subject):
    """Read the durable payload dict from the admission row (column: payload)."""
    from hermes_state_runtime import find_session_admission
    row = find_session_admission(
        authority.db,
        principal_id=actor_subject,
        session_id=session_id,
        request_id=request_id,
    )
    assert row is not None, f"admission row not found for {request_id!r}"
    raw = row['payload']
    return json.loads(raw) if isinstance(raw, str) else raw


def _patch_transport(monkeypatch, transport):
    import tui_gateway.transport as tt
    monkeypatch.setattr(tt, 'current_transport', lambda: transport)


# ---------------------------------------------------------------------------
# Integration tests — RED on unpatched main, GREEN on fix
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nous_login_stamps_auth_attribution_in_admission(simple_authority, monkeypatch):
    """Core RED test: Nous login must produce auth_attribution_v1 in the
    durable admission payload.  On unpatched main this key is absent."""
    from gateway.session_local import create_local_session
    from gateway.owner_identity import host_owner_subject
    actor = _owner_actor()
    ref = create_local_session(simple_authority, actor, {
        'request_id': 'create-1', 'source': 'gui', 'cwd': '/tmp',
        'model': 'fixture', 'toolsets': [],
    })
    _patch_transport(monkeypatch, _mock_nous_transport('nas_user:67a3ff6f', 'nous'))
    await simple_authority.submit(actor, _submission(ref, request_id='req-nous-1'))
    payload = _admission_payload(simple_authority, ref.session_id,
                                 'req-nous-1', host_owner_subject())
    assert 'auth_attribution_v1' in payload, (
        "Nous login must stamp auth_attribution_v1 in admission payload "
        "(#152674); got keys: " + str(list(payload.keys()))
    )
    assert payload['auth_attribution_v1']['user_id'] == 'nas_user:67a3ff6f'
    assert payload['auth_attribution_v1']['provider'] == 'nous'


@pytest.mark.asyncio
async def test_two_accounts_produce_distinct_audit_records(simple_authority, monkeypatch):
    """Same principal_id but distinct auth_attribution_v1.user_id per Nous account."""
    from gateway.session_local import create_local_session
    from gateway.owner_identity import host_owner_subject
    actor = _owner_actor()
    ref = create_local_session(simple_authority, actor, {
        'request_id': 'create-2', 'source': 'gui', 'cwd': '/tmp',
        'model': 'fixture', 'toolsets': [],
    })
    for user_id, req_id in [
        ('nas_user:67a3ff6f', 'req-acct-a'),
        ('nas_user:5fc47070', 'req-acct-b'),
    ]:
        _patch_transport(monkeypatch, _mock_nous_transport(user_id, 'nous'))
        await simple_authority.submit(actor, _submission(ref, f'msg {user_id}', req_id))
    subject = host_owner_subject()
    p_a = _admission_payload(simple_authority, ref.session_id, 'req-acct-a', subject)
    p_b = _admission_payload(simple_authority, ref.session_id, 'req-acct-b', subject)
    assert 'auth_attribution_v1' in p_a
    assert 'auth_attribution_v1' in p_b
    assert p_a['auth_attribution_v1']['user_id'] != p_b['auth_attribution_v1']['user_id']


@pytest.mark.asyncio
async def test_local_native_desktop_does_not_stamp_attribution(simple_authority, monkeypatch):
    """Native Desktop (provider=local) must NOT produce auth_attribution_v1."""
    from gateway.session_local import create_local_session
    from gateway.owner_identity import host_owner_subject
    actor = _owner_actor()
    ref = create_local_session(simple_authority, actor, {
        'request_id': 'create-3', 'source': 'gui', 'cwd': '/tmp',
        'model': 'fixture', 'toolsets': [],
    })
    _patch_transport(monkeypatch, _mock_local_transport())
    await simple_authority.submit(actor, _submission(ref, request_id='req-local-1'))
    payload = _admission_payload(simple_authority, ref.session_id,
                                 'req-local-1', host_owner_subject())
    assert 'auth_attribution_v1' not in payload, (
        "Native Desktop (provider=local) must not stamp auth_attribution_v1"
    )


@pytest.mark.asyncio
async def test_no_transport_does_not_stamp_attribution(simple_authority, monkeypatch):
    """current_transport() == None (stdio/PTY) → no auth_attribution_v1."""
    from gateway.session_local import create_local_session
    from gateway.owner_identity import host_owner_subject
    actor = _owner_actor()
    ref = create_local_session(simple_authority, actor, {
        'request_id': 'create-4', 'source': 'gui', 'cwd': '/tmp',
        'model': 'fixture', 'toolsets': [],
    })
    _patch_transport(monkeypatch, None)
    await simple_authority.submit(actor, _submission(ref, request_id='req-stdio-1'))
    payload = _admission_payload(simple_authority, ref.session_id,
                                 'req-stdio-1', host_owner_subject())
    assert 'auth_attribution_v1' not in payload


@pytest.mark.asyncio
async def test_principal_id_stays_host_owner_on_nous_login(simple_authority, monkeypatch):
    """The fix must NOT change principal_id — it stays host_owner_subject() (PR #145598)."""
    from gateway.session_local import create_local_session
    from gateway.owner_identity import host_owner_subject
    from hermes_state_runtime import find_session_admission
    actor = _owner_actor()
    ref = create_local_session(simple_authority, actor, {
        'request_id': 'create-5', 'source': 'gui', 'cwd': '/tmp',
        'model': 'fixture', 'toolsets': [],
    })
    _patch_transport(monkeypatch, _mock_nous_transport('nas_user:aabbcc', 'nous'))
    await simple_authority.submit(actor, _submission(ref, request_id='req-ownership-1'))
    row = find_session_admission(
        simple_authority.db,
        principal_id=host_owner_subject(),
        session_id=ref.session_id,
        request_id='req-ownership-1',
    )
    assert row is not None, (
        "Admission must remain keyed by host_owner_subject() — session ownership "
        "contract (PR #145598) must not change"
    )
