"""End-to-end tests for the job import pipeline (dba/svg/dxf/gcode).

These exercise the real parsers against the repository's own fixture files
(``library/*.dba`` and ``backend/testjobs/*``) so the full conversion path
is covered without any hardware.
"""

import base64
import copy
import glob
import io
import json
import os

import jobimport
import pytest
from jobimport import imagedata
from jobimport.gcode_reader import GcodeReader
from jobimport.imagedata import preview_image_data
from jobimport.svg_reader import SVGReader
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
        # a translate with no y given moves in x only, and y only moves with
        # the second parameter, which is the only way svg can ask for it
        ("translate(30)", [40.0, 20.0], [40.0, 20.0], [(R, G), (B, K)]),
        ("translate(0,30)", [10.0, 50.0], [40.0, 20.0], [(R, G), (B, K)]),
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
    ids=[
        "upright",
        "translate-x",
        "translate-y",
        "scaled",
        "cw",
        "ccw",
        "half-turn",
        "mirror-h",
        "mirror-v",
    ],
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


def _use_svg(body, defs=""):
    """A 100x100mm svg whose defs hold one 40x20 image with id "img0"."""
    return (
        '<?xml version="1.0"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="100mm" height="100mm" viewBox="0 0 100 100">'
        f'<defs><image id="img0" x="0" y="0" width="40" height="20" '
        f'xlink:href="{_png_data_uri(MARKER)}"/>{defs}</defs>'
        f"{body}"
        "</svg>"
    )


def test_use_places_a_referenced_image():
    """A 'use' draws the image it points at, offset by its own x/y."""
    job = jobimport.convert(_use_svg('<use xlink:href="#img0" x="10" y="20"/>'), optimize=False)
    defs = [d for d in job["defs"] if d["kind"] == "image"]
    assert len(defs) == 1
    assert defs[0]["pos"] == pytest.approx([10.0, 20.0])
    assert defs[0]["size"] == pytest.approx([40.0, 20.0])
    # nothing to reorient, so the pixels must not be re-encoded
    assert defs[0]["data"] == _png_data_uri(MARKER)


def test_use_takes_the_transform_and_clip_of_its_group():
    """The referencing element's frame and clip apply to what it draws."""
    svg = _use_svg(
        '<g transform="translate(0,50)" clip-path="url(#c1)">'
        '<use xlink:href="#img0" x="10" y="0"/>'
        "</g>",
        # the rect is in the group's own frame, which the translate moved
        defs='<clipPath id="c1"><rect x="10" y="0" width="20" height="10"/></clipPath>',
    )
    job = jobimport.convert(svg, optimize=False)
    defs = [d for d in job["defs"] if d["kind"] == "image"]
    assert len(defs) == 1
    assert defs[0]["pos"] == pytest.approx([10.0, 50.0])
    assert defs[0]["size"] == pytest.approx([20.0, 10.0])


def test_use_copies_share_one_source():
    """Two 'use' tags of one image are copies of a single original."""
    svg = _use_svg(
        '<use xlink:href="#img0" x="10" y="0" clip-path="url(#c1)"/>'
        '<use xlink:href="#img0" x="10" y="50" clip-path="url(#c2)"/>',
        defs=(
            '<clipPath id="c1"><rect x="10" y="0" width="20" height="10"/></clipPath>'
            '<clipPath id="c2"><rect x="10" y="50" width="20" height="10"/></clipPath>'
        ),
    )
    job = jobimport.convert(svg, optimize=False)
    defs = [d for d in job["defs"] if d["kind"] == "image"]
    assert len(defs) == 2
    assert defs[0]["source"] == defs[1]["source"]
    assert job["sources"] == {defs[0]["source"]: _png_data_uri(MARKER)}


def test_use_draws_a_referenced_group():
    """A 'use' of a group draws the whole group, offset by its x/y."""
    svg = _use_svg(
        '<use xlink:href="#g0" x="10" y="10"/>',
        defs='<g id="g0"><path d="M 0,0 L 20,0" stroke="#ff0000" fill="none"/></g>',
    )
    job = jobimport.convert(svg, optimize=False)
    paths = [d["data"] for d in job["defs"] if d["kind"] == "path"]
    assert len(paths) == 1
    assert paths[0] == [[pytest.approx([10.0, 10.0]), pytest.approx([30.0, 10.0])]]


