var app_config_main = undefined;
var app_config_editable = undefined;
var app_config_defaults = undefined;
var app_config_path = undefined;
var app_run_btn = undefined;
var app_fill_btn = undefined;
var app_visibility = true;

// toast messages, install jquery plugin
(function ($) {
  $.fn.uxmessage = function (kind, text, max_length) {
    if (max_length == undefined) {
      max_length = 100;
    }

    if (max_length !== false && text.length > max_length) {
      text = text.slice(0, max_length) + "\n...";
    }

    text = text.replace(/\n/g, "<br>");

    if (kind == "notice") {
      $("#log_content").prepend(
        '<div class="log_item log_notice well" style="display:none">' +
          text +
          "</div>",
      );
      $("#log_content").children("div").first().show("blind");
      if ($("#log_content").is(":hidden")) {
        $().toastmessage("showToast", {
          text: text,
          sticky: false,
          position: "top-center",
          type: "notice",
        });
      }
    } else if (kind == "success") {
      $("#log_content").prepend(
        '<div class="log_item log_success well" style="display:none">' +
          text +
          "</div>",
      );
      $("#log_content").children("div").first().show("blind");
      if ($("#log_content").is(":hidden")) {
        $().toastmessage("showToast", {
          text: text,
          sticky: false,
          position: "top-center",
          type: "success",
        });
      }
    } else if (kind == "warning") {
      $("#log_content").prepend(
        '<div class="log_item log_warning well" style="display:none">' +
          text +
          "</div>",
      );
      $("#log_content").children("div").first().show("blind");
      if ($("#log_content").is(":hidden")) {
        $().toastmessage("showToast", {
          text: text,
          sticky: false,
          position: "top-center",
          type: "warning",
        });
      }
    } else if (kind == "error") {
      $("#log_content").prepend(
        '<div class="log_item log_error well" style="display:none">' +
          text +
          "</div>",
      );
      $("#log_content").children("div").first().show("blind");
      if ($("#log_content").is(":hidden")) {
        $().toastmessage("showToast", {
          text: text,
          sticky: false,
          position: "top-center",
          type: "error",
          stayTime: 6000,
        });
      }
    }

    while ($("#log_content").children("div").length > 200) {
      $("#log_content").children("div").last().remove();
    }
  };
})(jQuery);

///////////////////////////////////////////////////////////////////////////////
///////////////////////////////////////////////////////////////////////////////

$(document).ready(function () {
  // $().uxmessage('notice', "Frontend started.")
  // modern browser check
  if (!Object.hasOwnProperty("keys")) {
    alert("Error: Browser may be too old/non-standard.");
  }

  // unblur button after pressing
  $(".btn").mouseup(function () {
    // $(this).blur()
    this.blur();
  });

  // run_btn, make a ladda progress spinner button
  // http://msurguy.github.io/ladda-bootstrap/
  app_run_btn = Ladda.create($("#run_btn")[0]);
  app_fill_btn = Ladda.create($("#addfill_btn")[0]);

  // page visibility events
  window.onfocus = function () {
    app_visibility = true;
    status_set_refresh();
    // console.log("onfocus")
  };
  window.onblur = function () {
    app_visibility = false;
    status_set_refresh();
    // console.log("onblur")
  };

  // connecting modal
  $("#connect_modal").modal({
    show: true,
    keyboard: false,
    backdrop: "static",
  });

  // get appconfig from server
  request_get({
    url: "/config",
    success: function (data) {
      // $().uxmessage('success', "App config received.")
      app_config_main = data.values;
      app_config_editable = data.editable;
      app_config_defaults = data.defaults;
      app_config_path = data.configpath;
      config_received();
    },
    error: function (data) {
      $().uxmessage("error", "Failed to receive app config.");
    },
    complete: function (data) {},
  });
});

function config_received() {
  // build editable config form
  config_build_form();

  // about modal
  $("#app_version").html(app_config_main.version);
  // $('#firmware_version').html(app_config_main.)

  // call 'ready' of jobview
  jobview_ready();
  // call 'ready' of controls
  controls_ready();
  // call 'ready' of queue
  queue_ready();
  // call 'ready' of library
  library_ready();
  // call 'ready' of status
  status_ready();
  // call 'ready' of presets
  presets_ready();
}

