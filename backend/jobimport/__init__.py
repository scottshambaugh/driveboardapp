
import json

from config import conf
from .svg_reader import SVGReader
from .dxf_parser import DXFParser
from .gcode_reader import GcodeReader
from . import pathoptimizer

from .utilities import matrixApply


__author__ = 'Stefan Hechenberger <stefan@nortd.com>'



def convert(job, optimize=True, tolerance=conf['tolerance'], matrix=None):
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
    if type_ == 'dba':
        if type(job) is bytes: 
            job = job.decode('utf-8')
        if type(job) is str:
            job = json.loads(job)
        if optimize:
            if 'defs' in job:
                for def_ in job['defs']:
                    if def_['kind'] == 'path':
                        pathoptimizer.optimize(def_['data'], tolerance)
                    if def_['kind'] == 'fill':
                        fill_mode = conf['fill_mode']
                        if fill_mode not in ['Forward', 'Bidirectional', 'NearestNeighbor']:
                            fill_mode = 'Bidirectional'
                            print("WARN: fill_mode not recognized. Please check your config file.")
                        if conf['fill_mode'] == 'Forward':
                            pass
                        elif conf['fill_mode'] == 'Reverse':
                            pathoptimizer.reverse_path(def_['data'])
                        elif conf['fill_mode'] == 'Bidirectional':
                            pathoptimizer.fill_optimize(def_['data'], tolerance)
                        elif conf['fill_mode'] == 'NearestNeighbor':
                            pathoptimizer.optimize(def_['data'], tolerance)                            
                if not 'head' in job:
                    job['head'] = {}
                job['head']['optimized'] = tolerance
    elif type_ == 'svg':
        job = read_svg(job, conf['workspace'],
                       tolerance, optimize=optimize)
    elif type_ == 'dxf':
        job = read_dxf(job, tolerance, optimize=optimize)
    elif type_ == 'gcode':
        job = read_gcode(job, tolerance, optimize=optimize)
    else:
        print("ERROR: file type not recognized")
        raise TypeError
    if matrix:
        apply_alignment_matrix(job, matrix)
    return job

def apply_alignment_matrix(job, matrix):
    """Transform the coordinates in the job with the supplied matrix."""
    # Get the SVG-style 6-element vector from the 3x3 matrix
    mat = [matrix[0][0], matrix[1][0], matrix[0][1], matrix[1][1], matrix[0][2], matrix[1][2]]

    # Only the 'defs' list contains coordinates that need to be transformed
    defs = job['defs']

    for one_def in defs:
        if one_def['kind'] == "image":
            # TODO: raster images are not supported!
            #       The rest of the code assumes the rows of pixel data are aligned
            #       with the X axis, so handling rotation will require transforming
            #       the entire image and generating new data.
            pass
        elif one_def['kind'] == "path":
            for one_path in one_def['data']:
                for one_point in one_path:
                    matrixApply(mat, one_point)

def read_svg(svg_string, workspace, tolerance, forced_dpi=None, optimize=True):
    """Read a svg file string and convert to dba job."""
    svgReader = SVGReader(tolerance, workspace)
    res = svgReader.parse(svg_string, forced_dpi)
    # {'boundarys':b, 'dpi':d, 'lasertags':l, 'rasters':r}

    # create an dba job from res
    # TODO: reader should generate an dba job to begin with
    job = {'head':{}, 'passes':[], 'items':[], 'defs':[]}
    if 'rasters' in res:
        for raster in res['rasters']:
            job['defs'].append({"kind":"image",
                                "data":raster['data'] ,
                                "pos":raster['pos'] ,
                                "size": raster['size']})
            job['items'].append({"def":len(job['defs'])-1})

    if 'boundarys' in res:
        if 'dpi' in res:
            job['head']['dpi'] = res['dpi']
        for color,path in res['boundarys'].items():
            if optimize:
                pathoptimizer.optimize(path, tolerance)
            job['defs'].append({"kind":"path",
                                "data":path})
            job['items'].append({"def":len(job['defs'])-1, "color":color})
        if optimize:
            job['head']['optimized'] = tolerance

    if 'lasertags' in res:
        # format: [('12', '2550', '', '100', '%', ':#fff000', ':#ababab', ':#ccc999', '', '', '')]
        # sort lasertags by pass number
        # def _cmp(a, b):
        #     if a[0] < b[0]: return -1
        #     elif a[0] > b[0]: return 1
        #     else: return 0
        res['lasertags'].sort()
        # add tags ass passes
        for tag in res['lasertags']:
            if len(tag) == 11:
                idxs = []
                for colidx in range(5,10):
                    color = tag[colidx]
                    i = 0
                    for item in job['items']:
                        if 'color' in item and item['color'] == color:
                            idxs.append(i)
                        i += 1
                if "passes" not in job:
                    job["passes"] = []
                job["passes"].append({
                    "items": idxs,
                    "feedrate": tag[1],
                    "intensity": tag[3]
                })
    return job

def read_dxf(dxf_string, tolerance, optimize=True):
    """Read a dxf file string and optimize returned value."""
    dxfParser = DXFParser(tolerance)
    # second argument is the forced unit, TBI in Driverboard
    job = dxfParser.parse(dxf_string, None)
    if 'vector' in job:
        if optimize:
            vec = job['vector']
            pathoptimizer.dxf_optimize(vec['paths'], tolerance)
            vec['optimized'] = tolerance
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
        type_ = 'dba'
    elif type(job) in [str, bytes]:
        if type(job) is bytes:
            job = job.decode('utf-8')
        jobheader = job[:1024].lstrip()
        if jobheader and jobheader[0] == '{':
            type_ = 'dba'
        elif '<?xml' in jobheader and '<svg' in jobheader:
            type_ = 'svg'
        elif 'SECTION' in jobheader and 'HEADER' in jobheader:
            type_ = 'dxf'
        elif 'G0' in jobheader or 'G1' in jobheader or \
             'G00' in jobheader or 'G01' in jobheader or \
             'g0' in jobheader or 'g1' in jobheader or \
             'g00' in jobheader or 'g01' in jobheader:
            type_ = 'gcode'
        else:
            print("ERROR: Cannot figure out file type 1.")
            raise TypeError
    else:
        print("ERROR: Cannot figure out file type 2.")
        raise TypeError
    return type_