def test_defs_are_only_drawn_where_used():
    # the image sits in defs and nothing references it
    job = jobimport.convert(_use_svg(""), optimize=False)
    assert not [d for d in job["defs"] if d["kind"] == "image"]


@pytest.mark.parametrize(
    "body",
    [
        '<use xlink:href="#nope" x="10" y="20"/>',
        '<use xlink:href="other.svg#img0" x="10" y="20"/>',
        # a self reference must not recurse forever
        '<g id="g0"><use xlink:href="#g0"/></g>',
    ],
    ids=["unknown-id", "external-file", "cycle"],
)
def test_use_skips_what_it_cannot_draw(body):
    job = jobimport.convert(_use_svg(body), optimize=False)
    assert not [d for d in job["defs"] if d["kind"] == "image"]


def _pattern_svg(defs, body):
    """A 100x100mm svg, one svg user unit to the mm."""
    return (
        '<?xml version="1.0"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="100mm" height="100mm" viewBox="0 0 100 100">'
        f"<defs>{defs}</defs>{body}"
        "</svg>"
    )


def _tile_pattern(pid="p", attribs='patternUnits="userSpaceOnUse" width="50" height="50"'):
    """A 50x50 pattern tile holding one 40x20 image at (10, 20) of the tile."""
    return (
        f"<pattern id='{pid}' {attribs}>"
        f'<image x="10" y="20" width="40" height="20" xlink:href="{_png_data_uri(MARKER)}"/>'
        "</pattern>"
    )


def _pattern_images(defs, body):
    """Convert a pattern fixture, returning its image defs by position."""
    job = jobimport.convert(_pattern_svg(defs, body), optimize=False)
    defs_out = [d for d in job["defs"] if d["kind"] == "image"]
    return sorted(defs_out, key=lambda d: (round(d["pos"][1], 3), round(d["pos"][0], 3)))


def test_pattern_fill_draws_the_tile():
    """A shape filled with a pattern draws the pattern content, placed in the
    tile's frame. This is what Inkscape's 'Objects to Pattern' leaves behind."""
    defs = _tile_pattern()
    imgs = _pattern_images(
        defs, '<rect x="0" y="0" width="50" height="50" fill="url(#p)" stroke="none"/>'
    )
    assert len(imgs) == 1
    assert imgs[0]["pos"] == pytest.approx([10.0, 20.0])
    assert imgs[0]["size"] == pytest.approx([40.0, 20.0])
    # nothing was cropped, so the pixels must not be re-encoded
    assert imgs[0]["data"] == _png_data_uri(MARKER)


def test_pattern_fill_repeats_over_the_shape():
    """A tile smaller than the shape is laid down as often as it takes to
    cover it."""
    imgs = _pattern_images(
        _tile_pattern(), '<rect x="0" y="0" width="100" height="100" fill="url(#p)" stroke="none"/>'
    )
    assert [(d["pos"][0], d["pos"][1]) for d in imgs] == [
        pytest.approx(p) for p in ((10.0, 20.0), (60.0, 20.0), (10.0, 70.0), (60.0, 70.0))
    ]
    # one image placed four times stays one image
    assert len({d["data"] for d in imgs}) == 1


def test_pattern_fill_follows_the_href_chain():
    """A pattern may take its content and size from another pattern and bring
    only its own patternTransform, which is how repeated copies are written."""
    defs = _tile_pattern() + '<pattern id="p2" xlink:href="#p" patternTransform="translate(50,0)"/>'
    imgs = _pattern_images(
        defs, '<rect x="50" y="0" width="50" height="50" fill="url(#p2)" stroke="none"/>'
    )
    assert len(imgs) == 1
    assert imgs[0]["pos"] == pytest.approx([60.0, 20.0])
    assert imgs[0]["size"] == pytest.approx([40.0, 20.0])


def test_pattern_fill_is_clipped_to_the_shape():
    """The pattern only paints inside the shape, so content that runs past it
    is cropped rather than engraved."""
    imgs = _pattern_images(
        _tile_pattern(), '<rect x="0" y="0" width="30" height="50" fill="url(#p)" stroke="none"/>'
    )
    assert len(imgs) == 1
    assert imgs[0]["pos"] == pytest.approx([10.0, 20.0])
    assert imgs[0]["size"] == pytest.approx([20.0, 20.0])


