"""Untrusted RFB clipboard lengths are bounded without breaking stream framing."""

import pytest

from tools.bot_desktop.rfb_filter import RfbClientFilter

_HANDSHAKE = b"RFB 003.008\n\x01\x01"
_CLIPBOARD_LIMIT = 256 * 1024


def clipboard_header(length):
    return b"\x06\x00\x00\x00" + length.to_bytes(4, "big", signed=True)


@pytest.mark.parametrize("length", [_CLIPBOARD_LIMIT + 1, -_CLIPBOARD_LIMIT - 1, 2**31 - 1, -(2**31)])
@pytest.mark.parametrize("holder", [False, True])
def test_oversized_clipboard_is_rejected_at_header_without_waiting_for_payload(length, holder):
    parser = RfbClientFilter(lambda: holder)
    parser.feed(_HANDSHAKE)
    header = clipboard_header(length)
    for byte in header[:-1]:
        assert parser.feed(bytes([byte])) == b""
    with pytest.raises(ValueError, match="clipboard"):
        parser.feed(header[-1:])


@pytest.mark.parametrize("extended", [False, True])
@pytest.mark.parametrize("holder", [False, True])
def test_bounded_clipboards_and_watch_requests_survive_fragmentation_and_large_coalesced_chunks(extended, holder):
    parser = RfbClientFilter(lambda: holder)
    assert parser.feed(_HANDSHAKE) == _HANDSHAKE
    payload = b"x" * _CLIPBOARD_LIMIT
    message = clipboard_header(-len(payload) if extended else len(payload)) + payload
    refresh = b"\x03\x00" + b"\x00" * 8
    # A WebSocket frame can contain several valid messages, not just one.
    stream = message + refresh + message + refresh
    received = parser.feed(stream[:7]) + parser.feed(stream[7:])
    expected = (message + refresh) * 2 if holder else refresh * 2
    assert received == expected
    assert parser.feed(refresh) == refresh


_FUR = bytes([3, 0]) + bytes(8)
_FIVE_MIB = 5 * 1024 * 1024


@pytest.mark.parametrize('holder', [False, True])
def test_single_feed_batch_output_is_bounded(holder):
    parser = RfbClientFilter(lambda: holder)
    parser.feed(_HANDSHAKE)
    batch = _FUR * (_FIVE_MIB // len(_FUR))
    assert len(batch) > _FIVE_MIB - len(_FUR)
    with pytest.raises(ValueError, match='batch'):
        parser.feed(batch)


def test_chunked_streaming_keeps_peak_bounded():
    parser = RfbClientFilter(lambda: True)
    parser.feed(_HANDSHAKE)
    stream = _FUR * (_FIVE_MIB // len(_FUR))
    peaks = []
    out_parts = []
    for i in range(0, len(stream), 65536):
        part = parser.feed(stream[i:i + 65536])
        peaks.append(len(part))
        out_parts.append(part)
    assert max(peaks) <= 1024 * 1024
    assert type(out_parts[0])().join(out_parts) == stream
