#!/usr/bin/env python3
"""ONE-SHOT controlled write test: answer a single get_time poll, then only listen.

Approved by Shane 2026-10-08 for exactly this: ONE get_time reply, nothing else.
Hard limits enforced in code:
  - sends at most ONE frame, ever, and only the get_time reply built below
  - waits for the device's own {"action":"get_time"...} poll before sending
  - never sends any other action (no enter_udisk_mode, change_theme, etc.)
  - after the single send it is read-only until the observation window ends
  - everything received is logged to captures/ as JSON lines (same format as capture.py)

Usage:
    .venv/bin/python tools/send_get_time_once.py --dry-run     # prints the frame, touches no device
    .venv/bin/python tools/send_get_time_once.py               # live: waits for a poll, sends once
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

import serial

PORT = "/dev/serial/by-id/usb-Espressif_Systems_Espressif_Device_123456-if00"
CAPTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "captures")
POLL_RE = re.compile(r'^\{"action":"get_time"[^{}]*\}\s*$')


def build_frame(with_offset: bool = False) -> bytes:
    # Shape recovered by static analysis (docs/protocol-host-messages.md):
    # keys in order: action, time, caps. Time format %Y-%m-%d %H:%M:%S, optionally followed by a
    # UTC offset "+HH:MM"/"-HH:MM" (vendor currentDateTime() appears to append one; see doc).
    # The vendor writeData() appends "\n" itself when missing, so "\n" is the correct terminator.
    now = datetime.now().astimezone()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    if with_offset:
        z = now.strftime("%z")  # e.g. -0400
        stamp += f"{z[:3]}:{z[3:]}"
    payload = {"action": "get_time", "time": stamp, "caps": False}
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--with-offset", action="store_true", help="append UTC offset to the time string")
    ap.add_argument("--wait-poll", type=float, default=30.0, help="seconds to wait for the device's poll")
    ap.add_argument("--observe", type=float, default=20.0, help="seconds to keep listening after the send")
    args = ap.parse_args()

    if args.dry_run:
        print("DRY RUN, nothing opened or sent. Frame would be:")
        print(repr(build_frame(args.with_offset)))
        return 0

    os.makedirs(CAPTURE_DIR, exist_ok=True)
    path = os.path.join(CAPTURE_DIR, datetime.now().strftime("write-test-%Y%m%d-%H%M%S.log"))
    ser = serial.Serial(PORT, 115200, timeout=0.1, write_timeout=2)
    sent = False
    buf = ""
    t0 = time.time()
    t_sent = None
    with open(path, "a", encoding="utf-8") as log:
        def emit(kind, **f):
            log.write(json.dumps({"t": time.time(), "kind": kind, **f}) + "\n")
            log.flush()

        emit("start", note="single get_time reply test")
        print(f"log: {os.path.normpath(path)}\nwaiting up to {args.wait_poll:.0f}s for the device's get_time poll...")
        try:
            while True:
                now = time.time()
                if not sent and now - t0 > args.wait_poll:
                    print("no get_time poll seen; nothing was sent.")
                    emit("abort", reason="no poll")
                    break
                if sent and now - t_sent > args.observe:
                    break
                data = ser.read(4096)
                if data:
                    text = data.decode("utf-8", errors="replace")
                    emit("rx", hex=data.hex(), text=text)
                    buf += text.replace("\r", "")
                    *lines, buf = buf.split("\n")
                    for line in lines:
                        if line.startswith('{"action"'):
                            print(f"[{now - t0:6.1f}s] device: {line}")
                        if not sent and POLL_RE.match(line):
                            frame = build_frame(args.with_offset)
                            emit("tx", hex=frame.hex(), text=frame.decode())
                            ser.write(frame)
                            ser.flush()
                            sent = True
                            t_sent = time.time()
                            print(f"[{now - t0:6.1f}s] SENT (once): {frame!r}")
        except (serial.SerialException, OSError) as exc:
            emit("error", text=str(exc))
            print(f"SERIAL ERROR: {exc}")
        finally:
            emit("stop", sent=sent)
            try:
                ser.close()
            except Exception:
                pass
    print(f"done. sent={sent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