def test_pattern_fill_takes_the_frame_of_the_shape():
    """The tile grid lives in the filled shape's own frame, so a transform on
    the shape moves the pattern with it."""
    imgs = _pattern_images(
        _tile_pattern(),
        '<g transform="translate(20,10)">'
        '<rect x="0" y="0" width="50" height="50" fill="url(#p)" stroke="none"/>'
        "</g>",
    )
    assert len(imgs) == 1
    assert imgs[0]["pos"] == pytest.approx([30.0, 30.0])


def test_pattern_fill_in_object_bounding_box_units():
    """patternUnits defaults to objectBoundingBox, where the tile is a fraction
    of the shape's box rather than a length."""
    imgs = _pattern_images(
        _tile_pattern(attribs='width="1" height="1"'),
        '<rect x="20" y="20" width="50" height="50" fill="url(#p)" stroke="none"/>',
    )
    assert len(imgs) == 1
    # the tile covers the whole box, and content is in user units from its corner
    assert imgs[0]["pos"] == pytest.approx([30.0, 40.0])


def test_pattern_fill_draws_vector_content():
    """Pattern content is not only rasters; paths tile the same way."""
    defs = (
        '<pattern id="p" patternUnits="userSpaceOnUse" width="50" height="50">'
        '<path d="M 0,0 L 20,0" stroke="#ff0000" fill="none"/>'
        "</pattern>"
    )
    job = jobimport.convert(
        _pattern_svg(
            defs, '<rect x="0" y="0" width="100" height="50" fill="url(#p)" stroke="none"/>'
        ),
        optimize=False,
    )
    paths = [p for d in job["defs"] if d["kind"] == "path" for p in d["data"]]
    assert sorted(p[0][0] for p in paths) == [pytest.approx(0.0), pytest.approx(50.0)]


def test_pattern_fill_keeps_the_stroke_of_the_shape():
    """Filling with a pattern says nothing about the outline, which still cuts."""
    job = jobimport.convert(
        _pattern_svg(
            _tile_pattern(),
            '<rect x="0" y="0" width="50" height="50" fill="url(#p)" stroke="#ff0000"/>',
        ),
        optimize=False,
    )
    kinds = sorted(d["kind"] for d in job["defs"])
    assert kinds == ["image", "path"]


@pytest.mark.parametrize(
    "defs,body",
    [
        # a gradient is a paint server this reader has no geometry for
        (
            '<linearGradient id="g"><stop offset="0" stop-color="#ff0000"/></linearGradient>',
            '<rect x="0" y="0" width="50" height="50" fill="url(#g)" stroke="none"/>',
        ),
        # nothing by that id
        ("", '<rect x="0" y="0" width="50" height="50" fill="url(#nope)" stroke="none"/>'),
        # an empty pattern paints nothing
        (
            '<pattern id="p" patternUnits="userSpaceOnUse" width="50" height="50"/>',
            '<rect x="0" y="0" width="50" height="50" fill="url(#p)" stroke="none"/>',
        ),
        # a pattern whose content fills itself must not recurse forever
        (
            '<pattern id="p" patternUnits="userSpaceOnUse" width="50" height="50">'
            '<rect x="0" y="0" width="50" height="50" fill="url(#p)" stroke="none"/>'
            "</pattern>",
            '<rect x="0" y="0" width="50" height="50" fill="url(#p)" stroke="none"/>',
        ),
    ],
    ids=["gradient", "unknown-id", "empty-pattern", "cycle"],
)
def test_pattern_fill_skips_what_it_cannot_draw(defs, body):
    job = jobimport.convert(_pattern_svg(defs, body), optimize=False)
    assert job["defs"] == []


def test_pattern_fill_stops_at_the_tile_limit():
    """A tile far smaller than the shape is capped rather than filling the job
    with copies of itself."""
    defs = (
        '<pattern id="p" patternUnits="userSpaceOnUse" width="1" height="1">'
        '<path d="M 0,0 L 0.5,0" stroke="#ff0000" fill="none"/>'
        "</pattern>"
    )
    job = jobimport.convert(
        _pattern_svg(
            defs, '<rect x="0" y="0" width="100" height="100" fill="url(#p)" stroke="none"/>'
        ),
        optimize=False,
    )
    paths = [p for d in job["defs"] if d["kind"] == "path" for p in d["data"]]
    assert len(paths) == SVGReader.MAX_PATTERN_TILES


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


