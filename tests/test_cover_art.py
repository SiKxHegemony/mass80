"""T21: the cover-art pipeline. No network, no real files outside a TemporaryDirectory, and
real magick only in the optional integration class at the bottom."""

import http.client
import io
import os
import ssl
import struct
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zlib
from types import SimpleNamespace

from mass80d import cover_art
from mass80d.cover_art import (
    ArtCache,
    CoverConfig,
    _NoRedirect,
    classify_art_url,
    fetch_https,
    get_jpeg,
    read_local,
    resize_to_jpeg,
    sniff_image,
)

JPEG = b"\xff\xd8\xff\xe0" + b"j" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 20
SPOTIFY = "https://i.scdn.co/image/ab67616d0000b273abcdef"
FIREFOX = "file:///home/shane/.zen/firefox-mpris/4003_21.png"


class FakeResp:
    def __init__(self, body=b"", status=200, headers=None, url=SPOTIFY):
        self.status = status
        self.headers = headers if headers is not None else {}
        self._buf = io.BytesIO(body)
        self._url = url
        self.reads = 0

    def read(self, n=-1):
        self.reads += 1
        return self._buf.read(n)

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class EndlessResp(FakeResp):
    def read(self, n=-1):
        self.reads += 1
        if self.reads > 10_000:
            raise AssertionError("read the stream without bound")
        return b"x" * (n if n > 0 else 65536)


class Opener:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def __call__(self, req, timeout):
        self.calls.append((req, timeout))
        if self.exc is not None:
            raise self.exc
        return self.resp


