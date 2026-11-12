"""#154513 — _history_to_messages re-parses every shell command on every read.

The five pure helpers (_strip_shell_pipe_tail, _split_shell_compound,
_clean_shell_segment, _shell_head_word, _is_shell_boundary_echo) each walk
_scan_quoted character-by-character.  After the fix they carry @lru_cache so
a repeated call with the same string argument is a cache hit.

RED on main: none of the five functions have .cache_info.
GREEN on fix: all five have .cache_info; second call with same arg is a hit.
"""
import functools
import pytest

from agent.display import (
    _clean_shell_segment,
    _is_shell_boundary_echo,
    _shell_head_word,
    _split_shell_compound,
    _strip_shell_pipe_tail,
)

_HELPERS = [
    _split_shell_compound,
    _strip_shell_pipe_tail,
    _clean_shell_segment,
    _shell_head_word,
    _is_shell_boundary_echo,
]

CMD = "export PATH=$HOME/.local/bin:$PATH && python -m pytest tests/ -x -q"
SEGMENT = "python -m pytest tests/ -x -q"


@pytest.mark.parametrize("fn", _HELPERS, ids=lambda f: f.__name__)
def test_helper_has_lru_cache(fn):
    """Every pure shell-display helper must carry @lru_cache."""
    assert hasattr(fn, "cache_info"), (
        f"{fn.__name__} has no .cache_info — apply @functools.lru_cache(maxsize=4096)"
    )


@pytest.mark.parametrize("fn,arg", [
    (_split_shell_compound, CMD),
    (_strip_shell_pipe_tail, SEGMENT),
    (_clean_shell_segment, SEGMENT),
    (_shell_head_word, SEGMENT),
    (_is_shell_boundary_echo, SEGMENT),
], ids=lambda x: getattr(x, '__name__', repr(x)[:30]))
def test_second_call_is_cache_hit(fn, arg):
    """Calling a helper twice with the same string must hit the LRU cache."""
    fn.cache_clear()
    fn(arg)
    before = fn.cache_info()
    fn(arg)
    after = fn.cache_info()
    assert after.hits == before.hits + 1, (
        f"{fn.__name__}: expected 1 cache hit on second call, got {after.hits - before.hits}"
    )
