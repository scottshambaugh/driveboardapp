// module to handle job data
// read, write, draw, stats, cleanup/filter
//
// {
//      "head": {
//          "noreturn": True,          # do not return to origin, default: False
//          "optimized": 0.08,         # optional, tolerance to which it was optimized, default: 0 (not optimized)
//       },
//      "passes": [
//          {
//              "items": [0],          # item by index
//              "relative": True,      # optional, default: False
//              "seekrate": 6000,      # optional, rate to first vertex
//              "feedrate": 2000,      # optional, rate to other vertices
//              "intensity": 100,      # optional, default: 0 (in percent)
//              "seekzero": False,     # optional, default: True
//              "pierce_time": 0,      # optional, default: 0
//              "pxsize": [0.4],       # optional
//              "air_assist": "pass",  # optional (off, feed, pass, job), default: pass
//              "aux_assist": "off",   # optional (off, feed, pass, job), default: off
//          }
//      ],
//     "items": [
//        {"def":0, "translate":[0,0,0], "color":"#BADA55"}
//     ],
//     "defs": [
//        {"kind":"path", "data":[[[0,10,0]]]},
//        {"kind":"fill", "data":[[[0,10,0]]], "pxsize":0.4},
//        {"kind":"image", "data":<data in base64>, "pos":[0,0], "size":[300,200], "source":<hash of pre-clip data>},
//     ],
//     "stats":{"items":[{"bbox":[x1,y1,x2,y2], "len":100}], "all":{}}
// }

