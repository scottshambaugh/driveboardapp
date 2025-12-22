"""SVG Text to Path Converter.

This module provides functionality to convert text elements in SVG files
to path elements, ensuring text is rendered correctly regardless of font
availability on the target system.

Uses fonttools for font loading and glyph-to-path conversion.
"""

import logging
import os
import platform
import re
import xml.etree.ElementTree as ET

log = logging.getLogger("svg_text_converter")

# Global list to collect warnings during conversion
# This is reset at the start of each convert_text_to_paths call
_conversion_warnings = []


def get_conversion_warnings():
    """Get the list of warnings from the last conversion."""
    return _conversion_warnings.copy()


def _add_warning(message):
    """Add a warning message."""
    if message not in _conversion_warnings:
        _conversion_warnings.append(message)
        log.warning(message)


# SVG namespace
SVG_NS = "http://www.w3.org/2000/svg"
NS_MAP = {"svg": SVG_NS}

# Register namespace to preserve svg prefix in output
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")


class FontManager:
    """Manages system font discovery and loading."""

    def __init__(self):
        self._font_cache = {}
        self._font_paths = None

    def _get_font_directories(self):
        """Get system font directories based on platform."""
        system = platform.system()
        font_dirs = []

        if system == "Linux":
            font_dirs = [
                "/usr/share/fonts",
                "/usr/local/share/fonts",
                os.path.expanduser("~/.fonts"),
                os.path.expanduser("~/.local/share/fonts"),
            ]
        elif system == "Darwin":
            font_dirs = [
                "/System/Library/Fonts",
                "/Library/Fonts",
                os.path.expanduser("~/Library/Fonts"),
            ]
        elif system == "Windows":
            windir = os.environ.get("WINDIR", "C:\\Windows")
            font_dirs = [os.path.join(windir, "Fonts")]

        return [d for d in font_dirs if os.path.exists(d)]

    def _scan_fonts(self):
        """Scan system for available fonts."""
        if self._font_paths is not None:
            return self._font_paths

        self._font_paths = {}
        # Track which simplified names have regular variants
        self._regular_names = set()
        font_dirs = self._get_font_directories()

        # Variant indicators (less preferred)
        variant_indicators = [
            "bold",
            "italic",
            "oblique",
            "condensed",
            "light",
            "thin",
            "black",
            "medium",
            "extra",
            "semi",
        ]

        def is_regular_variant(filename):
            """Check if filename suggests a regular/normal variant."""
            name_lower = filename.lower()
            for indicator in variant_indicators:
                if indicator in name_lower:
                    return False
            return True

        for font_dir in font_dirs:
            try:
                for root, _, files in os.walk(font_dir):
                    for f in files:
                        if f.lower().endswith((".ttf", ".otf")):
                            path = os.path.join(root, f)
                            # Use lowercase filename without extension as key
                            name = os.path.splitext(f)[0].lower()
                            # Also extract a simplified name
                            simple_name = re.sub(
                                r"[-_]?(regular|normal|book|roman|bold|italic|oblique|light|medium|thin|black|condensed|extra|semi).*$",
                                "",
                                name,
                                flags=re.IGNORECASE,
                            )

                            # Always store full name
                            self._font_paths[name] = path

                            # For simplified name, prefer regular variants
                            is_regular = is_regular_variant(f)
                            if simple_name != name:
                                if simple_name not in self._font_paths:
                                    # First occurrence
                                    self._font_paths[simple_name] = path
                                    if is_regular:
                                        self._regular_names.add(simple_name)
                                elif is_regular and simple_name not in self._regular_names:
                                    # Replace variant with regular
                                    self._font_paths[simple_name] = path
                                    self._regular_names.add(simple_name)
                                # else: keep existing (either it's regular already, or both are variants)
            except OSError as e:
                log.debug(f"Error scanning font directory {font_dir}: {e}")

        log.debug(f"Found {len(self._font_paths)} fonts")
        return self._font_paths

    def find_font(self, font_family):
        """Find a font file matching the font family name.

        Args:
            font_family: Font family name (e.g., 'Arial', 'sans-serif')

        Returns:
            Path to font file or None if not found
        """
        fonts = self._scan_fonts()

        # Common fallback fonts for generic families
        fallbacks = {
            "sans-serif": [
                "dejavusans",
                "arial",
                "helvetica",
                "liberationsans",
                "roboto",
                "freesans",
                "sans",
            ],
            "sans": ["dejavusans", "arial", "helvetica", "liberationsans", "roboto", "freesans"],
            "serif": ["dejavuserif", "times", "timesnewroman", "liberationserif", "freeserif"],
            "monospace": ["dejavusansmono", "courier", "couriernew", "liberationmono", "freemono"],
        }

        # Unwanted font variants - prefer regular weight/style
        unwanted_variants = [
            "bold",
            "italic",
            "oblique",
            "condensed",
            "light",
            "thin",
            "black",
            "medium",
        ]

        def is_preferred_variant(font_name):
            """Check if font name suggests it's a regular variant."""
            name_lower = font_name.lower()
            for variant in unwanted_variants:
                if variant in name_lower:
                    return False
            return True

        def find_best_match(candidates):
            """From candidates, prefer regular variants over bold/italic/etc."""
            preferred = [c for c in candidates if is_preferred_variant(c[0])]
            if preferred:
                return preferred[0][1]
            return candidates[0][1] if candidates else None

        # Normalize font family name
        family_lower = font_family.lower().replace(" ", "").replace("-", "")

        # Try exact match first
        if family_lower in fonts:
            return fonts[family_lower]

        # Try partial matches, collecting all candidates
        candidates = []
        for name, path in fonts.items():
            if family_lower in name or name in family_lower:
                candidates.append((name, path))

        if candidates:
            return find_best_match(candidates)

        # Try fallbacks for generic families (sans-serif, serif, monospace)
        # These are expected to fall back, so no warning
        generic_families = {"sans-serif", "sans", "serif", "monospace"}
        if font_family.lower() in generic_families:
            for fallback in fallbacks.get(font_family.lower(), []):
                if fallback in fonts:
                    return fonts[fallback]
                # Try partial match with preference for regular variants
                candidates = []
                for name, path in fonts.items():
                    if fallback in name:
                        candidates.append((name, path))
                if candidates:
                    return find_best_match(candidates)

        # If we get here for a non-generic font, we're falling back - warn the user
        # Last resort: return first available font (preferring regular variants)
        if fonts:
            regular_fonts = [(n, p) for n, p in fonts.items() if is_preferred_variant(n)]
            if regular_fonts:
                fallback_path = regular_fonts[0][1]
                fallback_name = regular_fonts[0][0]
            else:
                fallback_name = next(iter(fonts.keys()))
                fallback_path = fonts[fallback_name]

            # Warn if this is a specific font that wasn't found
            if font_family.lower() not in generic_families:
                _add_warning(f"Font '{font_family}' not found, using '{fallback_name}' instead")

            log.debug(f"Using fallback font: {fallback_path}")
            return fallback_path

        return None

    def load_font(self, font_path):
        """Load a font file.

        Args:
            font_path: Path to the font file

        Returns:
            TTFont object or None if loading fails
        """
        from fontTools.ttLib import TTFont

        if font_path in self._font_cache:
            return self._font_cache[font_path]

        try:
            font = TTFont(font_path)
            self._font_cache[font_path] = font
            return font
        except Exception as e:
            log.warning(f"Failed to load font {font_path}: {e}")
            return None


