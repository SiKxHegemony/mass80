"""Cover-art pipeline: an untrusted `artUrl` in, JPEG bytes (or None) out.

Pure functions plus one small cache. No threads, no device, no daemon wiring (T22/T23).
Every step fails closed: any problem returns None and `get_jpeg` never raises.

Pipeline: classify_art_url (scheme/host/path allow-list) -> fetch_https or read_local (size
capped) -> sniff_image (JPEG or PNG magic only) -> resize_to_jpeg (ImageMagick, explicit
coder, resource limits, scrubbed environment) -> ArtCache.

Nothing here logs an art URL above DEBUG, and nothing logs image bytes.
"""

from __future__ import annotations

import collections
import http.client
import logging
import os
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

log = logging.getLogger("mass80d.cover_art")

MAX_URL_LEN = 2048
_CHUNK = 65536
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


@dataclass(frozen=True)
class CoverConfig:
    hosts: tuple[str, ...] = ("i.scdn.co",)
    local_dirs: tuple[str, ...] = ()  # empty: nothing local is ever read
    size: int = 480
    quality: int = 60
    fetch_timeout_s: float = 3.0  # per-socket timeout AND total fetch deadline
    max_download_bytes: int = 2_000_000
    max_local_bytes: int = 5_000_000
    max_output_bytes: int = 200_000
    magick_path: str = "/usr/bin/magick"
    magick_timeout_s: float = 5.0
    cache_size: int = 8
    # Only the size x size box in the middle is the cover. The JPEG sent is canvas_width x
    # canvas_height, with a flat grey (fill_grey, 0..255) around the box. The SDK's builder
    # caps the cover at 240 (docs/cover-decode.md section 3). The screen draws a 480x480
    # reply and its Dark theme cover element is 480x480 (Stages 11a/11b, on hardware), so
    # the default `size` and canvas are 480x480 (no grey visible). 240 is the SDK size and
    # is still valid when the canvas matches (size 240, canvas 240x240) or is larger (grey
    # fills around the box). `size` must not exceed either canvas value.
    canvas_width: int = 480
    canvas_height: int = 480
    fill_grey: int = 32

    def __post_init__(self) -> None:
        if not isinstance(self.hosts, tuple) or not all(
            isinstance(h, str) and h != "" for h in self.hosts
        ):
            raise ValueError("hosts must be a tuple of non-empty strings")
        if not isinstance(self.local_dirs, tuple) or not all(
            isinstance(d, str) and os.path.isabs(d) and "\x00" not in d for d in self.local_dirs
        ):
            raise ValueError("local_dirs must be a tuple of absolute paths")
        if not _is_int(self.size) or not 1 <= self.size <= 480:
            raise ValueError("size must be an integer 1..480")
        if not _is_int(self.quality) or not 1 <= self.quality <= 100:
            raise ValueError("quality must be an integer 1..100")
        if not _is_num(self.fetch_timeout_s) or not 0 < self.fetch_timeout_s <= 10:
            raise ValueError("fetch_timeout_s must be a number > 0 and <= 10")
        for name in ("max_download_bytes", "max_local_bytes", "max_output_bytes"):
            v = getattr(self, name)
            if not _is_int(v) or v <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.magick_path, str) or not os.path.isabs(self.magick_path):
            raise ValueError("magick_path must be an absolute path")
        if not _is_num(self.magick_timeout_s) or self.magick_timeout_s <= 0:
            raise ValueError("magick_timeout_s must be a number > 0")
        if not _is_int(self.cache_size) or not 1 <= self.cache_size <= 64:
            raise ValueError("cache_size must be an integer 1..64")
        for name in ("canvas_width", "canvas_height"):
            v = getattr(self, name)
            if not _is_int(v) or not 1 <= v <= 480:
                raise ValueError(f"{name} must be an integer 1..480")
        if self.size > self.canvas_width or self.size > self.canvas_height:
            raise ValueError("size must not exceed canvas_width or canvas_height")
        if not _is_int(self.fill_grey) or not 0 <= self.fill_grey <= 255:
            raise ValueError("fill_grey must be an integer 0..255")


def _refuse(why: str, url: str) -> None:
    log.debug("art url refused (%s): %r", why, url[:200])
    log.info("art url refused: %s", why)


def classify_art_url(url: str, cfg: CoverConfig) -> tuple[str, str] | None:
    """("https", url), ("file", resolved_path) or None. `url` is untrusted."""
    if not isinstance(url, str) or url == "":
        return None
    if len(url) > MAX_URL_LEN:
        _refuse("too long", url)
        return None
    if not all(0x21 <= ord(c) <= 0x7E for c in url):  # printable ASCII, no whitespace
        _refuse("not printable ASCII", url)
        return None
    try:
        parts = urlparse(url)
        scheme = parts.scheme.lower()
        if scheme == "https":
            return _classify_https(url, parts, cfg)
        if scheme == "file":
            return _classify_file(url, parts, cfg)
    except ValueError:
        _refuse("unparsable", url)
        return None
    _refuse("scheme not allowed", url)
    return None


