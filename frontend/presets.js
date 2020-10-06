var favorites = [
]

function favorites_clear() {
  $('#favorites_content').html("")
}

function favorites_ready() {
  favorites_update()
  // Make the name input field active when the modal is shown
  $('#favorites_modal').on('shown.bs.modal', function() {
    $('#favorite_name').focus()
  })
}

function save_favorite() {
  var name = $('#favorite_name').val()
  var feedrate = parseInt($('#favorite_feedrate').val())
  var intensity = parseInt($('#favorite_intensity').val())
  if (!isNaN(feedrate) && !isNaN(intensity) & name.length > 0 && feedrate+intensity > 0) {
    request_get({
      url:`/save_favorite/${name}/${feedrate}/${intensity}`
    })
    $('#favorites_modal').modal('toggle')
    favorites_update()
  }
  return false
}

function favorites_update() {
  request_get({
    url:'/listing_favorites',
    success: function (data) {
      favorites = data
      var html = `
<table class="table table-hover table-condensed">
  <thead>
    <tr>
      <td>Name</td>
      <td style="text-align:right;">Feedrate</td>
      <td style="text-align:right;">Intensity</td>
      <td></td>
    </tr>
    </thead>
    <tbody>
    <tr>
      <td>
        <input id="favorite_name" type="text" class="form-control input-sm" value="" title="favorite name">
      </td>
      <td>
        <input id="favorite_feedrate" type="text" class="form-control input-sm" style="width:50px; margin-left:auto;" value="" title="favorite feedrate">
      </td>
      <td>
        <input id="favorite_intensity" type="text" class="form-control input-sm" style="width:44px; margin-left:auto;" value="" title="favorite intensity">
      </td>
      <td style="text-align:right;">
        <a id="favorite_ok" class="btn" role="button">
          <span class="glyphicon glyphicon-ok" style="color:#00A000"></span>
        </a>
      </td>
    </tr>
  `
      for (var i = 0; i < data.length; i++) {
        html += `
    <tr>
      <td class="fav-name">${data[i].name}</td>
      <td style="text-align:right;">${data[i].feedrate}</td>
      <td style="text-align:right;">${data[i].intensity}%</td>
      <td style="text-align:right;">
        <a id="del_favorite_btn_${i}" class="btn btn-del-fav" style="margin-left:8px; position:relative; top:1px" role="button">
          <span class="glyphicon glyphicon-trash" style="color:#888888"></span>
        </a>
      </td>
    </tr>
    `
      }
      html += '</tbody></table>'
      $('#favorites_content').html(html)

      // save actions
      $('#favorite_ok').click(save_favorite)
      $('#favorite_name').keyup(function(e) {
        if (e.which == 13) {
          save_favorite()
          return false
        }
        return true
      })

      // delete action
      $('.btn-del-fav').click(function(e) {
        var name = $(this).parent().parent().find('td.fav-name').text()
        request_get({
          url:`/save_favorite/${name}/0/0`,
          success: function() {
            favorites_update()
          }
        })
      })

      // update existing favorites menus, if any
      passes_update_favorites()
    }
  })
}
