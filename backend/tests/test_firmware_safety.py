"""Firmware (C-level) safety behavior, executed in simavr.

These run the *real compiled firmware* on a simulated atmega328p, drive its
input pins and serial line, and assert it reacts safely:

  - door / chiller interlocks are sensed and reported,
  - every limit switch reports its STOPERROR_LIMIT_HIT code,
  - a serial stop request halts and is recoverable via resume,
  - malformed serial input is rejected (invalid marker / transmission error),
  - the serial watchdog forces a stop when the host goes silent.

Pin polarity follows config.driveboardusb.h with SENSE_INVERT: limit/door/
chiller are ACTIVE HIGH. Self-skips unless avr-gcc + libsimavr-dev are present.
"""

import firmware_sim as fw
import pytest

pytestmark = fw.skip_unless_available


def _status():
    """A doubled CMD_SUPERSTATUS request (the firmware needs each byte twice)."""
    return fw.double([fw.CMD_SUPERSTATUS])


# ---------------------------------------------------------------------------
# Baseline: a healthy machine must not raise any safety condition.
# ---------------------------------------------------------------------------


def test_baseline_no_false_safety_trips():
    out, hello = fw.run(send=_status())
    assert hello
    assert fw.STATUS_END in out
    assert fw.INFO_DOOR_OPEN not in out
    assert fw.INFO_CHILLER_OFF not in out
    assert fw.STOPERROR_SERIAL_WATCHDOG not in out
    for code in fw.STOPERROR_LIMIT_HIT.values():
        assert code not in out


# ---------------------------------------------------------------------------
# Interlocks: door + chiller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("door_bit", [fw.DOOR1_PORTD_BIT, fw.DOOR2_PORTD_BIT])
def test_door_open_reported(door_bit):
    out, _ = fw.run(send=_status(), portd=[door_bit])
    assert fw.INFO_DOOR_OPEN in out, "firmware did not report INFO_DOOR_OPEN"


def test_chiller_off_reported():
    out, _ = fw.run(send=_status(), portd=[fw.CHILLER_PORTD_BIT])
    assert fw.INFO_CHILLER_OFF in out, "firmware did not report INFO_CHILLER_OFF"


# ---------------------------------------------------------------------------
# Limit switches: every axis endstop must report its own stop code.
#
# X/Y limits exist on the default 2-axis build (SENSE_INVERT -> active HIGH).
# Z limits only exist on the 3-axis mill build, which has SENSE_INVERT OFF
# (active LOW), so we drive the *other* limits high and leave the target low.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("axis", fw.XY_LIMITS)
def test_xy_limit_switch_reported(axis):
    out, _ = fw.run(send=_status(), portc=[fw.LIMIT_PORTC_BIT[axis]])
    assert fw.STOPERROR_LIMIT_HIT[axis] in out, f"limit {axis} not reported"


@pytest.mark.parametrize("axis", fw.Z_LIMITS)
def test_z_limit_switch_reported_on_mill(axis):
    target_bit = fw.LIMIT_PORTC_BIT[axis]
    # Active-low: hold every limit pin high except the one under test.
    held_high = [b for b in fw.LIMIT_PORTC_BIT.values() if b != target_bit]
    out, _ = fw.run(send=_status(), portc=held_high, variant=fw.MILL_VARIANT)
    assert fw.STOPERROR_LIMIT_HIT[axis] in out, f"limit {axis} not reported (mill)"
    # The held-high axes must NOT report (proves polarity, not a blanket trip).
    for other in fw.XY_LIMITS:
        assert fw.STOPERROR_LIMIT_HIT[other] not in out, f"{other} falsely tripped"


# ---------------------------------------------------------------------------
# Serial stop request + resume
# ---------------------------------------------------------------------------


def test_serial_stop_request_reported():
    out, _ = fw.run(send=fw.double([fw.CMD_STOP, fw.CMD_SUPERSTATUS]))
    assert fw.STOPERROR_SERIAL_STOP_REQUEST in out, "stop request not reported"


