var tools_tselect = undefined;
var tools_toffset = undefined;
var tools_tmove = undefined;
var tools_tjog = undefined;
var posText = undefined;

var tools_tselect_band = undefined;
var tools_tselect_start = undefined;
var tools_tselect_dragged = false;
var tools_tselect_mode = "replace";
// px of movement before a press turns into a box selection
var tools_tselect_threshold = 4;
// px around a click that still counts as touching an item
var tools_tselect_tolerance = 10;

function tools_tselect_init() {
  // rubber band lives on its own layer, away from the job geometry
  jobview_selectLayer = new paper.Layer();
  jobview_selectLayer.transformContent = false;
  jobview_selectLayer.pivot = new paper.Point(0, 0);
  tools_tselect = new paper.Tool();
  tools_tselect.onMouseDown = function (event) {
    tools_tselect_press(event.point, tools_tselect_event_mode(event.event));
  };
  tools_tselect.onMouseDrag = function (event) {
    if (tools_tselect_start === undefined) {
      // paper hands a press to the tool only when it saw the previous
      // release, so a swallowed release would leave a drag with no box
      tools_tselect_press(event.point, tools_tselect_event_mode(event.event));
    }
    tools_tselect_move(event.point);
  };
  tools_tselect.onMouseUp = function (event) {
    if (tools_tselect_start === undefined) {
      return;
    }
    tools_tselect_release(event.point);
  };
}

function tools_tselect_event_mode(e) {
  // shift adds to the selection, ctrl or cmd takes away
  if (!e) {
    return "replace";
  }
  if (e.shiftKey) {
    return "add";
  }
  if (e.ctrlKey || e.metaKey) {
    return "remove";
  }
  return "replace";
}

function tools_tselect_press(point, mode) {
  // a band can be left behind when a release never reached the tool
  tools_tselect_band_clear();
  tools_tselect_mode = mode;
  if (mode === "replace") {
    jobview_deselect_all();
    jobhandler.clearPassHighlights();
  }
  // any press can grow into a box selection, the hit test waits for the
  // release so a fast drag is not held up by it
  tools_tselect_start = point;
  tools_tselect_dragged = false;
}

function tools_tselect_move(point) {
  if (
    !tools_tselect_dragged &&
    tools_tselect_start.getDistance(point) < tools_tselect_threshold
  ) {
    return;
  }
  tools_tselect_dragged = true;
  tools_tselect_band_update(tools_tselect_start, point);
  // paper redraws on the next frame by itself, drawing here instead
  // renders the whole job again for every mouse move
}

function tools_tselect_release(point) {
  var start = tools_tselect_start;
  var dragged = tools_tselect_dragged;
  var mode = tools_tselect_mode;
  tools_tselect_start = undefined;
  tools_tselect_dragged = false;
  tools_tselect_band_clear();
  var idxs = [];
  var primary = undefined;
  if (dragged) {
    idxs = tools_tselect_inbox(start, point);
    if (idxs.length > 0) {
      primary = idxs[0];
    }
  } else {
    // a click takes what it pressed on, and everything matching it
    primary = tools_tselect_hit(start);
    if (primary !== undefined) {
      idxs = jobhandler.matchingItems(primary);
    }
  }
  var selection = tools_tselect_combine(jobview_items_selected, idxs, mode);
  jobview_deselect_all();
  jobhandler.clearPassHighlights();
  tools_tselect_show(selection);
  jobview_items_selected = selection;
  if (selection.length === 0) {
    jobview_item_selected = undefined;
  } else if (primary !== undefined && selection.indexOf(primary) != -1) {
    jobview_item_selected = primary;
  } else {
    jobview_item_selected = selection[0];
  }
  paper.view.draw();
}

function tools_tselect_combine(selection, idxs, mode) {
  // fold what was just picked into what was already selected
  if (mode === "replace") {
    return idxs;
  }
  var out = [];
  var i = 0;
  if (mode === "remove") {
    for (i = 0; i < selection.length; i++) {
      if (idxs.indexOf(selection[i]) == -1) {
        out.push(selection[i]);
      }
    }
    return out;
  }
  for (i = 0; i < selection.length; i++) {
    out.push(selection[i]);
  }
  for (i = 0; i < idxs.length; i++) {
    if (out.indexOf(idxs[i]) == -1) {
      out.push(idxs[i]);
    }
  }
  return out;
}