# Global font manager instance
_font_manager = None


def get_font_manager():
    """Get the global font manager instance."""
    global _font_manager
    if _font_manager is None:
        _font_manager = FontManager()
    return _font_manager


def get_text_width(text, font, font_size):
    """Calculate the width of text in the given font.

    Args:
        text: Text string to measure
        font: TTFont object
        font_size: Font size in user units

    Returns:
        Total width of the text
    """
    glyph_set = font.getGlyphSet()
    cmap = font.getBestCmap()
    units_per_em = font["head"].unitsPerEm
    scale = font_size / units_per_em

    width = 0
    for char in text:
        code = ord(char)
        glyph_name = cmap.get(code)

        if glyph_name is None:
            if char == " ":
                width += font_size * 0.25
            continue

        try:
            glyph = glyph_set[glyph_name]
            if hasattr(glyph, "width"):
                width += glyph.width * scale
            else:
                width += font_size * 0.6
        except Exception:
            width += font_size * 0.6

    return width


def text_to_path(text, font, font_size, x=0, y=0, text_anchor="start"):
    """Convert text string to SVG path data.

    Args:
        text: Text string to convert
        font: TTFont object
        font_size: Font size in user units
        x: X position (interpretation depends on text_anchor)
        y: Y position (baseline)
        text_anchor: "start" (left), "middle" (center), or "end" (right)

    Returns:
        SVG path 'd' attribute string
    """
    from fontTools.pens.svgPathPen import SVGPathPen

    glyph_set = font.getGlyphSet()
    cmap = font.getBestCmap()

    # Get units per em for scaling
    units_per_em = font["head"].unitsPerEm
    scale = font_size / units_per_em

    # Adjust x based on text-anchor
    if text_anchor == "middle":
        text_width = get_text_width(text, font, font_size)
        x = x - text_width / 2
    elif text_anchor == "end":
        text_width = get_text_width(text, font, font_size)
        x = x - text_width

    paths = []
    cursor_x = x

    for char in text:
        code = ord(char)
        glyph_name = cmap.get(code)

        if glyph_name is None:
            # Skip characters without glyphs (like spaces, use advance width)
            if char == " ":
                # Approximate space width
                cursor_x += font_size * 0.25
            continue

        try:
            glyph = glyph_set[glyph_name]
            pen = SVGPathPen(glyph_set)
            glyph.draw(pen)
            glyph_path = pen.getCommands()

            if glyph_path:
                # Transform the path: scale and translate
                # Font coordinates have Y going up, SVG has Y going down
                # So we need to flip Y and translate
                transformed = transform_path(glyph_path, scale, -scale, cursor_x, y)
                paths.append(transformed)

            # Advance cursor by glyph width
            if hasattr(glyph, "width"):
                cursor_x += glyph.width * scale
            else:
                cursor_x += font_size * 0.6  # Approximate
        except Exception as e:
            log.debug(f"Error rendering glyph '{char}': {e}")
            cursor_x += font_size * 0.6

    return " ".join(paths)


