"""Safety-critical behavior tests for the driveboard serial driver.

Goal: 100% coverage of the safety-relevant API surface, exercised against
the *real* ``SerialLoopClass`` (instantiated but never thread-started) plus a
fake serial device. This validates the actual command encoding and the actual
status-frame parser - the code paths that fire the laser, stop motion, and
react to limit switches / interlocks - without any hardware.

Safety functions covered:
  - intensity()         laser power, clamped to the 0..255 hardware range
  - stop() / unstop()   emergency stop: flush buffer + request stop char
  - pause() / unpause() freeze/resume motion in place
  - homing()            refuses to home while a job is running
  - target_in_workarea  rejects moves outside the bed (offset + machine coords)
  - job_laser_validate  rejects jobs with geometry outside the work area
  - move() / supermove  absolute / machine-coordinate move encoding
  - air/aux/dwell       auxiliary command encoding
  - send_param          28-bit protocol range saturation (no wraparound)
  - RX status parser     limit switches, stop request, watchdog, rx overflow,
                         door interlock, chiller interlock, paused flag
"""

import base64
import copy
import io

import driveboard
import pytest
from helpers import FakeSerialDevice as FakeDevice
from PIL import Image

# ---------------------------------------------------------------------------
# Fakes + helpers
# ---------------------------------------------------------------------------


def decode_param(buf, idx):
    """Decode the 5-byte (4 data + 1 marker) parameter at buf[idx:idx+5].

    Returns (param_char, value) mirroring SerialLoopClass.send_param.
    """
    c0, c1, c2, c3, p = buf[idx], buf[idx + 1], buf[idx + 2], buf[idx + 3], buf[idx + 4]
    num = (c0 - 128) + ((c1 - 128) << 7) + ((c2 - 128) << 14) + ((c3 - 128) << 21)
    val = num / 1000.0 - 134217.728
    return chr(p), val


def encode_param(marker, val):
    """Encode a value the way the *firmware* reports it: 4 data bytes + marker.

    Mirrors firmware serial_write_param so the RX parser can be exercised.
    """
    num = int(round((val + 134217.728) * 1000))
    return bytes(
        [
            (num & 127) + 128,
            ((num & (127 << 7)) >> 7) + 128,
            ((num & (127 << 14)) >> 14) + 128,
            ((num & (127 << 21)) >> 21) + 128,
            ord(marker),
        ]
    )


def feed(loop, data):
    """Push bytes through the real RX parser via a fake device."""
    loop.device = FakeDevice(rx=bytes(data))
    # _serial_read pulls up to RX_CHUNK_SIZE per call; loop until drained.
    while loop.device._rx:
        loop._serial_read()


# ---------------------------------------------------------------------------
# Laser power (intensity) clamping - prevents commanding out-of-range power
# ---------------------------------------------------------------------------


def test_intensity_normal_value(loop):
    driveboard.intensity(50.0)
    assert len(loop.tx_buffer) == 5
    param, val = decode_param(loop.tx_buffer, 0)
    assert param == driveboard.PARAM_INTENSITY
    assert val == pytest.approx(255 * 50 / 100, abs=1e-2)  # ~127.5


def test_intensity_clamps_above_100(loop):
    driveboard.intensity(150.0)  # would be 382.5 raw
    _, val = decode_param(loop.tx_buffer, 0)
    assert val == pytest.approx(255.0, abs=1e-2)


def test_intensity_clamps_below_0(loop):
    driveboard.intensity(-10.0)
    _, val = decode_param(loop.tx_buffer, 0)
    assert val == pytest.approx(0.0, abs=1e-2)


def test_intensity_full_scale(loop):
    driveboard.intensity(100.0)
    _, val = decode_param(loop.tx_buffer, 0)
    assert val == pytest.approx(255.0, abs=1e-2)


# ---------------------------------------------------------------------------
# Emergency stop / resume
# ---------------------------------------------------------------------------


def test_stop_flushes_buffer_and_requests_stop(loop):
    # Pretend a job is mid-flight.
    loop.tx_buffer = bytearray(b"ABCDEF")
    loop.tx_pos = 3
    loop.job_size = 6
    loop._paused = True

    driveboard.stop()

    assert loop.tx_buffer == bytearray()
    assert loop.tx_pos == 0
    assert loop.job_size == 0
    assert loop.request_stop is True
    assert loop._paused is False


def test_unstop_requests_resume(loop):
    driveboard.unstop()
    assert loop.request_resume is True


def test_serial_write_emits_stop_char(loop):
    loop.device = FakeDevice()
    loop.request_stop = True
    loop._serial_write()
    assert ord(driveboard.CMD_STOP) in loop.device.written
    assert loop.request_stop is False


