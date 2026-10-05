"""Regression tests for issue #133348: Telegram DM topic document silently dropped.

``_handle_media_message`` read ``update.message`` directly while the text/location
handlers use ``_effective_update_message`` (which also covers ``effective_message``,
e.g. channel posts / topic-routed updates). A media update with ``message=None``
but ``effective_message`` set was silently dropped. Every early return must log.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.event import MessageType

from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _make_file_obj(data: bytes = b"hello"):
    f = AsyncMock()
    f.download_as_bytearray = AsyncMock(return_value=bytearray(data))
    f.file_path = "documents/file.pdf"
    return f


def _make_message(document=None, caption=None):
    msg = MagicMock()
    msg.message_id = 42
    msg.text = caption or ""
    msg.caption = caption
    msg.date = None
    msg.photo = None
    msg.video = None
    msg.audio = None
    msg.voice = None
    msg.sticker = None
    msg.document = document
    msg.media_group_id = None
    msg.chat = MagicMock()
    msg.chat.id = 100
    msg.chat.type = "private"
    msg.chat.title = None
    msg.chat.full_name = "Test User"
    msg.from_user = MagicMock()
    msg.from_user.id = 1
    msg.from_user.full_name = "Test User"
    msg.message_thread_id = None
    msg.reply_text = AsyncMock()
    return msg


def _make_document():
    doc = MagicMock()
    doc.file_name = "report.pdf"
    doc.mime_type = "application/pdf"
    doc.file_size = 1024
    doc.get_file = AsyncMock(return_value=_make_file_obj())
    return doc


@pytest.fixture()
def adapter():
    config = PlatformConfig(enabled=True, token="fake-token")
    a = TelegramAdapter(config)
    a.handle_message = AsyncMock()
    a._is_callback_user_authorized = lambda user_id, **_kw: True
    return a


@pytest.fixture(autouse=True)
def _redirect_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache"
    )


class TestMediaEffectiveMessage:
    @pytest.mark.asyncio
    async def test_document_with_message_none_but_effective_set_is_delivered(self, adapter):
        """The #133348 regression: message=None, effective_message=document -> delivered."""
        msg = _make_message(document=_make_document())
        update = SimpleNamespace(message=None, effective_message=msg, update_id=7)
        await adapter._handle_media_message(update, MagicMock())
        assert adapter.handle_message.call_count == 1
        event = adapter.handle_message.call_args[0][0]
        assert event.message_type == MessageType.DOCUMENT

    @pytest.mark.asyncio
    async def test_no_message_at_all_logs_and_drops(self, adapter, caplog):
        """Both message and effective_message missing -> drop, but with a log record."""
        update = SimpleNamespace(message=None, effective_message=None, update_id=8)
        with caplog.at_level(logging.DEBUG, logger="plugins.platforms.telegram.adapter"):
            await adapter._handle_media_message(update, MagicMock())
        assert adapter.handle_message.call_count == 0
        assert any(r.name == "plugins.platforms.telegram.adapter" for r in caplog.records)

    @pytest.mark.asyncio
    async def test_gate_rejection_logs_and_drops(self, adapter, caplog, monkeypatch):
        """_should_process_message False without observation -> drop, but with a log record."""
        msg = _make_message(document=_make_document())
        update = SimpleNamespace(message=None, effective_message=msg, update_id=9)
        monkeypatch.setattr(adapter, "_should_process_message", lambda *a, **k: False)
        monkeypatch.setattr(adapter, "_should_observe_unmentioned_group_message", lambda *a, **k: False)
        with caplog.at_level(logging.DEBUG, logger="plugins.platforms.telegram.adapter"):
            await adapter._handle_media_message(update, MagicMock())
        assert adapter.handle_message.call_count == 0
        assert any(r.name == "plugins.platforms.telegram.adapter" for r in caplog.records)