def _stamped_svg(uri, clips, transform="", transforms=None):
    """One image placed once per entry in `clips`, each with its own clip rect
    given as (x, y, w, h) in mm. `transforms` gives a copy its own transform,
    which moves its clip rect with it."""
    defs = "".join(
        f'<clipPath id="k{i}"><rect x="{c[0]}" y="{c[1]}" width="{c[2]}" height="{c[3]}"/></clipPath>'
        for i, c in enumerate(clips)
    )
    images = "".join(
        f'<image x="0" y="0" width="40" height="40" clip-path="url(#k{i})" '
        f'transform="{transforms[i] if transforms else transform}" xlink:href="{uri}"/>'
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
    # three copies clipped to the top-left quadrant, one to the bottom-right.
    # the copies sit side by side, since stacked ones would be deduplicated
    job = jobimport.convert(
        _stamped_svg(
            uri,
            [(0, 0, 20, 20)] * 3 + [(20, 20, 20, 20)],
            transforms=["", "translate(30,0)", "translate(60,0)", ""],
        ),
        optimize=False,
    )
    defs = [d for d in job["defs"] if d["kind"] == "image"]
    assert len(defs) == 4
    # one encoding between them, so the copies hold the very same string
    assert defs[0]["data"] is defs[1]["data"] is defs[2]["data"]
    assert defs[3]["data"] != defs[0]["data"]
    # and the shared crop is still the right corner of the picture
    for def_, x in zip(defs[:3], [0.0, 30.0, 60.0]):
        assert def_["pos"] == pytest.approx([x, 0.0])
        assert _pixels(_decode(def_)) == [(R,) * 4] * 4
    assert defs[3]["pos"] == pytest.approx([20.0, 20.0])
    assert _pixels(_decode(defs[3])) == [(K,) * 4] * 4


def test_copies_clipped_alike_after_a_quarter_turn():
    """Reorienting and clipping compose into one pass over the pixels, so the
    turned copies have to come out both shared and correct."""
    uri = _png_data_uri(_quadrants(8))
    # the turn lands the image back over the same 40x40 box and takes the clip
    # rect with it, so the crop is the top-right quadrant of the turned picture.
    # the second copy is turned in a box next to it, since a stacked one would
    # be deduplicated
    job = jobimport.convert(
        _stamped_svg(
            uri,
            [(0, 0, 20, 20)] * 2,
            transforms=["translate(40,0) rotate(90)", "translate(90,0) rotate(90)"],
        ),
        optimize=False,
    )
    defs = [d for d in job["defs"] if d["kind"] == "image"]
    assert len(defs) == 2
    assert defs[0]["data"] is defs[1]["data"]
    assert defs[1]["pos"] == pytest.approx([70.0, 0.0])
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
# deduplication of stacked copies
# ---------------------------------------------------------------------------


def _path_job(*colored_paths):
    """A job drawing one path def per (color, path) pair given."""
    job = {"head": {}, "passes": [], "items": [], "defs": []}
    for color, path in colored_paths:
        job["defs"].append({"kind": "path", "data": path})
        job["items"].append({"def": len(job["defs"]) - 1, "color": color})
    return job


SQUARE = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]


def test_dedupe_drops_a_stacked_copy():
    job = _path_job(("#ff0000", [list(SQUARE), list(SQUARE)]))
    jobimport.dedupe_job(job)
    assert job["defs"][0]["data"] == [SQUARE]


def test_dedupe_drops_a_copy_in_another_def_of_the_same_color():
    job = _path_job(("#ff0000", [list(SQUARE)]), ("#ff0000", [list(SQUARE)]))
    jobimport.dedupe_job(job)
    assert len(job["defs"]) == 1
    assert len(job["items"]) == 1


def test_dedupe_matches_a_copy_drawn_backwards():
    job = _path_job(("#ff0000", [list(SQUARE), list(reversed(SQUARE))]))
    jobimport.dedupe_job(job)
    assert job["defs"][0]["data"] == [SQUARE]


def test_dedupe_keeps_a_copy_in_another_color():
    job = _path_job(("#ff0000", [list(SQUARE)]), ("#0000ff", [list(SQUARE)]))
    jobimport.dedupe_job(job)
    assert len(job["items"]) == 2
    assert all(d["data"] == [SQUARE] for d in job["defs"])


