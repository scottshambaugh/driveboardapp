__author__ = "Stefan Hechenberger <stefan@nortd.com>"

import base64
import io
import logging
import math

from .svg_tag_reader import SVGTagReader
from .svg_text_converter import convert_text_to_paths, get_conversion_warnings
from .utilities import matrixApply, matrixMult, parseFloats, parseScalar, vertexScale

try:
    from PIL import Image
except ImportError:
    Image = None

logging.basicConfig()
log = logging.getLogger("svg_reader")
# log.setLevel(logging.DEBUG)
log.setLevel(logging.INFO)
# log.setLevel(logging.WARN)


try:
    import xml.etree.ElementTree as ET
except ImportError:
    print(log.warn("Using non-C (slow) XML parser."))
    import xml.etree.ElementTree as ET


# SVG parser for the Lasersaur.
# Converts SVG DOM to a flat collection of paths.
#
# Copyright (c) 2011 Nortd Labs
# Open Source by the terms of the Gnu Public License (GPL3) or higher.
#
# Code inspired by cake.js, canvg.js, svg2obj.py, and Squirtle.
# Thank you for open sourcing your work!
#
# Usage:
# boundarys = SVGReader.parse(svgstring, config)
#
# Features:
#   * <svg> width and height, viewBox clipping.
#   * paths, rectangles, ellipses, circles, lines, polylines and polygons
#   * nested transforms
#   * transform lists (transform="rotate(30) translate(2,2) scale(4)")
#   * non-pixel units (cm, mm, in, pt, pc)
#   * 'style' attribute and presentation attributes
#   * curves, arcs, cirles, ellipses tesellated according to tolerance
#   * raster images
#   * text (automatically converted to paths using system fonts)
#   * 'use' tags referencing an element of the same document
#
# Intentionally not Supported:
#   * markers
#   * masking
#   * em, ex, % units
#   * style sheets
#
# ToDo:
#   * check for out of bounds geometry


