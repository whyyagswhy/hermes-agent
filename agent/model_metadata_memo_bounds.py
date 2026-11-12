"""Memory bounds for ``agent.model_metadata``'s per-credential in-process memos.

The memos are keyed by a credential fingerprint, and minted keys (Nous agent keys, OAuth access
tokens) rotate, so each rotation adds a key the process never reads again. Only entries no reader
can use are shed: expired ones, or the oldest past a count far above any real working set.
"""

import threading
import time
from typing import Any

from agent.cache_bounds import drop_expired, evict_oldest

# Same bound as ``agent.codex_service_tiers._MAX_CATALOGS`` for the same token-fingerprint keys.
CODEX_CACHE_MAX = 128
# Every writer of a paired memo (value + stamp, context + max-context) holds this from sweep to publish:
# a sweep pops two tables, and a refresh landing between the pops would keep half an entry.
MEMO_LOCK = threading.Lock()


def remember_endpoint_models(memo_key: tuple[str, str], cache: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Publish a ``/models`` catalog, sweeping expired ones (the reader refetches those anyway)."""
    from agent import model_metadata as mm  # facade seam: tests swap the memo dicts there
    with MEMO_LOCK:
        now = time.time()
        drop_expired(mm._endpoint_model_metadata_cache, mm._endpoint_model_metadata_cache_time, mm._ENDPOINT_MODEL_CACHE_TTL, now)
        mm._endpoint_model_metadata_cache[memo_key] = cache
        mm._endpoint_model_metadata_cache_time[memo_key] = now
    return cache


def bound_codex_caches(cache_key: str) -> None:
    """Before storing ``cache_key``, evict the stalest Codex token entry from both context memos.
    Caller holds ``MEMO_LOCK`` through its publish."""
    from agent import model_metadata as mm  # facade seam: tests swap the memo dicts there
    evict_oldest(mm._codex_oauth_context_cache, cache_key, lambda entry: entry[1], CODEX_CACHE_MAX, mm._codex_oauth_max_context_cache)

def endpoint_memo_key(normalized: str, api_key: object) -> tuple[str, str]:
    """In-process memo key for a ``/models`` probe.

    Callable keys get a slot keyed by their object identity, isolated from the keyless
    ``(url, "")`` slot so a keyless 401 cannot poison subsequent callable-key lookups.
    """
    from agent.credential_persistence import fingerprint_secret_value
    # Callable keys: use object identity as slot key, isolated from the keyless ("") slot.
    if callable(api_key):
        return normalized, f"callable:{id(api_key)}"
    return normalized, (fingerprint_secret_value(api_key) or "") if isinstance(api_key, str) else ""
