"""Helpers to compile the firmware + simavr harness and run scenarios.

Used by test_simavr.py (boot/protocol) and test_firmware_safety.py (interlocks,
limits, stop/watchdog). Everything self-skips unless avr-gcc and libsimavr-dev
are present, and the firmware ELF + harness binary are built once per session.

Set DRIVEBOARD_REQUIRE_FIRMWARE where the toolchain is installed on purpose,
and a missing one fails instead of skipping.
"""

import functools
import os
import shutil
import subprocess
import tempfile

import pytest

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
SRC_DIR = os.path.join(REPO_ROOT, "firmware", "src")
RUNNER_SRC = os.path.join(HERE, "simavr_runner.c")

OBJECTS = ["main", "serial", "protocol", "planner", "sense_control", "stepper"]
AVR_FLAGS = [
    "-Wall",
    "-Os",
    "-DF_CPU=16000000",
    "-mmcu=atmega328p",
    "-I.",
    "-ffunction-sections",
    "--std=c99",
]

AVR_GCC = shutil.which("avr-gcc")
CC = shutil.which("cc") or shutil.which("gcc")

# CI's firmware job installs the toolchain on purpose, so a skip there means
# the install broke rather than that the machine cannot build firmware. With
# this set the skips become failures, so that job cannot go green having run
# none of the firmware safety suite.
REQUIRE = bool(os.environ.get("DRIVEBOARD_REQUIRE_FIRMWARE"))


def unavailable(reason):
    """Skip for want of the toolchain, or fail where it was meant to be there."""
    if REQUIRE:
        pytest.fail(
            f"{reason} (DRIVEBOARD_REQUIRE_FIRMWARE is set, so this is a broken "
            "environment rather than a machine that cannot run these tests)",
            pytrace=False,
        )
    pytest.skip(reason)


# Firmware protocol/sense constants (mirrors firmware/src/protocol.h + config).
INFO_HELLO = 0x7E
STATUS_END = 0x06
INFO_IDLE_YES = ord("A")
INFO_DOOR_OPEN = ord("B")
INFO_CHILLER_OFF = ord("C")
INFO_PAUSED = ord("D")
CMD_STOP = 1
CMD_RESUME = 2
CMD_SUPERSTATUS = 4
CMD_PAUSE = 8
CMD_UNPAUSE = 7
STOPERROR_OK = ord(" ")
STOPERROR_SERIAL_STOP_REQUEST = ord("!")
STOPERROR_TRANSMISSION_ERROR = ord("=")
STOPERROR_INVALID_MARKER = ord("#")
STOPERROR_INVALID_DATA = ord(":")
STOPERROR_INVALID_COMMAND = ord("<")
STOPERROR_INVALID_PARAMETER = ord(">")
STOPERROR_SERIAL_WATCHDOG = ord(";")
# Buffered commands ([A-Z]) and parameters ([a-z]) for driving motion.
CMD_DWELL = ord("C")
CMD_LINE = ord("B")
CMD_RASTER = ord("D")
CMD_RASTER_DATA_START = 16
CMD_RASTER_DATA_END = 17
PARAM_INTENSITY = "s"
PARAM_DURATION = "d"
PARAM_FEEDRATE = "f"
PARAM_TARGET_X = "x"
PARAM_PIXEL_WIDTH = "p"
# config.driveboardusb.h: X step pulse is PORTB bit 0.
X_STEP_PORTB_BIT = 0
STOPERROR_LIMIT_HIT = {
    "x1": ord("$"),
    "x2": ord("%"),
    "y1": ord("&"),
    "y2": ord("*"),
    "z1": ord("+"),
    "z2": ord("-"),
}
# config.driveboardusb.h pin map (SENSE_INVERT -> active HIGH).
# Default variant is 2-axis (ENABLE_3AXES off), so only x/y limits are live.
LIMIT_PORTC_BIT = {"x1": 0, "x2": 1, "y1": 2, "y2": 3, "z1": 4, "z2": 5}
XY_LIMITS = ["x1", "x2", "y1", "y2"]
Z_LIMITS = ["z1", "z2"]
DOOR1_PORTD_BIT = 2
DOOR2_PORTD_BIT = 7
CHILLER_PORTD_BIT = 3
# assist relay outputs, config.driveboardusb.h AIR/AUX_ASSIST_BIT
AIR_ASSIST_PORTD_BIT = 4
AUX_ASSIST_PORTD_BIT = 6
CMD_AIR_ENABLE = ord("L")
CMD_AIR_DISABLE = ord("M")
CMD_AUX_ENABLE = ord("N")
CMD_AUX_DISABLE = ord("O")

