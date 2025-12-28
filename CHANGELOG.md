# DriveboardApp Changelog

## Unreleased

### New Features

- 

### Bug Fixes
- 

### Development
- Add CHANGELOG.md
- Rename master branch to main, drop the develop branch

## v25.12 (December 2025)

### New Features

#### SVG Import
- Automatically convert SVG text elements to paths for proper laser cutting/engraving
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
  - NearestNeighbor - Optimized pathing for complex shapes
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
- Pulse Mode: Implement lsser pulsing with frontend button and firmware support (safety interlocks still work)
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
