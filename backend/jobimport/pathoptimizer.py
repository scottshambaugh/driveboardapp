"""
Optimizations of paths.

The format of a path segment is:
[[x1,y1],[x2,y2],...]

The format of path is:
[pathseg1, pathseg2, ...]

This module is typically used by calling the 'optimize' function.
It takes a list of paths and optimizes in-place.
"""

__author__ = "Stefan Hechenberger <stefan@nortd.com>"


import functools
import logging
import math
import time

from . import kdtree

log = logging.getLogger("svg_reader")


def connect_segments(path, epsilon2):
    """
    Optimizes continuity of path.

    This function joins path segments if the next start point
    is congruent with the current end point.
    """
    join_count = 0
    newIdx = 0
    for i in range(1, len(path)):
        lastpathseg = path[newIdx]
        pathseg = path[i]
        point = lastpathseg[-1]
        startpoint = pathseg[0]

        # join into lastpathseg
        d2_start = (point[0] - startpoint[0]) ** 2 + (point[1] - startpoint[1]) ** 2
        if d2_start < epsilon2:
            lastpathseg.extend(pathseg[1:])
            join_count += 1
        else:
            # add pathseg to next slot
            newIdx += 1
            path[newIdx] = pathseg

    # remove exessive slots
    for _ in range(len(path) - (newIdx + 1)):
        path.pop()

    # report if excessive joins
    if join_count > 100:
        log.info("joined many path segments: " + str(join_count))


def d2(u, v):
    return (u[0] - v[0]) ** 2 + (u[1] - v[1]) ** 2


def simplifyDP(tol2, v, j, k, mk):
    #  This is the Douglas-Peucker recursive simplification routine
    #  It just marks vertices that are part of the simplified polyline
    #  for approximating the polyline subchain v[j] to v[k].
    #  mk[] ... array of markers matching vertex array v[]
    if k <= j + 1:  # there is nothing to simplify
        return
    # check for adequate approximation by segment S from v[j] to v[k]
    maxi = j  # index of vertex farthest from S
    maxd2 = 0  # distance squared of farthest vertex
    S = [v[j], v[k]]  # segment from v[j] to v[k]
    # u = diff(S[1], S[0])    # segment direction vector
    u = [S[1][0] - S[0][0], S[1][1] - S[0][1]]  # segment direction vector
    # cu = norm2(u)      # segment length squared
    cu = u[0] ** 2 + u[1] ** 2  # segment length squared
    # test each vertex v[i] for max distance from S
    # compute using the Feb 2001 Algorithm's dist_Point_to_Segment()
    # Note: this works in any dimension (2D, 3D, ...)
    w = None  # vector
    Pb = None  # point, base of perpendicular from v[i] to S
    b = 0.0
    cw = 0.0
    dv2 = 0.0  # dv2 = distance v[i] to S squared
    for i in range(j + 1, k):
        # compute distance squared
        # w = diff(v[i], S[0])
        w = [v[i][0] - S[0][0], v[i][1] - S[0][1]]  # diff
        # cw = dot(w,u)
        cw = w[0] * u[0] + w[1] * u[1]  # dot product
        if cw <= 0:
            dv2 = d2(v[i], S[0])
        elif cu <= cw:
            dv2 = d2(v[i], S[1])
        else:
            b = cw / cu
            Pb = [S[0][0] + b * u[0], S[0][1] + b * u[1]]
            dv2 = d2(v[i], Pb)
        # test with current max distance squared
        if dv2 <= maxd2:
            continue
        # v[i] is a new max vertex
        maxi = i
        maxd2 = dv2
    if maxd2 > tol2:  # error is worse than the tolerance
        # split the polyline at the farthest vertex from S
        mk[maxi] = 1  # mark v[maxi] for the simplified polyline
        # recursively simplify the two subpolylines at v[maxi]
        simplifyDP(tol2, v, j, maxi, mk)  # polyline v[j] to v[maxi]
        simplifyDP(tol2, v, maxi, k, mk)  # polyline v[maxi] to v[k]
    # else the approximation is OK, so ignore intermediate vertices
    return


