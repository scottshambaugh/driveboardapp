function fills_add_by_item(idx, callback) {
  if (jobhandler.defs[jobhandler.items[idx].def].kind != "path") {
    callback();
    return;
  }
  var color = jobhandler.items[idx].color;

  // Collect all path items with the same color for nested fill support
  var allPaths = [];
  var combinedBounds = [Infinity, Infinity, -Infinity, -Infinity];
  jobhandler.loopItems(function (item, i) {
    if (item.color === color) {
      var pathData = jobhandler.defs[item.def].data;
      allPaths.push(pathData);
      // expand combined bounds
      var itemBounds = jobhandler.stats.items[i].bbox;
      jobhandler.bboxExpand2(combinedBounds, itemBounds);
    }
  }, "path");

  var leadin = app_config_main.fill_leadin;
  if (
    app_config_main.fill_mode != "Forward" &&
    app_config_main.fill_mode != "Reverse" &&
    app_config_main.fill_mode != "Bidirectional"
  ) {
    leadin = 0;
  }
  var min_x = Math.max(combinedBounds[0] - leadin, 0);
  var max_x = Math.min(
    combinedBounds[2] + leadin,
    app_config_main.workspace[0],
  );
  var fillpxsize = parseFloat($("#fillpxsize").val());
  var fillpolylines = []; // polylines aka path

  // Pre-process: collect all segments with their Y-bounds for fast filtering
  var segments = [];
  var MIN_SEGMENT_HEIGHT = 1e-10; // Skip nearly-horizontal segments

  function addSegment(x1, y1, x2, y2) {
    var minY = Math.min(y1, y2);
    var maxY = Math.max(y1, y2);
    // Only add segments that have meaningful vertical extent
    if (maxY - minY > MIN_SEGMENT_HEIGHT) {
      segments.push({
        x1: x1,
        y1: y1,
        x2: x2,
        y2: y2,
        minY: minY,
        maxY: maxY,
      });
    }
  }

  for (var p = 0; p < allPaths.length; p++) {
    var path = allPaths[p];
    for (var i = 0; i < path.length; i++) {
      var polyline = path[i];
      if (polyline.length > 1) {
        var pv = polyline[0];
        for (var j = 1; j < polyline.length; j++) {
          var v = polyline[j];
          addSegment(pv[0], pv[1], v[0], v[1]);
          pv = v;
        }
        // Auto-close: check intersection with closing segment (last to first point)
        var first = polyline[0];
        var last = polyline[polyline.length - 1];
        var dx = first[0] - last[0];
        var dy = first[1] - last[1];
        // Only add closing segment if endpoints are not nearly coincident
        if (dx * dx + dy * dy > 1e-10) {
          addSegment(last[0], last[1], first[0], first[1]);
        }
      }
    }
  }

  // Sort segments by minY for efficient active edge tracking
  segments.sort(function (a, b) {
    return a.minY - b.minY;
  });

  var y = combinedBounds[1] + 0.001;
  var max_y_bounds = combinedBounds[3];
  var segmentIndex = 0; // Index into sorted segments
  var activeSegments = []; // Segments that span the current Y range

  // Process in batches to keep UI responsive
  var BATCH_SIZE = 100;
  var linesProcessed = 0;

  function processBatch() {
    var batchEnd = linesProcessed + BATCH_SIZE;

    while (y <= max_y_bounds && linesProcessed < batchEnd) {
      // Add new segments that start at or before current Y
      while (
        segmentIndex < segments.length &&
        segments[segmentIndex].minY <= y
      ) {
        activeSegments.push(segments[segmentIndex]);
        segmentIndex++;
      }

      // Remove segments that end before current Y (with small epsilon for robustness)
      activeSegments = activeSegments.filter(function (seg) {
        return seg.maxY > y - 1e-10;
      });

      // Find intersections with active segments only
      var intersections = [];
      for (var i = 0; i < activeSegments.length; i++) {
        var seg = activeSegments[i];
        var ix = fills_horizontal_intersect(y, seg.x1, seg.y1, seg.x2, seg.y2);
        if (ix !== null) {
          intersections.push(ix);
        }
      }

      // Sort intersection points by x
      intersections.sort(function (a, b) {
        return a - b;
      });

      // Generate cut path
      if (intersections.length > 1) {
        var x_i = intersections[0];
        var y_i = y;
        var min_x_opti = Math.max(x_i - leadin, 0);
        var max_x_opti = Math.min(
          intersections[intersections.length - 1] + leadin,
          app_config_main.workspace[0],
        );
        fillpolylines.push([[min_x_opti, y_i]]); // polyline of one
        for (var k = 0; k + 1 < intersections.length; k += 2) {
          fillpolylines.push([
            [intersections[k], y_i],
            [intersections[k + 1], y_i],
          ]); // polyline of two
        }
        fillpolylines.push([[max_x_opti, y_i]]); // polyline of one
      }

      y += fillpxsize;
      linesProcessed++;
    }

    if (y <= max_y_bounds) {
      // More work to do, yield to UI then continue
      setTimeout(processBatch, 0);
    } else {
      finalize();
    }
  }

  processBatch();

  function finalize() {
    // Call backend to optimize the fill path according to fill_mode
    fills_optimize_and_add(fillpolylines, color, fillpxsize, callback);
  }
}

