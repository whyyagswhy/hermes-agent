"""Root-owned install + non-owner service user must never exit launch with reinstall (#133351)."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import sys

import pytest

from hermes_cli import venv_sync


def _deny(path: Path, monkeypatch) -> None:
    real = Path.read_text

    def denied(self, *args, **kwargs):
        if str(self) == str(path):
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)


def _self_checkout(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='example'\n")
    (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": "self"}))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    return root


def test_unreadable_stamp_defers_pending_completion_to_owner(tmp_path, monkeypatch, capsys):
    """An unreadable stamp is not a missing stamp: no adopt, no tail, no reinstall."""
    import hermes_cli.post_update

    root = _self_checkout(tmp_path, monkeypatch)
    _deny(root / "install-stamp.json", monkeypatch)
    monkeypatch.setattr(
        hermes_cli.post_update,
        "step_adopt_blessed_checkout",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("an unreadable stamp must not be adopted over")),
    )
    pending = venv_sync.completion_pending_path(root)
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("source update tail not finished\n", encoding="utf-8")
    assert venv_sync.prepare_launch(root, []) is None
    assert "install owner" in capsys.readouterr().err


def test_foreign_owned_tree_defers_completion_tail(tmp_path, monkeypatch, capsys):
    """A non-owner boot leaves the owed tail for the owner instead of running it."""
    import pm
    from hermes_cli import _launchers

    root = _self_checkout(tmp_path, monkeypatch)
    pending = venv_sync.completion_pending_path(root)
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text("source update tail not finished\n", encoding="utf-8")
    monkeypatch.setattr(pm, "venv_is_current", lambda *, project_root: True)
    monkeypatch.setattr("pm.environments.owning_home_root", lambda root: None)
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda *_a, **_k: Path(sys.executable))
    monkeypatch.setattr(os, "geteuid", lambda: 424242)
    tail_calls: list = []
    monkeypatch.setattr(venv_sync.subprocess, "call", lambda *a, **k: tail_calls.append(a) or 0)
    assert venv_sync.prepare_launch(root, ["up"]) is None
    assert tail_calls == []
    assert "install owner" in capsys.readouterr().err


def test_plugin_admission_refuses_foreign_owned_install(tmp_path, monkeypatch):
    import pm.client
    from hermes_cli import plugins_admission

    monkeypatch.setattr(os, "geteuid", lambda: 424242)
    calls: list = []
    monkeypatch.setattr(pm.client, "sync_venv", lambda *a, **k: calls.append((a, k)))
    with pytest.raises(RuntimeError, match="not the current uid"):
        plugins_admission.admit_plugin_set_change({"demo"}, set())
    assert calls == []


def test_plugin_publish_refuses_foreign_owned_install(tmp_path, monkeypatch):
    import pm.client
    from hermes_cli import plugins_transaction

    monkeypatch.setattr(os, "geteuid", lambda: 424242)
    calls: list = []
    monkeypatch.setattr(pm.client, "sync_venv", lambda *a, **k: calls.append((a, k)))
    staged = tmp_path / "staged"
    staged.mkdir()
    with pytest.raises(RuntimeError, match="not the current uid"):
        plugins_transaction.publish_plugin(staged, tmp_path / "target", {}, {})
    assert calls == []