def test_dedupe_keeps_a_copy_somewhere_else():
    moved = [[x + 20, y] for x, y in SQUARE]
    job = _path_job(("#ff0000", [list(SQUARE), moved]))
    jobimport.dedupe_job(job)
    assert job["defs"][0]["data"] == [SQUARE, moved]


def test_dedupe_renumbers_the_passes():
    job = _path_job(("#ff0000", [list(SQUARE)]), ("#ff0000", [list(SQUARE)]))
    job["items"].append({"def": 0, "color": "#00ff00"})
    job["defs"].append({"kind": "path", "data": [list(SQUARE)]})
    job["items"][-1]["def"] = 2
    job["passes"] = [{"items": [0, 1, 2], "feedrate": 2000, "intensity": 50}]
    jobimport.dedupe_job(job)
    # the stacked red copy is gone and the green one keeps its place in the pass
    assert len(job["items"]) == 2
    assert job["passes"][0]["items"] == [0, 1]
    assert job["items"][1]["color"] == "#00ff00"


def _image_job(*placements):
    """A job placing images, each given as (data, pos, size)."""
    job = {"head": {}, "passes": [], "items": [], "defs": []}
    for data, pos, size in placements:
        job["defs"].append({"kind": "image", "data": data, "pos": pos, "size": size})
        job["items"].append({"def": len(job["defs"]) - 1})
    return job


def test_dedupe_drops_a_raster_placed_twice_in_one_spot():
    uri = _png_data_uri([[(0, 0, 0, 255), (255, 255, 255, 255)]])
    job = _image_job((uri, [5.0, 5.0], [10.0, 5.0]), (uri, [5.0, 5.0], [10.0, 5.0]))
    jobimport.dedupe_job(job)
    assert len(job["defs"]) == 1
    assert len(job["items"]) == 1


@pytest.mark.parametrize(
    "pos,size",
    [
        ([25.0, 5.0], [10.0, 5.0]),  # moved
        ([5.0, 5.0], [20.0, 10.0]),  # scaled
    ],
)
def test_dedupe_keeps_a_raster_placed_differently(pos, size):
    uri = _png_data_uri([[(0, 0, 0, 255), (255, 255, 255, 255)]])
    job = _image_job((uri, [5.0, 5.0], [10.0, 5.0]), (uri, pos, size))
    jobimport.dedupe_job(job)
    assert len(job["defs"]) == 2
    assert len(job["items"]) == 2


def _svg_body(body):
    """A 100x100mm document, one user unit to the mm, holding `body`."""
    return (
        '<?xml version="1.0"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm" '
        f'viewBox="0 0 100 100">{body}</svg>'
    )


def _points(job):
    """Every path coordinate in the job, in order, flat so approx can read it."""
    return [
        v for d in job["defs"] if d["kind"] == "path" for seg in d["data"] for p in seg for v in p
    ]


@pytest.mark.parametrize(
    "d",
    [
        "M 10 20 L 30 20",  # the long way round, for reference
        "M.1e2.2e2L.3e2.2e2",  # exponents, and no leading digit anywhere
        "M 1e1 2e1 L 3e1 2e1",
        "M 1E1 2E1 L 3E1 2E1",  # an upper case exponent
        "M 1e+1 2e+1 L 3e+1 2e+1",  # a signed exponent
        "M 10. 20. L 30. 20.",  # a trailing decimal point
    ],
)
def test_path_data_reads_the_whole_number_grammar(d):
    """Every spelling of the same two points has to land in the same place.
    Editors that shorten their output write all of these."""
    job = jobimport.convert(
        _svg_body(f'<path d="{d}" fill="none" stroke="#ff0000"/>'), optimize=False
    )
    assert _points(job) == pytest.approx([10.0, 20.0, 30.0, 20.0])


@pytest.mark.parametrize(
    "transform",
    ["translate(20,0)", "translate(2e1,0)", "translate(2E1,0)", "translate(2e+1,0)"],
)
def test_transform_numbers_read_the_whole_grammar(transform):
    body = f'<g transform="{transform}"><path d="M 0 20 L 10 20" fill="none" stroke="#ff0000"/></g>'
    job = jobimport.convert(_svg_body(body), optimize=False)
    assert _points(job) == pytest.approx([20.0, 20.0, 30.0, 20.0])