function tools_tselect_hit(point) {
  // nearest item within the click tolerance, whatever draws on top wins.
  // paper's hitTest walks every curve of the job, which stalls a click
  var tol = tools_tselect_tolerance / jobview_mm2px;
  var tol2 = tol * tol;
  var x = point.x / jobview_mm2px;
  var y = point.y / jobview_mm2px;
  var stats_items = jobhandler.stats.items;
  if (!stats_items) {
    return undefined;
  }
  var best = undefined;
  var best_rank = 0;
  var best_dist2 = Infinity;
  jobhandler.loopItems(function (item, i) {
    var stats = stats_items[i];
    if (!stats || !stats.bbox) {
      return;
    }
    var bbox = stats.bbox;
    if (
      x < bbox[0] - tol ||
      x > bbox[2] + tol ||
      y < bbox[1] - tol ||
      y > bbox[3] + tol
    ) {
      return;
    }
    var def = jobhandler.defs[item.def];
    var rank = tools_tselect_kind_rank(def.kind);
    if (rank < best_rank) {
      return;
    }
    var dist2;
    if (def.kind === "image") {
      // rasters engrave their whole bbox
      dist2 = tools_tselect_bbox_dist2(bbox, x, y);
    } else if (def.data) {
      dist2 = tools_tselect_data_dist2(def.data, x, y, tol);
    } else {
      return;
    }
    if (dist2 > tol2) {
      return;
    }
    if (rank > best_rank || dist2 <= best_dist2) {
      best = i;
      best_rank = rank;
      best_dist2 = dist2;
    }
  });
  return best;
}

function tools_tselect_kind_rank(kind) {
  // drawing order, paths sit on top of fills which sit on top of rasters
  if (kind === "path") {
    return 3;
  }
  if (kind === "fill") {
    return 2;
  }
  return 1;
}

function tools_tselect_bbox_dist2(bbox, x, y) {
  // squared distance to the bbox, zero inside it
  var dx = Math.max(bbox[0] - x, 0, x - bbox[2]);
  var dy = Math.max(bbox[1] - y, 0, y - bbox[3]);
  return dx * dx + dy * dy;
}

function tools_tselect_data_dist2(data, x, y, tol) {
  // squared distance to the nearest vertex or segment of the item, segments
  // that stay outside the tolerance band are skipped
  var lo_x = x - tol;
  var hi_x = x + tol;
  var lo_y = y - tol;
  var hi_y = y + tol;
  var best = Infinity;
  for (var i = 0; i < data.length; i++) {
    var polyline = data[i];
    if (polyline.length === 0) {
      continue;
    }
    var ax = polyline[0][0];
    var ay = polyline[0][1];
    var dist2 = (ax - x) * (ax - x) + (ay - y) * (ay - y);
    if (dist2 < best) {
      best = dist2;
    }
    for (var j = 1; j < polyline.length; j++) {
      var bx = polyline[j][0];
      var by = polyline[j][1];
      if (
        !(
          (ax < lo_x && bx < lo_x) ||
          (ax > hi_x && bx > hi_x) ||
          (ay < lo_y && by < lo_y) ||
          (ay > hi_y && by > hi_y)
        )
      ) {
        dist2 = tools_tselect_seg_dist2(ax, ay, bx, by, x, y);
        if (dist2 < best) {
          best = dist2;
          if (best === 0) {
            return 0;
          }
        }
      }
      ax = bx;
      ay = by;
    }
  }
  return best;
}

function tools_tselect_seg_dist2(ax, ay, bx, by, x, y) {
  // squared distance from the point to the segment
  var dx = bx - ax;
  var dy = by - ay;
  var len2 = dx * dx + dy * dy;
  var t = 0;
  if (len2 > 0) {
    t = ((x - ax) * dx + (y - ay) * dy) / len2;
    if (t < 0) {
      t = 0;
    } else if (t > 1) {
      t = 1;
    }
  }
  var cx = ax + t * dx - x;
  var cy = ay + t * dy - y;
  return cx * cx + cy * cy;
}

