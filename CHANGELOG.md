# DriveboardApp Changelog

## Unreleased
### New Features
- Jog moves are now clamped to the work area so the head cannot hit a limit switch. Toggled with the new `jog_soft_limits` config setting
- Speed up loading svg files with raster images (esp ducplicates)
- Seek-optimized ordering (NearestNeighbor rasters and vector path sorting) now runs a 2-opt untangling pass to further improve job time
- Bidirectional rasters now start from whichever of the four image corners is closest
- Seek ordering now minimizes machine time under the firmware's trapezoidal speed profiles and junction speeds
- Raster ordering also accounts for the seek onwards to the next item or pass, and for the job's final return to origin
- Vector paths get a second seek optimization at job time, re-sequencing their polylines from the head's actual position towards what follows (fills keep their fill_mode order)
- Closed contours are now entered at whichever point along the loop is fastest to reach
- Closed contours can be burned as two arcs when that saves travel, eg the near sides of a row of shapes on the way out and the far sides on the way back. Arcs kept adjacent still burn as one continuous move. Toggled with the new `split_closed_paths` config setting
- A split contour resuming on an already-cut point skips its pierce dwell, toggled with the new `skip_pierce_on_resume` config setting
- The job view's seek lines now show the true dispatch-time ordering instead of the stored file order
- Loading a file now shows the job as soon as it is parsed, the seek optimization finishes in the background and the view refreshes when it lands

### Bug Fixes
- Fixed loading a large file tripping the controller's serial watchdog, which turned the status red and left homing as the only way out
- Job import now runs in a worker process, so parsing a big file cannot starve the serial link
- The firmware serial watchdog now only stops the machine when it has motion to halt, (kills the beam regardless)
- Fixed importing a dba job with 3D vertices crashing during path optimization
- The UI's seek lines now show the true post-optimizer path

### Development
- 

## v26.08 (August 2026)

### New Features
- `pierce_time` now works, to dwell at the beginning of cut paths to ensure the material is fully pierced before moving. Per-pass, selectable in the UI and savable in presets
- `aux_assist` now works, to actuate an aux device similarly to air
- New "job" option for air and aux assist modes, to run through the whole job. "feed" no longer switches off and on between contiguous burns.
- Copies of the same raster image now share a single pass entry (like same-color lines), so they get the same burn settings assigned together

### Bug Fixes
- Fix errors in loading a scaled / transformed raster image
- Limit image extent validation to the bbox that will actually be engraved (skipping eg overhanging whitespace)
- Fix transparency in jpegs going black
- Fix buffer overflow for rasters
- Additional validation for move commands, and force zero laser intensity
- Fix raster images dropping their last pixel (correctness, and also cause a freeze on one-pixel rasters)
- Fix raster segments shorter than the acceleration ramp not engraving at all
- Scale raster intensity with head speed, keeping exposure even through the acceleration ramps

## v26.06 (June 2026)

### New Features
- Added a NearestNeighbor raster mode that reorders engraved segments to minimize seek travel, speeding up sparse/large-whitespace images.
- Engrave only the visible part of a cropped (rectangular clip-path) image from an SVG, including clips on a wrapping group.
- Pausing now freezes the machine in place (beam off) and keeps its job, so it resumes exactly where it left off.
- Auto-reconnect to the controller after a serial disconnect.

### Bug Fixes
- Fixed raster engraving freezing mid-job. A firmware data race on the serial
  chunk-acknowledgement counters (shared between the main loop and the stepper
  interrupt) dropped CMD_CHUNK_PROCESSED acks under sustained rastering, causing
  the host's buffer tally to desync and deadlock. The accounting is now atomic,
  with a host-side resync as a safety net.
- Fixed the low-memory safety stop never triggering.
- Fixed job import failing on segments longer than the max segment length.
- Fixed incorrect job bounding boxes from the first point of each shape.
- Fixed exported jobs missing their stats.
- Fixed the job name not resetting when clearing a job.
- Improved raster streaming performance by reducing serial lock contention.
- Fixed move commands firing the laser with leftover intensity from a prior job.
- Added a firmware serial watchdog that stops the laser if the host connection is lost.
- Sped up path optimization for large vector jobs.
- Sped up reverse and bidirectional ordering of large fills, which was quadratic in the number of segments.
- Cut memory use of large jobs by storing the outgoing serial buffer as bytes.
- Cached image thumbnails so they are not re-rendered on every pass change.
- Fixed serial disconnect errors always logging as "unknown".
- Hardened firmware flashing against shell injection via the serial port setting.
- Fixed DXF imports not being path-optimized.
- Fixed placement of DXF files with negative coordinates.
- Fixed import crash for unit-less SVGs from some editors.
- Fixed import crash for SVGs with physical dimension units (mm, cm, pt, pc, in).
- Fixed distorted corners on rounded rectangles with a large ry.
- Fixed running or exporting a job failing when it contained an empty field.
- Fixed the python client's run_file ignoring the local flag and any non-default host.
- Fixed a serial write error being swallowed mid-job instead of stopping the laser; the job no longer skips past the un-sent commands.
- Fixed a possible firmware buffer overflow when a job was paused for an open door or chiller fault.
- Fixed out-of-range coordinates and feedrates wrapping to wildly wrong values instead of being clamped; out-of-bounds move requests are now rejected.
- Fixed a corrupted serial byte still being acted on after failing the transmission-error check (could undo the resulting stop).
- Fixed reordering passes scrambling color assignments on jobs with ten or more colors.
- Fixed raster engraving firing slightly low at high power, caused by a 16-bit overflow in the firmware intensity calculation.
- Fixed DXF import bounds and placement using a fixed bed size instead of the configured workspace.
- Fixed import crash for circles/ellipses missing a radius.
- Fixed import crash for SVGs with non-numeric width/height.
- Fixed raster dithering artifacts when error diffusion pushed pixels out of range.
- Sped up text-to-path conversion for SVGs with many text elements.
- Fixed a serial port handle leak on disconnect/reconnect.
- Hardened firmware against rare data races in position reporting and underrun counting.
- Reduced UI overhead from the live head-position marker.
- Fixed firmware flashing using a stale/empty serial port instead of the connected one.
- Raster and fill mode settings are now dropdowns in the config UI instead of free text.
- Quieted noisy systemctl errors when the app can't disable system sleep (e.g. on Crostini).
- Starting the server with no controller connected no longer crashes: the air/aux commands are now safe no-ops while disconnected.
- Mill jobs are now validated against the work area (like laser jobs); an off-bed G0/G1 move is rejected instead of being sent to the machine.