def test_serial_write_resume_resets_status(loop):
    loop.device = FakeDevice()
    loop.firmbuf_used = 42
    loop.request_resume = True
    loop._serial_write()
    assert ord(driveboard.CMD_RESUME) in loop.device.written
    assert loop.firmbuf_used == 0  # resume clears the firmware rx buffer tally
    assert loop.request_resume is False


def test_stop_overrides_pause(loop):
    # A stop while paused must win: it clears the pause, purges the queued job,
    # and requests the stop. (stop() is the host side of "stop overrides pause".)
    loop.tx_buffer = bytearray(b"data")
    driveboard.pause()
    assert loop._paused is True
    driveboard.stop()
    assert loop._paused is False
    assert loop.request_stop is True
    assert loop.tx_buffer == bytearray()


def test_serial_write_sends_stop_char_while_paused(loop):
    # The CMD_STOP char is emitted ahead of the buffer-send gate, so the e-stop
    # reaches the controller even while paused (queued data is NOT sent).
    loop.device = FakeDevice()
    loop.request_status = 0  # suppress the periodic status request prefix
    loop._paused = True
    loop.request_stop = True
    loop.tx_buffer = bytearray(b"queued")
    loop._serial_write()
    assert ord(driveboard.CMD_STOP) in loop.device.written
    assert loop.tx_pos == 0, "queued data must not be sent while paused"


# ---------------------------------------------------------------------------
# Pause / unpause (freeze motion in place, beam off)
# ---------------------------------------------------------------------------


def test_pause_only_when_job_active(loop):
    loop.tx_buffer = bytearray()  # nothing running
    driveboard.pause()
    assert loop._paused is False
    assert loop.request_pause is False


def test_pause_when_job_active(loop):
    loop.tx_buffer = bytearray(b"data")
    driveboard.pause()
    assert loop._paused is True
    assert loop.request_pause is True


def test_unpause(loop):
    loop._paused = True
    driveboard.unpause()
    assert loop._paused is False
    assert loop.request_unpause is True


def test_serial_write_emits_pause_unpause_chars(loop):
    loop.device = FakeDevice()
    loop.request_pause = True
    loop.request_unpause = True
    loop._serial_write()
    assert ord(driveboard.CMD_PAUSE) in loop.device.written
    assert ord(driveboard.CMD_UNPAUSE) in loop.device.written


# ---------------------------------------------------------------------------
# Homing guard - must not home while a job is running
# ---------------------------------------------------------------------------


def test_homing_ignored_while_running(loop):
    loop._status["ready"] = False
    loop._status["stops"] = {}
    driveboard.homing()
    assert ord(driveboard.CMD_HOMING) not in loop.tx_buffer


def test_homing_allowed_when_ready(loop):
    loop._status["ready"] = True
    loop._status["stops"] = {}
    driveboard.homing()
    assert ord(driveboard.CMD_HOMING) in loop.tx_buffer


def test_homing_recovers_from_stop(loop):
    loop._status["ready"] = False
    loop._status["stops"] = {"x1": True}  # in a stop condition
    driveboard.homing()
    assert ord(driveboard.CMD_HOMING) in loop.tx_buffer
    assert loop.request_resume is True


# ---------------------------------------------------------------------------
# Work-area bounds - keep the head on the bed
# ---------------------------------------------------------------------------


