"""HTTP-layer tests for the bottle web API.

These run the real WSGI app via webtest (no socket, no serial thread). Hardware
calls are monkeypatched at the driveboard boundary so we test routing, auth,
status codes, and the work-area safety wiring - not driveboard internals (those
are covered in test_driveboard_safety.py).
"""

import base64
import copy
import gzip
import io
import json
import os
import time

import driveboard
import jobimport
import pytest
import web  # noqa: F401  (import registers routes on the default app)
from config import conf
from PIL import Image

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


# ---------------------------------------------------------------------------
# Jogging
# ---------------------------------------------------------------------------


@pytest.fixture
def jog(auth_app, monkeypatch):
    """Wire /jog to fakes and record what reaches the driveboard.

    Yields the fake machine status the route reads, so a test can set position,
    offset and readiness, along with the recorded calls.
    """
    web.jog_target_last = None
    calls = []
    stats = {
        "serial": True,
        "ready": True,
        "pos": [0.0, 0.0, 0.0],
        "offset": [0.0, 0.0, 0.0],
    }
    monkeypatch.setattr(driveboard, "connected", lambda: True)
    monkeypatch.setattr(driveboard, "status", lambda: stats)
    monkeypatch.setattr(driveboard, "intensity", lambda v: calls.append(("intensity", v)))
    monkeypatch.setattr(driveboard, "feedrate", lambda v: calls.append(("feedrate", v)))
    monkeypatch.setattr(driveboard, "relative", lambda: calls.append(("relative",)))
    monkeypatch.setattr(driveboard, "absolute", lambda: calls.append(("absolute",)))
    monkeypatch.setattr(driveboard, "move", lambda *a, **k: calls.append(("move", a)))
    monkeypatch.setitem(conf, "workspace", [1220, 610, 0])
    monkeypatch.setitem(conf, "jog_soft_limits", True)
    yield {"app": auth_app, "calls": calls, "stats": stats}
    web.jog_target_last = None


def _jog_moves(calls):
    return [call[1] for call in calls if call[0] == "move"]


def test_jog_forces_zero_intensity_and_absolute_targets(jog):
    resp = jog["app"].get("/jog/1/1/0")
    assert resp.status_int == 200
    # The beam must be commanded to 0% before any jog motion, and the target
    # goes out absolute so it cannot land outside the work area whatever the
    # controller's own idea of where it is.
    assert jog["calls"][0] == ("intensity", 0)
    assert ("relative",) not in jog["calls"]
    assert ("absolute",) in jog["calls"]


@pytest.mark.parametrize(
    "workspace,pos,offset,delta,expected,clamped",
    [
        # inside the work area a step lands exactly where it was asked to
        ([1220, 610, 0], [100, 100, 0], [0, 0, 0], (10, -50, 0), (110, 50, 0), False),
        # would have run into the x2/y2 switches at 1250/650
        ([1220, 610, 0], [1200, 600, 0], [0, 0, 0], (50, 50, 0), (1220, 610, 0), True),
        # and into the origin-side switches at -40/-45
        ([1220, 610, 0], [10, 5, 0], [0, 0, 0], (-50, -50, 0), (0, 0, 0), True),
        # a table offset shifts the range to [-offset, workspace-offset]
        ([1220, 610, 0], [-190, 0, 0], [200, 0, 0], (-50, 0, 0), (-200, 0, 0), True),
        # no z work area configured leaves the focus axis free to jog
        ([1220, 610, 0], [0, 0, 40], [0, 0, 0], (0, 0, 50), (0, 0, 90), False),
        ([1220, 610, 60], [0, 0, 40], [0, 0, 0], (0, 0, 50), (0, 0, 60), True),
        # fresh power-up reports 0,0 whether or not that is true, and clamping
        # at least keeps the jog off the origin-side switch
        ([1220, 610, 0], [0, 0, 0], [0, 0, 0], (-10, -10, 0), (0, 0, 0), True),
    ],
)
def test_jog_clamps_to_workarea(jog, monkeypatch, workspace, pos, offset, delta, expected, clamped):
    monkeypatch.setitem(conf, "workspace", workspace)
    jog["stats"]["pos"] = pos
    jog["stats"]["offset"] = offset
    resp = jog["app"].get(f"/jog/{delta[0]}/{delta[1]}/{delta[2]}")
    assert _jog_moves(jog["calls"]) == [expected]
    assert json.loads(resp.body)["clamped"] is clamped


