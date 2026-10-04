"""Tests for Blank Slate setup mode (hermes_cli/setup.py).

Blank Slate is the third first-time setup option: everything off except the
bare minimum needed to run an agent (provider/model + file + terminal). These
tests pin the config the writers produce and the invariant that the toolset
resolver + tool-schema builder yield exactly the file/terminal tools.
"""


from hermes_cli.setup_quick import _blank_slate_minimal_toolsets, _blank_slate_minimize_config
from hermes_cli import setup_quick


class TestBlankSlateMinimalToolsets:


    def test_no_disabled_bundle_overlaps_kept_tools(self):
        """Invariant: ``disabled_toolsets`` is applied at *tool* granularity and
        a single tool can belong to several toolsets, so no disabled entry may
        share a tool with a kept toolset — it would silently strip that tool
        from the blank-slate agent (#57315, #58281).
        """
        from toolsets import resolve_toolset
        cfg = {}
        _blank_slate_minimal_toolsets(cfg)
        kept_tools = set()
        for ts in cfg["platform_toolsets"]["cli"]:
            kept_tools.update(resolve_toolset(ts))
        for ts in cfg["agent"]["disabled_toolsets"]:
            overlap = set(resolve_toolset(ts)) & kept_tools
            assert not overlap, (
                f"disabled toolset '{ts}' overlaps kept tools {sorted(overlap)}; "
                "it would silently strip them from the blank-slate agent"
            )


    def test_tool_schema_survives_disabled_toolsets_from_config(self, monkeypatch):
        """Regression: disabled_toolsets must not erase the minimal Blank Slate
        surface when passed to model_tools.  Before the fix, posture toolsets
        like ``coding`` in disabled_toolsets caused model_tools to subtract
        terminal, read_file, write_file, etc. (#57315).

        vision_analyze is additionally check_fn-gated on a resolvable vision
        backend; mock the requirement check so the toolset logic is exercised
        independent of the test host's provider credentials.
        """
        import model_tools
        from tools.registry import registry as _tool_registry
        _entry = _tool_registry.get_entry("vision_analyze")
        monkeypatch.setattr(_entry, "check_fn", lambda: True)
        # This test pins disabled_toolsets SUBTRACTION, not deferral policy —
        # assemble with the legacy everything-eager override so the expected
        # list stays deferral-independent (#97979 defers process_manage by
        # default, which would swap it for the three bridge tools here).
        from tools.tool_search import ToolSearchConfig
        _legacy = ToolSearchConfig.from_raw({"enabled": "on", "defer": []})
        monkeypatch.setattr("tools.tool_search.load_config", lambda: _legacy)
        monkeypatch.setattr("tools.tool_search.load_config_readonly", lambda: _legacy)
        from hermes_cli.tools_config import _get_platform_tools
        cfg = {}
        _blank_slate_minimal_toolsets(cfg)
        _blank_slate_minimize_config(cfg)
        enabled = sorted(_get_platform_tools(cfg, "cli"))
        disabled = cfg.get("agent", {}).get("disabled_toolsets") or []
        defs = model_tools.get_tool_definitions(
            enabled_toolsets=enabled,
            disabled_toolsets=disabled,
            quiet_mode=True,
        )
        names = sorted(
            {(d.get("function") or {}).get("name") or d.get("name") for d in defs}
        )
        assert {"terminal", "read_file", "write_file", "patch", "search_files"} <= set(names)


class TestBlankSlateMinimizeConfig:
    def test_optional_features_turned_off(self):
        cfg = {}
        _blank_slate_minimize_config(cfg)
        assert cfg["compression"]["enabled"] is False
        assert cfg["memory"]["memory_enabled"] is False
        assert cfg["memory"]["user_profile_enabled"] is False
        assert cfg["checkpoints"]["enabled"] is False
        assert cfg["smart_model_routing"]["enabled"] is False


class TestBlankSlateFork:
    """The post-baseline fork: finish now vs walk through configurations."""

    def _patch_common(self, monkeypatch):
        import hermes_cli.setup as s
        # Neutralize side-effecting setup steps and I/O.
        monkeypatch.setattr(s, "setup_model_provider", lambda cfg, **k: None)
        monkeypatch.setattr(s, "setup_terminal_backend", lambda cfg, **k: None)
        monkeypatch.setattr(s, "save_config", lambda cfg: None)
        monkeypatch.setattr(s, "_print_setup_summary", lambda cfg, home: None)
        monkeypatch.setattr(s, "print_header", lambda *a, **k: None)
        monkeypatch.setattr(s, "print_info", lambda *a, **k: None)
        monkeypatch.setattr(s, "print_success", lambda *a, **k: None)
        monkeypatch.setattr(s, "print_warning", lambda *a, **k: None)

    def test_finish_now_skips_walkthrough(self, monkeypatch, tmp_path):
        import hermes_cli.setup as s
        self._patch_common(monkeypatch)
        # Fork prompt returns 0 = finish now.
        monkeypatch.setattr(s, "prompt_choice", lambda *a, **k: 0)
        walked = {"called": False}
        monkeypatch.setattr(setup_quick, "_blank_slate_walkthrough",
                            lambda cfg, home: walked.__setitem__("called", True))
        opted_out = {"value": None}
        monkeypatch.setattr("tools.skills_sync_bundled_ops.set_bundled_skills_opt_out",
                            lambda enabled: opted_out.__setitem__("value", enabled))
        monkeypatch.setattr("tools.skills_sync_bundled_ops.remove_pristine_bundled_skills",
                            lambda dry_run=False: {"ok": True, "removed": [], "skipped": []})
        monkeypatch.setattr("tools.skills_sync.sync_skills", lambda quiet=True: {"copied": []})

        cfg = {}
        setup_quick._run_blank_slate_setup(cfg, tmp_path, is_existing=False)

        # Minimal baseline was applied, walkthrough was NOT run.
        assert cfg["platform_toolsets"]["cli"] == ["file", "skills", "terminal", "vision"]
        assert walked["called"] is False
        # Finish-now path records the skill opt-out (no bundled skills).
        assert opted_out["value"] is True