def test_dimensions_read_a_number_with_no_leading_digit():
    body = '<rect x=".5" y=".5" width="10" height="10" fill="none" stroke="#ff0000"/>'
    job = jobimport.convert(_svg_body(body), optimize=False)
    xs = _points(job)[0::2]
    ys = _points(job)[1::2]
    assert (min(xs), min(ys)) == pytest.approx((0.5, 0.5))


def test_a_translate_with_no_y_moves_in_x_only():
    """A group copied sideways, as Inkscape writes it, has to land beside the
    original rather than diagonally on top of another copy."""
    rect = '<rect x="10" y="10" width="20" height="20" fill="none" stroke="#ff0000"/>'
    svg = (
        '<?xml version="1.0"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm" '
        'viewBox="0 0 100 100">'
        f'<g>{rect}</g><g transform="translate(50)">{rect}</g></svg>'
    )
    job = jobimport.convert(svg, optimize=False)
    segments = [seg for d in job["defs"] if d["kind"] == "path" for seg in d["data"]]
    assert len(segments) == 2
    corners = sorted((min(p[0] for p in s), min(p[1] for p in s)) for s in segments)
    assert corners == pytest.approx([(10.0, 10.0), (60.0, 10.0)])


def test_dedupe_drops_a_stacked_copy_from_an_svg():
    rect = '<rect x="10" y="10" width="20" height="20" fill="none" stroke="#ff0000"/>'
    svg = (
        '<?xml version="1.0"?>'
        '<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm" '
        'viewBox="0 0 100 100">' + rect + rect + "</svg>"
    )
    job = jobimport.convert(svg, optimize=False)
    segments = [seg for d in job["defs"] if d["kind"] == "path" for seg in d["data"]]
    assert len(segments) == 1


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


# ---------------------------------------------------------------------------
# source previews
# ---------------------------------------------------------------------------


def _big_png_uri(width, height):
    """Data URI for a noisy image, big enough to need downscaling."""
    img = Image.new("RGBA", (width, height))
    img.putdata(
        [
            ((x * 7) % 256, (y * 11) % 256, (x + y) % 256, 255)
            for y in range(height)
            for x in range(width)
        ]
    )
    return _data_uri(img)


def _decode_uri(uri):
    """A data URI as a PIL image (the def-level helper above takes a def)."""
    return Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1])))


def test_preview_downscales_a_large_image_keeping_aspect():
    uri = _big_png_uri(600, 300)
    preview = preview_image_data(uri)
    img = _decode_uri(preview)
    assert max(img.size) <= imagedata.PREVIEW_MAX_DIM
    assert img.size == (256, 128)


def test_preview_leaves_a_small_image_untouched():
    # small rasters are already preview sized, so they are not re-encoded
    uri = _png_data_uri(MARKER)
    assert preview_image_data(uri) == uri


def test_preview_is_idempotent():
    once = preview_image_data(_big_png_uri(600, 300))
    assert preview_image_data(once) == once


def test_preview_passes_through_data_it_cannot_read():
    assert (
        preview_image_data("data:image/png;base64,notanimage") == "data:image/png;base64,notanimage"
    )
    assert preview_image_data("") == ""


def test_preview_keeps_transparency():
    img = Image.new("RGBA", (400, 400), (255, 0, 0, 0))
    preview = preview_image_data(_data_uri(img))
    assert _decode_uri(preview).convert("RGBA").getpixel((0, 0))[3] == 0


def test_clipped_source_ships_downscaled_but_def_data_stays_full_size():
    """The preview a job carries for the passes list is thumbnail sized, while
    the raster the laser actually burns keeps every pixel."""
    uri = _big_png_uri(600, 300)
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
    full, clipped = defs
    # the unclipped placement still carries the original pixels
    assert _decode_uri(full["data"]).size == (600, 300)
    # the preview rides along downscaled, keyed by the shared source id
    assert list(job["sources"]) == [full["source"]]
    source = job["sources"][full["source"]]
    assert _decode_uri(source).size == (256, 128)