def simplify(pathseg, tolerance2):
    """
    Douglas-Peucker polyline simplification.

    pathseg     ... [[x1,y1],[x2,y2],...]
    tolerance2  ... approximation tolerance squared
    ===============================================
    Copyright 2002, softSurfer (www.softsurfer.com)
    This code may be freely used and modified for any purpose
    providing that this copyright notice is included with it.
    SoftSurfer makes no warranty for this code, and cannot be held
    liable for any real or imagined damage resulting from its use.
    Users of this code must verify correctness for their application.
    http://softsurfer.com/Archive/algorithm_0205/algorithm_0205.htm
    """

    n = len(pathseg)
    if n == 0:
        return []
    sPathseg = []
    tPathseg = []  # vertex buffer, points

    # STAGE 1.  Vertex Reduction within tolerance of prior vertex cluster
    tPathseg.append(pathseg[0])  # start at the beginning
    k = 1
    pv = 0
    for i in range(1, n):
        if d2(pathseg[i], pathseg[pv]) < tolerance2:
            continue
        tPathseg.append(pathseg[i])
        k += 1
        pv = i
    if pv < n - 1:
        tPathseg.append(pathseg[n - 1])  # finish at the end
        k += 1

    # STAGE 2.  Douglas-Peucker polyline simplification
    mk = [None for i in range(k)]  # marker buffer, ints
    mk[0] = mk[k - 1] = 1  # mark the first and last vertices
    simplifyDP(tolerance2, tPathseg, 0, k - 1, mk)

    # copy marked vertices to the output simplified polyline
    for i in range(k):
        if mk[i]:
            sPathseg.append(tPathseg[i])
    return sPathseg


def simplify_all(path, tolerance2):
    totalverts = 0
    optiverts = 0
    for u in range(len(path)):
        totalverts += len(path[u])
        path[u] = simplify(path[u], tolerance2)
        optiverts += len(path[u])
    if totalverts > 0:
        # report polyline optimizations
        difflength = totalverts - optiverts
        diffpct = 100 * difflength / totalverts
        if diffpct > 10:  # if diff more than 10%
            log.info("INFO: polylines optimized by " + str(int(diffpct)) + "%")


# firmware motion constants, in sync with CONFIG_ACCELERATION and
# CONFIG_JUNCTION_DEVIATION in firmware/src/config.*.h (laser variants)
# CONFIG_ACCELERATION is scalar accel along the path in mm/min^2
ACCEL = 1800000.0 / 3600.0  # mm/s^2
JUNCTION_DEVIATION = 0.006  # mm
# rates the optimizers assume when the pass rates are not known (mm/min)
DEFAULT_SEEKRATE = 6000.0
DEFAULT_FEEDRATE = 2000.0


def _junction_speed(d_prev, d_cur, vcap, accel=ACCEL, deviation=JUNCTION_DEVIATION):
    """Speed (mm/s) the firmware planner carries through a junction between
    unit directions d_prev and d_cur, replicating planner.c: full speed when
    nearly collinear, a stop when reversing, and the centripetal
    junction-deviation approximation between. None for a direction means the
    head stops there."""
    if d_prev is None or d_cur is None:
        return 0.0
    cos_theta = -(d_prev[0] * d_cur[0] + d_prev[1] * d_cur[1])
    if cos_theta >= 0.95:  # close to 180 degree turn
        return 0.0
    if cos_theta <= -0.95:  # close to straight through
        return vcap
    sin_half = math.sqrt(0.5 * (1.0 - cos_theta))
    return min(vcap, math.sqrt(accel * deviation * sin_half / (1.0 - sin_half)))


