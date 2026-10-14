"""Mass80 screen-module event parser.

Turns the raw serial byte stream into Event objects. See docs/protocol.md section 2.

Rules implemented (all from the captures):
  * The stream interleaves firmware debug text and JSON. Every event is echoed as
    `[CDC-TX] (<body>)` (not JSON, contains no brace) and then sent as `{<body>}`.
    Only the brace copy is parsed, so each event is emitted once.
  * JSON need not start at column 0, so each completed line is scanned for `{`.
  * Input is buffered across feed() calls, so chunks may split anywhere.
  * Terminators may be `\\n` or `\\r\\n`.
  * Debug text, invalid UTF-8 and malformed JSON are ignored without raising.
  * Unknown actions are surfaced as KIND_UNKNOWN, never dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

KIND_POLL = "poll"
KIND_INPUT = "input"
KIND_UNKNOWN = "unknown"

POLL_ACTIONS = frozenset({"get_time", "get_music_info", "get_performance"})
INPUT_ACTIONS = frozenset(
    {"volume", "light", "send_key", "music_play", "music_pause", "music_prev", "music_next"}
)

# Longest line kept while waiting for a terminator. Observed debug lines are a few
# hundred bytes; this only bounds memory if the stream never produces a newline.
DEFAULT_MAX_LINE_BYTES = 64 * 1024


@dataclass(frozen=True)
class Event:
    """One decoded device -> host JSON object.

    `value` is a slider POSITION (absolute), not a delta, and is not range-limited
    here: the true range is unknown. A slider event with value None is the
    begin-of-gesture marker (see is_slider_begin).
    """

    kind: str
    action: str | None
    value: Any = None
    key: str | None = None
    version: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def is_slider_begin(self) -> bool:
        return self.action in ("volume", "light") and self.value is None


def _classify(obj: dict[str, Any]) -> Event:
    action = obj.get("action")
    if not isinstance(action, str):
        return Event(KIND_UNKNOWN, None, obj.get("value"), None, None, obj)
    if action in POLL_ACTIONS:
        kind = KIND_POLL
    elif action in INPUT_ACTIONS:
        kind = KIND_INPUT
    else:
        kind = KIND_UNKNOWN
    key = obj.get("key")
    version = obj.get("version")
    return Event(
        kind,
        action,
        obj.get("value"),
        key if isinstance(key, str) else None,
        version if isinstance(version, str) else None,
        obj,
    )


class EventParser:
    def __init__(self, max_line_bytes: int = DEFAULT_MAX_LINE_BYTES) -> None:
        self._max = max_line_bytes
        self._buf = bytearray()
        self._decoder = json.JSONDecoder()

    def reset(self) -> None:
        """Drop any partial line (call after a reconnect)."""
        self._buf.clear()

    def feed(self, data: bytes) -> list[Event]:
        self._buf.extend(data)
        events: list[Event] = []
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._buf[:nl])
            del self._buf[: nl + 1]
            events.extend(self._parse_line(line))
        if len(self._buf) > self._max:
            # No terminator in a very long run: discard rather than grow forever.
            self._buf.clear()
        return events

    def _parse_line(self, line: bytes) -> list[Event]:
        text = line.decode("utf-8", errors="replace")
        events: list[Event] = []
        pos = text.find("{")
        while pos >= 0:
            try:
                obj, end = self._decoder.raw_decode(text, pos)
            except ValueError, RecursionError:  # deep nesting raises RecursionError
                pos = text.find("{", pos + 1)
                continue
            if isinstance(obj, dict):
                events.append(_classify(obj))
            pos = text.find("{", end)
        return events