class TestBlankSlateOptOutRemovesSeededSkills:
    """#132883: blank-slate opt-out must remove installer-seeded pristine skills.

    All disk state is under tmp_path (patched tools.skills_sync paths);
    the real home directory state is never touched.
    """

    def _seed_two_skills(self, tmp_path):
        from unittest.mock import patch
        from tools.skills_sync import sync_skills
        bundled = tmp_path / "bundled"
        for n in ("hermes-agent", "alpha"):
            d = bundled / n
            d.mkdir(parents=True)
            content = "---" + chr(10) + "name: " + n + chr(10) + "---" + chr(10) + "body " + n + chr(10)
            (d / "SKILL.md").write_text(content)
        skills_dir = tmp_path / "user_skills"
        manifest_file = skills_dir / ".bundled_manifest"
        home = tmp_path / "home"
        home.mkdir()
        patches = [
            patch("tools.skills_sync._get_bundled_dir", return_value=bundled),
            patch("tools.skills_sync._get_optional_dir", return_value=bundled.parent / "optional-skills"),
            patch("tools.skills_sync.SKILLS_DIR", skills_dir),
            patch("tools.skills_sync.MANIFEST_FILE", manifest_file),
            patch("tools.skills_sync.HERMES_HOME", home),
        ]
        for c in patches:
            c.start()
        try:
            sync_skills(quiet=True)
        finally:
            for c in patches:
                c.stop()
        assert (skills_dir / "hermes-agent" / "SKILL.md").exists()
        assert (skills_dir / "alpha" / "SKILL.md").exists()
        return bundled, skills_dir, manifest_file, home

    def _patched(self, bundled, skills_dir, manifest_file, home):
        import contextlib
        from unittest.mock import patch
        stack = contextlib.ExitStack()
        stack.enter_context(patch("tools.skills_sync._get_bundled_dir", return_value=bundled))
        stack.enter_context(patch("tools.skills_sync._get_optional_dir", return_value=bundled.parent / "optional-skills"))
        stack.enter_context(patch("tools.skills_sync.SKILLS_DIR", skills_dir))
        stack.enter_context(patch("tools.skills_sync.MANIFEST_FILE", manifest_file))
        stack.enter_context(patch("tools.skills_sync.HERMES_HOME", home))
        return stack

    def test_opt_out_removes_pristine_seeded_skills(self, tmp_path):
        bundled, skills_dir, manifest_file, home = self._seed_two_skills(tmp_path)
        with self._patched(bundled, skills_dir, manifest_file, home):
            setup_quick._set_bundled_skills_opt_out(True, "test opt-out")
        assert (home / ".no-bundled-skills").exists()
        assert not (skills_dir / "alpha").exists(), "pristine seeded skill left on disk (#132883)"
        assert (skills_dir / "hermes-agent" / "SKILL.md").exists(), "essential skill must be re-seeded"

    def test_opt_out_keeps_user_modified_skills(self, tmp_path):
        bundled, skills_dir, manifest_file, home = self._seed_two_skills(tmp_path)
        edited = "---" + chr(10) + "name: alpha" + chr(10) + "---" + chr(10) + "EDITED" + chr(10)
        (skills_dir / "alpha" / "SKILL.md").write_text(edited)
        with self._patched(bundled, skills_dir, manifest_file, home):
            setup_quick._set_bundled_skills_opt_out(True, "test opt-out")
        assert (skills_dir / "alpha" / "SKILL.md").exists()
        assert "EDITED" in (skills_dir / "alpha" / "SKILL.md").read_text()

    def test_opt_out_tolerates_removal_failure(self, tmp_path):
        from unittest.mock import patch
        bundled, skills_dir, manifest_file, home = self._seed_two_skills(tmp_path)
        seen = {}
        with self._patched(bundled, skills_dir, manifest_file, home):
            with patch("tools.skills_sync_bundled_ops.remove_pristine_bundled_skills", side_effect=OSError("disk gone")):
                setup_quick._set_bundled_skills_opt_out(True, "test opt-out",
                    on_success=lambda result: seen.__setitem__("ok", True),
                    on_error=lambda exc: seen.__setitem__("error", exc))
        assert seen.get("ok") is True and "error" not in seen

    def test_opt_in_does_not_remove_seeded_skills(self, tmp_path):
        bundled, skills_dir, manifest_file, home = self._seed_two_skills(tmp_path)
        with self._patched(bundled, skills_dir, manifest_file, home):
            setup_quick._set_bundled_skills_opt_out(False, "test opt-in")
        assert not (home / ".no-bundled-skills").exists()
        assert (skills_dir / "alpha" / "SKILL.md").exists()
