function request_get(args) {
  // args items: url, success, error, complete
  $.ajax({
    type: "GET",
    url: args.url,
    dataType: "json",
    username: "laser",
    password: "laser",
    headers: {
      Authorization: "Basic " + btoa("laser:laser"),
    },
    statusCode: {
      400: function (s) {
        // alert(JSON.stringify(s))
        if ("responseText" in s) {
          $().uxmessage("error", s.responseText);
        }
      },
      401: function () {
        $().uxmessage("error", "Wrong password/username.");
      },
    },
    success: function (data) {
      if ("success" in args) {
        args.success(data);
      }
    },
    error: function (data) {
      if ("error" in args) {
        args.error(data);
      } else {
        $().uxmessage("error", data.responseText, false);
      }
    },
    complete: function (data) {
      if ("complete" in args) {
        args.complete(data);
      }
    },
  });
}

function request_post(args) {
  // args items: url, data, success, error, complete

  // special case load job: the job travels as its own file part so the
  // megabytes never go through JSON.stringify
  if (args.url == "/load") {
    request_load_part(args.data.job, function (part) {
      var formData = new FormData();
      formData.append("job", part.file);
      args.data.job = part.marker;
      formData.append("load_request", JSON.stringify(args.data));
      request_post_send(args, formData);
    });
    return;
  }
  var formData = new FormData();
  formData.append("load_request", JSON.stringify(args.data));
  request_post_send(args, formData);
}

function request_use_gzip() {
  // gzip trades about two seconds of cpu per 40MB against a third fewer
  // bytes, which only pays for itself under roughly 50Mbit, so it is worth
  // it on a phone link and a waste on anything local or on a lan
  if (!app_config_main.enable_gzip) {
    return false;
  }
  if (app_config_main.force_gzip) {
    // for a slow link the browser reports as fast, or does not report at all
    return true;
  }
  var host = window.location.hostname;
  if (
    host == "" ||
    host == "localhost" ||
    host == "127.0.0.1" ||
    host == "::1" ||
    host == "[::1]"
  ) {
    return false;
  }
  var net =
    navigator.connection ||
    navigator.mozConnection ||
    navigator.webkitConnection;
  if (!net || !net.effectiveType) {
    return false;
  }
  return ["slow-2g", "2g", "3g"].indexOf(net.effectiveType) != -1;
}

function request_load_part(job, done) {
  // job is either a File the user picked or a job string
  if (!request_use_gzip()) {
    var raw = job instanceof File ? job : new File([job], "upload.dba");
    done({ file: raw, marker: "upload_raw" });
    return;
  }
  request_gzip(job, function (bytes) {
    done({ file: new File([bytes], "upload.gz"), marker: "upload" });
  });
}

function request_gzip(job, done) {
  // gzip in a worker so a big job does not freeze the interface, and at
  // level 1 because the extra levels cost far more time than bytes
  function on_main_thread() {
    if (job instanceof File) {
      var fr = new FileReader();
      fr.onload = function (e) {
        done(pako.gzip(new Uint8Array(e.target.result), { level: 1 }));
      };
      fr.readAsArrayBuffer(job);
    } else {
      done(pako.gzip(job, { level: 1 }));
    }
  }
  if (typeof Worker === "undefined") {
    on_main_thread();
    return;
  }
  var worker;
  try {
    worker = new Worker("js/gzip_worker.js");
  } catch (e) {
    on_main_thread();
    return;
  }
  worker.onmessage = function (e) {
    worker.terminate();
    done(e.data);
  };
  worker.onerror = function () {
    worker.terminate();
    on_main_thread();
  };
  worker.postMessage(job);
}

function request_post_send(args, formData) {
  $.ajax({
    type: "POST",
    url: args.url,
    // data: {'load_request':JSON.stringify(args.data)},
    data: formData,
    dataType: "json",
    contentType: false,
    processData: false,
    cache: false,
    username: "laser",
    password: "laser",
    headers: {
      Authorization: "Basic " + btoa("laser:laser"),
    },
    statusCode: {
      400: function (s) {
        // alert(JSON.stringify(s))
        if ("responseText" in s) {
          $().uxmessage("error", s.responseText);
        }
      },
      401: function () {
        $().uxmessage("error", "Wrong password/username.");
      },
    },
    success: function (data) {
      if ("success" in args) {
        args.success(data);
      }
    },
    error: function (data) {
      // $().uxmessage('error', args.url)
      if ("error" in args) {
        args.error(data);
      }
    },
    complete: function (data) {
      if ("complete" in args) {
        args.complete(data);
      }
    },
  });
}

function request_boundary(bounds, seekrate) {
  var job = {
    head: {},
    passes: [
      {
        items: [0],
        seekrate: seekrate,
        feedrate: seekrate,
        air_assist: "off",
      },
    ],
    items: [{ def: 0 }],
    defs: [
      {
        kind: "path",
        data: [
          [
            [bounds[0], bounds[1], 0],
            [bounds[2], bounds[1], 0],
            [bounds[2], bounds[3], 0],
            [bounds[0], bounds[3], 0],
            [bounds[0], bounds[1], 0],
          ],
        ],
      },
    ],
  };
  request_post({
    url: "/run",
    data: { job: request_stringify(job) },
    success: function (data) {
      $().uxmessage("notice", "Running boundary.");
    },
  });
}

function request_jog(x, y, z, success_msg) {
  request_get({
    url: "/jog/" + x + "/" + y + "/" + z,
    success: function (data) {
      if (data && data.clamped) {
        $().uxmessage("warning", "Jog held at the edge of the work area.");
      } else {
        $().uxmessage("notice", success_msg);
      }
    },
  });
}

function request_relative_move(x, y, z, seekrate, success_msg) {
  // DEPRECATED
  var job = {
    head: { noreturn: true },
    passes: [
      {
        items: [0],
        relative: true,
        seekrate: seekrate,
        air_assist: "off",
      },
    ],
    items: [{ def: 0 }],
    defs: [{ kind: "path", data: [[[x, y, z]]] }],
  };
  request_post({
    url: "/run",
    data: { job: request_stringify(job) },
    success: function (data) {
      $().uxmessage("notice", success_msg);
    },
  });
}

function request_absolute_move(x, y, z, seekrate, success_msg) {
  var job = {
    head: { noreturn: true },
    passes: [
      {
        items: [0],
        seekrate: seekrate,
        air_assist: "off",
      },
    ],
    items: [{ def: 0 }],
    defs: [{ kind: "path", data: [[[x, y, z]]] }],
  };
  request_post({
    url: "/run",
    data: { job: request_stringify(job) },
    success: function (data) {
      $().uxmessage("notice", success_msg);
    },
  });
}

function request_stringify(data) {
  // json stringify while limiting numbers to 3 decimals
  return JSON.stringify(data, function (key, val) {
    return typeof val === "number" ? Number(val.toFixed(3)) : val;
  });
}
