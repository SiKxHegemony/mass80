# mass80

`mass80d` is a Linux daemon for the built-in screen of the Mass80 keyboard (an ESP32-S3
module, USB `303a:4003`). It answers the screen's polls with host data (clock, volume,
now-playing title and progress, album art, CPU and network stats) and turns the screen's
touch, slider and key events into host actions (volume, play/pause/next/previous, keystrokes).

It was built for Omarchy (Arch Linux, Hyprland, PipeWire/WirePlumber). It is tested on that
setup only. Other distributions may work, but nothing here has been tried on them.

| Screen | Host data | Checked on the device |
|---|---|---|
| Clock | `get_time` | yes |
| Volume slider | `volume` events, `volume` query | yes |
| Music (title, artist, progress, cover) | `get_music_info`, `get_cover` | yes |
| Performance (CPU %, temperature, MHz, memory, network) | `get_performance` | yes. The clock bar does not follow MHz; see `docs/protocol-host-messages.md` |
| Previous / next track swipes | `music_prev`, `music_next` | yes |
| Touch keys (Print ... Page Down) | `send_key` -> keystroke | enabled in `configs/daily.toml`, but takes effect only after a service restart. Not yet checked on the device |
| LED touch slider and TrackPoint | not connected | no data reaches the serial port from them yet |

## Requirements

Python 3.14. The code uses the Python 3.14 `except A, B:` syntax, so older Pythons fail.

Packages, all in the official Arch repos (nothing from the AUR is needed):

```
sudo pacman -S --needed python playerctl wtype imagemagick wireplumber
```

| Package | Provides | Used for |
|---|---|---|
| `python` 3.14 | the interpreter | everything |
| `wireplumber` | `wpctl` | reading and setting system volume |
| `playerctl` | `playerctl` | Spotify and browser media (MPRIS): titles, play/pause, skip |
| `imagemagick` | `magick` | converting album art to the 480x480 JPEG the screen draws |
| `wtype` | `wtype` | injecting the nine device keys as keystrokes |

Optional: `hyprctl` (Hyprland, already installed on Omarchy), only if you add a
`type = "hyprctl"` key binding. The shipped config does not use it.

Project venv package: `pyserial` (3.5; the same as `python-pyserial` in the repos).
Developer tools, optional: `ruff`, `mypy`.

No udev rule is needed. The device appears under `/dev/serial/by-id/` through the standard
systemd rules, and that is the path the daemon uses by default.

## Install

```
git clone <your-fork-or-this-repo> ~/Repos/mass80
cd ~/Repos/mass80
python3.14 -m venv .venv
.venv/bin/pip install pyserial
```

The unit file assumes the repo is at `~/Repos/mass80`, and `configs/daily.toml` reads art from `~/.zen/firefox-mpris`. If you
clone elsewhere, change the paths before you install the service (see Autostart).

## Configure

The daemon reads only the config file you pass with `--config`. It does not search
`~/.config`.

- `config.example.toml` lists every key with its built-in default and a comment. Copy it
  and change what you need. A test checks that the example equals the defaults.
- `configs/daily.toml` is the configuration the service runs. It turns on the verified
  replies, sets the volume cap, the cover size (480x480) and the nine device keys. Read its
  header before you use it: it writes to the device, runs `wpctl` and `playerctl`, requests
  album art from `i.scdn.co`, and reads one local art directory.

Settings you are most likely to change, in `daily.toml` or your own copy:

| Setting | What it does |
|---|---|
| `[volume] max_fraction` | The slider's 0..100 is mapped onto system volume 0..this. `1.0` = slider 100 is system 100 %. Use a smaller value if you want a lower ceiling. |
| `[music] players` | Which media players the screen follows, in priority order. Names match by prefix (e.g. `spotify`, `firefox`). |
| `[cover] local_dirs` | Directories the daemon may read album art from (for Firefox/MPRIS art). Use `[]` if you only want Spotify art from the web. |
| `[cover] hosts` | The only hosts album art may be fetched from (default `i.scdn.co`, https only). |
| `[replies]` | Turn each screen reply on or off. |
| `[keys.<name>] type` | `"log"` (record only) or `"key"` (inject the default keysym). Nine keys: Print, Scroll, Pause, Insert, Home, Page Up, Delete, End, Page Down. |
| `[performance] interfaces` | Network interfaces to count. Empty = the default-route interface. |

Print the effective configuration without running anything:

