"""Unit tests for the raster/vector fill hatching generator."""

from jobimport import fill_generator


def _square(size=10.0):
    return [[[0.0, 0.0], [size, 0.0], [size, size], [0.0, size], [0.0, 0.0]]]


def test_get_bbox():
    paths = [[[1.0, 2.0], [5.0, 2.0]], [[3.0, -1.0], [3.0, 8.0]]]
    minx, miny, maxx, maxy = fill_generator.get_bbox(paths)
    assert (minx, miny, maxx, maxy) == (1.0, -1.0, 5.0, 8.0)


def test_line_intersect_crossing():
    # Horizontal scan line y=5 crossing a vertical segment at x=3.
    res = fill_generator.line_intersect(0, 5, 10, 5, 3, 0, 3, 10)
    assert res["on_seg1"] and res["on_seg2"]
    assert abs(res["x"] - 3.0) < 1e-9
    assert abs(res["y"] - 5.0) < 1e-9


def test_empty_input_returns_empty():
    assert fill_generator.generate_fill_hatching([], pxsize=1.0) == []


def test_nonpositive_pxsize_returns_empty():
    assert fill_generator.generate_fill_hatching(_square(), pxsize=0.0) == []


def test_fill_square_produces_hatching():
    # A non-zero leadin extends scan lines past the boundary so the edge
    # intersections fall strictly inside the scan segment (matches real use).
    lines = fill_generator.generate_fill_hatching(_square(10.0), pxsize=1.0, leadin=2.0)
    assert lines, "expected hatching lines for a filled square"
    # Find the actual fill segments (two-point polylines that aren't lead-in/out).
    segments = [poly for poly in lines if len(poly) == 2]
    assert segments
    for seg in segments:
        (x0, _y0), (x1, _y1) = seg
        # Fill segments span the interior of the 0..10 square.
        assert 0.0 <= x0 <= 10.0
        assert 0.0 <= x1 <= 10.0


def test_finer_pxsize_yields_more_lines():
    coarse = fill_generator.generate_fill_hatching(_square(10.0), pxsize=2.0, leadin=2.0)
    fine = fill_generator.generate_fill_hatching(_square(10.0), pxsize=0.5, leadin=2.0)
    assert len(fine) > len(coarse)
