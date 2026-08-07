"""Unit tests for jobimport.pathoptimizer (geometry simplification/ordering)."""

from jobimport import pathoptimizer


def _point_count(path):
    return sum(len(poly) for poly in path)


def test_optimize_simplifies_collinear_points():
    # A straight, densely-sampled line should collapse to its endpoints.
    line = [[float(i), 0.0] for i in range(11)]
    path = [line]
    pathoptimizer.optimize(path, tolerance=0.1)
    assert len(path) == 1
    assert _point_count(path) == 2
    assert path[0][0] == [0.0, 0.0]
    assert path[0][-1] == [10.0, 0.0]


def test_optimize_keeps_corner():
    # An L-shape must keep its corner point.
    corner = [[0.0, 0.0], [5.0, 0.0], [5.0, 5.0]]
    path = [corner]
    pathoptimizer.optimize(path, tolerance=0.1)
    flat = [pt for poly in path for pt in poly]
    assert [5.0, 0.0] in flat


def test_optimize_does_not_drop_distinct_polylines():
    p1 = [[0.0, 0.0], [1.0, 0.0]]
    p2 = [[10.0, 10.0], [11.0, 10.0]]
    path = [p1, p2]
    pathoptimizer.optimize(path, tolerance=0.01)
    assert len(path) == 2


def test_reverse_path_reverses_each_segment():
    path = [
        [[0.0, 0.0], [5.0, 0.0]],
        [[0.0, 1.0], [5.0, 1.0]],
    ]
    pathoptimizer.reverse_path(path)
    # Every segment should now run high-x -> low-x.
    for seg in path:
        assert seg[0][0] >= seg[-1][0]


def test_fill_optimize_preserves_coverage():
    # Bidirectional fill keeps the same number of segments, alternating dir.
    path = [
        [[0.0, 0.0], [5.0, 0.0]],
        [[0.0, 1.0], [5.0, 1.0]],
        [[0.0, 2.0], [5.0, 2.0]],
    ]
    before = len(path)
    pathoptimizer.fill_optimize(path, tolerance=0.01)
    assert len(path) == before


def test_connect_segments_joins_touching_ends():
    # Two segments sharing an endpoint should be joined into one polyline.
    path = [
        [[0.0, 0.0], [1.0, 0.0]],
        [[1.0, 0.0], [2.0, 0.0]],
    ]
    epsilon2 = (0.1 * 0.01) ** 2
    pathoptimizer.connect_segments(path, epsilon2)
    assert len(path) == 1


def test_optimize_handles_3d_vertices():
    # dba paths may carry [x, y, z] vertices, seek sorting must stay planar
    path = [
        [[0.0, 10.0, 0.0], [20.0, 10.0, 0.0]],
        [[5.0, 0.0, 0.0], [5.0, 30.0, 0.0]],
    ]
    pathoptimizer.optimize(path, tolerance=0.01)
    assert len(path) == 2
    assert all(len(vertex) == 3 for seg in path for vertex in seg)


def test_sort_by_seektime_orders_by_proximity():
    # nearest segment end from the origin comes first, reversing as needed
    path = [
        [[100.0, 100.0], [50.0, 50.0]],
        [[40.0, 40.0], [1.0, 1.0]],
    ]
    pathoptimizer.sort_by_seektime(path)
    assert path[0] == [[1.0, 1.0], [40.0, 40.0]]
    assert path[1] == [[50.0, 50.0], [100.0, 100.0]]


def _seek_cost(starts, ends, tour, start):
    # planner-model seek time with unknown directions, the optimizer's metric
    # when no dirs are passed
    total = 0.0
    pos = start
    for i, rev in tour:
        entry, exit_ = (ends[i], starts[i]) if rev else (starts[i], ends[i])
        total += pathoptimizer.seek_time(pos, None, entry, None)
        pos = exit_
    return total


def test_improve_seek_order_flips_a_backwards_segment():
    starts = [[0.0, 0.0], [20.0, 0.0]]
    ends = [[10.0, 0.0], [11.0, 0.0]]
    tour = [[0, False], [1, False]]
    pathoptimizer.improve_seek_order(starts, ends, tour, [0.0, 0.0])
    # entering the second segment at x=11 beats seeking out to x=20
    assert tour == [[0, False], [1, True]]


