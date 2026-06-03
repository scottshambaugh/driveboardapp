"""Firmware compilation test.

Compiles + links every ``config.*.h`` hardware variant with avr-gcc in an
isolated temp dir, so a build break in any variant fails CI. It never touches
the tracked ``firmware/*.hex`` artifacts (unlike backend/build.py, which moves
them into place). Self-skips when avr-gcc is unavailable, so the normal
``pytest`` run on a dev box without the AVR toolchain stays green; the dedicated
firmware CI job installs gcc-avr and actually exercises this.
"""

import glob
import os
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC_DIR = os.path.join(REPO_ROOT, "firmware", "src")

# Mirrors backend/build.py: only these translation units are part of the build.
OBJECTS = ["main", "serial", "protocol", "planner", "sense_control", "stepper"]

AVR_GCC = shutil.which("avr-gcc")

pytestmark = pytest.mark.skipif(AVR_GCC is None, reason="avr-gcc not installed")


def _config_variants():
    return sorted(os.path.basename(p) for p in glob.glob(os.path.join(SRC_DIR, "config.*.h")))


def test_config_variants_discovered():
    variants = _config_variants()
    assert variants, "expected config.*.h firmware variants in firmware/src"


@pytest.mark.parametrize("config_file", _config_variants())
def test_firmware_variant_compiles(config_file, tmp_path):
    build_dir = tmp_path / "src"
    shutil.copytree(SRC_DIR, build_dir)

    # Activate this hardware variant as config.h.
    shutil.copy(build_dir / config_file, build_dir / "config.h")

    flags = [
        "-Wall",
        "-Os",
        "-DF_CPU=16000000",
        "-mmcu=atmega328p",
        "-I.",
        "-ffunction-sections",
        "--std=c99",
    ]

    # Compile each object.
    for obj in OBJECTS:
        result = subprocess.run(
            [AVR_GCC, *flags, "-c", f"{obj}.c", "-o", f"{obj}.o"],
            cwd=build_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{config_file}: compiling {obj}.c failed:\n{result.stderr}"

    # Link.
    result = subprocess.run(
        [AVR_GCC, *flags, "-o", "main.elf", *[f"{o}.o" for o in OBJECTS], "-lm"],
        cwd=build_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{config_file}: link failed:\n{result.stderr}"
    assert (build_dir / "main.elf").exists()
