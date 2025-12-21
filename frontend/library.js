function library_clear() {
  $("#library_content").html("");
}

function library_ready() {
  library_update();
}

function library_update() {
  request_get({
    url: "/listing_library",
    success: function (data) {
      var files = data.files;
      var html =
        '<p class="text-muted" style="margin-bottom:15px; word-break:break-all;">';
      html += "<strong>Library folder:</strong> " + (data.path || "Unknown");
      html += "</p>";
      html += '<table class="table table-hover table-condensed">';
      html += "<thead><tr><td>Name</td></tr></thead><tbody>";
      files.reverse();
      for (var i = 0; i < files.length; i++) {
        html += '<tr style="cursor:pointer"><td>' + files[i] + "</td></tr>";
      }
      html += "</tbody></table>";
      $("#library_content").html(html);
      // load action
      $("#library_content table tbody tr").click(function (e) {
        var jobname = $(this).children("td").text();
        import_open(jobname, true);
        $("#library_modal").modal("toggle");
        return false;
      });
    },
  });
}
