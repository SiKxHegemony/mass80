"""Background cover-art worker: fetch and resize OFF the main thread.

The daemon's main thread is the only serial writer and must never wait on the network or on
`magick`. It calls `prefetch(url)` (non-blocking, de-duplicated) every poll and `lookup(url)`
(non-blocking, no side effects) when it needs the bytes. One daemon worker thread does the
slow part. No thread is started at import or construction; only `start()` starts one.

Shared state (queue, in-flight URL, failures, stopped flag) is guarded by ONE lock, never held
while `fetch` or the cache runs. `ArtCache` has its own lock. A stopped service is final.

Logging: no art URL above DEBUG; INFO carries only counts and reasons; never image bytes.
"""

from __future__ import annotations

import collections
import enum
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from mass80d.cover_art import ArtCache, CoverConfig, get_jpeg

log = logging.getLogger("mass80d.cover_service")

MAX_FAILED = 16


class CoverStatus(enum.Enum):
    READY = "ready"
    PENDING = "pending"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CoverLookup:
    status: CoverStatus
    jpeg: bytes | None = None  # set only when status is READY


class CoverService:
    def __init__(
        self,
        cfg: CoverConfig,
        cache: ArtCache | None = None,
        *,
        fetch: Callable[[str], bytes | None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        fail_ttl_s: float = 60.0,
        queue_size: int = 2,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be >= 1")
        self.cfg = cfg
        self.cache = cache if cache is not None else ArtCache(cfg.cache_size)
        self._fetch = fetch if fetch is not None else self._default_fetch
        self._clock = clock
        self._fail_ttl_s = fail_ttl_s
        self._queue_size = queue_size
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)  # shares the one lock
        self._queue: collections.deque[str] = collections.deque()
        self._running: str | None = None
        self._failed: collections.OrderedDict[str, float] = collections.OrderedDict()
        self._thread: threading.Thread | None = None
        self._started = False
        self._stopped = False

    def _default_fetch(self, url: str) -> bytes | None:
        return get_jpeg(url, self.cfg, self.cache)

    @property
    def failed_count(self) -> int:
        """Number of remembered failed URLs (read-only, bounded by MAX_FAILED)."""
        with self._lock:
            return len(self._failed)

    def start(self) -> None:
        with self._lock:
            if self._started or self._stopped:
                return
            self._started = True
            self._thread = threading.Thread(target=self._run, daemon=True, name="mass80d-cover")
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> bool:
        with self._cond:
            self._stopped = True
            self._queue.clear()
            thread = self._thread
            self._cond.notify_all()
        if thread is None:
            return True
        thread.join(timeout)
        if thread.is_alive():
            log.warning("cover worker did not stop in time")
            return False
        return True

    def _failed_recently(self, url: str) -> bool:
        """Caller holds the lock. Drops an expired entry."""
        t = self._failed.get(url)
        if t is None:
            return False
        if self._clock() - t < self._fail_ttl_s:
            return True
        del self._failed[url]
        return False

    def prefetch(self, art_url: str) -> CoverStatus:
        if not isinstance(art_url, str) or art_url == "":
            return CoverStatus.UNKNOWN
        try:
            if self.cache.get((art_url, self.cfg.size)) is not None:
                return CoverStatus.READY
            with self._cond:
                if not self._started or self._stopped:
                    return CoverStatus.UNKNOWN
                if art_url == self._running or art_url in self._queue:
                    return CoverStatus.PENDING
                if self._failed_recently(art_url):
                    return CoverStatus.FAILED
                if len(self._queue) >= self._queue_size:
                    self._queue.popleft()  # oldest QUEUED job; the running one is not in here
                    log.debug("cover queue full: dropped the oldest queued job")
                self._queue.append(art_url)
                self._cond.notify_all()
                return CoverStatus.PENDING
        except Exception:  # never raise into the main loop
            log.exception("cover prefetch failed")
            return CoverStatus.UNKNOWN

    def lookup(self, art_url: str) -> CoverLookup:
        if not isinstance(art_url, str) or art_url == "":
            return CoverLookup(CoverStatus.UNKNOWN)
        hit = self.cache.get((art_url, self.cfg.size))
        if hit is not None:
            return CoverLookup(CoverStatus.READY, hit)
        with self._lock:
            if art_url == self._running or art_url in self._queue:
                return CoverLookup(CoverStatus.PENDING)
            t = self._failed.get(art_url)
            if t is not None and self._clock() - t < self._fail_ttl_s:
                return CoverLookup(CoverStatus.FAILED)
        return CoverLookup(CoverStatus.UNKNOWN)

    def wait_idle(self, timeout: float) -> bool:
        with self._cond:
            return self._cond.wait_for(
                lambda: not self._queue and self._running is None, timeout=timeout
            )

    def _record_failure(self, url: str) -> None:
        """Caller holds the lock."""
        self._failed.pop(url, None)
        self._failed[url] = self._clock()
        while len(self._failed) > MAX_FAILED:
            self._failed.popitem(last=False)

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    self._cond.notify_all()
                    return
                url = self._queue.popleft()
                self._running = url
            jpeg: bytes | None = None
            try:
                jpeg = self._fetch(url)
                if isinstance(jpeg, (bytes, bytearray)) and len(jpeg) > 0:
                    jpeg = bytes(jpeg)
                    # idempotent for the default fetch (already cached); needed for a fake
                    self.cache.put((url, self.cfg.size), jpeg)
                else:
                    jpeg = None
            except Exception as exc:  # a bug must not kill the thread
                jpeg = None
                # type only above DEBUG: an exception message could carry the URL
                log.error("cover fetch raised %s", type(exc).__name__)
                log.debug("cover fetch traceback", exc_info=True)
            if jpeg is not None:
                log.info("cover art ready (%d bytes)", len(jpeg))
            else:
                log.info("cover art failed")
            with self._cond:
                if jpeg is None:
                    self._record_failure(url)
                else:
                    self._failed.pop(url, None)
                self._running = None
                self._cond.notify_all()