jobhandler = {
  passes: [],
  items: [],
  defs: [],
  stats: {},
  itemidx2group: [],
  image_group_rep: {},
  image_group_members: {},
  sources: {},
  name: "",
  path_group: undefined,
  fill_group: undefined,
  image_group: undefined,

  clear: function () {
    this.passes = [];
    this.items = [];
    this.defs = [];
    this.stats = {};
    this.itemidx2group = [];
    this.image_group_rep = {};
    this.image_group_members = {};
    this.sources = {};
    this.name = "";
    jobview_clear();
    passes_clear();
    $("#job_info_name").html("");
    $("#job_info_length").html("");
    $("#job_info_duration").html("");
    $("#info_content").html("");
    $("#info_btn").hide();
  },

  isEmpty: function () {
    return !this.defs.length || !this.items.length;
  },

  hasPasses: function () {
    return !!this.passes.length;
  },

  loopItems: function (func, kind) {
    if (kind === undefined) {
      for (var i = 0; i < this.items.length; i++) {
        func(this.items[i], i);
      }
    } else {
      for (var i = 0; i < this.items.length; i++) {
        if (kind.indexOf(this.defs[this.items[i].def].kind) != -1) {
          func(this.items[i], i);
        }
      }
    }
  },

  loopPasses: function (func) {
    for (var i = 0; i < this.passes.length; i++) {
      var pass = this.passes[i];
      var item_idxs = [];
      for (var j = 0; j < pass.items.length; j++) {
        item_idxs.push(pass.items[j]);
      }
      func(pass, item_idxs);
    }
  },

  // setters //////////////////////////////////

  set: function (job, name, donefunc) {
    this.clear();
    this.name = name;
    $("title").html("DriveboardApp - " + name);
    // handle json string representations as well
    if (typeof job === "string") {
      job = JSON.parse(job);
    }
    // defs and items
    if (job.defs.length && job.items.length) {
      // head
      if ("head" in job) {
        this.head = job.head;
      }
      //passes
      if ("passes" in job && job.passes.length) {
        this.passes = job.passes;
      }
      // items, defs
      this.defs = job.defs;
      this.items = job.items;

      // group identical images while data is still base64
      this.groupIdenticalImages();

      // one Image per distinct raster, so a job that places the same picture
      // many times decodes and holds it once instead of once per placement
      var data2img = {};
      var distinct_data = [];
      for (var i = 0; i < this.defs.length; i++) {
        var def = this.defs[i];
        if (def.kind == "image" && !(def.data in data2img)) {
          data2img[def.data] = null;
          distinct_data.push(def.data);
        }
      }

      var image_to_load = distinct_data.length - 1;
      function allImagesLoaded() {
        if (image_to_load <= 0) {
          // passes, show in gui
          passes_set_assignments();
          donefunc();
        } else {
          image_to_load -= 1;
        }
      }

      if ("sources" in job) {
        for (var key in job.sources) {
          image_to_load += 1;
        }
      }

      // convert base64 image data to Image objects
      for (var i = 0; i < distinct_data.length; i++) {
        var img_base64 = distinct_data[i];
        var img = new Image();
        img.onload = allImagesLoaded;
        img.onerror = function () {
          // a raster the browser cannot decode still has to release the
          // counter, otherwise the job never finishes loading at all
          $().uxmessage("warning", "An image could not be displayed.");
          allImagesLoaded();
        };
        data2img[img_base64] = img;
        img.src = img_base64; // NOTE: this is async
      }
      for (var i = 0; i < this.defs.length; i++) {
        var def = this.defs[i];
        if (def.kind == "image") {
          def.data = data2img[def.data];
        }
      }
      if ("sources" in job) {
        for (var key in job.sources) {
          var srcimg = new Image();
          srcimg.onload = allImagesLoaded;
          // previews fall back to the clipped data, so fail quietly
          srcimg.onerror = allImagesLoaded;
          srcimg.src = job.sources[key];
          this.sources[key] = srcimg;
        }
      }

      // colors
      this.normalizeColors();
      // stats
      if ("stats" in job) {
        this.stats = job.stats;
      } else {
        this.calculateStats();
      }
      // job info
      $("#job_info_name").html(this.name);
      $("#info_btn").show();
      // info modal
      var html = "";
      html += "name : " + this.name + "<br>";
      $("#info_content").html(html);

      // no images, still need to run some code
      if (image_to_load == -1) {
        allImagesLoaded();
      }
    }
  },

  // getters //////////////////////////////////

  get: function () {
    // convert images back to base64
    var defs_out = [];
    for (var i = 0; i < this.defs.length; i++) {
      var def = this.defs[i];
      if (def.kind == "image") {
        var def_out = {
          kind: "image",
          pos: def.pos,
          size: def.size,
          data: def.data.src,
        };
        if (def.source) {
          def_out.source = def.source;
        }
        defs_out.push(def_out);
      } else {
        defs_out.push(def);
      }
    }
    var job_out = {
      head: this.head,
      passes: this.passes,
      items: this.items,
      defs: defs_out,
      stats: this.stats,
    };
    var sources_out = {};
    var have_sources = false;
    for (var key in this.sources) {
      sources_out[key] = this.sources[key].src;
      have_sources = true;
    }
    if (have_sources) {
      job_out.sources = sources_out;
    }
    return job_out;
  },

  getJson: function (whitespace) {
    // json stringify while limiting numbers to 3 decimals
    return JSON.stringify(
      this.get(),
      function (key, val) {
        if (isNaN(+key)) return val;
        return typeof val === "number" ? Number(val.toFixed(3)) : val;
      },
      whitespace,
    );
  },

  getAllColors: function () {
    colors = [];
    this.loopItems(function (item, i) {
      if ("color" in item) {
        colors.push(item.color);
      }
    });
    return colors;
  },

  getImageThumb: function (imgitem, width, height) {
    var img = this.defs[imgitem.def];

    // preview the unclipped source when the import cropped this image
    if (img.source && img.source in this.sources) {
      var source = this.sources[img.source];
      if (source.width > 0 && source.height > 0) {
        return this.renderThumb(
          source,
          [source.width, source.height],
          width,
          height,
        );
      }
    }

    // Safety check for valid size values
    if (
      !img.size ||
      !img.size[0] ||
      !img.size[1] ||
      img.size[0] <= 0 ||
      img.size[1] <= 0
    ) {
      console.error("getImageThumb: Invalid img.size", img.size);
      // Fall back to image natural dimensions
      if (img.data && img.data.width > 0 && img.data.height > 0) {
        img.size = [img.data.width, img.data.height];
      } else {
        return new Image(); // Return empty image
      }
    }

    return this.renderThumb(img.data, img.size, width, height);
  },

  renderThumb: function (image, aspect, width, height) {
    if (width <= 0) {
      // scale proportionally by height
      var w = aspect[0] * (height / aspect[1]);
      if (width < 0) {
        // use this negative value for max width
        width = Math.min(w, -width);
      } else {
        width = w;
      }
    } else if (height <= 0) {
      // scale proportionally by width
      var h = aspect[1] * (width / aspect[0]);
      if (height < 0) {
        // use this negative value for max height
        height = Math.min(h, -height);
      } else {
        height = h;
      }
    }

    // Ensure we have valid dimensions
    width = Math.max(1, Math.round(width));
    height = Math.max(1, Math.round(height));

    // cache the rendered data URL per size (lives on the Image, get() ignores it)
    var key = width + "x" + height;
    if (!image._thumbCache) {
      image._thumbCache = {};
    }
    if (!(key in image._thumbCache)) {
      var canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      canvas
        .getContext("2d", { willReadFrequently: true })
        .drawImage(image, 0, 0, width, height);
      image._thumbCache[key] = canvas.toDataURL("image/png");
    }
    var thumb = new Image();
    thumb.src = image._thumbCache[key];
    return thumb;
  },

  // rendering //////////////////////////////////

  render: function () {
    var x = 0;
    var y = 0;
    jobview_clear();
    jobview_feedLayer.activate();
    this.image_group = new paper.Group();
    this.fill_group = new paper.Group();
    this.path_group = new paper.Group();
    // images
    this.loopItems(function (image, i) {
      var img = jobhandler.defs[image.def];
      var group = new paper.Group();
      group.itemidx = i;
      jobhandler.itemidx2group[i] = group;
      jobhandler.image_group.addChild(group);
      var pos_x = img.pos[0] * jobview_mm2px;
      var pos_y = img.pos[1] * jobview_mm2px;
      var img_w = img.size[0] * jobview_mm2px;
      var img_h = img.size[1] * jobview_mm2px;

      // Safety check: ensure valid dimensions before scaling
      if (
        !img.data.width ||
        !img.data.height ||
        img.data.width <= 0 ||
        img.data.height <= 0
      ) {
        console.error(
          "Image " +
            i +
            " has invalid dimensions: " +
            img.data.width +
            "x" +
            img.data.height,
        );
        return;
      }

      var img_paper = new paper.Raster(img.data);
      group.addChild(img_paper);
      img_paper.scale(img_w / img.data.width, img_h / img.data.height);
      img_paper.position = new paper.Point(
        pos_x + 0.5 * img_w,
        pos_y + 0.5 * img_h,
      );
    }, "image");
    // fills
    this.loopItems(function (fill, i) {
      add_vector(fill, i, jobhandler.fill_group);
    }, "fill");
    // paths
    this.loopItems(function (path, i) {
      add_vector(path, i, jobhandler.path_group);
    }, "path");
    // fills, paths helper function
    function add_vector(item, i, parent_group) {
      var path = jobhandler.defs[item.def].data;
      var isFill = jobhandler.defs[item.def].kind === "fill";
      jobview_feedLayer.activate();
      var group = new paper.Group();
      group.itemidx = i;
      if (isFill) {
        group.opacity = 0.5;
      }
      jobhandler.itemidx2group[i] = group;
      parent_group.addChild(group);
      for (var j = 0; j < path.length; j++) {
        var pathseg = path[j];
        if (pathseg.length > 0) {
          // seek move
          jobview_seekLayer.activate();
          var p_seek = new paper.Path();
          p_seek.strokeColor = "#dddddd";
          p_seek.add([x, y]);
          x = pathseg[0][0] * jobview_mm2px;
          y = pathseg[0][1] * jobview_mm2px;
          p_seek.add([x, y]);
          // feed move
          jobview_feedLayer.activate();
          var p_feed = new paper.Path();
          group.addChild(p_feed);
          p_feed.strokeColor = item.color || "#000000";
          p_feed.add([x, y]);
          for (vertex = 1; vertex < pathseg.length; vertex++) {
            x = pathseg[vertex][0] * jobview_mm2px;
            y = pathseg[vertex][1] * jobview_mm2px;
            p_feed.add([x, y]);
          }
        }
      }
    }
  },

  renderBounds: function () {
    jobview_boundsLayer.removeChildren();
    jobview_boundsLayer.activate();
    var bbox = this.getActivePassesBbox();
    var all_bounds = new paper.Path.Rectangle(
      new paper.Point(bbox[0] * jobview_mm2px, bbox[1] * jobview_mm2px),
      new paper.Point(bbox[2] * jobview_mm2px, bbox[3] * jobview_mm2px),
    );
    all_bounds.strokeWidth = 2;
    all_bounds.strokeColor = "#666666";
    all_bounds.dashArray = [2, 4];
  },

  draw: function () {
    paper.view.draw();
  },

  selectItem: function (idx) {
    var idxs = this.groupMembers(idx);
    var groups = [];
    for (var i = 0; i < idxs.length; i++) {
      var group = this.itemidx2group[idxs[i]];
      if (group) {
        group.selected = true;
        groups.push(group);
      }
    }
    jobview_item_selected = idx;
    setTimeout(function () {
      for (var i = 0; i < groups.length; i++) {
        groups[i].selected = false;
      }
      jobview_item_selected = undefined;
      paper.view.draw();
    }, 1500);
    paper.view.draw();
  },

  // passes and colors //////////////////////////

  groupIdenticalImages: function () {
    // image items with byte-identical raster data share one pass entry,
    // like same-color paths do
    this.image_group_rep = {};
    this.image_group_members = {};
    var data2rep = {};
    this.loopItems(function (item, i) {
      var def = jobhandler.defs[item.def];
      var key = def.source;
      if (!key) {
        key = typeof def.data === "string" ? def.data : def.data.src;
      }
      if (key in data2rep) {
        var rep = data2rep[key];
        jobhandler.image_group_rep[i] = rep;
        jobhandler.image_group_members[rep].push(i);
      } else {
        data2rep[key] = i;
        jobhandler.image_group_rep[i] = i;
        jobhandler.image_group_members[i] = [i];
      }
    }, "image");
  },

  groupRep: function (idx) {
    // index of the pass entry representing this item
    if (idx in this.image_group_rep) {
      return this.image_group_rep[idx];
    }
    return idx;
  },

  groupMembers: function (idx) {
    // all item indices sharing the pass entry at idx
    if (idx in this.image_group_members) {
      return this.image_group_members[idx];
    }
    return [idx];
  },

  normalizeColors: function () {
    this.loopItems(function (path, i) {
      if (!("color" in path)) {
        if (path === jobhandler.items[0]) {
          path.color = "#000000";
        } else {
          var random_color =
            "#" + ((Math.random() * 0xaaaaaa) << 0).toString(16);
          path.color = random_color;
        }
      }
    }, "path");
  },

  // stats //////////////////////////////////////

  calculateStats: function () {
    // calculate bounding boxes and feed lengths for each item
    // saves results in this.stats like so:
    // {'all':{'bbox':[xmin,ymin,xmax,ymax], 'len':numeral},
    //  'items':[{'bbox':[xmin,ymin,xmax,ymax], 'len':numeral}, ...],}
    this.stats.items = [];
    var length_all = 0;
    var bbox_all = [Infinity, Infinity, -Infinity, -Infinity];
    // images
    this.loopItems(function (image, i) {
      var img = jobhandler.defs[image.def];
      var width = img.size[0];
      var height = img.size[1];
      var left = img.pos[0];
      var right = img.pos[0] + width;
      var top = img.pos[1];
      var bottom = img.pos[1] + height;
      var line_count = Math.floor(height / app_config_main.pxsize);
      var image_length = width * line_count;
      var image_bbox = [left, top, right, bottom];
      jobhandler.stats.items[i] = { bbox: image_bbox, len: image_length };
      length_all += image_length;
      jobhandler.bboxExpand2(bbox_all, image_bbox);
    }, "image");
    // paths and fills
    this.loopItems(function (vectoritem, i) {
      var path = jobhandler.defs[vectoritem.def].data;
      var path_length = 0;
      var path_bbox = [Infinity, Infinity, -Infinity, -Infinity];
      for (var poly = 0; poly < path.length; poly++) {
        var polyline = path[poly];
        var x_prev = 0;
        var y_prev = 0;
        if (polyline.length > 1) {
          var x = polyline[0][0];
          var y = polyline[0][1];
          jobhandler.bboxExpand(path_bbox, x, y);
          x_prev = x;
          y_prev = y;
          for (vertex = 1; vertex < polyline.length; vertex++) {
            var x = polyline[vertex][0];
            var y = polyline[vertex][1];
            path_length += Math.sqrt(
              (x - x_prev) * (x - x_prev) + (y - y_prev) * (y - y_prev),
            );
            jobhandler.bboxExpand(path_bbox, x, y);
            x_prev = x;
            y_prev = y;
          }
        }
      }
      jobhandler.stats.items[i] = { bbox: path_bbox, len: path_length };
      length_all += path_length;
      jobhandler.bboxExpand(bbox_all, path_bbox[0], path_bbox[1]);
      jobhandler.bboxExpand(bbox_all, path_bbox[2], path_bbox[3]);
    }, "path fill");
    // store in object var
    this.stats["all"] = { bbox: bbox_all, len: length_all };
  },

  getActivePassesLength: function () {
    var length = 0;
    this.loopPasses(function (pass, item_idxs) {
      for (var i = 0; i < item_idxs.length; i++) {
        var item = item_idxs[i];
        length += jobhandler.stats.items[item_idxs[i]].len;
      }
    });
    return length;
  },

  getSeekPassesLength: function () {
    var length = 0;
    // loop over the passes
    this.loopPasses(function (pass, item_idxs) {
      // loop over the items
      for (var iPass = 0; iPass < item_idxs.length; iPass++) {
        var itemIdx = item_idxs[iPass];
        var itemObj = jobhandler.items[itemIdx];
        var def = jobhandler.defs[itemObj.def];

        // Skip image items - they don't have path data for seek calculation
        if (def.kind === "image") {
          continue;
        }

        // loop over the individual paths in the item
        var pathData = def.data;
        if (!pathData || !Array.isArray(pathData) || pathData.length < 2) {
          continue;
        }

        var lastPath = null;
        for (var iPath = 0; iPath < pathData.length - 1; iPath++) {
          var path = pathData[iPath];
          var nextPath = pathData[iPath + 1];
          if (path && path.length > 0 && nextPath && nextPath.length > 0) {
            var endPoint = path[path.length - 1];
            var startPoint = nextPath[0];
            // add distance to the seek-length
            length += Math.sqrt(
              (startPoint[0] - endPoint[0]) * (startPoint[0] - endPoint[0]) +
                (startPoint[1] - endPoint[1]) * (startPoint[1] - endPoint[1]),
            );
          }
          lastPath = nextPath;
        }

        // Calculate seek between items
        if (iPass < item_idxs.length - 1 && lastPath && lastPath.length > 0) {
          var nextItemIdx = item_idxs[iPass + 1];
          var nextItemObj = jobhandler.items[nextItemIdx];
          var nextItemDef = jobhandler.defs[nextItemObj.def];
          if (
            nextItemDef.kind !== "image" &&
            nextItemDef.data &&
            Array.isArray(nextItemDef.data) &&
            nextItemDef.data.length > 0
          ) {
            var lastPointLastPass = lastPath[lastPath.length - 1];
            var firstPathNextItem = nextItemDef.data[0];
            if (firstPathNextItem && firstPathNextItem.length > 0) {
              var firstPointNextPass = firstPathNextItem[0];
              length += Math.sqrt(
                (firstPointNextPass[0] - lastPointLastPass[0]) *
                  (firstPointNextPass[0] - lastPointLastPass[0]) +
                  (firstPointNextPass[1] - lastPointLastPass[1]) *
                    (firstPointNextPass[1] - lastPointLastPass[1]),
              );
            }
          }
        }
      }
    });
    return length;
  },

  getActivePassesDuration: function () {
    var duration = 0;
    this.loopPasses(function (pass, item_idxs) {
      for (var i = 0; i < item_idxs.length; i++) {
        var item = item_idxs[i];
        duration += (1 / pass.feedrate) * jobhandler.stats.items[item].len;
      }
    });
    return duration;
  },

  getActivePassesBbox: function () {
    var bbox = [Infinity, Infinity, -Infinity, -Infinity];
    this.loopPasses(function (pass, item_idxs) {
      for (var i = 0; i < item_idxs.length; i++) {
        var item = item_idxs[i];
        jobhandler.bboxExpand2(bbox, jobhandler.stats.items[item_idxs[i]].bbox);
      }
    });
    return bbox;
  },

  bboxExpand: function (bbox, x, y) {
    if (x < bbox[0]) {
      bbox[0] = x;
    }
    if (x > bbox[2]) {
      bbox[2] = x;
    }
    if (y < bbox[1]) {
      bbox[1] = y;
    }
    if (y > bbox[3]) {
      bbox[3] = y;
    }
  },

  bboxExpand2: function (bbox, bbox2) {
    this.bboxExpand(bbox, bbox2[0], bbox2[1]);
    this.bboxExpand(bbox, bbox2[2], bbox2[3]);
  },
};
