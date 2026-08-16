"""Modelled run time of the command stream on its way to the machine.

The serial loop feeds every parameter and command it buffers to a JobTimer,
which replays the firmware planner's motion model over them. The timer
therefore times exactly the moves the machine will run, with the ordering,
lead-ins, pierces and acceleration ramps that dispatch actually produced, and
without a second walk over the job.

Because it watches the byte stream, it also knows where in that stream each
move sits, which is what turns transmitted bytes into a time remaining.
"""

import bisect
import math

from jobimport import pathoptimizer


class JobTimer:
    """Run time of a command stream, accumulated as the stream is built.

    Feed it param() and command() in emission order, each command with the
    byte offset the stream has reached just past it. total() is the modelled
    run time, time_at() the time still to come at a byte offset.

    The model looks one move ahead, enough to know the speed each junction
    carries. The firmware plans over a whole buffer, so a run of short moves
    it starts braking for several moves early is timed slightly fast here.
    """

    # A move is checkpointed once the stream has advanced this far past the
    # last checkpoint, which bounds the table on jobs of millions of moves.
    # Time between checkpoints is interpolated linearly.
    CHECKPOINT_BYTES = 2048

    _AXIS = {"x": 0, "y": 1, "z": 2}

    def __init__(self, commands=None, params=None):
        """commands and params map the protocol characters driveboard sends
        onto the events below, so the wire protocol stays defined in one
        place."""
        self._commands = commands or {}
        self._params = params or {}
        self._target = [0.0, 0.0, 0.0]
        self._pos = [0.0, 0.0, 0.0]
        self._relative = False
        self._relative_store = False
        self._feedrate = 0.0  # mm/min
        self._dwell = 0.0  # seconds
        self.reset()

    def reset(self):
        """Start timing a new stream. The controller keeps its position, feed
        rate and reference mode between jobs, so the model keeps them too and
        only the timing state clears."""
        self._pending = None  # the move in hand, awaiting its exit speed
        self._time = 0.0  # time up to the start of the move in hand
        self._offset = 0  # stream offset that time was reached at
        self._offsets = [0]  # checkpoint offsets, ascending
        self._times = [0.0]  # time reached at each checkpoint
        self._pass_starts = []  # stream offset each pass begins at, ascending

    ################ stream events

    def param(self, param, val):
        """A parameter on its way to the controller, by protocol character."""
        kind = self._params.get(param)
        if kind is None:
            return
        axis = self._AXIS.get(kind)
        if axis is not None:
            # the controller accumulates relative targets and replaces
            # absolute ones, see on_param in firmware/src/protocol.c
            if self._relative:
                self._target[axis] += val
            else:
                self._target[axis] = val
        elif kind == "feedrate":
            self._feedrate = val
        elif kind == "duration":
            self._dwell = val

    def command(self, command, byte_offset):
        """A command on its way to the controller, by protocol character,
        with the stream offset just past it."""
        kind = self._commands.get(command)
        if kind == "line" or kind == "raster":
            self._move(byte_offset)
        elif kind == "dwell":
            self._flush(0.0, byte_offset)  # a dwell is a full stop either side
            self._time += self._dwell
        elif kind == "relative":
            self._relative = True
        elif kind == "absolute":
            self._relative = False
        elif kind == "ref_store":
            self._relative_store = self._relative
        elif kind == "ref_restore":
            self._relative = self._relative_store
        elif kind == "homing":
            # a homing cycle runs its own search over an unknown travel, so it
            # re-anchors the model rather than contributing a time
            self._flush(0.0, byte_offset)
            self._target = [0.0, 0.0, 0.0]
            self._pos = [0.0, 0.0, 0.0]

    def mark_pass(self, byte_offset):
        """Note that a pass begins here. Dispatch marks the start of every
        pass and the end of the last one, which is what lets the time left be
        reported for the pass running as well as for the whole job."""
        self._pass_starts.append(byte_offset)

    ################ results

    def total(self):
        """Modelled run time (s) of the stream so far, the move in hand
        decelerating to a stop at its end."""
        return self._time + self._pending_time(0.0)

    def time_at(self, byte_offset):
        """Modelled time (s) the machine is into the job once it has executed
        up to `byte_offset`."""
        if byte_offset <= 0:
            return 0.0
        if byte_offset >= self._offset:
            # everything emitted has been taken in, including the move in hand
            return self.total()
        i = bisect.bisect_right(self._offsets, byte_offset) - 1
        if i < len(self._offsets) - 1:
            o0, t0, o1, t1 = (
                self._offsets[i],
                self._times[i],
                self._offsets[i + 1],
                self._times[i + 1],
            )
        else:
            # past the last checkpoint, interpolate into the live tail
            o0, t0, o1, t1 = self._offsets[-1], self._times[-1], self._offset, self._time
        if byte_offset >= o1 or o1 <= o0:
            return t1
        return t0 + (byte_offset - o0) / (o1 - o0) * (t1 - t0)

    def remaining(self, byte_offset):
        """Modelled time (s) left once `byte_offset` has been executed."""
        return max(0.0, self.total() - self.time_at(byte_offset))

    def remaining_in_pass(self, byte_offset):
        """Modelled time (s) left in the pass being executed at `byte_offset`,
        the whole remainder when no passes were marked. What follows the last
        marked pass is the run home, which counts as its own stretch."""
        if not self._pass_starts:
            return self.remaining(byte_offset)
        i = bisect.bisect_right(self._pass_starts, byte_offset)
        if i >= len(self._pass_starts):
            return self.remaining(byte_offset)
        return max(0.0, self.time_at(self._pass_starts[i]) - self.time_at(byte_offset))

    ################ motion model

    def _move(self, byte_offset):
        dx = self._target[0] - self._pos[0]
        dy = self._target[1] - self._pos[1]
        dz = self._target[2] - self._pos[2]
        length = math.sqrt(dx * dx + dy * dy + dz * dz)
        vmax = max(self._feedrate, 0.0) / 60.0
        if length < 1e-9 or vmax <= 0.0:
            # no travel, so no time to take and no direction to turn out of
            self._pos = list(self._target)
            return
        direction = (dx / length, dy / length, dz / length)
        v_in = 0.0
        if self._pending is not None:
            p_len, p_vmax, p_dir, p_vin = self._pending
            cos_theta = -(
                p_dir[0] * direction[0] + p_dir[1] * direction[1] + p_dir[2] * direction[2]
            )
            v_in = pathoptimizer.junction_speed_cos(cos_theta, min(p_vmax, vmax))
            self._time += pathoptimizer.trapezoid_time(
                p_len, p_vmax, pathoptimizer.ACCEL, p_vin, v_in
            )
        # the machine is at this time when it has taken in the move just past
        # byte_offset, which it has yet to run
        self._advance(byte_offset)
        self._pending = (length, vmax, direction, v_in)
        self._pos = list(self._target)

    def _pending_time(self, v_out):
        if self._pending is None:
            return 0.0
        p_len, p_vmax, _p_dir, p_vin = self._pending
        return pathoptimizer.trapezoid_time(p_len, p_vmax, pathoptimizer.ACCEL, p_vin, v_out)

    def _flush(self, v_out, byte_offset):
        """Close out the move in hand, leaving it at speed v_out."""
        if self._pending is not None:
            self._time += self._pending_time(v_out)
            self._pending = None
        self._advance(byte_offset)

    def _advance(self, byte_offset):
        self._offset = byte_offset
        if byte_offset - self._offsets[-1] >= self.CHECKPOINT_BYTES:
            self._offsets.append(byte_offset)
            self._times.append(self._time)