function config_build_form() {
  // Show config file path at top
  var html =
    '<p class="text-muted" style="margin-bottom:15px; word-break:break-all;">';
  html += "<strong>Config file:</strong> " + (app_config_path || "Unknown");
  html += "</p>";

  html += '<table class="table table-condensed" style="margin-bottom:0">';
  html +=
    "<thead><tr><th>Setting</th><th>Value</th><th></th></tr></thead><tbody>";

  var keys_sorted = Object.keys(app_config_editable).sort();
  for (var i = 0; i < keys_sorted.length; i++) {
    var key = keys_sorted[i];
    // Skip fields that aren't in the values (e.g., users is excluded for security)
    if (!(key in app_config_main)) continue;
    var value = app_config_main[key];
    var description = app_config_editable[key];
    var defaultValue = app_config_defaults[key];
    var isDefault = JSON.stringify(value) === JSON.stringify(defaultValue);

    html += '<tr data-key="' + key + '">';
    html += '<td style="vertical-align:middle"><strong>' + key + "</strong>";
    html += '<br><small class="text-muted">' + description + "</small></td>";
    html += "<td>" + config_render_input(key, value) + "</td>";
    html += '<td style="vertical-align:middle; white-space:nowrap;">';
    html +=
      '<button type="button" class="btn btn-xs btn-warning config-reset-btn" data-key="' +
      key +
      '" title="Reset to default: ' +
      JSON.stringify(defaultValue) +
      '"' +
      (isDefault ? " disabled" : "") +
      ">";
    html += "Reset";
    html += "</button>";
    html += "</td>";
    html += "</tr>";
  }
  html += "</tbody></table>";

  $("#config_content").html(html);

  // bind input change events to update reset button state
  $("#config_content")
    .off("change input", "input")
    .on("change input", "input", function () {
      var $input = $(this);
      var key = $input.attr("data-key");
      if (!key) return;

      var currentValue = config_get_input_value(key);
      var defaultValue = app_config_defaults[key];
      var isDefault =
        JSON.stringify(currentValue) === JSON.stringify(defaultValue);

      var $resetBtn = $(".config-reset-btn[data-key='" + key + "']");
      $resetBtn.prop("disabled", isDefault);
    });

  // bind reset button clicks using event delegation
  $("#config_content")
    .off("click", ".config-reset-btn")
    .on("click", ".config-reset-btn", function (e) {
      e.preventDefault();
      e.stopPropagation();
      var $btn = $(this);
      if ($btn.prop("disabled")) return;
      var key = $btn.attr("data-key");
      config_reset_field(key);
    });

  // bind save button click
  $("#config_save_btn")
    .off("click")
    .on("click", function () {
      config_save();
    });
}

function config_render_input(key, value) {
  var inputId = "config_input_" + key;
  var valueType = typeof value;

  if (valueType === "boolean") {
    return (
      '<input type="checkbox" id="' +
      inputId +
      '" ' +
      (value ? "checked" : "") +
      ' data-key="' +
      key +
      '">'
    );
  } else if (Array.isArray(value)) {
    return (
      '<input type="text" class="form-control input-sm" id="' +
      inputId +
      "\" value='" +
      JSON.stringify(value) +
      "' data-key=\"" +
      key +
      '" style="width:200px">'
    );
  } else if (valueType === "object" && value !== null) {
    return (
      '<input type="text" class="form-control input-sm" id="' +
      inputId +
      "\" value='" +
      JSON.stringify(value) +
      "' data-key=\"" +
      key +
      '" style="width:200px">'
    );
  } else if (valueType === "number") {
    return (
      '<input type="number" class="form-control input-sm" id="' +
      inputId +
      '" value="' +
      value +
      '" data-key="' +
      key +
      '" style="width:120px" step="any">'
    );
  } else {
    // string or other
    return (
      '<input type="text" class="form-control input-sm" id="' +
      inputId +
      '" value="' +
      (value || "") +
      '" data-key="' +
      key +
      '" style="width:200px">'
    );
  }
}

function config_get_input_value(key) {
  var inputId = "#config_input_" + key;
  var $input = $(inputId);
  var originalValue = app_config_main[key];
  var valueType = typeof originalValue;

  if (valueType === "boolean") {
    return $input.is(":checked");
  } else if (
    Array.isArray(originalValue) ||
    (valueType === "object" && originalValue !== null)
  ) {
    try {
      return JSON.parse($input.val());
    } catch (e) {
      return $input.val();
    }
  } else if (valueType === "number") {
    var num = parseFloat($input.val());
    return isNaN(num) ? $input.val() : num;
  } else {
    return $input.val();
  }
}

function config_save() {
  var keys = Object.keys(app_config_editable);
  var changedCount = 0;
  var pendingRequests = 0;

  for (var i = 0; i < keys.length; i++) {
    var key = keys[i];
    // Skip fields that aren't in the values
    if (!(key in app_config_main)) continue;
    var newValue = config_get_input_value(key);
    var oldValue = app_config_main[key];

    if (JSON.stringify(newValue) !== JSON.stringify(oldValue)) {
      changedCount++;
      pendingRequests++;

      (function (k, v, encodedV) {
        request_get({
          url: "/config/" + k + "/" + encodedV,
          success: function () {
            app_config_main[k] = v;
            pendingRequests--;
            if (pendingRequests === 0) {
              $().uxmessage("success", "Configuration saved.");
              config_build_form();
            }
          },
          error: function () {
            pendingRequests--;
            $().uxmessage("error", "Failed to save " + k);
          },
        });
      })(key, newValue, encodeURIComponent(JSON.stringify(newValue)));
    }
  }

  if (changedCount === 0) {
    $().uxmessage("notice", "No changes to save.");
  }
}

function config_reset_field(key) {
  // Reset the input field to the default value (doesn't save until Save is clicked)
  var defaultValue = app_config_defaults[key];
  var inputId = "#config_input_" + key;
  var $input = $(inputId);

  if (typeof defaultValue === "boolean") {
    $input.prop("checked", defaultValue);
  } else if (
    Array.isArray(defaultValue) ||
    (typeof defaultValue === "object" && defaultValue !== null)
  ) {
    $input.val(JSON.stringify(defaultValue));
  } else {
    $input.val(defaultValue);
  }

  // Update reset button state
  $(".config-reset-btn[data-key='" + key + "']").prop("disabled", true);
  $().uxmessage("notice", key + " reset to default. Click Save to apply.");
}
