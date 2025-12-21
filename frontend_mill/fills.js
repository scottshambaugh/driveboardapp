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
  var min_x = Math.max(combinedBounds[0] - leadin, 0);
  var max_x = Math.min(
    combinedBounds[2] + leadin,
    app_config_main.workspace[0],
  );
  var fillpxsize = parseFloat($("#fillpxsize").val());
  var fillpolylines = []; // polylines aka path

  // Pre-process: collect all segments with their Y-bounds for fast filtering
  var segments = [];
  for (var p = 0; p < allPaths.length; p++) {
    var path = allPaths[p];
    for (var i = 0; i < path.length; i++) {
      var polyline = path[i];
      if (polyline.length > 1) {
        var pv = polyline[0];
        for (var j = 1; j < polyline.length; j++) {
          var v = polyline[j];
          segments.push({
            x1: pv[0],
            y1: pv[1],
            x2: v[0],
            y2: v[1],
            minY: Math.min(pv[1], v[1]),
            maxY: Math.max(pv[1], v[1]),
          });
          pv = v;
        }
        // Auto-close: check intersection with closing segment (last to first point)
        var first = polyline[0];
        var last = polyline[polyline.length - 1];
        if (first[0] !== last[0] || first[1] !== last[1]) {
          segments.push({
            x1: last[0],
            y1: last[1],
            x2: first[0],
            y2: first[1],
            minY: Math.min(last[1], first[1]),
            maxY: Math.max(last[1], first[1]),
          });
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

      // Remove segments that end before current Y
      activeSegments = activeSegments.filter(function (seg) {
        return seg.maxY >= y;
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
    // generate a new color shifted from the old
    var newcolor;
    var fillcolor = new paper.Color(color);
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
    jobhandler.defs.push({ kind: "fill", data: fillpolylines });
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
}

// Optimized intersection for horizontal scanline with a segment
// Returns the x-coordinate of intersection, or null if no intersection
function fills_horizontal_intersect(y, x1, y1, x2, y2) {
  // Check if segment spans this Y (exclusive to avoid double-counting at vertices)
  if ((y1 < y && y2 > y) || (y1 > y && y2 < y)) {
    // Calculate x at intersection using linear interpolation
    var t = (y - y1) / (y2 - y1);
    return x1 + t * (x2 - x1);
  }
  return null;
}