def transform_path(path_data, scale_x, scale_y, translate_x, translate_y):
    """Apply transformation to SVG path data.

    Args:
        path_data: SVG path 'd' attribute string
        scale_x: X scale factor
        scale_y: Y scale factor
        translate_x: X translation
        translate_y: Y translation

    Returns:
        Transformed path data string
    """
    # Parse path commands and transform coordinates
    result = []
    tokens = re.findall(r"([MmLlHhVvCcSsQqTtAaZz])|(-?[\d.]+)", path_data)

    current_cmd = None
    coords = []

    for token in tokens:
        cmd, num = token
        if cmd:
            # Process previous command
            if current_cmd:
                if coords:
                    result.append(
                        transform_command(
                            current_cmd, coords, scale_x, scale_y, translate_x, translate_y
                        )
                    )
                elif current_cmd in "Zz":
                    # Z command has no coordinates
                    result.append(current_cmd + " ")
            current_cmd = cmd
            coords = []
        elif num:
            coords.append(float(num))

    # Process last command
    if current_cmd:
        if coords:
            result.append(
                transform_command(current_cmd, coords, scale_x, scale_y, translate_x, translate_y)
            )
        elif current_cmd in "Zz":
            result.append(current_cmd + " ")

    return "".join(result)


def transform_command(cmd, coords, sx, sy, tx, ty):
    """Transform a single path command.

    Args:
        cmd: Path command letter
        coords: List of coordinate values
        sx, sy: Scale factors
        tx, ty: Translation offsets

    Returns:
        Transformed command string
    """
    result = cmd

    # Different commands have different coordinate patterns
    if cmd in "Mm":  # moveto (x, y)
        for i in range(0, len(coords), 2):
            x = coords[i] * sx + (tx if cmd == "M" else 0)
            y = coords[i + 1] * sy + (ty if cmd == "M" else 0)
            result += f"{x:.2f} {y:.2f} "
    elif cmd in "Ll":  # lineto (x, y)
        for i in range(0, len(coords), 2):
            x = coords[i] * sx + (tx if cmd == "L" else 0)
            y = coords[i + 1] * sy + (ty if cmd == "L" else 0)
            result += f"{x:.2f} {y:.2f} "
    elif cmd in "Hh":  # horizontal lineto (x)
        for coord in coords:
            x = coord * sx + (tx if cmd == "H" else 0)
            result += f"{x:.2f} "
    elif cmd in "Vv":  # vertical lineto (y)
        for coord in coords:
            y = coord * sy + (ty if cmd == "V" else 0)
            result += f"{y:.2f} "
    elif cmd in "Cc":  # curveto (x1, y1, x2, y2, x, y)
        for i in range(0, len(coords), 6):
            for j in range(0, 6, 2):
                x = coords[i + j] * sx + (tx if cmd == "C" else 0)
                y = coords[i + j + 1] * sy + (ty if cmd == "C" else 0)
                result += f"{x:.2f} {y:.2f} "
    elif cmd in "Ss":  # smooth curveto (x2, y2, x, y)
        for i in range(0, len(coords), 4):
            for j in range(0, 4, 2):
                x = coords[i + j] * sx + (tx if cmd == "S" else 0)
                y = coords[i + j + 1] * sy + (ty if cmd == "S" else 0)
                result += f"{x:.2f} {y:.2f} "
    elif cmd in "Qq":  # quadratic curveto (x1, y1, x, y)
        for i in range(0, len(coords), 4):
            for j in range(0, 4, 2):
                x = coords[i + j] * sx + (tx if cmd == "Q" else 0)
                y = coords[i + j + 1] * sy + (ty if cmd == "Q" else 0)
                result += f"{x:.2f} {y:.2f} "
    elif cmd in "Tt":  # smooth quadratic curveto (x, y)
        for i in range(0, len(coords), 2):
            x = coords[i] * sx + (tx if cmd == "T" else 0)
            y = coords[i + 1] * sy + (ty if cmd == "T" else 0)
            result += f"{x:.2f} {y:.2f} "
    elif cmd in "Aa":  # arc (rx, ry, rotation, large-arc, sweep, x, y)
        for i in range(0, len(coords), 7):
            rx = abs(coords[i] * sx)
            ry = abs(coords[i + 1] * sy)
            rotation = coords[i + 2]
            large_arc = int(coords[i + 3])
            sweep = int(coords[i + 4])
            x = coords[i + 5] * sx + (tx if cmd == "A" else 0)
            y = coords[i + 6] * sy + (ty if cmd == "A" else 0)
            result += f"{rx:.2f} {ry:.2f} {rotation:.2f} {large_arc} {sweep} {x:.2f} {y:.2f} "
    elif cmd in "Zz":
        pass  # No coordinates

    return result.strip() + " "