class FakeRun:
    def __init__(self, returncode=0, stdout=JPEG, exc=None):
        self.returncode, self.stdout, self.exc, self.calls = returncode, stdout, exc, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr=b"")


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        c = CoverConfig()
        self.assertEqual(c.hosts, ("i.scdn.co",))
        self.assertEqual(c.local_dirs, ())
        self.assertEqual((c.size, c.quality, c.cache_size), (480, 60, 8))
        self.assertEqual(c.fetch_timeout_s, 3.0)
        self.assertEqual(
            (c.max_download_bytes, c.max_local_bytes, c.max_output_bytes),
            (2_000_000, 5_000_000, 200_000),
        )
        self.assertEqual((c.magick_path, c.magick_timeout_s), ("/usr/bin/magick", 5.0))
        self.assertEqual((c.canvas_width, c.canvas_height, c.fill_grey), (480, 480, 32))

    def test_bad_canvas_and_grey_raise(self):
        bad = [
            {"canvas_width": 0},
            {"canvas_width": 481},
            {"canvas_width": True},
            {"canvas_width": 480.0},
            {"canvas_height": 0},
            {"canvas_height": 481},
            {"canvas_height": True},
            {"canvas_height": 240.0},
            {"canvas_width": 479},  # the 480 default size no longer fits
            {"canvas_height": 479},
            {"size": 481},
            {"size": 240, "canvas_width": 239},
            {"size": 240, "canvas_height": 239},
            {"size": 100, "canvas_width": 99},
            {"fill_grey": -1},
            {"fill_grey": 256},
            {"fill_grey": True},
            {"fill_grey": 32.0},
            {"fill_grey": "32"},
        ]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                CoverConfig(**kw)

    def test_size_480_bounds(self):
        for bad in (481, 0, True, 480.0):
            with self.subTest(size=bad), self.assertRaises(ValueError):
                CoverConfig(size=bad, canvas_width=480, canvas_height=480)
        c = CoverConfig(size=480, canvas_width=480, canvas_height=480)
        self.assertEqual((c.size, c.canvas_width, c.canvas_height), (480, 480, 480))
        self.assertEqual(CoverConfig(size=240).size, 240)
        self.assertEqual(CoverConfig(size=480).size, 480)  # fits the 480x480 default canvas
        with self.assertRaises(ValueError):  # larger than an explicit 240 canvas
            CoverConfig(size=480, canvas_width=240, canvas_height=240)
        with self.assertRaises(ValueError):
            CoverConfig(size=480, canvas_width=479)
        with self.assertRaises(ValueError):
            CoverConfig(size=480, canvas_height=479)

    def test_default_is_480_and_size_240_is_valid_on_it(self):
        c = CoverConfig()
        self.assertEqual((c.size, c.canvas_width, c.canvas_height), (480, 480, 480))
        c240 = CoverConfig(size=240)  # size <= canvas: valid, grey fills around the box
        self.assertEqual((c240.size, c240.canvas_width, c240.canvas_height), (240, 480, 480))
        sq = CoverConfig(size=240, canvas_width=240, canvas_height=240)
        self.assertEqual((sq.size, sq.canvas_width, sq.canvas_height), (240, 240, 240))
        with self.assertRaises(ValueError):
            CoverConfig(size=481)
        with self.assertRaises(ValueError):
            CoverConfig(canvas_width=479)
        with self.assertRaises(ValueError):
            CoverConfig(canvas_height=479)

    def test_canvas_and_grey_bounds_accepted(self):
        CoverConfig(size=240, canvas_width=480, canvas_height=240, fill_grey=0)
        CoverConfig(size=1, canvas_width=1, canvas_height=1, fill_grey=255)
        CoverConfig(size=240, canvas_width=240, canvas_height=480)

    def test_bad_values_raise(self):
        bad = [
            {"size": 0},
            {"size": 481},
            {"quality": 0},
            {"quality": 101},
            {"fetch_timeout_s": 0},
            {"fetch_timeout_s": 11},
            {"local_dirs": ("relative/dir",)},
            {"magick_path": "magick"},
            {"cache_size": 0},
            {"cache_size": 65},
            {"size": True},
            {"quality": True},
            {"cache_size": True},
            {"fetch_timeout_s": True},
            {"magick_timeout_s": True},
            {"max_download_bytes": True},
            {"max_local_bytes": True},
            {"max_output_bytes": True},
            {"max_download_bytes": 0},
            {"magick_timeout_s": 0},
        ]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                CoverConfig(**kw)

    def test_bounds_accepted(self):
        CoverConfig(size=1, quality=1, cache_size=1, fetch_timeout_s=10)
        CoverConfig(size=240, quality=100, cache_size=64)


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.art = os.path.join(self.root, "art")
        os.mkdir(self.art)
        self.cfg = CoverConfig(local_dirs=(self.art,))

    def test_spotify_accepted(self):
        self.assertEqual(classify_art_url(SPOTIFY, CoverConfig()), ("https", SPOTIFY))

    def test_host_case_insensitive(self):
        url = "https://I.SCDN.CO/x"
        self.assertEqual(classify_art_url(url, CoverConfig()), ("https", url))

    def test_scheme_case_insensitive(self):
        url = "HTTPS://i.scdn.co/x"
        self.assertEqual(classify_art_url(url, CoverConfig()), ("https", url))

    def test_port_443_accepted(self):
        url = "https://i.scdn.co:443/x"
        self.assertEqual(classify_art_url(url, CoverConfig()), ("https", url))

    def test_refused_urls(self):
        cfg = CoverConfig()
        bad = [
            "http://i.scdn.co/x",
            "https://i.scdn.co.evil.com/x",
            "https://evil.com/i.scdn.co",
            "https://i.scdn.co@evil.com/x",
            "https://evil.com#@i.scdn.co",
            "https://user:pw@i.scdn.co/x",
            "https://user@i.scdn.co/x",
            "https://i.scdn.co:8443/x",
            "https://i.scdn.co:80/x",
            "https://i.scdn.co:bad/x",
            "file:///etc/passwd",
            "file:///tmp/../etc/passwd",
            "file://evil/share/x.png",
            "https://i.scdn.co/x\x00y",
            "https://i.scdn.co/x y",
            "https://i.scdn.co/x\ny",
            "https://i.scdn.co/x\ty",
            "https://i.scdn.co/\u00e9",
            "https://i.scdn.co/" + "a" * 3000,
            "data:image/png;base64,AAAA",
            "javascript:alert(1)",
            "ftp://i.scdn.co/x",
            "",
            "i.scdn.co/x",
            "//i.scdn.co/x",
            "https:///x",
        ]
        for url in bad:
            with self.subTest(url=url[:60]):
                self.assertIsNone(classify_art_url(url, cfg))

    def test_length_limit_boundary(self):
        pad = "a" * (2048 - len("https://i.scdn.co/"))
        ok = "https://i.scdn.co/" + pad
        self.assertEqual(len(ok), 2048)
        self.assertEqual(classify_art_url(ok, CoverConfig()), ("https", ok))
        self.assertIsNone(classify_art_url(ok + "a", CoverConfig()))

    def test_non_str_is_refused(self):
        self.assertIsNone(classify_art_url(None, CoverConfig()))  # type: ignore[arg-type]

    def test_custom_hosts_exact(self):
        cfg = CoverConfig(hosts=("a.example", "b.example"))
        self.assertIsNotNone(classify_art_url("https://b.example/x", cfg))
        self.assertIsNone(classify_art_url(SPOTIFY, cfg))

    def test_file_url_refused_when_local_dirs_empty(self):
        self.assertIsNone(classify_art_url(FIREFOX, CoverConfig()))
        p = os.path.join(self.art, "a.png")
        self.assertIsNone(classify_art_url("file://" + p, CoverConfig()))

    def test_firefox_shape_accepted_when_dir_configured(self):
        # The path is only a string here; classify resolves it with realpath, which does
        # not need the file to exist.
        d = os.path.join(self.root, "firefox-mpris")
        os.mkdir(d)
        cfg = CoverConfig(local_dirs=(d,))
        r = classify_art_url("file://" + os.path.join(d, "4003_21.png"), cfg)
        self.assertEqual(r, ("file", os.path.join(d, "4003_21.png")))

    def test_file_in_allowed_dir_accepted_with_empty_and_localhost_netloc(self):
        p = os.path.join(self.art, "a.png")
        for prefix in ("file://", "file://localhost", "FILE://"):
            with self.subTest(prefix=prefix):
                self.assertEqual(classify_art_url(prefix + p, self.cfg), ("file", p))

    def test_percent_decoded(self):
        p = os.path.join(self.art, "a b.png")
        url = "file://" + os.path.join(self.art, "a%20b.png")
        self.assertEqual(classify_art_url(url, self.cfg), ("file", p))

    def test_percent_encoded_dotdot_and_nul_refused(self):
        self.assertIsNone(
            classify_art_url("file://" + self.art + "/%2e%2e/%2e%2e/etc/passwd", self.cfg)
        )
        self.assertIsNone(classify_art_url("file://" + self.art + "/a%00.png", self.cfg))

    def test_dotdot_escape_refused(self):
        self.assertIsNone(classify_art_url("file://" + self.art + "/../x.png", self.cfg))

    def test_dir_itself_refused(self):
        self.assertIsNone(classify_art_url("file://" + self.art, self.cfg))
        self.assertIsNone(classify_art_url("file://" + self.art + "/", self.cfg))

    def test_sibling_with_same_prefix_refused(self):
        sib = self.art + "2"
        os.mkdir(sib)
        self.assertIsNone(classify_art_url("file://" + os.path.join(sib, "a.png"), self.cfg))

    def test_symlink_leaving_dir_refused(self):
        outside = os.path.join(self.root, "secret.png")
        with open(outside, "wb") as f:
            f.write(PNG)
        link = os.path.join(self.art, "link.png")
        os.symlink(outside, link)
        self.assertIsNone(classify_art_url("file://" + link, self.cfg))

    def test_symlink_staying_inside_is_resolved(self):
        real = os.path.join(self.art, "real.png")
        with open(real, "wb") as f:
            f.write(PNG)
        link = os.path.join(self.art, "link.png")
        os.symlink(real, link)
        self.assertEqual(classify_art_url("file://" + link, self.cfg), ("file", real))

    def test_configured_dir_that_is_a_symlink_is_realpathed(self):
        link_dir = os.path.join(self.root, "linkdir")
        os.symlink(self.art, link_dir)
        cfg = CoverConfig(local_dirs=(link_dir,))
        p = os.path.join(self.art, "a.png")
        self.assertEqual(classify_art_url("file://" + p, cfg), ("file", p))

    def test_relative_path_in_file_url_refused(self):
        self.assertIsNone(classify_art_url("file:art/a.png", self.cfg))