// Call backend to optimize fill path, then add to jobhandler
function fills_optimize_and_add(
  fillpolylines,
  originalColor,
  fillpxsize,
  callback,
) {
  $.ajax({
    type: "POST",
    url: "/optimize_fill",
    contentType: "application/json",
    data: JSON.stringify({ data: fillpolylines }),
    dataType: "json",
    success: function (result) {
      var optimizedData = result.data;
      var fillMode = result.fill_mode;
      console.log("Fill optimized with mode: " + fillMode);
      fills_add_to_job(optimizedData, originalColor, fillpxsize, callback);
    },
    error: function (xhr, status, error) {
      console.error("Fill optimization failed, using raw data: " + error);
      // Fall back to unoptimized data
      fills_add_to_job(fillpolylines, originalColor, fillpxsize, callback);
    },
  });
}

// Add optimized fill to jobhandler
function fills_add_to_job(fillData, originalColor, fillpxsize, callback) {
  // generate a new color shifted from the old
  var newcolor;
  var fillcolor = new paper.Color(originalColor);
  var jobcolors = jobhandler.getAllColors();
  while (true) {
    if (fillcolor.brightness > 0.5) {
      fillcolor.brightness -= 0.3 + 0.1 * Math.random();
    } else {
      fillcolor.brightness += 0.3 + 0.1 * Math.random();
    }
    fillcolor.hue += 10 + 5 * Math.random();
    newcolor = fillcolor.toCSS(true);
    if (jobcolors.indexOf(newcolor) == -1) {
      break;
    }
  }
  // add to jobhandler
  jobhandler.defs.push({ kind: "fill", data: fillData });
  jobhandler.items.push({
    def: jobhandler.defs.length - 1,
    color: newcolor,
    pxsize: fillpxsize,
  });
  jobhandler.calculateStats(); // TODO: only caclulate fill
  // update pass widgets
  jobhandler.passes = passes_get_active();
  passes_clear();
  passes_set_assignments();
  jobhandler.render();
  // jobhandler.draw()
  callback();
}

// Optimized intersection for horizontal scanline with a segment
// Returns the x-coordinate of intersection, or null if no intersection
function fills_horizontal_intersect(y, x1, y1, x2, y2) {
  // Handle near-horizontal segments (avoid division by near-zero)
  var dy = y2 - y1;
  if (Math.abs(dy) < 1e-10) {
    return null;
  }

  // Calculate parameter t along the segment
  var t = (y - y1) / dy;

  // Check if intersection is strictly inside the segment (not at endpoints)
  // Using small epsilon to handle floating point precision
  if (t > 1e-10 && t < 1 - 1e-10) {
    return x1 + t * (x2 - x1);
  }
  return null;
}