def test_resume_clears_stop():
    # Stop, read status WHILE stopped (so the stop code is actually emitted),
    # resume, then read status again. Discriminating: with a working resume the
    # final frame is clean; a no-op resume would leave the stop set and the last
    # frame would still carry the stop marker.
    out, _ = fw.run(
        send=fw.double(
            [fw.CMD_STOP, fw.CMD_SUPERSTATUS, fw.CMD_RESUME, fw.CMD_SUPERSTATUS, fw.CMD_SUPERSTATUS]
        )
    )
    assert fw.STOPERROR_SERIAL_STOP_REQUEST in out, "stop was never actually reported"
    assert fw.STOPERROR_SERIAL_STOP_REQUEST not in fw.frames(out)[-1], "resume did not clear stop"


# ---------------------------------------------------------------------------
# Pause / unpause (freeze in place, beam off) -> INFO_PAUSED status flag
# ---------------------------------------------------------------------------


def test_pause_reports_paused():
    out, _ = fw.run(send=fw.double([fw.CMD_PAUSE, fw.CMD_SUPERSTATUS]))
    assert fw.INFO_PAUSED in out, "pause not reflected by INFO_PAUSED"


def test_unpause_clears_paused():
    out, _ = fw.run(
        send=fw.double([fw.CMD_PAUSE, fw.CMD_UNPAUSE, fw.CMD_SUPERSTATUS, fw.CMD_SUPERSTATUS])
    )
    assert fw.INFO_PAUSED not in fw.frames(out)[-1], "unpause did not clear the paused flag"


# ---------------------------------------------------------------------------
# Stop override: an e-stop must win over a pause and reach the controller even
# with data queued ahead of it (control chars act in the RX ISR immediately).
# ---------------------------------------------------------------------------


def test_stop_overrides_pause():
    out, _ = fw.run(send=fw.double([fw.CMD_PAUSE, fw.CMD_STOP, fw.CMD_SUPERSTATUS]))
    assert fw.STOPERROR_SERIAL_STOP_REQUEST in out, "stop not reported after a pause"
    assert fw.INFO_PAUSED not in fw.frames(out)[-1], "stop did not override the pause"


def test_stop_bypasses_queued_data():
    # Queue several buffered parameter frames, THEN issue the stop + status.
    queued = fw.double(fw.encode_value_param(fw.PARAM_INTENSITY, 100)) * 3
    out, _ = fw.run(send=queued + fw.double([fw.CMD_STOP, fw.CMD_SUPERSTATUS]))
    assert fw.STOPERROR_SERIAL_STOP_REQUEST in out, "stop did not bypass the queued data"


# ---------------------------------------------------------------------------
# Malformed serial input is rejected safely
# ---------------------------------------------------------------------------


def test_invalid_control_marker_rejected():
    # Control byte 15 (<16) is not a valid command -> STOPERROR_INVALID_MARKER.
    out, _ = fw.run(send=fw.double([15]) + fw.double([fw.CMD_SUPERSTATUS]))
    assert fw.STOPERROR_INVALID_MARKER in out


def test_transmission_error_rejected():
    # Two mismatched bytes break the duplicate-transmission check.
    out, _ = fw.run(send=[ord("B"), ord("C")] + fw.double([fw.CMD_SUPERSTATUS]))
    assert fw.STOPERROR_TRANSMISSION_ERROR in out


def test_invalid_command_rejected():
    # 'P' (0x50) is a well-formed [A-Z] marker but not a defined command.
    out, _ = fw.run(send=fw.double([ord("P")]) + fw.double([fw.CMD_SUPERSTATUS]))
    assert fw.STOPERROR_INVALID_COMMAND in out


def test_invalid_parameter_rejected():
    # Four data bytes followed by an undefined [a-z] parameter marker ('q').
    out, _ = fw.run(
        send=fw.double([180, 181, 182, 183, ord("q")]) + fw.double([fw.CMD_SUPERSTATUS])
    )
    assert fw.STOPERROR_INVALID_PARAMETER in out


def test_invalid_data_overflow_rejected():
    # More than four consecutive data bytes (no parameter marker) is invalid.
    out, _ = fw.run(send=fw.double([180, 181, 182, 183, 184]) + fw.double([fw.CMD_SUPERSTATUS]))
    assert fw.STOPERROR_INVALID_DATA in out