class FetchTest(unittest.TestCase):
    cfg = CoverConfig()

    def test_ok(self):
        op = Opener(FakeResp(b"abc", headers={"Content-Length": "3"}))
        self.assertEqual(fetch_https(SPOTIFY, self.cfg, op), b"abc")
        self.assertEqual(len(op.calls), 1)

    def test_request_shape(self):
        op = Opener(FakeResp(b"abc"))
        fetch_https(SPOTIFY, self.cfg, op)
        req, timeout = op.calls[0]
        self.assertEqual(req.full_url, SPOTIFY)
        self.assertEqual(req.get_method(), "GET")
        self.assertIsNone(req.data)
        self.assertEqual(timeout, self.cfg.fetch_timeout_s)
        self.assertEqual({k.lower(): v for k, v in req.header_items()}, {"user-agent": "mass80d"})

    def test_non_200_is_none(self):
        self.assertIsNone(fetch_https(SPOTIFY, self.cfg, Opener(FakeResp(b"abc", status=204))))
        self.assertIsNone(fetch_https(SPOTIFY, self.cfg, Opener(FakeResp(b"abc", status=206))))

    def test_http_errors_are_none(self):
        for code in (404, 302, 500):
            with self.subTest(code=code):
                exc = urllib.error.HTTPError(SPOTIFY, code, "x", {}, io.BytesIO(b""))  # type: ignore[arg-type]
                self.addCleanup(exc.close)
                self.assertIsNone(fetch_https(SPOTIFY, self.cfg, Opener(exc=exc)))

    def test_other_exceptions_are_none(self):
        excs = [
            urllib.error.URLError("boom"),
            TimeoutError("t"),  # socket.timeout is this class on Python 3.10+
            ssl.SSLError("bad cert"),
            OSError("x"),
            ValueError("x"),
            http.client.HTTPException("x"),
            http.client.IncompleteRead(b"a"),
        ]
        for exc in excs:
            with self.subTest(exc=type(exc).__name__):
                self.assertIsNone(fetch_https(SPOTIFY, self.cfg, Opener(exc=exc)))

    def test_failure_log_has_class_name_not_url_or_message(self):
        exc = urllib.error.URLError("secret-text " + SPOTIFY)
        with self.assertLogs("mass80d.cover_art", level="DEBUG") as cm:
            fetch_https(SPOTIFY, self.cfg, Opener(exc=exc))
        text = "\n".join(cm.output)
        self.assertIn("URLError", text)
        self.assertNotIn("secret-text", text)
        self.assertNotIn("scdn", text)

    def test_final_url_on_other_host_is_none(self):
        resp = FakeResp(b"abc", url="https://evil.com/x")
        self.assertIsNone(fetch_https(SPOTIFY, self.cfg, Opener(resp)))

    def test_final_url_on_non_https_is_none(self):
        resp = FakeResp(b"abc", url="http://i.scdn.co/x")
        self.assertIsNone(fetch_https(SPOTIFY, self.cfg, Opener(resp)))

    def test_content_length_over_cap_does_not_read(self):
        resp = FakeResp(b"abc", headers={"Content-Length": str(self.cfg.max_download_bytes + 1)})
        self.assertIsNone(fetch_https(SPOTIFY, self.cfg, Opener(resp)))
        self.assertEqual(resp.reads, 0)

    def test_content_length_at_cap_is_allowed(self):
        cfg = CoverConfig(max_download_bytes=10)
        resp = FakeResp(b"x" * 10, headers={"Content-Length": "10"})
        self.assertEqual(fetch_https(SPOTIFY, cfg, Opener(resp)), b"x" * 10)

    def test_garbage_content_length_falls_back_to_chunked_cap(self):
        cfg = CoverConfig(max_download_bytes=10)
        resp = FakeResp(b"x" * 50, headers={"Content-Length": "abc"})
        self.assertIsNone(fetch_https(SPOTIFY, cfg, Opener(resp)))

    def test_body_over_cap_without_content_length_stops_reading(self):
        resp = EndlessResp()
        self.assertIsNone(fetch_https(SPOTIFY, self.cfg, Opener(resp)))
        # 2 MB cap, so a bounded number of reads (chunk size is the implementation's)
        self.assertLess(resp.reads, 1000)
        self.assertGreater(resp.reads, 0)

    def test_body_exactly_at_cap_ok_one_over_not(self):
        cfg = CoverConfig(max_download_bytes=100)
        self.assertEqual(fetch_https(SPOTIFY, cfg, Opener(FakeResp(b"x" * 100))), b"x" * 100)
        self.assertIsNone(fetch_https(SPOTIFY, cfg, Opener(FakeResp(b"x" * 101))))

    def test_multi_chunk_body_is_joined(self):
        body = bytes(range(256)) * 1000  # 256000 bytes, more than one chunk
        self.assertEqual(fetch_https(SPOTIFY, self.cfg, Opener(FakeResp(body))), body)

    def test_empty_body_is_none(self):
        self.assertIsNone(fetch_https(SPOTIFY, self.cfg, Opener(FakeResp(b""))))


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class DripResp(FakeResp):
    """Every read advances the fake clock by `step` and returns 1 KB (up to `total` reads)."""

    def __init__(self, clock, step, total=20):
        super().__init__(b"")
        self.clock, self.step, self.total = clock, step, total

    def read(self, n=-1):
        self.reads += 1
        if self.reads > self.total:
            return b""
        self.clock.now += self.step
        return b"d" * 1024


