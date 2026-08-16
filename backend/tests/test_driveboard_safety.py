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
  - watchdog stall      a host stall only clears the controller's serial
                         watchdog when the machine was idle
"""

import base64
import copy
import io
import threading
import time

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
    loop.tx_buffer = bytearray()  # nothing left to send
    loop._status["ready"] = True  # and the controller has finished it too
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


def test_target_in_workarea_none_is_inside(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    assert driveboard.target_in_workarea() is True


def test_target_in_workarea_z_unbounded_without_a_z_workspace(loop, monkeypatch):
    from config import conf

    # workspace z of 0 means no z axis is configured, so focus jogging is free
    monkeypatch.setitem(conf, "workspace", [1220, 610, 0])
    loop._status["offset"] = [0.0, 0.0, 0.0]
    assert driveboard.target_in_workarea(z=500.0) is True
    assert driveboard.target_in_workarea(z=-500.0) is True


def test_target_in_workarea_bounds_z_when_configured(loop, monkeypatch):
    from config import conf

    monkeypatch.setitem(conf, "workspace", [1220, 610, 100])
    loop._status["offset"] = [0.0, 0.0, 0.0]
    assert driveboard.target_in_workarea(z=50.0) is True
    assert driveboard.target_in_workarea(z=150.0) is False
    assert driveboard.target_in_workarea(z=-1.0) is False


def conf_feedrate():
    from config import conf

    return conf["feedrate"]


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


def _commanded_x(buf):
    """Every PARAM_TARGET_X value in a command stream, in offset coordinates."""
    buf = bytes(buf)
    xs = []
    for i in range(len(buf) - 4):
        if buf[i] >= 128 and chr(buf[i + 4]) == driveboard.PARAM_TARGET_X:
            xs.append(decode_param(buf, i)[1])
    return xs


@pytest.mark.parametrize("offset", [0.0, 100.0, -50.0], ids=["none", "positive", "negative"])
def test_raster_leadout_stays_within_travel_under_an_offset(loop, offset):
    """Raster lead-ins clamp against travel, not the raw machine width.

    move() sends offset coordinates and the controller adds the offset back, so
    clamping to the machine width drives the head past the end of travel by as
    much as the lead-in.
    """
    loop._status["offset"] = [offset, 0.0, 0.0]
    x_min, x_max = -offset, conf_workspace_x() - offset
    width = 40.0
    job = {
        # noreturn keeps the trailing move to origin out of the measurement
        "head": {"noreturn": True},
        "passes": [{"items": [0], "feedrate": 2000, "intensity": 80, "air_assist": "off"}],
        "items": [{"def": 0}],
        # solid image butted right up against the far end of travel
        "defs": [
            {
                "kind": "image",
                "pos": [x_max - width, 10.0],
                "size": [width, 10.0],
                "data": _margin_png((0.0, 1.0)),
            }
        ],
    }
    driveboard.job(job)
    xs = _commanded_x(loop.tx_buffer)
    assert xs, "no moves were emitted"
    assert max(xs) <= x_max + 1e-6, f"lead-out ran {max(xs) - x_max:.1f}mm past travel"
    assert min(xs) >= x_min - 1e-6, f"lead-in ran {x_min - min(xs):.1f}mm past travel"


def _commanded_moves(buf):
    """Every motion command in a program as (command, x, y).

    Parameters latch until a command consumes them. Raster pixel payloads are
    skipped, their bytes are in the same range as parameter data.
    """
    buf = bytes(buf)
    moves = []
    params = {}
    i = 0
    while i < len(buf):
        b = buf[i]
        if b == ord(driveboard.CMD_RASTER_DATA_START):
            i = buf.index(ord(driveboard.CMD_RASTER_DATA_END), i) + 1
        elif b >= 128:
            marker, value = decode_param(buf, i)
            params[marker] = value
            i += 5
        else:
            if chr(b) in (driveboard.CMD_LINE, driveboard.CMD_RASTER):
                moves.append(
                    (
                        chr(b),
                        params.get(driveboard.PARAM_TARGET_X),
                        params.get(driveboard.PARAM_TARGET_Y),
                    )
                )
            i += 1
    return moves


@pytest.mark.parametrize(
    "raster_mode", ["Forward", "Bidirectional", "NearestNeighbor"], ids=str.lower
)
def test_raster_advances_y_along_the_leadout(loop, monkeypatch, raster_mode):
    """A scanline's y advance rides the lead-out rather than standing alone.

    On its own it is a hop of a fraction of a millimetre that the controller
    accelerates and stops the whole gantry for, once per scanline and always
    the same way within an image, which is how a raster loses y steps a vector
    pass never does. Spread over the lead-out's x travel, y is only ever the
    minor axis of a long move.
    """
    from config import conf

    monkeypatch.setitem(conf, "raster_mode", raster_mode)
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = {
        # noreturn keeps the trip home out of the measurement
        "head": {"noreturn": True},
        "passes": [
            {
                "items": [0],
                "feedrate": 2000,
                "intensity": 80,
                "air_assist": "off",
                "pxsize": 0.2,
            }
        ],
        "items": [{"def": 0}],
        # solid, and tall enough for a good few scanlines
        "defs": [
            {
                "kind": "image",
                "pos": [100.0, 100.0],
                "size": [40.0, 4.0],
                "data": _margin_png((0.0, 1.0)),
            }
        ],
    }
    driveboard.job(job)
    moves = _commanded_moves(loop.tx_buffer)
    assert len(moves) > 8, "expected several scanlines"

    scanlines = set()
    prev = None
    for cmd, x, y in moves:
        if prev is not None and y != prev[1]:
            dx, dy = abs(x - prev[0]), abs(y - prev[1])
            assert dx > 1.0, f"y advanced {dy:.3f}mm over only {dx:.3f}mm of x travel"
            assert dy <= driveboard._RASTER_TILT_MAX_SLOPE * dx + 1e-9, (
                f"y advance of {dy:.3f}mm over {dx:.3f}mm of x is too steep a corner"
            )
        if cmd == driveboard.CMD_RASTER:
            scanlines.add(round(y, 6))
            # the burn itself has to stay on its scanline, whatever the
            # lead-out either side of it does
            assert y == prev[1], "raster move is not axis aligned"
        prev = (x, y)

    # every scanline is still engraved exactly once, at its own y
    assert len(scanlines) > 4
    assert scanlines == {round(100.0 + (i + 0.5) * 0.2, 6) for i in range(len(scanlines))}


@pytest.mark.parametrize(
    "offset,returns",
    [(0.0, True), (100.0, True), (-50.0, False)],
    ids=["none", "positive", "origin-off-the-bed"],
)
def test_return_to_origin_respects_travel(loop, offset, returns):
    """The move home is bounds checked like every other move.

    A table offset outside the bed puts the job origin off the machine, and the
    trip home would drive into a hard stop.
    """
    loop._status["offset"] = [offset, 0.0, 0.0]
    x_min = -offset
    job = {
        "head": {},
        "passes": [{"items": [0], "feedrate": 2000, "intensity": 80, "air_assist": "off"}],
        "items": [{"def": 0}],
        "defs": [{"kind": "path", "data": [[[max(x_min, 10.0), 10.0], [max(x_min, 20.0), 20.0]]]}],
    }
    driveboard.job(job)
    xs = _commanded_x(loop.tx_buffer)
    went_home = xs[-1] == pytest.approx(0.0, abs=1e-3)
    assert went_home is returns
    if not returns:
        assert min(xs) >= x_min - 1e-6, "a move ran past the near end of travel"


def test_job_rejects_unreadable_image_data(loop):
    """An image the engraver cannot decode fails before anything is queued.

    The engraver decodes the same bytes, so letting it through validation would
    raise part way through the job with the assists already energised.
    """
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = _image_job([10.0, 10.0], [40.0, 20.0], None)
    job["head"] = {}  # so job() dispatches instead of rejecting the shape
    with pytest.raises(ValueError, match="cannot be read"):
        driveboard.job(job)
    assert loop.tx_buffer == bytearray(), "nothing may be queued for an unreadable image"


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


# ---------------------------------------------------------------------------
# Raster pixel registration
#
# A raster move must travel one pixel width per pixel of data it streams. The
# controller latches a pixel every pixel width from the start of the move, so a
# shorter move leaves trailing pixels unburnt, and a zero length move is one the
# controller's planner drops while the host still streams data for it, stalling
# the protocol loop on a block that never runs.
# ---------------------------------------------------------------------------


def _dots_png(dark, width=40, height=3):
    """An opaque white png with the given (x, y) pixels blacked out."""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    for x, y in dark:
        img.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _raster_runs(buf):
    """(leadin_mm, span_mm, pixel_count) for every raster move in a tx_buffer.

    Each segment is emitted as a seek to the lead-in, a ramp up to the first
    pixel, then the raster move itself, so leadin_mm is the distance between the
    last two line targets and span_mm the distance from there to the raster
    target. Both are unsigned, and comparable to a multiple of pxsize_x.
    """
    runs = []
    lines = []  # x target of each line move so far
    target_x = span = None
    i = 0
    while i < len(buf):
        byte = buf[i]
        if byte == ord(driveboard.CMD_RASTER_DATA_START):
            end = buf.index(ord(driveboard.CMD_RASTER_DATA_END), i)
            runs.append((abs(lines[-1] - lines[-2]), span, end - i - 1))
            i = end + 1
        elif byte >= 128:  # a 4-byte number followed by its parameter marker
            param, value = decode_param(buf, i)
            if param == driveboard.PARAM_TARGET_X:
                target_x = value
            i += 5
        else:
            if chr(byte) == driveboard.CMD_LINE:
                lines.append(target_x)
            elif chr(byte) == driveboard.CMD_RASTER:
                span = abs(target_x - lines[-1])
            i += 1
    return runs


def _emit_image(
    loop, monkeypatch, dark, raster_mode, width=40, height=3, pxsize=0.4, head_pos=None
):
    """Engrave a one-image job, returning (_raster_runs(...), pxsize_x).

    The image is sized so it maps one-to-one onto the raster grid, leaving the
    pixels exactly where they were put.
    """
    from config import conf

    monkeypatch.setitem(conf, "raster_mode", raster_mode)
    pxsize_x, pxsize_y = pxsize / 2.0, pxsize
    def_ = {
        "kind": "image",
        "pos": [100.0, 100.0],
        "size": [width * pxsize_x, height * pxsize_y],
        "data": _dots_png(dark, width, height),
    }
    pass_ = {"air_assist": "off", "pxsize": pxsize}
    driveboard._job_laser_image(def_, pass_, pxsize_x, pxsize_y, 6000.0, 2000.0, 25.5, head_pos)
    return _raster_runs(loop.tx_buffer), pxsize_x


RASTER_MODES = ["Forward", "Reverse", "Bidirectional", "NearestNeighbor"]


@pytest.mark.parametrize("raster_mode", RASTER_MODES)
def test_raster_move_spans_one_pixel_width_per_pixel(loop, monkeypatch, raster_mode):
    # two runs per line, separated by more than 2 * raster_leadin of whitespace
    # so they are emitted as separate segments rather than one merged run
    dark = [(x, y) for y in range(3) for x in (2, 3, 4, 150, 151)]
    runs, pxsize_x = _emit_image(loop, monkeypatch, dark, raster_mode, width=200)
    assert len(runs) == 6, "three lines of two runs each"
    for _leadin, span, pixels in runs:
        assert span == pytest.approx(pixels * pxsize_x, abs=1e-6)


@pytest.mark.parametrize("raster_mode", RASTER_MODES)
def test_single_pixel_raster_move_is_not_zero_length(loop, monkeypatch, raster_mode):
    # the degenerate case: an isolated pixel still travels its own width, or the
    # controller's planner drops the move and the byte is never read
    runs, pxsize_x = _emit_image(loop, monkeypatch, [(20, 1)], raster_mode)
    assert len(runs) == 1
    _leadin, span, pixels = runs[0]
    assert (span, pixels) == (pytest.approx(pxsize_x, abs=1e-6), 1)


@pytest.mark.parametrize("raster_mode", RASTER_MODES)
def test_raster_lead_in_is_the_configured_length(loop, monkeypatch, raster_mode):
    from config import conf

    dark = [(x, 1) for x in range(20, 25)]
    runs, _pxsize_x = _emit_image(loop, monkeypatch, dark, raster_mode)
    assert len(runs) == 1
    assert runs[0][0] == pytest.approx(conf["raster_leadin"], abs=1e-6)


def test_nn_ordering_starts_from_the_given_head_position(loop, monkeypatch):
    from config import conf

    # short lead-ins keep the seek entry points near the segment edges,
    # making the nearest segment unambiguous
    monkeypatch.setitem(conf, "raster_leadin", 1.0)
    # a 3 pixel run top-left and a 5 pixel run bottom-right
    dark = [(x, 0) for x in (2, 3, 4)] + [(x, 2) for x in (30, 31, 32, 33, 34)]

    # no head position: ordering anchors at the image corner, top-left first
    runs, _pxsize_x = _emit_image(loop, monkeypatch, dark, "NearestNeighbor")
    assert [pixels for _leadin, _span, pixels in runs] == [3, 5]

    # head arriving right of the bottom line: bottom-right first
    loop.tx_buffer.clear()
    runs, _pxsize_x = _emit_image(
        loop, monkeypatch, dark, "NearestNeighbor", head_pos=[110.0, 101.1]
    )
    assert [pixels for _leadin, _span, pixels in runs] == [5, 3]


def test_bidirectional_starts_at_the_nearest_corner(loop, monkeypatch):
    from config import conf

    monkeypatch.setitem(conf, "raster_leadin", 1.0)
    # a tall image, a 3 pixel run on the top line and a 5 pixel run on the
    # bottom line, both at the left so the approach seek decides the corner
    dark = [(x, 0) for x in (2, 3, 4)] + [(x, 29) for x in (2, 3, 4, 5, 6)]

    # no head position: top-down as always
    runs, _pxsize_x = _emit_image(loop, monkeypatch, dark, "Bidirectional", height=30)
    assert [pixels for _leadin, _span, pixels in runs] == [3, 5]

    # head arriving below the image: bottom-up
    loop.tx_buffer.clear()
    runs, _pxsize_x = _emit_image(
        loop, monkeypatch, dark, "Bidirectional", height=30, head_pos=[100.0, 115.0]
    )
    assert [pixels for _leadin, _span, pixels in runs] == [5, 3]


def test_bidirectional_first_pass_can_run_right_to_left(loop, monkeypatch):
    from config import conf

    monkeypatch.setitem(conf, "raster_leadin", 1.0)
    # the top line only, a 3 pixel run at the left and a 5 pixel run at the
    # right, separated by more than 2 * raster_leadin so they stay two segments
    dark = [(x, 0) for x in (2, 3, 4)] + [(x, 0) for x in (150, 151, 152, 153, 154)]

    # no head position: left-to-right
    runs, _pxsize_x = _emit_image(loop, monkeypatch, dark, "Bidirectional", width=200)
    assert [pixels for _leadin, _span, pixels in runs] == [3, 5]

    # head arriving right of the image: right-to-left
    loop.tx_buffer.clear()
    runs, _pxsize_x = _emit_image(
        loop, monkeypatch, dark, "Bidirectional", width=200, head_pos=[145.0, 100.0]
    )
    assert [pixels for _leadin, _span, pixels in runs] == [5, 3]


def test_bidirectional_corner_choice_considers_the_next_item(loop, monkeypatch):
    # approach alone slightly favors entering image A at its top, but the
    # next image sits above A, so exiting at the top wins overall: A engraves
    # bottom-up (5 pixel bottom run first) to end near B. B rides in its own
    # pass so item-level reordering cannot pull it ahead of A
    from config import conf

    monkeypatch.setitem(conf, "raster_leadin", 1.0)
    monkeypatch.setitem(conf, "raster_mode", "Bidirectional")
    loop._status["offset"] = [0.0, 0.0, 0.0]
    dark_a = [(x, 0) for x in (2, 3, 4)] + [(x, 29) for x in (2, 3, 4, 5, 6)]
    dark_b = [(x, 1) for x in range(20, 27)]
    job = {
        "head": {"noreturn": True},
        "passes": [
            {"items": [0, 1], "air_assist": "off", "pxsize": 0.4},
            {"items": [2], "air_assist": "off", "pxsize": 0.4},
        ],
        "items": [{"def": 0}, {"def": 1}, {"def": 2}],
        "defs": [
            {"kind": "path", "data": [[[10.0, 10.0], [90.0, 105.0]]]},
            {
                "kind": "image",
                "pos": [100.0, 100.0],
                "size": [40 * 0.2, 30 * 0.4],
                "data": _dots_png(dark_a, 40, 30),
            },
            {
                "kind": "image",
                "pos": [95.0, 88.0],
                "size": [40 * 0.2, 3 * 0.4],
                "data": _dots_png(dark_b, 40, 3),
            },
        ],
    }
    driveboard.job(job)
    runs = _raster_runs(loop.tx_buffer)
    assert [pixels for _leadin, _span, pixels in runs] == [5, 3, 7]


def test_bidirectional_corner_choice_ignores_the_return_home(loop, monkeypatch):
    # the closing seek home is deliberately not modeled, so ordering favors a
    # first cut near the origin: the approach decides, with or without return
    from config import conf

    monkeypatch.setitem(conf, "raster_leadin", 1.0)
    monkeypatch.setitem(conf, "raster_mode", "Bidirectional")
    loop._status["offset"] = [0.0, 0.0, 0.0]
    dark = [(x, 0) for x in (2, 3, 4)] + [(x, 29) for x in (2, 3, 4, 5, 6)]

    def make_job(head):
        return {
            "head": head,
            "passes": [{"items": [0, 1], "air_assist": "off", "pxsize": 0.4}],
            "items": [{"def": 0}, {"def": 1}],
            "defs": [
                {"kind": "path", "data": [[[10.0, 10.0], [90.0, 105.0]]]},
                {
                    "kind": "image",
                    "pos": [100.0, 100.0],
                    "size": [40 * 0.2, 30 * 0.4],
                    "data": _dots_png(dark, 40, 30),
                },
            ],
        }

    driveboard.job(make_job({"noreturn": True}))
    assert [pixels for _l, _s, pixels in _raster_runs(loop.tx_buffer)] == [3, 5]

    loop.tx_buffer.clear()
    loop.job_active = False
    loop._status["ready"] = True
    driveboard.job(make_job({}))
    assert [pixels for _l, _s, pixels in _raster_runs(loop.tx_buffer)] == [3, 5]


def _line_targets(buf):
    """(x, y) target of every line move in a tx_buffer, in emission order."""
    targets = []
    x = y = None
    i = 0
    while i < len(buf):
        byte = buf[i]
        if byte >= 128:  # a 4-byte number followed by its parameter marker
            param, value = decode_param(buf, i)
            if param == driveboard.PARAM_TARGET_X:
                x = value
            elif param == driveboard.PARAM_TARGET_Y:
                y = value
            i += 5
        else:
            if chr(byte) == driveboard.CMD_LINE:
                targets.append((round(x, 3), round(y, 3)))
            i += 1
    return targets


def test_path_polylines_reorder_and_reverse_from_head_position(loop):
    # stored order starts far away, the near polyline should burn first and
    # the backwards-stored one should be entered at its near end
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = {
        "head": {"noreturn": True},
        "passes": [{"items": [0], "air_assist": "off"}],
        "items": [{"def": 0}],
        "defs": [
            {
                "kind": "path",
                "data": [
                    [[50.0, 50.0], [60.0, 50.0]],
                    [[5.0, 5.0], [15.0, 5.0]],
                    [[30.0, 5.0], [20.0, 5.0]],
                ],
            }
        ],
    }
    driveboard.job(job)
    assert _line_targets(loop.tx_buffer) == [
        (5.0, 5.0),
        (15.0, 5.0),
        (20.0, 5.0),
        (30.0, 5.0),
        (50.0, 50.0),
        (60.0, 50.0),
    ]


def test_job_enters_closed_contours_at_the_near_vertex(loop):
    # a closed square stored entering at its far corner burns from the near one
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = {
        "head": {"noreturn": True},
        "passes": [{"items": [0], "air_assist": "off"}],
        "items": [{"def": 0}],
        "defs": [
            {
                "kind": "path",
                "data": [
                    [[60.0, 60.0], [50.0, 60.0], [50.0, 50.0], [60.0, 50.0], [60.0, 60.0]],
                ],
            }
        ],
    }
    driveboard.job(job)
    targets = _line_targets(loop.tx_buffer)
    assert len(targets) == 5
    assert targets[0] == (50.0, 50.0)
    assert targets[-1] == (50.0, 50.0)


def _move_distances(buf):
    """(seek_mm, feed_mm) totals from a tx_buffer, classified by the feedrate
    in effect at each line move. Assumes seekrate is well above feedrate."""
    import math

    seek = feed = 0.0
    x = y = None
    rate = None
    pos = (0.0, 0.0)
    i = 0
    while i < len(buf):
        byte = buf[i]
        if byte >= 128:
            param, value = decode_param(buf, i)
            if param == driveboard.PARAM_TARGET_X:
                x = value
            elif param == driveboard.PARAM_TARGET_Y:
                y = value
            elif param == driveboard.PARAM_FEEDRATE:
                rate = value
            i += 5
        else:
            if chr(byte) == driveboard.CMD_LINE:
                tx = x if x is not None else pos[0]
                ty = y if y is not None else pos[1]
                d = math.dist(pos, (tx, ty))
                if rate and rate > 4000:
                    seek += d
                else:
                    feed += d
                pos = (tx, ty)
            i += 1
    return seek, feed


def _octagon(cx, cy, r):
    import math

    pts = [
        [cx + r * math.cos(math.pi / 4 * i), cy + r * math.sin(math.pi / 4 * i)] for i in range(8)
    ]
    pts.append(pts[0][:])
    return pts


def test_split_closed_paths_beats_whole_loops_on_a_row_of_circles(loop, monkeypatch):
    # three circles in a row: interleaving near and far arcs beats completing
    # each loop, same geometry burned either way
    from config import conf

    loop._status["offset"] = [0.0, 0.0, 0.0]

    def make_job():
        return {
            "head": {},
            "passes": [{"items": [0], "air_assist": "off"}],
            "items": [{"def": 0}],
            "defs": [
                {
                    "kind": "path",
                    "data": [
                        _octagon(60.0, 60.0, 20.0),
                        _octagon(110.0, 60.0, 20.0),
                        _octagon(160.0, 60.0, 20.0),
                    ],
                }
            ],
        }

    monkeypatch.setitem(conf, "split_closed_paths", False)
    driveboard.job(make_job())
    seek_off, feed_off = _move_distances(loop.tx_buffer)

    loop.tx_buffer.clear()
    loop.job_active = False
    loop._status["ready"] = True
    monkeypatch.setitem(conf, "split_closed_paths", True)
    driveboard.job(make_job())
    seek_on, feed_on = _move_distances(loop.tx_buffer)

    assert feed_on == pytest.approx(feed_off, rel=1e-6)
    assert seek_on < seek_off


def test_split_resume_skips_the_pierce(loop, monkeypatch):
    # a resumed split arc sits on already-cut kerf: one pierce per contour
    # with suppression on, one per seek-resumed arc without
    from config import conf

    loop._status["offset"] = [0.0, 0.0, 0.0]

    def make_job():
        return {
            "head": {},
            "passes": [{"items": [0], "air_assist": "off", "pierce_time": 0.5}],
            "items": [{"def": 0}],
            "defs": [
                {
                    "kind": "path",
                    "data": [
                        _octagon(60.0, 60.0, 20.0),
                        _octagon(110.0, 60.0, 20.0),
                        _octagon(160.0, 60.0, 20.0),
                    ],
                }
            ],
        }

    driveboard.job(make_job())
    dwells_on = loop.tx_buffer.count(ord(driveboard.CMD_DWELL))
    assert dwells_on == 3

    loop.tx_buffer.clear()
    loop.job_active = False
    loop._status["ready"] = True
    monkeypatch.setitem(conf, "skip_pierce_on_resume", False)
    driveboard.job(make_job())
    dwells_off = loop.tx_buffer.count(ord(driveboard.CMD_DWELL))
    assert dwells_off > dwells_on


def test_job_seek_preview_reflects_dispatch_ordering():
    # the preview shows the job-time order: nearest polyline first
    job = {
        "head": {"noreturn": True},
        "passes": [{"items": [0]}],
        "items": [{"def": 0}],
        "defs": [
            {
                "kind": "path",
                "data": [
                    [[50.0, 50.0], [60.0, 50.0]],
                    [[5.0, 5.0], [15.0, 5.0]],
                ],
            }
        ],
    }
    seeks = driveboard.job_preview(job)["seeks"]
    assert seeks == [[[0.0, 0.0], [5.0, 5.0]], [[15.0, 5.0], [50.0, 50.0]]]


def test_job_seek_preview_handles_dataless_images():
    # the frontend sends image defs without pixel data, extents suffice
    job = {
        "head": {"noreturn": True},
        "passes": [{"items": [0, 1]}],
        "items": [{"def": 0}, {"def": 1}],
        "defs": [
            {"kind": "image", "pos": [50.0, 50.0], "size": [20.0, 10.0]},
            {"kind": "path", "data": [[[100.0, 100.0], [110.0, 100.0]]]},
        ],
    }
    seeks = driveboard.job_preview(job)["seeks"]
    assert seeks[0] == [[0.0, 0.0], [50.0, 50.0]]
    assert seeks[-1][1] == [100.0, 100.0]


def test_fill_polylines_keep_their_stored_order(loop):
    # fills carry a deliberate scanline order chosen by fill_mode, job
    # dispatch must not reorder them
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = {
        "head": {"noreturn": True},
        "passes": [{"items": [0], "air_assist": "off"}],
        "items": [{"def": 0}],
        "defs": [
            {
                "kind": "fill",
                "data": [
                    [[50.0, 50.0], [60.0, 50.0]],
                    [[5.0, 5.0], [15.0, 5.0]],
                ],
            }
        ],
    }
    driveboard.job(job)
    assert _line_targets(loop.tx_buffer) == [
        (50.0, 50.0),
        (60.0, 50.0),
        (5.0, 5.0),
        (15.0, 5.0),
    ]


def test_job_threads_head_position_between_items(loop, monkeypatch):
    # a path ending right of the image's bottom line makes the NN ordering
    # engrave the bottom segment first
    from config import conf

    monkeypatch.setitem(conf, "raster_leadin", 1.0)
    monkeypatch.setitem(conf, "raster_mode", "NearestNeighbor")
    loop._status["offset"] = [0.0, 0.0, 0.0]
    dark = [(x, 0) for x in (2, 3, 4)] + [(x, 2) for x in (30, 31, 32, 33, 34)]
    job = {
        "head": {"noreturn": True},
        "passes": [{"items": [0, 1], "air_assist": "off", "pxsize": 0.4}],
        "items": [{"def": 0}, {"def": 1}],
        "defs": [
            {"kind": "path", "data": [[[10.0, 10.0], [110.0, 101.1]]]},
            {
                "kind": "image",
                "pos": [100.0, 100.0],
                "size": [40 * 0.2, 3 * 0.4],
                "data": _dots_png(dark, 40, 3),
            },
        ],
    }
    driveboard.job(job)
    runs = _raster_runs(loop.tx_buffer)
    assert [pixels for _leadin, _span, pixels in runs] == [5, 3]


def test_pass_items_reorder_from_head_position(loop):
    # a pass listing the far item first still burns the near item first
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = {
        "head": {"noreturn": True},
        "passes": [{"items": [0, 1], "air_assist": "off"}],
        "items": [{"def": 0}, {"def": 1}],
        "defs": [
            {"kind": "path", "data": [[[200.0, 200.0], [210.0, 200.0]]]},
            {"kind": "path", "data": [[[5.0, 5.0], [15.0, 5.0]]]},
        ],
    }
    driveboard.job(job)
    assert _line_targets(loop.tx_buffer) == [
        (5.0, 5.0),
        (15.0, 5.0),
        (200.0, 200.0),
        (210.0, 200.0),
    ]


def test_pass_items_reorder_considers_the_next_pass(loop):
    # two items equidistant from the start, the one nearer the next pass'
    # first item goes last
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = {
        "head": {"noreturn": True},
        "passes": [
            {"items": [0, 1], "air_assist": "off"},
            {"items": [2], "air_assist": "off"},
        ],
        "items": [{"def": 0}, {"def": 1}, {"def": 2}],
        "defs": [
            {"kind": "path", "data": [[[0.0, 100.0], [0.0, 110.0]]]},
            {"kind": "path", "data": [[[100.0, 0.0], [110.0, 0.0]]]},
            {"kind": "path", "data": [[[0.0, 200.0], [0.0, 210.0]]]},
        ],
    }
    driveboard.job(job)
    assert _line_targets(loop.tx_buffer)[0][0] >= 100.0


def test_relative_pass_keeps_item_order(loop):
    # a relative pass has no known geometry, the listed order stands
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = {
        "head": {"noreturn": True},
        "passes": [{"items": [0, 1], "air_assist": "off", "relative": True}],
        "items": [{"def": 0}, {"def": 1}],
        "defs": [
            {"kind": "path", "data": [[[20.0, 20.0], [21.0, 20.0]]]},
            {"kind": "path", "data": [[[5.0, 5.0], [6.0, 5.0]]]},
        ],
    }
    driveboard.job(job)
    assert _line_targets(loop.tx_buffer)[0] == (20.0, 20.0)


def test_job_seek_preview_matches_item_reordering():
    # the preview walks items in the same re-sequenced order as dispatch
    job = {
        "head": {"noreturn": True},
        "passes": [{"items": [0, 1]}],
        "items": [{"def": 0}, {"def": 1}],
        "defs": [
            {"kind": "path", "data": [[[200.0, 200.0], [210.0, 200.0]]]},
            {"kind": "path", "data": [[[5.0, 5.0], [15.0, 5.0]]]},
        ],
    }
    seeks = driveboard.job_preview(job)["seeks"]
    assert seeks[0] == [[0.0, 0.0], [5.0, 5.0]]
    assert seeks[1] == [[15.0, 5.0], [200.0, 200.0]]


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


def _param_job(**pass_kwargs):
    pass_ = {"items": [0]}
    pass_.update(pass_kwargs)
    return {
        "head": {},
        "passes": [pass_],
        "items": [{"def": 0}],
        "defs": [{"kind": "path", "data": [[[10.0, 10.0], [20.0, 20.0]]]}],
    }


@pytest.mark.parametrize(
    "params,bad",
    [
        ({"feedrate": 0.0}, "feedrate"),
        ({"feedrate": -1000.0}, "feedrate"),
        ({"seekrate": -1.0}, "seekrate"),
        ({"intensity": 150.0}, "intensity"),
        ({"intensity": -1.0}, "intensity"),
        ({"pierce_time": 3600.0}, "pierce_time"),
        ({"pxsize": -0.2}, "pxsize"),
    ],
)
def test_job_rejects_out_of_range_pass_params(loop, params, bad):
    # rejected with a readable error rather than silently clamped on the wire
    loop._status["offset"] = [0.0, 0.0, 0.0]
    with pytest.raises(ValueError, match=bad):
        driveboard.job(_param_job(**params))


def test_job_accepts_in_range_pass_params(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    driveboard.job(_param_job(feedrate=2000.0, intensity=80.0, pierce_time=0.5))


# ---------------------------------------------------------------------------
# Pierce dwell - burn through in place before travelling, so a thick material
# is penetrated before the head sets off.
# ---------------------------------------------------------------------------


def _params_before(buf, command):
    """The (marker, value) params queued ahead of the first `command` byte.

    Single byte commands are interleaved with the 5 byte params, so this scans
    for the param shape rather than striding. Later values win, which is what
    is in effect when the command runs.
    """
    end = bytes(buf).index(ord(command))
    params = []
    i = 0
    while i + 4 < end:
        if all(b >= 128 for b in buf[i : i + 4]) and ord("a") <= buf[i + 4] <= ord("z"):
            params.append(decode_param(buf, i))
            i += 5
        else:
            i += 1
    return params


def test_pierce_dwell_emitted_at_the_cutting_intensity(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    driveboard.job(_param_job(intensity=80.0, pierce_time=0.4))
    buf = bytes(loop.tx_buffer)
    assert ord(driveboard.CMD_DWELL) in buf, "no dwell was queued for the pierce"
    params = dict(_params_before(loop.tx_buffer, driveboard.CMD_DWELL))
    assert params[driveboard.PARAM_DURATION] == pytest.approx(0.4, abs=1e-3)
    assert params[driveboard.PARAM_INTENSITY] == pytest.approx(255 * 0.8, abs=1e-1)


def test_pierce_precedes_the_cut(loop):
    # the dwell has to land after the seek and before the feed moves
    loop._status["offset"] = [0.0, 0.0, 0.0]
    driveboard.job(_param_job(intensity=80.0, pierce_time=0.4))
    buf = bytes(loop.tx_buffer)
    assert buf.index(ord(driveboard.CMD_DWELL)) < buf.rindex(ord(driveboard.CMD_LINE))


def test_no_pierce_when_unset(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    driveboard.job(_param_job(intensity=80.0))
    assert ord(driveboard.CMD_DWELL) not in bytes(loop.tx_buffer)
    assert ord(driveboard.PARAM_DURATION) not in bytes(loop.tx_buffer)


def test_pierce_falls_back_to_the_config_default(loop, monkeypatch):
    # a pass without its own pierce_time takes the machine wide default
    from config import conf

    monkeypatch.setitem(conf, "pierce_time", 0.25)
    loop._status["offset"] = [0.0, 0.0, 0.0]
    driveboard.job(_param_job(intensity=80.0))
    params = dict(_params_before(loop.tx_buffer, driveboard.CMD_DWELL))
    assert params[driveboard.PARAM_DURATION] == pytest.approx(0.25, abs=1e-3)


@pytest.mark.parametrize(
    "key,bad",
    [
        ("pierce_time", 999.0),
        ("pierce_time", "abc"),
        ("feedrate", 0.0),
        ("feedrate", -5.0),
    ],
)
def test_bad_config_default_is_rejected_not_clamped(loop, monkeypatch, key, bad):
    """A config value has to clear the same bounds a pass value does.

    Defaults used to be filled in after validation, so an out of range config
    only met the sender's silent clamp on its way to the machine.
    """
    from config import conf

    monkeypatch.setitem(conf, key, bad)
    loop._status["offset"] = [0.0, 0.0, 0.0]
    with pytest.raises(ValueError, match=f"config {key}"):
        driveboard.job(_param_job(intensity=80.0))


def test_pass_pierce_time_overrides_the_config_default(loop, monkeypatch):
    from config import conf

    monkeypatch.setitem(conf, "pierce_time", 0.25)
    loop._status["offset"] = [0.0, 0.0, 0.0]
    driveboard.job(_param_job(intensity=80.0, pierce_time=0.75))
    params = dict(_params_before(loop.tx_buffer, driveboard.CMD_DWELL))
    assert params[driveboard.PARAM_DURATION] == pytest.approx(0.75, abs=1e-3)


def test_pierce_runs_with_air_assist_already_on(loop):
    # the gas has to be flowing before the burn, not after it
    loop._status["offset"] = [0.0, 0.0, 0.0]
    driveboard.job(_param_job(intensity=80.0, pierce_time=0.4, air_assist="feed"))
    buf = bytes(loop.tx_buffer)
    assert buf.index(ord(driveboard.CMD_AIR_ENABLE)) < buf.index(ord(driveboard.CMD_DWELL))
    assert buf.index(ord(driveboard.CMD_DWELL)) < buf.rindex(ord(driveboard.CMD_AIR_DISABLE))


# ---------------------------------------------------------------------------
# Assist scopes - air and aux are independent outputs, each staying on for one
# burn ('feed'), one pass ('pass'), the whole job ('job') or never ('off').
# ---------------------------------------------------------------------------


def _two_contour_job(**pass_kwargs):
    pass_ = {"items": [0]}
    pass_.update(pass_kwargs)
    return {
        "head": {},
        "passes": [pass_],
        "items": [{"def": 0}],
        "defs": [
            {
                "kind": "path",
                "data": [[[10.0, 10.0], [20.0, 20.0]], [[30.0, 30.0], [40.0, 40.0]]],
            }
        ],
    }


@pytest.mark.parametrize(
    "key,on_cmd,off_cmd",
    [
        ("air_assist", driveboard.CMD_AIR_ENABLE, driveboard.CMD_AIR_DISABLE),
        ("aux_assist", driveboard.CMD_AUX_ENABLE, driveboard.CMD_AUX_DISABLE),
    ],
    ids=["air", "aux"],
)
@pytest.mark.parametrize(
    "mode,expected_ons",
    [("off", 0), ("feed", 1), ("pass", 1), ("job", 1)],
)
def test_assist_scope_cycles(loop, key, on_cmd, off_cmd, mode, expected_ons):
    # the two contours are contiguous, so no mode cycles more than once
    loop._status["offset"] = [0.0, 0.0, 0.0]
    other = "aux_assist" if key == "air_assist" else "air_assist"
    driveboard.job(_two_contour_job(**{key: mode, other: "off"}))
    buf = bytes(loop.tx_buffer)
    assert buf.count(ord(on_cmd)) == expected_ons
    # every job opens by resetting both valves, so an unused assist still has one
    assert buf.count(ord(off_cmd)) == 1 + expected_ons


def test_feed_assist_holds_across_contiguous_contours(loop):
    """Contiguous burns keep the assist on rather than cycling per contour.

    Only the seek to the next contour separates them, so switching each time
    would thrash the relay and never let the gas settle.
    """
    loop._status["offset"] = [0.0, 0.0, 0.0]
    driveboard.job(_two_contour_job(air_assist="feed"))
    buf = bytes(loop.tx_buffer)
    assert buf.count(ord(driveboard.CMD_AIR_ENABLE)) == 1, "cycled per contour"
    # it comes on ahead of the contour's seek, so the gas is already flowing
    # when the head arrives and no command block splits the seek from the burn
    assert buf.index(ord(driveboard.CMD_AIR_ENABLE)) < buf.index(ord(driveboard.CMD_LINE))


def test_feed_assist_skipped_for_a_pass_with_nothing_to_burn(loop):
    # a lone vertex and no pierce only seeks, so the relay stays shut
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = {
        "head": {},
        "passes": [{"items": [0], "intensity": 80, "air_assist": "feed"}],
        "items": [{"def": 0}],
        "defs": [{"kind": "path", "data": [[[10.0, 10.0]]]}],
    }
    driveboard.job(job)
    assert ord(driveboard.CMD_AIR_ENABLE) not in bytes(loop.tx_buffer)


def test_assist_off_is_the_aux_default(loop):
    # aux drives whatever the machine has wired to it, so it stays off unless asked
    loop._status["offset"] = [0.0, 0.0, 0.0]
    driveboard.job(_two_contour_job())
    assert ord(driveboard.CMD_AUX_ENABLE) not in bytes(loop.tx_buffer)


def test_job_scope_spans_every_pass(loop):
    """One enable at the top and one disable at the end, bracketing both passes.

    Only the return to origin falls outside, which runs with the beam off.
    """
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = _two_contour_job(aux_assist="job")
    job["passes"].append({"items": [0], "aux_assist": "job"})
    driveboard.job(job)
    buf = bytes(loop.tx_buffer)
    assert buf.count(ord(driveboard.CMD_AUX_ENABLE)) == 1, "cycled per pass"
    start = buf.index(ord(driveboard.CMD_AUX_ENABLE))
    assert buf.count(ord(driveboard.CMD_AUX_DISABLE), start) == 1, "cycled per pass"
    end = buf.index(ord(driveboard.CMD_AUX_DISABLE), start)
    bracketed = buf.count(ord(driveboard.CMD_LINE), start, end)
    assert bracketed == buf.count(ord(driveboard.CMD_LINE)) - 1


def _cuts_with_assist_off(buf, on_cmd, off_cmd):
    """Whether any burning move runs while a requested assist is off.

    A seek or the return to origin with the assist off is fine, so only moves
    made with a non-zero intensity in effect count.
    """
    buf = bytes(buf)
    on = wanted = False
    intensity = 0.0
    i = 0
    while i < len(buf):
        if buf[i] >= 128 and i + 4 < len(buf) and ord("a") <= buf[i + 4] <= ord("z"):
            if chr(buf[i + 4]) == driveboard.PARAM_INTENSITY:
                _, intensity = decode_param(buf, i)
            i += 5
            continue
        if buf[i] == ord(on_cmd):
            on = wanted = True
        elif buf[i] == ord(off_cmd):
            on = False
        elif buf[i] == ord(driveboard.CMD_LINE) and wanted and not on and intensity > 0:
            return True
        i += 1
    return False


# 'off' is left out: a pass that asks for no assist is supposed to cut dry, and
# the stream carries no pass boundary to tell that apart from a lost hold.
@pytest.mark.parametrize("first", ["feed", "pass", "job"])
@pytest.mark.parametrize("second", ["feed", "pass", "job"])
def test_mixed_assist_scopes_never_cut_with_the_assist_off(loop, first, second):
    """One scope ending must not switch off an output another scope holds.

    A 'feed' or 'pass' pass ordered before a 'job' pass used to tear down the
    shared output, so every cut in the later pass ran with the air relay shut.
    """
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = {
        "head": {},
        "passes": [
            # an intensity, so the moves actually burn and a dark cut is visible
            {"items": [0], "intensity": 80, "air_assist": first, "aux_assist": "off"},
            {"items": [0], "intensity": 80, "air_assist": second, "aux_assist": "off"},
        ],
        "items": [{"def": 0}],
        "defs": [{"kind": "path", "data": [[[10.0, 10.0], [20.0, 20.0]]]}],
    }
    driveboard.job(job)
    assert not _cuts_with_assist_off(
        loop.tx_buffer, driveboard.CMD_AIR_ENABLE, driveboard.CMD_AIR_DISABLE
    ), f"air off during a cut with scopes {first} then {second}"


def test_job_scope_survives_an_inner_pass_teardown(loop):
    # the job hold keeps the output up across the whole run, one on and one off
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = {
        "head": {},
        "passes": [
            {"items": [0], "air_assist": "feed"},
            {"items": [0], "air_assist": "job"},
        ],
        "items": [{"def": 0}],
        "defs": [{"kind": "path", "data": [[[10.0, 10.0], [20.0, 20.0]]]}],
    }
    driveboard.job(job)
    buf = bytes(loop.tx_buffer)
    assert buf.count(ord(driveboard.CMD_AIR_ENABLE)) == 1
    start = buf.index(ord(driveboard.CMD_AIR_ENABLE))
    assert buf.count(ord(driveboard.CMD_AIR_DISABLE), start) == 1


def test_a_pass_failure_stops_and_purges_the_partial_job(loop, monkeypatch):
    """A partial program must not survive a failure after dispatch begins."""
    loop._status["offset"] = [0.0, 0.0, 0.0]

    def boom(*a, **k):
        raise RuntimeError("decoder exploded")

    monkeypatch.setattr(driveboard, "_job_laser_path", boom)
    job = {
        "head": {},
        "passes": [{"items": [0], "intensity": 80, "air_assist": "job", "aux_assist": "job"}],
        "items": [{"def": 0}],
        "defs": [{"kind": "path", "data": [[[10.0, 10.0], [20.0, 20.0]]]}],
    }
    with pytest.raises(RuntimeError):
        driveboard.job(job)
    assert loop.tx_buffer == bytearray()
    assert loop.request_stop is True
    assert not any(driveboard._assist_holds.values()), "holds left out of step with the hardware"


@pytest.mark.parametrize("key", ["air_assist", "aux_assist"])
def test_job_rejects_unknown_assist_mode(loop, key):
    # an unrecognised mode would otherwise match no branch and run with it off
    loop._status["offset"] = [0.0, 0.0, 0.0]
    with pytest.raises(ValueError, match=key):
        driveboard.job(_two_contour_job(**{key: "pas"}))


def test_job_coerces_numeric_strings(loop):
    # svg cut setting tags carry their values as strings
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = _param_job(feedrate="2550", intensity="100")
    driveboard.job(job)
    assert job["passes"][0]["feedrate"] == 2550.0
    assert job["passes"][0]["intensity"] == 100.0


def test_job_treats_empty_pass_params_as_unset(loop):
    # an svg tag leaves unset fields empty, which must mean 'use the default'
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = _param_job(feedrate="", intensity="80")
    driveboard.job(job)
    assert job["passes"][0]["feedrate"] == conf_feedrate()


def test_job_rejects_non_numeric_pass_params(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    with pytest.raises(ValueError, match="not a number"):
        driveboard.job(_param_job(feedrate="fast"))


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


def test_supermove_forces_the_beam_off(loop):
    """A positioning move zeroes intensity itself. Intensity persists in the
    controller between commands, so a stopped job leaves it at its cutting
    value and the next rapid would cross the bed with the beam on."""
    loop.tx_buffer = bytearray()
    driveboard.supermove(x=5.0)
    param, val = decode_param(loop.tx_buffer, 0)
    assert param == driveboard.PARAM_INTENSITY
    assert val == pytest.approx(0.0, abs=1e-3)
    # and it lands ahead of the move that it has to cover
    assert bytes(loop.tx_buffer).index(ord(driveboard.PARAM_INTENSITY)) < bytes(
        loop.tx_buffer
    ).index(ord(driveboard.PARAM_TARGET_X))


@pytest.mark.parametrize("axis", ["x", "y", "z"])
def test_supermove_forces_the_beam_off_on_every_axis(loop, axis):
    driveboard.supermove(**{axis: 1.0})
    param, val = decode_param(loop.tx_buffer, 0)
    assert param == driveboard.PARAM_INTENSITY
    assert val == pytest.approx(0.0, abs=1e-3)


def test_move_leaves_intensity_alone(loop):
    # move() is the cutting primitive, so it carries whatever intensity the
    # pass set. Manual jogs zero the beam at their route instead.
    driveboard.move(x=5.0)
    assert ord(driveboard.PARAM_INTENSITY) not in bytes(loop.tx_buffer)


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


def test_rastermove_leaves_unnamed_axes_alone(loop):
    # a raster line only names x and y, so a z the head was jogged to (e.g. a
    # focus) must not be commanded back to the offset origin on every line
    driveboard.rastermove(5.0, 6.0)
    cmds = bytes(loop.tx_buffer)
    assert ord(driveboard.PARAM_TARGET_Z) not in cmds


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


@pytest.mark.parametrize(
    "fn,val,expected",
    [
        # a feed rate at or below zero reaches the planner as a zero or
        # wrapped step rate, and divides into the beam dynamics intensity
        (driveboard.feedrate, 0.0, driveboard.MIN_FEEDRATE),
        (driveboard.feedrate, -1000.0, driveboard.MIN_FEEDRATE),
        (driveboard.feedrate, 1e9, driveboard.MAX_FEEDRATE),
        (driveboard.feedrate, 2000.0, 2000.0),
        # a dwell holds the beam on in one spot for its whole duration
        (driveboard.duration, -1.0, 0.0),
        (driveboard.duration, 1e9, driveboard.MAX_DWELL_SECONDS),
        (driveboard.duration, 0.1, 0.1),
        (driveboard.pixelwidth, -0.5, 0.0),
        (driveboard.pixelwidth, 0.2, 0.2),
    ],
)
def test_motion_params_clamped_before_the_wire(loop, fn, val, expected):
    fn(val)
    _, sent = decode_param(loop.tx_buffer, 0)
    assert sent == pytest.approx(expected, abs=1e-3)


@pytest.mark.parametrize("val,expected", [(-50.0, 0.0), (150.0, 255.0), (50.0, 127.5)])
def test_intensity_clamped_before_the_wire(loop, val, expected):
    driveboard.intensity(val)
    _, sent = decode_param(loop.tx_buffer, 0)
    assert sent == pytest.approx(expected, abs=1e-2)


def test_send_param_saturates_high(loop):
    # a target is not range-clamped by its caller, so it reaches the encoder
    loop.send_param(driveboard.PARAM_TARGET_X, 200000.0)  # beyond the 28-bit range
    _, val = decode_param(loop.tx_buffer, 0)
    max_val = ((1 << 28) - 1) / 1000.0 - 134217.728
    assert val == pytest.approx(max_val, abs=1e-3)


def test_send_param_saturates_low(loop):
    loop.send_param(driveboard.PARAM_TARGET_X, -200000.0)
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


def test_rx_version_decoded_with_patch_digit(loop):
    # old firmware sends hundredths, x10 values carry a patch digit last
    feed(loop, encode_param(driveboard.INFO_VERSION, 2608.0))
    assert loop._s["firmver"] == "26.08"
    feed(loop, encode_param(driveboard.INFO_VERSION, 26081.0))
    assert loop._s["firmver"] == "26.08.1"
    feed(loop, encode_param(driveboard.INFO_VERSION, 26090.0))
    assert loop._s["firmver"] == "26.09"


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


# ---------------------------------------------------------------------------
# Serial watchdog stall recovery.
#
# The controller stops itself when it hears nothing for a second. Heavy host
# work outlasts that: importing a large file runs C parsers that hold the
# interpreter, so the serial thread never gets to send its status poll. With
# the machine sitting idle that stop protects nothing, and the operator is
# left with a red status and a re-home. The loop pre-empts it with a resume,
# but only when the machine was idle with nothing queued. Every other stall
# leaves the stop standing.
# ---------------------------------------------------------------------------


def _stall(loop, seconds=1.2):
    """Backdate the last write so the loop sees a stall of that length."""
    loop.last_tx_time = time.time() - seconds


def _idle(loop):
    """Put the loop in the state a controller sitting idle reports."""
    loop._status["ready"] = True
    loop._status["stops"] = {}
    loop.tx_buffer = bytearray()
    loop._paused = False


def test_watchdog_stall_resumes_when_idle(loop):
    loop.device = FakeDevice()
    _idle(loop)
    _stall(loop)
    loop._serial_write()
    assert ord(driveboard.CMD_RESUME) in loop.device.written


def test_watchdog_resume_precedes_the_next_status_request(loop):
    # The resume has to reach the controller ahead of any status request,
    # otherwise the frame in between reports the stop and the UI goes red.
    loop.device = FakeDevice()
    _idle(loop)
    loop.request_status = 2
    _stall(loop)
    loop._serial_write()
    written = bytes(loop.device.written)
    assert ord(driveboard.CMD_RESUME) in written
    assert ord(driveboard.CMD_STATUS) not in written
    assert ord(driveboard.CMD_SUPERSTATUS) not in written
    assert loop.request_status == 2, "status request re-armed for the next pass"


def test_watchdog_resume_keeps_cached_status(loop):
    # Unlike unstop(), this resume must not wipe the cached frame: the
    # controller held its position and settings right through the stop.
    loop.device = FakeDevice()
    _idle(loop)
    loop._status["pos"] = [10.0, 20.0, 0.0]
    loop._status["firmver"] = "1.5"
    loop.firmbuf_used = 42
    _stall(loop)
    loop._serial_write()
    assert loop._status["pos"] == [10.0, 20.0, 0.0]
    assert loop._status["firmver"] == "1.5"
    assert loop.firmbuf_used == 0  # a resume clears the controller's rx buffer


def test_watchdog_stall_leaves_stop_standing_mid_job(loop):
    # Controller busy running a job: the watchdog aborted real motion.
    loop.device = FakeDevice()
    _idle(loop)
    loop._status["ready"] = False
    _stall(loop)
    loop._serial_write()
    assert ord(driveboard.CMD_RESUME) not in loop.device.written


def test_watchdog_stall_leaves_stop_standing_with_queued_data(loop):
    loop.device = FakeDevice()
    _idle(loop)
    loop.tx_buffer = bytearray(b"queued")
    _stall(loop)
    loop._serial_write()
    assert ord(driveboard.CMD_RESUME) not in loop.device.written


def test_watchdog_stall_leaves_stop_standing_while_paused(loop):
    loop.device = FakeDevice()
    _idle(loop)
    loop._paused = True
    _stall(loop)
    loop._serial_write()
    assert ord(driveboard.CMD_RESUME) not in loop.device.written


def test_watchdog_stall_leaves_a_real_stop_standing(loop):
    # A limit hit is not something the host may clear behind the operator.
    loop.device = FakeDevice()
    _idle(loop)
    loop._status["stops"] = {"x1": True}
    _stall(loop)
    loop._serial_write()
    assert ord(driveboard.CMD_RESUME) not in loop.device.written


def test_watchdog_stall_does_not_pre_empt_a_requested_stop(loop):
    loop.device = FakeDevice()
    _idle(loop)
    loop.request_stop = True
    _stall(loop)
    loop._serial_write()
    written = bytes(loop.device.written)
    assert ord(driveboard.CMD_STOP) in written
    assert ord(driveboard.CMD_RESUME) not in written


def test_normal_cadence_never_resumes(loop):
    # The 0.4s status cadence keeps the watchdog fed, so there is nothing to
    # recover from and no resume may be sent.
    loop.device = FakeDevice()
    _idle(loop)
    loop.last_tx_time = time.time()
    loop._serial_write()
    assert ord(driveboard.CMD_RESUME) not in loop.device.written


class WatchdogDevice:
    """Fake controller applying the firmware's serial watchdog rule.

    Every received byte feeds the watchdog (firmware USART_RX_vect). A second
    without one trips a stop that only CMD_RESUME clears, as in
    firmware/src/serial.c.
    """

    def __init__(self, timeout=1.0):
        self.timeout = timeout
        self.last_rx = time.time()
        self.stopped = False
        self.stop_count = 0
        self.written = bytearray()

    def _tick(self):
        if not self.stopped and time.time() - self.last_rx > self.timeout:
            self.stopped = True
            self.stop_count += 1

    def read(self, n):
        self._tick()
        return b""

    def write(self, data):
        self._tick()  # a byte arriving after the timeout is already too late
        self.last_rx = time.time()
        self.written.extend(data)
        if ord(driveboard.CMD_RESUME) in bytes(data):
            self.stopped = False
        return len(data)

    def flushOutput(self):
        pass

    def flushInput(self):
        pass

    def close(self):
        pass


def test_host_stall_does_not_leave_the_controller_stopped():
    """A stalled serial thread must not cost the operator a re-home.

    Holding the loop's lock reproduces what a large file import does: keep the
    serial thread off the wire. The fake controller applies the firmware's
    watchdog rule to the resulting silence.
    """
    dev = WatchdogDevice()
    sl = driveboard.SerialLoopClass()
    sl.device = dev
    sl._status["ready"] = True  # controller sitting idle
    sl.start()
    try:
        time.sleep(0.5)  # normal status cadence
        assert dev.stopped is False, "the normal cadence must feed the watchdog"
        with sl.lock:  # the stall
            mark = len(dev.written)
            time.sleep(1.4)
        time.sleep(0.5)  # let the loop recover
        assert dev.stop_count >= 1, "the stall should have tripped the watchdog"
        assert dev.stopped is False, "watchdog stop left standing after the stall"
        after = bytes(dev.written[mark:])
        assert after[:1] == driveboard.CMD_RESUME.encode("latin-1"), (
            "the resume must be the first thing sent after the stall"
        )
    finally:
        sl.stop_processing = True
        sl.join(timeout=5)


def test_host_stall_mid_job_leaves_the_controller_stopped():
    """The same stall during a job must leave the stop for the operator.

    Motion was aborted mid-cut, so silently resuming would hide it.
    """
    dev = WatchdogDevice()
    sl = driveboard.SerialLoopClass()
    sl.device = dev
    sl._status["ready"] = False  # controller running a job
    sl.tx_buffer = bytearray(b"A" * 64)
    sl.job_size = 64
    sl.start()
    try:
        time.sleep(0.3)
        with sl.lock:
            time.sleep(1.4)
        time.sleep(0.5)
        assert dev.stop_count >= 1
        assert dev.stopped is True, "a stop that aborted a job must stand"
    finally:
        sl.stop_processing = True
        sl.join(timeout=5)


# ---------------------------------------------------------------------------
# Run-time estimate (analytical, out of the same ordering pass as the seeks)
# ---------------------------------------------------------------------------


def _duration_job(**pass_kw):
    pass_ = {"items": [0], "feedrate": 2000, "seekrate": 6000, "intensity": 50}
    pass_.update(pass_kw)
    return {
        "head": {},
        "passes": [pass_],
        "items": [{"def": 0}],
        "defs": [
            {
                "kind": "path",
                "data": [[[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0], [0.0, 0.0]]],
            }
        ],
    }


def _duration(job):
    return driveboard.job_preview(job)["duration"]


def test_job_duration_needs_no_machine():
    # the info line has to show a time before anything is plugged in
    assert driveboard.SerialLoop is None
    assert _duration(_duration_job()) > 0.0


def test_job_duration_exceeds_length_over_feedrate():
    # the ramps and corners the old length/rate estimate ignored
    naive = 400.0 / (2000.0 / 60.0)
    assert _duration(_duration_job()) > naive


def test_job_duration_scales_with_feedrate():
    assert _duration(_duration_job(feedrate=1000)) > _duration(_duration_job(feedrate=4000))


def test_job_duration_counts_pierce_time():
    # one open polyline, so exactly one burn and one pierce to account for
    def open_path_job(pierce):
        return {
            "head": {},
            "passes": [{"items": [0], "feedrate": 2000, "seekrate": 6000, "pierce_time": pierce}],
            "items": [{"def": 0}],
            "defs": [{"kind": "path", "data": [[[0.0, 0.0], [100.0, 0.0], [100.0, 100.0]]]}],
        }

    plain = _duration(open_path_job(0.0))
    pierced = _duration(open_path_job(2.0))
    assert pierced == pytest.approx(plain + 2.0, abs=0.01)


def test_job_duration_leaves_the_job_and_the_serial_loop_alone(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = _duration_job()
    before = copy.deepcopy(job)
    assert _duration(job) > 0.0
    assert job == before  # the estimate reads the job, it does not rewrite it
    assert loop.tx_buffer == bytearray()
    assert loop.job_size == 0


def test_job_duration_runs_while_a_job_is_on_the_wire(loop):
    # nothing is emitted to estimate, so an edit mid-run still gets an answer
    loop._status["offset"] = [0.0, 0.0, 0.0]
    driveboard.job(_duration_job())
    assert loop.tx_buffer
    assert _duration(_duration_job()) > 0.0


def test_job_duration_follows_raster_mode(monkeypatch):
    # a unidirectional raster flies back over every scanline, a bidirectional
    # one engraves on the way back, so the config choice changes the time
    job = {
        "head": {},
        "passes": [{"items": [0], "feedrate": 3000, "seekrate": 6000, "intensity": 50}],
        "items": [{"def": 0}],
        "defs": [{"kind": "image", "pos": [10.0, 10.0], "size": [80.0, 40.0]}],
    }
    monkeypatch.setitem(driveboard.conf, "raster_mode", "Forward")
    forward = _duration(job)
    monkeypatch.setitem(driveboard.conf, "raster_mode", "Bidirectional")
    bidi = _duration(job)
    assert forward > bidi


def test_job_duration_times_images_without_pixel_data():
    # the frontend sends image defs slim, the estimate works off the extent
    job = {
        "head": {},
        "passes": [{"items": [0], "feedrate": 3000, "seekrate": 6000, "pxsize": 0.4}],
        "items": [{"def": 0}],
        "defs": [{"kind": "image", "pos": [10.0, 10.0], "size": [80.0, 40.0]}],
    }
    # 40mm of scanlines at 0.4mm is 100 lines, each crossing at least the 80mm
    assert _duration(job) > 100 * 80.0 / (3000.0 / 60.0)


def test_job_duration_scales_with_image_area():
    def image_job(w, h):
        return {
            "head": {},
            "passes": [{"items": [0], "feedrate": 3000, "seekrate": 6000, "pxsize": 0.4}],
            "items": [{"def": 0}],
            "defs": [{"kind": "image", "pos": [10.0, 10.0], "size": [w, h]}],
        }

    assert _duration(image_job(80.0, 80.0)) > _duration(image_job(80.0, 40.0))


def test_dispatch_marks_every_pass_for_the_time_left(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = _duration_job()
    job["passes"] = [dict(job["passes"][0]), dict(job["passes"][0])]
    driveboard.job(job)
    # one mark per pass, plus the end of the last one
    assert len(loop.timer._pass_starts) == 3
    assert loop.timer._pass_starts == sorted(loop.timer._pass_starts)


def test_remaining_in_pass_tracks_the_pass_being_run(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = _duration_job()
    job["passes"] = [dict(job["passes"][0]), dict(job["passes"][0])]
    driveboard.job(job)
    first_pass_end = loop.timer._pass_starts[1]
    # in the first pass, what is left of it is less than what is left overall
    assert loop.timer.remaining_in_pass(1) < loop.timer.remaining(1)
    # in the last pass, only the run home separates the two
    late = first_pass_end + 1
    assert loop.timer.remaining_in_pass(late) <= loop.timer.remaining(late)


def test_estimate_and_dispatch_agree_on_a_vector_job(loop):
    # the analytical estimate and the model that watches the real command
    # stream are two views of the same machine, so they must not drift
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = _duration_job()
    estimate = _duration(job)
    driveboard.job(copy.deepcopy(job))
    assert loop.timer.total() == pytest.approx(estimate, rel=0.02)


def _raster_time_job(b64, w=80.0, h=40.0, px=0.4, **pass_kw):
    pass_ = {"items": [0], "feedrate": 3000, "seekrate": 6000, "intensity": 50, "pxsize": px}
    pass_.update(pass_kw)
    return {
        "head": {"noreturn": True},
        "passes": [pass_],
        "items": [{"def": 0}],
        "defs": [{"kind": "image", "data": b64, "pos": [10.0, 10.0], "size": [w, h]}],
    }


def _png(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def test_job_duration_skips_rows_that_engrave_nothing():
    # dispatch skips a blank scanline outright, so the estimate must too
    full = Image.new("L", (200, 100), 0)
    half = Image.new("L", (200, 100), 255)
    for y in range(50):
        for x in range(200):
            half.putpixel((x, y), 0)
    assert _duration(_raster_time_job(_png(half))) == pytest.approx(
        _duration(_raster_time_job(_png(full))) / 2.0, rel=0.05
    )


def test_job_duration_follows_how_far_across_a_row_engraves():
    wide = Image.new("L", (200, 100), 255)
    narrow = Image.new("L", (200, 100), 255)
    for y in range(100):
        for x in range(200):
            wide.putpixel((x, y), 0)
        for x in range(20):
            narrow.putpixel((x, y), 0)
    assert _duration(_raster_time_job(_png(wide))) > _duration(_raster_time_job(_png(narrow)))


def test_job_duration_matches_dispatch_on_a_sparse_image(loop):
    # the pixel scan exists so a mostly blank raster is not read as a full one
    loop._status["offset"] = [0.0, 0.0, 0.0]
    img = Image.new("L", (200, 100), 255)
    for y in range(40, 60):
        for x in range(80, 120):
            img.putpixel((x, y), 0)
    job = _raster_time_job(_png(img))
    estimate = _duration(job)
    driveboard.job(copy.deepcopy(job))
    assert loop.timer.total() == pytest.approx(estimate, rel=0.05)


def test_job_duration_matches_dispatch_on_a_full_image(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    job = _raster_time_job(_png(Image.new("L", (200, 100), 0)))
    estimate = _duration(job)
    driveboard.job(copy.deepcopy(job))
    assert loop.timer.total() == pytest.approx(estimate, rel=0.05)


def test_job_duration_times_an_image_with_no_pixel_data_as_full():
    # a def sent without data has only its extent to go on
    slim = {
        "head": {"noreturn": True},
        "passes": [{"items": [0], "feedrate": 3000, "seekrate": 6000, "pxsize": 0.4}],
        "items": [{"def": 0}],
        "defs": [{"kind": "image", "pos": [10.0, 10.0], "size": [80.0, 40.0]}],
    }
    full = _raster_time_job(_png(Image.new("L", (200, 100), 0)))
    assert _duration(slim) == pytest.approx(_duration(full), rel=0.05)


def test_raster_row_extents_keys_on_invert(monkeypatch):
    # inverting swaps which pixels are ink, so a cached scan must not be reused
    img = Image.new("L", (100, 10), 255)
    for x in range(20):
        img.putpixel((x, 5), 0)
    b64 = _png(img)
    monkeypatch.setitem(driveboard.conf, "raster_invert", False)
    plain = driveboard._raster_row_extents(b64, 100, 10, False)
    monkeypatch.setitem(driveboard.conf, "raster_invert", True)
    inverted = driveboard._raster_row_extents(b64, 100, 10, True)
    assert plain != inverted
    assert plain.count(None) > inverted.count(None)  # mostly white becomes mostly ink


# ---------------------------------------------------------------------------
# Stopping mid-dispatch
# ---------------------------------------------------------------------------


def test_stop_discards_what_a_running_dispatch_still_emits(loop):
    # Queueing a big job takes seconds, and the machine is already burning the
    # start of it. A stop empties the buffer, but the dispatch that is still
    # running would refill it, and the resume that follows would run it.
    loop._status["offset"] = [0.0, 0.0, 0.0]
    driveboard.stop()
    assert loop.tx_buffer == bytearray()
    driveboard.feedrate(2000)
    driveboard.intensity(50)
    driveboard.move(10.0, 10.0)
    driveboard.rastermove(20.0, 10.0)
    driveboard.rasterdata([0, 128, 255], 0, 3)
    driveboard.dwell()
    assert loop.tx_buffer == bytearray()  # nothing of it reached the wire
    assert loop.job_size == 0


def test_unstop_lets_the_machine_be_driven_again(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    driveboard.stop()
    driveboard.move(10.0, 10.0)
    assert loop.tx_buffer == bytearray()
    loop.request_resume = False
    driveboard.unstop()
    driveboard.move(10.0, 10.0)
    assert loop.tx_buffer  # driveable once the stop is cleared


def test_a_new_job_starts_from_a_clean_buffer_after_a_stop(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    driveboard.stop()
    loop._status["stops"].clear()  # the operator cleared the stop condition
    driveboard.unstop()
    loop.request_stop = False  # controller has consumed stop/resume and reported idle
    loop._status["ready"] = True
    driveboard.job(_duration_job())
    assert loop.tx_buffer  # the next job is not swallowed by the old stop


def test_two_jobs_cannot_interleave_into_one_stream(loop):
    # requests are served concurrently now, and two dispatches sharing the
    # buffer would produce a stream that is neither job
    loop._status["offset"] = [0.0, 0.0, 0.0]
    started = threading.Event()
    release = threading.Event()
    real_move = driveboard.move

    def slow_move(*a, **k):
        started.set()
        release.wait(5)
        return real_move(*a, **k)

    driveboard.move = slow_move
    try:
        first = threading.Thread(target=lambda: driveboard.job(_duration_job()))
        first.start()
        assert started.wait(5)
        with pytest.raises(ValueError, match="already being queued"):
            driveboard.job(_duration_job())
        release.set()
        first.join(10)
    finally:
        driveboard.move = real_move


def test_pause_works_once_the_send_buffer_has_drained(loop):
    # the controller still holds an rx buffer and up to BLOCK_BUFFER_SIZE
    # planned moves, so an empty send buffer is not a finished job
    loop.tx_buffer = bytearray()
    loop._status["ready"] = False  # controller still working through its own
    driveboard.pause()
    assert loop._paused is True
    assert loop.request_pause is True


def test_pause_still_works_with_a_job_left_to_send(loop):
    loop.tx_buffer = bytearray(b"abc")
    loop._status["ready"] = True
    driveboard.pause()
    assert loop._paused is True
    assert loop.request_pause is True


def test_pause_does_nothing_on_an_idle_machine(loop):
    loop.tx_buffer = bytearray()
    loop._status["ready"] = True
    driveboard.pause()
    assert loop._paused is False
    assert loop.request_pause is False


def test_pause_keeps_the_job_where_stop_discards_it(loop):
    # pausing must not lose what is queued, that is the difference from a stop
    loop._status["offset"] = [0.0, 0.0, 0.0]
    driveboard.job(_duration_job())
    queued = bytes(loop.tx_buffer)
    assert queued
    driveboard.pause()
    assert bytes(loop.tx_buffer) == queued
    assert loop.discard_writes is False  # more of the job may still be emitted
    driveboard.unpause()
    assert loop._paused is False
    assert bytes(loop.tx_buffer) == queued


def test_a_jog_cannot_splice_itself_into_a_job_being_queued(loop):
    # requests are served concurrently, so a jog can arrive while a job is
    # being written into the send buffer. Appending it would put that move in
    # the middle of the job and the machine would run it mid-cut.
    loop._status["offset"] = [0.0, 0.0, 0.0]
    started = threading.Event()
    release = threading.Event()
    real_move = driveboard.move
    seen = []

    def slow_move(*a, **k):
        started.set()
        release.wait(5)
        return real_move(*a, **k)

    driveboard.move = slow_move
    try:
        worker = threading.Thread(target=lambda: driveboard.job(_duration_job()))
        worker.start()
        assert started.wait(5)
        for call in (
            lambda: driveboard.move(5.0, 5.0),
            lambda: driveboard.feedrate(1000),
            lambda: driveboard.intensity(80),
            lambda: driveboard.pulse(),
            lambda: driveboard.air_on(),
        ):
            with pytest.raises(driveboard.MachineBusy):
                call()
            seen.append(True)
        release.set()
        worker.join(10)
    finally:
        driveboard.move = real_move
    assert len(seen) == 5


def test_the_dispatching_thread_itself_is_not_blocked(loop):
    # the guard tells another thread's writes apart from the job's own
    loop._status["offset"] = [0.0, 0.0, 0.0]
    driveboard.job(_duration_job())
    assert loop.tx_buffer  # the job got all the way out


def test_manual_moves_are_refused_while_the_job_is_still_running(loop):
    # queued behind a streaming job, a jog would run the moment the job ended
    loop._status["offset"] = [0.0, 0.0, 0.0]
    driveboard.job(_duration_job())
    queued = bytes(loop.tx_buffer)
    for call in (
        lambda: driveboard.move(5.0, 5.0),
        lambda: driveboard.supermove(5.0, 5.0),
        lambda: driveboard.feedrate(1000),
        lambda: driveboard.intensity(80),
        lambda: driveboard.pulse(),
        lambda: driveboard.air_on(),
    ):
        with pytest.raises(driveboard.MachineBusy):
            call()
    driveboard.homing()  # has a guard of its own, and declines quietly
    assert bytes(loop.tx_buffer) == queued  # none of it reached the wire


def test_manual_moves_work_again_once_the_job_has_gone_out(loop):
    loop._status["offset"] = [0.0, 0.0, 0.0]
    driveboard.job(_duration_job())
    with pytest.raises(driveboard.MachineBusy):
        driveboard.move(5.0, 5.0)
    loop.job_active = False  # the serial loop clears this as the last goes out
    before = len(loop.tx_buffer)
    driveboard.move(5.0, 5.0)
    assert len(loop.tx_buffer) > before


def test_a_stop_lets_the_machine_be_driven_again(loop):
    # a stop drops the job, so the machine is free even though one was running
    loop._status["offset"] = [0.0, 0.0, 0.0]
    driveboard.job(_duration_job())
    driveboard.stop()
    assert loop.job_active is False
    loop._status["stops"].clear()
    driveboard.unstop()
    driveboard.move(5.0, 5.0)
    assert loop.tx_buffer


def test_stop_during_raster_chunk_construction_cannot_refill_queue(loop):
    entered = threading.Event()
    release = threading.Event()

    class SlowPixels:
        def __iter__(self):
            yield 0
            entered.set()
            assert release.wait(5)
            yield 255

    worker = threading.Thread(target=lambda: driveboard.rasterdata(SlowPixels(), 0, 2))
    worker.start()
    assert entered.wait(5)
    driveboard.stop()
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert loop.tx_buffer == bytearray()
    assert loop.job_size == 0


def test_unstop_during_cancelled_dispatch_does_not_restore_its_tail(loop, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    real_move = driveboard.move

    def slow_move(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return real_move(*args, **kwargs)

    monkeypatch.setattr(driveboard, "move", slow_move)
    worker = threading.Thread(target=lambda: driveboard.job(_duration_job()))
    worker.start()
    assert started.wait(5)
    driveboard.stop()
    driveboard.unstop()
    release.set()
    worker.join(10)
    assert not worker.is_alive()
    assert loop.tx_buffer == bytearray()
    assert loop.discard_writes is False  # released only after producer exited


def test_second_job_rejected_until_firmware_reports_idle(loop):
    driveboard.job(_duration_job())
    with pytest.raises(driveboard.MachineBusy, match="not idle"):
        driveboard.job(_duration_job())


def test_job_rejected_after_manual_move_until_firmware_reports_idle(loop):
    driveboard.move(5.0, 5.0)
    assert loop._status["ready"] is False
    with pytest.raises(driveboard.MachineBusy, match="not idle"):
        driveboard.job(_duration_job())


def test_job_stays_active_after_host_buffer_drains_until_firmware_idle(loop):
    driveboard.job(_duration_job())
    loop.tx_pos = len(loop.tx_buffer)
    loop.request_status = 0
    loop._serial_write()
    assert loop.tx_buffer == bytearray()
    assert loop.job_active is True
    with pytest.raises(driveboard.MachineBusy, match="running"):
        driveboard.move(5.0, 5.0)

    feed(loop, driveboard.INFO_IDLE_YES.encode("latin-1"))
    assert loop.job_active is False
    driveboard.move(5.0, 5.0)  # manual control is available only now


def test_failed_validation_does_not_leave_machine_marked_active(loop):
    bad = _duration_job()
    bad["passes"][0]["feedrate"] = 0
    with pytest.raises(ValueError, match="feedrate"):
        driveboard.job(bad)
    assert loop.job_active is False
    driveboard.move(5.0, 5.0)


def test_manual_operation_keeps_pulse_out_of_positioning_move(loop):
    move_ready = threading.Event()
    allow_move = threading.Event()
    pulse_errors = []

    def positioning_move():
        with driveboard.machine_operation():
            driveboard.intensity(0)
            move_ready.set()
            assert allow_move.wait(5)
            driveboard.move(10.0, 10.0)

    mover = threading.Thread(target=positioning_move)
    mover.start()
    assert move_ready.wait(5)

    def try_pulse():
        try:
            driveboard.pulse()
        except Exception as error:
            pulse_errors.append(error)

    pulser = threading.Thread(target=try_pulse)
    pulser.start()
    pulser.join(5)
    assert len(pulse_errors) == 1
    assert isinstance(pulse_errors[0], driveboard.MachineBusy)
    allow_move.set()
    mover.join(5)
    assert not mover.is_alive() and not pulser.is_alive()
    assert ord(driveboard.CMD_LINE) in loop.tx_buffer
    assert ord(driveboard.CMD_DWELL) not in loop.tx_buffer
