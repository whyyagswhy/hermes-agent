"""#154492: child_task_message strips the goal before joining so KV-cache
normalization in turn_request_assembly is idempotent and goal whitespace is
handled the same whether context is supplied or not.

The normalization pass (turn_request_assembly.py L196) calls .strip() on every
string message.  Before this fix, supplying context caused the goal's LEADING
whitespace to survive as internal text while the TRAILING whitespace was removed;
without context the full .strip() removed both.  Callers were given different
effective goal text depending on whether context was present.
"""

import pytest


@pytest.mark.parametrize("context", [None, "Fixture context"])
def test_goal_trailing_whitespace_stripped_before_join(context):
    """Trailing whitespace on the goal must be removed by child_task_message itself."""
    from tools.delegate_tool_progress import child_task_message

    result = child_task_message("count the files  \n", context)
    # Extract the goal portion from the result
    if context:
        assert "TASK:\n" in result
        goal_portion = result.split("TASK:\n", 1)[1]
    else:
        goal_portion = result

    assert not goal_portion.endswith(" "), "trailing space survived"
    assert not goal_portion.endswith("\n"), "trailing newline survived"
    assert goal_portion == "count the files"


@pytest.mark.parametrize("context", [None, "Fixture context"])
def test_goal_leading_whitespace_stripped_symmetrically(context):
    """Leading whitespace on the goal must be stripped regardless of context presence."""
    from tools.delegate_tool_progress import child_task_message

    result = child_task_message("  Inspect this specific failure.", context)
    if context:
        assert "TASK:\n" in result
        goal_portion = result.split("TASK:\n", 1)[1]
    else:
        goal_portion = result

    assert not goal_portion.startswith(" "), f"leading space survived (context={context!r})"
    assert goal_portion == "Inspect this specific failure."


def test_goal_with_context_and_goal_without_context_deliver_same_text():
    """The normalised goal text is the same whether context is present or not."""
    from tools.delegate_tool_progress import child_task_message

    goal = "  Inspect this specific failure. \n"
    result_no_ctx = child_task_message(goal, None)
    result_with_ctx = child_task_message(goal, "Fixture context")
    goal_in_ctx = result_with_ctx.split("TASK:\n", 1)[1]

    assert result_no_ctx == goal_in_ctx == goal.strip()


def test_cache_prefix_still_works_after_strip():
    """Sibling cache behaviour must be unaffected: same context → same prefix bytes."""
    from agent.prompt_cache_boundary import register_stable_prefix
    from tools.delegate_tool_progress import child_task_message

    brief = "Repo layout: " + "x" * 200
    one = child_task_message("find the bug", brief)
    two = child_task_message("write the test", brief)

    prefix = f"CONTEXT:\n{brief}\n\nTASK:\n"
    assert one.startswith(prefix)
    assert two.startswith(prefix)
    assert one.endswith("find the bug")
    assert two.endswith("write the test")
