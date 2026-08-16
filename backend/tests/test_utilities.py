"""Unit tests for jobimport.utilities matrix/parsing helpers."""

import math

import pytest
from jobimport import utilities


def test_parse_floats_mixed_separators():
    assert utilities.parseFloats("1,2 3;4") == [1.0, 2.0, 3.0, 4.0]
    assert utilities.parseFloats("-1.5 2e2") == [-1.5, 200.0]


def test_parse_floats_empty():
    assert utilities.parseFloats("nonumbers") == []


@pytest.mark.parametrize(
    "text,expected",
    [
        (".5,.5", [0.5, 0.5]),  # a decimal point with no leading digit
        ("-.5 -.5", [-0.5, -0.5]),
        ("5.,3", [5.0, 3.0]),  # and with no trailing digit
        ("+10,+20", [10.0, 20.0]),  # an explicit plus
        ("1e2,3", [100.0, 3.0]),  # exponents in either case, signed or not
        ("1E2,3", [100.0, 3.0]),
        ("1e+2,3", [100.0, 3.0]),
        ("1E-2,3", [0.01, 3.0]),
        ("10-5", [10.0, -5.0]),  # a sign standing in for a separator
    ],
)
def test_parse_floats_reads_the_whole_svg_number_grammar(text, expected):
    """Writers that shorten their output use every corner of the grammar, and
    a number read short is a coordinate in the wrong place."""
    assert utilities.parseFloats(text) == expected


def test_parse_scalar_without_leading_digit():
    assert utilities.parseScalar(".5in") == (0.5, "in")


def test_parse_scalar_with_unit():
    assert utilities.parseScalar("12mm") == (12.0, "mm")
    assert utilities.parseScalar("3.5in") == (3.5, "in")


def test_parse_scalar_without_unit():
    assert utilities.parseScalar("42") == (42.0, "")


def test_parse_scalar_missing_number():
    assert utilities.parseScalar("auto") == (None, "")


@pytest.mark.parametrize(
    "mat,vec,expected",
    [
        ([1, 0, 0, 1, 0, 0], [3.0, 4.0], [3.0, 4.0]),  # identity
        ([1, 0, 0, 1, 5, 7], [1.0, 2.0], [6.0, 9.0]),  # translation
        ([0, 1, -1, 0, 0, 0], [1.0, 0.0], [0.0, 1.0]),  # 90deg rotation
    ],
)
def test_matrix_apply(mat, vec, expected):
    utilities.matrixApply(mat, vec)
    assert math.isclose(vec[0], expected[0], abs_tol=1e-9)
    assert math.isclose(vec[1], expected[1], abs_tol=1e-9)


def test_matrix_apply_does_not_use_stale_x():
    # Regression: y must be computed from the original x, not the updated one.
    scale_and_skew = [2, 1, 0, 1, 0, 0]
    vec = [3.0, 4.0]
    utilities.matrixApply(scale_and_skew, vec)
    # x' = 2*3 = 6 ; y' = 1*3 + 1*4 = 7 (uses original x=3)
    assert vec == [6.0, 7.0]


def test_matrix_mult_identity():
    identity = [1, 0, 0, 1, 0, 0]
    m = [2, 0, 0, 3, 10, 20]
    assert utilities.matrixMult(identity, m) == m


def test_matrix_mult_composes_translations():
    a = [1, 0, 0, 1, 1, 2]
    b = [1, 0, 0, 1, 3, 4]
    assert utilities.matrixMult(a, b) == [1, 0, 0, 1, 4, 6]


def test_vertex_scale():
    v = [2.0, 3.0]
    utilities.vertexScale(v, 2)
    assert v == [4.0, 6.0]