class FetchDeadlineTest(unittest.TestCase):
    def test_slow_drip_hits_total_deadline(self):
        clock = FakeClock()
        resp = DripResp(clock, 0.25)
        cfg = CoverConfig(fetch_timeout_s=1.0)
        with self.assertLogs("mass80d.cover_art", level="WARNING") as cm:
            out = fetch_https(SPOTIFY, cfg, Opener(resp), clock=clock)
        self.assertIsNone(out)
        self.assertLessEqual(resp.reads, 5)
        self.assertTrue(any("art fetch: deadline exceeded" in m for m in cm.output))

    def test_fast_enough_returns_bytes(self):
        clock = FakeClock()
        resp = DripResp(clock, 0.01)
        cfg = CoverConfig(fetch_timeout_s=1.0)
        out = fetch_https(SPOTIFY, cfg, Opener(resp), clock=clock)
        self.assertEqual(out, b"d" * 1024 * 20)

    def test_first_read_already_over_deadline(self):
        clock = FakeClock()
        resp = DripResp(clock, 5.0)
        cfg = CoverConfig(fetch_timeout_s=1.0)
        with self.assertLogs("mass80d.cover_art", level="WARNING") as cm:
            out = fetch_https(SPOTIFY, cfg, Opener(resp), clock=clock)
        self.assertIsNone(out)
        self.assertEqual(resp.reads, 1)
        self.assertTrue(any("deadline exceeded" in m for m in cm.output))

    def test_deadline_taken_before_open(self):
        clock = FakeClock()

        def slow_open(req, timeout):
            clock.now += 5.0  # opening itself eats the whole budget
            return DripResp(clock, 0.0, total=1)

        cfg = CoverConfig(fetch_timeout_s=1.0)
        with self.assertLogs("mass80d.cover_art", level="WARNING"):
            self.assertIsNone(fetch_https(SPOTIFY, cfg, slow_open, clock=clock))


class OpenerTest(unittest.TestCase):
    def test_no_redirect_returns_none(self):
        req = urllib.request.Request(SPOTIFY)
        r = _NoRedirect().redirect_request(req, None, 302, "Found", {}, "https://evil.com/x")
        self.assertIsNone(r)

    def test_redirect_response_raises_http_error(self):
        # The real opener chain, no network: a 302 reaches _NoRedirect, which declines, and
        # the default error handler raises HTTPError.
        opener = cover_art._build_opener()
        req = urllib.request.Request(SPOTIFY)
        hdrs = http.client.HTTPMessage()
        hdrs["Location"] = "https://evil.com/x"
        with self.assertRaises(urllib.error.HTTPError) as cm:
            opener.error("https", req, io.BytesIO(b""), 302, "Found", hdrs)
        self.assertEqual(cm.exception.code, 302)
        cm.exception.close()

    def test_build_opener_handlers(self):
        from unittest import mock

        seen = []

        class SpyProxyHandler(urllib.request.ProxyHandler):
            def __init__(self, proxies=None):
                seen.append(proxies)
                super().__init__(proxies)

        with mock.patch.object(urllib.request, "ProxyHandler", SpyProxyHandler):
            opener = cover_art._build_opener()
        # ProxyHandler({}) has no protocol methods, so the opener does not even list it;
        # what matters is that it was built with no proxies and no env-proxy handler is left.
        self.assertEqual(seen, [{}])
        self.assertTrue(any(isinstance(h, _NoRedirect) for h in opener.handlers))
        self.assertEqual(
            [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)], []
        )
        self.assertFalse(
            any(type(h) is urllib.request.HTTPRedirectHandler for h in opener.handlers)
        )
        self.assertFalse(
            any(isinstance(h, urllib.request.HTTPCookieProcessor) for h in opener.handlers)
        )

    def test_https_handler_uses_default_verification(self):
        opener = cover_art._build_opener()
        hh = [h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler)]
        self.assertEqual(len(hh), 1)
        ctx = hh[0]._context  # type: ignore[attr-defined]
        if ctx is not None:
            self.assertTrue(ctx.check_hostname)
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)


class ReadLocalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        self.art = os.path.join(self.root, "art")
        os.mkdir(self.art)
        self.cfg = CoverConfig(local_dirs=(self.art,), max_local_bytes=100)

    def write(self, name, data):
        p = os.path.join(self.art, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_regular_file(self):
        p = self.write("a.png", PNG)
        self.assertEqual(read_local(p, self.cfg), PNG)

    def test_exactly_at_cap(self):
        p = self.write("a.png", b"x" * 100)
        self.assertEqual(read_local(p, self.cfg), b"x" * 100)

    def test_over_cap(self):
        p = self.write("a.png", b"x" * 101)
        self.assertIsNone(read_local(p, self.cfg))

    def test_empty_file_is_none(self):
        p = self.write("a.png", b"")
        self.assertIsNone(read_local(p, self.cfg))

    def test_symlink_refused_by_nofollow(self):
        outside = os.path.join(self.root, "secret.png")
        with open(outside, "wb") as f:
            f.write(PNG)
        link = os.path.join(self.art, "link.png")
        os.symlink(outside, link)
        self.assertIsNone(read_local(link, self.cfg))
        self.assertIsNone(classify_art_url("file://" + link, self.cfg))

    def test_fifo_does_not_hang(self):
        fifo = os.path.join(self.art, "pipe.png")
        os.mkfifo(fifo)
        result = []
        t = threading.Thread(target=lambda: result.append(read_local(fifo, self.cfg)))
        t.daemon = True
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "read_local hung on a FIFO")
        self.assertEqual(result, [None])

    def test_directory_is_none(self):
        d = os.path.join(self.art, "sub")
        os.mkdir(d)
        self.assertIsNone(read_local(d, self.cfg))

    def test_vanished_file_is_none(self):
        self.assertIsNone(read_local(os.path.join(self.art, "gone.png"), self.cfg))

    def test_descriptor_closed(self):
        p = self.write("a.png", PNG)
        before = set(os.listdir("/proc/self/fd"))
        for _ in range(20):
            read_local(p, self.cfg)
            read_local(os.path.join(self.art, "gone.png"), self.cfg)
        self.assertLessEqual(len(set(os.listdir("/proc/self/fd")) - before), 1)


