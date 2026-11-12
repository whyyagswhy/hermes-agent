"""#154244 — callable api_key must not share the keyless in-process memo slot or keyless disk cache.

Bug 1: _endpoint_memo_key returns (url, "") for a callable, same as no-key.
       A keyless 401 caches {} there; the next callable call hits it and returns {} with no request.
Bug 2: _endpoint_disk_cache_{get,put} are keyed by URL only.
       A keyless successful probe writes a disk entry the callable caller reads without its own request.
"""
from unittest.mock import MagicMock, patch

import agent.model_metadata as mm

URL = "https://keycmd.example/v1"


def _streamed(response):
    ctx = MagicMock()
    ctx.__enter__.return_value = response
    return ctx


def _keyless_401():
    r = MagicMock()
    r.status_code = 401
    r.raise_for_status.side_effect = RuntimeError("401")
    return _streamed(r)


def _ok_response(context_length, model_id="m-x"):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = {"data": [{"id": model_id, "context_length": context_length}]}
    return _streamed(r)


class TestCallableKeyMemoSlot:
    """Bug 1: callable keys must not share the keyless (url, '') in-process memo slot."""

    def setup_method(self):
        mm._endpoint_model_metadata_cache.clear()
        mm._endpoint_model_metadata_cache_time.clear()

    def test_keyless_401_does_not_poison_callable_key_slot(self):
        """After a keyless 401 caches {}, a callable key must still make its own network request."""
        my_key_cmd = lambda: "tok-secret"

        with patch(
            "agent.model_metadata.model_metadata_http.stream",
            side_effect=[_keyless_401(), _ok_response(777_777)],
        ) as mock_stream:
            keyless = mm.fetch_endpoint_model_metadata(URL, api_key="")
            keyed = mm.fetch_endpoint_model_metadata(URL, api_key=my_key_cmd)

        assert keyless == {}
        assert keyed.get("m-x", {}).get("context_length") == 777_777, (
            "callable key returned wrong window; it hit the keyless 401 slot"
        )
        assert mock_stream.call_count == 2, (
            f"expected 2 HTTP requests (keyless 401 + callable probe); got {mock_stream.call_count}"
        )

    def test_two_callable_keys_get_independent_slots(self):
        """Two different callable keys do not share one another's cached results."""
        key_a = lambda: "tok-a"
        key_b = lambda: "tok-b"

        with patch(
            "agent.model_metadata.model_metadata_http.stream",
            side_effect=[_ok_response(100_000, "m-a"), _ok_response(200_000, "m-b")],
        ) as mock_stream:
            result_a = mm.fetch_endpoint_model_metadata(URL, api_key=key_a)
            result_b = mm.fetch_endpoint_model_metadata(URL, api_key=key_b)

        assert result_a.get("m-a", {}).get("context_length") == 100_000
        assert result_b.get("m-b", {}).get("context_length") == 200_000
        assert mock_stream.call_count == 2


class TestCallableKeyDiskCache:
    """Bug 2: a keyless disk memo must not be served to a callable-key lookup."""

    def setup_method(self):
        mm._endpoint_model_metadata_cache.clear()
        mm._endpoint_model_metadata_cache_time.clear()

    def test_keyless_disk_cache_does_not_serve_callable_key(self, tmp_path, monkeypatch):
        """A callable-key probe arriving after a keyless disk write must fetch from the endpoint."""
        monkeypatch.setattr(
            mm, "_get_endpoint_metadata_cache_path",
            lambda: tmp_path / "endpoint_model_metadata.json",
        )
        my_key_cmd = lambda: "tok-secret"

        with patch(
            "agent.model_metadata.model_metadata_http.stream",
            side_effect=[_ok_response(111_111), _ok_response(777_777)],
        ) as mock_stream:
            # Keyless probe writes disk cache with 111_111
            keyless = mm.fetch_endpoint_model_metadata(URL, api_key="")
            # Simulate new process: clear in-process cache only
            mm._endpoint_model_metadata_cache.clear()
            mm._endpoint_model_metadata_cache_time.clear()
            # Callable key must not read the keyless disk entry
            keyed = mm.fetch_endpoint_model_metadata(URL, api_key=my_key_cmd)

        assert keyless.get("m-x", {}).get("context_length") == 111_111
        assert keyed.get("m-x", {}).get("context_length") == 777_777, (
            "callable key got the keyless disk-cached context window; disk cache is not key-isolated"
        )
        assert mock_stream.call_count == 2, (
            f"expected 2 HTTP requests (keyless + callable); got {mock_stream.call_count}"
        )
