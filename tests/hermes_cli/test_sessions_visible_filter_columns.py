"""Issue #133013: prune/archive preview + list must show the filtered values.

Preview rows only rendered id/last-active/source/model/msgs/title, so a
`prune --min-cost 5 --provider x --before ...` confirmation never showed the
cost, provider, or start time it was filtering on. `sessions list` had no
--columns/--json/--sort at all.
"""

import json
import sys
import time

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    d = SessionDB(tmp_path / "state.db")
    yield d
    try:
        d.close()
    except Exception:
        pass


def _mk(db, sid, **cols):
    db.create_session(sid, source=cols.pop("source", "cli"))
    base = {
        "started_at": time.time() - 100 * 86400,
        "ended_at": time.time() - 99 * 86400,
        "message_count": 7,
        "title": "filter me",
        "model": "unit-model",
        "input_tokens": 1200,
        "output_tokens": 800,
        "tool_call_count": 9,
        "actual_cost_usd": 0.42,
        "estimated_cost_usd": 0.10,
        "billing_provider": "nous",
        "git_branch": "feat/cols",
        "end_reason": "complete",
        "pinned": 0,
        "cwd": "/home/me/proj",
        "user_id": "u7",
        "chat_id": "c9",
        "chat_type": "dm",
    }
    base.update(cols)
    with db._lock:
        db._conn.execute(
            "UPDATE sessions SET {} WHERE id=?".format(
                ", ".join(f"{k}=?" for k in base)
            ),
            (*base.values(), sid),
        )
        db._conn.commit()


def test_prune_candidates_expose_filter_columns(db):
    _mk(db, "20260101_000000_aaaaaa")
    (row,) = db.list_prune_candidates(title_like="filter")
    for key in (
        "input_tokens", "output_tokens", "tool_call_count",
        "actual_cost_usd", "estimated_cost_usd", "billing_provider",
        "git_branch", "end_reason", "pinned", "cwd",
        "user_id", "chat_id", "chat_type",
    ):
        assert key in row, f"candidate row missing {key}"
    assert row["input_tokens"] == 1200
    assert row["tool_call_count"] == 9
    assert row["actual_cost_usd"] == pytest.approx(0.42)
    assert row["billing_provider"] == "nous"
    assert row["git_branch"] == "feat/cols"
    assert row["pinned"] == 0


def test_prune_row_keys_match_candidate_rows(db):
    from hermes_cli.web_routers.sessions import _PRUNE_ROW_KEYS
    _mk(db, "20260101_000000_aaaaaa")
    (row,) = db.list_prune_candidates(title_like="filter")
    assert set(_PRUNE_ROW_KEYS) <= set(row), (
        set(_PRUNE_ROW_KEYS) - set(row)
    )


def _run_cli(monkeypatch, capsys, argv, candidates):
    import hermes_cli.main as main_mod
    import hermes_state

    class FakeDB:
        def list_sessions_rich(self, **kwargs):
            return candidates

        def list_prune_candidates(self, **kwargs):
            return candidates

        def count_open_prune_matches(self, **kwargs):
            return 0

        def count_prune_matches(self, pinned_only=False, **kwargs):
            return 0

        def prune_sessions(self, **kwargs):
            return len(candidates)

        def close(self):
            pass

    monkeypatch.setattr(hermes_state, "SessionDB", lambda *a, **k: FakeDB())
    monkeypatch.setattr(sys, "argv", ["hermes", *argv])
    main_mod.main()
    return capsys.readouterr().out


def _candidate(**over):
    row = {
        "id": "20260101_000000_aaaaaa",
        "source": "cli",
        "title": "filter me",
        "model": "unit-model",
        "started_at": 1_700_000_000.0,
        "last_active": 1_700_000_050.0,
        "ended_at": 1_700_000_100.0,
        "message_count": 7,
        "archived": 0,
        "input_tokens": 1200,
        "output_tokens": 800,
        "tool_call_count": 9,
        "actual_cost_usd": 0.42,
        "estimated_cost_usd": 0.10,
        "billing_provider": "nous",
        "git_branch": "feat/cols",
        "end_reason": "complete",
        "pinned": 0,
        "cwd": "/home/me/proj",
        "user_id": "u7",
        "chat_id": "c9",
        "chat_type": "dm",
    }
    row.update(over)
    return row


def test_preview_shows_columns_active_filters_select_on(monkeypatch, capsys):
    out = _run_cli(
        monkeypatch, capsys,
        ["sessions", "prune", "--dry-run", "--model", "unit",
         "--provider", "nous", "--min-tokens", "100", "--min-cost", "0.1",
         "--min-tool-calls", "2", "--branch", "feat",
         "--end-reason", "complete", "--cwd", "/home/me",
         "--user", "u7", "--chat-id", "c9", "--chat-type", "dm"],
        [_candidate()],
    )
    for needle in ("nous", "feat/cols", "complete", "/home/me", "u7", "c9",
                   "dm", "2000", "9", "0.42"):
        assert needle in out, f"preview hides filtered value {needle!r}:\n{out}"


def test_preview_shows_started_when_before_after_in_play(monkeypatch, capsys):
    out = _run_cli(
        monkeypatch, capsys,
        ["sessions", "prune", "--dry-run", "--before", "2026-02-01"],
        [_candidate()],
    )
    from hermes_cli.session_filters import format_epoch
    assert format_epoch(1_700_000_000.0) in out


def test_preview_cost_labels_actual_vs_estimated(monkeypatch, capsys):
    out_actual = _run_cli(
        monkeypatch, capsys,
        ["sessions", "prune", "--dry-run", "--min-cost", "0.1"],
        [_candidate(actual_cost_usd=0.42, estimated_cost_usd=0.10)],
    )
    assert "$0.42 actual" in out_actual
    out_est = _run_cli(
        monkeypatch, capsys,
        ["sessions", "prune", "--dry-run", "--min-cost", "0.01"],
        [_candidate(actual_cost_usd=0, estimated_cost_usd=0.03)],
    )
    assert "~$0.03 est" in out_est


def test_preview_shows_pin_marker(monkeypatch, capsys):
    out = _run_cli(
        monkeypatch, capsys,
        ["sessions", "prune", "--dry-run", "--include-pinned", "--title", "filter"],
        [_candidate(pinned=1)],
    )
    assert "pinned" in out.lower()


def test_format_epoch_is_shared(monkeypatch, capsys):
    from hermes_cli import session_filters, timefmt
    assert session_filters.format_epoch is timefmt.format_epoch


def _rich(**over):
    row = _candidate()
    row.update({
        "preview": "hello world",
        "created_source": "",
        "hidden": 0,
    })
    row.update(over)
    return row


def test_list_columns_json_sort(monkeypatch, capsys):
    rows = [_rich(id="20260101_000000_aaaaaa", message_count=2),
            _rich(id="20260601_000000_bbbbbb", message_count=9)]
    out = _run_cli(
        monkeypatch, capsys,
        ["sessions", "list", "--columns", "id,messages,cost",
         "--sort=-messages", "--json"],
        rows,
    )
    payload = json.loads(out)
    assert [r["id"] for r in payload] == [
        "20260601_000000_bbbbbb", "20260101_000000_aaaaaa"]
    assert set(payload[0]) >= {"id", "messages", "cost"}
