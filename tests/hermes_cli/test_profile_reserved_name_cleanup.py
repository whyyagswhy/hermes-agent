"""Cleanup verbs must accept a reserved-name profile that already exists on disk.

``profile create`` used to materialize the full profile skeleton *before* refusing a reserved
name (#153287), so an older version could leave a real profile directory at e.g.
``profiles/hermes``. Every cleanup verb then re-ran the same reserved-name guard and refused,
which made that directory permanent: there was no supported way to remove it.

The create side is validated up front now, but a leftover still has to be removable. These
tests pin both halves: create refuses without materializing anything, and delete/rename act on
an existing reserved-name profile — while a reserved name is still refused as a rename
*destination*, which is where the collision guard belongs.
"""

import shutil
from pathlib import Path

import pytest

from hermes_cli.profiles import (
    create_profile,
    delete_profile,
    get_profile_dir,
    rename_profile,
)

RESERVED = "hermes"


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    """Same redirect as ``tests/hermes_cli/test_profiles.py``: home and HERMES_HOME -> tmp_path."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


def _reserved_profile_left_by_old_create(reserved: str = RESERVED) -> Path:
    """A genuinely valid profile parked under a reserved name.

    That is the exact shape the old ``profile create`` left behind: a complete skeleton under a
    name the guard refuses. Building it by moving a real profile (rather than ``mkdir``) keeps
    the identity files that both ``profile list`` and the deletion path look for.
    """
    created = create_profile("keeper", no_skills=True, description="reserved-name fixture")
    target = get_profile_dir(reserved)
    shutil.move(str(created), str(target))
    assert target.is_dir(), "fixture must land a real profile directory at the reserved name"
    return target


class TestCreateRefusesWithoutMaterializing:
    def test_reserved_name_is_refused_before_any_directory_exists(self, profile_env):
        with pytest.raises(ValueError, match="reserved"):
            create_profile(RESERVED, no_skills=True)
        assert not get_profile_dir(RESERVED).exists()


class TestCleanupAcceptsExistingReservedName:
    def test_delete_removes_reserved_name_profile(self, profile_env):
        target = _reserved_profile_left_by_old_create()
        delete_profile(RESERVED, yes=True)
        assert not target.exists()

    def test_rename_accepts_reserved_name_as_source(self, profile_env):
        target = _reserved_profile_left_by_old_create()
        rename_profile(RESERVED, "recovered")
        assert not target.exists()
        assert get_profile_dir("recovered").is_dir()

    def test_rename_still_refuses_a_reserved_name_as_destination(self, profile_env):
        created = create_profile("keeper", no_skills=True, description="t")
        with pytest.raises(ValueError, match="reserved"):
            rename_profile("keeper", RESERVED)
        assert created.is_dir()
