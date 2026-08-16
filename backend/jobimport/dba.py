"""The .dba job format itself: writing it out, and the payload sharing that
keeps a sheet of copies from storing the same picture once per copy.
"""

import json

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
