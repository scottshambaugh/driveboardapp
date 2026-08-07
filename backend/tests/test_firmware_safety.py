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
# Serial watchdog: host silence stops the machine, but only when it has motion
# to halt. An idle machine has nothing to run away, and a host that is merely
# busy rather than gone must not cost the operator a stop and a re-home.
#
# NOTE: simavr's modeled WDT fires sooner than the firmware's configured 1s
# period, so these bound the *behavior* (stops a moving machine on prolonged
# silence, leaves an idle one alone, does not trip on a brief gap) rather than
# validating the exact timeout.
# ---------------------------------------------------------------------------


def test_watchdog_leaves_an_idle_machine_running():
    # Long silence with an empty block buffer: no stop code, and the firmware
    # still reports itself idle rather than stopped.
    out, _, info = fw.run(
        send=_status(),
        idle_cycles=18_000_000,
        run_cycles=42_000_000,
        watch_symbol="stop_status",
    )
    assert fw.STOPERROR_SERIAL_WATCHDOG not in out, "idle machine stopped for nothing"
    assert info["final"] == fw.STOPERROR_OK
    assert fw.INFO_IDLE_YES in out, "expected the machine to still report idle"
    assert fw.STATUS_END in out


def test_watchdog_leaves_the_beam_off_while_idle():
    out, _, info = fw.run(
        send=_status(),
        idle_cycles=18_000_000,
        run_cycles=42_000_000,
        watch_symbol="pwm_duty",
    )
    assert info["max"] == 0, "beam duty rose while idle"
    assert fw.STATUS_END in out


def test_watchdog_stops_a_moving_machine():
    # A long move then silence: the stepper is still stepping when the watchdog
    # fires, so the stop stands. stop_status is read directly because asking
    # for status would feed the watchdog.
    _out, _, info = fw.run(
        send=fw.line_program(200.0, feedrate=600),
        run_cycles=42_000_000,
        watch_symbol="stop_status",
    )
    assert info["final"] == fw.STOPERROR_SERIAL_WATCHDOG, "moving machine was not stopped"


def test_watchdog_stops_a_dwelling_machine():
    # A dwell holds the beam on in one spot without moving, the state that
    # looks idle from outside and is the most dangerous to leave running.
    dwell = fw.dwell_program(200, 5.0)
    _out, _, info = fw.run(send=dwell, run_cycles=42_000_000, watch_symbol="stop_status")
    assert info["final"] == fw.STOPERROR_SERIAL_WATCHDOG, "dwelling machine was not stopped"


def test_watchdog_kills_the_beam_of_a_dwelling_machine():
    dwell = fw.dwell_program(200, 5.0)
    _out, _, info = fw.run(send=dwell, run_cycles=42_000_000, watch_symbol="pwm_duty")
    assert info["max"] > 0, "the dwell never fired the beam"
    assert info["final"] == 0, "beam left on after the watchdog fired"


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
# Intensity is stored in a uint8_t but arrives as a double spanning the whole
# 28bit wire range, where an out of range value is undefined behavior rather
# than a wrap. The firmware bounds it to 0-255 before the narrowing, so an
# absurd request cannot land on a duty higher than the one asked for.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("intensity", [1000.0, 100000.0], ids=["over", "way-over"])
def test_intensity_above_range_clamped(intensity):
    _, _, sym = fw.run(
        send=fw.dwell_program(intensity, 0.2), run_cycles=8_000_000, watch_symbol="pwm_duty"
    )
    assert sym["max"] <= 255, "duty exceeded full scale"


@pytest.mark.parametrize("intensity", [-1.0, -100000.0], ids=["under", "way-under"])
def test_intensity_below_range_clamped_to_off(intensity):
    # a negative narrowed to uint8_t commonly lands on 255, ie full power
    _, _, sym = fw.run(
        send=fw.dwell_program(intensity, 0.2), run_cycles=8_000_000, watch_symbol="pwm_duty"
    )
    assert sym["max"] == 0, "a negative intensity request fired the beam"


# ---------------------------------------------------------------------------
# A limit hit must halt an IN-PROGRESS move in the stepper ISR (not merely be
# reported). We run a long line move and count step pulses on the X step pin:
# the control move completes; tripping the limit partway through stops stepping
# early, so far fewer pulses are emitted.
#
# (Raster-mode beam safety and the homing delimit cycle remain out of scope:
# they need more elaborate in-sim motion choreography.)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Raster data must never outlive the block that reads it. The planner drops any
# move shorter than a step, so a raster move that rounds to zero length leaves
# its pixels with no consumer. The protocol loop blocks in raster mode without
# reading a byte, so the rx buffer stops draining and the host, which only sends
# as far ahead as the buffer reports room for, gates forever.
# ---------------------------------------------------------------------------


def _raster_then_move(target_x, pixels=(200, 200, 200, 200)):
    """Run a raster move to target_x followed by a line move, and report
    whether raster mode cleared and how far the line move got.

    Stays inside the 1s serial watchdog window (16M cycles at 16MHz). Past it
    the watchdog stops the machine, which clears raster mode on its way out and
    would hide a stall behind a rescue.
    """
    send = fw.raster_program(target_x, pixels, pixel_width=0.5, feedrate=3000) + fw.line_program(
        20, feedrate=3000
    )
    _out, _hello, info = fw.run(
        send=send,
        run_cycles=8_000_000,
        watch_symbol="raster_mode",
        count_portb=fw.X_STEP_PORTB_BIT,
    )
    return info


def test_raster_move_consumes_its_data():
    # control: a raster move long enough to queue a block reads its own pixels
    info = _raster_then_move(2.0)
    assert info["max"] == 1, "the firmware never entered raster mode"
    assert info["final"] == 0, "raster mode never cleared"
    assert info["steps"] > 0


