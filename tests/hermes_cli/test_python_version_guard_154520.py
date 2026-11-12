"""Tests for the Python version startup guard (fix for #154520).

The guard in hermes_cli/main.py exits with a clear message on Python < 3.14
instead of letting a gated import fail deep in the turn loop with
ModuleNotFoundError: No module named snowballstemmer.

These tests run on the host Python (3.14) but patch sys.version_info to
simulate an older interpreter.  They test the guard logic only; full import
of hermes_cli.main is intentionally avoided so the fixture stays fast.
"""
from __future__ import annotations

import sys
import textwrap
import types
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helper: execute the guard block in isolation with a patched version_info
# ---------------------------------------------------------------------------

def _run_guard(version_tuple: tuple, argv: list[str]) -> None:
    """Execute only the version-guard block from main.py with synthetic version/argv."""
    # Build a minimal synthetic sys module so the guard block can reference sys.version
    fake_version = ".".join(str(v) for v in version_tuple[:3])
    fake_sys = types.SimpleNamespace(
        version_info=version_tuple,
        version=f"{fake_version} (default, Nov 12 2026, 00:00:00) [GCC 14]",
        argv=argv,
        exit=sys.exit,  # real exit so SystemExit propagates
    )
    # Reproduce exactly the guard block from hermes_cli/main.py
    guard_code = textwrap.dedent("""\
        import sys as _sys_real
        _sys = fake_sys
        if _sys.version_info < (3, 14):
            _argv = _sys.argv[1:5]
            _update_cmds = {"update", "--update", "self-update"}
            if not any(a in _update_cmds for a in _argv):
                _sys.exit(
                    "Hermes requires Python 3.14 or later; "
                    "you are running Python " + _sys.version.split()[0] + ".\\n"
                    "If you have an existing Hermes install, run `hermes update` to upgrade.\\n"
                    "For a fresh install, see: https://github.com/NousResearch/hermes-agent"
                )
            del _argv, _update_cmds
        del _sys
    """)
    exec(guard_code, {"fake_sys": fake_sys})


# ---------------------------------------------------------------------------
# RED tests: guard does NOT exist on main (no SystemExit for old Python)
# We test the ABSENCE of the guard by checking main.py source directly.
# ---------------------------------------------------------------------------

def _main_py_has_guard() -> bool:
    import pathlib
    src = pathlib.Path(__file__).parent.parent.parent / "hermes_cli" / "main.py"
    return "Hermes requires Python 3.14 or later" in src.read_text()


def test_guard_text_present_in_main_py() -> None:
    """The version guard message must be present in hermes_cli/main.py."""
    assert _main_py_has_guard(), (
        "hermes_cli/main.py has no Python version guard; "
        "on Python 3.12 the user gets ModuleNotFoundError deep in the turn loop (#154520)"
    )


def test_guard_runs_before_gated_import_location() -> None:
    """The guard must appear before the first non-stdlib import in main.py."""
    import pathlib
    src = pathlib.Path(__file__).parent.parent.parent / "hermes_cli" / "main.py"
    text = src.read_text()
    guard_pos = text.find("Hermes requires Python 3.14 or later")
    # _early_recovery is the first hermes import after hermes_bootstrap
    early_recovery_pos = text.find("from hermes_cli import _early_recovery")
    assert guard_pos != -1, "guard not found"
    assert guard_pos < early_recovery_pos, (
        "guard must appear before from hermes_cli import _early_recovery "
        "so it fires before any gated-package import path is reachable"
    )


# ---------------------------------------------------------------------------
# GREEN tests: guard fires for old Python non-update subcommands
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("version", [(3, 12, 13), (3, 11, 0), (3, 13, 2)])
def test_old_python_chat_raises_system_exit(version: tuple) -> None:
    """Python < 3.14 running `hermes chat` must exit with a readable message."""
    with pytest.raises(SystemExit) as exc_info:
        _run_guard(version, ["hermes", "chat"])
    msg = str(exc_info.value.code)
    assert "3.14" in msg
    assert version[0:2] == (3, version[1])  # sanity


@pytest.mark.parametrize("version", [(3, 12, 13), (3, 11, 0)])
def test_old_python_bare_invocation_raises_system_exit(version: tuple) -> None:
    """Python < 3.14 with bare `hermes` (no subcommand) exits cleanly."""
    with pytest.raises(SystemExit) as exc_info:
        _run_guard(version, ["hermes"])
    assert "3.14" in str(exc_info.value.code)


@pytest.mark.parametrize("update_argv", [
    ["hermes", "update"],
    ["hermes", "--update"],
    ["hermes", "self-update"],
])
def test_old_python_update_subcommand_passes_guard(update_argv: list[str]) -> None:
    """`hermes update` on old Python must NOT be blocked — the updater contract."""
    # Should not raise
    _run_guard((3, 12, 13), update_argv)


def test_new_python_does_not_trigger_guard() -> None:
    """Python 3.14 must not trigger the guard regardless of subcommand."""
    _run_guard((3, 14, 0), ["hermes", "chat"])
    _run_guard((3, 14, 8), ["hermes", "gateway", "run"])


def test_exit_message_mentions_upgrade_path() -> None:
    """The exit message must mention `hermes update` so users know how to fix it."""
    with pytest.raises(SystemExit) as exc_info:
        _run_guard((3, 12, 13), ["hermes"])
    msg = str(exc_info.value.code)
    assert "hermes update" in msg


def test_exit_message_mentions_running_version() -> None:
    """The exit message must include the actual running Python version."""
    with pytest.raises(SystemExit) as exc_info:
        _run_guard((3, 12, 13), ["hermes"])
    assert "3.12" in str(exc_info.value.code)