@pytest.mark.parametrize(
    "pos,expected",
    [
        ([0.0, 0.0, 0.0], [(50, 0, 0), (100, 0, 0), (150, 0, 0)]),
        ([1150.0, 0.0, 0.0], [(1200, 0, 0), (1220, 0, 0), (1220, 0, 0)]),
    ],
)
def test_jog_burst_accumulates_while_moving(jog, pos, expected):
    # The reported position lags a move in flight, so successive jogs have to
    # build on the last target commanded rather than re-clamp against a stale
    # position.
    jog["stats"]["ready"] = False
    jog["stats"]["pos"] = pos
    for _ in range(3):
        jog["app"].get("/jog/50/0/0")
    assert _jog_moves(jog["calls"]) == expected


def test_jog_reseeds_from_position_once_idle(jog):
    jog["stats"]["ready"] = False
    jog["app"].get("/jog/50/0/0")
    # Machine settled somewhere else, e.g. after a job or a /move.
    jog["stats"]["ready"] = True
    jog["stats"]["pos"] = [300.0, 0.0, 0.0]
    jog["app"].get("/jog/50/0/0")
    assert _jog_moves(jog["calls"])[-1] == (350.0, 0.0, 0.0)


def test_jog_tolerates_status_without_position(jog):
    # A disconnect between the connection check and the status read leaves the
    # short status dict, which must not break the route.
    jog["stats"].clear()
    jog["stats"].update({"serial": False, "ready": False})
    resp = jog["app"].get("/jog/10/10/0")
    assert resp.status_int == 200
    assert _jog_moves(jog["calls"]) == [(10.0, 10.0, 0.0)]


def test_jog_soft_limits_off_restores_relative_passthrough(jog, monkeypatch):
    monkeypatch.setitem(conf, "jog_soft_limits", False)
    jog["stats"]["ready"] = False
    jog["stats"]["pos"] = [1200.0, 600.0, 0.0]
    jog["app"].get("/jog/50/50/0")
    assert ("relative",) in jog["calls"]
    assert _jog_moves(jog["calls"]) == [(50.0, 50.0, 0.0)]
    # Turning it back on must not build on what the relative jog left behind.
    monkeypatch.setitem(conf, "jog_soft_limits", True)
    jog["stats"]["pos"] = [400.0, 0.0, 0.0]
    jog["app"].get("/jog/50/0/0")
    assert _jog_moves(jog["calls"])[-1] == (450.0, 0.0, 0.0)


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


# ---------------------------------------------------------------------------
# /load upload forms
# ---------------------------------------------------------------------------

MINIMAL_DBA = json.dumps(
    {
        "head": {},
        "passes": [],
        "items": [{"def": 0}],
        "defs": [{"kind": "path", "data": [[[0, 0, 0], [10, 0, 0]]]}],
    }
)


def _stored_job(isolated_config, name):
    path = os.path.join(isolated_config.conf["stordir"], name + ".dba")
    with open(path) as fp:
        return json.load(fp)


def test_load_accepts_inline_job_field(auth_app, isolated_config):
    body = {"load_request": json.dumps({"job": MINIMAL_DBA, "name": "inline", "optimize": False})}
    resp = auth_app.post("/load", body)
    assert json.loads(resp.body) == "inline"
    assert _stored_job(isolated_config, "inline")["defs"][0]["kind"] == "path"


