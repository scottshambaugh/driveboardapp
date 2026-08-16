"""Turning a file of any supported kind into a parsed .dba job."""

import json

from config import conf

from . import pathoptimizer
from .dba import resolve_image_data
from .dedupe import dedupe_job
from .dxf_parser import DXFParser
from .gcode_reader import GcodeReader
from .imagedata import normalize_image_data, preview_image_data
from .svg_reader import SVGReader
from .utilities import matrixApply


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
