# mass80d runbook (autostart)

Source: docs/plans/2026-10-09-autostart.md. Nothing here has been installed yet.

## What it is

`mass80d` is the daemon for the Mass80 keyboard screen. It runs as a systemd USER service
(`systemd/mass80d.service`) with `configs/daily.toml`, started with the graphical session,
log level WARNING. The daemon waits for the device itself: if the screen is absent or
unplugged it keeps retrying (verified with a fake absent port and a real unplug, Stage 9m).

With `daily.toml` it WRITES to the device, changes system volume (slider 100 = system 100%, no cap) with
`wpctl`, controls players with `playerctl`, fetches cover art from i.scdn.co (https) and
reads art files from `~/.zen/firefox-mpris`. The nine device keys inject keystrokes with `wtype` (Stage 4c, enabled 2026-10-14: Print, Scroll_Lock, Pause, Insert, Home, Prior, Delete, End, Next).

## Install (not yet done; requires operator approval)

These steps were written but not run.

    mkdir -p ~/.config/systemd/user
    systemctl --user enable --now <repo>/systemd/mass80d.service

`enable` links the unit into `~/.config/systemd/user/` and `--now` starts it.

## Status and logs

    systemctl --user status mass80d
    journalctl --user -u mass80d -f
    journalctl --user -u mass80d --since "1 hour ago"

## Stop and kill switch

    systemctl --user stop mass80d

Stop it before a stage run or `tools/capture.py`: the serial port can have only one owner.
The port guard makes the second process exit with code 2 (port held by another process).
Start it again with `systemctl --user start mass80d`.

Exit codes: 0 clean stop, 2 port held (the unit retries every 10 s, at most 10 times in
300 s), 3 bad arguments or config (not restarted).

## Temporary full trace

    systemctl --user edit mass80d

In the editor that opens, enter:

    [Service]
    ExecStart=
    ExecStart=<repo>/.venv/bin/python -B -m mass80d --config configs/daily.toml --log-level INFO

Save, then `systemctl --user restart mass80d`. WARNING: at INFO the journal records song
titles and artist names, and one line per frame sent. To go back, run
`systemctl --user revert mass80d` and restart.

## Dry run (no device writes, no commands)

With the service stopped, from the repo root (`<repo>`):

    PYTHONPATH=src .venv/bin/python -B -m mass80d --config configs/daily.toml --no-write --no-actions
    PYTHONPATH=src .venv/bin/python -B -m mass80d --config configs/daily.toml --no-write
    PYTHONPATH=src .venv/bin/python -B -m mass80d --config configs/daily.toml --no-actions

The first sends nothing and runs nothing. `--no-write` alone still runs commands;
`--no-actions` alone still writes frames to the device. `--print-config` shows the
effective configuration without running anything.

## Uninstall

    systemctl --user disable --now mass80d

`disable` removes the enable symlink; also remove the unit link if it is still there:

    rm ~/.config/systemd/user/mass80d.service
    systemctl --user daemon-reload

## Known limits

- With the device absent the daemon logs a WARNING about every 10 s (accepted for now;
  roughly 8,600 lines a day, inferred from the backoff, not measured).
- The clock bar behaviour is as observed in the stage runs; nothing more is claimed.
- The performance page needs about 3 s for its first frame (the first poll only takes the baseline).
- The previous/next cover slots are never asked for by the screen, so they are not served.
- Login ordering is untested until a real login or reboot: `graphical-session.target` versus
  when `WAYLAND_DISPLAY` reaches the user manager under uwsm. Actions needing `wtype` could
  fail in the first seconds; check the journal after a login.
- The unit points into the repo (`<repo>`) and its `.venv`; moving or rebuilding either breaks it.
