"""End-to-end tests for the job import pipeline (dba/svg/dxf/gcode).

These exercise the real parsers against the repository's own fixture files
(``library/*.dba`` and ``backend/testjobs/*``) so the full conversion path
is covered without any hardware.
"""

import glob
import json
import os

import jobimport
import pytest
from jobimport.gcode_reader import GcodeReader

# ---------------------------------------------------------------------------
# get_type detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "job,expected",
    [
        ({"defs": []}, "dba"),
        ('   {"defs": []}', "dba"),
        (b'{"defs": []}', "dba"),
        ('<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>', "svg"),
        ("999\nSECTION\n2\nHEADER\n", "dxf"),
        ("G0 X0 Y0\nG1 X10 Y10\n", "gcode"),
    ],
)
def test_get_type(job, expected):
    assert jobimport.get_type(job) == expected


def test_get_type_unknown_raises():
    with pytest.raises(TypeError):
        jobimport.get_type("this is not a recognizable job")


# ---------------------------------------------------------------------------
# dba conversion against the real library
# ---------------------------------------------------------------------------


def _dba_files(library_dir):
    return sorted(glob.glob(os.path.join(library_dir, "*.dba")))


def test_convert_all_library_dba(library_dir):
    files = _dba_files(library_dir)
    assert files, "expected .dba fixtures in library/"
    for path in files:
        with open(path) as fp:
            raw = fp.read()
        assert jobimport.get_type(raw) == "dba", path
        job = jobimport.convert(raw)
        assert isinstance(job, dict), path
        assert "defs" in job, path
        # Optimized dba jobs get an 'optimized' marker in head.
        assert job["head"].get("optimized") is not None, path


def test_convert_dba_idempotent_structure(library_dir):
    # Converting an already-parsed dict and its JSON string should agree.
    path = os.path.join(library_dir, "lines.dba")
    with open(path) as fp:
        raw = fp.read()
    from_str = jobimport.convert(raw, optimize=False)
    from_dict = jobimport.convert(json.loads(raw), optimize=False)
    assert len(from_str["defs"]) == len(from_dict["defs"])


# ---------------------------------------------------------------------------
# svg conversion against testjobs fixtures
# ---------------------------------------------------------------------------


def test_convert_svg_fixtures(testjobs_dir):
    svgs = sorted(glob.glob(os.path.join(testjobs_dir, "*.svg")))
    assert svgs, "expected svg fixtures in testjobs/"
    for path in svgs:
        with open(path) as fp:
            raw = fp.read()
        if jobimport.get_type(raw) != "svg":
            continue
        job = jobimport.convert(raw)
        assert "defs" in job
        assert "items" in job


def test_convert_svg_has_geometry(testjobs_dir):
    path = os.path.join(testjobs_dir, "full-bed.svg")
    with open(path) as fp:
        raw = fp.read()
    job = jobimport.convert(raw)
    assert len(job["defs"]) >= 1
    # At least one path def with coordinate data.
    path_defs = [d for d in job["defs"] if d["kind"] == "path"]
    assert path_defs
    assert any(d["data"] for d in path_defs)


# ---------------------------------------------------------------------------
# dxf conversion
# ---------------------------------------------------------------------------


def test_convert_dxf_fixture(testjobs_dir):
    dxfs = sorted(glob.glob(os.path.join(testjobs_dir, "*.dxf")))
    if not dxfs:
        pytest.skip("no dxf fixtures available")
    for path in dxfs:
        with open(path) as fp:
            raw = fp.read()
        assert jobimport.get_type(raw) == "dxf", path
        job = jobimport.convert(raw)
        assert "defs" in job


# ---------------------------------------------------------------------------
# gcode conversion
# ---------------------------------------------------------------------------


def test_gcode_reader_minimal():
    gcode = "\n".join(
        [
            "T1 M6",
            "G0 X0 Y0",
            "G1 X10 Y10 F1000",
            "G1 X10 Y0",
        ]
    )
    job = GcodeReader().parse(gcode)
    assert job["head"]["kind"] == "mill"
    assert job["defs"], "expected at least one tool pass"
    # The motion commands should have populated the path of the first pass.
    assert job["defs"][0]["tool"] == "T1"
    assert job["defs"][0]["data"], "expected motion actions recorded"


def test_convert_gcode_via_dispatch():
    gcode = "T1 M6\nG0 X0 Y0\nG1 X5 Y5 F800\n"
    job = jobimport.convert(gcode)
    assert "defs" in job
    assert job["head"]["kind"] == "mill"


# ---------------------------------------------------------------------------
# alignment matrix
# ---------------------------------------------------------------------------


def test_apply_alignment_matrix_translation():
    job = {
        "defs": [
            {"kind": "path", "data": [[[0.0, 0.0], [1.0, 1.0]]]},
        ]
    }
    # 3x3 matrix translating by (10, 20).
    matrix = [
        [1, 0, 10],
        [0, 1, 20],
        [0, 0, 1],
    ]
    jobimport.apply_alignment_matrix(job, matrix)
    assert job["defs"][0]["data"][0][0] == [10.0, 20.0]
    assert job["defs"][0]["data"][0][1] == [11.0, 21.0]
