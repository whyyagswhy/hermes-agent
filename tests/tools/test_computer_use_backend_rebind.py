"""A cached computer_use backend is bound to the display it spawned on; the identity helpers notice a change."""

from __future__ import annotations

from tools.computer_use import cua_backend


def test_backend_display_identity_tracks_the_display_a_spawn_would_get():
    before = cua_backend.desktop_identity({"HOME": "/x"})  # no screen yet
    after = cua_backend.desktop_identity({"HOME": "/x", "DISPLAY": ":37"})  # Bot Desktop came up
    assert before == "" and after == ":37"
    assert cua_backend.backend_display_stale(before, after)
    assert not cua_backend.backend_display_stale(after, cua_backend.desktop_identity({"DISPLAY": ":37"}))


def test_desktop_identity_retires_rebound_native_wayland_session():
    env_a = {
        "DISPLAY": ":0",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus-a",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "WAYLAND_DISPLAY": "wayland-0",
    }
    env_b = {
        "DISPLAY": ":0",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus-b",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "WAYLAND_DISPLAY": "wayland-0",
    }
    assert cua_backend.desktop_identity(env_a) != cua_backend.desktop_identity(env_b)
    assert cua_backend.backend_display_stale(
        cua_backend.desktop_identity(env_a), cua_backend.desktop_identity(env_b)
    )


def test_desktop_identity_stable_for_same_spawn_env():
    env = {
        "DISPLAY": ":0",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "WAYLAND_DISPLAY": "wayland-0",
    }
    assert cua_backend.desktop_identity(dict(env)) == cua_backend.desktop_identity(dict(env))
    assert not cua_backend.backend_display_stale(
        cua_backend.desktop_identity(dict(env)), cua_backend.desktop_identity(dict(env))
    )


def test_desktop_identity_display_only_env_unchanged():
    assert cua_backend.desktop_identity({"DISPLAY": ":37"}) == ":37"
    assert cua_backend.desktop_identity({"HOME": "/x"}) == ""


def test_desktop_identity_tracks_bus_without_display():
    env_a = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus-a"}
    env_b = {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus-b"}
    assert cua_backend.desktop_identity(env_a) != cua_backend.desktop_identity(env_b)
    assert cua_backend.backend_display_stale(
        cua_backend.desktop_identity(env_a), cua_backend.desktop_identity(env_b)
    )