def test_target_in_workarea_inside(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    assert driveboard.target_in_workarea(x=600, y=300) is True


def test_target_in_workarea_x_out(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    assert driveboard.target_in_workarea(x=conf_workspace_x() + 1) is False


def test_target_in_workarea_y_out(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    assert driveboard.target_in_workarea(y=conf_workspace_y() + 1) is False


def test_target_in_workarea_respects_offset(loop):
    loop._status["offset"] = [100.0, 50.0, 0.0]
    # With +100 x offset the reachable x range shifts to [-100, w-100].
    assert driveboard.target_in_workarea(x=-50) is True
    assert driveboard.target_in_workarea(x=-150) is False


def test_target_in_workarea_machine_coords_ignores_offset(loop):
    loop._status["offset"] = [100.0, 50.0, 0.0]
    # machine coords bypass offset -> x=-50 is now off the bed
    assert driveboard.target_in_workarea(x=-50, machine_coords=True) is False


def test_target_in_workarea_none_and_z_unbounded(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    # No x/y given -> always inside; z is intentionally not bounded here.
    assert driveboard.target_in_workarea() is True


def conf_workspace_x():
    from config import conf

    return conf["workspace"][0]


def conf_workspace_y():
    from config import conf

    return conf["workspace"][1]


def conf_workspace_z():
    from config import conf

    return conf["workspace"][2]


# ---------------------------------------------------------------------------
# Job validation - reject geometry outside the work area before lasing
# ---------------------------------------------------------------------------


def _path_job(points):
    return {
        "passes": [{"items": [0]}],
        "items": [{"def": 0}],
        "defs": [{"kind": "path", "data": [points]}],
    }


def test_job_validate_accepts_in_bounds(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = _path_job([[10.0, 10.0], [100.0, 100.0]])
    driveboard.job_laser_validate(job)  # must not raise


def test_job_validate_rejects_right_of_bed(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = _path_job([[10.0, 10.0], [conf_workspace_x() + 50, 10.0]])
    with pytest.raises(ValueError, match="right"):
        driveboard.job_laser_validate(job)


def test_job_validate_rejects_negative(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = _path_job([[-5.0, 10.0]])
    with pytest.raises(ValueError, match="left"):
        driveboard.job_laser_validate(job)


def test_job_validate_rejects_top(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = _path_job([[10.0, -5.0]])  # y < 0 -> beyond top
    with pytest.raises(ValueError, match="top"):
        driveboard.job_laser_validate(job)


def test_job_validate_rejects_bottom(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = _path_job([[10.0, conf_workspace_y() + 50]])  # y > limit -> beyond bottom
    with pytest.raises(ValueError, match="bottom"):
        driveboard.job_laser_validate(job)


def test_job_validate_relative_coords_accumulate(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    # Relative steps accumulate; a series that walks off the right edge must fail.
    job = {
        "passes": [{"items": [0], "relative": True}],
        "items": [{"def": 0}],
        "defs": [{"kind": "path", "data": [[[conf_workspace_x(), 0.0], [50.0, 0.0]]]}],
    }
    with pytest.raises(ValueError, match="right"):
        driveboard.job_laser_validate(job)


def test_job_validate_relative_in_bounds(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = {
        "passes": [{"items": [0], "relative": True}],
        "items": [{"def": 0}],
        "defs": [{"kind": "path", "data": [[[10.0, 10.0], [10.0, 10.0]]]}],
    }
    driveboard.job_laser_validate(job)  # must not raise


def test_job_validate_image_bounds(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = {
        "passes": [{"items": [0]}],
        "items": [{"def": 0}],
        "defs": [
            {
                "kind": "image",
                "pos": [10.0, 10.0],
                "size": [conf_workspace_x(), 10.0],  # extends past the right edge
                "data": None,
            }
        ],
    }
    with pytest.raises(ValueError):
        driveboard.job_laser_validate(job)


# ---------------------------------------------------------------------------
# Raster validation - a blank margin is skipped by the engraver, so it must
# not constrain the job. Images are often exported with transparent padding
# around the artwork, which would otherwise fail an in-bounds job.
# ---------------------------------------------------------------------------


def _margin_png(ink=(0.6, 1.0), width=100, height=50):
    """A transparent png with an opaque black band over the `ink` fraction of
    its width, so the rest is margin the engraver will skip."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for x in range(int(width * ink[0]), int(width * ink[1])):
        for y in range(height):
            img.putpixel((x, y), (0, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _image_job(pos, size, data):
    return {
        "passes": [{"items": [0]}],
        "items": [{"def": 0}],
        "defs": [{"kind": "image", "pos": pos, "size": size, "data": data}],
    }


@pytest.mark.parametrize(
    "pos,ink,rejected",
    [
        # hangs 20mm off the left edge, but only the transparent part does
        ([-20.0, 10.0], (0.6, 1.0), None),
        # same geometry, but now the artwork itself reaches past the edge
        ([-20.0, 10.0], (0.0, 1.0), "left"),
        ([conf_workspace_x() - 20.0, 10.0], (0.0, 0.4), None),
        ([conf_workspace_x() - 20.0, 10.0], (0.0, 1.0), "right"),
        # nothing engraves at all, so the placement cannot matter
        ([-500.0, -500.0], (1.0, 1.0), None),
    ],
    ids=["blank-off-left", "ink-off-left", "blank-off-right", "ink-off-right", "all-blank"],
)
def test_job_validate_image_ignores_blank_margins(loop, pos, ink, rejected):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = _image_job(pos, [40.0, 20.0], _margin_png(ink))
    if rejected:
        with pytest.raises(ValueError, match=rejected):
            driveboard.job_laser_validate(job)
    else:
        driveboard.job_laser_validate(job)  # must not raise


def test_job_validate_blank_margin_burns_when_inverted(loop, monkeypatch):
    from config import conf

    # under raster_invert the blank margin is what burns and the artwork is
    # what gets skipped, so the very same job must now be rejected
    monkeypatch.setitem(conf, "raster_invert", True)
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = _image_job([-20.0, 10.0], [40.0, 20.0], _margin_png())
    with pytest.raises(ValueError, match="left"):
        driveboard.job_laser_validate(job)


def test_job_validate_falls_back_when_pixels_unreadable(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    # undecodable data, so the full extent is checked instead of the artwork
    driveboard.job_laser_validate(_image_job([10.0, 10.0], [40.0, 20.0], None))
    with pytest.raises(ValueError, match="left"):
        driveboard.job_laser_validate(_image_job([-20.0, 10.0], [40.0, 20.0], None))


@pytest.mark.parametrize(
    "ink,expected",
    [
        # ink starts at 60% of the width, grown by a pixel on each side to
        # cover dithering error diffused into the margin
        ((0.6, 1.0), (59, 0, 100, 50)),
        ((1.0, 1.0), None),  # nothing to engrave at all
    ],
    ids=["padded", "blank"],
)
def test_raster_engraved_box(ink, expected):
    gray = driveboard._raster_grayscale(_margin_png(ink), 100, 50)
    assert driveboard._raster_engraved_box(gray, 100, 50) == expected


@pytest.mark.parametrize(
    "prefix,fmt",
    [
        ("data:image/png;base64,", "PNG"),
        ("data:image/jpeg;base64,", "JPEG"),  # a character longer than the png one
        ("", "PNG"),  # the job dict documents data as plain base64
    ],
    ids=["png-uri", "jpeg-uri", "bare-base64"],
)
def test_raster_grayscale_reads_any_data_uri(prefix, fmt):
    # the payload starts after the comma, wherever that falls, so nothing may
    # be sliced off the front at a fixed offset
    img = Image.new("RGB", (10, 10), (255, 255, 255))
    img.putpixel((0, 0), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    gray = driveboard._raster_grayscale(prefix + base64.b64encode(buf.getvalue()).decode(), 10, 10)
    assert gray.size == (10, 10)
    assert gray.getextrema()[0] < 255, "the black pixel should have survived"


def test_job_dispatch_validates_before_lasing(loop):
    # The public job() entry must run the work-area gate before queueing output.
    loop._status["offset"] = [0.0, 0.0, 0.0]
    bad = {
        "head": {},
        "passes": [{"items": [0]}],
        "items": [{"def": 0}],
        "defs": [{"kind": "path", "data": [[[conf_workspace_x() + 100, 0.0]]]}],
    }
    with pytest.raises(ValueError):
        driveboard.job(bad)


def _good_job():
    return {
        "head": {},
        "passes": [{"items": [0]}],
        "items": [{"def": 0}],
        "defs": [{"kind": "path", "data": [[[10.0, 10.0]]]}],
    }


def test_job_refused_while_stopped(loop):
    """A stop has to be cleared before a job can be queued on top of it. The
    controller holds its unconsumed rx buffer until a resume, and reports
    neither idle nor drained meanwhile."""
    loop._status["offset"] = [0.0, 0.0, 0.0]
    loop._status["stops"] = {"buffer": True}
    with pytest.raises(ValueError, match="stopped"):
        driveboard.job(_good_job())
    assert loop.tx_buffer == bytearray(), "nothing may be queued behind the stop"


def test_job_refused_while_stopped_names_the_stop(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    loop._status["stops"] = {"x1": True, "door": True}
    with pytest.raises(ValueError, match="door, x1"):
        driveboard.job(_good_job())


def test_job_allowed_once_stop_cleared(loop):
    # stop flags are rebuilt from every status frame, so the guard lifts itself
    loop._status["offset"] = [0.0, 0.0, 0.0]
    loop._status["stops"] = {}
    driveboard.job(_good_job())  # must not raise
    assert loop.tx_buffer, "the job should have been queued"


def test_jobfile_reads_and_validates(loop, tmp_path):
    import json

    loop._status["offset"] = [0.0, 0.0, 0.0]
    bad = {
        "head": {},
        "passes": [{"items": [0]}],
        "items": [{"def": 0}],
        "defs": [{"kind": "path", "data": [[[-50.0, 0.0]]]}],
    }
    p = tmp_path / "bad.dba"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        driveboard.jobfile(str(p))


# ---------------------------------------------------------------------------
# Move encoding
# ---------------------------------------------------------------------------


def test_move_encodes_targets_and_line(loop):
    driveboard.move(x=10.0, y=20.0)
    # two params (5 bytes each) followed by CMD_LINE
    assert len(loop.tx_buffer) == 11
    p0, v0 = decode_param(loop.tx_buffer, 0)
    p1, v1 = decode_param(loop.tx_buffer, 5)
    assert (p0, p1) == (driveboard.PARAM_TARGET_X, driveboard.PARAM_TARGET_Y)
    assert v0 == pytest.approx(10.0, abs=1e-3)
    assert v1 == pytest.approx(20.0, abs=1e-3)
    assert loop.tx_buffer[-1] == ord(driveboard.CMD_LINE)


def test_supermove_clears_offset_and_moves(loop):
    driveboard.supermove(x=5.0)
    cmds = bytes(loop.tx_buffer)
    # supermove brackets the move with offset/ref store+restore and a CMD_LINE
    assert ord(driveboard.CMD_OFFSET_STORE) in cmds
    assert ord(driveboard.CMD_OFFSET_RESTORE) in cmds
    assert cmds[-1] == ord(driveboard.CMD_LINE)


def test_move_with_z(loop):
    driveboard.move(z=3.0)
    p, v = decode_param(loop.tx_buffer, 0)
    assert p == driveboard.PARAM_TARGET_Z
    assert v == pytest.approx(3.0, abs=1e-3)
    assert loop.tx_buffer[-1] == ord(driveboard.CMD_LINE)


def test_supermove_all_axes(loop):
    driveboard.supermove(x=1.0, y=2.0, z=3.0)
    cmds = bytes(loop.tx_buffer)
    # offset params for all three axes are zeroed then targets sent
    assert ord(driveboard.PARAM_OFFSET_X) in cmds
    assert ord(driveboard.PARAM_OFFSET_Y) in cmds
    assert ord(driveboard.PARAM_OFFSET_Z) in cmds
    assert ord(driveboard.PARAM_TARGET_Y) in cmds
    assert ord(driveboard.PARAM_TARGET_Z) in cmds


def test_relative_and_absolute_modes(loop):
    driveboard.relative()
    assert loop.tx_buffer[-1] == ord(driveboard.CMD_REF_RELATIVE)
    driveboard.absolute()
    assert loop.tx_buffer[-1] == ord(driveboard.CMD_REF_ABSOLUTE)


def test_offset_brackets_with_ref_store_restore(loop):
    driveboard.offset(x=10.0, y=20.0, z=1.0)
    cmds = bytes(loop.tx_buffer)
    assert cmds[0] == ord(driveboard.CMD_REF_STORE)
    assert ord(driveboard.CMD_REF_RELATIVE) in cmds
    assert cmds[-1] == ord(driveboard.CMD_REF_RESTORE)
    assert ord(driveboard.PARAM_OFFSET_X) in cmds


def test_absoffset_uses_absolute_ref(loop):
    driveboard.absoffset(x=10.0, y=20.0, z=1.0)
    cmds = bytes(loop.tx_buffer)
    assert ord(driveboard.CMD_REF_ABSOLUTE) in cmds
    assert ord(driveboard.PARAM_OFFSET_Y) in cmds


def test_duration_and_pixelwidth(loop):
    driveboard.duration(0.5)
    p, v = decode_param(loop.tx_buffer, 0)
    assert p == driveboard.PARAM_DURATION
    assert v == pytest.approx(0.5, abs=1e-3)
    loop.tx_buffer = bytearray()
    driveboard.pixelwidth(0.2)
    p, v = decode_param(loop.tx_buffer, 0)
    assert p == driveboard.PARAM_PIXEL_WIDTH
    assert v == pytest.approx(0.2, abs=1e-3)


# ---------------------------------------------------------------------------
# pulse() - single test fire of the laser; must bracket beam-on with air on/off
# ---------------------------------------------------------------------------


def test_pulse_brackets_beam_with_air(loop):
    driveboard.pulse()
    cmds = bytes(loop.tx_buffer)
    # air must be enabled before and disabled after the dwell(s)
    assert cmds[0] == ord(driveboard.CMD_AIR_ENABLE)
    assert cmds[-1] == ord(driveboard.CMD_AIR_DISABLE)
    assert ord(driveboard.CMD_DWELL) in cmds
    # the sequence must end with the beam commanded back to zero intensity
    # (an intensity param with value 0) before air off - guard against a stuck beam
    assert ord(driveboard.PARAM_INTENSITY) in cmds


def test_rastermove_encodes_targets_and_raster_cmd(loop):
    driveboard.rastermove(5.0, 6.0, 0.0)
    cmds = bytes(loop.tx_buffer)
    assert ord(driveboard.PARAM_TARGET_X) in cmds
    assert ord(driveboard.PARAM_TARGET_Y) in cmds
    assert cmds[-1] == ord(driveboard.CMD_RASTER)


def test_rasterdata_brackets_pixels(loop):
    driveboard.rasterdata([0, 128, 255], 0, 3)
    cmds = bytes(loop.tx_buffer)
    assert cmds[0] == ord(driveboard.CMD_RASTER_DATA_START)
    assert cmds[-1] == ord(driveboard.CMD_RASTER_DATA_END)
    # three pixels encoded between the markers (each maps to a 128..255 byte)
    assert len(cmds) == 5


# ---------------------------------------------------------------------------
# Auxiliary commands
# ---------------------------------------------------------------------------


def test_air_on_off(loop):
    driveboard.air_on()
    assert loop.tx_buffer[-1] == ord(driveboard.CMD_AIR_ENABLE)
    driveboard.air_off()
    assert loop.tx_buffer[-1] == ord(driveboard.CMD_AIR_DISABLE)


def test_aux_on_off(loop):
    driveboard.aux_on()
    assert loop.tx_buffer[-1] == ord(driveboard.CMD_AUX_ENABLE)
    driveboard.aux_off()
    assert loop.tx_buffer[-1] == ord(driveboard.CMD_AUX_DISABLE)


def test_dwell(loop):
    driveboard.dwell()
    assert loop.tx_buffer[-1] == ord(driveboard.CMD_DWELL)


# ---------------------------------------------------------------------------
# Parameter range saturation - out-of-range must NOT wraparound
# ---------------------------------------------------------------------------


def test_send_param_saturates_high(loop):
    driveboard.feedrate(200000.0)  # beyond the 28-bit positive range
    _, val = decode_param(loop.tx_buffer, 0)
    max_val = ((1 << 28) - 1) / 1000.0 - 134217.728
    assert val == pytest.approx(max_val, abs=1e-3)


def test_send_param_saturates_low(loop):
    driveboard.feedrate(-200000.0)
    _, val = decode_param(loop.tx_buffer, 0)
    assert val == pytest.approx(-134217.728, abs=1e-3)


# ---------------------------------------------------------------------------
# RX status parser - limit switches and interlocks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker,key",
    [
        (driveboard.ERROR_LIMIT_HIT_X1, "x1"),
        (driveboard.ERROR_LIMIT_HIT_X2, "x2"),
        (driveboard.ERROR_LIMIT_HIT_Y1, "y1"),
        (driveboard.ERROR_LIMIT_HIT_Y2, "y2"),
        (driveboard.ERROR_LIMIT_HIT_Z1, "z1"),
        (driveboard.ERROR_LIMIT_HIT_Z2, "z2"),
    ],
)
def test_rx_limit_switches(loop, marker, key):
    feed(loop, marker.encode("latin-1"))
    assert loop._s["stops"].get(key) is True


@pytest.mark.parametrize(
    "marker,key",
    [
        (driveboard.ERROR_SERIAL_STOP_REQUEST, "requested"),
        (driveboard.ERROR_RX_BUFFER_OVERFLOW, "buffer"),
        (driveboard.ERROR_INVALID_MARKER, "marker"),
        (driveboard.ERROR_INVALID_DATA, "data"),
        (driveboard.ERROR_INVALID_COMMAND, "command"),
        (driveboard.ERROR_INVALID_PARAMETER, "parameter"),
        (driveboard.ERROR_TRANSMISSION_ERROR, "transmission"),
        (driveboard.ERROR_SERIAL_WATCHDOG, "watchdog"),
    ],
)
def test_rx_stop_conditions(loop, marker, key):
    feed(loop, marker.encode("latin-1"))
    assert loop._s["stops"].get(key) is True


def test_rx_limit_sets_ready_in_stop_mode(loop):
    feed(loop, driveboard.ERROR_LIMIT_HIT_X1.encode("latin-1"))
    # In a stop condition the firmware is idle-but-stopped: ready flips True.
    assert loop._s["ready"] is True


def test_rx_door_interlock(loop):
    feed(loop, driveboard.INFO_DOOR_OPEN.encode("latin-1"))
    assert loop._s["info"].get("door") is True


def test_rx_chiller_interlock(loop):
    feed(loop, driveboard.INFO_CHILLER_OFF.encode("latin-1"))
    assert loop._s["info"].get("chiller") is True


def test_rx_paused_flag(loop):
    feed(loop, driveboard.INFO_PAUSED.encode("latin-1"))
    assert loop._s["info"].get("paused") is True


def test_rx_status_end_flips_frame(loop):
    # A door-open flag followed by STATUS_END must surface in the live status.
    payload = driveboard.INFO_DOOR_OPEN + driveboard.STATUS_END
    feed(loop, payload.encode("latin-1"))
    assert loop._status["info"].get("door") is True


def test_rx_idle_sets_ready_when_buffer_empty(loop):
    loop.tx_buffer = bytearray()
    feed(loop, driveboard.INFO_IDLE_YES.encode("latin-1"))
    assert loop._s["ready"] is True


def test_rx_chunk_processed_decrements_firmbuf(loop):
    loop.firmbuf_used = loop.TX_CHUNK_SIZE
    feed(loop, driveboard.CMD_CHUNK_PROCESSED.encode("latin-1"))
    assert loop.firmbuf_used == 0


# ---------------------------------------------------------------------------
# status() reports disconnected safely
# ---------------------------------------------------------------------------


def test_status_when_disconnected(monkeypatch):
    monkeypatch.setattr(driveboard, "connected", lambda: False)
    s = driveboard.status()
    assert s["serial"] is False
    assert s["ready"] is False


def test_status_when_connected(loop, monkeypatch):
    monkeypatch.setattr(driveboard, "connected", lambda: True)
    loop._status = copy.deepcopy(loop._status)
    loop._status["info"]["door"] = True
    s = driveboard.status()
    assert s["serial"] is True
    assert s["info"]["door"] is True


# ---------------------------------------------------------------------------
# RX parameter decode - the reported offset/intensity/position the bounds
# checks and UI rely on. A decode bug here silently corrupts work-area limits.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "marker,value,getter",
    [
        (driveboard.INFO_POS_X, 42.125, lambda s: s["pos"][0]),
        (driveboard.INFO_POS_Y, -7.5, lambda s: s["pos"][1]),
        (driveboard.INFO_OFFSET_X, 12.5, lambda s: s["offset"][0]),
        (driveboard.INFO_OFFSET_Y, 100.0, lambda s: s["offset"][1]),
        (driveboard.INFO_FEEDRATE, 2000.0, lambda s: s["feedrate"]),
    ],
)
def test_rx_param_decode_roundtrip(loop, marker, value, getter):
    feed(loop, encode_param(marker, value))
    assert getter(loop._s) == pytest.approx(value, abs=1e-3)


def test_rx_intensity_scaled_to_percent(loop):
    # Firmware reports raw 0..255; the host rescales to 0..100%.
    feed(loop, encode_param(driveboard.INFO_INTENSITY, 255.0))
    assert loop._s["intensity"] == pytest.approx(100.0, abs=1e-2)


def test_rx_stack_clearance_decoded(loop):
    feed(loop, encode_param(driveboard.INFO_STACK_CLEARANCE, 1234.0))
    assert loop._s["stackclear"] == pytest.approx(1234.0, abs=1e-3)


def test_rx_status_end_progress_with_active_job(loop):
    loop.tx_pos = 5
    loop.job_size = 10
    feed(loop, driveboard.STATUS_END.encode("latin-1"))
    assert loop._status["progress"] == pytest.approx(0.5, abs=1e-3)


# ---------------------------------------------------------------------------
# _serial_write streaming + firmware-buffer stall handling
# ---------------------------------------------------------------------------


def test_serial_write_streams_chunk_doubled(loop):
    loop.device = FakeDevice()
    loop.request_status = 0  # suppress the periodic status request prefix
    loop.tx_buffer = bytearray(b"ABCD")
    loop.tx_pos = 0
    loop._paused = False
    loop.firmbuf_used = 0
    loop._serial_write()
    # protocol duplicates every byte on the wire
    assert bytes(loop.device.written) == b"AABBCCDD"
    assert loop.tx_pos == 4
    assert loop.firmbuf_used == 4


def test_serial_write_paused_sends_nothing(loop):
    loop.device = FakeDevice()
    loop.request_status = 0  # suppress the periodic status request prefix
    loop.tx_buffer = bytearray(b"ABCD")
    loop.tx_pos = 0
    loop._paused = True
    loop._serial_write()
    assert bytes(loop.device.written) == b""
    assert loop.tx_pos == 0


def _stalled_sender(loop, **kwargs):
    """A loop gated on a full firmware buffer, stalled past the timeout."""
    loop.device = FakeDevice()
    loop.request_status = 0  # suppress the periodic status request prefix
    loop.tx_buffer = bytearray(b"ABCD")
    loop.tx_pos = 0
    loop._paused = False
    loop.firmbuf_used = loop.FIRMBUF_SIZE  # no room -> stall branch
    loop.last_tx_progress = 1000.0  # long ago -> stall timeout elapsed
    loop.last_firmware_idle = 0.0  # no idle report
    for key, value in kwargs.items():
        setattr(loop, key, value)
    return loop


def test_serial_write_no_resync_during_interlock(loop):
    # firmware held on a door interlock: the buffer is full, not desynced
    _stalled_sender(loop)
    loop._status["info"]["door"] = True
    loop._serial_write()
    assert loop.firmbuf_used == loop.FIRMBUF_SIZE  # resyncing would overflow it
    assert bytes(loop.device.written) == b""


def test_serial_write_no_resync_while_firmware_is_busy(loop):
    """A stall with no idle report is backpressure, so the tally has to stand.

    The controller consumes a raster line one pixel at a time, holding its rx
    buffer for seconds at a stretch. Clearing the tally there would push a
    second bufferful into a full buffer and overflow it.
    """
    _stalled_sender(loop)
    loop._status["info"] = {}
    loop._serial_write()
    assert loop.firmbuf_used == loop.FIRMBUF_SIZE, "must not resync without proof of drain"
    assert bytes(loop.device.written) == b"", "must not send into a full buffer"


def test_serial_write_resyncs_once_firmware_reports_idle(loop):
    # idle is only reported with an empty rx buffer, so an idle since the last
    # send means the acks were lost rather than the buffer being full
    _stalled_sender(loop, last_firmware_idle=2000.0)
    loop._status["info"] = {}
    loop._serial_write()
    assert loop.firmbuf_used == 0  # resynced so streaming can resume


def test_idle_report_records_drain_proof(loop):
    # the timestamp gating the resync comes off the real status frame
    loop.tx_buffer = bytearray(b"ABCD")  # mid-job, so 'ready' stays False
    loop.last_firmware_idle = 0.0
    feed(loop, driveboard.INFO_IDLE_YES.encode())
    assert loop.last_firmware_idle > 0.0
    assert loop._s["ready"] is False, "a queued job still means not ready"


# ---------------------------------------------------------------------------
# reconnect() - serial-disconnect recovery (recent "pause on disconnect" work)
# ---------------------------------------------------------------------------


def test_reconnect_noop_when_connected(monkeypatch):
    monkeypatch.setattr(driveboard, "connected", lambda: True)
    called = []
    monkeypatch.setattr(driveboard, "connect_withfind", lambda *a, **k: called.append(True))
    driveboard.reconnect()
    assert not called, "reconnect must be a no-op while connected"


def test_reconnect_tears_down_dead_loop(loop, monkeypatch):
    # loop fixture wired it in as the (now dead) SerialLoop.
    loop.device = FakeDevice()
    monkeypatch.setattr(driveboard, "connected", lambda: False)
    called = []
    monkeypatch.setattr(driveboard, "connect_withfind", lambda *a, **k: called.append(True))
    driveboard.reconnect()
    assert loop.device.closed is True, "stale device must be closed before reconnecting"
    assert called, "reconnect must attempt connect_withfind"


# ---------------------------------------------------------------------------
# job() dispatch - laser vs mill vs invalid (the run entry point)
# ---------------------------------------------------------------------------


def test_job_dispatches_to_laser(loop, monkeypatch):
    seen = {}
    monkeypatch.setattr(driveboard, "job_laser", lambda j: seen.setdefault("laser", j))
    monkeypatch.setattr(driveboard, "job_mill", lambda j: seen.setdefault("mill", j))
    driveboard.job({"head": {}, "defs": [], "items": [], "passes": []})
    assert "laser" in seen and "mill" not in seen


def test_job_dispatches_to_mill(loop, monkeypatch):
    seen = {}
    monkeypatch.setattr(driveboard, "job_laser", lambda j: seen.setdefault("laser", j))
    monkeypatch.setattr(driveboard, "job_mill", lambda j: seen.setdefault("mill", j))
    driveboard.job({"head": {"kind": "mill"}, "defs": [], "items": [], "passes": []})
    assert "mill" in seen and "laser" not in seen


def test_job_without_head_is_noop(loop, monkeypatch):
    seen = {}
    monkeypatch.setattr(driveboard, "job_laser", lambda j: seen.setdefault("laser", j))
    monkeypatch.setattr(driveboard, "job_mill", lambda j: seen.setdefault("mill", j))
    driveboard.job({"defs": [], "items": []})  # no 'head'
    assert seen == {}


def test_mill_job_rejects_off_bed_target(loop):
    # job_mill validates G0/G1 targets against the work area, like job_laser.
    way_out = conf_workspace_x() + 10_000
    mill_job = {
        "head": {"kind": "mill"},
        "defs": [{"data": [["G1", [way_out, 10.0, 0.0]]]}],
    }
    with pytest.raises(ValueError, match="work area"):
        driveboard.job(mill_job)
    # nothing should have been queued for the rejected job
    assert ord(driveboard.CMD_LINE) not in loop.tx_buffer


def test_mill_job_rejects_off_bed_z(loop):
    # Z is bounded too (workspace[2]); a z move beyond it is rejected.
    way_down = conf_workspace_z() + 10_000
    mill_job = {
        "head": {"kind": "mill"},
        "defs": [{"data": [["G1", [10.0, 10.0, way_down]]]}],
    }
    with pytest.raises(ValueError, match="z="):
        driveboard.job(mill_job)


def test_mill_job_in_bounds_runs(loop):
    # An in-bounds mill job validates and queues moves (z=0 is within [0, wz]).
    mill_job = {
        "head": {"kind": "mill"},
        "defs": [{"data": [["G0", [10.0, 10.0, 0.0]], ["G1", [20.0, 20.0, 0.0]]]}],
    }
    driveboard.job(mill_job)  # must not raise
    assert ord(driveboard.CMD_LINE) in loop.tx_buffer


# ---------------------------------------------------------------------------
# Aux/air commands must be safe no-ops when no controller is connected.
#
# web.start() calls driveboard.air_off() after attempting to connect; with no
# controller attached SerialLoop is None, so these must not raise (they used to
# crash the server on launch with AttributeError on `SerialLoop.lock`).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fn", ["air_off", "air_on", "aux_off", "aux_on"])
def test_aux_commands_safe_when_disconnected(monkeypatch, fn):
    monkeypatch.setattr(driveboard, "SerialLoop", None)
    getattr(driveboard, fn)()  # must not raise while disconnected
