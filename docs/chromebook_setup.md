# DriveboardApp on a Chromebook

A Chromebook can be used as a cheap dedicated computer to interface with the lasersaur. This details the setup of installing the driveboardapp from source, using ChromeOS's Linux VM.

## 1. Set up the Chromebook

Power on your chromebook, go through the setup guide, and log in to your Google account (guest mode can't run Linux).

## 2. Enable Linux

Go to Settings, then Advanced, then Developers, then Linux development environment, and click Turn on.

Pick a username and accept the defaults. It requires downloading a few hundred MB and takes a few minutes.

## 3. Open the Terminal

Open the Launcher (circle icon, bottom-left), search for Terminal, open it, and click penguin.

Run the remaining commands there.

## 4. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
```

## 5. Clone the repo and run

```bash
git clone https://github.com/scottshambaugh/driveboardapp.git
cd driveboardapp
uv sync
uv run python backend/app.py
```

Your browser should open to the Chrome UI automatically.

## 6. Pass the Driveboard through to Linux

Plug the board in, then go to Settings, then Developers, then Linux, then USB preferences, and toggle the device on. The UI should detect and connect to the Lasersaur at this point. If not, restart the app in the terminal.

If the app reports a serial port error, point it at the right device via a [config file](https://github.com/scottshambaugh/driveboardapp/blob/main/docs/configure.md).