def test_load_accepts_gzip_file_upload(auth_app, isolated_config):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(MINIMAL_DBA.encode("utf-8"))
    body = {"load_request": json.dumps({"job": "upload", "name": "gz", "optimize": False})}
    resp = auth_app.post("/load", body, upload_files=[("job", "upload.gz", buf.getvalue())])
    assert json.loads(resp.body) == "gz"
    assert _stored_job(isolated_config, "gz")["defs"][0]["kind"] == "path"


def test_load_accepts_raw_file_upload(auth_app, isolated_config):
    body = {"load_request": json.dumps({"job": "upload_raw", "name": "raw", "optimize": False})}
    resp = auth_app.post(
        "/load", body, upload_files=[("job", "upload.dba", MINIMAL_DBA.encode("utf-8"))]
    )
    assert json.loads(resp.body) == "raw"
    assert _stored_job(isolated_config, "raw")["defs"][0]["kind"] == "path"


def test_load_raw_file_upload_sniffs_svg(auth_app, isolated_config, testjobs_dir):
    # type detection has to work on the uploaded bytes, not just on a str
    with open(os.path.join(testjobs_dir, "full-bed.svg"), "rb") as fp:
        svg = fp.read()
    body = {"load_request": json.dumps({"job": "upload_raw", "name": "svgraw", "optimize": False})}
    resp = auth_app.post("/load", body, upload_files=[("job", "full-bed.svg", svg)])
    assert json.loads(resp.body) == "svgraw"
    assert _stored_job(isolated_config, "svgraw")["defs"]


def test_load_upload_marker_without_a_file_is_rejected(auth_app, isolated_config):
    body = {"load_request": json.dumps({"job": "upload_raw", "name": "nofile"})}
    resp = auth_app.post("/load", body, expect_errors=True)
    assert resp.status_int == 400


# ---------------------------------------------------------------------------
# Job import runs in a worker process. In this one it is seconds of C parsing
# that never releases the GIL, which starves the serial thread until the
# controller's watchdog reads the silence as a lost host and stops the machine.
# ---------------------------------------------------------------------------


def test_convert_runs_in_a_worker_process():
    pool = web._convert_pool_get()
    assert pool.submit(os.getpid).result(timeout=60) != os.getpid()


@pytest.mark.parametrize("as_text", [False, True])
def test_convert_matches_an_in_process_convert(as_text, testjobs_dir):
    # str and bytes sources both have to survive the trip through the worker
    with open(os.path.join(testjobs_dir, "key.svg"), "rb") as fp:
        job = fp.read()
    if as_text:
        job = job.decode("utf-8")
    expected = jobimport.dumps(
        jobimport.share_image_data(jobimport.convert(job, optimize=True, matrix=None))
    )
    assert web._convert_job(job, True, None) == (expected, None)


def test_convert_reports_an_unusable_file_as_a_type_error():
    # the exception has to come back from the worker with its type intact,
    # otherwise /load answers 500 instead of 400
    with pytest.raises(TypeError):
        web._convert_job(b"not a job at all", True, None)


def _collect_preview(auth_app, job):
    """POST a preview and poll until the worker has the answer."""
    token = json.loads(auth_app.post_json("/job_preview", {"job": job}).body)["token"]
    for _ in range(200):
        resp = auth_app.get(f"/job_preview/{token}", expect_errors=True)
        if resp.status_int != 200:
            return resp
        body = json.loads(resp.body)
        if not body.get("pending"):
            return body
        time.sleep(0.02)
    raise AssertionError("preview never completed")


def test_job_preview_endpoint_returns_seeks_and_duration(auth_app):
    job = {
        "head": {},
        "passes": [{"items": [0], "feedrate": 2000, "seekrate": 6000, "intensity": 50}],
        "items": [{"def": 0}],
        "defs": [{"kind": "path", "data": [[[0.0, 0.0], [100.0, 0.0]]]}],
    }
    body = _collect_preview(auth_app, job)
    assert body["duration"] > 100.0 / (2000.0 / 60.0)  # the ramps at either end
    assert isinstance(body["seeks"], list)


