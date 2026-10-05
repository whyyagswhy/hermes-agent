"""``hermes sessions list`` tells the user when ``--limit`` cut the listing (#111989).

The cap is applied inside the SQL query, so the lister probes one row past it; the footer
must appear only when that probe row exists.
"""

from argparse import Namespace

import pytest

from hermes_cli import sessions_cmd


@pytest.fixture
def db(tmp_path):
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / "state.db")
    for i in range(6):
        db.create_session(f"sess_{i}", "cli")
        db.append_message(f"sess_{i}", "user", f"hello {i}")
    yield db
    db.close()


def _list(db, limit, capsys):
    sessions_cmd._cmd_list(db, Namespace(limit=limit, source=None, workspace=None))
    return capsys.readouterr().out


def test_footer_when_more_sessions_than_limit(db, capsys):
    out = _list(db, 4, capsys)
    ids = [line.split()[-1] for line in out.splitlines() if line.startswith("hello")]
    assert len(ids) == 4  # the probe row is never rendered
    assert "--limit 8" in out


def test_no_footer_when_listing_fits(db, capsys):
    assert "more" not in _list(db, 6, capsys)  # exactly the limit
    assert "more" not in _list(db, 20, capsys)  # fewer than the limit


"""hermes sessions list --json/--sort/--columns (#133036)."""

import json as _json


def _list_ns(db, capsys, **kw):
    args = Namespace(limit=20, source=None, workspace=None, json=False, sort="started", columns=None)
    for k, v in kw.items():
        setattr(args, k, v)
    rc = sessions_cmd._cmd_list(db, args)
    return rc, capsys.readouterr().out


def test_json_output_is_parseable_array(db, capsys):
    rc, out = _list_ns(db, capsys, json=True)
    assert rc is None
    rows = _json.loads(out)
    assert [r["id"] for r in rows] == [f"sess_{i}" for i in reversed(range(6))]


def test_json_columns_projects_keys(db, capsys):
    _, out = _list_ns(db, capsys, json=True, columns="id,title")
    rows = _json.loads(out)
    assert rows and all(set(r) == {"id", "title"} for r in rows)


def test_sort_messages_orders_by_message_count(tmp_path, capsys):
    from hermes_state import SessionDB
    tdb = SessionDB(db_path=tmp_path / "msgs.db")
    try:
        tdb.create_session("many", "cli")
        for i in range(4):
            tdb.append_message("many", "user", f"msg {i}")
        tdb.create_session("few", "cli")
        tdb.append_message("few", "user", "hi")
        _, out = _list_ns(tdb, capsys, sort="messages")
    finally:
        tdb.close()
    first = [line for line in out.splitlines() if line.startswith(("hi", "msg "))][0]
    assert first.startswith("msg ")


def test_sort_last_active_puts_bumped_session_first(tmp_path, capsys):
    from hermes_state import SessionDB
    tdb = SessionDB(db_path=tmp_path / "recent.db")
    try:
        tdb.create_session("old", "cli")
        tdb.append_message("old", "user", "old hello")
        tdb.create_session("new", "cli")
        tdb.append_message("new", "user", "new hello")
        tdb.append_message("old", "user", "old again")  # bumps old past new
        _, default_out = _list_ns(tdb, capsys)
        _, sorted_out = _list_ns(tdb, capsys, sort="last-active")
    finally:
        tdb.close()
    assert default_out.splitlines()[2].endswith("new")
    assert sorted_out.splitlines()[2].endswith("old")


def test_columns_table_shows_only_requested(db, capsys):
    _, out = _list_ns(db, capsys, columns="id,title")
    header, *rows = out.splitlines()
    assert "Title" in header and "ID" in header
    assert "Preview" not in header and "Workspace" not in header
    assert len([r for r in rows[1:] if r.strip()]) == 6


def test_unknown_column_is_rejected(db, capsys):
    rc, out = _list_ns(db, capsys, columns="bogus")
    assert rc == 1
    assert "unknown column" in out
