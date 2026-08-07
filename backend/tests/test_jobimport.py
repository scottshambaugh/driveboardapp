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
from jobimport.svg_text_converter import convert_text_to_paths
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
    come back as an upright mm box. A zero size here hides the image from the
    preview and collapses both work-area bounds checks onto one point."""
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


def test_raster_clipped_copies_share_source():
    """Copies of one image keep a common source id even when clipping crops
    their data apart, and the original rides along once for previews."""
    uri = _png_data_uri(MARKER)
    svg = (
        '<?xml version="1.0"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="100mm" height="100mm" viewBox="0 0 100 100">'
        '<defs><clipPath id="c1"><rect x="10" y="50" width="20" height="10"/></clipPath></defs>'
        f'<image x="10" y="20" width="40" height="20" xlink:href="{uri}"/>'
        f'<image x="10" y="50" width="40" height="20" clip-path="url(#c1)" xlink:href="{uri}"/>'
        "</svg>"
    )
    job = jobimport.convert(svg, optimize=False)
    defs = [d for d in job["defs"] if d["kind"] == "image"]
    assert len(defs) == 2
    full, clipped = defs
    assert full["source"] == clipped["source"]
    assert full["data"] == uri
    assert clipped["data"] != uri
    assert clipped["pos"] == pytest.approx([10.0, 50.0])
    assert clipped["size"] == pytest.approx([20.0, 10.0])
    # the original is kept once, keyed by the shared source id
    assert job["sources"] == {full["source"]: uri}


def test_raster_unclipped_has_no_sources():
    # nothing was cropped, so there is no original to carry along
    job = jobimport.convert(_raster_svg(), optimize=False)
    assert "sources" not in job


def _group_images_like_frontend(job):
    """Group image items by the key jobhandler.groupIdenticalImages uses:
    the source id, falling back to the raster data when there is none."""
    groups = {}
    for i, item in enumerate(job["items"]):
        def_ = job["defs"][item["def"]]
        if def_["kind"] != "image":
            continue
        key = def_.get("source") or def_["data"]
        groups.setdefault(key, []).append(i)
    return groups


def _copies_svg(uri, second_uri=None):
    """One image placed five times: three clipped to different widths, one
    plain, and one turned a quarter. Every copy but the plain one comes out
    with different bytes, so only the source id can hold them together."""
    clips = "".join(
        f'<clipPath id="c{i}"><rect x="{10 + 20 * i}" y="10" width="{5 + i}" height="8"/></clipPath>'
        for i in range(3)
    )
    clipped = "".join(
        f'<image x="{10 + 20 * i}" y="10" width="16" height="8" '
        f'clip-path="url(#c{i})" xlink:href="{uri}"/>'
        for i in range(3)
    )
    other = (
        f'<image x="10" y="80" width="16" height="8" xlink:href="{second_uri}"/>'
        if second_uri
        else ""
    )
    return (
        '<?xml version="1.0"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="100mm" height="100mm" viewBox="0 0 100 100">'
        f"<defs>{clips}</defs>"
        f"{clipped}"
        f'<image x="10" y="60" width="16" height="8" xlink:href="{uri}"/>'
        f'<image x="10" y="20" width="40" height="20" transform="translate(60,0) rotate(90)" '
        f'xlink:href="{uri}"/>'
        f"{other}"
        "</svg>"
    )


def test_clipped_copies_group_as_one_through_the_load_round_trip():
    """The path a browser import actually takes: the gzip upload hands convert
    a bytes job, /load writes it as json and /get reads it back, then the
    frontend keys its passes entries off what survived."""
    uri = _png_data_uri(MARKER)
    job = jobimport.convert(_copies_svg(uri).encode("utf-8"), optimize=False)
    job = json.loads(json.dumps(job))  # what /load stores and /get returns

    defs = [d for d in job["defs"] if d["kind"] == "image"]
    assert len(defs) == 5
    # clipping and the quarter turn leave the copies genuinely different, so a
    # frontend keying on the data alone would split them into several entries
    assert len({d["data"] for d in defs}) > 1
    assert all(d.get("source") for d in defs)

    groups = _group_images_like_frontend(job)
    assert len(groups) == 1, "identical images must collapse to one pass entry"
    assert len(next(iter(groups.values()))) == 5
    # the uncropped original rides along once, for the entry's thumbnail
    assert list(job["sources"]) == [defs[0]["source"]]


def test_different_images_stay_separate_entries():
    # two unrelated images must never share a pass entry
    uri = _png_data_uri(MARKER)
    other = _png_data_uri(((G, B), (K, R)))
    job = jobimport.convert(_copies_svg(uri, second_uri=other), optimize=False)
    job = json.loads(json.dumps(job))

    groups = _group_images_like_frontend(job)
    assert len(groups) == 2
    assert sorted(len(v) for v in groups.values()) == [1, 5]


def test_raster_transparency_survives_a_non_png_source():
    """Reorienting a gif must not re-encode it as JPEG. JPEG carries no alpha
    channel, so flattening a transparent image into one turns every clear pixel
    black, which the engraver burns at full power."""
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
        # Pillow reads these but no browser renders them
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
    """The backend receives the svg content, never the path it came from, so a
    href has nothing to resolve against. A def with no data would still claim
    its pos and size during work-area validation."""
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
# repeated placements of one image
#
# A document that stamps the same picture down many times is the common heavy
# case, so the importer decodes, transforms and encodes each distinct result
# once and shares it between the copies.
# ---------------------------------------------------------------------------


def _decode(def_):
    """The image def's raster data as a PIL image."""
    return Image.open(io.BytesIO(base64.b64decode(def_["data"].split(",", 1)[1])))