def test_job_preview_result_is_pending_before_the_worker_finishes(auth_app):
    job = {
        "head": {},
        "passes": [{"items": [0], "feedrate": 2000}],
        "items": [{"def": 0}],
        "defs": [{"kind": "path", "data": [[[0.0, 0.0], [10.0, 0.0]]]}],
    }
    token = json.loads(auth_app.post_json("/job_preview", {"job": job}).body)["token"]
    # an unknown token never resolves, so it reads as pending rather than 404
    body = json.loads(auth_app.get(f"/job_preview/{token + 500}").body)
    assert body["pending"] is True


def test_job_preview_endpoint_reports_an_unreadable_job(auth_app):
    # optimizing runs on the worker, so a job it chokes on surfaces on collect
    job = {
        "head": {},
        "passes": [{"items": [0], "feedrate": 2000}],
        "items": [{"def": 0}],
        "defs": [{"kind": "path", "data": "not a path"}],
    }
    assert _collect_preview(auth_app, job).status_int == 400


def test_job_preview_endpoint_reports_malformed_json(auth_app):
    resp = auth_app.post(
        "/job_preview", b"not json", content_type="application/json", expect_errors=True
    )
    assert resp.status_int == 400


def test_job_preview_endpoint_reports_a_failure_from_the_worker(auth_app):
    # a job that reads fine but cannot be ordered fails on the worker thread,
    # so the error surfaces when the result is collected
    job = {
        "head": {},
        "passes": [{"items": [0], "feedrate": 2000}],
        "items": [{"def": 5}],  # no such def
        "defs": [{"kind": "path", "data": [[[0.0, 0.0], [10.0, 0.0]]]}],
    }
    resp = _collect_preview(auth_app, job)
    assert resp.status_int == 400


def test_job_preview_endpoint_follows_fill_mode(auth_app, monkeypatch):
    # /load applies fill_mode on the way to a run, so the preview has to see
    # it too: unidirectional flies back over every scanline, bidirectional
    # engraves on the way back
    def fill_job():
        lines = [[[10.0, 10.0 + 0.4 * i], [90.0, 10.0 + 0.4 * i]] for i in range(100)]
        return {
            "head": {},
            "passes": [{"items": [0], "feedrate": 2000, "seekrate": 6000, "intensity": 50}],
            "items": [{"def": 0}],
            "defs": [{"kind": "fill", "data": lines}],
        }

    monkeypatch.setitem(conf, "fill_mode", "Forward")
    forward = _collect_preview(auth_app, fill_job())
    monkeypatch.setitem(conf, "fill_mode", "Bidirectional")
    bidi = _collect_preview(auth_app, fill_job())
    assert forward["duration"] > bidi["duration"]


def test_job_preview_shares_pixel_data_between_defs(auth_app):
    # a picture placed many times is sent once, the rest point at it
    img = Image.new("L", (200, 100), 0)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    shared = {
        "head": {},
        "passes": [{"items": [0, 1], "feedrate": 3000, "seekrate": 6000, "pxsize": 0.4}],
        "items": [{"def": 0}, {"def": 1}],
        "defs": [
            {"kind": "image", "pos": [10.0, 10.0], "size": [80.0, 40.0], "data": b64},
            {"kind": "image", "pos": [10.0, 60.0], "size": [80.0, 40.0], "data_of": 0},
        ],
    }
    repeated = copy.deepcopy(shared)
    repeated["defs"][1] = {
        "kind": "image",
        "pos": [10.0, 60.0],
        "size": [80.0, 40.0],
        "data": b64,
    }
    assert _collect_preview(auth_app, shared)["duration"] == pytest.approx(
        _collect_preview(auth_app, repeated)["duration"]
    )