# config.driveboard1403mill.h is the only 3-axis build; it has SENSE_INVERT OFF,
# so its limit switches are ACTIVE LOW (untriggered = pin high).
MILL_VARIANT = "config.driveboard1403mill.h"


def _simavr_include_dir():
    for d in ("/usr/include/simavr", "/usr/local/include/simavr", "/usr/include"):
        if os.path.exists(os.path.join(d, "sim_avr.h")):
            return d
    return None


SIMAVR_INC = _simavr_include_dir()


def available():
    return AVR_GCC is not None and CC is not None and SIMAVR_INC is not None


# where the toolchain is required, the tests run and report what is missing
# through unavailable() below, rather than quietly dropping out here
skip_unless_available = pytest.mark.skipif(
    not available() and not REQUIRE, reason="requires avr-gcc and libsimavr-dev"
)


@functools.cache
def _build_dir():
    return tempfile.mkdtemp(prefix="dbfwsim_")


@functools.cache
def firmware_elf(variant="config.driveboardusb.h"):
    """Compile + link a firmware variant to an ELF (cached per session)."""
    if AVR_GCC is None:
        unavailable("avr-gcc is not installed")
    build = os.path.join(_build_dir(), "fw_" + variant)
    shutil.copytree(SRC_DIR, build)
    shutil.copy(os.path.join(build, variant), os.path.join(build, "config.h"))
    for obj in OBJECTS:
        r = subprocess.run(
            [AVR_GCC, *AVR_FLAGS, "-c", f"{obj}.c", "-o", f"{obj}.o"],
            cwd=build,
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"compile {obj}: {r.stderr}")
    elf = os.path.join(build, "firmware.elf")
    r = subprocess.run(
        [AVR_GCC, *AVR_FLAGS, "-o", elf, *[f"{o}.o" for o in OBJECTS], "-lm"],
        cwd=build,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"link: {r.stderr}")
    return elf


