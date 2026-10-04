"""#132939: resume display must not repeat a compacted tail's tool result.

Compaction archives the full tool-result row and carries a pruned copy
forward under the same ``message_uid``. The shared
``_dedupe_display_generations`` keeps both on purpose (tool RESULT rows keep
their payload in the display key so compacted reads/exports never lose the
archived full output -- #117750), so the resume display projection
(``_history_to_messages``) collapses the pair itself: the first (full) copy
projects once. Tmp fixtures only.
"""

import json

from tui_gateway.server import _history_to_messages


def _assistant_call(call_id="stable-call"):
    return {
        "role": "assistant",
        "content": "Earlier progress",
        "timestamp": 101.0,
        "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "write_file", "arguments": json.dumps({"path": "a.txt"})},
        }],
    }


def _tool_result(content, uid, call_id="stable-call"):
    return {
        "role": "tool", "content": content, "tool_call_id": call_id,
        "tool_name": "write_file", "timestamp": 102.0, "message_uid": uid,
    }


def test_pruned_tail_tool_copy_projects_once_keeping_full_output():
    """Archived full result + pruned carried copy (same uid) -> one row, full content."""
    full = "R" * 5000
    history = [
        _assistant_call(),
        _tool_result(full, "tool-uid-1"),
        _tool_result("short result", "tool-uid-1"),
        {"role": "assistant", "content": "Later answer", "timestamp": 200.0},
    ]
    projected = _history_to_messages(history)
    tool_msgs = [m for m in projected if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["content"] == full


def test_distinct_tool_uids_still_project_twice():
    """Different message_uids are different events: no over-collapse."""
    history = [
        _assistant_call(),
        _tool_result("first output", "tool-uid-1"),
        _tool_result("second output", "tool-uid-2"),
    ]
    projected = _history_to_messages(history)
    assert len([m for m in projected if m["role"] == "tool"]) == 2


def test_tool_rows_without_uid_always_project():
    """Uid-less rows (older builds) never merge: a missing uid collapses nothing."""
    first = _tool_result("first output", "tool-uid-1")
    second = _tool_result("second output", "tool-uid-1")
    del first["message_uid"]
    del second["message_uid"]
    projected = _history_to_messages([_assistant_call(), first, second])
    assert len([m for m in projected if m["role"] == "tool"]) == 2


def test_compacted_session_resume_projects_pruned_tail_once(tmp_path):
    """End to end on tmp state: prune + compact, then the resume projection shows one tool row."""
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    try:
        sid = "pruned-tail-resume"
        db.create_session(sid, source="test")
        db.append_messages_batch(sid, [
            {"role": "user", "content": "start", "timestamp": 100.0},
            _assistant_call(),
            _tool_result("R" * 5000, "tool-uid-1"),
            {"role": "assistant", "content": "Later answer", "timestamp": 200.0},
        ])
        history = db.get_messages_as_conversation(sid, include_row_ids=True)
        history[1]["tool_calls"][0]["function"]["arguments"] = json.dumps({"path": "short"})
        history[2]["content"] = "short result"
        db.archive_and_compact(sid, history)
        compacted = db.get_messages_as_conversation(sid, include_row_ids=True, include_compacted=True)
        uids = [m.get("message_uid") for m in compacted if m["role"] == "tool"]
        assert len(uids) == 2 and uids[0] == uids[1]
        projected = _history_to_messages(compacted)
        tool_msgs = [m for m in projected if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["content"] == "R" * 5000
    finally:
        db.close()