def test_improve_seek_order_untangles_a_crossing():
    # two columns of segments, interleaved so the seeks zigzag between them
    starts = [[0.0, 0.0], [100.0, 0.0], [0.0, 1.0], [100.0, 1.0]]
    ends = [[10.0, 0.0], [110.0, 0.0], [10.0, 1.0], [110.0, 1.0]]
    tour = [[0, False], [1, False], [2, False], [3, False]]
    before = _seek_cost(starts, ends, tour, [0.0, 0.0])
    pathoptimizer.improve_seek_order(starts, ends, tour, [0.0, 0.0])
    after = _seek_cost(starts, ends, tour, [0.0, 0.0])
    assert after < before
    assert sorted(i for i, _rev in tour) == [0, 1, 2, 3]
    # both left column segments come before the right column
    order = [i for i, _rev in tour]
    assert order.index(2) < order.index(1)
    assert order.index(2) < order.index(3)


def test_improve_seek_order_never_worsens_random_tours():
    import math
    import random

    rng = random.Random(42)
    for _trial in range(10):
        n = rng.randrange(2, 40)
        starts = [[rng.uniform(0, 100), rng.uniform(0, 100)] for _ in range(n)]
        ends = [[s[0] + rng.uniform(-5, 5), s[1] + rng.uniform(-5, 5)] for s in starts]
        tour = [[i, bool(rng.getrandbits(1))] for i in rng.sample(range(n), n)]
        start = [rng.uniform(0, 100), rng.uniform(0, 100)]
        before = _seek_cost(starts, ends, tour, start)
        pathoptimizer.improve_seek_order(starts, ends, tour, start)
        after = _seek_cost(starts, ends, tour, start)
        assert after <= before + 1e-9
        assert sorted(i for i, _rev in tour) == list(range(n))
        assert math.isfinite(after)


def test_trapezoid_time_matches_closed_forms():
    a = pathoptimizer.ACCEL
    # long move from stop to stop: cruise time plus one full ramp up and down
    t = pathoptimizer._trapezoid_time(100.0, 100.0, a, 0.0, 0.0)
    assert abs(t - (100.0 / 100.0 + 100.0 / a)) < 1e-9
    # short move never reaches cruise: symmetric triangular profile
    t = pathoptimizer._trapezoid_time(1.0, 100.0, a, 0.0, 0.0)
    assert abs(t - 2.0 * (1.0 / a) ** 0.5) < 1e-9


def test_junction_speed_by_angle():
    vcap = 100.0
    straight = pathoptimizer._junction_speed((1.0, 0.0), (1.0, 0.0), vcap)
    corner = pathoptimizer._junction_speed((1.0, 0.0), (0.0, 1.0), vcap)
    reversal = pathoptimizer._junction_speed((1.0, 0.0), (-1.0, 0.0), vcap)
    assert straight == vcap
    assert reversal == 0.0
    assert 0.0 < corner < 5.0  # a 90 degree corner is close to a stop


def test_seek_time_reversal_costs_more_than_continuation():
    # the same 10mm seek, continuing the feed direction vs reversing it
    cont = pathoptimizer.seek_time([0.0, 0.0], (1.0, 0.0), [10.0, 0.0], (1.0, 0.0))
    rev = pathoptimizer.seek_time([10.0, 0.0], (1.0, 0.0), [0.0, 0.0], (-1.0, 0.0))
    assert 0.0 < cont < rev


def test_improve_seek_order_avoids_reversal_over_short_backtrack():
    # continuing in the feed direction to the far end of the next segment
    # beats a shorter seek that forces two full reversals
    starts = [[0.0, 0.0], [12.0, 0.4]]
    ends = [[10.0, 0.0], [2.0, 0.4]]
    dirs = ([(1.0, 0.0), (-1.0, 0.0)], [(1.0, 0.0), (-1.0, 0.0)])
    tour = [[0, False], [1, True]]
    pathoptimizer.improve_seek_order(starts, ends, tour, [0.0, 0.0], dirs=dirs)
    assert tour == [[0, False], [1, False]]


def test_improve_seek_order_ends_near_the_end_rect():
    # two stacked segments: without a terminal target the short backtrack
    # wins, with one just right of the top segment the tour ends there
    starts = [[0.0, 0.0], [0.0, 2.0]]
    ends = [[10.0, 0.0], [10.0, 2.0]]
    home = [10.5, 2.0, 10.5, 2.0]
    tour = [[0, False], [1, False]]
    pathoptimizer.improve_seek_order(starts, ends, tour, [0.0, 0.0])
    assert tour == [[0, False], [1, True]]
    tour = [[0, False], [1, False]]
    pathoptimizer.improve_seek_order(starts, ends, tour, [0.0, 0.0], end_rect=home)
    assert tour == [[0, False], [1, False]]


