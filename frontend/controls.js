

function controls_ready() {


  // dropdown //////////////////////////////////////////////////////////////

  $("#info_btn").tooltip({placement:'bottom', delay: {show:1000, hide:100}})
  $("#info_btn").click(function(e){
    $('#info_modal').modal('toggle')
    return false
  })

  $("#export_btn").tooltip({placement:'bottom', delay: {show:1000, hide:100}})
  $("#export_btn").click(function(e){
    if (!jobhandler.isEmpty()) {
      var filename = jobhandler.name
      if (filename.length > 4 && filename.slice(-4,-3) == '.') {
        filename = filename.slice(0,-4)+'.dba'
      } else {
        filename = filename+'.dba'
      }
      jobhandler.passes = passes_get_active()
      var blob = new Blob([jobhandler.getJson('\t')], {type: "application/json;charset=utf-8"})
      saveAs(blob, filename)
      // var load_request = {'job':jobhandler.getJson()}
      // request_post({
      //   url:'/temp',
      //   data: load_request,
      //   success: function (jobname) {
      //     console.log("stashing successful")
      //     // download file
      //     window.open('/download/'+jobname+'/'+jobhandler.name+'.dba', '_blank')
      //   },
      //   error: function (data) {
      //     $().uxmessage('error', "/temp error.")
      //     $().uxmessage('error', JSON.stringify(data), false)
      //   }
      // })
      // $('#hamburger').dropdown("toggle")
    } else {
      $().uxmessage('notice', "Cannot Export. No job loaded.")
    }
    $("body").trigger("click")
    return false
  })

  $("#clear_btn").tooltip({placement:'bottom', delay: {show:1000, hide:100}})
  $("#clear_btn").click(function(e){
    jobhandler.clear()
    tools_addfill_init()
    $("body").trigger("click")
    return false
  })

  $("#queue_btn").tooltip({placement:'bottom', delay: {show:1000, hide:100}})
  $("#queue_btn").click(function(e){
    $("body").trigger("click")
    $('#queue_modal').modal('toggle')
    return false
  })

  $("#library_btn").tooltip({placement:'bottom', delay: {show:1000, hide:100}})
  $("#library_btn").click(function(e){
    $("body").trigger("click")
    $('#library_modal').modal('toggle')
    return false
  })

  $("#flash_btn").tooltip({placement:'bottom', delay: {show:1000, hide:100}})
  $("#flash_btn").click(function(e){
    $().uxmessage('notice', "Flashing firmware, may take several minutes...")
    request_get({
      url:'/flash',
      success: function (data) {
        status_cache.firmver = undefined
        $().uxmessage('success', "Flashing successful.")
      }
    })
    $("body").trigger("click")
    return false
  })

  $("#rebuild_btn").tooltip({placement:'bottom', delay: {show:1000, hide:100}})
  $("#rebuild_btn").click(function(e){
    $().uxmessage('notice', "Building firmware, may take several minutes...")
    request_get({
      url:'/build',
      success: function (data) {
        $().uxmessage('notice', "Firmware build successful.")
      }
    })
    $("body").trigger("click")
    return false
  })

  $("#reset_btn").tooltip({placement:'bottom', delay: {show:1000, hide:100}})
  $("#reset_btn").click(function(e){
    request_get({
      url:'/reset',
      success: function (data) {
        status_cache.firmver = undefined
        $().uxmessage('success', "Reset successful.")
      }
    })
    $("body").trigger("click")
    return false
  })



  // navbar ////////////////////////////////////////////////////////////////


  $("#open_btn").tooltip({placement:'bottom', delay: {show:1000, hide:100}})
  $("#open_btn").click(function(e){
    $('#open_file_fld').trigger('click')
    return false
  })

  if (app_config_main.alignment_host) {
    $("#open_align_btn").tooltip({placement:'bottom', delay: {show:1000, hide:100}})
    $("#open_align_btn").click(function(e){
      $('#open_align_file_fld').trigger('click')
      return false
    })
  } else {
    $("#open_align_container").hide()
  }

  $("#run_btn").tooltip({placement:'bottom', delay: {show:1000, hide:100}})
  $("#run_btn").click(function(e){
    jobhandler.passes = passes_get_active()
    // check for job
    if (jobhandler.isEmpty()) {
      $().uxmessage('notice', "Cannot run. No job loaded.")
      return false
    }
    // check for passes
    if (!jobhandler.hasPasses()) {
      $().uxmessage('notice', "No passes assigned to this job.")
      return false
    }
    // check for machine
    if (!status_cache.serial) {
      $().uxmessage('error', "No machine.")
      return false
    }
    // button feedback
    app_run_btn.start()
    $('#boundary_btn').prop('disabled', true)
    status_cache.ready = true  // prevent ready update
    // save job to queue, in-place
    var load_request = {
      'job':jobhandler.getJson(),
      'name':jobhandler.name,
      'optimize':true,
      // 'optimize':false,
      'overwrite':true
    }
    request_post({
      url:'/load',
      data: load_request,
      success: function (jobname) {
        // $().uxmessage('notice', "Saved to queue: "+jobname)
        // run job
        request_get({
          url:'/run/'+jobname,
          success: function (data) {
            // $().uxmessage('success', "Running job ...")
          },
          error: function (data) {
            $().uxmessage('error', "/run error.")
            app_run_btn.stop()
          },
          complete: function (data) {
            // console.log("complete run")
          }
        })
      },
      error: function (data) {
        $().uxmessage('error', "/load error.")
        $().uxmessage('error', JSON.stringify(data), false)
        app_run_btn.stop()
      },
      complete: function (data) {
        status_cache.ready = undefined  // allow ready update
        // console.log("complete load")
      }
    })
    return false
  })

  $("#boundary_btn").tooltip({placement:'bottom', delay: {show:1000, hide:100}})
  $("#boundary_btn").click(function(e){
    jobhandler.passes = passes_get_active()
    // check for job
    if (jobhandler.isEmpty()) {
      $().uxmessage('notice', "Cannot run. No job loaded.")
      return false
    }
    // check for passes
    if (!jobhandler.hasPasses()) {
      $().uxmessage('notice', "No passes assigned to this job.")
      return false
    }
    // check for machine
    if (!status_cache.serial) {
      $().uxmessage('error', "No machine.")
      return false
    }
    // send bounds request
    var bounds = jobhandler.getActivePassesBbox()
    request_boundary(bounds, app_config_main.seekrate)
    return false
  })

  $("#pause_btn").tooltip({placement:'bottom', delay: {show:1000, hide:100}})
  $("#pause_btn").click(function(e){
    if (status_cache.paused) {  // unpause
      request_get({
        url:'/unpause',
        success: function (data) {
          // $().uxmessage('notice', "Continuing...")
        }
      })
    } else {  // pause
      request_get({
        url:'/pause',
        success: function (data) {
          // $().uxmessage('notice', "Pausing in a bit...")
        }
      })
    }
    return false
  })

  $("#stop_btn").tooltip({placement:'bottom', delay: {show:1000, hide:100}})
  $("#stop_btn").click(function(e){
    request_get({
      url:'/stop',
      success: function (data) {
        setTimeout(function() {
          request_get({
            url:'/unstop',
            success: function (data) {
              request_absolute_move(0, 0, 0, app_config_main.seekrate, "Moving to Origin.")
            }
          })
        }, 1500)
      }
    });
    return false
  })



  // footer buttons /////////////////////////////////////////////////////////


  $("#origin_btn").tooltip({placement:'top', delay: {show:1000, hide:100}})
  $("#origin_btn").click(function(e){
    var gcode;
    if(e.shiftKey) {
      // also reset offset
      alert("TODO: reset offset")
      reset_offset____();  // TODO
    }
    request_absolute_move(0, 0, 0, app_config_main.seekrate, "Moving to Origin.")
    return false
  })

  $("#homing_btn").tooltip({placement:'top', delay: {show:1000, hide:100}})
  $("#homing_btn").click(function(e){
    request_get({
      url:'/homing',
      success: function (data) {
        $().uxmessage('notice', "Homing ...")
		$('#offset_reset_btn').trigger('click')
      }
    })
    return false
  })


  $("#select_btn").tooltip({placement:'top', delay: {show:1000, hide:100}})
  $("#select_btn").click(function(e){
    jobview_jogLayer.visible = false
    $(".tool_extra_btn").hide()
    tools_tselect.activate()
    $("#addfill_wgt").show()
    jobview_moveLayer.visible = false
    return true
  })

  $("#addfill_btn").tooltip({placement:'right', delay: {show:1000, hide:100}})
  $("#addfill_btn").click(function(e){
    if (jobview_item_selected !== undefined) {
      var kind = jobhandler.defs[jobhandler.items[jobview_item_selected].def].kind
      if (kind != "path") {
        $().uxmessage('notice', "Make sure a path is selected.")
        return false
      }
      app_fill_btn.start()
      fills_add_by_item(jobview_item_selected,
        function() {
          app_fill_btn.stop()
      })
      return false
    } else {
      return true
    }
  })

  $("#offset_set_btn").tooltip({placement:'top', delay: {show:1000, hide:100}})
  $("#offset_set_btn").click(function(e){
    if (!$(this).hasClass('disabled')) {
      jobview_jogLayer.visible = false
	  jobview_moveLayer.visible = false
      $(".tool_extra_btn").hide()
      tools_toffset.offset_set()
    } else {
      setTimeout(function(){
        $('#select_btn').trigger('click')
      },500)
    }
    return true
  })

  /*
  $("#offset_btn").tooltip({placement:'top', delay: {show:1000, hide:100}})
  $("#offset_btn").click(function(e){
    if (!$(this).hasClass('disabled')) {
      jobview_jogLayer.visible = false
      $(".tool_extra_btn").hide()
      tools_toffset.activate()
      $("#offset_reset_btn").show()
      jobview_moveLayer.visible = false
    } else {
      setTimeout(function(){
        $('#select_btn').trigger('click')
      },500)
    }
    return true
  })
  */

  $("#offset_reset_btn").tooltip({placement:'top', delay: {show:1000, hide:100}})
  $("#offset_reset_btn").click(function(e){
    request_get({
      url:'/absoffset/0/0/0',
      success: function (data) {
        $().uxmessage('notice', "Offset cleared.")
        $('#select_btn').trigger('click')
      }
    })
    return true
  })


  $("#motion_btn").tooltip({placement:'top', delay: {show:1000, hide:100}})
  $("#motion_btn").click(function(e){
    if (!$(this).hasClass('disabled')) {
      jobview_jogLayer.visible = false
      $(".tool_extra_btn").hide()
      tools_tmove.activate()
    } else {
      setTimeout(function(){
        $('#select_btn').trigger('click')
      },500)
    }
    return true
  })

  $("#moveBy_btn").tooltip({placement:'top', delay: {show:1000, hide:100}})
  $("#moveBy_btn").click(function(e){
    if (!$(this).hasClass('disabled')) {
        jobview_jogLayer.visible = false
        $(".tool_extra_btn").hide()
        x_mm = Math.round(parseFloat(document.getElementById("x_input").value)*10)/10
        y_mm = Math.round(parseFloat(document.getElementById("y_input").value)*10)/10
        if (isNaN(x_mm)) {
            x_mm = 0;
        }
        if (x_mm > app_config_main.workspace[0] - status_cache.pos[0] - status_cache.offset[0]) {
          x_mm = Math.round((app_config_main.workspace[0] - status_cache.pos[0] - status_cache.offset[0])*10)/10
        } else if (x_mm < - status_cache.pos[0] - status_cache.offset[0]) {
          x_mm = Math.round((- status_cache.pos[0] - status_cache.offset[0])*10)/10
        }
        if (isNaN(y_mm)) {
            y_mm = 0;
        }
        if (y_mm > app_config_main.workspace[1] - status_cache.pos[1] - status_cache.offset[1]) {
          y_mm = Math.round((app_config_main.workspace[1] - status_cache.pos[1] - status_cache.offset[1])*10)/10
        } else if (y_mm < - status_cache.pos[1] - status_cache.offset[1]) {
          y_mm = Math.round((- status_cache.pos[1] - status_cache.offset[1])*10)/10
        }
          
      request_relative_move(x_mm, y_mm, 0, app_config_main.seekrate, "Moving by "+x_mm+","+y_mm)
      status_cache.ready = undefined  // force status update
    } else {
      setTimeout(function(){
        $('#select_btn').trigger('click')
      },500)
    }
    return true
  })

  $("#jog_btn").tooltip({placement:'top', delay: {show:1000, hide:100}})
  $("#jog_btn").click(function(e){
    if (!$(this).hasClass('disabled')) {
      $(".tool_extra_btn").hide()
      $("#jog_hotkey_hint").show()
      tools_tjog.activate()
      jobview_moveLayer.visible = false
      jobview_jogLayer.visible = true
    } else {
      setTimeout(function(){
        $('#select_btn').trigger('click')
      },500)
    }
    return true
  })

  $("#x_input").keyup(function(e){
    // make sure 'value' is a float with at most one decimal point and always smaller than the workspace
	value = document.getElementById("x_input").value;
	x_mm = value.match(/[-+]?[0-9]+[\.|,]?[0-9]?/);
	if (!x_mm) {
	  // if no match is found, null is returned for which 0 should be the replacement
	  x_mm = 0
	}
	// check for only minus sign as input. Otherwise, a single minus-sign without any following digit will quickly be removed, making it annoying to type negative values.
	if (x_mm.toString().localeCompare('-') == 0) {
		document.getElementById("x_input").value = x_mm
	} else {	
		if (isNaN(x_mm)) {
			x_mm = 0;
		}
		if (x_mm > app_config_main.workspace[0] - status_cache.pos[0] - status_cache.offset[0]) {
		  document.getElementById("x_input").value = Math.round((app_config_main.workspace[0] - status_cache.pos[0] - status_cache.offset[0])*10)/10
		} else if (x_mm < - status_cache.pos[0] - status_cache.offset[0]) {
		  document.getElementById("x_input").value = Math.round((- status_cache.pos[0] - status_cache.offset[0])*10)/10
		} else {
		  document.getElementById("x_input").value = x_mm
		}
	}
  })

  $("#y_input").keyup(function(e){
    // make sure 'value' is a float with at most one decimal point and always smaller than the workspace
	value = document.getElementById("y_input").value;
	y_mm = value.match(/[-+]?[0-9]+[\.|,]?[0-9]?/);
	if (!y_mm) {
	  // if no match is found, null is returned for which 0 should be the replacement
	  y_mm = 0
	}
	// check for only minus sign as input. Otherwise, a single minus-sign without any following digit will quickly be removed, making it annoying to type negative values.
	if (y_mm.toString().localeCompare('-') == 0) {
		document.getElementById("y_input").value = y_mm
	} else {	
		if (isNaN(y_mm)) {
			y_mm = 0;
		}
		if (y_mm > app_config_main.workspace[1] - status_cache.pos[1] - status_cache.offset[1]) {
		  document.getElementById("y_input").value = Math.round((app_config_main.workspace[1] - status_cache.pos[1] - status_cache.offset[1])*10)/10
		} else if (y_mm < - status_cache.pos[1] - status_cache.offset[1]) {
		  document.getElementById("y_input").value = Math.round((- status_cache.pos[1] - status_cache.offset[1])*10)/10
		} else {
		  document.getElementById("y_input").value = y_mm
		}
	}
  })
  
  



  // shortcut keys //////////////////////////////////////////////////////////


  Mousetrap.bind(['i'], function(e) {
      $('#info_btn').trigger('click')
      return false;
  })

  Mousetrap.bind(['e'], function(e) {
      $('#export_btn').trigger('click')
      return false;
  })

  Mousetrap.bind(['del', 'backspace'], function(e) {
      $('#clear_btn').trigger('click')
      return false;
  })

  Mousetrap.bind(['q'], function(e) {
      $('#queue_btn').trigger('click')
      return false;
  })

  Mousetrap.bind(['l'], function(e) {
      $('#library_btn').trigger('click')
      return false;
  })

  Mousetrap.bind(['c'], function(e) {
      $('#config_btn').trigger('click')
      return false;
  })

  Mousetrap.bind(['shift+l'], function(e) {
      $('#log_btn').trigger('click')
      return false;
  })

  Mousetrap.bind(['enter'], function(e) {
      $('#open_btn').trigger('click')
      return false;
  })

  Mousetrap.bind(['command+enter', 'ctrl+enter'], function(e) {
      $('#run_btn').trigger('click')
      return false;
  })

  Mousetrap.bind(['command+shift+enter', 'ctrl+shift+enter'], function(e) {
      $('#boundary_btn').trigger('click')
      return false;
  })

  Mousetrap.bind(['space'], function(e) {
      $('#pause_btn').trigger('click')
      return false;
  })

  Mousetrap.bind(['ctrl+esc', 'command+esc'], function(e) {
      $('#stop_btn').trigger('click')
      return false;
  })


  Mousetrap.bind(['0'], function(e) {
      $('#origin_btn').trigger('click')
      return false;
  })

  Mousetrap.bind(['h'], function(e) {
      $('#homing_btn').trigger('click')
      return false;
  })

  Mousetrap.bind(['s'], function(e) {
      $('#select_btn').trigger('click')
      return false;
  })

  Mousetrap.bind(['m'], function(e) {
      $('#motion_btn').trigger('click')
      return false;
  })

  Mousetrap.bind(['o'], function(e) {
      $('#offset_set_btn').trigger('click')
      return false;
  })

  Mousetrap.bind(['j'], function(e) {
      $('#jog_btn').trigger('click')
      return false;
  })

  Mousetrap.bind(['ctrl+up'], function(e) {
      request_jog(0, -1, 0, "jogging up 1mm")
      return false;
  })
  Mousetrap.bind(['up'], function(e) {
      request_jog(0, -10, 0, "jogging up 10mm")
      return false;
  })
  Mousetrap.bind(['shift+up'], function(e) {
      request_jog(0, -50, 0, "jogging up 50mm")
      return false;
  })
  Mousetrap.bind(['ctrl+down'], function(e) {
      request_jog(0, 1, 0, "jogging down 1mm")
      return false;
  })
  Mousetrap.bind(['down'], function(e) {
      request_jog(0, 10, 0, "jogging down 10mm")
      return false;
  })
  Mousetrap.bind(['shift+down'], function(e) {
      request_jog(0, 50, 0, "jogging down 50mm")
      return false;
  })
  Mousetrap.bind(['ctrl+left'], function(e) {
      request_jog(-1, 0, 0, "jogging left 1mm")
      return false;
  })
  Mousetrap.bind(['left'], function(e) {
      request_jog(-10, 0, 0, "jogging left 10mm")
      return false;
  })
  Mousetrap.bind(['shift+left'], function(e) {
      request_jog(-50, 0, 0, "jogging left 50mm")
      return false;
  })
  Mousetrap.bind(['ctrl+right'], function(e) {
      request_jog(1, 0, 0, "jogging right 1mm")
      return false;
  })
  Mousetrap.bind(['right'], function(e) {
      request_jog(10, 0, 0, "jogging right 10mm")
      return false;
  })
  Mousetrap.bind(['shift+right'], function(e) {
      request_jog(50, 0, 0, "jogging right 50mm")
      return false;
  })

}
