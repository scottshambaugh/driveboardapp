"""Reading job files into the .dba job format, and writing it back out.

The work lives in the modules beside this one: converter for the file kinds
and the pipeline over them, dedupe for stacked copies, dba for the format
itself. This only names what the rest of the app uses.
"""

from .converter import (
    apply_alignment_matrix,
    convert,
    get_type,
    optimize_job,
    read_dxf,
    read_gcode,
    read_svg,
)
from .dba import COORD_DECIMALS, dumps, resolve_image_data, share_image_data
from .dedupe import dedupe_job

__author__ = "Stefan Hechenberger <stefan@nortd.com>"

__all__ = [
    "COORD_DECIMALS",
    "apply_alignment_matrix",
    "convert",
    "dedupe_job",
    "dumps",
    "get_type",
    "optimize_job",
    "read_dxf",
    "read_gcode",
    "read_svg",
    "resolve_image_data",
    "share_image_data",
]
