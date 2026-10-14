#!/usr/bin/env python3
"""Passive, read-only logger for the Mass80 screen module serial stream.

Opens the port read-only and NEVER writes to the device. Every received chunk
is appended to a timestamped log as one JSON record (raw hex + decoded text),
so nothing is lost for later analysis. The terminal is deliberately QUIET:
the device's constant polling (get_time, get_music_info) and its [CDC-TX]/debug
chatter are logged but not printed, so only real events appear and your typed
labels stay readable.

If the device disconnects or crashes, the logger records it, waits for the
port to come back, reopens it, and keeps writing to the SAME log file.

Usage:
    .venv/bin/python tools/capture.py [--port PATH] [--baud N] [--label TEXT]
                                      [--show-polling]

Type a label and press Enter to mark the log (e.g. "slider up"). Labels are
written to the log only, never to the device. Stop with Ctrl+C.
"""

import argparse
import contextlib
import json
import os
import re
import select
import sys
import time
from datetime import datetime

import serial

DEFAULT_PORT = "/dev/serial/by-id/usb-Espressif_Systems_Espressif_Device_123456-if00"
CAPTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "captures")
POLLING = {"get_time", "get_music_info"}
# A real host-bound JSON line starts at column 0. Debug echoes are prefixed by
# "[CDC-TX]" and use "(" instead of "{", so they never match.
EVENT_RE = re.compile(r'^\{"action":"([a-z_]+)"[^{}]*\}\s*$')


class LineSplitter:
    """Reassemble a chunked byte stream into text lines."""

    def __init__(self) -> None:
        self.buf = ""

    def feed(self, text: str) -> list:
        self.buf += text.replace("\r", "")
        *lines, self.buf = self.buf.split("\n")
        return lines


def displayable(line: str, show_polling: bool = False):
    """Return the line if it is a real event worth showing, else None."""
    m = EVENT_RE.match(line)
    if not m:
        return None
    if m.group(1) in POLLING and not show_polling:
        return None
    return line


def open_port(path: str, baud: int):
    return serial.Serial(path, baud, timeout=0.1)  # this script never calls write()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--label", default="", help="initial label written to the log")
    ap.add_argument(
        "--show-polling", action="store_true", help="also print get_time/get_music_info"
    )
    args = ap.parse_args()

    os.makedirs(CAPTURE_DIR, exist_ok=True)
    path = os.path.join(CAPTURE_DIR, datetime.now().strftime("capture-%Y%m%d-%H%M%S.log"))
    splitter = LineSplitter()
    chunks = 0
    t_start = time.time()

    with open(path, "a", encoding="utf-8") as log:

        def emit(kind: str, **fields) -> None:
            log.write(json.dumps({"t": time.time(), "kind": kind, **fields}) + "\n")
            log.flush()

        def say(msg: str) -> None:
            print(f"[{time.time() - t_start:6.1f}s] {msg}", flush=True)

        print(f"Logging to {os.path.normpath(path)}")
        print(
            "Type a label + Enter to mark the log. Ctrl+C to stop. Polling/debug noise is hidden."
        )
        emit("start", port=args.port, baud=args.baud)
        if args.label:
            emit("label", text=args.label)

        ser = None
        try:
            while True:
                if ser is None:
                    try:
                        ser = open_port(args.port, args.baud)
                        emit("connected")
                        say("connected")
                    except serial.SerialException:
                        time.sleep(0.5)
                        continue
                try:
                    data = ser.read(4096)
                except (serial.SerialException, OSError) as exc:
                    emit("error", text=str(exc))
                    say(f"DEVICE DISCONNECTED/CRASHED: {exc}")
                    with contextlib.suppress(Exception):
                        ser.close()
                    ser = None
                    splitter = LineSplitter()
                    continue
                if data:
                    chunks += 1
                    text = data.decode("utf-8", errors="replace")
                    emit("rx", hex=data.hex(), text=text)
                    for line in splitter.feed(text):
                        shown = displayable(line, args.show_polling)
                        if shown:
                            say(shown)
                if select.select([sys.stdin], [], [], 0)[0]:
                    line = sys.stdin.readline().strip()
                    if line:
                        emit("label", text=line)
                        say(f"LABEL recorded: {line}")
        except KeyboardInterrupt:
            pass
        finally:
            emit("stop", chunks=chunks)
            if ser is not None:
                ser.close()
    print(f"\nStopped. {chunks} chunks logged to {os.path.normpath(path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
