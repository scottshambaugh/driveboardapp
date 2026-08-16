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
