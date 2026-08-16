"""The run-time model that watches the command stream, see backend/jobtime.py."""

import math

import driveboard
import pytest
from jobimport import pathoptimizer
from jobtime import JobTimer


def new_timer():
    """A timer at the origin. Every case starts from one, since reset() keeps
    the head where it is on purpose."""
    return JobTimer(driveboard.TIMED_COMMANDS, driveboard.TIMED_PARAMS)


@pytest.fixture
def timer():
    return new_timer()


def feed_move(timer, x=None, y=None, z=None, rate=None, offset=None):
    """Emit a move the way driveboard.move() does, returning the byte offset
    just past it. Byte counts match send_param/send_command: 5 per param, 1
    per command."""
    if offset is None:
        offset = timer._offset
    if rate is not None:
        timer.param(driveboard.PARAM_FEEDRATE, rate)
        offset += 5
    for param, val in (
        (driveboard.PARAM_TARGET_X, x),
        (driveboard.PARAM_TARGET_Y, y),
        (driveboard.PARAM_TARGET_Z, z),
    ):
        if val is not None:
            timer.param(param, val)
            offset += 5
    offset += 1
    timer.command(driveboard.CMD_LINE, offset)
    return offset


def test_straight_move_matches_the_trapezoid_model(timer):
    feed_move(timer, x=100.0, y=0.0, rate=2000.0)
    expected = pathoptimizer.trapezoid_time(100.0, 2000.0 / 60.0, pathoptimizer.ACCEL, 0.0, 0.0)
    assert timer.total() == pytest.approx(expected)


def test_a_move_is_slower_than_its_length_over_its_rate(timer):
    # the ramps at either end are what the old length/rate estimate missed
    feed_move(timer, x=100.0, y=0.0, rate=2000.0)
    assert timer.total() > 100.0 / (2000.0 / 60.0)


def test_collinear_moves_cost_no_more_than_one_long_one(timer):
    # the planner carries full speed through a straight junction, so splitting
    # a move must not invent two more ramps
    feed_move(timer, x=100.0, y=0.0, rate=2000.0)
    whole = timer.total()
    split = new_timer()
    feed_move(split, x=50.0, y=0.0, rate=2000.0)
    feed_move(split, x=100.0, y=0.0)
    assert split.total() == pytest.approx(whole)


def test_a_reversal_costs_a_full_stop(timer):
    feed_move(timer, x=50.0, y=0.0, rate=2000.0)
    feed_move(timer, x=0.0, y=0.0)
    straight = new_timer()
    feed_move(straight, x=50.0, y=0.0, rate=2000.0)
    feed_move(straight, x=100.0, y=0.0)
    assert timer.total() > straight.total()


def test_a_right_angle_costs_less_than_a_reversal_and_more_than_straight():
    def two_moves(second):
        timer = new_timer()
        feed_move(timer, x=50.0, y=0.0, rate=2000.0)
        feed_move(timer, **second)
        return timer.total()

    straight = two_moves({"x": 100.0, "y": 0.0})
    corner = two_moves({"x": 50.0, "y": 50.0})
    reversal = two_moves({"x": 0.0, "y": 0.0})
    assert straight < corner < reversal


def test_a_dwell_adds_its_own_time_and_stops_the_head(timer):
    feed_move(timer, x=100.0, y=0.0, rate=2000.0)
    moved = timer.total()
    timer.param(driveboard.PARAM_DURATION, 0.5)
    timer.command(driveboard.CMD_DWELL, timer._offset + 6)
    assert timer.total() == pytest.approx(moved + 0.5)


def test_relative_targets_accumulate(timer):
    timer.command(driveboard.CMD_REF_RELATIVE, 1)
    feed_move(timer, x=30.0, y=0.0, rate=2000.0)
    feed_move(timer, x=30.0)  # another 30 on, not back to 30
    timer.command(driveboard.CMD_REF_ABSOLUTE, timer._offset + 1)
    absolute = JobTimer(driveboard.TIMED_COMMANDS, driveboard.TIMED_PARAMS)
    feed_move(absolute, x=60.0, y=0.0, rate=2000.0)
    assert timer.total() == pytest.approx(absolute.total())


