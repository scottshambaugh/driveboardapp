"""Unit tests for the kd-tree used by the nearest-neighbor path optimizer."""

import math

from jobimport import kdtree


def _brute_force_nearest(points, query):
    best = None
    best_d2 = None
    for p in points:
        d2 = sum((a - b) ** 2 for a, b in zip(p, query))
        if best_d2 is None or d2 < best_d2:
            best, best_d2 = p, d2
    return best, best_d2


def test_empty_tree_returns_none():
    tree = kdtree.Tree(2)
    node, distsq = tree.nearest([0.0, 0.0])
    assert node is None
    assert distsq is None


def test_single_point():
    tree = kdtree.Tree(2)
    tree.insert([1.0, 2.0], data="a")
    node, distsq = tree.nearest([1.0, 2.0])
    assert node.data == "a"
    assert math.isclose(distsq, 0.0, abs_tol=1e-9)


def test_nearest_matches_brute_force():
    points = [
        [0.0, 0.0],
        [10.0, 10.0],
        [5.0, 1.0],
        [3.0, 8.0],
        [-2.0, 4.0],
        [7.0, 7.0],
    ]
    tree = kdtree.Tree(2)
    for i, p in enumerate(points):
        tree.insert(p, data=i)

    for query in ([4.0, 2.0], [9.0, 9.0], [-1.0, 3.0], [6.0, 6.5]):
        node, distsq = tree.nearest(query)
        expected_point, expected_d2 = _brute_force_nearest(points, query)
        assert math.isclose(distsq, expected_d2, abs_tol=1e-9)
        assert list(node.pos[:2]) == expected_point


def test_check_empty_skips_consumed_nodes():
    tree = kdtree.Tree(2)
    n0 = tree.insert([0.0, 0.0], data="zero")
    tree.insert([10.0, 10.0], data="far")
    # Mark the closest node as consumed.
    n0.data = None
    node, _ = tree.nearest([0.1, 0.1], checkempty=True)
    assert node is not None
    assert node.data == "far"