class SniffTest(unittest.TestCase):
    def test_jpeg_and_png(self):
        self.assertEqual(sniff_image(b"\xff\xd8\xff"), "jpeg")
        self.assertEqual(sniff_image(JPEG), "jpeg")
        self.assertEqual(sniff_image(PNG), "png")
        self.assertEqual(sniff_image(b"\x89PNG\r\n\x1a\n"), "png")

    def test_negatives(self):
        for data in (
            b"GIF89a....",
            b"GIF87a....",
            b"RIFF\x00\x00\x00\x00WEBPVP8 ",
            b"<svg xmlns='http://www.w3.org/2000/svg'/>",
            b"<?xml version='1.0'?><svg/>",
            b"push graphic-context",
            b"",
            b"\xff",
            b"\xff\xd8",
            b"\x89PNG",
            b"\x89PNG\r\n\x1a",
            b"%PDF-1.7",
            b"II*\x00....",
            b"MM\x00*....",
            b"BM....",
        ):
            with self.subTest(data=data[:12]):
                self.assertIsNone(sniff_image(data))


def expected_argv(
    fmt="png", size=480, quality=60, magick="/usr/bin/magick", canvas=(480, 480), grey=32
):
    argv = [
        magick,
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
        f"{size}x{size}^",
        "-gravity",
        "center",
        "-extent",
        f"{size}x{size}",
    ]
    if tuple(canvas) != (size, size):
        argv += [
            "-background",
            f"#{grey:02x}{grey:02x}{grey:02x}",
            "-extent",
            f"{canvas[0]}x{canvas[1]}",
        ]
    argv += ["-strip", "-quality", str(quality), "jpeg:-"]
    return argv


class ResizeTest(unittest.TestCase):
    cfg = CoverConfig()

    def test_exact_call(self):
        run = FakeRun()
        self.assertEqual(resize_to_jpeg(PNG, self.cfg, run), JPEG)
        argv, kw = run.calls[0]
        self.assertEqual(argv, expected_argv("png"))
        self.assertFalse(kw.get("shell", False))
        self.assertEqual(kw["input"], PNG)
        self.assertTrue(kw["capture_output"])
        self.assertEqual(kw["timeout"], self.cfg.magick_timeout_s)
        self.assertEqual(kw["env"], {"LC_ALL": "C", "MAGICK_TMPDIR": tempfile.gettempdir()})
        self.assertIs(kw["check"], False)
        self.assertEqual(len(run.calls), 1)

    def test_jpeg_input_uses_jpeg_coder(self):
        run = FakeRun()
        resize_to_jpeg(JPEG, self.cfg, run)
        self.assertEqual(run.calls[0][0], expected_argv("jpeg"))

    def test_config_values_flow_into_argv(self):
        cfg = CoverConfig(size=100, quality=33, magick_path="/opt/m/magick", magick_timeout_s=2.5)
        run = FakeRun()
        resize_to_jpeg(PNG, cfg, run)
        self.assertEqual(run.calls[0][0], expected_argv("png", 100, 33, "/opt/m/magick"))
        self.assertEqual(run.calls[0][1]["timeout"], 2.5)

    def test_canvas_argv_exact(self):
        cfg = CoverConfig(size=240, canvas_width=480, canvas_height=240, fill_grey=32)
        run = FakeRun()
        resize_to_jpeg(PNG, cfg, run)
        argv = run.calls[0][0]
        self.assertEqual(argv, expected_argv("png", size=240, canvas=(480, 240), grey=32))
        i = argv.index("-extent")
        self.assertEqual(
            argv[i : i + 8],
            [
                "-extent",
                "240x240",
                "-background",
                "#202020",
                "-extent",
                "480x240",
                "-strip",
                "-quality",
            ],
        )

    def test_size_480_argv_exact_no_grey_fill(self):
        cfg = CoverConfig(size=480, canvas_width=480, canvas_height=480)
        run = FakeRun()
        resize_to_jpeg(PNG, cfg, run)
        argv = run.calls[0][0]
        self.assertEqual(argv, expected_argv("png", size=480, canvas=(480, 480)))
        i = argv.index("-resize")
        self.assertEqual(
            argv[i : i + 6],
            ["-resize", "480x480^", "-gravity", "center", "-extent", "480x480"],
        )
        self.assertEqual(argv.count("-extent"), 1)
        self.assertEqual(argv.count("-background"), 1)

    def test_default_canvas_has_one_extent_and_no_grey(self):
        run = FakeRun()
        resize_to_jpeg(PNG, self.cfg, run)
        argv = run.calls[0][0]
        self.assertEqual(argv.count("-extent"), 1)
        self.assertEqual(argv.count("-background"), 1)
        self.assertNotIn("-resize 240x240!", " ".join(argv))
        self.assertNotIn("240x240!", argv)

    def test_grey_values_hex(self):
        for g, hexs in ((0, "#000000"), (255, "#ffffff"), (16, "#101010"), (32, "#202020")):
            with self.subTest(g=g):
                run = FakeRun()
                resize_to_jpeg(
                    PNG,
                    CoverConfig(size=240, canvas_width=480, canvas_height=240, fill_grey=g),
                    run,
                )
                argv = run.calls[0][0]
                self.assertEqual(argv, expected_argv("png", size=240, canvas=(480, 240), grey=g))
                self.assertEqual(argv[-8:-4], ["-background", hexs, "-extent", "480x240"])

    def test_canvas_values_flow_with_custom_size(self):
        cfg = CoverConfig(size=100, canvas_width=300, canvas_height=150, fill_grey=77)
        run = FakeRun()
        resize_to_jpeg(PNG, cfg, run)
        self.assertEqual(
            run.calls[0][0], expected_argv("png", size=100, canvas=(300, 150), grey=77)
        )
        self.assertIn("#4d4d4d", run.calls[0][0])
        self.assertIn("300x150", run.calls[0][0])

    def test_unsniffable_input_never_runs(self):
        for data in (b"<svg/>", b"push graphic-context", b"", b"GIF89a"):
            run = FakeRun()
            self.assertIsNone(resize_to_jpeg(data, self.cfg, run))
            self.assertEqual(run.calls, [])

    def assert_fails_with_warning(self, run, secret=PNG):
        with self.assertLogs("mass80d.cover_art", level="WARNING") as cm:
            self.assertIsNone(resize_to_jpeg(secret, self.cfg, run))
        text = "\n".join(cm.output)
        self.assertNotIn(repr(secret), text)
        self.assertNotIn("p" * 20, text)
        return text

    def test_nonzero_exit(self):
        text = self.assert_fails_with_warning(FakeRun(returncode=1, stdout=b""))
        self.assertIn("1", text)

    def test_nonzero_exit_even_with_jpeg_stdout(self):
        self.assert_fails_with_warning(FakeRun(returncode=1, stdout=JPEG))

    def test_stdout_without_magic(self):
        self.assert_fails_with_warning(FakeRun(stdout=b"notajpeg"))

    def test_output_too_large(self):
        big = b"\xff\xd8\xff" + b"j" * self.cfg.max_output_bytes
        self.assertEqual(len(big), self.cfg.max_output_bytes + 3)
        self.assert_fails_with_warning(FakeRun(stdout=big))

    def test_output_exactly_at_cap_ok(self):
        ok = b"\xff\xd8\xff" + b"j" * (self.cfg.max_output_bytes - 3)
        self.assertEqual(resize_to_jpeg(PNG, self.cfg, FakeRun(stdout=ok)), ok)

    def test_empty_output(self):
        self.assert_fails_with_warning(FakeRun(stdout=b""))

    def test_none_output(self):
        self.assert_fails_with_warning(FakeRun(stdout=None))

    def test_timeout(self):
        text = self.assert_fails_with_warning(
            FakeRun(exc=subprocess.TimeoutExpired(["magick"], 5.0))
        )
        self.assertIn("TimeoutExpired", text)

    def test_file_not_found(self):
        text = self.assert_fails_with_warning(FakeRun(exc=FileNotFoundError("magick")))
        self.assertIn("FileNotFoundError", text)

    def test_oserror(self):
        text = self.assert_fails_with_warning(FakeRun(exc=OSError("x")))
        self.assertIn("OSError", text)


