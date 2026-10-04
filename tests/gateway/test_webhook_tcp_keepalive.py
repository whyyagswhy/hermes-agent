"""connect() must disable aiohttp's handler-level TCP keepalive.

Regression guard: the gateway inbound webhook listener built its
web.Application with no handler_args, so aiohttp 3.14.3's
RequestHandler.connection_made called tcp_helpers.tcp_keepalive() — a
bare setsockopt with no OSError guard (unlike tcp_nodelay) — and a
firewall-torn socket raised unhandled OSError/EINVAL that masked the
real cause. The fix passes handler_args={"tcp_keepalive": False} so the
handler path never touches SO_KEEPALIVE.

Both tests use socket/transport-level mocks — no real network.
"""

import errno
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter


def _make_adapter() -> WebhookAdapter:
    config = PlatformConfig(
        enabled=True,
        extra={
            "host": "127.0.0.1",
            "port": 0,
            "routes": {"r1": {"secret": "real-secret-abc123", "prompt": "x"}},
        },
    )
    return WebhookAdapter(config)


async def _connect_app_kwargs(adapter: WebhookAdapter) -> dict:
    """Run connect() with the runner/site fully mocked; return web.Application kwargs."""
    with (
        patch("gateway.platforms.webhook.web.Application") as mock_app_cls,
        patch("gateway.platforms.webhook.web.AppRunner") as mock_runner_cls,
        patch("gateway.platforms.webhook.start_tcp_site", new=AsyncMock()),
        patch.object(adapter, "_reload_dynamic_routes"),
    ):
        runner = MagicMock()
        runner.setup = AsyncMock()
        runner.cleanup = AsyncMock()
        mock_runner_cls.return_value = runner
        assert await adapter.connect() is True
        await adapter.disconnect()
    assert mock_app_cls.call_count == 1
    return dict(mock_app_cls.call_args.kwargs)


class TestWebhookHandlerTcpKeepalive:
    @pytest.mark.asyncio
    async def test_connect_disables_handler_tcp_keepalive(self):
        """connect() must build the app with handler_args disabling tcp_keepalive."""
        kwargs = await _connect_app_kwargs(_make_adapter())
        assert kwargs.get("handler_args") == {"tcp_keepalive": False}

    @pytest.mark.asyncio
    async def test_torn_socket_connection_made_raises_no_einval(self):
        """A firewall-torn socket must not raise EINVAL out of the handler path.

        Replays the real RequestHandler.connection_made (aiohttp 3.14.3)
        with the exact handler_args connect() passes, driving a mocked
        transport whose socket raises EINVAL on setsockopt — the
        firewall-torn case. Without the fix this raises OSError out of
        connection_made; with the fix the keepalive branch is skipped.
        """
        import asyncio

        from aiohttp.web_protocol import RequestHandler

        kwargs = await _connect_app_kwargs(_make_adapter())
        handler_args = dict(kwargs.get("handler_args") or {})

        torn_sock = MagicMock()
        torn_sock.setsockopt.side_effect = OSError(errno.EINVAL, "Invalid argument")
        transport = MagicMock()
        transport.get_extra_info.return_value = torn_sock

        loop = asyncio.get_running_loop()
        handler = RequestHandler(MagicMock(), loop=loop, **handler_args)
        with patch.object(RequestHandler, "start", new=AsyncMock()):
            handler.connection_made(transport)  # must not raise
            task = handler._task_handler
            if task is not None:
                task.cancel()
