import json

from config import conf

from . import pathoptimizer
from .dxf_parser import DXFParser
from .gcode_reader import GcodeReader
from .imagedata import normalize_image_data, preview_image_data
from .svg_reader import SVGReader
from .utilities import matrixApply

__author__ = "Stefan Hechenberger <stefan@nortd.com>"


def convert(job, optimize=True, tolerance=conf["tolerance"], matrix=None):
    """Convert a job string (dba, svg, dxf, or gcode).

    Args:
        job: Parsed dba or job string (dba, svg, dxf, or gcode).
        optimize: Flag for optimizing path tolerances.
        tolerance: Tolerance used in convert/optimization.
        matrix: Transformation matrix to apply to dba

    Returns:
        A parsed .dba job.
    """
    type_ = get_type(job)
    if type_ == "dba":
        if type(job) is bytes:
            job = job.decode("utf-8")
        if type(job) is str:
            job = json.loads(job)
        # a stored job may share one payload between defs, everything from
        # here on expects each to carry its own
        resolve_image_data(job)
        dedupe_job(job)
        if optimize:
            optimize_job(job, tolerance)
    elif type_ == "svg":
        job = read_svg(job, tolerance, optimize=optimize)
    elif type_ == "dxf":
        job = read_dxf(job, tolerance, optimize=optimize)
    elif type_ == "gcode":
        job = read_gcode(job, tolerance, optimize=optimize)
    else:
        print("ERROR: file type not recognized")
        raise TypeError
    if matrix:
        apply_alignment_matrix(job, matrix)
    # whatever the source, hand the frontend a raster it can actually display
    # (copies of one image share a string, so each is only looked at once)
    normalized = {}
    for def_ in job.get("defs", []):
        if def_.get("kind") == "image" and def_.get("data"):
            data = def_["data"]
            if data not in normalized:
                normalized[data] = normalize_image_data(data)
            def_["data"] = normalized[data]
    # a source is only ever shown as a small preview, so it ships downscaled
    # rather than as a second full resolution copy of the raster
    previews = {}
    for key, data in job.get("sources", {}).items():
        if data not in previews:
            previews[data] = preview_image_data(data)
        job["sources"][key] = previews[data]
    return job


# The controller takes three decimals and no more, see driveboard.send_param,
# so a coordinate written at the full width of a float repr stores digits that
# are discarded on the way to the machine. Three is also ten times finer than
# the tolerance paths are optimized to.
COORD_DECIMALS = 3


def _round_coords(value):
    """Round the numbers inside lists, which is where coordinates live.

    Named fields are left alone, so settings that happen to be small floats
    (a tolerance, a pixel size) keep their value. This is the rule the
    frontend's own writer follows.
    """
    if isinstance(value, list):
        return [round(v, COORD_DECIMALS) if type(v) is float else _round_coords(v) for v in value]
    if isinstance(value, dict):
        return {k: _round_coords(v) for k, v in value.items()}
    return value


def dumps(job):
    """Serialize a job to .dba text.

    Coordinates go out at the precision the machine can actually act on, and
    the separators lose their padding, which together are worth about a third
    of a vector-heavy job.
    """
    return json.dumps(_round_coords(job), separators=(",", ":"))


def share_image_data(job):
    """Collapse repeated image payloads to a reference to the def that first
    carries them, in place. Undone by resolve_image_data.

    A job that places one picture many times otherwise writes that picture out
    once per placement, so a sheet of copies is nearly all duplicate payload,
    and pays for it again on every save, every load, and every run.
    """
    seen = {}
    for i, def_ in enumerate(job.get("defs") or []):
        if def_.get("kind") != "image":
            continue
        data = def_.get("data")
        if data is None:
            continue
        first = seen.get(data)
        if first is None:
            seen[data] = i
        else:
            del def_["data"]
            def_["data_of"] = first
    return job


def resolve_image_data(job):
    """Give every image def its own pixel data back, in place, undoing
    share_image_data. A job stored before defs could share has none to undo,
    and a reference that leads nowhere is dropped rather than trusted."""
    defs = job.get("defs") or []
    for def_ in defs:
        shared = def_.pop("data_of", None)
        if shared is None:
            continue
        if isinstance(shared, int) and 0 <= shared < len(defs):
            data = defs[shared].get("data")
            if data is not None:
                def_["data"] = data
    return job


def _segment_key(segment):
    """Key a path segment by where it lies, at the precision the machine has.

    A copy traced the other way around covers the same ground, so the segment
    and its reverse share a key.
    """
    points = tuple((round(p[0], COORD_DECIMALS), round(p[1], COORD_DECIMALS)) for p in segment)
    return min(points, points[::-1])


def _placement_key(values):
    """Key a position or a size at the same precision."""
    if not isinstance(values, list):
        return values
    return tuple(round(v, COORD_DECIMALS) if isinstance(v, (int, float)) else v for v in values)


