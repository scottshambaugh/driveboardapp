var presets = [
]

function presets_clear() {
  $('#presets_content').html("")
}

function presets_ready() {
  presets_update()
  // Make the name input field active when the modal is shown
  $('#presets_modal').on('shown.bs.modal', function() {
    $('#preset_name').focus()
  })
}

function save_preset() {
  var name = $('#preset_name').val()
  var feedrate = parseInt($('#preset_feedrate').val())
  var intensity = parseInt($('#preset_intensity').val())
  var pxsize = parseFloat($('#preset_pxsize').val())
  if (!isNaN(feedrate) && !isNaN(intensity) & !isNaN(pxsize) & name.length > 0 && feedrate+intensity > 0) {
    request_get({
      url:`/save_preset/${name}/${feedrate}/${intensity}/${pxsize}`
    })
    $('#presets_modal').modal('toggle')
    presets_update()
  }
  return false
}

function presets_update() {
  request_get({
    url:'/listing_presets',
    success: function (data) {
      presets = data
      var html = `
<table class="table table-hover table-condensed">
  <thead>
    <tr>
      <td>Name</td>
      <td style="text-align:right;">Feedrate</td>
      <td style="text-align:right;">Intensity</td>
      <td style="text-align:right;">Pxsize</td>
      <td></td>
    </tr>
    </thead>
    <tbody>
    <tr>
      <td>
        <input id="preset_name" type="text" class="form-control input-sm" value="" title="preset name">
      </td>
      <td>
        <input id="preset_feedrate" type="text" class="form-control input-sm" style="width:50px; margin-left:auto;" value="" title="preset feedrate">
      </td>
      <td>
        <input id="preset_intensity" type="text" class="form-control input-sm" style="width:44px; margin-left:auto;" value="" title="preset intensity">
      </td>
      <td>
        <input id="preset_pxsize" type="text" class="form-control input-sm" style="width:44px; margin-left:auto;" value="" title="pixel size">
      </td>
      <td style="text-align:right;">
        <a id="preset_ok" class="btn" role="button">
          <span class="glyphicon glyphicon-ok" style="color:#00A000"></span>
        </a>
      </td>
    </tr>
  `
      for (var i = 0; i < data.length; i++) {
        html += `
    <tr>
      <td class="preset-name">${data[i].name}</td>
      <td style="text-align:right;">${data[i].feedrate}</td>
      <td style="text-align:right;">${data[i].intensity}%</td>
      <td style="text-align:right;">${data[i].pxsize}%</td>
      <td style="text-align:right;">
        <a id="del_preset_btn_${i}" class="btn btn-del-preset" style="margin-left:8px; position:relative; top:1px" role="button">
          <span class="glyphicon glyphicon-trash" style="color:#888888"></span>
        </a>
      </td>
    </tr>
    `
      }
      html += '</tbody></table>'
      $('#presets_content').html(html)

      // save actions
      $('#preset_ok').click(save_preset)
      $('#preset_name').keyup(function(e) {
        if (e.which == 13) {
          save_preset()
          return false
        }
        return true
      })

      // delete action
      $('.btn-del-preset').click(function(e) {
        var name = $(this).parent().parent().find('td.preset-name').text()
        request_get({
          url:`/save_preset/${name}/0/0`,
          success: function() {
            presets_update()
          }
        })
      })

      // update existing presets menus, if any
      passes_update_presets()
    }
  })
}