@functools.cache
def harness():
    """Compile the simavr harness (cached per session), or skip if it won't link."""
    if CC is None or SIMAVR_INC is None:
        unavailable("libsimavr-dev or a host compiler is not installed")
    out = os.path.join(_build_dir(), "simavr_runner")
    last = ""
    for extra in (["-lelf"], []):
        r = subprocess.run(
            [CC, RUNNER_SRC, "-I", SIMAVR_INC, "-o", out, "-lsimavr", *extra, "-lpthread"],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return out
        last = r.stderr
    unavailable(f"could not link against libsimavr: {last.strip()[:300]}")


def double(cmd_bytes):
    """Apply the firmware's duplicate-transmission protocol to a command stream."""
    out = []
    for b in cmd_bytes:
        out.append(b)
        out.append(b)
    return out


def frames(out_bytes):
    """Split a captured UART stream into status frames (each ends in STATUS_END)."""
    result = []
    cur = []
    for b in out_bytes:
        cur.append(b)
        if b == STATUS_END:
            result.append(cur)
            cur = []
    if cur:
        result.append(cur)
    return result


def run(
    send=None,
    portc=None,
    portd=None,
    idle_cycles=0,
    run_cycles=None,
    watch_symbol=None,
    watch_size=None,
    count_portb=None,
    portc_delay=0,
    watch_pin=None,
    variant="config.driveboardusb.h",
):
    """Run a scenario.

    Returns (output_bytes, hello_seen). If watch_symbol and/or count_portb is
    given, returns (output_bytes, hello_seen, info) where info may hold the
    watched symbol's "max"/"final" and the step-pin "steps" edge count.
    """
    elf = firmware_elf(variant)
    hbin = harness()
    args = [hbin, elf]
    if portc:
        args.append("--portc=" + ",".join(str(b) for b in portc))
    if portd:
        args.append("--portd=" + ",".join(str(b) for b in portd))
    if send:
        args.append("--send=" + ",".join(str(b) for b in send))
    if idle_cycles:
        args.append(f"--idle-cycles={idle_cycles}")
    if run_cycles:
        args.append(f"--run-cycles={run_cycles}")
    if watch_symbol:
        args.append(f"--watch-symbol={watch_symbol}")
    if watch_size:
        args.append(f"--watch-size={watch_size}")
    if count_portb is not None:
        args.append(f"--count-portb={count_portb}")
    if portc_delay:
        args.append(f"--portc-delay={portc_delay}")
    if watch_pin:
        args.append(f"--watch-pin={watch_pin[0]},{watch_pin[1]}")
    r = subprocess.run(args, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, f"harness failed: {r.stdout}\n{r.stderr}"
    lines = r.stdout.splitlines()
    out_line = next((ln for ln in lines if ln.startswith("OUT:")), "OUT:")
    hello_line = next((ln for ln in lines if ln.startswith("HELLO=")), "HELLO=0")
    out_bytes = [int(x) for x in out_line[4:].split()]
    hello = hello_line.strip() == "HELLO=1"
    if watch_symbol or count_portb is not None or watch_pin:
        info = {}
        for prefix in ("SYM ", "PIN "):
            line = next((ln for ln in lines if ln.startswith(prefix)), "")
            for tok in line.split():
                if "=" in tok:
                    k, v = tok.split("=")
                    info[k] = int(v)
        steps_line = next((ln for ln in lines if ln.startswith("STEPS=")), "")
        if steps_line:
            info["steps"] = int(steps_line.split("=")[1])
        return out_bytes, hello, info
    return out_bytes, hello


def encode_value_param(marker, value):
    """Encode a 4-data-byte + marker parameter the way the firmware decodes it
    (mirrors the host send_param), as a list of logical (undoubled) bytes."""
    num = int(round((value + 134217.728) * 1000))
    return [
        (num & 127) + 128,
        ((num & (127 << 7)) >> 7) + 128,
        ((num & (127 << 14)) >> 14) + 128,
        ((num & (127 << 21)) >> 21) + 128,
        ord(marker),
    ]


def dwell_program(intensity, duration):
    """Wire bytes for a dwell at the given laser intensity (0-255) and duration.

    A dwell makes the stepper ISR fire the laser (set the PWM duty) without
    moving, so it is the simplest way to drive the beam on in-sim.
    """
    logical = (
        encode_value_param(PARAM_INTENSITY, intensity)
        + encode_value_param(PARAM_DURATION, duration)
        + [CMD_DWELL]
    )
    return double(logical)


def line_program(x, feedrate=1000):
    """Wire bytes for an absolute X line move (queues a stepper block)."""
    logical = (
        encode_value_param(PARAM_FEEDRATE, feedrate)
        + encode_value_param(PARAM_TARGET_X, x)
        + [CMD_LINE]
    )
    return double(logical)


def raster_segment_program(
    start, end, pixels, leadin=5.0, feedrate=4000, seekrate=6000, pixel_width=0.1, intensity=200
):
    """Wire bytes for a raster segment the way the host emits one: seek to the
    lead-in, ramp to the start edge at the feedrate, raster move with data,
    lead out. The colinear lead-in gives the raster block the entry speed the
    planner allows a short block, as in a real job."""
    sign = 1.0 if end >= start else -1.0
    logical = (
        encode_value_param(PARAM_INTENSITY, 0)
        + encode_value_param(PARAM_FEEDRATE, seekrate)
        + encode_value_param(PARAM_TARGET_X, start - sign * leadin)
        + [CMD_LINE]
        + encode_value_param(PARAM_FEEDRATE, feedrate)
        + encode_value_param(PARAM_TARGET_X, start)
        + [CMD_LINE]
        + encode_value_param(PARAM_INTENSITY, intensity)
        + encode_value_param(PARAM_PIXEL_WIDTH, pixel_width)
        + encode_value_param(PARAM_TARGET_X, end)
        + [CMD_RASTER, CMD_RASTER_DATA_START]
        + list(pixels)
        + [CMD_RASTER_DATA_END]
        + encode_value_param(PARAM_INTENSITY, 0)
        + encode_value_param(PARAM_TARGET_X, end + sign * leadin)
        + [CMD_LINE]
    )
    return double(logical)


def raster_program(x, pixels, pixel_width=0.1, intensity=100, feedrate=1000):
    """Wire bytes for a raster move to absolute X, streaming the given pixels.

    Pixel values are wire bytes in [128, 255], 128 being no power. Passing the
    X the head is already at makes the move zero length.
    """
    logical = (
        encode_value_param(PARAM_FEEDRATE, feedrate)
        + encode_value_param(PARAM_INTENSITY, intensity)
        + encode_value_param(PARAM_PIXEL_WIDTH, pixel_width)
        + encode_value_param(PARAM_TARGET_X, x)
        + [CMD_RASTER, CMD_RASTER_DATA_START]
        + list(pixels)
        + [CMD_RASTER_DATA_END]
    )
    return double(logical)