def convert_text_to_paths(svg_string):
    """Convert all text elements in an SVG to path elements.

    Args:
        svg_string: SVG content as bytes or string

    Returns:
        Modified SVG content with text converted to paths,
        or the original svg_string if conversion fails.
    """
    global _conversion_warnings
    _conversion_warnings = []  # Reset warnings for this conversion

    try:
        import fontTools.ttLib  # noqa: F401
    except ImportError:
        log.warning("fonttools not installed, text will not be converted to paths")
        return svg_string

    # Ensure we're working with a string
    is_bytes = isinstance(svg_string, bytes)
    if is_bytes:
        svg_str = svg_string.decode("utf-8")
    else:
        svg_str = svg_string

    try:
        # Parse the SVG
        root = ET.fromstring(svg_str)

        # Find all text elements (handle namespace)
        text_elements = []
        for elem in root.iter():
            tag = elem.tag
            if tag.endswith("}text") or tag == "text":
                text_elements.append(elem)

        if not text_elements:
            log.debug("No text elements found in SVG")
            return svg_string

        fm = get_font_manager()
        converted_count = 0

        for text_elem in text_elements:
            try:
                # Get default styling from text element
                font_family = get_style_attr(text_elem, "font-family", "sans-serif")
                font_size_str = get_style_attr(text_elem, "font-size", "16")
                font_size = parse_font_size(font_size_str)

                # Default position from text element
                default_x = float(text_elem.get("x", "0"))
                default_y = float(text_elem.get("y", "0"))

                # Get text-anchor for horizontal alignment
                text_anchor = get_style_attr(text_elem, "text-anchor", "start")

                # Find and load font
                font_path = fm.find_font(font_family)
                if not font_path:
                    log.warning(f"Could not find font for family '{font_family}'")
                    continue

                log.info(f"Converting text with font '{font_family}' -> {font_path}")

                font = fm.load_font(font_path)
                if not font:
                    continue

                # Collect path data grouped by color
                # Each entry: {'paths': [...], 'color': '#...'}
                path_groups = []

                # Default color from text element
                default_color = get_style_attr(text_elem, "fill", "#000000")
                default_color = normalize_color(default_color)

                # Process direct text content of the text element
                if text_elem.text and text_elem.text.strip():
                    path_data = text_to_path(
                        text_elem.text, font, font_size, default_x, default_y, text_anchor
                    )
                    if path_data:
                        path_groups.append({"paths": [path_data], "color": default_color})

                # Process tspan children
                for child in text_elem:
                    child_tag = child.tag
                    if child_tag.endswith("}tspan") or child_tag == "tspan":
                        # Get tspan-specific position (falls back to parent's position)
                        tspan_x = child.get("x")
                        tspan_y = child.get("y")
                        x = float(tspan_x) if tspan_x else default_x
                        y = float(tspan_y) if tspan_y else default_y

                        # Get tspan-specific font size (falls back to parent's size)
                        tspan_font_size_str = get_style_attr(child, "font-size")
                        if tspan_font_size_str:
                            tspan_font_size = parse_font_size(tspan_font_size_str)
                        else:
                            tspan_font_size = font_size

                        # Get tspan-specific color (falls back to parent's color)
                        tspan_color = get_style_attr(child, "fill")
                        if tspan_color:
                            tspan_color = normalize_color(tspan_color)
                        else:
                            tspan_color = default_color

                        # Get tspan text content
                        tspan_text = child.text or ""
                        if tspan_text.strip():
                            path_data = text_to_path(
                                tspan_text, font, tspan_font_size, x, y, text_anchor
                            )
                            if path_data:
                                path_groups.append({"paths": [path_data], "color": tspan_color})

                        # Handle tail text (text after tspan closing tag)
                        if child.tail and child.tail.strip():
                            # Tail text uses parent's color and size
                            path_data = text_to_path(child.tail, font, font_size, x, y, text_anchor)
                            if path_data:
                                path_groups.append({"paths": [path_data], "color": default_color})

                if not path_groups:
                    continue

                # Group paths by color
                color_to_paths = {}
                for group in path_groups:
                    color = group["color"]
                    if color not in color_to_paths:
                        color_to_paths[color] = []
                    color_to_paths[color].extend(group["paths"])

                # Create path elements for each color
                path_elements = []
                for color, paths in color_to_paths.items():
                    path_elem = ET.Element("path")
                    path_elem.set("d", " ".join(paths))
                    path_elem.set("stroke", color)
                    path_elem.set("fill", color)
                    path_elements.append(path_elem)

                # Copy transform and id to all path elements
                transform = text_elem.get("transform")
                text_id = text_elem.get("id")

                for i, path_elem in enumerate(path_elements):
                    if transform:
                        path_elem.set("transform", transform)
                    if text_id:
                        suffix = f"_path_{i}" if len(path_elements) > 1 else "_path"
                        path_elem.set("id", text_id + suffix)

                # Replace text element with path element(s) in parent
                parent = find_parent(root, text_elem)
                if parent is not None:
                    idx = list(parent).index(text_elem)
                    parent.remove(text_elem)
                    # Insert all path elements at the same position
                    for i, path_elem in enumerate(path_elements):
                        parent.insert(idx + i, path_elem)
                    converted_count += 1

            except Exception as e:
                log.debug(f"Error converting text element: {e}")
                continue

        if converted_count > 0:
            log.info(f"Converted {converted_count} text element(s) to paths")
            output = ET.tostring(root, encoding="unicode")
            return output.encode("utf-8") if is_bytes else output

        return svg_string

    except ET.ParseError as e:
        log.warning(f"Failed to parse SVG: {e}")
        return svg_string
    except Exception as e:
        log.warning(f"Failed to convert text to paths: {e}")
        return svg_string