def test_zero_length_raster_move_does_not_stall_the_protocol_loop():
    # the head starts at the origin, so targeting it makes the move zero length
    # and the planner drops it
    info = _raster_then_move(0.0)
    assert info["max"] == 1, "the firmware never entered raster mode"
    assert info["final"] == 0, "raster mode never cleared, the protocol loop is stuck"
    assert info["steps"] > 0, "the move queued behind the raster data never ran"


# ---------------------------------------------------------------------------
# Raster pixels are latched by distance travelled, one per pixel width, in
# every part of the speed profile, with intensity scaled to the current speed
# so energy per mm holds through the ramps. At 4000 mm/min the ramp covers
# nominal^2/(4*accel) ~ 2.2mm at the configured 500 mm/s^2, so a short run
# never cruises and must engrave anyway. Runs use the host's real segment
# shape (seek, colinear lead-in, raster, lead-out): the lead-in gives the
# raster block the entry speed the planner allows a short block.
# ---------------------------------------------------------------------------

RASTER_NOMINAL_DUTY = 199  # intensity 200 after the [128,255] pixel mapping


def _segment_duty(pixels, span_px, portd=None):
    start = 11.125
    send = fw.raster_segment_program(start, start + span_px * 0.1, pixels)
    _, _, info = fw.run(send=send, run_cycles=8_000_000, watch_symbol="pwm_duty", portd=portd)
    return info


def test_short_raster_run_fires_inside_the_speed_ramp():
    # 0.4mm at 4000 mm/min lies entirely inside the acceleration ramp
    info = _segment_duty([255] * 4, 4)
    assert info["max"] > 0, "short raster run never fired the beam"


@pytest.mark.parametrize("hot", [0, 8, 16], ids=["first", "middle", "last"])
def test_raster_pixels_latch_across_the_whole_run(hot):
    # a lone hot pixel anywhere in a ramp-bound run must fire at its position
    px = [128] * 17
    px[hot] = 255
    info = _segment_duty(px, 17)
    assert info["max"] > 0, f"pixel {hot} of 17 never fired"


def test_raster_intensity_scales_down_on_the_ramp():
    # a run entirely inside the ramp burns at the ramp's fraction of nominal
    # (~30-40% of speed for 0.4mm), never anywhere near full duty
    info = _segment_duty([255] * 4, 4)
    assert 0 < info["max"] < 150, f"ramp run duty {info['max']} not speed-scaled"


def test_raster_intensity_reaches_nominal_at_cruise():
    # a run long enough to cruise must still burn at full commanded intensity
    info = _segment_duty([255] * 60, 60)
    assert info["max"] >= RASTER_NOMINAL_DUTY - 9, f"cruise duty only {info['max']}"


def test_beam_dark_after_raster_line_ends():
    # a hot trailing pixel must not bleed into the lead-out
    info = _segment_duty([255] * 4, 4)
    assert info["max"] > 0
    assert info["final"] == 0, "beam left on after the raster line finished"


@pytest.mark.parametrize(
    "portd", [[fw.DOOR1_PORTD_BIT], [fw.CHILLER_PORTD_BIT]], ids=["door", "chiller"]
)
def test_raster_beam_stays_dark_on_interlock(portd):
    # latched pixels go through the interlock-guarded setter like any other
    # intensity write, so an open door keeps the beam dark mid-raster
    info = _segment_duty([255] * 17, 17, portd=portd)
    assert info["max"] == 0, "raster fired the beam with an interlock open"


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


# ---------------------------------------------------------------------------
# A dwell clears its own progress only when it runs to completion, so every
# path that abandons the current block has to clear it too. Otherwise the
# leftover count carries into the next dwell, which then burns for the wrong
# time at whatever tick rate the preceding move left behind.
# ---------------------------------------------------------------------------


def test_dwell_progress_cleared_when_a_stop_abandons_it():
    # trip a limit part way through a long pierce, the way a real stop lands
    _, _, info = fw.run(
        send=fw.dwell_program(200, 0.6),
        run_cycles=8_000_000,
        watch_symbol="dwell_counter",
        portc=[fw.LIMIT_PORTC_BIT["x1"]],
        portc_delay=1_000_000,
    )
    assert info["max"] > 0, "the dwell never ran, so the stop proves nothing"
    assert info["final"] == 0, "abandoned dwell left its count for the next pierce"


# ---------------------------------------------------------------------------
# Assist relays are switched through the motion planner, so a stop that throws
# the block buffer away also throws away the queued disable. They have to be
# driven off directly, the way the beam is, or they stay energised for good.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "on_cmd,bit",
    [
        (fw.CMD_AIR_ENABLE, fw.AIR_ASSIST_PORTD_BIT),
        (fw.CMD_AUX_ENABLE, fw.AUX_ASSIST_PORTD_BIT),
    ],
    ids=["air", "aux"],
)
def test_assist_de_energised_by_a_stop(on_cmd, bit):
    # switch the assist on, start a long move, then trip a limit part way in
    send = fw.double([on_cmd]) + fw.line_program(600, feedrate=3000)

    _, _, control = fw.run(send=send, run_cycles=8_000_000, watch_pin=("D", bit))
    assert control["everhigh"] == 1, "the assist never came on, so the stop proves nothing"
    assert control["final"] == 1, "the assist should stay on when nothing stops the job"

    _, _, stopped = fw.run(
        send=send,
        run_cycles=8_000_000,
        watch_pin=("D", bit),
        portc=[fw.LIMIT_PORTC_BIT["x1"]],
        portc_delay=2_000_000,
    )
    assert stopped["everhigh"] == 1
    assert stopped["final"] == 0, "assist left energised after a stop"
