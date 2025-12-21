**Notes on this fork:**

The original codebase at [nortd/driveboardapp](https://github.com/nortd/driveboardapp) is no longer maintained. Here are some of the highlights in this fork, see [this thread](https://groups.google.com/g/lasersaur/c/n54LNN86H-c) or [this blog post](https://theshamblog.com/software-mods-to-the-lasersaur/) for more details:
* Upgrade from python2 to python3
* Rastering completely rewritten: skips over large whitespace for speed, option for inverting, can select number of gray levels (set to 2-3 for dithering, max of 128)
* Bidirectional and nearest neighbor pathing algorithms for fill and rastering speedups
* Added a 'pulse' functionality for the laser (all safety interlocks still work)
* You can save material preset laser settings through the UI and easily load them back up
* The configuration is editable through the UI
* The debug terminal can now be resized, scroll, and will not auto-jump back down when scrolling around
* Option to print out human-readable versions of the tx/rx serial commands
* Option for the head to auto-home when the interface is first started
* You can now jog in 1 mm increments by holding the ctrl key with the arrows
* Your current X-Y coordinates are shown on screen
* “Set Offset” now sets the offset to the current head position
* The head position is now set in relative dx, dy coordinates
* Fills of the same color are now nested to create holes in fill areas
* Many small bugfixes
* Tested on Windows and Linux

Outstanding issues:
* Building a Windows executable isn’t currently working
* Documentation needs to be updated
* Not tested on Mac

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


Installation
------------
- [installation](docs/install.md)

Hardware
--------
- [Driveboard](https://github.com/nortd/lasersaur/wiki/driveboard) via [Lasersaur project](http://www.lasersaur.com)
- [MinimalDriveboard](docs/minimaldriveboard.md)


**DISCLAIMER:** Please be aware that operating CNC machines can be dangerous and requires full awareness of the risks involved. NORTD Labs does not warrant for any code or documentation and does not assume any risks whatsoever with regard to using this software.