class SVGReader:
    """SVG parser.

    Usage:
    reader = SVGReader(0.08, [1220,610])
    boundarys = reader.parse(open('filename').read())
    """

    # how far 'use' tags may reference each other before the chain is dropped,
    # also what keeps a self referencing chain from recursing forever
    MAX_USE_DEPTH = 10

    def __init__(self, tolerance, target_size):
        # parsed path data, paths by color
        # {'#ff0000': [[[x,y], [x,y], ...], [], ..], '#0000ff':[]}
        # Each path is a list of vertices which is a list of two floats.
        self.boundarys = {}

        # the conversion factor to physical dimensions
        # applied to all coordinates in the SVG
        self.px2mm = None

        # what the svg size (typically page dimensions) should be mapped to
        self._target_size = target_size

        # tolerance settings, used in tessalation, path simplification, etc
        self.tolerance = tolerance
        self.tolerance2 = tolerance**2
        self.tolerance2_half = (0.5 * tolerance) ** 2
        self.tolerance2_px = None

        # init helper object for tag reading
        self._tagReader = SVGTagReader(self)

        # lasersaur cut setting from SVG file
        # list of triplets ... [(pass#, key, value), ...]
        # pass# designates the pass this lasertag controls
        # key is the kind of setting (one of: intensity, feedrate, color)
        # value is the actual value to use
        self.lasertags = []

        # # tags that should not be further traversed
        # self.ignore_tags = {'defs':None, 'pattern':None, 'clipPath':None}

        self.rasters = []

        # rectangular clipPaths, by id: {"corners": [[x,y],[x2,y2]], "local": xform}
        # (clipPath elements are not otherwise traversed; collected up front)
        self._clip_rects = {}

        # every element that carries an id, for resolving 'use' references
        self._elements_by_id = {}

        # per parse image caches, see _decode_image and _apply_image_ops
        self._image_uris = {}
        self._decoded_images = {}
        self._decoded_px = 0
        self._derived_images = {}

    @staticmethod
    def _encode_image_data(img, header):
        """Re-encode a PIL image into a base64 data URI.

        JPEG is kept only for a fully opaque JPEG source. Everything else goes
        out as PNG, since JPEG flattens transparency to black, which the
        engraver burns at full power.
        """
        opaque = img.mode not in ("RGBA", "LA", "PA") or img.getchannel("A").getextrema()[0] == 255
        if opaque and ("jpeg" in header.lower() or "jpg" in header.lower()):
            img_format = "JPEG"
            if img.mode not in ("RGB", "L", "CMYK"):
                img = img.convert("RGB")
        else:
            img_format = "PNG"
            header = "data:image/png;base64"

        buffer = io.BytesIO()
        img.save(buffer, format=img_format)
        return header + "," + base64.b64encode(buffer.getvalue()).decode("utf-8")

    # ceiling on decoded pixels kept in the cache below, so a document full of
    # distinct large images cannot pile them all up in memory at once
    _decode_cache_px_max = 32000000

    def _decode_image(self, data_uri, to_rgba):
        """Decode a data URI, returning (header, image).

        One embedded image is commonly placed many times over, so decodes are
        cached for the parse and callers treat the image as read only. `to_rgba`
        forces an alpha channel, otherwise only the modes the ops cannot work in
        are converted.
        """
        key = (data_uri, to_rgba)
        entry = self._decoded_images.get(key)
        if entry is not None:
            self._decoded_images[key] = self._decoded_images.pop(key)
            return entry

        header, b64data = data_uri.split(",", 1)
        img = Image.open(io.BytesIO(base64.b64decode(b64data)))
        if to_rgba or img.mode in ("P", "1", "L", "LA", "PA"):
            img = img.convert("RGBA")
        else:
            img.load()
        entry = (header, img)
        self._decoded_images[key] = entry
        self._decoded_px += img.width * img.height
        while len(self._decoded_images) > 1 and self._decoded_px > self._decode_cache_px_max:
            dropped = self._decoded_images.pop(next(iter(self._decoded_images)))[1]
            self._decoded_px -= dropped.width * dropped.height
        return entry

    # ceiling on the pixel count of a resampled image, so an extreme skew
    # cannot blow up memory (24 megapixels is past any engravable detail)
    _resample_px_max = 24000000

    def _plan_resample(self, src_size, origin, u, v, pos, size):
        """Work out the output size and inverse map for resampling a rotated or
        skewed image onto the axis-aligned mm box (`pos`, `size`).

        `origin`, `u` and `v` say where the image lands in mm. `origin` is its
        (0,0) pixel corner, `u` the vector along a pixel row and `v` the vector
        down a pixel column. The parts of the box the image does not cover come
        out transparent, so they never engrave.

        Returns (out_w, out_h, affine), or None if the placement is degenerate.
        """
        src_w, src_h = src_size
        det = u[0] * v[1] - v[0] * u[1]
        if not det or not src_w or not src_h:
            return None

        # keep the finer of the two source pixel densities so rotating
        # cannot soften the image, then clamp the resulting pixel count
        density = max(src_w / math.hypot(*u), src_h / math.hypot(*v))
        out_w = max(1, round(size[0] * density))
        out_h = max(1, round(size[1] * density))
        if out_w * out_h > self._resample_px_max:
            shrink = math.sqrt(self._resample_px_max / (out_w * out_h))
            out_w = max(1, int(out_w * shrink))
            out_h = max(1, int(out_h * shrink))

        # Image.transform wants the inverse map, output pixel to source pixel
        sx = size[0] / out_w
        sy = size[1] / out_h
        ox = pos[0] - origin[0]
        oy = pos[1] - origin[1]
        affine = (
            src_w * v[1] * sx / det,
            -src_w * v[0] * sy / det,
            src_w * (v[1] * ox - v[0] * oy) / det,
            -src_h * u[1] * sx / det,
            src_h * u[0] * sy / det,
            src_h * (u[0] * oy - u[1] * ox) / det,
        )
        return out_w, out_h, affine

    def _resolve_image_ops(self, src_size, ops):
        """Pin the queued operations to pixels for an image of `src_size`.

        Clips arrive as fractions of the image extent, so turning them into
        integer boxes here is also what lets two placements that land on the
        very same pixels share one decode, transform and encode.

        Returns a tuple of resolved ops, with the ones that do nothing dropped.
        """
        w, h = src_size
        resolved = []
        for op in ops:
            if op[0] == "reorient":
                if op[1]:  # transpose swaps the axes
                    w, h = h, w
                resolved.append(op)
            elif op[0] == "resample":
                plan = self._plan_resample((w, h), *op[1:])
                if plan is None:
                    continue
                w, h = plan[0], plan[1]
                resolved.append(("resample",) + plan)
            else:
                box = (
                    max(0, min(w, round(op[1] * w))),
                    max(0, min(h, round(op[2] * h))),
                    max(0, min(w, round(op[3] * w))),
                    max(0, min(h, round(op[4] * h))),
                )
                if box[2] <= box[0] or box[3] <= box[1]:
                    continue
                w, h = box[2] - box[0], box[3] - box[1]
                resolved.append(("crop", box))
        return tuple(resolved)

    @staticmethod
    def _run_image_ops(img, resolved):
        """Apply resolved ops to an image, returning a new image."""
        for op in resolved:
            if op[0] == "reorient":
                if op[1]:
                    img = img.transpose(Image.Transpose.TRANSPOSE)
                if op[2]:
                    img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                if op[3]:
                    img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            elif op[0] == "resample":
                img = img.transform(
                    (op[1], op[2]), Image.Transform.AFFINE, op[3], resample=Image.BICUBIC
                )
            else:
                img = img.crop(op[1])
        return img

    def _apply_image_ops(self, data_uri, ops):
        """Bake the operations queued by placement and clipping into the pixels.

        The whole chain runs off a single decode and ends in a single encode,
        and the result is cached, so repeated placements of one image cost one
        pass between them.

        Returns a new data URI, or the original if there is nothing to do or
        processing fails. Resampling always goes out as PNG so the alpha
        channel it introduces survives.
        """
        if not ops or "," not in data_uri:
            return data_uri
        try:
            to_rgba = any(op[0] == "resample" for op in ops)
            header, img = self._decode_image(data_uri, to_rgba)
            resolved = self._resolve_image_ops(img.size, ops)
            if not resolved:
                return data_uri
            key = (data_uri, resolved)
            cached = self._derived_images.get(key)
            if cached is not None:
                return cached
            if to_rgba:
                header = "data:image/png;base64"
            result = self._encode_image_data(self._run_image_ops(img, resolved), header)
        except Exception as e:
            log.warning(f"Failed to transform image: {e}")
            return data_uri
        self._derived_images[key] = result
        return result

    def _place_raster(self, raster, mat):
        """Place a raster in mm space, given its element's transform to world.

        The engraver only runs axis-aligned images, so any rotation or skew has
        to be baked into the pixel data instead. Quarter turns are exact
        transposes, anything else is resampled onto the transform's bounding
        box. The pixel work is queued in `raster['ops']` for _render_raster. On
        return `raster['pos']` is the top-left mm corner and `raster['size']`
        the positive mm extent.
        """
        # where the image's box lands in mm: its (0,0) pixel corner, plus the
        # vectors along a pixel row (local +x) and down a pixel column (local +y)
        origin = [raster["pos"][0], raster["pos"][1]]
        matrixApply(mat, origin)
        vertexScale(origin, self.px2mm)
        w, h = raster["size"]
        u = [mat[0] * w * self.px2mm, mat[1] * w * self.px2mm]
        v = [mat[2] * h * self.px2mm, mat[3] * h * self.px2mm]

        # axis-aligned bounding box of the placed parallelogram
        xs = (origin[0], origin[0] + u[0], origin[0] + u[0] + v[0], origin[0] + v[0])
        ys = (origin[1], origin[1] + u[1], origin[1] + u[1] + v[1], origin[1] + v[1])
        raster["pos"] = [min(xs), min(ys)]
        raster["size"] = [max(xs) - min(xs), max(ys) - min(ys)]

        # the pixel grid has to come out with rows along x and columns down y
        tol = 1e-9 * (math.hypot(*u) + math.hypot(*v))
        upright = abs(u[1]) <= tol and abs(v[0]) <= tol
        quarter_turn = abs(u[0]) <= tol and abs(v[1]) <= tol
        if upright:
            # already the right way round, so only mirroring can be left
            transpose, flip_h, flip_v = False, u[0] < 0, v[1] < 0
        elif quarter_turn:
            # rows run along y and columns along x, so the grid transposes and
            # the flips then point each axis the right way round
            transpose, flip_h, flip_v = True, v[0] < 0, u[1] < 0
        else:
            transpose = flip_h = flip_v = False

        if not raster["data"]:
            return
        if Image is None:
            if not upright or flip_h or flip_v:
                log.warning("rotated or mirrored image left as is, Pillow is missing")
            return

        if not (upright or quarter_turn):
            # arbitrary rotation or skew, so resample onto the bounding box
            raster.setdefault("ops", []).append(
                (
                    "resample",
                    tuple(origin),
                    tuple(u),
                    tuple(v),
                    tuple(raster["pos"]),
                    tuple(raster["size"]),
                )
            )
        elif transpose or flip_h or flip_v:
            raster.setdefault("ops", []).append(("reorient", transpose, flip_h, flip_v))

    def _render_raster(self, raster):
        """Bake the pixel operations queued by placement and clipping."""
        ops = tuple(raster.pop("ops", ()))
        if ops:
            raster["data"] = self._apply_image_ops(raster["data"], ops)

    def _prescan_clip_rects(self, root):
        """Collect rectangular clipPaths (a single child <rect>) keyed by id.

        clipPath elements have no tag handler, so they are never visited during
        the normal traversal; gather the supported (rect) ones up front.
        """
        ar = self._tagReader._attribReader
        for el in root.iter():
            if self._tagReader._get_tag(el) != "clipPath":
                continue
            cid = el.get("id")
            if not cid:
                continue
            rects = [c for c in el if self._tagReader._get_tag(c) == "rect"]
            if len(rects) != 1:
                continue  # only single-rect clips are supported for now
            r = rects[0]
            x = ar._parseUnit(r.get("x") or "0") or 0.0
            y = ar._parseUnit(r.get("y") or "0") or 0.0
            w = ar._parseUnit(r.get("width") or "0") or 0.0
            h = ar._parseUnit(r.get("height") or "0") or 0.0
            if w <= 0 or h <= 0:
                continue
            tmp = {"xform": [1, 0, 0, 1, 0, 0]}
            tx = r.get("transform")
            if tx:
                ar.transformAttrib(tmp, "transform", tx)
            self._clip_rects[cid] = {
                "corners": [[x, y], [x + w, y + h]],
                "local": tmp["xform"],
            }

    def _prescan_ids(self, root):
        """Map every id to its element so 'use' references resolve.

        The first element wins when an id repeats, which matches how a browser
        resolves a duplicate id.
        """
        for el in root.iter():
            eid = el.get("id")
            if eid and eid not in self._elements_by_id:
                self._elements_by_id[eid] = el

    def _clip_box_mm(self, clip):
        """Axis-aligned mm bounding box of a clip context's rect, after applying
        the rect's own transform, the referencing element's frame, and px2mm."""
        corners_xy = clip["rect"]["corners"]
        local = clip["rect"]["local"]
        frame = clip["frame"]
        (x0, y0), (x1, y1) = corners_xy
        xs, ys = [], []
        for px, py in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            p = [px, py]
            matrixApply(local, p)  # rect's own transform
            matrixApply(frame, p)  # clipping element's frame
            vertexScale(p, self.px2mm)  # to mm
            xs.append(p[0])
            ys.append(p[1])
        return min(xs), min(ys), max(xs), max(ys)

    def _apply_clips(self, raster, clips):
        """Shrink raster in-place to the intersection of its clip boxes and
        image box, queuing the matching crop in `raster['ops']`. Returns False
        if the clip removes the image entirely (drop it)."""
        px0, py0 = raster["pos"][0], raster["pos"][1]
        pw, ph = raster["size"][0], raster["size"][1]
        ix0, iy0, ix1, iy1 = px0, py0, px0 + pw, py0 + ph
        for clip in clips:
            cx0, cy0, cx1, cy1 = self._clip_box_mm(clip)
            ix0, iy0 = max(ix0, cx0), max(iy0, cy0)
            ix1, iy1 = min(ix1, cx1), min(iy1, cy1)
        if ix1 <= ix0 or iy1 <= iy0:
            return False  # nothing left
        if ix0 <= px0 and iy0 <= py0 and ix1 >= px0 + pw and iy1 >= py0 + ph:
            return True  # clip contains the image; no crop
        if Image is not None and raster["data"]:
            raster.setdefault("ops", []).append(
                (
                    "crop",
                    (ix0 - px0) / pw,
                    (iy0 - py0) / ph,
                    (ix1 - px0) / pw,
                    (iy1 - py0) / ph,
                )
            )
        raster["pos"] = [ix0, iy0]
        raster["size"] = [ix1 - ix0, iy1 - iy0]
        return True

    def parse(self, svgstring, force_dpi=None, require_unit=False):
        """Parse a SVG document.

        This traverses through the document tree and collects all path
        data and converts it to polylines of the requested tolerance.

        Path data is returned as paths by color:
        {'#ff0000': [[path0, path1, ..], [path0, ..], ..]}
        Each path is a list of vertices which is a list of two floats.

        Determining Physical Dimensions
        -------------------------------
        SVG files may use physical units (mm, in) or screen units (px).
        For obvious reason former are preferred as these take out any
        guess-work of how to interpret any coordinates.

        A good SVG authoring app writes physical dimensions to file like this:
        - the svg tag has a width, height, viewBox attribute
        - width and height contains the page dimensions and unit
        - viewBox defines a rectangle with (x, y, width, height)
        - width/viewBox:width is the factor that needs to be applied to
          any (unit-less) coordinates in the file
        - x,y is a translation that needs to be applied to any coordinates

        One issue with svg documents is that they are not always clear on
        the physical dimensions. Often they lack or use px units in the
        width/height attributes (no units implies px units in the SVG
        standard). For example, it's possible to encounter px
        units in the file even when the authoring app interprets these
        as physical units (e.g mm). This means there is an implied DPI
        conversion in the app that we need to guess/know.

        The following strategy is used to get physical dimensions:

        1. from argument (force_dpi)
        2. from units of svg width/height and viewBox
        3. from hints of (known) originating apps
        4. from ratio of page and target size
        5. defaults to 90 DPI
        """
        self.px2mm = None
        self.boundarys = {}
        self.lasertags = []
        self.rasters = []
        self._image_uris = {}
        self._decoded_images = {}
        self._decoded_px = 0
        self._derived_images = {}
        self._clip_rects = {}
        self._elements_by_id = {}

        # Convert text elements to paths before parsing
        svgstring = convert_text_to_paths(svgstring)

        # parse xml
        svgRootElement = ET.fromstring(svgstring)
        tagName = self._tagReader._get_tag(svgRootElement)

        if tagName != "svg":
            log.error("Invalid file, no 'svg' tag found.")
            return self.boundarys

        # 1. Get px2mm from argument
        if force_dpi is not None:
            self.px2mm = 25.4 / force_dpi
            log.info(f"SVG import forced to {force_dpi} dpi.")

        # Get width, height, viewBox for further processing
        if not self.px2mm:
            width = None
            height = None
            vb_x = None
            vb_y = None
            vb_w = None
            vb_h = None
            unit = ""

            # get width, height, unit
            width_str = svgRootElement.attrib.get("width")
            height_str = svgRootElement.attrib.get("height")
            if width_str and height_str:
                width, width_unit = parseScalar(width_str)
                height, height_unit = parseScalar(height_str)
                if width_unit != height_unit:
                    log.error("Conflicting units found.")
                unit = width_unit
                log.info(f"SVG w,h (unit) is {width},{height} ({unit}).")

            # get viewBox
            # http://www.w3.org/TR/SVG11/coords.html#ViewBoxAttribute
            vb = svgRootElement.attrib.get("viewBox")
            if vb:
                vb_x, vb_y, vb_w, vb_h = parseFloats(vb)
                log.info(f"SVG viewBox ({vb_x},{vb_y},{vb_w},{vb_h}).")

        # 2. Get px2mm from width, height, viewBox
        if not self.px2mm:
            if (width and height) or vb:
                if not (width and height):
                    # default to viewBox
                    width = vb_w
                    height = vb_h
                if not vb:
                    # default to width, height, and no offset
                    vb_x = 0.0
                    vb_y = 0.0
                    vb_w = width
                    vb_h = height

                self.px2mm = width / vb_w

                if unit == "mm":
                    # great, the svg file already uses mm
                    pass
                    log.info("px2mm by svg mm unit")
                elif unit == "in":
                    # prime for inch to mm conversion
                    self.px2mm *= 25.4
                    log.info("px2mm by svg inch unit")
                elif unit == "cm":
                    # prime for cm to mm conversion
                    self.px2mm *= 10.0
                    log.info("px2mm by svg cm unit")
                elif require_unit:
                    raise ValueError("Invalid or no unit in SVG data, must be 'mm', 'cm' or 'in'.")
                elif unit == "px" or unit == "":
                    # no physical units in file
                    # we have to interpret user (px) units
                    # 3. For some apps we can make a good guess.
                    svghead = svgstring[0:400]
                    if isinstance(svghead, bytes):
                        svghead = svghead.decode("utf-8", errors="ignore")
                    if "Inkscape" in svghead:
                        self.px2mm *= 25.4 / 90.0
                        log.info("SVG exported with Inkscape -> 90dpi.")
                    elif "Illustrator" in svghead:
                        self.px2mm *= 25.4 / 72.0
                        log.info("SVG exported with Illustrator -> 72dpi.")
                    elif "Intaglio" in svghead:
                        self.px2mm *= 25.4 / 72.0
                        log.info("SVG exported with Intaglio -> 72dpi.")
                    elif "CorelDraw" in svghead:
                        self.px2mm *= 25.4 / 96.0
                        log.info("SVG exported with CorelDraw -> 96dpi.")
                    elif "Qt" in svghead:
                        self.px2mm *= 25.4 / 90.0
                        log.info("SVG exported with Qt lib -> 90dpi.")
                    else:
                        # give up in this step
                        self.px2mm = None
                else:
                    log.error("SVG with unsupported unit.")
                    self.px2mm = None

        # 4. Get px2mm by the ratio of svg size to target size
        if not self.px2mm and (width and height):
            self.px2mm = self._target_size[0] / width
            log.info("px2mm by target_size/page_size ratio")

        # 5. Fall back on px unit DPIs default value
        if not self.px2mm:
            log.warn("Failed to determin physical dimensions -> defaulting to 90dpi.")
            self.px2mm = 25.4 / 90.0

        # adjust tolerances to px units
        self.tolerance2_px = (self.tolerance / self.px2mm) * (self.tolerance / self.px2mm)

        # translation from viewbox
        if vb_x:
            tx = vb_x
        else:
            tx = 0.0
        if vb_y:
            ty = vb_y
        else:
            ty = 0.0

        # let the fun begin
        # recursively parse children
        # output will be in self.boundarys
        node = {
            "xformToWorld": [1, 0, 0, 1, tx, ty],
            "display": "visible",
            "visibility": "visible",
            "fill": "#000000",
            "stroke": "#000000",
            "color": "#000000",
            "fill-opacity": 1.0,
            "stroke-opacity": 1.0,
            "opacity": 1.0,
        }
        self._prescan_clip_rects(svgRootElement)
        self._prescan_ids(svgRootElement)
        self.parse_children(svgRootElement, node)

        # build result dictionary
        parse_results = {"dpi": round(25.4 / self.px2mm)}

        if self.boundarys:
            parse_results["boundarys"] = self.boundarys

        if self.lasertags:
            parse_results["lasertags"] = self.lasertags

        if self.rasters:
            parse_results["rasters"] = self.rasters

        # Collect any warnings from text conversion
        warnings = get_conversion_warnings()
        if warnings:
            parse_results["warnings"] = warnings

        return parse_results

    def parse_children(self, domNode, parentNode, use_depth=0):
        for child in domNode:
            self.parse_element(child, parentNode, use_depth)

    def parse_element(self, child, parentNode, use_depth=0, from_use=False):
        # log.debug("considering tag: " + child.tag)
        if not self._tagReader.has_handler(child):
            return

        tagName = self._tagReader._get_tag(child)
        if tagName in ("defs", "symbol") and not from_use:
            # containers, their content is only drawn where a 'use' references it
            return

        # 1. setup a new node
        # and inherit from parent
        node = {
            "paths": [],
            "rasters": [],
            "xform": [1, 0, 0, 1, 0, 0],
            "xformToWorld": parentNode["xformToWorld"],
            "display": parentNode.get("display"),
            "visibility": parentNode.get("visibility"),
            "fill": parentNode.get("fill"),
            "stroke": parentNode.get("stroke"),
            "color": parentNode.get("color"),
            "fill-opacity": parentNode.get("fill-opacity"),
            "stroke-opacity": parentNode.get("stroke-opacity"),
            "opacity": parentNode.get("opacity"),
            # active rectangular clip contexts inherited from ancestors
            # (clip-path is not an inherited property, but the clipped
            # region still constrains descendants)
            "clips": list(parentNode.get("clips", [])),
        }

        # 2. parse child
        # with current attributes and transformation
        self._tagReader.read_tag(child, node)

        # 2b. if this element carries its own clip-path that resolves to
        # a supported rect, add it as a clip context in this element's
        # own frame (xformToWorld now includes the element's transform)
        own_clip = node.get("clip-path")
        if own_clip:
            if own_clip in self._clip_rects:
                node["clips"].append(
                    {
                        "rect": self._clip_rects[own_clip],
                        "frame": list(node["xformToWorld"]),
                    }
                )
            else:
                log.info(f"clip-path #{own_clip} is not a supported rectangular clip; ignored")

        # 3. compile boundarys + conversions
        for path_entry in node["paths"]:
            # path_entry is {'data': [...], 'color': '#...'}
            path = path_entry["data"]
            hexcolor = path_entry["color"]
            if path:  # skip if empty subpath
                # 3a.) convert to world coordinates and then to mm units
                for vert in path:
                    # print isinstance(vert[0],float) and isinstance(vert[1],float)
                    matrixApply(node["xformToWorld"], vert)
                    vertexScale(vert, self.px2mm)
                # 3b.) sort output by color
                if hexcolor in self.boundarys:
                    self.boundarys[hexcolor].append(path)
                else:
                    self.boundarys[hexcolor] = [path]

        # 4. any lasertags (cut settings)?
        if "lasertags" in node:
            self.lasertags.extend(node["lasertags"])

        # 5. Raster Data [(x, y, size, data)]
        for raster in node["rasters"]:
            # to world coordinates and mm units, baking any rotation,
            # skew or mirroring into the pixel data (pos/size come back
            # as the top-left corner and a positive extent)
            self._place_raster(raster, node["xformToWorld"])

            # Crop to any active rectangular clip-path (pos/size are now
            # the normalized top-left + positive extent in mm)
            if node["clips"] and not self._apply_clips(raster, node["clips"]):
                continue  # fully clipped away -> drop the image

            self._render_raster(raster)
            self.rasters.append(raster)

        # 6. recursive call, a 'use' draws its referenced element instead of
        # its own children
        if tagName == "use":
            self.parse_use(node, use_depth)
        else:
            self.parse_children(child, node, use_depth)

    def parse_use(self, node, use_depth):
        """Draw the element a 'use' tag references, in the frame of the tag.

        Only same document references are supported. The reference is resolved
        against the id map collected before the traversal, so it works no
        matter where the referenced element sits in the document.
        """
        if use_depth >= self.MAX_USE_DEPTH:
            log.warn("'use' tags nested too deeply, ignored")
            return
        href = node.get("href")
        if not href or not href.startswith("#"):
            log.warn("'use' tag skipped: only same document references are supported")
            return
        target = self._elements_by_id.get(href[1:])
        if target is None:
            log.warn(f"'use' tag skipped: no element with id {href[1:]}")
            return

        # x/y on the tag translate the referenced element, after its transform
        x = node.get("x") or 0
        y = node.get("y") or 0
        if x or y:
            node["xformToWorld"] = matrixMult(node["xformToWorld"], [1, 0, 0, 1, x, y])

        self.parse_element(target, node, use_depth + 1, from_use=True)


# if __name__ == "__main__":
#     # do something here when used directly
