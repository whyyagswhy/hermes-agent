"""A single inbound WebSocket frame bigger than the bridge cap is refused before parsing."""
from __future__ import annotations

import asyncio
import os
import tempfile

from hermes_cli.web_routers import display
from tools.bot_desktop import lease


class _Ws:
    def __init__(self, messages):
        self._messages = list(messages)
        self.closes = []
        self.sent = []

    async def accept(self):
        pass

    async def receive(self):
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(3600)
        return {'type': 'websocket.disconnect', 'code': 1000}

    async def send_bytes(self, data):
        self.sent.append(data)

    async def close(self, code=1000, reason=''):
        self.closes.append((code, reason))


async def _run(messages, home):
    received = bytearray()
    sock = os.path.join(home, 'bot-desktop', 'rfb.sock')
    os.makedirs(os.path.dirname(sock), exist_ok=True)

    async def _xvnc(reader, writer):
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                return
            received.extend(chunk)

    server = await asyncio.start_unix_server(_xvnc, path=sock)
    try:
        ws = _Ws(messages)
        await display._bridge(ws, {'hermes_home': home, 'viewer_id': 'desk-1'})
        await asyncio.sleep(0.2)
    finally:
        server.close()
    return ws, bytes(received)


def _bridge_case(messages):
    lease._reset_for_tests()
    try:
        with tempfile.TemporaryDirectory() as home:
            return asyncio.run(_run(messages, home))
    finally:
        lease._reset_for_tests()


def test_oversize_frame_is_refused_before_parsing():
    big = bytes([3, 0]) + bytes(2 * 1024 * 1024)
    ws, received = _bridge_case([{'bytes': big}])
    assert any(code == 1009 for code, _reason in ws.closes), ws.closes
    assert received == bytes(0)


def test_small_frames_still_stream_to_xvnc():
    handshake = bytes([82, 70, 66, 32, 48, 48, 51, 46, 48, 48, 56, 10]) + bytes([1, 1])
    fur = bytes([3, 0]) + bytes(8)
    ws, received = _bridge_case([{'bytes': handshake}, {'bytes': fur}, {'type': 'websocket.disconnect', 'code': 1000}])
    assert received == handshake + fur
    assert ws.closes and ws.closes[-1][0] == 1000, ws.closes