function tools_tselect_band_update(from, to) {
  // the band is one path that follows the drag, corners in draw order
  var x1 = Math.min(from.x, to.x);
  var y1 = Math.min(from.y, to.y);
  var x2 = Math.max(from.x, to.x);
  var y2 = Math.max(from.y, to.y);
  if (!tools_tselect_band) {
    tools_tselect_band = new paper.Path();
    tools_tselect_band.closed = true;
    tools_tselect_band.strokeColor = "#0088cc";
    tools_tselect_band.strokeWidth = 1;
    tools_tselect_band.dashArray = [4, 4];
    tools_tselect_band.add([x1, y1], [x2, y1], [x2, y2], [x1, y2]);
    jobview_selectLayer.addChild(tools_tselect_band);
    return;
  }
  var segments = tools_tselect_band.segments;
  segments[0].point = [x1, y1];
  segments[1].point = [x2, y1];
  segments[2].point = [x2, y2];
  segments[3].point = [x1, y2];
}

function tools_tselect_band_clear() {
  if (tools_tselect_band) {
    tools_tselect_band.remove();
    tools_tselect_band = undefined;
  }
}

function tools_tselect_show(idxs) {
  // mark the items and their pass entries as selected
  for (var i = 0; i < idxs.length; i++) {
    var group = jobhandler.itemidx2group[idxs[i]];
    if (group) {
      group.selected = true;
    }
  }
  jobhandler.highlightPassEntries(idxs);
}

function tools_tselect_inbox(from, to) {
  // items with geometry in the box spanned by the two canvas points,
  // in mm to match the item data
  var x1 = Math.min(from.x, to.x) / jobview_mm2px;
  var y1 = Math.min(from.y, to.y) / jobview_mm2px;
  var x2 = Math.max(from.x, to.x) / jobview_mm2px;
  var y2 = Math.max(from.y, to.y) / jobview_mm2px;
  var idxs = [];
  var stats_items = jobhandler.stats.items;
  if (!stats_items) {
    return idxs;
  }
  jobhandler.loopItems(function (item, i) {
    var stats = stats_items[i];
    if (!stats || !stats.bbox) {
      return;
    }
    var bbox = stats.bbox;
    if (bbox[2] < x1 || bbox[0] > x2 || bbox[3] < y1 || bbox[1] > y2) {
      // the box is nowhere near this item
      return;
    }
    var def = jobhandler.defs[item.def];
    if (def.kind === "image") {
      // rasters engrave their whole bbox
      idxs.push(i);
      return;
    }
    if (def.data && tools_tselect_data_inbox(def.data, x1, y1, x2, y2)) {
      idxs.push(i);
    }
  });
  return idxs;
}

function tools_tselect_data_inbox(data, x1, y1, x2, y2) {
  // an item counts as boxed when a vertex sits in the box or a segment
  // crosses it, so empty space between its polylines does not select it
  for (var i = 0; i < data.length; i++) {
    var polyline = data[i];
    for (var j = 0; j < polyline.length; j++) {
      var x = polyline[j][0];
      var y = polyline[j][1];
      if (x >= x1 && x <= x2 && y >= y1 && y <= y2) {
        return true;
      }
      if (
        j > 0 &&
        tools_tselect_seg_inbox(
          polyline[j - 1][0],
          polyline[j - 1][1],
          x,
          y,
          x1,
          y1,
          x2,
          y2,
        )
      ) {
        return true;
      }
    }
  }
  return false;
}

function tools_tselect_seg_inbox(ax, ay, bx, by, x1, y1, x2, y2) {
  // segment against box, clipped one slab at a time
  var dx = bx - ax;
  var dy = by - ay;
  var t0 = 0;
  var t1 = 1;
  var tmp = 0;
  if (dx === 0) {
    if (ax < x1 || ax > x2) {
      return false;
    }
  } else {
    var tx0 = (x1 - ax) / dx;
    var tx1 = (x2 - ax) / dx;
    if (tx0 > tx1) {
      tmp = tx0;
      tx0 = tx1;
      tx1 = tmp;
    }
    if (tx0 > t0) {
      t0 = tx0;
    }
    if (tx1 < t1) {
      t1 = tx1;
    }
  }
  if (dy === 0) {
    if (ay < y1 || ay > y2) {
      return false;
    }
  } else {
    var ty0 = (y1 - ay) / dy;
    var ty1 = (y2 - ay) / dy;
    if (ty0 > ty1) {
      tmp = ty0;
      ty0 = ty1;
      ty1 = tmp;
    }
    if (ty0 > t0) {
      t0 = ty0;
    }
    if (ty1 < t1) {
      t1 = ty1;
    }
  }
  return t0 <= t1;
}