```
PYTHONPATH=src .venv/bin/python -m mass80d --config configs/daily.toml --print-config
```

## Run it

Start with a dry run. It opens no port, writes nothing and runs no command:

```
PYTHONPATH=src .venv/bin/python -m mass80d --config configs/daily.toml --replay captures/<some-log>.log
```

Then the device, read only (no frames sent, no commands run):

```
PYTHONPATH=src .venv/bin/python -m mass80d --config configs/daily.toml --no-write --no-actions
```

Then for real:

```
PYTHONPATH=src .venv/bin/python -m mass80d --config configs/daily.toml
```

The startup banner states what is on. `WRITES to device: ON` and `system ACTIONS: ON` mean
frames are sent and commands run. Stop with Ctrl-C.

Flags: `--port PATH` (default: the by-id path), `--no-write`, `--no-actions`,
`--replay FILE`, `--log-level DEBUG|INFO|WARNING|ERROR`, `--print-config`.
`--tiocexcl` and `--no-port-guard` are for testing and are off by default.

Exit codes: `0` clean stop, `2` port held by another process, `3` bad arguments or config.

Only one process can use the serial port. If something else holds it (another `mass80d`, a
capture, a stage run), the daemon exits with code 2. Stop the service before you start a
second copy: `systemctl --user stop mass80d`.

If the screen is unplugged, the daemon keeps retrying (back-off up to 10 s) and recovers when
the screen returns. It logs one WARNING per failed attempt while the screen is absent.

## Autostart (systemd user service)

The service runs `configs/daily.toml` with `--log-level WARNING`, restarts on failure (not on
exit code 3, a bad config), and starts with your graphical session.

1. Check the paths in `systemd/mass80d.service`. `WorkingDirectory` and `ExecStart` point at
   the repo location. If your repo is somewhere else, edit both lines.
2. Install and start it:

   ```
   mkdir -p ~/.config/systemd/user
   systemctl --user enable --now ~/Repos/mass80/systemd/mass80d.service
   ```

   `enable` links the unit into `~/.config/systemd/user/`. `--now` starts it.
3. Check it:

   ```
   systemctl --user status mass80d
   journalctl --user -u mass80d -f
   ```

It starts at login, not at boot. The service is tied to `graphical-session.target`, so it
starts after you log in to the desktop. The actions (volume, media keys, keystrokes) need your
session's audio and Wayland, so the daemon cannot run usefully before login. Running it at
boot without a login (`loginctl enable-linger`) has not been tried and is not supported here.

Stop it: `systemctl --user stop mass80d`. Stop it from starting at login:
`systemctl --user disable --now mass80d`, then remove the link with
`rm ~/.config/systemd/user/mass80d.service` and `systemctl --user daemon-reload`.

The full runbook, including a temporary trace at `--log-level INFO` (it records song and
artist names) and the known limits, is in `docs/runbook.md`.

## Tests and checks

```
.venv/bin/python -B -m unittest discover -s tests -t .
.venv/bin/ruff check src tests tools --no-cache
.venv/bin/ruff format --check --no-cache src tests tools
.venv/bin/mypy src --no-incremental
```

These check the code, not the device. The device behaviour was checked by hand in the
stage runs. The stage reports are kept in the project's Obsidian vault
(`Repos/mass80/reports/`), not in this repo.

## Layout

- `src/mass80d/` the daemon (`daemon.py` entry point, `replies.py`, `dispatcher.py`,
  `actions.py`, `config.py`, `cover_*.py`, `perf_reader.py`, `music_reader.py`, and others)
- `configs/` stage and daily configurations (`daily.toml` is the one to run)
- `systemd/mass80d.service` the user service
- `docs/` protocol notes (`protocol.md`, `protocol-host-messages.md`, decodes) and `runbook.md`
- `tools/` read-only capture and analysis helpers; `capture.py` is the passive serial logger
- `captures/` raw logs (gitignored; review before you share them, since they can contain song titles)
- `vendor/` vendor material (gitignored; not needed to run the daemon)
- `tests/` unit tests

## Safety notes

- The daemon never runs a shell. Commands are fixed argument lists.
- Keystrokes are limited to nine whitelisted keysyms. A key binding with any other keysym is
  refused when the config loads.
- `tools/send_get_time_once.py` is a one-off hardware test that talks to the port directly. It
  bypasses the reply whitelist by design. Do not use it for normal running.
- Album art is fetched only over https from the configured hosts, with a 3 s limit and a size
  cap. Local art is read only from the configured directories.