def get_text_content(elem):
    """Extract all text content from an element and its children."""
    text = elem.text or ""
    for child in elem:
        text += get_text_content(child)
        if child.tail:
            text += child.tail
    return text


def normalize_color(color):
    """Convert a color value to a hex color code.

    Handles:
    - Hex colors (#rgb, #rrggbb)
    - Named colors (red, blue, etc.)
    - rgb() format

    Args:
        color: Color string in any supported format

    Returns:
        Hex color code (e.g., '#ff0000') or '#000000' if conversion fails
    """
    if not color or color == "none":
        return "#000000"

    color = color.strip().lower()

    # Already a hex color
    if color.startswith("#"):
        # Handle 3-digit hex (#rgb -> #rrggbb)
        if len(color) == 4:
            return "#" + color[1] * 2 + color[2] * 2 + color[3] * 2
        return color

    # Try to convert named color to hex using webcolors
    try:
        from . import webcolors

        return webcolors.name_to_hex(color)
    except (ValueError, KeyError):
        pass

    # Handle rgb() format
    if color.startswith("rgb(") and color.endswith(")"):
        try:
            rgb_str = color[4:-1]
            parts = [p.strip() for p in rgb_str.split(",")]
            if len(parts) == 3:
                r, g, b = [int(p) for p in parts]
                return f"#{r:02x}{g:02x}{b:02x}"
        except ValueError:
            pass

    # Default to black
    return "#000000"


