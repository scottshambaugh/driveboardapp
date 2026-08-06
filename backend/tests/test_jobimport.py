"""End-to-end tests for the job import pipeline (dba/svg/dxf/gcode).

These exercise the real parsers against the repository's own fixture files
(``library/*.dba`` and ``backend/testjobs/*``) so the full conversion path
is covered without any hardware.
"""

import base64
import glob
import io
import json
import os

import jobimport
import pytest
from jobimport.gcode_reader import GcodeReader
from PIL import Image

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
# svg raster placement
#
# The engraver can only run axis-aligned images, so the importer has to bake
# any rotation, skew or mirroring in the transform into the pixel data itself
# and report the resulting upright mm box.
# ---------------------------------------------------------------------------

R = (255, 0, 0, 255)
G = (0, 255, 0, 255)
B = (0, 0, 255, 255)
K = (0, 0, 0, 255)
MARKER = ((R, G), (B, K))  # a 2x2 image whose every corner is distinguishable


def _data_uri(img, fmt="PNG", mime="png"):
    """Base64 data URI holding `img` encoded as `fmt` and labelled `mime`."""
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return f"data:image/{mime};base64," + base64.b64encode(buf.getvalue()).decode()


def _png_data_uri(rows):
    """Base64 PNG data URI for `rows`, given as rows of RGBA tuples."""
    img = Image.new("RGBA", (len(rows[0]), len(rows)))
    img.putdata([px for row in rows for px in row])
    return _data_uri(img)


def _raster_svg(transform="", rows=MARKER, uri=None):
    """A 100x100mm svg holding one 40x20 image at (10, 20). The viewBox matches
    the page size, so one svg user unit is exactly one mm."""
    return (
        '<?xml version="1.0"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="100mm" height="100mm" viewBox="0 0 100 100">'
        f'<image x="10" y="20" width="40" height="20" transform="{transform}" '
        f'xlink:href="{uri or _png_data_uri(rows)}"/>'
        "</svg>"
    )


def _import_raster(transform="", rows=MARKER, uri=None):
    """Convert the fixture svg, returning (image def, decoded PIL image)."""
    job = jobimport.convert(_raster_svg(transform, rows, uri), optimize=False)
    defs = [d for d in job["defs"] if d["kind"] == "image"]
    assert len(defs) == 1, "expected exactly one image def"
    _, b64 = defs[0]["data"].split(",", 1)
    return defs[0], Image.open(io.BytesIO(base64.b64decode(b64)))


def _pixels(img):
    """The image as a list of rows of RGBA tuples."""
    img = img.convert("RGBA")
    data = list(img.getdata())
    return [tuple(data[y * img.width : (y + 1) * img.width]) for y in range(img.height)]


@pytest.mark.parametrize(
    "transform,pos,size,pixels",
    [
        ("", [10.0, 20.0], [40.0, 20.0], [(R, G), (B, K)]),
        ("scale(2,3)", [20.0, 60.0], [80.0, 60.0], [(R, G), (B, K)]),
        # a quarter turn lands the 40x20 image as a 20x40 one
        ("translate(60,0) rotate(90)", [20.0, 10.0], [20.0, 40.0], [(B, R), (K, G)]),
        ("translate(0,60) rotate(-90)", [20.0, 10.0], [20.0, 40.0], [(G, K), (R, B)]),
        # a half turn is upright again, but only within floating point noise,
        # so this also covers the tolerance on the axis-aligned test
        ("translate(100,100) rotate(180)", [50.0, 60.0], [40.0, 20.0], [(K, B), (G, R)]),
        ("translate(100,0) scale(-1,1)", [50.0, 20.0], [40.0, 20.0], [(G, R), (K, B)]),
        ("translate(0,100) scale(1,-1)", [10.0, 60.0], [40.0, 20.0], [(B, K), (R, G)]),
    ],
    ids=["upright", "scaled", "cw", "ccw", "half-turn", "mirror-h", "mirror-v"],
)
def test_raster_axis_aligned_placement(transform, pos, size, pixels):
    """Upright, quarter-turned and mirrored images keep their exact pixels and
    come back as an upright mm box.

    Regression: reading the scale off a rotation matrix as (m[0], m[3]) gave a
    quarter-turned image a zero size, which hid it from the preview and
    collapsed both work-area bounds checks onto a single point.
    """
    def_, img = _import_raster(transform)
    assert def_["pos"] == pytest.approx(pos)
    assert def_["size"] == pytest.approx(size)
    assert _pixels(img) == pixels


def test_raster_upright_data_passes_through():
    # nothing to reorient, so the pixels must not be re-encoded
    def_, _ = _import_raster()
    assert def_["data"] == _png_data_uri(MARKER)