def test_z_travel_is_timed(timer):
    feed_move(timer, x=0.0, y=0.0, z=10.0, rate=2000.0)
    assert timer.total() > 0.0


def test_a_zero_length_move_takes_no_time(timer):
    feed_move(timer, x=0.0, y=0.0, rate=2000.0)
    assert timer.total() == 0.0


def test_time_at_walks_the_stream_forwards(timer):
    # one long run of moves at a steady rate, so time really is proportional
    # to bytes and the checkpoint interpolation has an exact answer to hit
    offsets = [feed_move(timer, x=float(20 * i), y=0.0, rate=2000.0) for i in range(1, 501)]
    times = [timer.time_at(o) for o in offsets]
    assert times == sorted(times)
    assert times[0] < timer.total() / 100.0
    assert times[-1] <= timer.total()
    half = times[len(times) // 2]
    assert half == pytest.approx(timer.total() / 2.0, rel=0.02)


def test_remaining_falls_to_zero_over_the_stream(timer):
    end = None
    for i in range(1, 501):
        end = feed_move(timer, x=float(20 * i), y=0.0, rate=2000.0)
    assert timer.remaining(0) == pytest.approx(timer.total())
    assert timer.remaining(end // 2) == pytest.approx(timer.total() / 2.0, rel=0.02)
    assert timer.remaining(end) == pytest.approx(0.0, abs=timer.total() / 100.0)
    assert timer.remaining(end * 2) == 0.0


def test_remaining_in_pass_covers_only_the_pass_being_run(timer):
    timer.mark_pass(0)
    first = feed_move(timer, x=100.0, y=0.0, rate=2000.0)
    timer.mark_pass(first)
    feed_move(timer, x=100.0, y=100.0)
    end = feed_move(timer, x=0.0, y=100.0)
    # partway through the first pass, the pass ends well before the job does
    assert timer.remaining_in_pass(1) < timer.remaining(1)
    # the second pass is the longer one, so its share is the bigger of the two
    assert timer.remaining_in_pass(first + 1) > timer.remaining_in_pass(1)
    # nothing follows the last pass here, so it runs out with the job
    assert timer.remaining_in_pass(first + 1) == pytest.approx(timer.remaining(first + 1))
    assert end > first


def test_remaining_in_pass_is_the_whole_job_when_no_passes_are_marked(timer):
    end = feed_move(timer, x=100.0, y=0.0, rate=2000.0)
    assert timer.remaining_in_pass(end // 2) == timer.remaining(end // 2)


def test_checkpoints_stay_bounded_on_a_long_stream(timer):
    for i in range(1, 4001):
        feed_move(timer, x=float(i % 100), y=float(i % 7), rate=2000.0)
    assert len(timer._offsets) <= timer._offset / timer.CHECKPOINT_BYTES + 2


def test_reset_clears_timing_but_keeps_the_head_where_it_is(timer):
    feed_move(timer, x=100.0, y=0.0, rate=2000.0)
    timer.reset()
    assert timer.total() == 0.0
    # the controller holds its target across jobs, so a move back to where the
    # head already is still costs nothing
    feed_move(timer, x=100.0, y=0.0)
    assert timer.total() == 0.0


def test_junction_speed_cos_matches_the_planar_helper():
    vcap = 100.0
    for d_prev, d_cur in (
        ((1.0, 0.0), (1.0, 0.0)),
        ((1.0, 0.0), (0.0, 1.0)),
        ((1.0, 0.0), (-1.0, 0.0)),
        ((1.0, 0.0), (math.sqrt(0.5), math.sqrt(0.5))),
    ):
        cos_theta = -(d_prev[0] * d_cur[0] + d_prev[1] * d_cur[1])
        assert pathoptimizer.junction_speed_cos(cos_theta, vcap) == pytest.approx(
            pathoptimizer._junction_speed(d_prev, d_cur, vcap)
        )
