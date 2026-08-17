**Notes on this fork:**

The original codebase at [nortd/driveboardapp](https://github.com/nortd/driveboardapp) is no longer maintained. Here are some of the highlights in this fork, see [this thread](https://groups.google.com/g/lasersaur/c/n54LNN86H-c), [this blog post](https://theshamblog.com/software-mods-to-the-lasersaur/), or the [changelog](./CHANGELOG.md) for more info:
* Upgrade from Python 2 to Python 3
* Rastering completely rewritten: skips over large whitespace for speed, option for inverting, can select number of gray levels (set to 2-3 for dithering, max of 128)
* Rastering supports svg-embedded PNG, JPEG, GIF, BMP, WebP, ICO, TIFF, PPM, PCX, TGA, JPEG 2000, AVIF
* Added a 'pulse' functionality for the laser (all safety interlocks still work)
* Faster pathing algorithms, and new faster Bidirectional and NearestNeighbor fill modes for rasters and fills
* SVG text and area fills are imported as paths
* Fills of the same color are nested to make holes in fill areas, to support text
* The UI has new modals for saving and loading material preset laser settings, editing the configuration, jogging in smaller 1mm increments, and many tweaks for showing the head coordinates and setting offsets
* Option for the head to auto-home when the machine is booted
* Many small bugfixes and safety improvements
* Added an automated test suite (Python behavior, firmware compilation + behavior in a simulator, and a frontend smoke check) that runs in GitHub Actions
* Tested on Windows and Linux

Outstanding issues:
* Building a Windows executable isn’t currently working
* Documentation needs to be updated

Many thanks to scottshambaugh, FREILab, Johann150, makermusings, vanillasoap, and martinxyz for keeping this project alive. Below is the original documentation.

-------------

DriveboardApp
=============

DriveboardApp is the official app to control Driveboard-based CNC machines like the [Lasersaur](http://lasersaur.com). Its aim is to provide a simple yet powerful way to control the machine. Its primary features are:

- load svg vector files and send them to the machine
- display the status of the machine
- pausing/continuing/stopping a job
- firmware flashing

This software is written in Javascript (frontend), Python (backend) and C (firmware). The backend can either run directly on the Driveboard or on the client computer. The frontend runs in a web browser either on the same client computer or on a tablet computer.

- frontend
- [backend](docs/backend.md)
  - [Low-Level API](docs/api_low.md)
  - [High-Level API](docs/api_high.md)
- firmware
  - [serial protocol](docs/protocol.md)
- jobs
  - [dba file format](docs/dba.md)
- [configuration](docs/configure.md)
- [testing](docs/testing.md)


Installation
------------
- [installation](docs/install.md)

Hardware
--------
- [Driveboard](https://github.com/nortd/lasersaur/wiki/driveboard) via [Lasersaur project](http://www.lasersaur.com)
- [MinimalDriveboard](docs/minimaldriveboard.md)


**DISCLAIMER:** Please be aware that operating CNC machines can be dangerous and requires full awareness of the risks involved. NORTD Labs does not warrant for any code or documentation and does not assume any risks whatsoever with regard to using this software.
