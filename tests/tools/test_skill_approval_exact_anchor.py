"""Approval-path patches require an exact anchor (#132822).

An approved (staged-then-replayed) skill patch must only apply when the
fuzzy matcher resolves through the exact strategy; a non-exact
resolution (e.g. line_trimmed) must fail LOUDLY with the strategy
named, and the /skills diff preview must agree with what approve
would do. Foreground patches keep the fuzzy ladder (matcher untouched).
"""
import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

SK = (
    "---\nname: {n}\ndescription: Probe skill for approval-anchor tests.\n---\n"
    "# Probe\n\nStep 1: do the thing.\nStep 2: other.\n"
)

# Leading-whitespace drift: resolves via the line_trimmed strategy,
# never exact.
FUZZY_OLD = "  Step 1: do the thing."
NEW = "Step 1: DONE."


def _patch_payload():
    return {"action": "patch", "name": "probe",
            "old_string": FUZZY_OLD, "new_string": NEW}


class TestSkillApprovalExactAnchor(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="skanchor_t_")
        os.environ["HERMES_HOME"] = self.home
        os.makedirs(os.path.join(self.home, "skills", "probe"), exist_ok=True)
        with open(os.path.join(self.home, "skills", "probe", "SKILL.md"), "w",
                  encoding="utf-8") as f:
            f.write(SK.format(n="probe"))
        import tools.skill_manager_tool as smt
        importlib.reload(smt)
        import tools.write_approval as wa
        importlib.reload(wa)
        self.smt = smt
        self.wa = wa

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def test_approval_replay_rejects_non_exact_anchor(self):
        """apply_skill_pending on a fuzzy-anchored patch fails and names the strategy."""
        raw = self.smt.apply_skill_pending(_patch_payload())
        out = json.loads(raw)
        self.assertFalse(out.get("success"), f"approval replay applied a fuzzy patch: {out}")
        self.assertIn("line_trimmed", json.dumps(out))

    def test_preview_agrees_with_approve(self):
        """skill_pending_diff renders the rejection the approve path would hit."""
        diff = self.wa.skill_pending_diff({"payload": _patch_payload()})
        self.assertIn("(patch would fail:", diff)
        self.assertIn("line_trimmed", diff)

    def test_foreground_fuzzy_patch_still_allowed(self):
        """Narrow scope: the foreground path keeps the fuzzy ladder."""
        res = self.smt._patch_skill("probe", FUZZY_OLD, NEW)
        self.assertTrue(res.get("success"), f"foreground fuzzy patch broke: {res}")


if __name__ == "__main__":
    unittest.main()
