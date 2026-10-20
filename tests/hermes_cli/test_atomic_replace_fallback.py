"""Tests for utils.atomic_replace errno fallback — overlayfs stale-handle renames."""

import errno
import os

import pytest

import utils


def test_estale_rename_falls_back_to_copy(tmp_path, monkeypatch):
    """An overlayfs rename raising ESTALE (errno 116) must copy/fsync/unlink like
    EXDEV/EBUSY instead of propagating — see #142011 (hermes setup cannot update .env
    on overlay filesystems)."""
    target = tmp_path / ".env"
    target.write_text("OLD=1\n", encoding="utf-8")
    tmp_file = tmp_path / ".env_tmp123"
    tmp_file.write_text("NEW=2\n", encoding="utf-8")

    real_replace = os.replace

    def stale_replace(src, dst):
        raise OSError(errno.ESTALE, "Stale file handle")

    monkeypatch.setattr(os, "replace", stale_replace)
    try:
        result = utils.atomic_replace(tmp_file, target)
    finally:
        monkeypatch.setattr(os, "replace", real_replace)
    assert result == str(target)
    assert target.read_text(encoding="utf-8") == "NEW=2\n"
    assert not tmp_file.exists()


def test_unlisted_errno_still_raises(tmp_path, monkeypatch):
    """Errnos outside the never-clears-on-retry set (e.g. EACCES) must still propagate."""
    target = tmp_path / ".env"
    target.write_text("OLD=1\n", encoding="utf-8")
    tmp_file = tmp_path / ".env_tmp123"
    tmp_file.write_text("NEW=2\n", encoding="utf-8")

    def denied_replace(src, dst):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(os, "replace", denied_replace)
    with pytest.raises(OSError):
        utils.atomic_replace(tmp_file, target)
    assert target.read_text(encoding="utf-8") == "OLD=1\n"
