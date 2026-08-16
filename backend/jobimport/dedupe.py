"""Dropping geometry a job draws twice in the same place."""

from .dba import COORD_DECIMALS


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