def dedupe_job(job, log_dropped=True):
    """Drop geometry a job draws twice in one place, in place.

    Stacked copies come out of a design the moment something is pasted back
    over itself, and the machine has no way to tell: it cuts the line a second
    time and engraves the image a second time, which costs the time and burns
    past the depth the design asks for.

    A copy only counts as a copy where it lands in the same place. Paths are
    matched inside a color, forwards or backwards, and images by their pixels
    together with their position and size. Copies in different colors are left
    alone, since those are separate passes with settings of their own.

    Run this before optimization, which joins segments that meet end to end
    and would fuse a stacked pair into one longer path first.
    """
    defs = job.get("defs") or []
    items = job.get("items") or []

    seen_segments = {}  # color -> set of segment keys
    seen_images = set()
    seen_draws = set()  # (def index, color) already drawn
    dropped_items = set()
    dropped_segments = 0

    for i, item in enumerate(items):
        idx = item.get("def")
        if not isinstance(idx, int) or not 0 <= idx < len(defs):
            continue
        def_ = defs[idx]
        kind = def_.get("kind")
        color = item.get("color")
        if kind not in ("path", "fill", "image"):
            continue
        # the same def drawn twice the same way is a copy of itself, and its
        # data is shared, so it is dropped without touching what it points at
        if (idx, color) in seen_draws:
            dropped_items.add(i)
            continue
        seen_draws.add((idx, color))
        if kind == "image":
            data = def_.get("data")
            if data is None:
                continue
            key = (data, _placement_key(def_.get("pos")), _placement_key(def_.get("size")))
            if key in seen_images:
                dropped_items.add(i)
            else:
                seen_images.add(key)
        else:
            path = def_.get("data")
            if not isinstance(path, list):
                continue
            seen = seen_segments.setdefault(color, set())
            kept = []
            for segment in path:
                key = _segment_key(segment)
                if key in seen:
                    dropped_segments += 1
                    continue
                seen.add(key)
                kept.append(segment)
            path[:] = kept
            if not kept:
                dropped_items.add(i)

    if dropped_items:
        _drop_items(job, items, dropped_items)
    _drop_unused_defs(job)

    if log_dropped and (dropped_items or dropped_segments):
        print(
            f"INFO: dropped {dropped_segments} duplicate path(s) and {len(dropped_items)} duplicate item(s)"
        )
    return job


def _drop_items(job, items, dropped):
    """Remove the listed items and renumber the passes that referred to them."""
    remap = {}
    kept = []
    for i, item in enumerate(items):
        if i in dropped:
            continue
        remap[i] = len(kept)
        kept.append(item)
    items[:] = kept
    for pass_ in job.get("passes") or []:
        if "items" in pass_:
            pass_["items"] = [remap[i] for i in pass_["items"] if i in remap]


def _drop_unused_defs(job):
    """Remove defs no item draws any more, along with the sources they own."""
    defs = job.get("defs") or []
    items = job.get("items") or []
    used = set()
    for item in items:
        idx = item.get("def")
        if isinstance(idx, int) and 0 <= idx < len(defs):
            used.add(idx)
    if len(used) == len(defs):
        return
    order = sorted(used)
    remap = {old: new for new, old in enumerate(order)}
    job["defs"] = [defs[i] for i in order]
    for item in items:
        idx = item.get("def")
        if idx in remap:
            item["def"] = remap[idx]
    sources = job.get("sources")
    if sources:
        kept = {def_["source"] for def_ in job["defs"] if "source" in def_}
        job["sources"] = {k: v for k, v in sources.items() if k in kept}


def optimize_job(job, tolerance):
    """Optimize the path and fill defs of a parsed dba job in place."""
    for def_ in job.get("defs", []):
        if def_["kind"] == "path":
            pathoptimizer.optimize(def_["data"], tolerance)
        if def_["kind"] == "fill":
            fill_mode = conf["fill_mode"]
            if fill_mode not in ["Forward", "Reverse", "Bidirectional", "NearestNeighbor"]:
                fill_mode = "Bidirectional"
                print("WARN: fill_mode not recognized. Please check your config file.")
            if fill_mode == "Forward":
                pass
            elif fill_mode == "Reverse":
                pathoptimizer.reverse_path(def_["data"])
            elif fill_mode == "Bidirectional":
                pathoptimizer.fill_optimize(def_["data"], tolerance)
            elif fill_mode == "NearestNeighbor":
                pathoptimizer.optimize(def_["data"], tolerance)
    if "head" not in job:
        job["head"] = {}
    job["head"]["optimized"] = tolerance


def apply_alignment_matrix(job, matrix):
    """Transform the coordinates in the job with the supplied matrix."""
    # Get the SVG-style 6-element vector from the 3x3 matrix
    mat = [
        matrix[0][0],
        matrix[1][0],
        matrix[0][1],
        matrix[1][1],
        matrix[0][2],
        matrix[1][2],
    ]

    # Only the 'defs' list contains coordinates that need to be transformed
    defs = job["defs"]

    for one_def in defs:
        if one_def["kind"] == "image":
            # TODO: raster images are not supported!
            #       The rest of the code assumes the rows of pixel data are aligned
            #       with the X axis, so handling rotation will require transforming
            #       the entire image and generating new data.
            pass
        elif one_def["kind"] == "path":
            for one_path in one_def["data"]:
                for one_point in one_path:
                    matrixApply(mat, one_point)


