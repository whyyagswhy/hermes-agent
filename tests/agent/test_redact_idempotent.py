"""The head/tail mask must be a fixed point of ``redact_sensitive_text`` (#153079).

Every rule that masks a secret through ``_mask_token`` emits
``value[:6] + "..." + value[-4:]`` -- 13 characters. That shape is shorter than
``_mask_token``'s own 18-character floor, so a second redaction pass over
already-redacted text replaced it with ``***``. The function runs at several
boundaries (script capture, gateway storage, logging), so the second pass
silently shortened the text: the measured -10 character drift in #153079.

These tests pin the fixed point and, just as importantly, pin that the guard
which recognises an already-masked value stays narrow: a genuine secret that
merely resembles the mask is still masked.
"""

import pytest

from agent.redact import mask_secret, redact_sensitive_text


@pytest.fixture(autouse=True)
def _ensure_redaction_enabled(monkeypatch):
    monkeypatch.delenv("HERMES_REDACT_SECRETS", raising=False)
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)


# Opaque, digit-and-symbol-bearing value: 24 chars, so _mask_token keeps a
# 6-char head and a 4-char tail. Synthetic, not a real credential.
SECRET = "exampl3AKIAQYLPMN5HH-32c"


def _r(text):
    return redact_sensitive_text(text)


# One entry per rule that produces a head/tail mask. A first pass must mask the
# secret and emit the ``head...tail`` shape.
HEAD_TAIL_RULES = {
    "env-assignment": f"SECRET={SECRET}",
    "env-assignment-quoted": f"export SECRET='{SECRET}'",
    "bearer-header": f"Authorization: Bearer {SECRET}",
    "json-field": f'{{"api_key": "{SECRET}"}}',
    "yaml-assign": f"api_key: {SECRET}",
    "python-repr": f"Cfg(api_key='{SECRET}')",
    "cli-flag": f"tool --password={SECRET} run",
}


@pytest.mark.parametrize("rule, text", sorted(HEAD_TAIL_RULES.items()))
def test_head_tail_mask_is_idempotent(rule, text):
    """r(r(s)) == r(s) for every rule that emits a head/tail mask."""
    first = _r(text)
    # Sanity: the rule really did mask on the first pass (not a vacuous case).
    assert SECRET not in first, (
        f"{rule} did not mask the secret on a first pass: {first!r}"
    )
    assert "..." in first, f"{rule} did not emit a head/tail mask: {first!r}"
    # The defect: a second pass over already-masked text shortened it.
    again = _r(first)
    assert again == first, (
        f"{rule}: second pass changed already-masked text "
        f"({len(first)} -> {len(again)} chars): {first!r} -> {again!r}"
    )


@pytest.mark.parametrize("rule, text", sorted(HEAD_TAIL_RULES.items()))
def test_third_pass_stable(rule, text):
    """The fixed point survives repeated passes, not just the second one."""
    once = _r(text)
    assert _r(_r(_r(once))) == once, rule


@pytest.mark.parametrize(
    "text",
    [
        # Fully masked already, and the shape is too short to be a head/tail mask.
        "OPENAI_API_KEY=***",
        "api_key: ***",
        '{"api_key": "***"}',
        # Non-reusable sentinel (file_read path) is a marker, not a mask.
        "OPENAI_API_KEY=\u00abredacted:sk-abc\u2026\u00bb",
        "api_key: \u00abredacted-secret\u00bb",
        # A prefix-matched token mask whose body cannot contain a token body char
        # ('.'), so the prefix pass never re-matches it.
        "token ghp_ex...ABCD end",
        # URL userinfo is masked to the placeholder, never to a head/tail mask.
        f"https://user:{SECRET}@example.test/path",
    ],
)
def test_already_idempotent_shapes_stay_stable(text):
    """Shapes that were already idempotent must not regress."""
    once = _r(text)
    assert _r(once) == once, f"{once!r} -> {_r(once)!r}"


# A real secret whose value merely RESEMBLES the mask: every case breaks the
# exact emitted shape, so the guard must not accept it.
RESEMBLING_SECRETS = {
    # 14 chars: right dots, wrong length.
    "one-char-too-long": "A" * 6 + "..." + "B" * 5,
    # 12 chars: right dots, wrong length.
    "one-char-too-short": "A" * 6 + "..." + "B" * 3,
    # 13 chars, but only two dots before the tail.
    "two-dots": "A" * 6 + ".." + "B" * 5,
    # 13 chars, dots one position late.
    "dots-one-late": "A" * 7 + "..." + "B" * 3,
    # 13 chars, dots one position early.
    "dots-one-early": "A" * 5 + "..." + "B" * 5,
    # Four dots.
    "four-dots": "A" * 6 + "...." + "B" * 4,
    # Dots at the right offset but far longer than any mask.
    "long-with-dots": "A" * 6 + "..." + "B" * 30,
    # Spaced dots, not an ellipsis mask.
    "spaced-dots": "A" * 6 + ". . ." + "B" * 4,
}


@pytest.mark.parametrize("rule, value", sorted(RESEMBLING_SECRETS.items()))
def test_secret_resembling_the_mask_is_still_masked(rule, value):
    """Only the exact emitted shape counts as already-redacted.

    A redactor that skips text it should mask is worse than one that re-masks,
    so a credential that happens to contain dots must still be masked.
    """
    text = f"API_KEY={value}"
    out = _r(text)
    assert value not in out, f"{rule}: an unmasked secret was passed through: {out!r}"


@pytest.mark.parametrize("rule, value", sorted(RESEMBLING_SECRETS.items()))
def test_resembling_secret_is_masked_on_a_second_pass_too(rule, value):
    """Idempotency must not turn a resembling secret into an accepted mask."""
    once = _r(f"API_KEY={value}")
    twice = _r(once)
    assert value not in twice, f"{rule}: {once!r} -> {twice!r}"
    assert twice == once, f"{rule}: second pass changed {once!r} -> {twice!r}"


@pytest.mark.parametrize(
    "value",
    [
        SECRET,
        "A" * 40,
        "exampl...-32c",  # the exact 13-char mask (_mask_token)
        "sk-p...7890",  # the exact 11-char mask (mask_secret default)
        "abc...wxyz",  # 11 chars, dots at index 3: not the default shape
        "A" * 6 + "..." + "B" * 4 + "C",  # 14 chars
        "***",
        "",
    ],
)
@pytest.mark.parametrize("head, tail, floor", [(4, 4, 12), (6, 4, 18)])
def test_mask_secret_is_a_fixed_point(value, head, tail, floor):
    """mask_secret(mask_secret(v)) == mask_secret(v), for any head/tail/floor."""
    once = mask_secret(value, head=head, tail=tail, floor=floor)
    assert mask_secret(once, head=head, tail=tail, floor=floor) == once


@pytest.mark.parametrize("rule, text", sorted(HEAD_TAIL_RULES.items()))
def test_first_pass_still_masks_a_real_secret(rule, text):
    """Blast radius: the guard must not weaken first-pass masking."""
    assert SECRET not in _r(text), rule