class CacheTest(unittest.TestCase):
    def test_get_put_len_clear(self):
        c = ArtCache(2)
        self.assertIsNone(c.get(("a", 240)))
        c.put(("a", 240), b"1")
        self.assertEqual(c.get(("a", 240)), b"1")
        self.assertEqual(len(c), 1)
        c.clear()
        self.assertEqual(len(c), 0)
        self.assertIsNone(c.get(("a", 240)))

    def test_lru_eviction_order(self):
        c = ArtCache(2)
        c.put(("a", 1), b"a")
        c.put(("b", 1), b"b")
        c.put(("c", 1), b"c")
        self.assertIsNone(c.get(("a", 1)))
        self.assertEqual(c.get(("b", 1)), b"b")
        self.assertEqual(c.get(("c", 1)), b"c")
        self.assertEqual(len(c), 2)

    def test_get_refreshes_recency(self):
        c = ArtCache(2)
        c.put(("a", 1), b"a")
        c.put(("b", 1), b"b")
        c.get(("a", 1))
        c.put(("c", 1), b"c")
        self.assertEqual(c.get(("a", 1)), b"a")
        self.assertIsNone(c.get(("b", 1)))

    def test_put_existing_refreshes_and_replaces(self):
        c = ArtCache(2)
        c.put(("a", 1), b"a")
        c.put(("b", 1), b"b")
        c.put(("a", 1), b"a2")
        c.put(("c", 1), b"c")
        self.assertEqual(c.get(("a", 1)), b"a2")
        self.assertIsNone(c.get(("b", 1)))
        self.assertEqual(len(c), 2)

    def test_key_includes_size(self):
        c = ArtCache(4)
        c.put(("a", 240), b"x")
        self.assertIsNone(c.get(("a", 100)))

    def test_put_none_not_allowed(self):
        c = ArtCache(2)
        with self.assertRaises((TypeError, ValueError)):
            c.put(("a", 1), None)  # type: ignore[arg-type]
        self.assertEqual(len(c), 0)

    def test_bad_capacity(self):
        for n in (0, -1, True):
            with self.subTest(n=n), self.assertRaises(ValueError):
                ArtCache(n)

    def test_threads_smoke(self):
        c = ArtCache(8)
        errors = []

        def work(i):
            try:
                for j in range(200):
                    k = (f"u{(i * 7 + j) % 20}", 240)
                    c.put(k, b"x")
                    c.get(k)
                    self.assertLessEqual(len(c), 8)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertLessEqual(len(c), 8)


