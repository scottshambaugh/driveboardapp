
- when serial port auto changes, fails to do so in flash module


bugs
----
- stall between certain x-axis jogs
  - send a pierce command in between
- unplugging usb is not always handled gracfully
- (found by tests) air_off/air_on/aux_* raise AttributeError when no controller
  is connected (SerialLoop is None); web.start() calls air_off() on launch, so
  starting the server with no hardware crashes. Should be a safe no-op while
  disconnected. Tracked by xfail test_aux_commands_safe_when_disconnected.
- (found by tests) job_mill issues moves with no target_in_workarea check, unlike
  job_laser which validates. An off-bed mill job is sent to hardware unchecked.

beauty bugs
-----------
- dba files: optimize should maybe split up into simplified and sorted

optimizations
-------------
- consider changing laser drive board for more frequency control
- scale rastering for speed


features
--------
- lasertags
- pixel size assignment for image rasters
- gcode editor
- importers
  - load dxf
  - load gcode
