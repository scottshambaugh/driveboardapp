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

import glob
import os

import driveboard
import jobimport
import pytest
from helpers import FakeSerialDevice


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


def test_library_jobs_exist(library_dir):
    found = {os.path.basename(p) for p in glob.glob(os.path.join(library_dir, "*.dba"))}
    missing = set(JOBS) - found
    assert not missing, f"missing expected library fixtures: {missing}"