class GetJpegTest(unittest.TestCase):
    def setUp(self):
        self.cfg = CoverConfig()

    def test_empty_url_does_nothing(self):
        op, run = Opener(FakeResp(PNG)), FakeRun()
        cache = ArtCache(4)
        self.assertIsNone(get_jpeg("", self.cfg, cache, open_url=op, run=run))
        self.assertEqual((op.calls, run.calls, len(cache)), ([], [], 0))

    def test_miss_then_hit(self):
        op, run, cache = Opener(FakeResp(PNG)), FakeRun(), ArtCache(4)
        self.assertEqual(get_jpeg(SPOTIFY, self.cfg, cache, open_url=op, run=run), JPEG)
        self.assertEqual((len(op.calls), len(run.calls), len(cache)), (1, 1, 1))
        self.assertEqual(get_jpeg(SPOTIFY, self.cfg, cache, open_url=op, run=run), JPEG)
        self.assertEqual((len(op.calls), len(run.calls)), (1, 1))

    def test_cache_hit_calls_no_fake(self):
        cache = ArtCache(4)
        cache.put((SPOTIFY, 480), b"cached")
        op, run = Opener(exc=AssertionError("no")), FakeRun(exc=AssertionError("no"))
        self.assertEqual(get_jpeg(SPOTIFY, self.cfg, cache, open_url=op, run=run), b"cached")
        self.assertEqual((op.calls, run.calls), ([], []))

    def test_size_passed_to_resize_and_in_key(self):
        cfg = CoverConfig(size=120)
        run, cache = FakeRun(), ArtCache(4)
        get_jpeg(SPOTIFY, cfg, cache, open_url=Opener(FakeResp(PNG)), run=run)
        self.assertIn("120x120^", run.calls[0][0])  # was "120x120!" (stretch); now crop-to-fill
        self.assertNotIn("120x120!", run.calls[0][0])
        self.assertEqual(cache.get((SPOTIFY, 120)), JPEG)

    def test_bad_url_is_none_and_nothing_called(self):
        op, run = Opener(FakeResp(PNG)), FakeRun()
        self.assertIsNone(
            get_jpeg("http://i.scdn.co/x", self.cfg, ArtCache(4), open_url=op, run=run)
        )
        self.assertEqual((op.calls, run.calls), ([], []))

    def test_failures_are_not_cached(self):
        cache = ArtCache(4)
        self.assertIsNone(
            get_jpeg(SPOTIFY, self.cfg, cache, open_url=Opener(exc=OSError("x")), run=FakeRun())
        )
        self.assertEqual(len(cache), 0)
        self.assertIsNone(
            get_jpeg(
                SPOTIFY, self.cfg, cache, open_url=Opener(FakeResp(PNG)), run=FakeRun(returncode=1)
            )
        )
        self.assertEqual(len(cache), 0)
        # the next try works because nothing bad was remembered
        self.assertEqual(
            get_jpeg(SPOTIFY, self.cfg, cache, open_url=Opener(FakeResp(PNG)), run=FakeRun()), JPEG
        )

    def test_non_image_download_never_reaches_magick(self):
        run = FakeRun()
        r = get_jpeg(SPOTIFY, self.cfg, ArtCache(4), open_url=Opener(FakeResp(b"<svg/>")), run=run)
        self.assertIsNone(r)
        self.assertEqual(run.calls, [])

    def test_unexpected_exception_is_none_and_logged(self):
        class Boom(Exception):
            pass

        with self.assertLogs("mass80d.cover_art", level="ERROR") as cm:
            r = get_jpeg(
                SPOTIFY,
                self.cfg,
                ArtCache(4),
                open_url=Opener(FakeResp(PNG)),
                run=FakeRun(exc=Boom("x")),
            )
        self.assertIsNone(r)
        self.assertEqual(len(cm.records), 1)

    def test_unexpected_exception_in_opener_is_none(self):
        class Boom(Exception):
            pass

        with self.assertLogs("mass80d.cover_art", level="ERROR"):
            r = get_jpeg(
                SPOTIFY, self.cfg, ArtCache(4), open_url=Opener(exc=Boom("x")), run=FakeRun()
            )
        self.assertIsNone(r)

    def test_local_file_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            art = os.path.join(os.path.realpath(tmp), "art")
            os.mkdir(art)
            with open(os.path.join(art, "a.png"), "wb") as f:
                f.write(PNG)
            cfg = CoverConfig(local_dirs=(art,))
            op, run = Opener(exc=AssertionError("no net")), FakeRun()
            url = "file://" + os.path.join(art, "a.png")
            self.assertEqual(get_jpeg(url, cfg, ArtCache(4), open_url=op, run=run), JPEG)
            self.assertEqual(op.calls, [])
            self.assertEqual(run.calls[0][1]["input"], PNG)

    def test_local_file_refused_without_local_dirs(self):
        run = FakeRun()
        self.assertIsNone(get_jpeg(FIREFOX, self.cfg, ArtCache(4), run=run))
        self.assertEqual(run.calls, [])

    def test_cache_default_is_none_ok(self):
        self.assertEqual(
            get_jpeg(SPOTIFY, self.cfg, open_url=Opener(FakeResp(PNG)), run=FakeRun()), JPEG
        )


def make_png(w=64, h=64, pixel=None):
    """In-memory RGB PNG. `pixel(x, y)` returns (r, g, b); default is a gradient."""

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    def gradient(x, y):
        return (x * 4 % 256, y * 4 % 256, 128)

    px = pixel or gradient
    rows = b"".join(b"\x00" + b"".join(bytes(px(x, y)) for x in range(w)) for y in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def jpeg_dimensions(data):
    """Walk the JPEG markers to the first SOF0/SOF1/SOF2 and return (width, height)."""
    assert data[:3] == b"\xff\xd8\xff"
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            raise AssertionError("bad marker alignment")
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0x01, *range(0xD0, 0xD9)):  # standalone markers
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return (width, height)
        i += 2 + length
    raise AssertionError("no SOF segment")