### Development
- Add CHANGELOG.md
- Rename master branch to main, drop the develop branch
- Bump firmware VERSION to 2606
- Refactored job_laser raster handling into smaller functions (image, path, pixel load, segment finder)
- Hardened the firmware build script with argv lists instead of shell=True (no injection, handles paths with spaces)
- Consolidated the AVR toolchain constants (device, programmer, bitrate, clock) into config, shared by build and flash
- Added an automated test suite (Python, firmware, and frontend) that runs in
  GitHub Actions

## v25.12 (December 2025)

### New Features

#### SVG Import
- Automatically convert SVG text elements to paths. Text color is used as the path color
- Import SVG fills as paths

#### Fill Engraving
- Added support for nested fill patterns within shapes
- Optimized fill generation algorithm for significantly better performance

#### Configuration & Validation
- Configuration settings can now be edited directly in the UI
- Added option to require units in SVG files for precision
- Validate jobs before running to catch errors early
- More helpful error messages throughout the application, and show error message for longer
- Make pulse intensity and duration user-configurable

### Bug Fixes

- Fixed various console warnings in the frontend
- Fixed fills over poorly defined same-point paths
- Fixed escape character handling in processing
- Show actual nearest-neighbor fill paths in the UI
- Continue gracefully when unable to turn off system hibernation
- Properly handle flipped/mirrored raster images during import

### Development

- Migrated to pyproject.toml for Python dependency management
- Prefer uv over pip for environment setup
- Added pre-commit hooks for linting
- Confirmed and documented Linux compatibility

---

## v21.01 (January 2021)

### New Features

#### Raster
- Dithering implemented for raster images, with Floyd-Steinberg algorithm and configurable number of power levels
- Added support for less common image modes
- Prevent computer from sleeping while running a job to prevent long jobs from pausing (tested on Windows, not tested on Linux, not implemented for macOS)

#### Other
- Split dwelling off into its own command for better control
- Added lasersaur-raster and raster-test example files to the library for testing raster engraving
- If feedrate, intensity, or pixel size data is missing for a pass, read in defaults from the config

### Bug Fixes

- Fixed raster not working due to incorrect integer division
- Fixed dwell timing being off by a millisecond
- Fixed issue with deleting presets
- Fixed presets pixel size units handling

---

## v20.12 (December 2020)

### New Features

#### Raster

- New raster modes:
  - Bidirectional - Halves engraving time by engraving in both forward and reverse directions (also for fills)
  - Reverse - Engrave in reverse direction
- Allow inverting raster images (useful for etching white on black, e.g., on slate)
- Speed up by skipping completely white lines and large whitespace areas in raster images

#### UI
- Presets system: Save and recall named settings for common cutting/engraving configurations
- Show estimated job duration in UI
- Passes can be reordered
- Show RGB values when selecting pass color
- Display current head coordinates in top right of job view
- Arrow buttons to jog for 1mm movements (also responds to Ctrl+ArrowKey)
- Can move head to specified coordinates
- Set offset at current position with improved offset controls
- Enhanced the debug window with scrolling, resizing, and copy-text capability
- Serial data prettified into human-readable serial format

#### Other
- New fill mode: NearestNeighbor, to more quickly fill complex shapes
- Pulsing: Implement laser pulsing with frontend button and firmware support (safety interlocks still work)
- User option to run homing cycle on startup (disabled by default for safety)
- Use transformation matrices to align jobs when opening files

### Bug Fixes

- Fixed arc bug in SVG path reader (thanks to Christian Walther and Martin Renold)
- Fix/workaround for hangs on SVG import (thanks to Martin Renold)
- Fixed TypeError on SVG load in Python 3
- Fixed crash when sorting lasertags
- Prevent raster images from getting cut off short
- Fixed race condition that could lock up firmware after instant-stop (thanks to Martin Renold)
- Server-side verification of movement values; only allow movement within limit switches
- Turn off air assist after homing to prevent air stuck on after power cycle


### Development

- Python 3 Migration, drop Python 2 support
- Generated pip requirements file for dependencies
- Removed local copies of dxfgrabber and PyInstaller (use pip)

---

## v18.05 (May 2018)

Base release for comparison, from https://github.com/nortd/driveboardapp/releases/tag/v18.05. Earlier changelogs not included in this document.

---

**Contributors:** Scott Shambaugh, Johann150, makermusings, freilab, vanillasoap, Martin Renold (martinxyz), Christian Walther
