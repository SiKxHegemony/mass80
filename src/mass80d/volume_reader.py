"""Read-only system volume reader: runs `wpctl get-volume <sink>` and parses the answer.

This changes NOTHING on the system, so it deliberately does not go through the ActionGate
(`--no-actions` still means "no system change"). It needs the command's stdout, which
CommandRunner does not return, so it has its own injectable `run` (default subprocess.run).
It never raises, never uses a shell, and gives up after 2 seconds.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("mass80d.volume_reader")

READ_TIMEOUT_S = 2

# `wpctl get-volume` prints "Volume: 0.30" or "Volume: 0.30 [MUTED]".
_LINE = re.compile(r"^Volume:\s+(\d+(?:\.\d+)?)(\s+\[MUTED\])?$")


@dataclass(frozen=True)
class VolumeReading:
    """volume is None when it could not be read or parsed."""

    volume: float | None
    muted: bool = False


UNREADABLE = VolumeReading(None, False)


def parse_get_volume(text: str) -> VolumeReading:
    m = _LINE.match(text.strip())
    if m is None:
        return UNREADABLE
    return VolumeReading(float(m.group(1)), m.group(2) is not None)


class VolumeReader:
    def __init__(self, sink: str, run: Callable[..., Any] | None = None) -> None:
        self._sink = sink
        self._run = run  # None -> subprocess.run, resolved at call time

    def __call__(self) -> VolumeReading:
        run = self._run or subprocess.run
        argv = ["wpctl", "get-volume", self._sink]
        try:
            res = run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=READ_TIMEOUT_S,
                check=False,
            )
            if res.returncode != 0:
                log.debug("wpctl get-volume exited %s", res.returncode)
                return UNREADABLE
            out = res.stdout
            if out is None:
                return UNREADABLE
            text = out.decode("utf-8") if isinstance(out, (bytes, bytearray)) else str(out)
            return parse_get_volume(text)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            log.debug("wpctl get-volume failed: %s", exc)
            return UNREADABLE
        except Exception:  # never raise into the dispatcher
            log.exception("unexpected error reading the system volume")
            return UNREADABLE