def get_style_attr(elem, attr_name, default=None):
    """Get a style attribute from element, checking both style property and attribute.

    CSS style properties take precedence over presentation attributes in SVG,
    so we check the style attribute first.
    """
    # Check style attribute first (higher precedence in CSS)
    style = elem.get("style", "")
    if style:
        for part in style.split(";"):
            if ":" in part:
                name, val = part.split(":", 1)
                if name.strip() == attr_name:
                    return val.strip()

    # Fall back to direct attribute (presentation attribute)
    value = elem.get(attr_name)
    if value:
        return value

    return default


def parse_font_size(size_str):
    """Parse font size string to numeric value in pixels."""
    size_str = size_str.strip().lower()

    # Remove 'px' suffix
    if size_str.endswith("px"):
        return float(size_str[:-2])
    elif size_str.endswith("pt"):
        return float(size_str[:-2]) * 1.333  # pt to px
    elif size_str.endswith("em"):
        return float(size_str[:-2]) * 16  # assume 16px base
    elif size_str.endswith("%"):
        return float(size_str[:-1]) * 0.16  # assume 16px base

    try:
        return float(size_str)
    except ValueError:
        return 16.0


def find_parent(root, target):
    """Find the parent element of target in the tree."""
    for parent in root.iter():
        for child in parent:
            if child is target:
                return parent
    return None
