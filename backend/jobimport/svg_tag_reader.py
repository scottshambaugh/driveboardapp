__author__ = "Stefan Hechenberger <stefan@nortd.com>"

import hashlib
import logging
import re

from .svg_attribute_reader import SVGAttributeReader
from .svg_path_reader import SVGPathReader
from .utilities import matrixMult

# from PIL import Image

log = logging.getLogger("svg_reader")


class SVGTagReader:
    def __init__(self, svgreader):
        self._svgreader = svgreader
        # init helper for attribute reading
        self._attribReader = SVGAttributeReader(svgreader)
        # init helper for path handling
        self._pathReader = SVGPathReader(svgreader)

        self._handlers = {
            "g": self.g,
            "path": self.path,
            "polygon": self.polygon,
            "polyline": self.polyline,
            "rect": self.rect,
            "line": self.line,
            "circle": self.circle,
            "ellipse": self.ellipse,
            "image": self.image,
            "use": self.use,
            "symbol": self.symbol,
            "defs": self.defs,
            "style": self.style,
            "text": True,  # text is special, see read_tag func
        }

        self.re_findall_lasertags = re.compile(
            r"=pass([0-9]+):([0-9]*)(mm/min)?:([0-9]*)(%)?(:#[a-fA-F0-9]{6})?(:#[a-fA-F0-9]{6})?(:#[a-fA-F0-9]{6})?(:#[a-fA-F0-9]{6})?(:#[a-fA-F0-9]{6})?(:#[a-fA-F0-9]{6})?="
        ).findall

    def read_tag(self, tag, node):
        """Read a tag.

        Any tag name that is in self._handlers will be handled.
        Similarly any attribute name in self._attribReader._handlers
        will be parsed. Both tag and attribute results are stored in
        node.

        Any path data is ultimately handled by
        self._pathReader.add_path(...). For any  geometry that is not
        already in the 'd' attribute of a 'path' tag this class
        converts it first to this format and then delegates it to
        add_path(...).

        """
        tagName = self._get_tag(tag)
        if tagName in self._handlers:
            # log.debug("reading tag: " + tagName)
            # parse own attributes and overwrite in node
            # Parse style attribute LAST so inline styles take precedence
            # over presentation attributes (CSS specificity rules)
            style_value = None
            for attr, value in list(tag.attrib.items()):
                # log.debug("considering attrib: " + attr)
                if attr == "style" or attr.endswith("}style"):
                    style_value = value
                else:
                    self._attribReader.read_attrib(node, attr, value)
            # Now parse style to override presentation attributes
            if style_value is not None:
                self._attribReader.read_attrib(node, "style", style_value)
            # accumulate transformations
            node["xformToWorld"] = matrixMult(node["xformToWorld"], node["xform"])
            # read tag
            if tagName != "text":
                self._handlers[tagName](node)
            else:
                self.find_cut_settings_tags(tag, node)

    def has_handler(self, tag):
        tagName = self._get_tag(tag)
        return bool(tagName in self._handlers)

    def g(self, node):
        # http://www.w3.org/TR/SVG11/struct.html#Groups
        # has transform and style attributes
        pass

    def path(self, node):
        # http://www.w3.org/TR/SVG11/paths.html
        # has transform and style attributes
        d = node.get("d")
        if self._has_valid_stroke(node):
            self._pathReader.add_path(d, node)
        if self._has_valid_fill(node):
            self._pathReader.add_path(d, node, color=node.get("fill"))
        if d and self._has_pattern_fill(node):
            self._queue_pattern_fill(node, d)

    def polygon(self, node):
        # http://www.w3.org/TR/SVG11/shapes.html#PolygonElement
        # has transform and style attributes
        d = ["M"] + node["points"] + ["z"]
        node["points"] = None
        if self._has_valid_stroke(node):
            self._pathReader.add_path(d, node)
        if self._has_valid_fill(node):
            self._pathReader.add_path(d, node, color=node.get("fill"))
        if self._has_pattern_fill(node):
            self._queue_pattern_fill(node, d)

    def polyline(self, node):
        # http://www.w3.org/TR/SVG11/shapes.html#PolylineElement
        # has transform and style attributes
        d = ["M"] + node["points"]
        node["points"] = None
        if self._has_valid_stroke(node):
            self._pathReader.add_path(d, node)
        if self._has_valid_fill(node):
            self._pathReader.add_path(d, node, color=node.get("fill"))
        if self._has_pattern_fill(node):
            self._queue_pattern_fill(node, d)

    def rect(self, node):
        # http://www.w3.org/TR/SVG11/shapes.html#RectElement
        # has transform and style attributes
        has_stroke = self._has_valid_stroke(node)
        has_fill = self._has_valid_fill(node)
        has_pattern = self._has_pattern_fill(node)
        if not has_stroke and not has_fill and not has_pattern:
            return
        w = node.get("width") or 0.0
        h = node.get("height") or 0.0
        x = node.get("x") or 0.0
        y = node.get("y") or 0.0
        rx = node.get("rx")
        ry = node.get("ry")
        if rx is None and ry is None:  # no rounded corners
            d = ["M", x, y, "h", w, "v", h, "h", -w, "z"]
        else:  # rounded corners
            if rx is None:
                rx = ry
            elif ry is None:
                ry = rx
            if rx > w / 2.0:
                rx = w / 2.0
            if ry > h / 2.0:
                ry = h / 2.0
            if rx < 0.0:
                rx *= -1
            if ry < 0.0:
                ry *= -1
            d = [
                "M",
                x + rx,
                y,
                "h",
                w - 2 * rx,
                "c",
                rx,
                0.0,
                rx,
                ry,
                rx,
                ry,
                "v",
                h - 2 * ry,
                "c",
                0.0,
                ry,
                -rx,
                ry,
                -rx,
                ry,
                "h",
                -w + 2 * rx,
                "c",
                -rx,
                0.0,
                -rx,
                -ry,
                -rx,
                -ry,
                "v",
                -h + 2 * ry,
                "c",
                0.0,
                0.0,
                0.0,
                -ry,
                rx,
                -ry,
                "z",
            ]
        if has_stroke:
            self._pathReader.add_path(d, node)
        if has_fill:
            self._pathReader.add_path(d, node, color=node.get("fill"))
        if has_pattern:
            self._queue_pattern_fill(node, d)

    def line(self, node):
        # http://www.w3.org/TR/SVG11/shapes.html#LineElement
        # has transform and style attributes
        # Note: lines can't really have fills, but we check anyway for consistency
        has_stroke = self._has_valid_stroke(node)
        has_fill = self._has_valid_fill(node)
        if not has_stroke and not has_fill:
            return
        x1 = node.get("x1") or 0.0
        y1 = node.get("y1") or 0.0
        x2 = node.get("x2") or 0.0
        y2 = node.get("y2") or 0.0
        d = ["M", x1, y1, "L", x2, y2]
        if has_stroke:
            self._pathReader.add_path(d, node)
        if has_fill:
            self._pathReader.add_path(d, node, color=node.get("fill"))

    def circle(self, node):
        # http://www.w3.org/TR/SVG11/shapes.html#CircleElement
        # has transform and style attributes
        has_stroke = self._has_valid_stroke(node)
        has_fill = self._has_valid_fill(node)
        has_pattern = self._has_pattern_fill(node)
        if not has_stroke and not has_fill and not has_pattern:
            return
        r = node.get("r") or 0.0
        cx = node.get("cx") or 0.0
        cy = node.get("cy") or 0.0
        if r > 0.0:
            d = [
                "M",
                cx - r,
                cy,
                "A",
                r,
                r,
                0.0,
                0.0,
                0.0,
                cx,
                cy + r,
                "A",
                r,
                r,
                0.0,
                0.0,
                0.0,
                cx + r,
                cy,
                "A",
                r,
                r,
                0.0,
                0.0,
                0.0,
                cx,
                cy - r,
                "A",
                r,
                r,
                0.0,
                0.0,
                0.0,
                cx - r,
                cy,
                "Z",
            ]
            if has_stroke:
                self._pathReader.add_path(d, node)
            if has_fill:
                self._pathReader.add_path(d, node, color=node.get("fill"))
            if has_pattern:
                self._queue_pattern_fill(node, d)

    def ellipse(self, node):
        # has transform and style attributes
        has_stroke = self._has_valid_stroke(node)
        has_fill = self._has_valid_fill(node)
        has_pattern = self._has_pattern_fill(node)
        if not has_stroke and not has_fill and not has_pattern:
            return
        rx = node.get("rx") or 0.0
        ry = node.get("ry") or 0.0
        cx = node.get("cx") or 0.0
        cy = node.get("cy") or 0.0
        if rx > 0.0 and ry > 0.0:
            d = [
                "M",
                cx - rx,
                cy,
                "A",
                rx,
                ry,
                0.0,
                0.0,
                0.0,
                cx,
                cy + ry,
                "A",
                rx,
                ry,
                0.0,
                0.0,
                0.0,
                cx + rx,
                cy,
                "A",
                rx,
                ry,
                0.0,
                0.0,
                0.0,
                cx,
                cy - ry,
                "A",
                rx,
                ry,
                0.0,
                0.0,
                0.0,
                cx - rx,
                cy,
                "Z",
            ]
            if has_stroke:
                self._pathReader.add_path(d, node)
            if has_fill:
                self._pathReader.add_path(d, node, color=node.get("fill"))
            if has_pattern:
                self._queue_pattern_fill(node, d)

    def image(self, node):
        # has transform and style attributes
        data = node.get("href")
        x = node.get("x") or 0
        y = node.get("y") or 0
        width = node.get("width") or 0
        height = node.get("height") or 0

        if width <= 0 or height <= 0:
            return

        if data is None:
            log.error("image tag skipped: no href attribute found")
            return

        if not data.startswith("data:image/"):
            # only the svg content reaches here, never the file it came from,
            # so a href has no base path to resolve against
            log.error("image tag skipped: only embedded images are supported, not linked ones")
            return

        # Clean base64 data: remove whitespace that may have been introduced
        # by XML entity encoding (e.g., &#10; converted to newlines)
        # Split at comma to preserve the data URI prefix
        prefix, _, b64data = data.partition(",")
        if b64data:
            # Remove all whitespace from base64 portion
            b64data = "".join(b64data.split())
            image = prefix + "," + b64data
        else:
            image = data

        # one image is often placed many times over, so every copy shares a
        # single string and a single hash of it
        uris = self._svgreader._image_uris
        entry = uris.get(image)
        if entry is None:
            entry = (image, hashlib.md5(image.encode("utf-8")).hexdigest())
            uris[image] = entry
        image, source = entry

        raster = {}
        raster["pos"] = [x, y]
        raster["size"] = [width, height]
        # raster['image'] = converted_image
        raster["data"] = image
        raster["source"] = source
        # kept so the original survives clip cropping, same string until then
        raster["source_data"] = image
        node["rasters"].append(raster)

    def use(self, node):
        # http://www.w3.org/TR/SVG11/struct.html#UseElement
        # the referenced element is resolved and traversed by the reader,
        # here only the attributes matter (href, x, y, transform, style)
        pass

    def symbol(self, node):
        # http://www.w3.org/TR/SVG11/struct.html#SymbolElement
        # a container, only drawn where a 'use' tag references it
        pass

    def defs(self, node):
        # http://www.w3.org/TR/SVG11/struct.html#Head
        # a container, its content is only drawn where referenced
        # gradients and patterns in here stay unsupported
        log.debug("'defs' content is only drawn where a 'use' tag references it")

    def style(self, node):
        # not supported: embedded style sheets
        # http://www.w3.org/TR/SVG11/styling.html#StyleElement
        # instead presentation attributes and the 'style' attribute
        log.debug(
            "'style' tag is not supported, use presentation attributes or the style attribute instead"
        )

    def find_cut_settings_tags(self, tag, node):
        # Parse special text used for setting lasersaur cut
        # parameters from within the SVG file.
        # Any text in the SVG file within a 'text' tag (and one level deep)
        # with the following format gets read.
        # =pass1:550mm/min:90%:#ff0000=
        # =pass2:550:90:#00ff00:#ffff00:#000000=
        # =pass3:1200mm/min:80%:#00000=
        # =pass4:1200mm/min:80%=
        # =pass5:4000mm/min:100%=
        # =pass6:4000:100=
        text_accum = [tag.text or ""]
        # # search one level deep
        for child in tag:
            text_accum.append(child.text or "")
        text_accum = " ".join(text_accum)
        matches = self.re_findall_lasertags(text_accum)
        # Something like: =pass12:2550:100%:#fff000:#ababab:#ccc999=
        # Results in: [('12', '2550', '', '100', '%', ':#fff000', ':#ababab', ':#ccc999', '', '', '')]
        # convert values to actual numbers
        for i in range(len(matches)):
            vals = list(matches[i])
            # pass
            vals[0] = int(vals[0])
            # feedrate
            if vals[1]:
                vals[1] = int(vals[1])
            # intensity
            if vals[3]:
                vals[3] = int(vals[3])
            # colors, strip leading column
            for ii in range(5, 11):
                vals[ii] = vals[ii][1:]
            matches[i] = vals
        # store in the following format
        # [(12, 2550, '', 100, '%', '#fff000', '#ababab', '#ccc999', '', '', '')]
        node["lasertags"] = matches

    def _get_tag(self, domNode):
        """Get tag name without possible namespace prefix."""
        tag = domNode.tag
        return tag[tag.rfind("}") + 1 :]

    @staticmethod
    def _is_shown(node):
        """Whether the element is painted at all, going by display, visibility
        and opacity."""
        display = node.get("display")
        visibility = node.get("visibility")
        opacity = node.get("opacity")
        return bool(
            display
            and display != "none"
            and visibility
            and visibility != "hidden"
            and visibility != "collapse"
            and opacity
            and opacity != 0.0
        )

    def _has_pattern_fill(self, node):
        """Whether the fill is a reference to a pattern this reader can tile.

        Gradients and unknown ids come through the same url(#id) syntax and
        leave the shape unpainted.
        """
        ref = node.get("fill-ref")
        if not ref or not self._is_shown(node):
            return False
        fill_opacity = node.get("fill-opacity")
        if not fill_opacity:
            return False
        if not self._svgreader.is_pattern(ref):
            log.warn(f"fill url(#{ref}) is not a pattern, shape left unpainted")
            return False
        return True

    def _queue_pattern_fill(self, node, d):
        """Hand the shape outline to the reader, which tiles the pattern into
        it once the element's frame and clips are known."""
        node["fill_patterns"].append({"ref": node["fill-ref"], "d": list(d)})

    def _has_valid_stroke(self, node):
        # http://www.w3.org/TR/SVG11/styling.html#SVGStylingProperties
        display = node.get("display")
        visibility = node.get("visibility")
        stroke_color = node.get("stroke")
        stroke_opacity = node.get("stroke-opacity")
        color = node.get("color")
        opacity = node.get("opacity")
        return bool(
            display
            and display != "none"
            and visibility
            and visibility != "hidden"
            and visibility != "collapse"
            and stroke_color
            and stroke_color[0] == "#"
            and stroke_opacity
            and stroke_opacity != 0.0
            and color
            and color[0] == "#"
            and opacity
            and opacity != 0.0
        )

    def _has_valid_fill(self, node):
        # Check if node has a valid (non-transparent) fill
        # http://www.w3.org/TR/SVG11/styling.html#SVGStylingProperties
        display = node.get("display")
        visibility = node.get("visibility")
        fill_color = node.get("fill")
        fill_opacity = node.get("fill-opacity")
        opacity = node.get("opacity")
        return bool(
            display
            and display != "none"
            and visibility
            and visibility != "hidden"
            and visibility != "collapse"
            and fill_color
            and fill_color[0] == "#"
            and fill_opacity
            and fill_opacity != 0.0
            and opacity
            and opacity != 0.0
        )
