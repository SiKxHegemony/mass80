"""Serial transport for the Mass80 screen module: by-id path, reconnect with backoff.

Uses only pyserial. The serial object is created through an injectable factory so
tests can substitute a fake and never open a device node.

write() exists for the later reply layer (T6). Nothing in this module calls it.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

from .parser import Event, EventParser

log = logging.getLogger("mass80d.transport")

DEFAULT_PORT = "/dev/serial/by-id/usb-Espressif_Systems_Espressif_Device_123456-if00"
DEFAULT_BAUD = 115200  # same as tools/capture.py; CDC-ACM ignores it in practice
READ_TIMEOUT_S = 0.1
# Serial write timeout, in seconds. Without it Serial.write() blocks forever if the device
# stops draining the USB endpoint, and the read-side silence watchdog cannot fire while the
# main thread sits inside write(). SerialTimeoutException is a SerialException, so
# SerialTransport.write already treats it as a failed write.
WRITE_TIMEOUT_S = 5.0
# Reconnect if NO BYTES (of any kind, including firmware debug text) arrive for this
# long. The largest gap between received chunks in the captures was 4.71 s.
DEFAULT_SILENCE_TIMEOUT_S = 10.0
# Open-failure log lines: at most one per this many seconds, plus count milestones.
OPEN_FAIL_LOG_INTERVAL_S = 30.0


class TransportError(Exception):
    """Raised by write() when the port is absent or the write failed."""


def _default_factory(port: str, baudrate: int, timeout: float) -> Any:
    import serial  # deferred so tests and imports do not need an open port

    return serial.Serial(port, baudrate, timeout=timeout, write_timeout=WRITE_TIMEOUT_S)


def _serial_errors() -> tuple[type[BaseException], ...]:
    try:
        import serial

        return (serial.SerialException, OSError)
    except ImportError:  # pragma: no cover
        return (OSError,)


class SerialTransport:
    def __init__(
        self,
        port: str = DEFAULT_PORT,
        baudrate: int = DEFAULT_BAUD,
        serial_factory: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        backoff_initial: float = 0.5,
        backoff_factor: float = 2.0,
        backoff_max: float = 10.0,
        silence_timeout: float = DEFAULT_SILENCE_TIMEOUT_S,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self._factory = serial_factory or _default_factory
        self._sleep = sleep
        self._clock = clock
        self._b0 = backoff_initial
        self._bf = backoff_factor
        self._bmax = backoff_max
        self._ser: Any = None
        self._failures = 0
        if (
            isinstance(silence_timeout, bool)
            or not isinstance(silence_timeout, (int, float))
            or silence_timeout <= 0
        ):
            raise ValueError("silence_timeout must be a positive number")
        self.silence_timeout = float(silence_timeout)
        self._errors = _serial_errors()
        self.parser = EventParser()
        # Clock time of the last received byte, or of the open.
        self._last_rx: float = 0.0
        # Logging-only state. Nothing here influences reads, writes or timing.
        self._connected_at: float = 0.0
        self._away_since: float | None = None  # last drop, or first failed open
        self._attempts = 0  # open attempts since the last successful open
        self._open_failures = 0  # consecutive failed opens
        self._last_fail_log: float | None = None

    @property
    def connected(self) -> bool:
        return self._ser is not None

    def _delay(self) -> float:
        return min(self._bmax, self._b0 * (self._bf ** (self._failures - 1)))

    def _drop(self) -> None:
        ser, self._ser = self._ser, None
        self.parser.reset()  # never glue a partial line onto a new connection
        if ser is not None:
            with contextlib.suppress(*self._errors):
                ser.close()

    def _connect(self) -> bool:
        if self._failures > 0:
            self._sleep(self._delay())
        try:
            self._ser = self._factory(self.port, self.baudrate, READ_TIMEOUT_S)
        except self._errors as exc:
            self._failures += 1
            self._note_open_failure(exc)
            return False
        self._last_rx = self._clock()  # the silence window starts at the open
        self._note_open_success()
        return True

    def _note_open_failure(self, exc: BaseException) -> None:
        now = self._clock()
        self._attempts += 1
        self._open_failures += 1
        if self._away_since is None:
            self._away_since = now
        n = self._open_failures
        due = (
            self._last_fail_log is None
            or now - self._last_fail_log >= OPEN_FAIL_LOG_INTERVAL_S
            or n in (1, 5, 10)
            or n % 10 == 0
        )
        if due:
            self._last_fail_log = now
            log.warning(
                "open failed: port=%s error=%s: %s; retry in %.1f s; consecutive failures=%d",
                self.port,
                type(exc).__name__,
                exc,
                self._delay(),
                n,
            )

    def _note_open_success(self) -> None:
        now = self._clock()
        self._attempts += 1
        away = "" if self._away_since is None else f", away {now - self._away_since:.1f} s"
        log.info("connected: port=%s, attempt %d%s", self.port, self._attempts, away)
        self._connected_at = now
        self._away_since = None
        self._attempts = 0
        self._open_failures = 0
        self._last_fail_log = None

    def read_events(self) -> list[Event]:
        """One step: connect if needed, do one read, return any decoded events.

        Never raises on port loss; returns [] and retries on the next call, after
        a backoff sleep. Backoff resets only once data is actually received.
        """
        if self._ser is None and not self._connect():
            return []
        try:
            data = self._ser.read(getattr(self._ser, "in_waiting", 0) or 1)
        except self._errors as exc:
            now = self._clock()
            log.warning(
                "read failed, dropping port: %s: %s; connection was up %.1f s",
                type(exc).__name__,
                exc,
                now - self._connected_at,
            )
            self._away_since = now
            self._drop()
            self._failures += 1
            return []
        if not data:
            now = self._clock()
            if now - self._last_rx >= self.silence_timeout:
                # Open but silent: treat as a dead link and use the normal reconnect path.
                log.warning(
                    "silence watchdog: no bytes for %.1f s (timeout %.1f s); "
                    "closing port and reconnecting",
                    now - self._last_rx,
                    self.silence_timeout,
                )
                self._away_since = now
                self._drop()
                self._failures += 1
            return []
        self._failures = 0
        self._last_rx = self._clock()
        return self.parser.feed(data)

    def events(self, should_stop: Callable[[], bool]) -> Iterator[Event]:
        while not should_stop():
            yield from self.read_events()

    def write(self, data: bytes) -> None:
        if self._ser is None:
            raise TransportError("port not connected")
        try:
            self._ser.write(data)
        except self._errors as exc:
            log.warning("write failed, dropping port: %s: %s", type(exc).__name__, exc)
            self._away_since = self._clock()
            self._drop()
            self._failures += 1
            raise TransportError(f"write failed: {exc}") from exc

    def close(self) -> None:
        self._drop()