def read_svg(svg_string, tolerance, forced_dpi=None, optimize=True):
    """Read a svg file string and convert to dba job."""
    svgReader = SVGReader(tolerance, conf["workspace"])
    res = svgReader.parse(svg_string, forced_dpi, conf["require_unit"])
    # {'boundarys':b, 'dpi':d, 'lasertags':l, 'rasters':r}

    # create an dba job from res
    # TODO: reader should generate an dba job to begin with
    job = {"head": {}, "passes": [], "items": [], "defs": []}
    if "rasters" in res:
        sources = {}
        for raster in res["rasters"]:
            def_ = {
                "kind": "image",
                "data": raster["data"],
                "pos": raster["pos"],
                "size": raster["size"],
            }
            if "source" in raster:
                def_["source"] = raster["source"]
                # keep the original data when clipping cropped this copy,
                # so the frontend can preview the unclipped image
                if raster["data"] != raster.get("source_data"):
                    sources[raster["source"]] = raster["source_data"]
            job["defs"].append(def_)
            job["items"].append({"def": len(job["defs"]) - 1})
        if sources:
            job["sources"] = sources

    if "boundarys" in res:
        if "dpi" in res:
            job["head"]["dpi"] = res["dpi"]
        for color, path in res["boundarys"].items():
            job["defs"].append({"kind": "path", "data": path})
            job["items"].append({"def": len(job["defs"]) - 1, "color": color})

    if "lasertags" in res:
        # format: [('12', '2550', '', '100', '%', ':#fff000', ':#ababab', ':#ccc999', '', '', '')]
        # sort lasertags by pass number
        # def _cmp(a, b):
        #     if a[0] < b[0]: return -1
        #     elif a[0] > b[0]: return 1
        #     else: return 0
        res["lasertags"].sort()
        # index item ids by color so each tag maps to its items in one lookup
        color_to_idxs = {}
        for i, item in enumerate(job["items"]):
            if "color" in item:
                color_to_idxs.setdefault(item["color"], []).append(i)
        # add tags as passes
        for tag in res["lasertags"]:
            if len(tag) == 11:
                idxs = []
                for colidx in range(5, 10):
                    idxs.extend(color_to_idxs.get(tag[colidx], []))
                if "passes" not in job:
                    job["passes"] = []
                job["passes"].append({"items": idxs, "feedrate": tag[1], "intensity": tag[3]})

    # before optimization, which would fuse a stacked pair into one path
    dedupe_job(job)
    if optimize:
        optimize_job(job, tolerance)

    # Include any warnings from conversion
    if "warnings" in res:
        job["head"]["warnings"] = res["warnings"]

    return job


def read_dxf(dxf_string, tolerance, optimize=True):
    """Read a dxf file string and optimize returned value."""
    dxfParser = DXFParser(tolerance, conf["workspace"])
    # second argument is the forced unit, TBI in Driverboard
    job = dxfParser.parse(dxf_string, None)
    dedupe_job(job)
    if optimize:
        # optimize each path def in place
        for def_ in job["defs"]:
            if def_["kind"] == "path":
                pathoptimizer.optimize(def_["data"], tolerance)
        job["head"]["optimized"] = tolerance
    return job


def read_gcode(gcode_string, tolerance, optimize=False):
    """Read a gcode file string and convert to dba job."""
    reader = GcodeReader()
    job = reader.parse(gcode_string)
    if optimize:
        pass
    return job


def get_type(job):
    """Figure out file type from job string."""
    # figure out type
    if type(job) is dict:
        type_ = "dba"
    elif type(job) in [str, bytes]:
        if type(job) is bytes:
            # only the header is inspected, so a whole-job decode is wasted
            jobheader = job[:1024].decode("utf-8", "ignore").lstrip()
        else:
            jobheader = job[:1024].lstrip()
        if jobheader and jobheader[0] == "{":
            type_ = "dba"
        elif "<?xml" in jobheader and "<svg" in jobheader:
            type_ = "svg"
        elif "SECTION" in jobheader and "HEADER" in jobheader:
            type_ = "dxf"
        elif (
            "G0" in jobheader
            or "G1" in jobheader
            or "G00" in jobheader
            or "G01" in jobheader
            or "g0" in jobheader
            or "g1" in jobheader
            or "g00" in jobheader
            or "g01" in jobheader
        ):
            type_ = "gcode"
        else:
            print("ERROR: Cannot figure out file type 1.")
            raise TypeError
    else:
        print("ERROR: Cannot figure out file type 2.")
        raise TypeError
    return type_