function tools_addfill_init() {
  // add color choices to addfill_btn
  var select_html = "";
  // params
  select_html +=
    "<li>" +
    '<form class="form-inline">' +
    '<div class="form-group">' +
    '<div class="input-group" style="margin:10px">' +
    '<div class="input-group-addon" style="width:10px">pxsize [mm]</div>' +
    '<input id="fillpxsize" type="text" class="form-control input-sm" style="width:40px;"' +
    'value="' +
    app_config_main.pxsize +
    '" title="match this to laser focus size">' +
    "</div>" +
    "</div>" +
    "</form>" +
    "</li>";
  // colors
  jobhandler.loopItems(function (path, idx) {
    select_html +=
      '<li id="addfill_' +
      idx +
      '" style="background-color:' +
      path.color +
      ';"">' +
      '<a href="#" class="addfill_color" style="color:' +
      path.color +
      '">' +
      '<span class="label label-default kindmem">path</span>' +
      '<span style="display:none" class="idxmem">' +
      idx +
      "</span></a></li>";
  }, "path");
  $("#addfill_colors").html(select_html);

  // bind all color add buttons within dropdown
  $(".addfill_color").click(function (e) {
    var idx = parseFloat($(this).children("span.idxmem").text());
    $("#addfill_colors").dropdown("toggle");
    app_fill_btn.start();
    fills_add_by_item(idx, function () {
      app_fill_btn.stop();
    });
    return false;
  });
}

function tools_toffset_init() {
  // create layer
  jobview_offsetLayer = new paper.Layer();
  jobview_offsetLayer.transformContent = false;
  jobview_offsetLayer.pivot = new paper.Point(0, 0);
  jobview_offsetLayer.visible = false;
  jobview_offsetLayer.activate();
  // greate group
  var group = new paper.Group();
  var rec1 = new paper.Path.Rectangle(
    new paper.Point(-9999, -9999),
    new paper.Point(9999, 0),
  );
  group.addChild(rec1);
  var rec2 = new paper.Path.Rectangle(
    new paper.Point(-9999, 0),
    new paper.Point(0, 9999),
  );
  group.addChild(rec2);
  group.fillColor = "#000000";
  rec1.opacity = 0.5;
  rec2.opacity = 0.5;
  // create tool
  tools_toffset = new paper.Tool();
  tools_toffset.onMouseDown = function (event) {
    var x = Math.ceil(event.point.x / jobview_mm2px);
    var y = Math.ceil(event.point.y / jobview_mm2px);
    request_get({
      url: "/absoffset/" + x + "/" + y + "/0",
      success: function (data) {
        $().uxmessage("notice", "Offset set to: " + x + "," + y);
      },
      error: function (data) {
        jobview_offsetLayer.position = new paper.Point(
          status_cache.offset[0],
          status_cache.offset[1],
        );
      },
    });
    $("#select_btn").trigger("click");
  };
  tools_toffset.onMouseMove = function (event) {
    if (event.point.x <= jobview_width && event.point.y <= jobview_height) {
      jobview_offsetLayer.visible = true;
      jobview_offsetLayer.position = event.point;
    }
  };
  tools_toffset.offset_set = function (e) {
    // offset_set is called when the user presses the offset_set_btn. Sets the
    // current position of the head as the new offset.
    var x = status_cache.pos[0] + status_cache.offset[0];
    var y = status_cache.pos[1] + status_cache.offset[1];
    request_get({
      url: "/absoffset/" + x + "/" + y + "/0",
      success: function (data) {
        $("#select_btn").trigger("click");
        x_print = Math.round(x * 10) / 10;
        y_print = Math.round(y * 10) / 10;
        $().uxmessage(
          "notice",
          "Offset set to current position: " + x_print + "," + y_print,
        );
      },
      error: function (data) {
        $().uxmessage(
          "error",
          "Setting current position as offset not possible!",
        );
      },
    });
  };
}