def _classify_https(url: str, parts: Any, cfg: CoverConfig) -> tuple[str, str] | None:
    host = parts.hostname
    if host is None or host.lower() not in cfg.hosts:
        _refuse("host not allowed", url)
        return None
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        _refuse("userinfo present", url)
        return None
    if parts.port not in (None, 443):  # .port raises ValueError on garbage: caught above
        _refuse("port not allowed", url)
        return None
    return ("https", url)


def _classify_file(url: str, parts: Any, cfg: CoverConfig) -> tuple[str, str] | None:
    if parts.netloc not in ("", "localhost"):
        _refuse("file host not allowed", url)
        return None
    if not cfg.local_dirs:
        _refuse("no local_dirs configured", url)
        return None
    path = unquote(parts.path)
    if "\x00" in path or not os.path.isabs(path):
        _refuse("bad file path", url)
        return None
    real = os.path.realpath(path)
    for d in cfg.local_dirs:
        root = os.path.realpath(d)
        try:
            inside = os.path.commonpath([root, real]) == root and real != root
        except ValueError:
            inside = False
        if inside:
            return ("file", real)
    _refuse("file outside local_dirs", url)
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a 3xx surfaces as HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _build_opener() -> urllib.request.OpenerDirector:
    """Hardened opener: no redirects, no proxies, no cookies, default certificate checks.

    `ProxyHandler({})` deliberately ignores an environment proxy (https_proxy etc.).
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _default_open(req: urllib.request.Request, timeout: float) -> Any:
    return _build_opener().open(req, timeout=timeout)


def fetch_https(
    url: str,
    cfg: CoverConfig,
    open_url: Callable[[urllib.request.Request, float], Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> bytes | None:
    """One GET of an already-classified https URL. None on any failure.

    Only a `User-Agent: mass80d` header is sent. A redirect is a failure (see _NoRedirect).
    Status must be 200, the final URL must still be an allowed https host, and the body is
    capped at cfg.max_download_bytes (Content-Length is checked first when present).

    cfg.fetch_timeout_s is BOTH the per-socket-operation timeout handed to the opener and a
    total deadline measured with `clock` from before the request is opened (checked before
    the first read and after every chunk). Honest worst case: the deadline is only checked
    between reads, so one blocked read can still last fetch_timeout_s; the total is bounded
    by the deadline plus ONE socket timeout, about 6 s with the defaults.
    """
    opener = open_url or _default_open
    deadline = clock() + cfg.fetch_timeout_s
    try:
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", "mass80d")
        with opener(req, cfg.fetch_timeout_s) as resp:
            if resp.status != 200:
                log.warning("art fetch: HTTP status %s", resp.status)
                return None
            final = urlparse(resp.geturl())
            if final.scheme.lower() != "https" or (final.hostname or "").lower() not in cfg.hosts:
                log.warning("art fetch: final host not allowed")
                return None
            declared = resp.headers.get("Content-Length")
            if declared is not None:
                try:
                    too_big = int(declared) > cfg.max_download_bytes
                except ValueError:
                    too_big = False  # unusable header: the chunked cap below still applies
                if too_big:
                    log.warning("art fetch: Content-Length over the cap")
                    return None
            chunks: list[bytes] = []
            total = 0
            if clock() > deadline:
                log.warning("art fetch: deadline exceeded")
                return None
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > cfg.max_download_bytes:
                    log.warning("art fetch: body over the cap")
                    return None
                chunks.append(chunk)
                if clock() > deadline:
                    log.warning("art fetch: deadline exceeded")
                    return None
            data = b"".join(chunks)
            return data or None
    except (
        urllib.error.URLError,  # includes HTTPError
        OSError,  # socket.timeout, ssl.SSLError
        ValueError,
        http.client.HTTPException,
    ) as exc:
        log.warning("art fetch failed: %s", type(exc).__name__)
        return None


def read_local(path: str, cfg: CoverConfig) -> bytes | None:
    """Read an already-classified local file, or None.

    O_NOFOLLOW refuses a symlink as the last component, O_NONBLOCK keeps a FIFO from
    hanging the open, and the descriptor is checked with fstat (regular file, size cap)
    before reading at most max_local_bytes + 1 bytes.

    Known gap, accepted: a TOCTOU window exists between classify_art_url's realpath and
    this open (a directory component could be swapped for a symlink in between). The
    allow-listed directory is owned by the user running the daemon.
    """
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > cfg.max_local_bytes:
            log.warning("art file: not a regular file or over the cap")
            return None
        limit = cfg.max_local_bytes + 1
        chunks: list[bytes] = []
        total = 0
        while total < limit:
            chunk = os.read(fd, min(_CHUNK, limit - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > cfg.max_local_bytes:
            log.warning("art file: over the cap")
            return None
        return b"".join(chunks) or None
    except OSError as exc:
        log.warning("art file unreadable: %s", type(exc).__name__)
        return None
    finally:
        if fd >= 0:
            os.close(fd)


def sniff_image(data: bytes) -> str | None:
    """ "jpeg" or "png" by magic bytes; nothing else is accepted."""
    if data.startswith(_JPEG_MAGIC):
        return "jpeg"
    if data.startswith(_PNG_MAGIC):
        return "png"
    return None


def resize_to_jpeg(
    data: bytes, cfg: CoverConfig, run: Callable[..., Any] | None = None
) -> bytes | None:
    """ImageMagick: crop to fill size x size, flatten alpha on white, JPEG at cfg.quality.

    Only the size x size box in the middle is the cover. If the canvas is larger than that
    box, the sides (and top/bottom) are a flat grey, `fill_grey`, applied after alpha is
    flattened. The SDK's builder caps `size` at 240 (default); a 480x480 cover (size 480 on
    a 480x480 canvas, no grey) was drawn by the screen in Stage 9n but loaded offset, so
    that case is INFERRED to behave.

    CHANGED from the earlier stretch (`-resize SxS!`): the source is now scaled to cover
    the box and the overflow is cut off at the centre (`-resize SxS^ -gravity center
    -extent SxS`), for every source. A square source looks the same; a non-square source
    is cropped instead of squashed.

    The explicit `png:`/`jpeg:` coder prefix (from the magic bytes) keeps delegates,
    SVG/MVG and URL handlers from ever being selected. Returns None without running
    anything if the data is neither PNG nor JPEG.
    """
    fmt = sniff_image(data)
    if fmt is None:
        log.warning("art resize: input is not PNG or JPEG")
        return None
    s = cfg.size
    argv = [
        cfg.magick_path,
        "-limit",
        "memory",
        "64MiB",
        "-limit",
        "map",
        "64MiB",
        "-limit",
        "disk",
        "0",
        "-limit",
        "time",
        "5",
        f"{fmt}:-",
        "-background",
        "white",
        "-alpha",
        "remove",
        "-alpha",
        "off",
        "-resize",
        f"{s}x{s}^",
        "-gravity",
        "center",
        "-extent",
        f"{s}x{s}",
    ]
    if (cfg.canvas_width, cfg.canvas_height) != (s, s):
        g = cfg.fill_grey
        argv += [
            "-background",
            f"#{g:02x}{g:02x}{g:02x}",
            "-extent",
            f"{cfg.canvas_width}x{cfg.canvas_height}",
        ]
    argv += ["-strip", "-quality", str(cfg.quality), "jpeg:-"]
    runner = run or subprocess.run
    try:
        res = runner(
            argv,
            input=data,
            capture_output=True,
            timeout=cfg.magick_timeout_s,
            # Scrubbed on purpose: no other inherited environment. magick spools stdin to a
            # `magick-*` temp file and removes it at exit; MAGICK_TMPDIR keeps it in the same
            # TMPDIR this process already uses instead of a hard-coded /tmp.
            env={"LC_ALL": "C", "MAGICK_TMPDIR": tempfile.gettempdir()},
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.warning("art resize failed: %s", type(exc).__name__)
        return None
    out = res.stdout
    if res.returncode != 0:
        log.warning("art resize: magick exit code %s", res.returncode)
        return None
    if not isinstance(out, (bytes, bytearray)) or not out.startswith(_JPEG_MAGIC):
        log.warning("art resize: output is not a JPEG")
        return None
    if not 0 < len(out) <= cfg.max_output_bytes:
        log.warning("art resize: output size %d outside the cap", len(out))
        return None
    return bytes(out)


class ArtCache:
    """Thread-safe bounded LRU of JPEG bytes keyed by (art_url, size). Failures are not stored."""

    def __init__(self, capacity: int) -> None:
        if not _is_int(capacity) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self._cap = capacity
        self._lock = threading.Lock()
        self._items: collections.OrderedDict[tuple[str, int], bytes] = collections.OrderedDict()

    def get(self, key: tuple[str, int]) -> bytes | None:
        with self._lock:
            val = self._items.get(key)
            if val is not None:
                self._items.move_to_end(key)
            return val

    def put(self, key: tuple[str, int], value: bytes) -> None:
        if not isinstance(value, (bytes, bytearray)):
            raise TypeError("only bytes can be cached")
        with self._lock:
            self._items[key] = bytes(value)
            self._items.move_to_end(key)
            while len(self._items) > self._cap:
                self._items.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


def get_jpeg(
    art_url: str,
    cfg: CoverConfig,
    cache: ArtCache | None = None,
    *,
    open_url: Callable[[urllib.request.Request, float], Any] | None = None,
    run: Callable[..., Any] | None = None,
) -> bytes | None:
    """The entry point: art URL -> JPEG bytes, or None. Never raises."""
    if art_url == "":
        return None
    try:
        key = (art_url, cfg.size)
        if cache is not None:
            hit = cache.get(key)
            if hit is not None:
                return hit
        kind = classify_art_url(art_url, cfg)
        if kind is None:
            return None
        raw = (
            fetch_https(kind[1], cfg, open_url) if kind[0] == "https" else read_local(kind[1], cfg)
        )
        if raw is None:
            return None
        jpeg = resize_to_jpeg(raw, cfg, run)
        if jpeg is None:
            return None
        if cache is not None:
            cache.put(key, jpeg)
        return jpeg
    except Exception:  # a bug here must never stop the caller
        log.exception("unexpected error building cover art")
        return None
