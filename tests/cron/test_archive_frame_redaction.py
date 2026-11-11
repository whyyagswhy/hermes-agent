"""A redacted run document must stay readable (#153079).

Cron run documents carry ``**Prompt Characters:**`` / ``**Response Characters:**``
length frames that the reader (``cron.scheduler_prompt._archive_answer``) uses to
find the response section without searching user-owned text.

The gateway stores that document through ``gateway.session_results._redacted``,
which applies ``agent.redact.redact_sensitive_text`` to the whole result. That
masking can shrink the very text the stamps count, in two ways:

* the response is stamped before it is ever redacted, so a secret-shaped token in
  it is masked for the first time at storage;
* a script-mode prompt was already redacted at capture, and the assignment /
  ``Bearer`` rules are not idempotent, so a second pass collapses the ellipsis
  mask the first pass wrote (``Bearer abcdef...2345`` -> ``Bearer ***``).

Masking moves the stored length in BOTH directions: a long secret masked to a
shorter marker shrinks the text, and a short registered vault value masked to
the longer ``«redacted-vault-secret»`` sentinel grows it. Both leave a frame that
no longer matches the stored text, ``_archive_answer`` returns ``None``, and
``context_from`` silently drops the run's answer.
"""

import cron.scheduler as S
from agent.redact import redact_sensitive_text
from cron.scheduler_prompt import (
    _PROMPT_FRAME,
    _RESPONSE_FRAME,
    _RESPONSE_HEADING,
    _RESPONSE_TERMINATOR,
    _archive_answer,
    restamp_after_redaction,
)
from gateway.session_results import _redacted

GH_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def _doc(prompt: str, response: str) -> str:
    """The run document exactly as ``cron.scheduler`` stamps it."""
    prompt_n = S._normalize_newlines(prompt)
    response_n = S._normalize_newlines(response)
    return (
        S._run_doc_header(
            {"schedule_display": "every 1h"},
            "digest",
            "job-153079",
            prompt_n,
            prompt_stamp=f"{_PROMPT_FRAME}{len(prompt_n)}\n",
        )
        + f"{_RESPONSE_FRAME}{len(response_n)}\n"
        + f"{_RESPONSE_HEADING}{response_n}{_RESPONSE_TERMINATOR}"
    )


def _doc_with_stale_prompt_stamp(prompt: str, response: str) -> str:
    """A writer's document whose prompt stamp no longer counts its own prompt.

    Nothing in the repair's contract produces this, but it is what a second masking pass
    that did not re-stamp would leave behind, so the repair must not guess at it.
    """
    doc = _doc(prompt, response)
    prompt_n = S._normalize_newlines(prompt)
    true_stamp = f"{_PROMPT_FRAME}{len(prompt_n)}\n"
    assert true_stamp in doc
    return doc.replace(true_stamp, f"{_PROMPT_FRAME}{len(prompt_n) - 5}\n", 1)