@pytest.mark.parametrize(
    "transform,pos,size,blank_corner",
    [
        # 45 degrees grows the box to the rotated diagonal on both axes
        ("translate(30,0) rotate(45)", [8.7868, 21.2132], [42.4264, 42.4264], (0, 0)),
        # a shear widens the box without moving its top edge
        ("skewX(45)", [30.0, 20.0], [60.0, 20.0], (-1, 0)),
    ],
    ids=["rotate45", "skew"],
)
def test_raster_arbitrary_transform_resampled(transform, pos, size, blank_corner):
    """Anything that is not a quarter turn gets resampled onto its bounding
    box, leaving the corners it does not cover transparent."""
    def_, img = _import_raster(transform)
    assert def_["pos"] == pytest.approx(pos, abs=1e-3)
    assert def_["size"] == pytest.approx(size, abs=1e-3)
    px = _pixels(img)
    assert px[blank_corner[0]][blank_corner[1]][3] == 0, "uncovered corner should be blank"
    assert px[len(px) // 2][len(px[0]) // 2][3] == 255, "the centre should be covered"


def test_raster_transparency_survives_a_non_png_source():
    """Reorienting a gif must not re-encode it as JPEG.

    JPEG carries no alpha channel, so flattening a transparent image into one
    turns every clear pixel black, which the engraver burns at full power. It
    also left the data URI claiming a mime type its bytes no longer were.
    """
    # a 2x2 gif, left column transparent and right column opaque black
    src = Image.new("P", (2, 2), 0)
    src.putpalette([255, 255, 255, 0, 0, 0])
    src.putpixel((1, 0), 1)
    src.putpixel((1, 1), 1)
    src.info["transparency"] = 0
    uri = _data_uri(src, fmt="GIF", mime="gif")

    def_, img = _import_raster("translate(100,0) scale(-1,1)", uri=uri)
    assert def_["data"].startswith("data:image/png;base64,")
    assert img.format == "PNG"
    # the mirror swapped the columns and the clear one is still clear
    assert [[px[3] for px in row] for row in _pixels(img)] == [[255, 0], [255, 0]]


@pytest.mark.parametrize(
    "fmt,mime,stays",
    [
        ("PNG", "png", True),
        ("JPEG", "jpeg", True),
        ("GIF", "gif", True),
        ("BMP", "bmp", True),
        ("WEBP", "webp", True),
        ("ICO", "x-icon", True),
        # Pillow reads these but no browser renders them, so the preview would
        # never load and the job would sit there waiting for it
        ("TIFF", "tiff", False),
        ("PPM", "x-portable-pixmap", False),
        ("PCX", "x-pcx", False),
        ("TGA", "x-tga", False),
        ("JPEG2000", "jp2", False),
        ("AVIF", "avif", False),
    ],
)
def test_raster_normalized_to_a_displayable_format(fmt, mime, stays):
    src = Image.new("RGB", (32, 32), (255, 255, 255))
    src.putpixel((0, 0), (0, 0, 0))
    def_, img = _import_raster(uri=_data_uri(src, fmt=fmt, mime=mime))
    if stays:
        assert def_["data"].startswith(f"data:image/{mime};base64,")
    else:
        assert def_["data"].startswith("data:image/png;base64,")
        assert img.format == "PNG"
    assert img.size == (32, 32)


def test_linked_image_is_skipped():
    """A linked image cannot be resolved, so it must not leave a phantom def.

    The backend only ever receives the svg content, never the path it was read
    from, so a relative href has nothing to resolve against. A def carrying no
    data would still claim its pos and size during work-area validation.
    """
    job = jobimport.convert(_raster_svg(uri="linked.png"), optimize=False)
    assert [d for d in job["defs"] if d["kind"] == "image"] == []


def test_raster_opaque_jpeg_stays_jpeg():
    # no alpha to lose here, so a photo is not inflated into a PNG
    src = Image.new("RGB", (8, 8), (255, 255, 255))
    for y in range(8):
        for x in range(4):
            src.putpixel((x, y), (0, 0, 0))  # black left half
    def_, img = _import_raster(
        "translate(100,0) scale(-1,1)", uri=_data_uri(src, fmt="JPEG", mime="jpeg")
    )
    assert def_["data"].startswith("data:image/jpeg;base64,")
    assert img.format == "JPEG"
    # the mirror moved the black half over to the right
    px = _pixels(img)
    assert px[0][0][0] > 128, "left column should now be white"
    assert px[0][-1][0] < 128, "right column should now be black"


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
