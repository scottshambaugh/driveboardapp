
DriveboardApp Testing
=====================

DriveboardApp has an automated test suite that runs in GitHub Actions (see `.github/workflows/ci.yml`) and locally with `pytest`. It covers the Python *backend*, compilation and behavior of the C *firmware*, and a *frontend* smoke check. None of it needs a Driveboard attached: the firmware is exercised in a simulator and the serial hardware is faked, so the whole suite runs on a plain development machine.

Running the tests
-----------------

```bash
uv sync --dev
uv run pytest backend/tests
uv run ruff check . && uv run ruff format --check .
```

The firmware tests compile the real firmware and run it in [simavr](https://github.com/buserror/simavr). They self-skip unless the AVR toolchain and simavr are installed, so the rest of the suite stays green without them. On Debian/Ubuntu:

```bash
sudo apt-get install -y gcc-avr avr-libc simavr libsimavr-dev libelf-dev
```

What is covered
---------------

- **Job import** - `test_jobimport.py` parses real svg, dxf, dba, and gcode fixtures from `library/` and `backend/testjobs/`.
- **Geometry** - the path optimizer, kd-tree, and matrix helpers (`test_pathoptimizer.py`, `test_kdtree.py`, `test_utilities.py`).
- **Configuration** - load/write round-trips against an isolated temporary config, so the developer's real config is never touched (`test_config.py`).
- **Web API** - auth, the work-area gates on motion endpoints, the emergency endpoints, and the status-poll reconnect, driven through the real WSGI app (`test_web.py`).
- **Frontend** - every static asset the app serves returns 200, and CI runs `node --check` over all frontend JavaScript (`test_frontend.py`).
- **Firmware compilation** - every `config.*.h` variant is compiled and linked with `avr-gcc` (`test_firmware.py`).
- **Firmware behavior** - the real compiled firmware is run in simavr (`test_simavr.py`, `test_firmware_safety.py`).
- **End to end** - a real `.dba` job is taken all the way through convert, work-area validation, and serialization, and the exact bytes that would reach the controller are checked (`test_e2e.py`).

The safety-critical paths in the [backend](backend.md) and the [firmware](firmware.md) get dedicated, thorough coverage, described below.

Host-side safety (`test_driveboard_safety.py`)
----------------------------------------------

These tests drive the real `SerialLoopClass` (constructed but never thread-started) against a fake serial device, so the actual command encoding and status parsing are exercised:

- laser intensity clamping (0-255) and 28-bit parameter saturation, so an out-of-range value can never wrap to a wildly wrong one
- emergency stop / unstop and pause / unpause, including that a stop overrides a pause and that the stop byte is sent ahead of any queued data
- the homing guard that refuses to home while a job is running
- work-area bounds (`target_in_workarea`) with offsets and machine coordinates
- job validation that rejects out-of-bed geometry, for both laser and mill jobs, including through the `job()` and `jobfile()` entry points
- parsing of every stop condition reported by the controller: all six limit switches, stop request, rx-buffer overflow, invalid marker/data/command/parameter, transmission error, and watchdog; the door and chiller interlocks; the paused flag; and the status-frame flip
- decoding of the reported position, offset, intensity, and feedrate that the bounds checks rely on
- serial-disconnect recovery (`reconnect`) and the `_serial_write` send, stall, and resync paths

Firmware safety (`test_firmware_safety.py`)
-------------------------------------------

The real compiled firmware runs on a simulated atmega328p. A small C harness (`simavr_runner.c`, driven from Python via `firmware_sim.py`) drives the input pins and the serial line, and reads back what the firmware transmits - so these are tests of the actual compiled C, not of a model of it. Pin polarity follows `config.driveboardusb.h` (limit/door/chiller active high under `SENSE_INVERT`); the 3-axis Z limits are checked against the mill build, which is active low.

- **Interlocks** - the door (both door pins) and chiller faults are sensed and reported, and with either open the laser PWM duty is forced to zero, while a healthy machine fires normally.
- **Limit switches** - each of the six endstops reports its own stop code, several limits report together without faulting, and a limit tripped partway through a move halts the stepper rather than only being reported.
- **Stop, pause, resume** - a serial stop halts and is reported, resume clears it, pause and unpause set and clear the paused flag, and a stop overrides a pause and bypasses queued data.
- **Malformed input** - invalid markers, commands, parameters, and data, and a duplicate-byte transmission mismatch, are all rejected with the matching stop code.
- **Serial watchdog** - prolonged host silence forces a safe stop, while a brief gap does not.
- **Baseline** - a healthy machine raises none of these conditions.

Note that simavr's modeled watchdog fires sooner than the firmware's configured 1-second timeout, so the watchdog tests bound the behavior (stops on prolonged silence, not on a brief gap) rather than the exact period. Raster-mode beam safety and the homing delimit cycle need more elaborate in-sim motion and are the only firmware safety paths not exercised here.