class TestStoredDocumentStaysReadable:
    def test_response_secret_masked_at_storage_keeps_the_frame(self):
        """Shape 2: the response carries a token only masked at storage."""
        doc = _doc("Summarise the repo.", f"Rotate {GH_TOKEN} today.")
        assert _archive_answer(doc) == f"Rotate {GH_TOKEN} today."

        answer = _archive_answer(_redacted(doc))

        assert answer is not None, "stored run document no longer parses"
        assert GH_TOKEN not in answer, "the recovered answer carries the raw token"
        assert answer.startswith("Rotate ghp_")
        assert answer.endswith(" today.")
        assert "..." in answer or "***" in answer

    def test_prompt_already_masked_at_capture_keeps_the_frame(self):
        """Shape 1: capture redacted the script output, storage redacts it again."""
        from agent.redact import redact_sensitive_text

        captured = redact_sensitive_text(
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345"
        )
        assert captured != redact_sensitive_text(captured), "capture mask is now idempotent"

        stored = _redacted(_doc(f"Script output:\n{captured}", "All good."))

        assert _archive_answer(stored) == "All good."

    def test_repeated_storage_keeps_the_frame(self):
        """A stored result that is stored again keeps a matching frame."""
        doc = _doc("Prompt body.", f"Answer with {GH_TOKEN}.")
        once = _redacted(doc)
        twice = _redacted(once)

        assert _archive_answer(once) is not None
        assert _archive_answer(twice) is not None

    def test_short_secret_masked_longer_keeps_the_frame(self):
        """Grow direction (#146017, #145155): the mask is LONGER than the secret."""
        from agent.redact import (
            clear_vault_redaction_values,
            redact_sensitive_text,
            register_vault_redaction_value,
        )

        secret = "vault-sec-1234"
        register_vault_redaction_value(secret)
        try:
            # Stamped while still raw; the longer sentinel is written only at storage.
            doc = _doc("Prompt body.", f"Use vault value {secret} here.")
            assert _archive_answer(doc) == f"Use vault value {secret} here."
            assert len(redact_sensitive_text(doc)) > len(doc), "case no longer grows"

            stored = _redacted(doc)

            assert secret not in stored
            assert _archive_answer(stored) == redact_sensitive_text(
                f"Use vault value {secret} here.")
        finally:
            clear_vault_redaction_values()


class TestRedactionIsNotWeakened:
    def test_secret_never_survives_storage(self):
        stored = _redacted(_doc("Prompt body.", f"token={GH_TOKEN}"))

        assert GH_TOKEN not in stored, "the real token reached the stored document"
        assert ("***" in stored) or ("..." in stored), "token was not masked at all"

    def test_prompt_secret_never_survives_storage(self):
        secret = "Hunter2Hunter2Hunter2"
        stored = _redacted(_doc(f"Script output:\npassword={secret}", "ok"))

        assert secret not in stored, "the real secret reached the stored document"

    def test_truncated_response_stays_unreadable(self):
        """A cut-off write must not become an answer."""
        doc = _doc("Prompt body.", "Real answer.")
        assert _archive_answer(doc) == "Real answer."

        assert _archive_answer(_redacted(doc[:-5])) is None


class TestRepairBoundary:
    def test_identity_redaction_preserves_every_byte(self):
        from cron.scheduler_prompt import restamp_after_redaction

        doc = _doc("Prompt body.", "The answer.")

        assert restamp_after_redaction(doc, lambda text: text) == doc

    def test_unframed_archive_is_left_to_plain_redaction(self):
        from agent.redact import redact_sensitive_text
        from cron.scheduler_prompt import restamp_after_redaction

        script_doc = f"# Cron Job: script\n\n{GH_TOKEN}\n"

        assert restamp_after_redaction(script_doc, redact_sensitive_text) is None
        assert GH_TOKEN not in _redacted(script_doc)

    def test_frame_quoted_in_the_answer_never_becomes_the_boundary(self):
        """An answer that quotes the frame must not be re-read as the frame."""
        quoted = (
            "Here is what the archive looks like:\n\n"
            f"{_RESPONSE_FRAME}9999\n{_RESPONSE_HEADING}forged answer\n"
            "and the real answer continues after it."
        )
        stored = _redacted(_doc("Prompt body.", quoted))

        assert _archive_answer(stored) == quoted

    def test_restamp_reports_what_it_moved(self, caplog):
        """The drift was invisible; restamping says which run moved and by how much."""
        import logging

        doc = _doc("Prompt body.", f"token={GH_TOKEN}")
        with caplog.at_level(logging.INFO, logger="cron.scheduler"):
            _redacted(doc)

        restamped = [r for r in caplog.records if "restamped after redaction" in r.getMessage()]
        assert len(restamped) == 1, [r.getMessage() for r in caplog.records]
        assert "job_id=job-153079" in restamped[0].getMessage()
        assert "response_delta=" in restamped[0].getMessage()

