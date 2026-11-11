"""Display masking of one secret value, for agent.redact.

``mask_secret`` is the single place a found secret is shortened for display, and it is the
mirror image of the work the facade does: redact.py locates secrets, this shortens one.
Keeping it here leaves the facade room under its file cap for the rules that find them.
"""

import re


# mask_secret strips EVERY control char (incl. \n/\t, C1, DEL, zero-width) so a
# masked secret never emits multiline or invisible bytes into display output.
_DISPLAY_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\x80-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064]")


def mask_secret(value: str, *, head: int = 4, tail: int = 4, floor: int = 12,
                placeholder: str = "***", empty: str = "") -> str:
    """Mask a secret for display (``hermes config`` / ``status`` / ``dump``):
    ``sk-p...7890``; shorter than ``floor`` (after control-byte stripping) →
    ``placeholder``; falsy → ``empty``."""
    value = _DISPLAY_CONTROL_RE.sub("", value) if value else value
    if not value:
        return empty
    # Idempotency (#153079): value[:head] + "..." + value[-tail:] is exactly
    # ``head + 3 + tail`` chars, which is under ``floor``, so a second pass over
    # already-masked text fell through to the placeholder and silently shortened
    # it (``exampl...-32c`` -> ``***``). Recognise the shape this function itself
    # emits and return it unchanged, which makes mask_secret a fixed point.
    # Only that exact shape counts: a value whose length differs or whose dots
    # sit at another offset is a real secret and is still masked.
    if len(value) == head + 3 + tail and value.startswith("...", head):
        return value
    return placeholder if len(value) < floor else f"{value[:head]}...{value[-tail:]}"
