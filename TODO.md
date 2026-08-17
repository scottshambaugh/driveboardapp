bugs
----
- stall between certain x-axis jogs
  - sending a pierce command in between clears it
- ready means "firmware reported idle", but it is also read as "no job on the
  machine". A fresh connection has neither, so a job or a home in the first
  moments after connect is refused until the first status frame lands. Homing
  now goes by the job guard instead, the job admission check has not moved


beauty bugs
-----------
- dba files: optimize should maybe split up into simplified and sorted


testing
-------
- frontend tests cover status.js and jobhandler.js. controls.js, passes.js,
  tools.js and jobview.js are still only covered by "the file parses"


optimizations
-------------
- consider changing laser drive board for more frequency control
- /run queues the whole job in the http request thread, so the request is held
  open for as long as dispatch takes. A worker would also give the ui a real
  queued state to show instead of inferring one


features
--------
- gcode editor
- clip to the viewport. Content off the page is drawn and cut, and since it is
  also outside the work area it gets the whole job refused rather than ignored
  the way every renderer ignores it


known limitations, do not retry
-------------------------------
- resuming a job across a usb disconnect. The board DTR-resets on replug, so
  the machine loses its position and its program. This was built and tested on
  hardware and removed. Pause and resume in the ui, and plain auto-reconnect,
  are what remain