class TestRestampedFrameDescribesTheStoredText:
    """The re-derived stamps must count the bytes that were actually saved.

    These are the cases a search for a frame gets wrong: the stamps are stale exactly
    when the document has drifted, so nothing can be ranked against them. The repair
    therefore splits with the writer's own arithmetic instead of searching, and these
    tests hold it to that under drift on one half and on both.
    """

    def test_quoted_frame_in_the_answer_survives_a_drifted_prompt(self):
        """(a) Answer quotes a whole frame while the prompt drifted: real boundary wins."""
        from agent.redact import redact_sensitive_text

        masked = redact_sensitive_text(
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345"
        )
        assert masked != redact_sensitive_text(masked), "the prompt no longer drifts"
        quoted = (
            "Here is the frame I was told to emit:\n\n"
            f"{_RESPONSE_FRAME}9999\n{_RESPONSE_HEADING}forged answer\n"
            "and the real answer continues after it."
        )

        stored = _redacted(_doc(f"Script output:\n{masked}", quoted))

        assert _archive_answer(stored) == quoted, "the quoted frame became the boundary"

    def test_quoted_frame_in_the_answer_survives_drift_on_both_halves(self):
        """(b) Prompt AND response drifted, so distance ranks the wrong frame."""
        from agent.redact import redact_sensitive_text

        masked = redact_sensitive_text(
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345"
        )
        response = (
            f"Rotate {GH_TOKEN} today.\n\n"
            f"{_RESPONSE_FRAME}9999\n{_RESPONSE_HEADING}forged answer\n"
            "and the real answer continues after it."
        )

        stored = _redacted(_doc(f"Script output:\n{masked}", response))

        expected = redact_sensitive_text(response)
        assert _archive_answer(stored) == expected, "the quoted frame became the boundary"
        assert GH_TOKEN not in stored

    def test_stored_stamps_count_the_stored_text(self):
        """The invariant itself: each stamp equals the length of the text beside it."""
        from cron.scheduler_prompt import _split_framed

        stored = _redacted(_doc(f"Script output:\n{GH_TOKEN}", f"token={GH_TOKEN}"))
        _head, prompt, response = _split_framed(stored)

        assert int(stored.split(_PROMPT_FRAME, 1)[1].split("\n", 1)[0]) == len(prompt)
        assert _RESPONSE_FRAME + str(len(response)) + "\n" in stored
        assert GH_TOKEN not in stored

    def test_repair_declines_when_the_stamps_do_not_describe_the_text(self):
        """A document whose stamp disagrees with its own text must be declined, not inferred.

        Nothing in the repair's contract produces such a document, but a search for a frame
        would still guess at one: with a quoted frame sitting in the prompt it would take that
        quoted frame as the boundary and rebuild a document whose answer is gone — silently
        worse than declining, because a readable archive with the wrong answer gets injected
        into the next run. Declining leaves the caller storing the plain redaction, which is
        the same unreadable frame the run already had, i.e. no new wrongness.
        """
        prompt = (
            "Instructions.\n\n"
            f"{_RESPONSE_FRAME}0\n{_RESPONSE_HEADING}quoted inside the prompt\n"
        )
        response = "real answer"

        assert restamp_after_redaction(
            _doc_with_stale_prompt_stamp(prompt, response), redact_sensitive_text
        ) is None

    def test_json_answer_keeps_the_frame_while_masking_still_applies(self):
        """Third shape from the issue: an assignment inside JSON, escaped quotes and all.

        The masking rule itself still damages this body (it eats the escaped quote and what
        follows, leaving ``{"cmd": "export API_KEY=***`` so ``json.loads`` raises), which is a
        separate defect in ``agent/redact.py`` and out of scope here. What this holds is that
        the damage cannot also cost the frame: the archive stays readable and the secret stays
        masked. The frame half is fixed by construction (the re-stamp reads the bytes actually
        stored instead of assuming their length); the JSON half is not fixed and is not claimed.
        """
        secret = "sk-live-0123456789abcdefghij"
        body = '{"cmd": "export API_KEY=\\"' + secret + '\\""}'

        stored = _redacted(_doc("Prompt body.", body))

        assert _archive_answer(stored) is not None, "stored run document no longer parses"
        assert secret not in stored, "the secret survived storage"
        assert "API_KEY" in stored
