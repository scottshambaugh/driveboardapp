"""HTTP-layer tests for the bottle web API.

These run the real WSGI app via webtest (no socket, no serial thread). Hardware
calls are monkeypatched at the driveboard boundary so we test routing, auth,
status codes, and the work-area safety wiring - not driveboard internals (those
are covered in test_driveboard_safety.py).
"""

import json

import driveboard
import pytest
import web  # noqa: F401  (import registers routes on the default app)
from config import conf

# `app` fixture is provided by conftest.py.


@pytest.fixture
def auth_app(app):
    app.authorization = ("Basic", ("laser", "laser"))
    return app


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_status_requires_auth(app):
    resp = app.get("/status", expect_errors=True)
    assert resp.status_int == 401


def test_config_requires_auth(app):
    resp = app.get("/config", expect_errors=True)
    assert resp.status_int == 401


def test_auth_accepts_valid_credentials(auth_app, monkeypatch):
    monkeypatch.setattr(driveboard, "connected", lambda: True)
    monkeypatch.setattr(driveboard, "status", lambda: {"serial": True, "ready": True})
    resp = auth_app.get("/status")
    assert resp.status_int == 200


def test_auth_rejects_wrong_password(app):
    app.authorization = ("Basic", ("laser", "wrong"))
    resp = app.get("/config", expect_errors=True)
    assert resp.status_int == 401


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------


