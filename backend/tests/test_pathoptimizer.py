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


def _seek_length(starts, ends, tour, start):
    import math

    total = 0.0
    pos = start
    for i, rev in tour:
        entry, exit_ = (ends[i], starts[i]) if rev else (starts[i], ends[i])
        total += math.dist(pos, entry)
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
    before = _seek_length(starts, ends, tour, [0.0, 0.0])
    pathoptimizer.improve_seek_order(starts, ends, tour, [0.0, 0.0])
    after = _seek_length(starts, ends, tour, [0.0, 0.0])
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
        before = _seek_length(starts, ends, tour, start)
        pathoptimizer.improve_seek_order(starts, ends, tour, start)
        after = _seek_length(starts, ends, tour, start)
        assert after <= before + 1e-9
        assert sorted(i for i, _rev in tour) == list(range(n))
        assert math.isfinite(after)


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