def _trapezoid_time(length, vmax, accel, v0, v1):
    """Time (s) of a move of `length` mm under the planner's trapezoidal
    profile: enter at v0, ramp towards vmax, leave at v1 (mm/s)."""
    if length <= 0.0:
        return 0.0
    v0 = min(v0, vmax)
    v1 = min(v1, vmax)
    # an exit speed the length cannot reach collapses to what is reachable
    v1 = min(v1, math.sqrt(v0 * v0 + 2.0 * accel * length))
    v0 = min(v0, math.sqrt(v1 * v1 + 2.0 * accel * length))
    vpeak = math.sqrt(accel * length + 0.5 * (v0 * v0 + v1 * v1))
    if vpeak <= vmax:  # triangular, cruise speed never reached
        return (2.0 * vpeak - v0 - v1) / accel
    d_acc = (vmax * vmax - v0 * v0) / (2.0 * accel)
    d_dec = (vmax * vmax - v1 * v1) / (2.0 * accel)
    return (vmax - v0) / accel + (vmax - v1) / accel + (length - d_acc - d_dec) / vmax


def _unit(a, b):
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    d = math.hypot(dx, dy)
    return (dx / d, dy / d) if d > 1e-12 else None


def _neg(d):
    return None if d is None else (-d[0], -d[1])


def polyline_dirs(path):
    """Unit entry and exit directions of each polyline in a path, as the
    (entry_dirs, exit_dirs) pair improve_seek_order takes. None entries mark
    degenerate ends (single vertex, or coincident end vertices)."""
    entry_dirs = []
    exit_dirs = []
    for seg in path:
        if len(seg) < 2:
            entry_dirs.append(None)
            exit_dirs.append(None)
        else:
            entry_dirs.append(_unit(seg[0], seg[1]))
            exit_dirs.append(_unit(seg[-2], seg[-1]))
    return entry_dirs, exit_dirs


def split_closed_paths(polys, grid=30.0, closed_eps=0.001):
    """Split closed polylines into two arcs at the vertex nearest half the
    perimeter, so seek ordering may burn the near half on the way out and
    the far half on the way back. Halves stay single relocatable pieces for
    the tour moves, and each contour gains at most one extra burn start.
    Contours whose halves would be shorter than `grid` mm stay whole (small
    loops gain little and doubling their count burdens the tour search),
    open polylines pass through unchanged.

    The arcs share their boundary vertices, so an ordering that keeps them
    adjacent burns seamlessly again (emission coalesces touching polylines
    back into one continuous move). Returns (polylines, arc_flags) where
    arc_flags marks the split arcs, the input polylines are not modified.
    """
    eps2 = closed_eps * closed_eps
    out = []
    flags = []
    for p in polys:
        n = len(p)
        if not (n > 3 and d2(p[0][:2], p[-1][:2]) <= eps2):
            out.append(p)
            flags.append(False)
            continue
        lengths = [math.dist(p[k - 1][:2], p[k][:2]) for k in range(1, n)]
        perimeter = sum(lengths)
        if perimeter < 2.0 * grid:
            out.append(p)
            flags.append(False)
            continue
        half = best_k = None
        acc = 0.0
        for k in range(1, n - 1):
            acc += lengths[k - 1]
            gap = abs(acc - 0.5 * perimeter)
            if (half is None or gap < half) and acc >= grid and perimeter - acc >= grid:
                half = gap
                best_k = k
        if best_k is None:
            out.append(p)
            flags.append(False)
            continue
        out.append(p[: best_k + 1])
        out.append(p[best_k:])
        flags.extend((True, True))
    return out, flags