def test_share_image_data_points_repeats_at_the_first_copy():
    job = {
        "defs": [
            {"kind": "image", "pos": [0, 0], "size": [1, 1], "data": "AAA"},
            {"kind": "path", "data": [[[0, 0]]]},
            {"kind": "image", "pos": [2, 0], "size": [1, 1], "data": "AAA"},
            {"kind": "image", "pos": [4, 0], "size": [1, 1], "data": "BBB"},
            {"kind": "image", "pos": [6, 0], "size": [1, 1], "data": "AAA"},
        ]
    }
    jobimport.share_image_data(job)
    assert job["defs"][0]["data"] == "AAA"
    assert "data" not in job["defs"][2] and job["defs"][2]["data_of"] == 0
    assert job["defs"][3]["data"] == "BBB"  # a different picture keeps its own
    assert job["defs"][4]["data_of"] == 0
    assert job["defs"][1]["data"] == [[[0, 0]]]  # paths untouched


def test_resolve_image_data_undoes_sharing():
    job = {
        "defs": [
            {"kind": "image", "data": "AAA"},
            {"kind": "image", "data_of": 0},
            {"kind": "image", "data_of": 0},
        ]
    }
    jobimport.resolve_image_data(job)
    assert [d["data"] for d in job["defs"]] == ["AAA", "AAA", "AAA"]
    assert not any("data_of" in d for d in job["defs"])


def test_sharing_round_trips_through_json():
    job = {
        "defs": [
            {"kind": "image", "pos": [0, 0], "size": [1, 1], "data": "AAA"},
            {"kind": "image", "pos": [2, 0], "size": [1, 1], "data": "AAA"},
        ]
    }
    shared = json.dumps(jobimport.share_image_data(copy.deepcopy(job)))
    assert len(shared) < len(json.dumps(job))
    assert jobimport.resolve_image_data(json.loads(shared))["defs"] == job["defs"]


def test_resolve_image_data_drops_a_reference_that_leads_nowhere():
    job = {"defs": [{"kind": "image", "data_of": 7}, {"kind": "image", "data_of": "x"}]}
    jobimport.resolve_image_data(job)
    assert not any("data_of" in d for d in job["defs"])
    assert not any("data" in d for d in job["defs"])


def test_a_job_stored_without_sharing_still_loads():
    job = {"defs": [{"kind": "image", "data": "AAA"}, {"kind": "image", "data": "AAA"}]}
    assert jobimport.resolve_image_data(copy.deepcopy(job))["defs"] == job["defs"]


def test_dumps_rounds_coordinates_to_what_the_wire_carries():
    job = {"defs": [{"kind": "path", "data": [[[300.03750739553743, 1.1658347295008896e-05]]]}]}
    out = json.loads(jobimport.dumps(job))
    assert out["defs"][0]["data"] == [[[300.038, 0.0]]]


def test_dumps_rounding_never_changes_what_the_controller_decodes():
    # send_param quantizes to a thousandth of a millimetre, so storing more
    # decimals than that keeps digits the machine throws away
    def wire(v):
        return int(round((v + 134217.728) * 1000))

    job = {
        "defs": [
            {
                "kind": "path",
                "data": [
                    [
                        [300.03750739553743, 1.1658347295008896e-05],
                        [0.1 + 0.2, 123.456789012345],
                        [-45.6789012345, 999.9994999999],
                    ]
                ],
            }
        ]
    }
    out = json.loads(jobimport.dumps(job))
    for before, after in zip(job["defs"][0]["data"][0], out["defs"][0]["data"][0]):
        for a, b in zip(before, after):
            assert wire(a) == wire(b)


def test_dumps_leaves_named_settings_alone():
    # a tolerance or pixel size is not a coordinate, rounding it would change it
    job = {"head": {"optimized": 0.0001}, "passes": [{"pxsize": 0.0625, "feedrate": 2000}]}
    out = json.loads(jobimport.dumps(job))
    assert out["head"]["optimized"] == 0.0001
    assert out["passes"][0]["pxsize"] == 0.0625


def test_dumps_drops_separator_padding():
    assert ", " not in jobimport.dumps({"a": [1, 2]})
    assert '": ' not in jobimport.dumps({"a": [1, 2]})


def test_dumps_output_reloads_unchanged():
    job = {"head": {}, "passes": [], "items": [{"def": 0}], "defs": [{"kind": "path", "data": []}]}
    assert json.loads(jobimport.dumps(job)) == job