function tools_tmove_init() {
  // create layer
  jobview_moveLayer = new paper.Layer();
  jobview_moveLayer.transformContent = false;
  jobview_moveLayer.pivot = new paper.Point(0, 0);
  jobview_moveLayer.visible = false;
  jobview_moveLayer.activate();
  // greate group
  var group = new paper.Group();
  var line1 = new paper.Path();
  line1.add([-9999, 0], [9999, 0]);
  group.addChild(line1);
  var line2 = new paper.Path();
  line2.add([0, -9999], [0, 9999]);
  group.addChild(line2);
  var circ1 = new paper.Path.Circle([0, 0], 10);
  group.addChild(circ1);
  group.strokeColor = "#ff0000";
  // create tool
  tools_tmove = new paper.Tool();
  tools_tmove.onMouseDown = function (event) {
    // check for machine
    if (!status_cache.serial) {
      $().uxmessage("error", "No machine.");
      return;
    }
    // round to 100um increments
    var x_mm =
      Math.round(
        (event.point.x / jobview_mm2px - status_cache.offset[0]) * 10,
      ) / 10;
    var y_mm =
      Math.round(
        (event.point.y / jobview_mm2px - status_cache.offset[1]) * 10,
      ) / 10;
    request_absolute_move(
      x_mm,
      y_mm,
      0,
      app_config_main.seekrate,
      "Moving to " + x_mm + "," + y_mm,
    );
    status_cache.ready = undefined; // force status update
    // setTimeout(function(){
    //   jobview_moveLayer.visible = false
    // },1000)
  };
  tools_tmove.onMouseMove = function (event) {
    if (event.point.x <= jobview_width && event.point.y <= jobview_height) {
      jobview_moveLayer.visible = true;
      jobview_moveLayer.position = event.point;
    }
  };
}

