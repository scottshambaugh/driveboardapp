"""Firmware compilation tests.

Compiles + links every ``config.*.h`` hardware variant with avr-gcc in an
isolated temp dir, so a build break in any variant fails CI, and checks that the
committed ``firmware/firmware.*.hex`` artifacts still match a fresh build of the
source. Self-skips when avr-gcc is unavailable, so the normal ``pytest`` run on a
dev box without the AVR toolchain stays green; the dedicated firmware CI job
installs gcc-avr and actually exercises these.
"""

import glob
import os
import re
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC_DIR = os.path.join(REPO_ROOT, "firmware", "src")
FIRMWARE_DIR = os.path.join(REPO_ROOT, "firmware")

# Mirrors backend/build.py: only these translation units are part of the build.
OBJECTS = ["main", "serial", "protocol", "planner", "sense_control", "stepper"]
FLAGS = [
    "-Wall",
    "-Os",
    "-DF_CPU=16000000",
    "-mmcu=atmega328p",
    "-I.",
    "-ffunction-sections",
    "--std=c99",
]

AVR_GCC = shutil.which("avr-gcc")
AVR_OBJCOPY = shutil.which("avr-objcopy")

# The committed firmware/*.hex are built with this avr-gcc. Other versions emit
# different (still-valid) machine code, so the byte-for-byte comparison only
# holds on this toolchain. A major-version mismatch fails fast (below) so we
# know when the CI runner / committed hexes need realigning.
# Ubuntu 24.04 "noble" ships 7.3.0 (20.04-22.04 shipped 5.4.0).
HEX_BUILD_AVR_GCC = "7.3.0"


def _avr_gcc_version():
    if AVR_GCC is None:
        return None
    # Parse `--version` rather than `-dumpversion`: gcc 5 dumps the full "5.4.0"
    # but gcc 7+ dumps only "7", whereas --version always carries the full X.Y.Z.
    out = subprocess.run([AVR_GCC, "--version"], capture_output=True, text=True).stdout
    m = re.search(r"\b(\d+\.\d+\.\d+)\b", out.splitlines()[0] if out else "")
    return m.group(1) if m else None


AVR_GCC_VERSION = _avr_gcc_version()

pytestmark = pytest.mark.skipif(AVR_GCC is None, reason="avr-gcc not installed")


def _config_variants():
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(SRC_DIR, "config.*.h")))


def _designator(config_file):
    """config.driveboardusb.h -> driveboardusb (matches backend/build.py)."""
    return ".".join(config_file.split(".")[1:-1])


def _build_main_elf(build_dir, config_file):
    """Compile + link a variant in build_dir, returning the main.elf path."""
    shutil.copytree(SRC_DIR, build_dir)
    # Activate this hardware variant as config.h.
    shutil.copy(build_dir / config_file, build_dir / "config.h")

    for obj in OBJECTS:
        result = subprocess.run(
            [AVR_GCC, *FLAGS, "-c", f"{obj}.c", "-o", f"{obj}.o"],
            cwd=build_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{config_file}: compiling {obj}.c failed:\n{result.stderr}"

    result = subprocess.run(
        [AVR_GCC, *FLAGS, "-o", "main.elf", *[f"{o}.o" for o in OBJECTS], "-lm"],
        cwd=build_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{config_file}: link failed:\n{result.stderr}"
    elf = build_dir / "main.elf"
    assert elf.exists()
    return elf


def test_config_variants_discovered():
    variants = _config_variants()
    assert variants, "expected config.*.h firmware variants in firmware/src"


def test_beam_interlock_not_gated_by_disable_limits():
    """The stepper ISR beam cutoff has to sit outside the disable_limits guard.

    disable_limits only suppresses limit switch stops, so homing can drive off
    a triggered switch. The beam has no such exception, and this ISR copy is
    the backstop for when the protocol loop is not running.

    Structural because homing plans its moves at intensity 0, so simavr cannot
    tell the two arrangements apart. The arrangement is the invariant.
    """
    with open(os.path.join(SRC_DIR, "stepper.c")) as fp:
        isr = fp.read().split("ISR(TIMER1_COMPA_vect)", 1)[1]
    beam = isr.index("SENSE_DOOR_OPEN")
    guard = isr.index("if (!disable_limits)")
    limit = isr.index("SENSE_X1_LIMIT")
    assert beam < guard, "door/chiller beam cutoff is gated by disable_limits"
    assert guard < limit, "limit switch stops must stay under disable_limits"


def test_avr_gcc_toolchain_alignment():
    """Fail fast when avr-gcc drifts a major version from the committed hexes.

    When this trips (e.g. the CI runner image bumps its gcc-avr), realign:
    rebuild the hexes with the new toolchain (``python backend/build.py``),
    commit them, and update ``HEX_BUILD_AVR_GCC``.
    """
    have = (AVR_GCC_VERSION or "unknown").split(".")[0]
    want = HEX_BUILD_AVR_GCC.split(".")[0]
    assert have == want, (
        f"avr-gcc {AVR_GCC_VERSION} found, but the committed firmware hexes were built "
        f"with {HEX_BUILD_AVR_GCC}. Rebuild the hexes (backend/build.py), commit them, "
        f"and update HEX_BUILD_AVR_GCC."
    )


@pytest.mark.parametrize("config_file", _config_variants())
def test_firmware_variant_compiles(config_file, tmp_path):
    _build_main_elf(tmp_path / "src", config_file)


@pytest.mark.skipif(AVR_OBJCOPY is None, reason="avr-objcopy not installed")
@pytest.mark.parametrize("config_file", _config_variants())
def test_firmware_variant_matches_committed_hex(config_file, tmp_path):
    """A fresh build of the source must reproduce the committed .hex.

    Catches committed firmware binaries drifting out of sync with the source
    (e.g. source edited but the .hex not rebuilt + committed). Also fails on a
    toolchain mismatch, since another avr-gcc emits different machine code -
    see test_avr_gcc_toolchain_alignment for the fail-fast diagnostic.
    """
    committed = os.path.join(FIRMWARE_DIR, f"firmware.{_designator(config_file)}.hex")
    assert os.path.exists(committed), f"no committed hex for {config_file}: {committed}"

    elf = _build_main_elf(tmp_path / "src", config_file)
    fresh = tmp_path / "fresh.hex"
    result = subprocess.run(
        [AVR_OBJCOPY, "-j", ".text", "-j", ".data", "-O", "ihex", str(elf), str(fresh)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{config_file}: objcopy failed:\n{result.stderr}"

    with open(fresh, "rb") as f:
        fresh_bytes = f.read()
    with open(committed, "rb") as f:
        committed_bytes = f.read()
    assert fresh_bytes == committed_bytes, (
        f"firmware.{_designator(config_file)}.hex is out of sync with the source "
        f"(built here with avr-gcc {AVR_GCC_VERSION}; committed hexes expect "
        f"{HEX_BUILD_AVR_GCC}). Rebuild the firmware (backend/build.py) and commit "
        f"the .hex files; if the toolchain changed, also update HEX_BUILD_AVR_GCC."
    )
