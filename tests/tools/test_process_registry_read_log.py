"""read_log paging coercion (#141988): red-proven tests, green neighbors."""
import time

import pytest

from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.fixture()
def registry():
    return ProcessRegistry()


def _make_session(output="", sid="proc_readlog"):
    return ProcessSession(
        id=sid,
        command="echo hello",
        task_id="t1",
        started_at=time.time(),
        output_buffer=output,
    )


def _hundred_lines():
    return "\n".join([f"line {i}" for i in range(100)])


class TestReadLogPagingCoercion:
    def test_read_with_float_offset_and_limit(self, registry):
        s = _make_session(output=_hundred_lines())
        registry._running[s.id] = s
        result = registry.read_log(s.id, offset=10.0, limit=5.0)
        assert result["output"].splitlines() == [f"line {i}" for i in range(10, 15)]

    def test_read_with_string_offset_and_limit(self, registry):
        s = _make_session(output=_hundred_lines())
        registry._running[s.id] = s
        result = registry.read_log(s.id, offset="10", limit="5")
        assert result["output"].splitlines() == [f"line {i}" for i in range(10, 15)]

    def test_read_with_garbage_offset_returns_error(self, registry):
        s = _make_session(output=_hundred_lines())
        registry._running[s.id] = s
        result = registry.read_log(s.id, offset="ten", limit=5)
        assert result["status"] == "error"

    def test_read_with_none_limit_reads_default_window(self, registry):
        s = _make_session(output=_hundred_lines())
        registry._running[s.id] = s
        result = registry.read_log(s.id, limit=None)
        assert result["total_lines"] == 100
        assert len(result["output"].splitlines()) == 100
