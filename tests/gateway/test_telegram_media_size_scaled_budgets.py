"""Size-scaled Telegram media send budgets (issue #133093).

Fixed per-send (60s read) and whole-request (300s deadline) budgets fail
large uploads on slow links: a 50MB file at ~2 Mbit/s needs ~200s of pure
bandwidth before server processing. Budgets must scale with payload size
while flooring at the current defaults so small sends are unchanged.
"""

from __future__ import annotations

import asyncio
import io
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.telegram import adapter as tg  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402

FIFTY_MB = 50 * 1024 * 1024


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr("tools.url_safety.is_safe_url", lambda *a, **k: True)
    a = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    a._bot = MagicMock()
    a._metadata_thread_id = lambda metadata: None
    a._thread_kwargs_for_send = lambda *args, **kwargs: {}
    a._notification_kwargs = lambda metadata: {}
    a._reply_to_message_id_for_send = lambda *args, **kwargs: None

    async def _direct(fn, payload, *args, **kwargs):
        return await fn(**payload)

    a._send_with_dm_topic_reply_anchor_retry = _direct
    return a


def test_budgets_floor_at_defaults_for_unknown_size():
    read, deadline = tg._media_send_budgets_for_size(None)
    assert read == tg._MEDIA_SEND_READ_TIMEOUT
    assert deadline == tg._MEDIA_SEND_DEADLINE
    read, deadline = tg._media_send_budgets_for_size(0)
    assert (read, deadline) == (tg._MEDIA_SEND_READ_TIMEOUT, tg._MEDIA_SEND_DEADLINE)
    read, deadline = tg._media_send_budgets_for_size(1024)
    assert (read, deadline) == (tg._MEDIA_SEND_READ_TIMEOUT, tg._MEDIA_SEND_DEADLINE)


def test_budgets_scale_with_size():
    small_read, small_deadline = tg._media_send_budgets_for_size(1024)
    big_read, big_deadline = tg._media_send_budgets_for_size(FIFTY_MB)
    assert big_read > small_read
    assert big_deadline > small_deadline
    bigger_read, bigger_deadline = tg._media_send_budgets_for_size(2 * FIFTY_MB)
    assert bigger_read >= big_read
    assert bigger_deadline >= big_deadline


def test_budgets_env_configurable(monkeypatch):
    monkeypatch.setenv("HERMES_TELEGRAM_MEDIA_READ_TIMEOUT", "90")
    monkeypatch.setenv("HERMES_TELEGRAM_MEDIA_SEND_DEADLINE", "400")
    read, deadline = tg._media_send_budgets_for_size(None)
    assert read == 90.0
    assert deadline == 400.0


@pytest.mark.asyncio
async def test_media_send_kwargs_scales_read_timeout(adapter):
    _, small = adapter._media_send_kwargs("123", None, None)
    assert small["read_timeout"] == tg._MEDIA_SEND_READ_TIMEOUT
    _, big = adapter._media_send_kwargs("123", None, None, size_bytes=FIFTY_MB)
    expected_read, _ = tg._media_send_budgets_for_size(FIFTY_MB)
    assert big["read_timeout"] == expected_read
    assert big["read_timeout"] > tg._MEDIA_SEND_READ_TIMEOUT


@pytest.mark.asyncio
async def test_dm_retry_uses_scaled_deadline(adapter, monkeypatch):
    seen = {}

    async def _capture(awaitable, timeout, **kwargs):
        seen["timeout"] = timeout
        return await awaitable

    monkeypatch.setattr(tg, "_await_with_thread_deadline", _capture)
    # Restore the real retry method (fixture replaced it with a direct call).
    real = TelegramAdapter._send_with_dm_topic_reply_anchor_retry.__get__(
        adapter, TelegramAdapter
    )
    adapter._send_with_dm_topic_reply_anchor_retry = real
    adapter._should_retry_without_dm_topic_reply_anchor = lambda *a: False

    async def _ok(**kwargs):
        return "sent"

    expected_deadline = tg._media_send_budgets_for_size(FIFTY_MB)[1]
    out = await adapter._send_with_dm_topic_reply_anchor_retry(
        _ok, {"chat_id": 123}, None, None, "document", deadline=expected_deadline
    )
    assert out == "sent"
    assert seen["timeout"] == expected_deadline


@pytest.mark.asyncio
async def test_send_media_scales_both_budgets(adapter, monkeypatch):
    seen_kwargs = {}
    seen_deadline = {}

    async def _capture_retry(
        send_fn,
        send_kwargs,
        metadata,
        reply_to_id,
        label,
        reset_media=None,
        deadline=None,
    ):
        seen_kwargs.update(send_kwargs)
        seen_deadline["deadline"] = deadline
        return "sent"

    adapter._send_with_dm_topic_reply_anchor_retry = _capture_retry
    payload = io.BytesIO(b"x" * (5 * 1024 * 1024))
    out = await adapter._send_media(
        adapter._bot.send_document, "123", None, None, "document", document=payload
    )
    assert out == "sent"
    expected_read, expected_deadline = tg._media_send_budgets_for_size(5 * 1024 * 1024)
    assert seen_kwargs["read_timeout"] == expected_read
    assert seen_deadline["deadline"] == expected_deadline


@pytest.mark.asyncio
async def test_timeout_failure_notes_may_still_be_in_progress(
    adapter, monkeypatch, caplog
):
    real = TelegramAdapter._send_with_dm_topic_reply_anchor_retry.__get__(
        adapter, TelegramAdapter
    )
    adapter._send_with_dm_topic_reply_anchor_retry = real
    adapter._should_retry_without_dm_topic_reply_anchor = lambda *a: False

    async def _hang(**kwargs):
        raise asyncio.TimeoutError("timed out after 300s (telegram-media-send)")

    with caplog.at_level(logging.WARNING, logger="plugins.platforms.telegram.adapter"):
        with pytest.raises(asyncio.TimeoutError):
            await adapter._send_with_dm_topic_reply_anchor_retry(
                _hang, {"chat_id": 123}, None, None, "document"
            )
    assert any("may still" in r.getMessage().lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_standalone_media_send_scales_read_timeout(tmp_path):
    """The standalone _send_telegram path mirrors the scaled read budget."""
    from tools import send_message_senders as senders

    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (20 * 1024 * 1024))
    seen = {}

    async def _send_document(chat_id, document, **kwargs):
        seen.update(kwargs)
        msg = MagicMock()
        msg.message_id = 7
        return msg

    bot = MagicMock()
    bot.send_document = AsyncMock(side_effect=_send_document)
    out = await senders._telegram_send_one_media(
        bot,
        123,
        str(big),
        False,
        caption=None,
        parse_mode=None,
        has_html=False,
        thread_kwargs={},
        force_document=True,
    )
    assert out.message_id == 7
    assert seen.get("read_timeout", 0) > 60.0