def _quadrants(n):
    """An n x n image split into four solid colored quadrants."""
    half = n // 2
    return tuple(
        tuple((R if x < half else G) if y < half else (B if x < half else K) for x in range(n))
        for y in range(n)
    )


def _stamped_svg(uri, clips, transform=""):
    """One image placed once per entry in `clips`, each with its own clip rect
    given as (x, y, w, h) in mm."""
    defs = "".join(
        f'<clipPath id="k{i}"><rect x="{c[0]}" y="{c[1]}" width="{c[2]}" height="{c[3]}"/></clipPath>'
        for i, c in enumerate(clips)
    )
    images = "".join(
        f'<image x="0" y="0" width="40" height="40" clip-path="url(#k{i})" '
        f'transform="{transform}" xlink:href="{uri}"/>'
        for i in range(len(clips))
    )
    return (
        '<?xml version="1.0"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="100mm" height="100mm" viewBox="0 0 100 100">'
        f"<defs>{defs}</defs>{images}</svg>"
    )


def test_copies_clipped_alike_share_one_encoding():
    """Placements that land on the same pixels come back as the identical
    string, and ones that do not stay distinct."""
    uri = _png_data_uri(_quadrants(8))
    # three copies clipped to the top-left quadrant, one to the bottom-right
    job = jobimport.convert(
        _stamped_svg(uri, [(0, 0, 20, 20)] * 3 + [(20, 20, 20, 20)]), optimize=False
    )
    defs = [d for d in job["defs"] if d["kind"] == "image"]
    assert len(defs) == 4
    # one encoding between them, so the copies hold the very same string
    assert defs[0]["data"] is defs[1]["data"] is defs[2]["data"]
    assert defs[3]["data"] != defs[0]["data"]
    # and the shared crop is still the right corner of the picture
    for def_ in defs[:3]:
        assert def_["pos"] == pytest.approx([0.0, 0.0])
        assert _pixels(_decode(def_)) == [(R,) * 4] * 4
    assert defs[3]["pos"] == pytest.approx([20.0, 20.0])
    assert _pixels(_decode(defs[3])) == [(K,) * 4] * 4


def test_copies_clipped_alike_after_a_quarter_turn():
    """Reorienting and clipping compose into one pass over the pixels, so the
    turned copies have to come out both shared and correct."""
    uri = _png_data_uri(_quadrants(8))
    # the turn lands the image back over the same 40x40 box and takes the clip
    # rect with it, so the crop is the top-right quadrant of the turned picture
    job = jobimport.convert(
        _stamped_svg(uri, [(0, 0, 20, 20)] * 2, transform="translate(40,0) rotate(90)"),
        optimize=False,
    )
    defs = [d for d in job["defs"] if d["kind"] == "image"]
    assert len(defs) == 2
    assert defs[0]["data"] is defs[1]["data"]
    assert defs[0]["pos"] == pytest.approx([20.0, 0.0])
    assert _pixels(_decode(defs[0])) == [(R,) * 4] * 4


def test_clip_smaller_than_a_pixel_leaves_the_image_alone():
    # the crop rounds away to nothing, so the data has to pass through
    uri = _png_data_uri(MARKER)
    job = jobimport.convert(_stamped_svg(uri, [(0, 0, 0.4, 0.4)]), optimize=False)
    defs = [d for d in job["defs"] if d["kind"] == "image"]
    assert len(defs) == 1
    assert defs[0]["data"] == uri


# ---------------------------------------------------------------------------
# text to path conversion
# ---------------------------------------------------------------------------


def test_text_conversion_skips_documents_without_text():
    # nothing to convert, so the document is handed back untouched rather than
    # parsed and reserialized
    svg = _raster_svg()
    assert convert_text_to_paths(svg) is svg
    assert convert_text_to_paths(svg.encode()) is not None


@pytest.mark.parametrize(
    "element",
    ['<text x="5" y="5">hi</text>', '<svg:text x="5" y="5">hi</svg:text>'],
)
def test_text_conversion_still_sees_text(element):
    svg = (
        '<?xml version="1.0"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:svg="http://www.w3.org/2000/svg" '
        'width="100mm" height="100mm" viewBox="0 0 100 100">'
        f"{element}</svg>"
    )
    assert convert_text_to_paths(svg) is not svg


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