def magick_pixel(jpeg, x, y):
    res = subprocess.run(
        [
            "/usr/bin/magick",
            "jpeg:-",
            "-format",
            f"%[fx:round(p{{{x},{y}}}.r*255)],%[fx:round(p{{{x},{y}}}.g*255)],"
            f"%[fx:round(p{{{x},{y}}}.b*255)]",
            "info:",
        ],
        input=jpeg,
        capture_output=True,
        timeout=10,
        env={"LC_ALL": "C"},
        check=True,
    )
    return tuple(int(v) for v in res.stdout.decode().split(","))


@unittest.skipUnless(os.path.exists("/usr/bin/magick"), "ImageMagick not installed")
class MagickIntegrationTest(unittest.TestCase):
    """Real /usr/bin/magick on a synthetic in-memory PNG. Nothing is written by the test."""

    def magick_entries(self):
        return {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("magick-")}

    def test_real_resize(self):
        before = self.magick_entries()
        out = resize_to_jpeg(make_png(), CoverConfig())
        self.assertIsNotNone(out)
        assert out is not None
        self.assertTrue(out.startswith(b"\xff\xd8\xff"))
        self.assertGreater(len(out), 0)
        self.assertLess(len(out), 20_000)
        self.assertEqual(self.magick_entries() - before, set())

    def canvas_cfg(self, **kw):
        return CoverConfig(size=240, canvas_width=480, canvas_height=240, **kw)

    def test_canvas_dimensions_for_square_wide_and_portrait(self):
        before = self.magick_entries()
        for name, (w, h) in (
            ("square", (640, 640)),
            ("16:9", (640, 360)),
            ("portrait", (300, 600)),
        ):
            with self.subTest(name=name):
                out = resize_to_jpeg(make_png(w, h), self.canvas_cfg())
                self.assertIsNotNone(out)
                assert out is not None
                self.assertTrue(out.startswith(b"\xff\xd8\xff"))
                self.assertEqual(jpeg_dimensions(out), (480, 240))
        self.assertEqual(self.magick_entries() - before, set())

    def test_default_canvas_is_a_480_square(self):
        out = resize_to_jpeg(make_png(640, 360), CoverConfig())
        assert out is not None
        self.assertEqual(jpeg_dimensions(out), (480, 480))

    def test_size_240_on_the_240_square_canvas_still_works(self):
        cfg = CoverConfig(size=240, canvas_width=240, canvas_height=240)
        out = resize_to_jpeg(make_png(640, 360), cfg)
        assert out is not None
        self.assertEqual(jpeg_dimensions(out), (240, 240))

    def test_sides_are_flat_grey(self):
        before = self.magick_entries()
        out = resize_to_jpeg(make_png(640, 360), self.canvas_cfg())
        assert out is not None
        for x in (30, 450):
            got = magick_pixel(out, x, 120)
            for ch in got:
                self.assertLessEqual(abs(ch - 32), 2, msg=f"x={x} got={got}")
        self.assertEqual(self.magick_entries() - before, set())

    def test_grey_value_is_used(self):
        out = resize_to_jpeg(make_png(640, 360), self.canvas_cfg(fill_grey=16))
        assert out is not None
        for ch in magick_pixel(out, 30, 120):
            self.assertLessEqual(abs(ch - 16), 2)

    def test_centre_shows_the_middle_of_the_source_crop_not_squash(self):
        red = (200, 30, 30)
        blue = (30, 30, 200)

        def px(x, y):
            return blue if 220 <= x < 420 and 130 <= y < 230 else red

        # a 640x360 source: the middle block spans the centre of the picture.
        out = resize_to_jpeg(make_png(640, 360, px), self.canvas_cfg())
        assert out is not None
        got = magick_pixel(out, 240, 120)
        for ch, want in zip(got, blue, strict=True):
            self.assertLessEqual(abs(ch - want), 12, msg=f"got={got}")

    def test_480_cover_dimensions_for_square_wide_and_portrait(self):
        before = self.magick_entries()
        cfg = CoverConfig(size=480, canvas_width=480, canvas_height=480)
        for name, (w, h) in (
            ("square", (640, 640)),
            ("16:9", (640, 360)),
            ("portrait", (300, 600)),
        ):
            with self.subTest(name=name):
                out = resize_to_jpeg(make_png(w, h), cfg)
                self.assertIsNotNone(out)
                assert out is not None
                self.assertTrue(out.startswith(b"\xff\xd8\xff"))
                self.assertEqual(jpeg_dimensions(out), (480, 480))
        self.assertEqual(self.magick_entries() - before, set())

    def test_480_cover_has_no_grey_sides_and_keeps_the_centre(self):
        before = self.magick_entries()
        cfg = CoverConfig(size=480, canvas_width=480, canvas_height=480)
        red = (200, 30, 30)
        blue = (30, 30, 200)

        def px(x, y):
            return blue if 280 <= x < 360 and 130 <= y < 230 else red

        out = resize_to_jpeg(make_png(640, 360, px), cfg)
        assert out is not None
        got = magick_pixel(out, 240, 240)
        for ch, want in zip(got, blue, strict=True):
            self.assertLessEqual(abs(ch - want), 12, msg=f"got={got}")
        side = magick_pixel(out, 5, 240)
        self.assertFalse(all(abs(ch - 32) <= 2 for ch in side), msg=f"side={side}")
        self.assertEqual(self.magick_entries() - before, set())

    def test_hostile_payloads_fail_closed_without_running(self):
        for data in (b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"push graphic-context\n"):
            run = FakeRun()
            self.assertIsNone(resize_to_jpeg(data, CoverConfig(), run))
            self.assertEqual(run.calls, [])

    def test_png_header_plus_garbage_fails(self):
        before = self.magick_entries()
        with self.assertLogs("mass80d.cover_art", level="WARNING"):
            out = resize_to_jpeg(b"\x89PNG\r\n\x1a\n" + b"garbage" * 50, CoverConfig())
        self.assertIsNone(out)
        self.assertEqual(self.magick_entries() - before, set())


if __name__ == "__main__":
    unittest.main()
