"""Helpers to compile the firmware + simavr harness and run scenarios.

Used by test_simavr.py (boot/protocol) and test_firmware_safety.py (interlocks,
limits, stop/watchdog). Everything self-skips unless avr-gcc and libsimavr-dev
are present, and the firmware ELF + harness binary are built once per session.
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

# Firmware protocol/sense constants (mirrors firmware/src/protocol.h + config).
INFO_HELLO = 0x7E
STATUS_END = 0x06
INFO_DOOR_OPEN = ord("B")
INFO_CHILLER_OFF = ord("C")
INFO_PAUSED = ord("D")
CMD_STOP = 1
CMD_RESUME = 2
CMD_SUPERSTATUS = 4
CMD_PAUSE = 8
CMD_UNPAUSE = 7
STOPERROR_SERIAL_STOP_REQUEST = ord("!")
STOPERROR_TRANSMISSION_ERROR = ord("=")
STOPERROR_INVALID_MARKER = ord("#")
STOPERROR_INVALID_DATA = ord(":")
STOPERROR_INVALID_COMMAND = ord("<")
STOPERROR_INVALID_PARAMETER = ord(">")
STOPERROR_SERIAL_WATCHDOG = ord(";")
# Buffered commands ([A-Z]) and parameters ([a-z]) for driving a dwell.
CMD_DWELL = ord("C")
PARAM_INTENSITY = "s"
PARAM_DURATION = "d"
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


skip_unless_available = pytest.mark.skipif(
    not available(), reason="requires avr-gcc and libsimavr-dev"
)


@functools.cache
def _build_dir():
    return tempfile.mkdtemp(prefix="dbfwsim_")


@functools.cache
def firmware_elf(variant="config.driveboardusb.h"):
    """Compile + link a firmware variant to an ELF (cached per session)."""
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
    pytest.skip(f"could not link against libsimavr: {last.strip()[:300]}")


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
    variant="config.driveboardusb.h",
):
    """Run a scenario.

    Returns (output_bytes, hello_seen). If watch_symbol is given, returns
    (output_bytes, hello_seen, {"max": m, "final": f}) for that SRAM symbol.
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
    r = subprocess.run(args, capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, f"harness failed: {r.stdout}\n{r.stderr}"
    lines = r.stdout.splitlines()
    out_line = next((ln for ln in lines if ln.startswith("OUT:")), "OUT:")
    hello_line = next((ln for ln in lines if ln.startswith("HELLO=")), "HELLO=0")
    out_bytes = [int(x) for x in out_line[4:].split()]
    hello = hello_line.strip() == "HELLO=1"
    if watch_symbol:
        sym_line = next((ln for ln in lines if ln.startswith("SYM ")), "")
        info = {}
        for tok in sym_line.split():
            if "=" in tok:
                k, v = tok.split("=")
                info[k] = int(v)
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
