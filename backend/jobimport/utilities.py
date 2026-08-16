import re

# The number grammar svg uses, see the BNF in the spec's "Basic Data Types".
# A sign may be either, the decimal point may lead or trail the digits, and an
# exponent takes either case of e and a sign of its own. Writers that shorten
# their output lean on all of it, so a pattern that only reads 1.0 style
# numbers silently misreads their coordinates.
NUMBER = r"[+-]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][+-]?\d+)?"

re_findall_floats = re.compile(NUMBER).findall
re_scalar_unit = re.compile(rf"({NUMBER})([a-z]*)").findall


def parseFloats(float_strings):
    """Convert a list of float strings to an actual list of floats.

    The function can deal with pretty much any separation chars.
    """
    float_strings = re_findall_floats(float_strings)
    for i in range(len(float_strings)):  # use index so we can edit in-place
        float_strings[i] = float(float_strings[i])
    return float_strings


def parseScalar(scalar_unit_string):
    """Parse one scalar string with (optional) unit and return both."""
    matches = re_scalar_unit(scalar_unit_string)
    if not matches:  # no number present (e.g. "auto") -> treat as missing
        return (None, "")
    num, unit = matches[0]
    num = float(num)
    return (num, unit)


# the nine alignments preserveAspectRatio can name, plus 'none' for a fit
# that stretches. Keyed by lower case so a miscased attribute still reads.
PAR_ALIGNS = {
    a.lower(): a
    for a in (
        "none",
        "xMinYMin",
        "xMidYMin",
        "xMaxYMin",
        "xMinYMid",
        "xMidYMid",
        "xMaxYMid",
        "xMinYMax",
        "xMidYMax",
        "xMaxYMax",
    )
}


def parsePreserveAspectRatio(text):
    """Read a 'preserveAspectRatio' attribute into (align, meetOrSlice).

    Anything missing or unreadable comes back as the default the spec gives,
    which is the fit every renderer falls back on. The optional leading
    'defer' only means anything on an image and is dropped here.
    """
    align, meet_or_slice = "xMidYMid", "meet"
    parts = (text or "").split()
    if parts and parts[0] == "defer":
        parts = parts[1:]
    if parts:
        align = PAR_ALIGNS.get(parts[0].lower(), align)
    if len(parts) > 1 and parts[1] in ("meet", "slice"):
        meet_or_slice = parts[1]
    return align, meet_or_slice


def viewboxFit(vb_w, vb_h, box_w, box_h, align="xMidYMid", meet_or_slice="meet"):
    """Fit a viewBox of vb_w x vb_h into a box of box_w x box_h.

    Returns (scale_x, scale_y, tx, ty). 'none' stretches to fill the box, so
    the two scales part ways there. 'meet' takes the smaller scale and leaves
    the box short in one direction, 'slice' takes the larger and runs over it,
    and the alignment says where the difference goes.
    """
    if not vb_w or not vb_h:
        return 1.0, 1.0, 0.0, 0.0
    scale_x = box_w / vb_w
    scale_y = box_h / vb_h
    if align != "none":
        if meet_or_slice == "slice":
            scale_x = scale_y = max(scale_x, scale_y)
        else:
            scale_x = scale_y = min(scale_x, scale_y)
    align = align.lower()
    x_align, y_align = align[:4], align[4:]
    tx = box_w - vb_w * scale_x
    ty = box_h - vb_h * scale_y
    tx = tx / 2 if x_align == "xmid" else (tx if x_align == "xmax" else 0.0)
    ty = ty / 2 if y_align == "ymid" else (ty if y_align == "ymax" else 0.0)
    return scale_x, scale_y, tx, ty


def viewboxMatrix(viewbox, box, align="xMidYMid", meet_or_slice="meet"):
    """The transform from viewBox coordinates into the box (x, y, w, h)."""
    vb_x, vb_y, vb_w, vb_h = viewbox
    scale_x, scale_y, tx, ty = viewboxFit(vb_w, vb_h, box[2], box[3], align, meet_or_slice)
    return [
        scale_x,
        0,
        0,
        scale_y,
        box[0] + tx - vb_x * scale_x,
        box[1] + ty - vb_y * scale_y,
    ]


def matrixMult(mA, mB):
    return [
        mA[0] * mB[0] + mA[2] * mB[1],
        mA[1] * mB[0] + mA[3] * mB[1],
        mA[0] * mB[2] + mA[2] * mB[3],
        mA[1] * mB[2] + mA[3] * mB[3],
        mA[0] * mB[4] + mA[2] * mB[5] + mA[4],
        mA[1] * mB[4] + mA[3] * mB[5] + mA[5],
    ]


def matrixApply(mat, vec):
    vec0 = mat[0] * vec[0] + mat[2] * vec[1] + mat[4]
    vec[1] = mat[1] * vec[0] + mat[3] * vec[1] + mat[5]
    vec[0] = vec0


def vertexScale(v, f):
    v[0] *= f
    v[1] *= f