def test_rotate_closed_entries_picks_the_near_vertex():
    # a closed square stored entering at its far corner re-enters at the near one
    square = [[60.0, 60.0], [50.0, 60.0], [50.0, 50.0], [60.0, 50.0], [60.0, 60.0]]
    polys = [square]
    pathoptimizer.rotate_closed_entries(polys, [[0, False]], [0.0, 0.0], grid=5.0)
    assert square[0] == [50.0, 50.0]
    assert square[-1] == square[0]
    assert len(square) == 5
    assert sorted(map(tuple, square[:-1])) == [
        (50.0, 50.0),
        (50.0, 60.0),
        (60.0, 50.0),
        (60.0, 60.0),
    ]


def test_rotate_closed_entries_respects_the_grid():
    # a densely sampled circle gets entered near the ideal point without
    # evaluating every vertex, so the entry lands within a grid step of it
    import math

    n = 360
    circle = [
        [50.0 + 20.0 * math.cos(2 * math.pi * i / n), 20.0 * math.sin(2 * math.pi * i / n)]
        for i in range(n)
    ]
    circle.append(circle[0][:])
    pathoptimizer.rotate_closed_entries([circle], [[0, False]], [0.0, 0.0], grid=10.0)
    # nearest point to the origin is (30, 0)
    assert math.dist(circle[0], [30.0, 0.0]) <= 10.0
    assert circle[-1] == circle[0]
    assert len(circle) == n + 1


def test_rotate_closed_entries_leaves_open_contours_alone():
    # a 0.5mm gap marks a deliberately open contour, rotating it would burn
    # a bridge across the gap
    arc = [[60.0, 60.0], [50.0, 60.0], [50.0, 50.0], [60.0, 50.0], [60.0, 59.5]]
    ref = [row[:] for row in arc]
    pathoptimizer.rotate_closed_entries([arc], [[0, False]], [0.0, 0.0], grid=5.0)
    assert arc == ref


def test_split_closed_paths_splits_into_shared_vertex_halves():
    square = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]
    open_line = [[50.0, 50.0], [60.0, 50.0]]
    out, flags = pathoptimizer.split_closed_paths([square, open_line], grid=10.0)
    assert open_line in out
    assert flags == [True, True, False]
    half_a, half_b = out[0], out[1]
    # halves meet at the half-perimeter vertex and close the loop together
    assert half_a == [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]]
    assert half_b == [[10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]


def test_split_closed_paths_keeps_small_contours_whole():
    small = [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0], [0.0, 0.0]]
    assert pathoptimizer.split_closed_paths([small], grid=10.0) == ([small], [False])


def _octagon(cx, cy, r):
    import math

    return [
        [
            round(cx + r * math.cos(math.tau * i / 8.0), 3),
            round(cy + r * math.sin(math.tau * i / 8.0), 3),
        ]
        for i in range(9)
    ]


def test_split_closed_paths_neighbor_cuts_face_the_gaps():
    # a row of contours cut at the vertices facing their neighbors, so an
    # out-and-back interleave hops only the gaps between them
    polys = [_octagon(60.0, 60.0, 20.0), _octagon(110.0, 60.0, 20.0), _octagon(160.0, 60.0, 20.0)]
    out, flags = pathoptimizer.split_closed_paths(polys, start=[0.0, 0.0], neighbors=True)
    assert flags == [True] * 6
    mid_ends = {tuple(out[2][0]), tuple(out[2][-1])}
    assert mid_ends == {(90.0, 60.0), (130.0, 60.0)}


def test_improve_seek_order_returns_the_tour_time():
    starts = [[0.0, 0.0], [20.0, 0.0]]
    ends = [[10.0, 0.0], [30.0, 0.0]]
    tour = [[0, False], [1, False]]
    t = pathoptimizer.improve_seek_order(starts, ends, tour, [0.0, 0.0])
    assert t == pathoptimizer.seek_time([10.0, 0.0], None, [20.0, 0.0], None)


def test_knn_ids_matches_brute_force():
    import random

    rng = random.Random(7)
    points = [[rng.uniform(0, 50), rng.uniform(0, 50)] for _ in range(60)]
    k = 5
    got = pathoptimizer._knn_ids(points, k)
    for i, p in enumerate(points):
        want = sorted(
            (j for j in range(len(points)) if j != i),
            key=lambda j: (p[0] - points[j][0]) ** 2 + (p[1] - points[j][1]) ** 2,
        )[:k]
        assert got[i] == want