function tools_tjog_init() {
  // create layer
  jobview_jogLayer = new paper.Layer();
  jobview_jogLayer.transformContent = false;
  jobview_jogLayer.pivot = new paper.Point(0, 0);
  jobview_jogLayer.visible = false;
  jobview_jogLayer.activate();
  // greate group
  var group = new paper.Group();
  var ref = undefined;
  // up widget
  // var rec_up = new paper.Path.Rectangle(
  //   new paper.Point(jobview_width*0.3,0),
  //   new paper.Point(jobview_width*0.7,jobview_height*0.3))
  // group.addChild(rec_up)
  var arrow_up_sm = new paper.Path();
  ref = [0.5 * jobview_width, jobview_height * 0.1 - 30];
  arrow_up_sm.add(
    ref,
    [ref[0] - 15, ref[1] + 15],
    [ref[0] - 7.5, ref[1] + 15],
    [ref[0] - 7.5, ref[1] + 20],
    [ref[0] + 7.5, ref[1] + 20],
    [ref[0] + 7.5, ref[1] + 15],
    [ref[0] + 15, ref[1] + 15],
    ref,
  );
  group.addChild(arrow_up_sm);
  var arrow_up = new paper.Path();
  ref = [0.5 * jobview_width, jobview_height * 0.1];
  arrow_up.add(
    ref,
    [ref[0] - 30, ref[1] + 30],
    [ref[0] - 15, ref[1] + 30],
    [ref[0] - 15, ref[1] + 40],
    [ref[0] + 15, ref[1] + 40],
    [ref[0] + 15, ref[1] + 30],
    [ref[0] + 30, ref[1] + 30],
    ref,
  );
  group.addChild(arrow_up);
  var arrow_up_lg = new paper.Path();
  ref = [0.5 * jobview_width, jobview_height * 0.1 + 50];
  arrow_up_lg.add(
    ref,
    [ref[0] - 60, ref[1] + 60],
    [ref[0] - 30, ref[1] + 60],
    [ref[0] - 30, ref[1] + 80],
    [ref[0] + 30, ref[1] + 80],
    [ref[0] + 30, ref[1] + 60],
    [ref[0] + 60, ref[1] + 60],
    ref,
  );
  group.addChild(arrow_up_lg);
  // down widget
  // var rec_down = new paper.Path.Rectangle(
  //   new paper.Point(jobview_width*0.3,jobview_height*0.7),
  //   new paper.Point(jobview_width*0.7,jobview_height))
  // group.addChild(rec_down)
  var arrow_down_sm = new paper.Path();
  ref = [0.5 * jobview_width, jobview_height * 0.9 + 30];
  arrow_down_sm.add(
    ref,
    [ref[0] - 15, ref[1] - 15],
    [ref[0] - 7.5, ref[1] - 15],
    [ref[0] - 7.5, ref[1] - 20],
    [ref[0] + 7.5, ref[1] - 20],
    [ref[0] + 7.5, ref[1] - 15],
    [ref[0] + 15, ref[1] - 15],
    ref,
  );
  group.addChild(arrow_down_sm);
  var arrow_down = new paper.Path();
  ref = [0.5 * jobview_width, jobview_height * 0.9];
  arrow_down.add(
    ref,
    [ref[0] - 30, ref[1] - 30],
    [ref[0] - 15, ref[1] - 30],
    [ref[0] - 15, ref[1] - 40],
    [ref[0] + 15, ref[1] - 40],
    [ref[0] + 15, ref[1] - 30],
    [ref[0] + 30, ref[1] - 30],
    ref,
  );
  group.addChild(arrow_down);
  var arrow_down_lg = new paper.Path();
  ref = [0.5 * jobview_width, jobview_height * 0.9 - 50];
  arrow_down_lg.add(
    ref,
    [ref[0] - 60, ref[1] - 60],
    [ref[0] - 30, ref[1] - 60],
    [ref[0] - 30, ref[1] - 80],
    [ref[0] + 30, ref[1] - 80],
    [ref[0] + 30, ref[1] - 60],
    [ref[0] + 60, ref[1] - 60],
    ref,
  );
  group.addChild(arrow_down_lg);
  // left widget
  // var rec_left = new paper.Path.Rectangle(
  //   new paper.Point(0,jobview_height*0.3),
  //   new paper.Point(jobview_width*0.3,jobview_height*0.7))
  // group.addChild(rec_left)
  var arrow_left_sm = new paper.Path();
  ref = [0.1 * jobview_width - 30, 0.5 * jobview_height];
  arrow_left_sm.add(
    ref,
    [ref[0] + 15, ref[1] - 15],
    [ref[0] + 15, ref[1] - 7.5],
    [ref[0] + 20, ref[1] - 7.5],
    [ref[0] + 20, ref[1] + 7.5],
    [ref[0] + 15, ref[1] + 7.5],
    [ref[0] + 15, ref[1] + 15],
    ref,
  );
  group.addChild(arrow_left_sm);
  var arrow_left = new paper.Path();
  ref = [0.1 * jobview_width, 0.5 * jobview_height];
  arrow_left.add(
    ref,
    [ref[0] + 30, ref[1] - 30],
    [ref[0] + 30, ref[1] - 15],
    [ref[0] + 40, ref[1] - 15],
    [ref[0] + 40, ref[1] + 15],
    [ref[0] + 30, ref[1] + 15],
    [ref[0] + 30, ref[1] + 30],
    ref,
  );
  group.addChild(arrow_left);
  var arrow_left_lg = new paper.Path();
  ref = [0.1 * jobview_width + 50, 0.5 * jobview_height];
  arrow_left_lg.add(
    ref,
    [ref[0] + 60, ref[1] - 60],
    [ref[0] + 60, ref[1] - 30],
    [ref[0] + 80, ref[1] - 30],
    [ref[0] + 80, ref[1] + 30],
    [ref[0] + 60, ref[1] + 30],
    [ref[0] + 60, ref[1] + 60],
    ref,
  );
  group.addChild(arrow_left_lg);
  // right widget
  // var rec_right = new paper.Path.Rectangle(
  //   new paper.Point(jobview_width*0.7,jobview_height*0.3),
  //   new paper.Point(jobview_width,jobview_height*0.7))
  // group.addChild(rec_right)
  var arrow_right_sm = new paper.Path();
  ref = [0.9 * jobview_width + 30, 0.5 * jobview_height];
  arrow_right_sm.add(
    ref,
    [ref[0] - 15, ref[1] - 15],
    [ref[0] - 15, ref[1] - 7.5],
    [ref[0] - 20, ref[1] - 7.5],
    [ref[0] - 20, ref[1] + 7.5],
    [ref[0] - 15, ref[1] + 7.5],
    [ref[0] - 15, ref[1] + 15],
    ref,
  );
  group.addChild(arrow_right_sm);
  var arrow_right = new paper.Path();
  ref = [0.9 * jobview_width, 0.5 * jobview_height];
  arrow_right.add(
    ref,
    [ref[0] - 30, ref[1] - 30],
    [ref[0] - 30, ref[1] - 15],
    [ref[0] - 40, ref[1] - 15],
    [ref[0] - 40, ref[1] + 15],
    [ref[0] - 30, ref[1] + 15],
    [ref[0] - 30, ref[1] + 30],
    ref,
  );
  group.addChild(arrow_right);
  var arrow_right_lg = new paper.Path();
  ref = [0.9 * jobview_width - 50, 0.5 * jobview_height];
  arrow_right_lg.add(
    ref,
    [ref[0] - 60, ref[1] - 60],
    [ref[0] - 60, ref[1] - 30],
    [ref[0] - 80, ref[1] - 30],
    [ref[0] - 80, ref[1] + 30],
    [ref[0] - 60, ref[1] + 30],
    [ref[0] - 60, ref[1] + 60],
    ref,
  );
  group.addChild(arrow_right_lg);
  // properties
  group.fillColor = "#000000";
  arrow_up_sm.opacity = 0.7;
  arrow_up.opacity = 0.7;
  arrow_up_lg.opacity = 0.7;
  arrow_down_sm.opacity = 0.7;
  arrow_down.opacity = 0.7;
  arrow_down_lg.opacity = 0.7;
  arrow_left_sm.opacity = 0.7;
  arrow_left.opacity = 0.7;
  arrow_left_lg.opacity = 0.7;
  arrow_right_sm.opacity = 0.7;
  arrow_right.opacity = 0.7;
  arrow_right_lg.opacity = 0.7;
  // create tool
  tools_tjog = new paper.Tool();
  tools_tjog.onMouseDown = function (event) {
    var hit = jobview_jogLayer.hitTest(event.point);
    if (hit) {
      if (hit.item === arrow_up) {
        request_jog(0, -10, 0, "jogging up 10mm");
      } else if (hit.item === arrow_up_lg) {
        request_jog(0, -50, 0, "jogging up 50mm");
      } else if (hit.item === arrow_up_sm) {
        request_jog(0, -1, 0, "jogging up 1mm");
      } else if (hit.item === arrow_down) {
        request_jog(0, 10, 0, "jogging down 10mm");
      } else if (hit.item === arrow_down_lg) {
        request_jog(0, 50, 0, "jogging down 50mm");
      } else if (hit.item === arrow_down_sm) {
        request_jog(0, 1, 0, "jogging down 1mm");
      } else if (hit.item === arrow_left) {
        request_jog(-10, 0, 0, "jogging left 10mm");
      } else if (hit.item === arrow_left_lg) {
        request_jog(-50, 0, 0, "jogging left 50mm");
      } else if (hit.item === arrow_left_sm) {
        request_jog(-1, 0, 0, "jogging left 1mm");
      } else if (hit.item === arrow_right) {
        request_jog(10, 0, 0, "jogging right 10mm");
      } else if (hit.item === arrow_right_lg) {
        request_jog(50, 0, 0, "jogging right 50mm");
      } else if (hit.item === arrow_right_sm) {
        request_jog(1, 0, 0, "jogging right 1mm");
      }
    }
  };
  tools_tjog.onMouseMove = function (event) {
    // if (event.point.x <= jobview_width && event.point.y <= jobview_height) {
    //   jobview_jogLayer.visible = true
    //   jobview_jogLayer.position = event.point
    // }
  };
}

function tools_tpos_init() {
  // create layer
  jobview_posLayer = new paper.Layer();
  jobview_posLayer.transformContent = false;
  jobview_posLayer.pivot = new paper.Point(0, 0);
  jobview_posLayer.visible = false;
  jobview_posLayer.activate();
  // create group
  var group = new paper.Group();
  var xPoint = jobview_width - 5;
  var yPoint = jobview_height * 0.025;
  posText = new paper.PointText(new paper.Point(xPoint, yPoint));
  posText.justification = "right";
  posText.fillColor = "black";
  posText.opacity = 0.5;
  posText.fontSize = jobview_height * 0.025;
  posText.content = "(X: XXX,x; Y: YYY,y)";
  group.addChild(posText);
  jobview_posLayer.visible = true;
}
