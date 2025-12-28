
DriveboardApp Installation
==========================

Requirements
------------
- Python 3.8 or higher
- [uv](https://docs.astral.sh/uv/) (recommended) or pip

Installation with uv (Recommended)
----------------------------------

1. Install uv if you haven't already:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

2. Clone the repository:
```bash
git clone https://github.com/scottshambaugh/driveboardapp.git
cd driveboardapp
```

3. Install dependencies and run:
```bash
uv sync
uv run python backend/app.py
```

Installation with pip
---------------------

```bash
git clone https://github.com/scottshambaugh/driveboardapp.git
cd driveboardapp
pip install .
python backend/app.py
```

If necessary, create a [configuration](configure.md) file for the app.

Development Setup
-----------------

To set up pre-commit hooks for linting and formatting:

```bash
uv tool install pre-commit
pre-commit install
```

This will run [Ruff](https://docs.astral.sh/ruff/) (Python linting/formatting) and [Prettier](https://prettier.io/) (JavaScript formatting) automatically on each commit.

To run the hooks manually on all files:
```bash
pre-commit run --all-files
```

### Troubleshooting
If any issues occur it helps to install the [Arduino IDE installation](https://www.arduino.cc/en/Guide/HomePage) and get the blink LED example to run. This makes sure the basics work. For example on Linux the Arduino IDE will ask you to give access permission to the serial port.


Lasersaur Driveboard v14.04 Setup (Old)
---------------------------------------
- make sure the Driveboard/Lasersaur can access the Internet
- ssh into the Driveboard/Lasersaur with `ssh lasersaur.local` and do the following:
```
git clone https://github.com/scottshambaugh/driveboardapp.git
cd driveboardapp
scripts/install_packages.sh
scripts/upgrade_to_driveboardapp.sh
pkill python
python backend/flash.py
reboot
```
If for some reason you want to downgrade and use LasaurApp again run:
```
scripts/downgrade_to_lasaurapp.sh
reboot
```


MinimalDriveboard Setup
------------------------

DriveboardApp is quite flexible software and can be run on any Windows, OSX, or Linux computer. A [MinimalDriveboard](minimaldriveboard.md) can be connected via USB directly. In this case the DriveboardApp *backend* runs on the computer and the browser connects locally.

- Open the command line.
- Make sure you have Python 3.8+, run `python --version`
  - If not, get installers from the [Python Website](http://python.org/download/).
- Download the latest [stable DriveboardApp](https://github.com/scottshambaugh/driveboardapp/archive/main.zip) and unzip to a convenient location.
  - For advanced users we recommend using `git clone https://github.com/scottshambaugh/driveboardapp.git` instead. This way you can easily update with `git pull`
- Change directory to that location and run:
```bash
uv sync
uv run python backend/app.py
```

At this point your default browser should open at [http://localhost:4444](http://localhost:4444). DriveboardApp runs in any current Firefox or Chrome (Safari and IE may work too). Congrats!

If you get a serial port error you may have to configure it by [setting up a configuration](configure.md) file and point to the port where the Hardware is connected to. On Linux you may also have to set proper r/w permissions for the serial port.