def rotate_closed_entries(
    polys,
    tour,
    start,
    seekrate=DEFAULT_SEEKRATE,
    feedrate=DEFAULT_FEEDRATE,
    end_rect=None,
    grid=10.0,
    closed_eps=0.001,
):
    """Rotate each closed polyline in a tour to the entry vertex with the
    fastest seek in and out, walking the tour in order. Mutates the polylines
    in place, ordering and orientation stay as the tour says.

    A polyline counts as closed only when its ends coincide within
    closed_eps (mm): rotating anything looser would close the gap with a
    burned edge. Candidate entries are existing vertices spaced at least
    `grid` mm apart along the contour (the stored entry always among them),
    so densely sampled contours stay cheap to evaluate.
    """
    eps2 = closed_eps * closed_eps
    prev_pos = start[:2]
    prev_dir = None
    for t, (ci, rev) in enumerate(tour):
        p = polys[ci]
        n = len(p)
        if n > 3 and d2(p[0][:2], p[-1][:2]) <= eps2:
            # next seek target and its entry direction, for the out cost
            next_pos = next_dir = None
            if t + 1 < len(tour):
                nci, nrev = tour[t + 1]
                np_ = polys[nci]
                next_pos = np_[-1][:2] if nrev else np_[0][:2]
                if len(np_) >= 2:
                    if nrev:
                        next_dir = _neg(_unit(np_[-2], np_[-1]))
                    else:
                        next_dir = _unit(np_[0], np_[1])
            m = n - 1  # unique vertices, p[m] duplicates p[0]
            cands = [0]
            acc = 0.0
            for k in range(1, m):
                acc += math.dist(p[k - 1][:2], p[k][:2])
                if acc >= grid:
                    cands.append(k)
                    acc = 0.0
            best = None
            for k in cands:
                t_in = _unit(p[(k - 1) % m], p[k])  # incoming edge at vertex k
                t_out = _unit(p[k], p[(k + 1) % m])  # outgoing edge at vertex k
                entry_dir = _neg(t_in) if rev else t_out
                exit_dir = _neg(t_out) if rev else t_in
                cost = seek_time(prev_pos, prev_dir, p[k][:2], entry_dir, seekrate, feedrate)
                if next_pos is not None:
                    cost += seek_time(p[k][:2], exit_dir, next_pos, next_dir, seekrate, feedrate)
                elif end_rect is not None:
                    towards = [
                        min(max(p[k][0], end_rect[0]), end_rect[2]),
                        min(max(p[k][1], end_rect[1]), end_rect[3]),
                    ]
                    cost += seek_time(p[k][:2], exit_dir, towards, None, seekrate, feedrate)
                if best is None or cost < best[0]:
                    best = (cost, k)
            k = best[1]
            if k != 0:
                # re-close at vertex k, reusing the existing first edge
                p[:] = p[k:] + p[1 : k + 1]
        prev_pos = p[0][:2] if rev else p[-1][:2]
        if len(p) >= 2:
            prev_dir = _neg(_unit(p[0], p[1])) if rev else _unit(p[-2], p[-1])
        else:
            prev_dir = None


