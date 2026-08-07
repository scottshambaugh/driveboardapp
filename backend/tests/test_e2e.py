"""End-to-end host pipeline test (no hardware).

Drives a real job all the way through the stack:

    .dba file  ->  jobimport.convert (parse + path-optimize)
               ->  driveboard.job   (validate work-area + emit command program)
               ->  SerialLoopClass._serial_write  (serialize onto the wire)

and verifies the bytes that would reach the controller are well-formed: the
duplicate-byte protocol holds and the serialized stream round-trips exactly to
the queued command program. This is the closest we can get to a full run
without a Driveboard attached; the firmware end of the same protocol is
exercised separately in test_simavr.py.
"""

import base64
import glob
import io
import os

import driveboard
import jobimport
import pytest
from helpers import FakeSerialDevice
from PIL import Image


def _drain(loop):
    """Serialize the whole tx_buffer through the real _serial_write path.

    Simulates the firmware consuming each chunk (firmbuf acks) so the send
    never stalls, and returns the raw bytes that went out on the wire.
    """
    loop.device = FakeSerialDevice()
    loop.request_status = 0
    loop._paused = False
    guard = 0
    while loop.tx_buffer:
        loop.firmbuf_used = 0  # pretend the controller drained its buffer
        loop._serial_write()
        guard += 1
        assert guard < 100000, "drain did not terminate"
    return bytes(loop.device.written)


# Small library jobs that fit the default 1220x610 bed.
JOBS = ["four-quadrant-test.dba", "lines.dba", "tangram.dba"]


@pytest.mark.parametrize("jobname", JOBS)
def test_dba_job_runs_through_to_wire(loop, library_dir, jobname):
    raw = open(os.path.join(library_dir, jobname)).read()

    # 1. Parse + optimize.
    job = jobimport.convert(raw)
    assert job["defs"], jobname

    # 2. Validate + emit the command program (raises if out of work area).
    driveboard.job(job)
    program = bytes(loop.tx_buffer)
    assert program, "job produced no controller commands"
    # A real laser job must contain motion (CMD_LINE).
    assert ord(driveboard.CMD_LINE) in program, "no motion commands emitted"

    # 3. Serialize onto the wire and check the protocol invariants.
    wire = _drain(loop)
    assert wire, "nothing serialized"
    assert len(wire) == 2 * len(program), "every byte must be duplicated on the wire"
    # de-doubling must reproduce the exact command program
    assert wire[0::2] == program
    assert wire[1::2] == program


def test_full_stack_job_has_bounded_params(loop, library_dir):
    """Every parameter emitted for a real job decodes within the protocol range."""
    raw = open(os.path.join(library_dir, "four-quadrant-test.dba")).read()
    driveboard.job(jobimport.convert(raw))
    buf = loop.tx_buffer

    i = 0
    param_markers = set("xyzfsdphij")  # PARAM_* lowercase markers
    saw_param = False
    while i < len(buf):
        b = buf[i]
        if 65 <= b <= 90:  # command byte (A-Z)
            i += 1
        elif b >= 128:  # start of a 4-byte data + marker parameter
            assert i + 4 < len(buf), "truncated parameter"
            data = buf[i : i + 4]
            marker = chr(buf[i + 4])
            assert marker in param_markers, f"unexpected marker {marker!r}"
            num = (
                (data[0] - 128)
                + ((data[1] - 128) << 7)
                + ((data[2] - 128) << 14)
                + ((data[3] - 128) << 21)
            )
            assert 0 <= num <= (1 << 28) - 1, "parameter outside 28-bit protocol range"
            saw_param = True
            i += 5
        else:
            i += 1
    assert saw_param, "expected at least one parameter in the job program"


def _decode_program(buf):
    """Decode a queued command program into (rastermoves, rasterdata lengths).

    Parameters latch until a command byte consumes them, so each CMD_RASTER
    reads its target from the last x/y params. Raster pixel streams are framed
    by CMD_RASTER_DATA_START/END with every payload byte >= 128."""
    moves = []
    chunks = []
    params = {}
    i = 0
    while i < len(buf):
        b = buf[i]
        if b == ord(driveboard.CMD_RASTER_DATA_START):
            j = i + 1
            while buf[j] != ord(driveboard.CMD_RASTER_DATA_END):
                j += 1
            chunks.append(j - i - 1)
            i = j + 1
        elif b >= 128:
            num = (
                (buf[i] - 128)
                + ((buf[i + 1] - 128) << 7)
                + ((buf[i + 2] - 128) << 14)
                + ((buf[i + 3] - 128) << 21)
            )
            params[chr(buf[i + 4])] = num / 1000.0 - 134217.728
            i += 5
        else:
            if chr(b) == driveboard.CMD_RASTER:
                moves.append((params.get("x"), params.get("y")))
            i += 1
    return moves, chunks


def test_clipped_raster_engraves_only_the_cropped_region(loop):
    """A clip-path on an SVG image carries all the way to the wire: every
    raster move targets the clipped box and each line streams the cropped
    pixel count, not the full image's."""
    img = Image.new("RGB", (2, 2), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    svg = (
        '<?xml version="1.0"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="100mm" height="100mm" viewBox="0 0 100 100">'
        '<defs><clipPath id="c"><rect x="10" y="20" width="20" height="10"/></clipPath></defs>'
        f'<image x="10" y="20" width="40" height="20" clip-path="url(#c)" xlink:href="{uri}"/>'
        "</svg>"
    )

    job = jobimport.convert(svg, optimize=False)
    assert job["defs"][0]["pos"] == pytest.approx([10.0, 20.0])
    assert job["defs"][0]["size"] == pytest.approx([20.0, 10.0])

    job["passes"] = [{"items": [0], "feedrate": 4000, "intensity": 50, "pxsize": 0.4}]
    driveboard.job(job)
    moves, chunks = _decode_program(loop.tx_buffer)

    # 10mm of clipped height at 0.4mm lines, all solid color, one run per line
    assert len(moves) == 25
    assert len(chunks) == 25
    # every raster move ends on a pixel edge inside the 20mm clipped width,
    # on a scanline inside the clipped height (the full image spans to x=50,
    # y=40, so an unclipped run would burst these bounds)
    for x, y in moves:
        assert 10.0 - 1e-6 <= x <= 30.0 + 1e-6
        assert 20.0 <= y <= 30.0
    # 20mm wide at 0.2mm horizontal pixels (pxsize/2) is 100 pixels per line
    assert chunks == [100] * 25

    # the serialized wire stream still honors the duplicate-byte protocol
    program = bytes(loop.tx_buffer)
    wire = _drain(loop)
    assert wire[0::2] == program
    assert wire[1::2] == program


def test_library_jobs_exist(library_dir):
    found = {os.path.basename(p) for p in glob.glob(os.path.join(library_dir, "*.dba"))}
    missing = set(JOBS) - found
    assert not missing, f"missing expected library fixtures: {missing}"