def test_job_preview_reuses_the_answer_for_an_unchanged_job(auth_app):
    # the ordering is the expensive part and most edits do not change the job
    job = {
        "head": {},
        "passes": [{"items": [0], "feedrate": 2000, "seekrate": 6000}],
        "items": [{"def": 0}],
        "defs": [{"kind": "path", "data": [[[0.0, 0.0], [100.0, 0.0], [100.0, 50.0]]]}],
    }
    first = _collect_preview(auth_app, job)
    # served straight from the cache, so it is ready on the first collect
    token = json.loads(auth_app.post_json("/job_preview", {"job": job}).body)["token"]
    body = json.loads(auth_app.get(f"/job_preview/{token}").body)
    assert not body.get("pending")
    assert body["duration"] == first["duration"]


def test_job_preview_cache_notices_a_config_change(auth_app, monkeypatch):
    lines = [[[10.0, 10.0 + 0.4 * i], [90.0, 10.0 + 0.4 * i]] for i in range(60)]
    job = {
        "head": {},
        "passes": [{"items": [0], "feedrate": 2000, "seekrate": 6000}],
        "items": [{"def": 0}],
        "defs": [{"kind": "fill", "data": lines}],
    }
    monkeypatch.setitem(conf, "fill_mode", "Forward")
    forward = _collect_preview(auth_app, job)
    monkeypatch.setitem(conf, "fill_mode", "Bidirectional")
    bidi = _collect_preview(auth_app, job)
    assert forward["duration"] != bidi["duration"]


def test_load_reports_a_truncated_svg_readably(auth_app):
    # a file cut off in transit fails deep in the XML parser, which on its own
    # surfaces as a 500 and a traceback rather than something actionable
    svg = (
        '<?xml version="1.0"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm">'
        '<path d="M 0,0 L 10,10"'  # cut off mid-tag
    )
    body = {
        "load_request": json.dumps(
            {"job": "upload_raw", "name": "truncated", "optimize": False, "overwrite": True}
        )
    }
    resp = auth_app.post(
        "/load", body, upload_files=[("job", "truncated.svg", svg.encode())], expect_errors=True
    )
    assert resp.status_int == 422
    assert "well-formed" in resp.body.decode("utf-8")
    assert "truncated" in resp.body.decode("utf-8")


def test_load_stores_an_unchanged_dba_without_reconverting(auth_app, isolated_config, monkeypatch):
    # the run button posts the job back only so it lands in the queue, and
    # converting it again would copy tens of megabytes through the import
    # worker to arrive at the same bytes
    called = []
    monkeypatch.setattr(web, "_convert_job", lambda *a, **k: called.append(a) or ("{}", None))
    body = {
        "load_request": json.dumps(
            {"job": MINIMAL_DBA, "name": "asis", "optimize": False, "overwrite": True}
        )
    }
    resp = auth_app.post("/load", body)
    assert json.loads(resp.body) == "asis"
    assert called == []  # never reached the converter
    assert _stored_job(isolated_config, "asis")["defs"][0]["kind"] == "path"


def test_load_still_converts_when_optimizing(auth_app, isolated_config):
    body = {
        "load_request": json.dumps(
            {"job": MINIMAL_DBA, "name": "opt", "optimize": True, "overwrite": True}
        )
    }
    resp = auth_app.post("/load", body)
    assert json.loads(resp.body) == "opt"
    # convert() stamps the tolerance it optimized to
    assert _stored_job(isolated_config, "opt")["head"]["optimized"] == conf["tolerance"]


def test_load_still_converts_an_svg_with_optimize_off(auth_app, isolated_config):
    svg = (
        '<?xml version="1.0"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm" '
        'viewBox="0 0 100 100"><path d="M 10,10 L 20,20" stroke="#000"/></svg>'
    )
    body = {
        "load_request": json.dumps(
            {"job": "upload_raw", "name": "svg", "optimize": False, "overwrite": True}
        )
    }
    resp = auth_app.post("/load", body, upload_files=[("job", "a.svg", svg.encode())])
    assert json.loads(resp.body) == "svg"
    assert _stored_job(isolated_config, "svg")["defs"]  # actually parsed