@functools.lru_cache(maxsize=8)
def seek_cost(seekrate=DEFAULT_SEEKRATE, feedrate=DEFAULT_FEEDRATE):
    """seek_time specialized to fixed rates, for the optimization hot loops.
    Returns a cost(p0, d0, p1, d1) closure with the per-call constant work
    hoisted, the physics stays in _junction_speed and _trapezoid_time."""
    vseek = seekrate / 60.0
    vcap = min(feedrate / 60.0, vseek)

    def cost(p0, d0, p1, d1):
        dx = p1[0] - p0[0]
        dy = p1[1] - p0[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            return 0.0
        u = (dx / length, dy / length)
        v_in = _junction_speed(d0, u, vcap)
        v_out = _junction_speed(u, d1, vcap)
        return _trapezoid_time(length, vseek, ACCEL, v_in, v_out)

    return cost


def seek_time(p0, d0, p1, d1, seekrate=DEFAULT_SEEKRATE, feedrate=DEFAULT_FEEDRATE):
    """Planner-model time (s) of a seek from p0 to p1.

    d0 is the unit direction the head was moving on arrival at p0 (its last
    feed move), d1 the direction it must move leaving p1. Junction angles set
    how much speed carries into and out of the seek, so a seek continuing
    straight through rides the ramps while a direction reversal pays a full
    stop at both ends. None directions mean a stop. Rates in mm/min.
    """
    return seek_cost(seekrate, feedrate)(p0, d0, p1, d1)


def stop_seek_time(length, seekrate=DEFAULT_SEEKRATE):
    """Planner-model time (s) of a stop-to-stop seek of `length` mm."""
    return _trapezoid_time(length, seekrate / 60.0, ACCEL, 0.0, 0.0)


def _knn_ids(points, k):
    """For each 2D point, ids of up to k nearest other points.

    Grid bucketed, so near-linear in the number of points. The lists are
    candidate sets for local search, exactness is not required.
    """
    n = len(points)
    if n <= k + 1:
        out = []
        for i in range(n):
            ds = sorted((d2(points[i], points[j]), j) for j in range(n) if j != i)
            out.append([j for _dist, j in ds])
        return out
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-9)
    cell = span / math.sqrt(n)
    grid = {}
    for i, p in enumerate(points):
        grid.setdefault((int(p[0] // cell), int(p[1] // cell)), []).append(i)
    max_ring = int(span / cell) + 2
    out = []
    for i, p in enumerate(points):
        cx, cy = int(p[0] // cell), int(p[1] // cell)
        best = []  # (distance squared, id)
        for ring in range(max_ring + 1):
            # cells beyond this ring hold no point closer than (ring-1)*cell
            if ring > 0 and len(best) >= k and best[k - 1][0] <= ((ring - 1) * cell) ** 2:
                break
            if ring == 0:
                cells = ((cx, cy),)
            else:
                xr = range(cx - ring, cx + ring + 1)
                cells = (
                    [(x, cy - ring) for x in xr]
                    + [(x, cy + ring) for x in xr]
                    + [(cx - ring, y) for y in range(cy - ring + 1, cy + ring)]
                    + [(cx + ring, y) for y in range(cy - ring + 1, cy + ring)]
                )
            grown = False
            for key in cells:
                for j in grid.get(key, ()):
                    if j != i:
                        best.append((d2(p, points[j]), j))
                        grown = True
            if grown:
                best.sort()
                del best[k:]
        out.append([j for _dist, j in best])
    return out


def improve_seek_order(
    starts,
    ends,
    tour,
    start,
    k=None,
    max_passes=50,
    time_budget=3.0,
    dirs=None,
    seekrate=DEFAULT_SEEKRATE,
    feedrate=DEFAULT_FEEDRATE,
    end_rect=None,
):
    """2-opt improvement of an open seek tour over reversible segments,
    minimizing planner-model seek time (see seek_time).

    starts, ends ... per-segment endpoint coords (entry/exit when not reversed)
    tour         ... visit order as [seg_index, reversed] pairs, improved in place
    start        ... head position before the first seek
    dirs         ... optional (entry_dirs, exit_dirs) unit vectors per segment
                     in the unreversed orientation, None entries mean a stop.
                     Reversing a segment negates and swaps its pair.
    end_rect     ... optional [x0, y0, x1, y1] extent the head seeks towards
                     after the tour (the next item, or the return to origin),
                     costed as one more edge so the tour ends near it.

    Two move kinds, both candidate-driven from k-nearest endpoint lists so a
    pass is near-linear. 2-opt: reversing a stretch of the tour flips each
    segment in it, which time-reverses the seeks inside the stretch (their
    cost is unchanged) and only rewires its two boundary seeks. Or-opt:
    relocating one segment (optionally flipped) next to a nearby one, which
    escapes the cost-neutral plateaus 2-opt cannot cross, like leaving a
    split contour half-done for the return trip. Greedy nearest-neighbor
    tours lose most of their excess seek time to a handful of such moves.
    Raster scanline endpoints stack in near-identical columns, so the
    candidate lists need a generous k to contain the useful reconnections: k
    defaults to the segment count, capped so the lists stay small in memory.
    The time budget bounds the improvement sweeps on top of that.
    """
    n = len(tour)
    if n < 2:
        return
    if k is None:
        k = min(len(starts), 128)
    points = []
    for i in range(len(starts)):
        points.append(starts[i][:2])
        points.append(ends[i][:2])
    entry_dirs, exit_dirs = dirs if dirs else (None, None)
    nbrs = _knn_ids(points, k)
    start = start[:2]
    start_nbrs = sorted(range(len(points)), key=lambda j: d2(start, points[j]))[:k]
    pos = {}
    for t in range(n):
        pos[tour[t][0]] = t

    def neg(d):
        return None if d is None else (-d[0], -d[1])

    def entry(t):
        i, rev = tour[t]
        return points[2 * i + 1] if rev else points[2 * i]

    def exit_(t):
        i, rev = tour[t]
        return points[2 * i] if rev else points[2 * i + 1]

    def entry_dir(t):
        if not dirs:
            return None
        i, rev = tour[t]
        return neg(exit_dirs[i]) if rev else entry_dirs[i]

    def exit_dir(t):
        if not dirs:
            return None
        i, rev = tour[t]
        return neg(entry_dirs[i]) if rev else exit_dirs[i]

    cost = seek_cost(seekrate, feedrate)

    def end_cost(p, d):
        towards = [
            min(max(p[0], end_rect[0]), end_rect[2]),
            min(max(p[1], end_rect[1]), end_rect[3]),
        ]
        return cost(p, d, towards, None)

    def exit_state(t):
        if t < 0:
            return start, None
        return exit_(t), exit_dir(t)

    def link(a, b):
        # seek cost leaving position a for position b, -1 is the tour start
        # and n the end target (free when there is none)
        p, d = exit_state(a)
        if b >= n:
            return end_cost(p, d) if end_rect is not None else 0.0
        return cost(p, d, entry(b), entry_dir(b))

    # segments probed without finding a move sleep until a move lands nearby
    dont_look = set()

    def wake(positions):
        for t in positions:
            if 0 <= t < n:
                dont_look.discard(tour[t][0])

    def relocate(i):
        # Or-opt: move the segment at position i to just after a nearby one,
        # optionally flipped. Escapes plateaus 2-opt reversals cannot cross.
        seg = tour[i][0]
        rem = link(i - 1, i + 1) - link(i - 1, i) - link(i, i + 1)
        tried = set()
        for e in nbrs[2 * seg] + nbrs[2 * seg + 1]:
            j = pos.get(e // 2)
            if j is None or j == i or j == i - 1 or j in tried:
                continue
            tried.add(j)
            base = link(j, j + 1)
            pj, dj = exit_state(j)
            for flip in (False, True):
                if flip:
                    e_pt, e_dir = exit_(i), neg(exit_dir(i))
                    x_pt, x_dir = entry(i), neg(entry_dir(i))
                else:
                    e_pt, e_dir = entry(i), entry_dir(i)
                    x_pt, x_dir = exit_(i), exit_dir(i)
                ins = cost(pj, dj, e_pt, e_dir) - base
                if j + 1 >= n:
                    ins += end_cost(x_pt, x_dir) if end_rect is not None else 0.0
                else:
                    ins += cost(x_pt, x_dir, entry(j + 1), entry_dir(j + 1))
                if rem + ins < -1e-9:
                    wake((i - 1, i, i + 1, j, j + 1))
                    elem = tour.pop(i)
                    if flip:
                        elem = [elem[0], not elem[1]]
                    tour.insert(j + 1 if j < i else j, elem)
                    pos.clear()
                    for t, (s, _r) in enumerate(tour):
                        pos[s] = t
                    dont_look.discard(elem[0])
                    return True
        return False

    # 2-opt sweeps converge first, the pricier Or-opt relocations then run
    # on what 2-opt alone cannot fix
    use_oropt = False
    deadline = time.monotonic() + time_budget
    for _pass in range(max_passes):
        improved = False
        for i in range(n):
            if time.monotonic() > deadline:
                return
            if tour[i][0] in dont_look:
                continue
            if i == 0:
                prev = start
                prevdir = None
                cand = start_nbrs
            else:
                previdx, prevrev = tour[i - 1]
                prev = exit_(i - 1)
                prevdir = exit_dir(i - 1)
                cand = nbrs[2 * previdx if prevrev else 2 * previdx + 1]
            t_prev_entry = cost(prev, prevdir, entry(i), entry_dir(i))
            applied = False
            tried = set()
            for e in cand:
                j = pos.get(e // 2)
                if j is None or j < i or j in tried:
                    continue
                tried.add(j)
                # reversal makes segment j's exit the new stretch entry and
                # segment i's entry the new stretch exit, with flipped dirs
                delta = cost(prev, prevdir, exit_(j), neg(exit_dir(j))) - t_prev_entry
                if j + 1 < n:
                    delta += cost(entry(i), neg(entry_dir(i)), entry(j + 1), entry_dir(j + 1))
                    delta -= cost(exit_(j), exit_dir(j), entry(j + 1), entry_dir(j + 1))
                elif end_rect is not None:
                    delta += end_cost(entry(i), neg(entry_dir(i)))
                    delta -= end_cost(exit_(j), exit_dir(j))
                if delta < -1e-9:
                    wake((i - 1, i, i + 1, j, j + 1))
                    tour[i : j + 1] = [[s, not r] for s, r in reversed(tour[i : j + 1])]
                    for t in range(i, j + 1):
                        pos[tour[t][0]] = t
                    applied = True
                    break
            if not applied and use_oropt:
                applied = relocate(i)
            if applied:
                improved = True
            else:
                dont_look.add(tour[i][0])
        if not improved:
            if use_oropt:
                break
            use_oropt = True
            dont_look.clear()


def sort_by_seektime(path, start=None):
    if start is None:
        start = [0.0, 0.0]
    path_unsorted = []
    tree = kdtree.Tree(2)
    for i in range(len(path)):
        pathseg = path[i]
        # copy, so we can place the result in path
        path_unsorted.append(pathseg)
        # populate kdtree
        # seek distance is planar, so only x and y feed the tree
        # (dba vertices may carry a z coordinate)
        tree.insert(pathseg[0][:2], (i, False))  # startpoint, data
        tree.insert(pathseg[-1][:2], (i, True))  # endpoint, data

    # sort by proximity, greedy
    endpoint = start
    tour = []
    usedIdxs = {}
    for _p in range(2 * len(path_unsorted)):
        node, distsq = tree.nearest(endpoint[:2], checkempty=True)
        i, rev = node.data
        node.data = None
        if i not in usedIdxs:
            tour.append([i, rev])
            endpoint = path_unsorted[i][0] if rev else path_unsorted[i][-1]
            usedIdxs[i] = True

    # untangle greedy crossings, minimizing planner-model seek time
    improve_seek_order(
        [seg[0] for seg in path_unsorted],
        [seg[-1] for seg in path_unsorted],
        tour,
        start,
        dirs=polyline_dirs(path_unsorted),
    )
    # closed contours are free to be entered anywhere along the loop
    rotate_closed_entries(path_unsorted, tour, start)

    for t, (i, rev) in enumerate(tour):
        path[t] = path_unsorted[i]
        if rev:
            path[t].reverse()


def remove_waypoints(path):
    # will remove all lead-in and lead-out points
    inds = []
    for i in range(len(path)):
        if len(path[i]) == 1:
            inds.append(i)
    for i in inds[::-1]:
        del path[i]


def reverse_path(path):
    # group fill segments by start-point y, keeping original order per level
    by_y = {}
    for seg in path.copy():
        by_y.setdefault(seg[0][1], []).append(seg)
    path_idx = 0
    for y in sorted(by_y):
        for seg in reversed(by_y[y]):
            path[path_idx] = seg[::-1]
            path_idx += 1


def bidirectionalize_fill(path):
    # group fill segments by start-point y, keeping original order per level
    by_y = {}
    for seg in path.copy():
        by_y.setdefault(seg[0][1], []).append(seg)
    path_idx = 0
    for i, y in enumerate(sorted(by_y)):
        if i % 2 == 0:  # keep even-line passes forward
            for seg in by_y[y]:
                path[path_idx] = seg
                path_idx += 1
        else:  # reverse odd-line passes
            for seg in reversed(by_y[y]):
                path[path_idx] = seg[::-1]
                path_idx += 1


def fill_optimize(path, tolerance):
    tolerance2 = tolerance**2
    epsilon2 = (0.1 * tolerance) ** 2
    connect_segments(path, epsilon2)
    simplify_all(path, tolerance2)
    bidirectionalize_fill(path)


def optimize(path, tolerance):
    tolerance2 = tolerance**2
    epsilon2 = (0.1 * tolerance) ** 2
    connect_segments(path, epsilon2)
    simplify_all(path, tolerance2)
    remove_waypoints(path)
    sort_by_seektime(path)