# ---------------------------------------------------------------------------
# Multiple simultaneous limits must all be reported without faulting.
# ---------------------------------------------------------------------------


def test_multiple_limits_reported_together():
    out, _ = fw.run(
        send=_status(),
        portc=[fw.LIMIT_PORTC_BIT["x1"], fw.LIMIT_PORTC_BIT["y1"]],
    )
    assert fw.STOPERROR_LIMIT_HIT["x1"] in out
    assert fw.STOPERROR_LIMIT_HIT["y1"] in out
    # firmware stays alive and keeps producing frames
    assert fw.STATUS_END in out


# ---------------------------------------------------------------------------
# Serial watchdog: host silence forces a safe stop.
#
# NOTE: simavr's modeled WDT fires sooner than the firmware's configured 1s
# period, so these bound the *behavior* (stops on prolonged silence, does not
# trip on a brief gap) rather than validating the exact timeout.
# ---------------------------------------------------------------------------


def test_watchdog_trips_on_host_silence():
    out, _ = fw.run(send=_status(), idle_cycles=18_000_000, run_cycles=42_000_000)
    assert fw.STOPERROR_SERIAL_WATCHDOG in out, "watchdog did not stop on host silence"


def test_watchdog_not_tripped_on_brief_gap():
    # A short idle then a prompt status request must not raise the watchdog.
    out, _ = fw.run(send=_status(), idle_cycles=50_000, run_cycles=4_000_000)
    assert fw.STATUS_END in out, "expected a status frame"
    assert fw.STOPERROR_SERIAL_WATCHDOG not in out


# ---------------------------------------------------------------------------
# Laser beam cutoff on interlock (THE beam-kill guarantee).
#
# A dwell drives the laser PWM duty (the `pwm_duty` SRAM symbol) non-zero. With
# the door/chiller interlock asserted, control_laser_intensity must force the
# duty to 0 so the beam never fires. We watch the max duty seen over the run:
# closed -> reaches the commanded value; interlocked -> stays 0.
# ---------------------------------------------------------------------------


def test_beam_fires_when_interlocks_clear():
    _, _, sym = fw.run(
        send=fw.dwell_program(200, 0.2), run_cycles=8_000_000, watch_symbol="pwm_duty"
    )
    assert sym["max"] > 0, "beam never fired on a clear machine (dwell did not drive PWM)"


@pytest.mark.parametrize(
    "portd",
    [[fw.DOOR1_PORTD_BIT], [fw.DOOR2_PORTD_BIT], [fw.CHILLER_PORTD_BIT]],
    ids=["door1", "door2", "chiller"],
)
def test_beam_forced_off_on_interlock(portd):
    _, _, sym = fw.run(
        send=fw.dwell_program(200, 0.2),
        portd=portd,
        run_cycles=8_000_000,
        watch_symbol="pwm_duty",
    )
    assert sym["max"] == 0, "laser duty was non-zero while an interlock was open"


# ---------------------------------------------------------------------------
# A limit hit must halt an IN-PROGRESS move in the stepper ISR (not merely be
# reported). We run a long line move and count step pulses on the X step pin:
# the control move completes; tripping the limit partway through stops stepping
# early, so far fewer pulses are emitted.
#
# (Raster-mode beam safety and the homing delimit cycle remain out of scope:
# they need more elaborate in-sim motion choreography.)
# ---------------------------------------------------------------------------


def test_limit_halts_active_move():
    far = fw.line_program(600, feedrate=3000)
    bit = fw.X_STEP_PORTB_BIT

    # Control: the move runs to completion and emits its full pulse train.
    _, _, ctl = fw.run(send=far, run_cycles=12_000_000, count_portb=bit)
    assert ctl["steps"] > 0, "control move produced no step pulses"

    # Trip the X1 limit ~1.5M cycles in, after stepping has started.
    _, _, mid = fw.run(
        send=far,
        portc=[fw.LIMIT_PORTC_BIT["x1"]],
        portc_delay=1_500_000,
        run_cycles=12_000_000,
        count_portb=bit,
    )
    assert mid["steps"] > 0, "move never started before the limit was tripped"
    assert mid["steps"] < ctl["steps"], "limit did not halt the in-progress move"
