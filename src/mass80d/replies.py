"""Poll-reply layer: the ONLY code in this package that writes to the device.

Writes go through an injectable `write(bytes)` callable (in production
SerialTransport.write); tests pass a list.append. Only four reply actions are
whitelisted; everything else is refused. Anything that changes device state
(for example enter_udisk_mode or change_theme) is deliberately absent.

Status of each frame shape:
  get_time          VERIFIED on hardware (docs/protocol-host-messages.md): needs a UTC
                    offset suffix on `time`; terminator "\\n"; caps false accepted.
  get_performance   INFERRED from disassembly, NEVER sent to the device. ASSUMPTIONS:
                    six int32 values; key order as listed; units unconfirmed
                    (percent / MHz / degC / KB/s are guesses).
  get_music_info    INFERRED from disassembly (docs/music-info-decode.md), NEVER sent to the
                    device. SDK-exact: compact, alphabetical keys action, artist, duration,
                    index, progress, state, title; duration/progress integer ms, state
                    integer 1/0, index 0; title/artist whitelist-filtered (whole field
                    blanked on any other character). No opencc conversion (deviation).
                    No player -> the empty frame every poll. Unreadable -> nothing.
  volume            INFERRED from disassembly, NEVER sent to the device
                    (docs/volume-reply-decode.md). The answer to a bare device
                    {"action":"volume"} query: {"query":"volume","result":"ok","value":N},
                    N an integer 0..100, NO "action" key, compact, one "\\n". ASSUMPTIONS:
                    the device accepts it as the answer; key order is irrelevant.
The last three are disabled by default in config.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import Config
from .cover_service import CoverLookup, CoverService, CoverStatus
from .music_reader import NO_PLAYER, MusicReading
from .parser import Event
from .player_select import PlayerInfo, PlayerSelector
from .transport import TransportError
from .volume_reader import VolumeReading

log = logging.getLogger("mass80d.replies")

REPLY_ACTIONS = frozenset({"get_time", "get_performance", "get_music_info", "volume", "get_cover"})

# Minimum seconds between frames per action, just under the observed poll period
# (get_time 2 s, get_music_info 1 s, get_performance 3 s): at most one reply per poll.
# volume: a query marker arrives at the start of every slider gesture, not on a timer.
# get_cover: one request per track change; 0.5 s only stops a burst.
MIN_INTERVAL_S: dict[str, float] = {
    "get_time": 1.5,
    "get_music_info": 0.8,
    "get_performance": 2.5,
    "volume": 0.5,
    "get_cover": 0.5,
}

INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1
MUSIC_BACKOFF_S = 2.0  # no new music read this long after a failed one
MAX_COVER_JPEG_BYTES = 200_000
COVER_INDEX_MAX = 1024
COVER_DIM_MAX = 1024


class ReplyRefused(Exception):
    """Raised when a frame is not one of the whitelisted replies."""


def _encode(obj: dict[str, Any]) -> bytes:
    # Compact separators, key order preserved, one line, "\n" terminator.
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def _int32(name: str, v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not INT32_MIN <= v <= INT32_MAX:
        raise ValueError(f"{name} must be an int32, got {v!r}")
    return v


def _utc_offset(now: datetime) -> str:
    off = now.utcoffset()
    if off is None:
        raise ValueError("time must be timezone-aware (an offset suffix is required)")
    minutes = int(off.total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    h, m = divmod(abs(minutes), 60)
    return f"{sign}{h:02d}:{m:02d}"


def build_get_time(now: datetime | None = None, caps: bool = False) -> bytes:
    if now is None:
        now = datetime.now().astimezone()  # local zone and its current offset
    text = now.strftime("%Y-%m-%d %H:%M:%S") + _utc_offset(now)
    return _encode({"action": "get_time", "time": text, "caps": bool(caps)})


def build_get_performance(
    memory_usage: int,
    cpu_usage: int,
    cpu_freq: int,
    cpu_temp: int,
    net_upload: int,
    net_download: int,
) -> bytes:
    return _encode(
        {
            "action": "get_performance",
            "memory_usage": _int32("memory_usage", memory_usage),
            "cpu_usage": _int32("cpu_usage", cpu_usage),
            "cpu_freq": _int32("cpu_freq", cpu_freq),
            "cpu_temp": _int32("cpu_temp", cpu_temp),
            "net_upload": _int32("net_upload", net_upload),
            "net_download": _int32("net_download", net_download),
        }
    )


def build_get_music_info(
    progress: int, duration: int, title: str, artist: str, index: int, state: bool
) -> bytes:
    if not isinstance(title, str) or not isinstance(artist, str):
        raise ValueError("title and artist must be strings")
    if not isinstance(state, bool):
        raise ValueError("state must be a bool")
    # SDK-exact: compact, keys in alphabetical order (docs/music-info-decode.md section 7).
    return _encode(
        {
            "action": "get_music_info",
            "artist": artist,
            "duration": _int32("duration", duration),
            "index": _int32("index", index),
            "progress": _int32("progress", progress),
            "state": 1 if state else 0,
            "title": title,
        }
    )


# The SDK's title/artist whitelist (docs/music-info-decode.md section 5).
_SDK_TEXT_ALLOWED = frozenset(
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ ~!@#$%^&*(),.;:\"'[]<>{}-_|/"
)
_CJK_FIRST, _CJK_LAST = 0x4E00, 0x9FFF


def filter_sdk_text(s: str) -> str:
    """Return s unchanged if every character is whitelisted, else "" (the whole field).

    Not a strip: one bad character anywhere blanks the field, as the SDK does. The SDK's
    opencc Traditional-to-Simplified conversion is NOT copied (a documented deviation).
    """
    for ch in s:
        if ch not in _SDK_TEXT_ALLOWED and not _CJK_FIRST <= ord(ch) <= _CJK_LAST:
            return ""
    return s


def ms_from_us(us: int) -> int:
    """Microseconds to integer milliseconds, clamped to 0..INT32_MAX. Never raises."""
    return min(INT32_MAX, max(0, us // 1000))


class ZeroLengthHold:
    """Keeps the music bar moving when a PLAYING player reports length 0 (seen with Firefox).

    Remembers the last good (length > 0) reading for one (instance, title, artist). For a
    Playing reading with length 0 and the SAME key it returns the held duration and the held
    progress advanced by the injected monotonic clock, clamped to 0..duration and to int32
    milliseconds. Different key, or no good reading yet: what the player reports, and the held
    values are dropped. A non-Playing reading is returned unchanged (a paused Spotify was seen
    reporting 0/0), but it freezes the extrapolated position so a later resume continues from
    there. Values are microseconds, as playerctl reports them.
    """

    _MAX_US = INT32_MAX * 1000  # ms_from_us() never exceeds INT32_MAX anyway; stay under it

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self.reset()

    def reset(self) -> None:
        self._key: tuple[str | None, str, str] | None = None
        self._length_us = 0
        self._anchor_us = 0
        self._anchor_at = 0.0
        self._advancing = False  # True while the anchor is moving with the clock

    def _position_now(self, now: float) -> int:
        pos = self._anchor_us
        if self._advancing:
            pos += int(max(0.0, now - self._anchor_at) * 1_000_000)
        return max(0, min(pos, self._length_us, self._MAX_US))

    def apply(
        self,
        instance: str | None,
        title: str,
        artist: str,
        status: str,
        length_us: int,
        position_us: int,
    ) -> tuple[int, int]:
        now = self._clock()
        key = (instance, title, artist)
        playing = status == "Playing"
        if length_us > 0:
            self._key = key
            self._length_us = length_us
            self._anchor_us = position_us
            self._anchor_at = now
            self._advancing = playing
            return length_us, position_us
        if length_us == 0 and self._key == key and self._length_us > 0:
            pos = self._position_now(now)
            self._anchor_us, self._anchor_at, self._advancing = pos, now, playing
            if playing:
                return self._length_us, pos
            return length_us, position_us  # paused with length 0: unchanged
        if self._key != key:
            self.reset()
        return length_us, position_us


def build_volume_query_reply(percent: int) -> bytes:
    """Answer to a bare device volume query (docs/volume-reply-decode.md). No "action" key."""
    if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
        raise ValueError(f"percent must be an int 0..100, got {percent!r}")
    return _encode({"query": "volume", "result": "ok", "value": percent})


def build_get_cover(jpeg: bytes | None, index: int) -> bytes:
    """The get_cover reply (docs/cover-decode.md). INFERRED, NEVER sent to the device.

    Keys action, image_data, index (the SDK's sorted order); image_data is the standard
    padded base64 of a JPEG, or the 4-character string "null" (the SDK's failure shape).
    """
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= COVER_INDEX_MAX:
        raise ValueError(f"index must be an int 0..{COVER_INDEX_MAX}, got {index!r}")
    if jpeg is None:
        data = "null"
    else:
        if not isinstance(jpeg, (bytes, bytearray)) or bytes(jpeg[:3]) != b"\xff\xd8\xff":
            raise ValueError("jpeg must be bytes starting with FF D8 FF")
        if len(jpeg) > MAX_COVER_JPEG_BYTES:
            raise ValueError(f"jpeg is larger than {MAX_COVER_JPEG_BYTES} bytes")
        data = base64.b64encode(bytes(jpeg)).decode("ascii")
    return _encode({"action": "get_cover", "image_data": data, "index": index})


def _valid_volume_reply(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and set(obj) == {"query", "result", "value"}
        and obj["query"] == "volume"
        and obj["result"] == "ok"
        and type(obj["value"]) is int
        and 0 <= obj["value"] <= 100
    )


@dataclass(frozen=True)
class _PendingCover:
    url: str
    index: int
    deadline: float


class ReplyLayer:
    def __init__(
        self,
        write: Callable[[bytes], Any],
        config: Config,
        performance_provider: Callable[[], Any] | None = None,
        music_provider: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now_func: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        volume_reader: Callable[[], VolumeReading] | None = None,
        music_reader: Callable[[], Any] | None = None,
        music_selector: PlayerSelector | None = None,
        *,
        cover_service: CoverService | None = None,
        cover_test_jpeg: bytes | None = None,
    ) -> None:
        self._write = write
        self._cfg = config
        self._perf = performance_provider
        self._music = music_provider
        self._music_reader = music_reader
        self._music_selector = music_selector
        self._music_hold = ZeroLengthHold(clock)
        self._music_failing = False  # True inside a run of unreadable reads
        self._music_retry_at = 0.0  # no music read before this clock value (back-off)
        self._clock = clock
        self._now = now_func
        self._volume_reader = volume_reader
        self._volume_failing = False  # True inside a run of unreadable/muted reads
        self._last_sent: dict[str, float] = {}
        self._cover_service = cover_service
        self._cover_test_jpeg = cover_test_jpeg
        self._last_art_url = ""  # art URL of the player last shown; never logged above DEBUG
        self._cover_pending: _PendingCover | None = None
        self._cover_test_sent = 0
        self._perf_sent = 0
        self._perf_limit_logged = False
        self._cover_limit_logged = False
        self._cover_bad_logged = False
        self._closed = False

    def _send_frame(self, action: str, frame: bytes) -> bool:
        """Validate, rate-limit and write one frame. True if written. PRIVATE: only on_poll
        and on_volume_query call this, with frames built by the build_* functions.

        Raises ReplyRefused for anything outside the whitelist or malformed.
        Write failures are logged and return False without using the rate slot.
        """
        if action not in REPLY_ACTIONS:
            raise ReplyRefused(f"action {action!r} is not an allowed reply")
        if not frame.endswith(b"\n") or frame.count(b"\n") != 1:
            raise ReplyRefused("frame must be exactly one line ending in a newline")
        try:
            obj = json.loads(frame)
        except ValueError as exc:
            raise ReplyRefused("frame is not valid JSON") from exc
        if action == "volume":
            if not _valid_volume_reply(obj):
                raise ReplyRefused("frame is not a valid volume query reply")
        elif not isinstance(obj, dict) or obj.get("action") != action:
            raise ReplyRefused("frame action does not match the declared action")
        now = self._clock()
        last = self._last_sent.get(action)
        if last is not None and now - last < MIN_INTERVAL_S[action]:
            return False
        try:
            self._write(frame)
        except (TransportError, OSError) as exc:
            log.warning("reply %s not written: %s", action, exc)
            return False
        self._last_sent[action] = now
        return True

    def on_poll(self, event: Event) -> bool:
        """Answer a device poll if enabled, supported and not rate-limited."""
        action = event.action
        try:
            if action == "get_time" and self._cfg.reply_get_time:
                return self._send_frame("get_time", build_get_time(self._now()))
            if action == "get_performance" and self._cfg.reply_get_performance and self._perf:
                limit = self._cfg.perf_max_replies
                if limit > 0 and self._perf_sent >= limit:
                    if not self._perf_limit_logged:
                        self._perf_limit_logged = True
                        log.info("performance: frame limit reached, not answering")
                    return False
                values = self._perf()
                if values is None:
                    return False
                if self._send_frame("get_performance", build_get_performance(*values)):
                    self._perf_sent += 1
                    return True
                return False
            if action == "get_music_info" and self._cfg.reply_get_music_info:
                if self._music_selector is not None:
                    return self._on_music_poll(self._music_selector.select)
                if self._music_reader is not None:
                    return self._on_music_poll(self._music_reader)
                if self._music:
                    values = self._music()
                    if values is None:
                        return False
                    return self._send_frame("get_music_info", build_get_music_info(*values))
        except (ValueError, TypeError) as exc:
            log.warning("reply for %s skipped: %s", action, exc)
        except Exception:  # provider bugs must not kill the daemon
            log.exception("provider for %s failed", action)
        return False

    def _on_music_poll(self, reader: Callable[[], Any]) -> bool:
        """get_music_info from the selector (or a single-player reader). Rate-limited BEFORE
        the read; after a failed read, no new read for MUSIC_BACKOFF_S. No player -> the SDK's
        empty frame, every poll. Unreadable -> nothing, one log line per run of failures."""
        now = self._clock()
        last = self._last_sent.get("get_music_info")
        if last is not None and now - last < MIN_INTERVAL_S["get_music_info"]:
            return False
        if now < self._music_retry_at:
            return False
        try:
            result = reader()
        except Exception:  # reader bugs must not kill the daemon
            log.exception("music reader failed")
            self._music_retry_at = now + MUSIC_BACKOFF_S
            self._music_failing = True
            return False
        if isinstance(result, PlayerInfo):
            self._music_failing = False
            self._last_art_url = result.art_url
            self._prefetch_cover(result.art_url)
            length_us, position_us = self._music_hold.apply(
                result.instance,
                result.title,
                result.artist,
                result.status,
                result.length_us,
                result.position_us,
            )
            frame = build_get_music_info(
                ms_from_us(position_us),
                ms_from_us(length_us),
                filter_sdk_text(result.title),
                filter_sdk_text(result.artist),
                0,
                result.status == "Playing",
            )
        elif isinstance(result, MusicReading):
            self._music_failing = False
            self._last_art_url = ""
            frame = build_get_music_info(
                ms_from_us(result.position_us),
                ms_from_us(result.length_us),
                filter_sdk_text(result.title),
                filter_sdk_text(result.artist),
                0,
                result.status == "Playing",
            )
        elif result is NO_PLAYER:
            self._music_failing = False
            self._last_art_url = ""
            frame = build_get_music_info(0, 0, "", "", 0, False)
        else:
            self._last_art_url = ""
            if not self._music_failing:
                self._music_failing = True
                log.info("get_music_info not answered: the player could not be read")
            self._music_retry_at = now + MUSIC_BACKOFF_S
            return False
        return self._send_frame("get_music_info", frame)

    # --- get_cover ---------------------------------------------------------------------
    def _prefetch_cover(self, url: str) -> None:
        """Ask the worker to start on this art (non-blocking). Never raises."""
        if not self._cfg.reply_get_cover or self._cover_service is None or url == "":
            return
        try:
            self._cover_service.prefetch(url)
        except Exception as exc:  # the art is optional; the music frame must still go out
            log.debug("cover prefetch failed: %s", exc)

    def _send_cover(self, jpeg: bytes | None, index: int) -> bool:
        """Build and send one get_cover frame; False if rate-limited, refused or failed."""
        try:
            return self._send_frame("get_cover", build_get_cover(jpeg, index))
        except (ValueError, ReplyRefused) as exc:
            log.warning("get_cover reply skipped: %s", exc)
            return False

    def _send_cover_failure(self, index: int) -> None:
        if self._cfg.cover_reply_null:
            self._send_cover(None, index)

    def _lookup(self, url: str) -> CoverLookup:
        """Non-blocking lookup; no service or any error counts as UNKNOWN. Never raises."""
        if self._cover_service is None:
            return CoverLookup(CoverStatus.UNKNOWN)
        try:
            return self._cover_service.lookup(url)
        except Exception as exc:  # DEBUG only: the message may carry the art URL
            log.debug("cover lookup failed: %s", exc)
            return CoverLookup(CoverStatus.UNKNOWN)

    def on_cover_request(self, event: Event) -> bool:
        """Answer a device get_cover request. True only if a frame was written now.

        A request whose art is still being fetched is stored and answered later by tick().
        """
        cfg = self._cfg
        if not cfg.reply_get_cover:
            return False
        raw = event.raw
        width, height, index = raw.get("width"), raw.get("height"), raw.get("index")
        if (
            type(width) is not int
            or type(height) is not int
            or type(index) is not int
            or not 1 <= width <= COVER_DIM_MAX
            or not 1 <= height <= COVER_DIM_MAX
        ):
            if not self._cover_bad_logged:
                self._cover_bad_logged = True
                log.debug("get_cover request ignored (malformed): %r", raw)
            return False
        index = min(COVER_INDEX_MAX, max(0, index))
        # The size sent is always cfg.cover_size, not the requested one (documented deviation).
        if self._cover_test_jpeg is not None:
            limit = cfg.cover_test_max_replies
            if limit > 0 and self._cover_test_sent >= limit:
                if not self._cover_limit_logged:
                    self._cover_limit_logged = True
                    log.info("cover test image: reply limit reached, not answering")
                return False
            if self._send_cover(self._cover_test_jpeg, index):
                self._cover_test_sent += 1
                log.info("cover test image sent (%d bytes)", len(self._cover_test_jpeg))
                return True
            return False
        url = self._last_art_url
        if url == "":
            self._cover_pending = None
            if cfg.cover_reply_null:
                return self._send_cover(None, index)
            return False
        look = self._lookup(url)
        status = look.status
        if status is CoverStatus.READY:
            self._cover_pending = None
            return self._send_cover(look.jpeg, index)
        if status is CoverStatus.FAILED:
            self._cover_pending = None
            if cfg.cover_reply_null:
                return self._send_cover(None, index)
            return False
        self._cover_pending = _PendingCover(url, index, self._clock() + cfg.cover_wait_s)
        return False

    def tick(self) -> None:
        """Answer a deferred get_cover when its art is ready, failed or out of time (no raise)."""
        try:
            self._tick_cover()
        except Exception as exc:
            log.debug("cover tick failed: %s", exc)

    def _tick_cover(self) -> None:
        pending = self._cover_pending
        if pending is None:
            return
        if self._last_art_url != pending.url:
            self._cover_pending = None  # the track changed; the screen will ask again
            return
        look = self._lookup(pending.url)
        status = look.status
        if status is CoverStatus.READY:
            if self._send_cover(look.jpeg, pending.index):
                self._cover_pending = None
            elif self._clock() >= pending.deadline:
                self._cover_pending = None  # rate-limited or failing past the deadline
            return
        if status is CoverStatus.FAILED or self._clock() >= pending.deadline:
            self._cover_pending = None
            self._send_cover_failure(pending.index)

    def close(self) -> None:
        """Stop the cover worker (if any). Idempotent; never raises."""
        if self._closed:
            return
        self._closed = True
        service = self._cover_service
        if service is None:
            return
        try:
            service.stop(2.0)
        except Exception as exc:
            log.debug("cover service stop failed: %s", exc)

    def on_volume_query(self) -> bool:
        """Answer a bare device volume query with the host volume as a percent of the cap.

        Only when enabled. Nothing is written (one log line per run of failures) when the
        volume is muted or unreadable. Rate-limited BEFORE the read, so a burst of markers
        does not spawn wpctl. The reader is read-only and is not gated by --no-actions.
        """
        if not self._cfg.reply_volume_query or self._volume_reader is None:
            return False
        last = self._last_sent.get("volume")
        if last is not None and self._clock() - last < MIN_INTERVAL_S["volume"]:
            return False
        try:
            reading = self._volume_reader()
            if reading.volume is None or reading.muted:
                if not self._volume_failing:
                    self._volume_failing = True
                    why = "muted" if reading.muted else "unreadable"
                    log.info("volume query not answered: system volume is %s", why)
                return False
            self._volume_failing = False
            cap = self._cfg.volume_max_fraction
            percent = min(100, max(0, round(reading.volume / cap * 100)))
            return self._send_frame("volume", build_volume_query_reply(percent))
        except (ValueError, TypeError, OverflowError) as exc:
            log.warning("volume query reply skipped: %s", exc)
        except Exception:  # reader bugs must not kill the daemon
            log.exception("volume reader failed")
        return False