def test_status_returns_json(auth_app, monkeypatch):
    monkeypatch.setattr(driveboard, "connected", lambda: True)
    monkeypatch.setattr(
        driveboard, "status", lambda: {"serial": True, "ready": False, "pos": [1, 2, 3]}
    )
    resp = auth_app.get("/status")
    data = json.loads(resp.body)
    assert data["serial"] is True
    assert data["pos"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# /config
# ---------------------------------------------------------------------------


def test_config_get_returns_settings(auth_app):
    resp = auth_app.get("/config")
    data = json.loads(resp.body)
    assert "values" in data
    assert "editable" in data
    assert "defaults" in data
    # users must never be exposed via the API
    assert "users" not in data["values"]


def test_config_set_rejects_non_editable_key(auth_app):
    resp = auth_app.get("/config/network_port/9999")
    data = json.loads(resp.body)
    assert data["status"] == "error"
    # the live config must be unchanged
    assert conf["network_port"] != 9999


# ---------------------------------------------------------------------------
# Machine-required endpoints gate on connection
# ---------------------------------------------------------------------------


def test_homing_requires_machine(auth_app, monkeypatch):
    monkeypatch.setattr(driveboard, "connected", lambda: False)
    resp = auth_app.get("/homing", expect_errors=True)
    assert resp.status_int == 400  # "No machine."


# ---------------------------------------------------------------------------
# Work-area safety wiring on motion endpoints
# ---------------------------------------------------------------------------


def test_move_rejects_outside_workarea(auth_app, monkeypatch):
    monkeypatch.setattr(driveboard, "connected", lambda: True)
    monkeypatch.setattr(driveboard, "target_in_workarea", lambda *a, **k: False)
    calls = []
    monkeypatch.setattr(driveboard, "move", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(driveboard, "intensity", lambda *a, **k: None)
    resp = auth_app.get("/move/5000/5000/0", expect_errors=True)
    assert resp.status_int == 400
    assert not calls, "move must not be issued for an out-of-bounds target"


def test_move_allows_inside_workarea(auth_app, monkeypatch):
    monkeypatch.setattr(driveboard, "connected", lambda: True)
    monkeypatch.setattr(driveboard, "target_in_workarea", lambda *a, **k: True)
    calls = []
    monkeypatch.setattr(driveboard, "move", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(driveboard, "intensity", lambda *a, **k: None)
    resp = auth_app.get("/move/100/100/0")
    assert resp.status_int == 200
    assert calls, "move should be issued for an in-bounds target"


def test_movex_rejects_outside_workarea(auth_app, monkeypatch):
    monkeypatch.setattr(driveboard, "connected", lambda: True)
    monkeypatch.setattr(driveboard, "target_in_workarea", lambda *a, **k: False)
    monkeypatch.setattr(driveboard, "move", lambda *a, **k: None)
    monkeypatch.setattr(driveboard, "intensity", lambda *a, **k: None)
    resp = auth_app.get("/movex/99999", expect_errors=True)
    assert resp.status_int == 400


def test_supermove_uses_machine_coords_bounds(auth_app, monkeypatch):
    monkeypatch.setattr(driveboard, "connected", lambda: True)
    seen = {}
    monkeypatch.setattr(
        driveboard,
        "target_in_workarea",
        lambda *a, **k: seen.update(machine_coords=k.get("machine_coords")) or False,
    )
    monkeypatch.setattr(driveboard, "supermove", lambda *a, **k: None)
    resp = auth_app.get("/supermove/9000/9000/0", expect_errors=True)
    assert resp.status_int == 400
    # supermove must bounds-check in machine coordinates (bypassing offset).
    assert seen.get("machine_coords") is True


def test_jog_forces_zero_intensity(auth_app, monkeypatch):
    monkeypatch.setattr(driveboard, "connected", lambda: True)
    calls = []
    monkeypatch.setattr(driveboard, "intensity", lambda v: calls.append(("intensity", v)))
    monkeypatch.setattr(driveboard, "feedrate", lambda v: calls.append(("feedrate", v)))
    monkeypatch.setattr(driveboard, "relative", lambda: calls.append(("relative",)))
    monkeypatch.setattr(driveboard, "absolute", lambda: calls.append(("absolute",)))
    monkeypatch.setattr(driveboard, "move", lambda *a, **k: calls.append(("move", a)))
    resp = auth_app.get("/jog/1/1/0")
    assert resp.status_int == 200
    # The beam must be commanded to 0% before any jog motion.
    assert calls[0] == ("intensity", 0)
    assert ("move", (1.0, 1.0, 0.0)) in calls


# ---------------------------------------------------------------------------
# Emergency / job-control endpoints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint,fn",
    [
        ("/stop", "stop"),
        ("/unstop", "unstop"),
        ("/pause", "pause"),
        ("/unpause", "unpause"),
        ("/pulse", "pulse"),
        ("/air_on", "air_on"),
        ("/air_off", "air_off"),
    ],
)
def test_emergency_endpoints_invoke_driveboard(auth_app, monkeypatch, endpoint, fn):
    monkeypatch.setattr(driveboard, "connected", lambda: True)
    called = []
    monkeypatch.setattr(driveboard, fn, lambda *a, **k: called.append(True))
    resp = auth_app.get(endpoint)
    assert resp.status_int == 200
    assert called, f"{endpoint} should call driveboard.{fn}"


def test_stop_requires_machine(auth_app, monkeypatch):
    monkeypatch.setattr(driveboard, "connected", lambda: False)
    resp = auth_app.get("/stop", expect_errors=True)
    assert resp.status_int == 400


# ---------------------------------------------------------------------------
# Job execution gates
# ---------------------------------------------------------------------------


def test_run_rejects_when_not_ready(auth_app, monkeypatch):
    monkeypatch.setattr(driveboard, "connected", lambda: True)
    monkeypatch.setattr(driveboard, "status", lambda: {"ready": False})
    monkeypatch.setattr(web, "_get", lambda name: "{}")
    started = []
    monkeypatch.setattr(driveboard, "job", lambda j: started.append(True))
    resp = auth_app.get("/run/somejob", expect_errors=True)
    assert resp.status_int == 400
    assert not started, "a job must not start while the machine is not ready"


def test_run_maps_out_of_bounds_to_422(auth_app, monkeypatch):
    monkeypatch.setattr(driveboard, "connected", lambda: True)
    monkeypatch.setattr(driveboard, "status", lambda: {"ready": True})
    monkeypatch.setattr(web, "_get", lambda name: "{}")

    def _raise(_job):
        raise ValueError("point beyond right of work area")

    monkeypatch.setattr(driveboard, "job", _raise)
    resp = auth_app.get("/run/somejob", expect_errors=True)
    assert resp.status_int == 422  # validation failure surfaced to the client


def test_offset_requires_ready(auth_app, monkeypatch):
    monkeypatch.setattr(driveboard, "connected", lambda: True)
    monkeypatch.setattr(driveboard, "status", lambda: {"ready": False})
    monkeypatch.setattr(driveboard, "offset", lambda *a, **k: None)
    resp = auth_app.get("/offset/1/1/1", expect_errors=True)
    assert resp.status_int == 400


# ---------------------------------------------------------------------------
# /status drives the serial reconnect (the "pause on disconnect" recovery)
# ---------------------------------------------------------------------------


def test_status_triggers_reconnect_when_disconnected(auth_app, monkeypatch):
    monkeypatch.setattr(driveboard, "connected", lambda: False)
    monkeypatch.setattr(driveboard, "status", lambda: {"serial": False, "ready": False})
    reconnects = []
    monkeypatch.setattr(driveboard, "reconnect", lambda: reconnects.append(True))
    monkeypatch.setattr(web, "time_reconnect_last", 0)  # force the throttle window open
    resp = auth_app.get("/status")
    assert resp.status_int == 200
    assert reconnects, "status poll should attempt a reconnect while disconnected"


# ---------------------------------------------------------------------------
# Presets carry pierce_time, and files written before it existed still load
# ---------------------------------------------------------------------------


@pytest.fixture
def presets_dir(tmp_path, monkeypatch):
    monkeypatch.setitem(conf, "confdir", str(tmp_path))
    return tmp_path


def _presets_on_disk(presets_dir):
    with open(presets_dir / "presets.json") as fp:
        return {p["name"]: p for p in json.load(fp)}


def test_save_preset_stores_pierce_time(auth_app, presets_dir):
    auth_app.get("/save_preset/cut/2000/80/0.2/0.4")
    assert _presets_on_disk(presets_dir)["cut"]["pierce_time"] == 0.4


def test_save_preset_without_pierce_time_still_works(auth_app, presets_dir):
    # the older four argument url, kept for clients that predate pierce_time
    auth_app.get("/save_preset/cut/2000/80/0.2")
    assert _presets_on_disk(presets_dir)["cut"]["pierce_time"] == 0.0


def test_listing_presets_migrates_files_without_pierce_time(auth_app, presets_dir):
    with open(presets_dir / "presets.json", "w") as fp:
        json.dump([{"name": "old", "feedrate": 2000, "intensity": 80, "pxsize": 0.2}], fp)
    listed = json.loads(auth_app.get("/listing_presets").body)["presets"]
    assert listed[0]["pierce_time"] == 0.0


def test_delete_preset_still_works(auth_app, presets_dir):
    auth_app.get("/save_preset/cut/2000/80/0.2/0.4")
    auth_app.get("/save_preset/cut/0/0/0/0")
    assert _presets_on_disk(presets_dir) == {}
