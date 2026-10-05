"""Regression tests for #132989.

``gateway stop`` drained api_server runs and deferred executor workers on the
chat budget (``restart_drain_timeout``, default 0s): the wait loop held
``(agents or api or deferred) and now < deadline``, so with the shipped
default the deadline was already expired and an in-flight API run was
force-interrupted after a 0.00s drain. An API run has no resume path — the
caller is an external HTTP client, not an announced chat session pre-marked
resume_pending — so killing it mid-flight is a permanent externally-visible
failure, the same shape as the killed cron run from #82161. The restart path
never hit this: ``_await_active_work_before_restart`` holds api/deferred up
to ``restart_after_turn_timeout`` (1800s) before ``stop()``; only the stop
path amputated them.

API/deferred work now rides the already-clamped extended deadline
(``resolve_cron_drain_budget``): same watchdog-leash clamp as cron, no new
config key. ``cron_drain_timeout=0`` still opts the floor out for all three.
"""

import asyncio

import pytest

from tests.gateway.restart_test_helpers import make_restart_runner


def _stub_api_runs(runner, live: set):
    runner._active_api_run_count = lambda: len(live)  # noqa: E731


class TestStopDrainWaitsForApiRuns:
    """The reported repro: default config, api-only workload."""

    @pytest.mark.asyncio
    async def test_zero_drain_timeout_still_waits_for_api_run(self):
        runner, _adapter = make_restart_runner()
        live = {"run-1"}
        _stub_api_runs(runner, live)

        async def finish_run():
            await asyncio.sleep(0.12)
            live.discard("run-1")

        task = asyncio.create_task(finish_run())
        # restart_drain_timeout=0 (the shipped default) with a 2s extended floor.
        _snapshot, timed_out = await runner._drain_active_agents(0.0, 2.0)
        await task

        assert timed_out is False, (
            "drain returned timed_out=True with an api_server run in flight — "
            "this is the 0.00s stop drain from #132989"
        )
        assert runner._active_api_run_count() == 0

    @pytest.mark.asyncio
    async def test_zero_drain_timeout_still_waits_for_deferred_worker(self):
        runner, _adapter = make_restart_runner()
        loop = asyncio.get_running_loop()
        worker = loop.create_future()
        runner._deferred_agent_workers = {worker: object()}

        async def finish_worker():
            await asyncio.sleep(0.12)
            worker.set_result(None)

        task = asyncio.create_task(finish_worker())
        _snapshot, timed_out = await runner._drain_active_agents(0.0, 2.0)
        await task

        assert timed_out is False, (
            "drain returned timed_out=True with a deferred agent worker in "
            "flight — same 0.00s stop drain as #132989"
        )
        assert runner._active_deferred_agent_worker_count() == 0

    @pytest.mark.asyncio
    async def test_api_run_that_never_finishes_still_loses(self):
        """A run that never finishes must still lose, or a wedged API worker
        would pin the gateway inside the drain until the watchdog SIGKILLs it
        mid-cleanup — the same deadlock the cron bounded-floor test guards."""
        runner, _adapter = make_restart_runner()
        _stub_api_runs(runner, {"wedged-run"})

        _snapshot, timed_out = await runner._drain_active_agents(0.0, 0.2)

        assert timed_out is True
        assert runner._active_api_run_count() == 1

    @pytest.mark.asyncio
    async def test_chat_only_workload_still_interrupted_immediately(self):
        """The extended floor must not become a chat-turn grace window —
        ``restart_drain_timeout: 0`` still means interrupt chat immediately,
        even while api/deferred/cron ride the longer deadline."""
        runner, _adapter = make_restart_runner()
        runner._running_agents = {"sess-1": object()}

        loop = asyncio.get_running_loop()
        before = loop.time()
        _snapshot, timed_out = await runner._drain_active_agents(0.0, 30.0)
        elapsed = loop.time() - before

        assert timed_out is True
        assert elapsed < 1.0, f"chat-only drain waited {elapsed:.2f}s on a 0s budget"

    @pytest.mark.asyncio
    async def test_single_arg_call_keeps_the_shared_budget(self):
        """Callers that pass one argument keep the pre-fix semantics: with no
        extended deadline, api work drains on the chat budget."""
        runner, _adapter = make_restart_runner()
        _stub_api_runs(runner, {"run-1"})

        _snapshot, timed_out = await runner._drain_active_agents(0.0)

        assert timed_out is True
