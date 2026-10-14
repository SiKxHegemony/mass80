"""CoverService tests: fake `fetch` driven by Events, no network, no magick, no real sleeps."""

import functools
import logging
import threading
import time
import unittest
from unittest import mock

from mass80d import cover_service
from mass80d.cover_art import ArtCache, CoverConfig
from mass80d.cover_service import CoverLookup, CoverService, CoverStatus

A = "https://i.scdn.co/image/aaaa"
B = "https://i.scdn.co/image/bbbb"
C = "https://i.scdn.co/image/cccc"
D = "https://i.scdn.co/image/dddd"
JPEG = b"\xff\xd8\xff\xe0" + b"j" * 20


def body_for(url):
    return JPEG + url.encode()[-4:]


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeFetch:
    """Counts calls; per-URL gates (Events) block the worker until released."""

    def __init__(self, results=None, default=None):
        self.calls = []
        self.gates = {}
        self.started = {}
        self.results = results if results is not None else {}
        self.default = default
        self._lock = threading.Lock()

    def gate(self, url):
        self.gates[url] = threading.Event()
        self.started[url] = threading.Event()
        return self.gates[url]

    def __call__(self, url):
        with self._lock:
            self.calls.append(url)
        if url in self.started:
            self.started[url].set()
        if url in self.gates and not self.gates[url].wait(5.0):
            raise AssertionError("gate never released")
        res = self.results.get(url, self.default if self.default is not None else body_for(url))
        if isinstance(res, BaseException):
            raise res
        return res

    def count(self, url):
        return self.calls.count(url)


class ServiceCase(unittest.TestCase):
    def make(self, fetch=None, start=True, **kw):
        self.fetch = fetch if fetch is not None else FakeFetch()
        self.clock = kw.pop("clock", None) or FakeClock()
        svc = CoverService(CoverConfig(), fetch=self.fetch, clock=self.clock, **kw)
        self.addCleanup(self._cleanup, svc)
        if start:
            svc.start()
        return svc

    def _cleanup(self, svc):
        for gate in getattr(self.fetch, "gates", {}).values():
            gate.set()
        svc.stop(timeout=2.0)

    def idle(self, svc):
        self.assertTrue(svc.wait_idle(2.0), "service did not go idle")


class StateTest(ServiceCase):
    def test_unknown_before_start(self):
        svc = self.make(start=False)
        self.assertEqual(svc.prefetch(A), CoverStatus.UNKNOWN)
        self.assertEqual(svc.lookup(A), CoverLookup(CoverStatus.UNKNOWN))
        self.assertEqual(self.fetch.calls, [])

    def test_prefetch_then_ready_with_exact_bytes(self):
        svc = self.make()
        gate = self.fetch.gate(A)
        self.assertEqual(svc.prefetch(A), CoverStatus.PENDING)
        self.assertTrue(self.fetch.started[A].wait(2.0))
        self.assertEqual(svc.lookup(A), CoverLookup(CoverStatus.PENDING))
        gate.set()
        self.idle(svc)
        res = svc.lookup(A)
        self.assertEqual(res.status, CoverStatus.READY)
        self.assertEqual(res.jpeg, body_for(A))
        self.assertEqual(svc.cache.get((A, CoverConfig().size)), body_for(A))

    def test_repeat_prefetch_is_ready_without_second_fetch(self):
        svc = self.make()
        svc.prefetch(A)
        self.idle(svc)
        self.assertEqual(svc.prefetch(A), CoverStatus.READY)
        self.assertEqual(svc.prefetch(A), CoverStatus.READY)
        self.idle(svc)
        self.assertEqual(self.fetch.count(A), 1)

    def test_lookup_has_no_side_effects(self):
        svc = self.make()
        self.assertEqual(svc.lookup(A), CoverLookup(CoverStatus.UNKNOWN))
        self.assertTrue(svc.wait_idle(0.5))
        self.assertEqual(self.fetch.calls, [])

    def test_empty_url_is_unknown(self):
        svc = self.make()
        self.assertEqual(svc.prefetch(""), CoverStatus.UNKNOWN)
        self.assertEqual(svc.lookup(""), CoverLookup(CoverStatus.UNKNOWN))
        self.idle(svc)
        self.assertEqual(self.fetch.calls, [])

    def test_prefetch_after_stop_is_unknown(self):
        svc = self.make()
        self.assertTrue(svc.stop(timeout=2.0))
        self.assertEqual(svc.prefetch(A), CoverStatus.UNKNOWN)
        self.assertEqual(svc.lookup(A).status, CoverStatus.UNKNOWN)
        self.assertEqual(self.fetch.calls, [])

    def test_preseeded_cache_is_ready_without_job(self):
        cache = ArtCache(4)
        cache.put((A, CoverConfig().size), JPEG)
        fetch = FakeFetch()
        svc = CoverService(CoverConfig(), cache, fetch=fetch)
        self.addCleanup(svc.stop)
        svc.start()
        self.assertEqual(svc.prefetch(A), CoverStatus.READY)
        self.assertEqual(svc.lookup(A), CoverLookup(CoverStatus.READY, JPEG))
        self.assertEqual(fetch.calls, [])
        self.assertIs(svc.cache, cache)

    def test_service_creates_cache_with_cfg_size(self):
        svc = CoverService(CoverConfig(cache_size=3), fetch=FakeFetch())
        self.assertIsInstance(svc.cache, ArtCache)


class DedupTest(ServiceCase):
    def test_fifty_rapid_prefetches_one_fetch(self):
        svc = self.make()
        gate = self.fetch.gate(A)
        results = [svc.prefetch(A) for _ in range(50)]
        self.assertTrue(self.fetch.started[A].wait(2.0))
        results += [svc.prefetch(A) for _ in range(50)]
        self.assertEqual(set(results), {CoverStatus.PENDING})
        self.assertEqual(self.fetch.count(A), 1)
        gate.set()
        self.idle(svc)
        self.assertEqual(svc.lookup(A).status, CoverStatus.READY)
        self.assertEqual(self.fetch.count(A), 1)


class FailureTest(ServiceCase):
    def test_none_is_failed_and_not_retried_until_ttl(self):
        svc = self.make(FakeFetch(results={A: None}))
        self.assertEqual(svc.prefetch(A), CoverStatus.PENDING)
        self.idle(svc)
        self.assertEqual(svc.lookup(A), CoverLookup(CoverStatus.FAILED))
        self.assertEqual(svc.prefetch(A), CoverStatus.FAILED)
        self.assertEqual(self.fetch.count(A), 1)
        self.clock.now += 59.0
        self.assertEqual(svc.prefetch(A), CoverStatus.FAILED)
        self.assertEqual(self.fetch.count(A), 1)
        self.clock.now += 2.0  # 61 s after the failure
        self.assertEqual(svc.lookup(A).status, CoverStatus.UNKNOWN)
        self.assertEqual(svc.prefetch(A), CoverStatus.PENDING)
        self.idle(svc)
        self.assertEqual(self.fetch.count(A), 2)
        self.assertEqual(svc.prefetch(A), CoverStatus.FAILED)
        self.assertEqual(self.fetch.count(A), 2)

    def test_custom_fail_ttl(self):
        svc = self.make(FakeFetch(results={A: None}), fail_ttl_s=5.0)
        svc.prefetch(A)
        self.idle(svc)
        self.clock.now += 6.0
        self.assertEqual(svc.prefetch(A), CoverStatus.PENDING)
        self.idle(svc)
        self.assertEqual(self.fetch.count(A), 2)

    def test_exception_is_failed_and_thread_survives(self):
        svc = self.make(FakeFetch(results={A: RuntimeError("boom")}))
        with self.assertLogs("mass80d.cover_service", level="ERROR"):
            svc.prefetch(A)
            self.idle(svc)
        self.assertEqual(svc.lookup(A).status, CoverStatus.FAILED)
        self.assertTrue(any(t.name == "mass80d-cover" for t in threading.enumerate()))
        self.assertEqual(svc.prefetch(B), CoverStatus.PENDING)
        self.idle(svc)
        self.assertEqual(svc.lookup(B), CoverLookup(CoverStatus.READY, body_for(B)))

    def test_non_bytes_or_empty_result_is_failure(self):
        svc = self.make(FakeFetch(results={A: b"", B: "text"}))
        svc.prefetch(A)
        svc.prefetch(B)
        self.idle(svc)
        self.assertEqual(svc.lookup(A).status, CoverStatus.FAILED)
        self.assertEqual(svc.lookup(B).status, CoverStatus.FAILED)

    def test_success_after_ttl_clears_failure(self):
        fetch = FakeFetch(results={A: None})
        svc = self.make(fetch)
        svc.prefetch(A)
        self.idle(svc)
        fetch.results[A] = JPEG
        self.clock.now += 61.0
        svc.prefetch(A)
        self.idle(svc)
        self.assertEqual(svc.lookup(A), CoverLookup(CoverStatus.READY, JPEG))

    def test_fail_memory_is_bounded(self):
        urls = [f"https://i.scdn.co/image/u{i:03d}" for i in range(40)]
        svc = self.make(FakeFetch(results=dict.fromkeys(urls)), queue_size=64)
        for u in urls:
            svc.prefetch(u)
            self.idle(svc)
        self.assertEqual(len(self.fetch.calls), 40)
        self.assertLessEqual(svc.failed_count, 16)  # public read-only property
        self.assertEqual(svc.failed_count, 16)
        self.assertEqual(svc.lookup(urls[-1]).status, CoverStatus.FAILED)
        self.assertEqual(svc.lookup(urls[0]).status, CoverStatus.UNKNOWN)


class QueueTest(ServiceCase):
    def test_full_queue_drops_oldest_queued_never_running(self):
        svc = self.make(queue_size=2)
        gate = self.fetch.gate(A)
        self.assertEqual(svc.prefetch(A), CoverStatus.PENDING)
        self.assertTrue(self.fetch.started[A].wait(2.0))  # A is now RUNNING
        for u in (B, C, D):
            self.assertEqual(svc.prefetch(u), CoverStatus.PENDING)
        self.assertEqual(svc.lookup(B), CoverLookup(CoverStatus.UNKNOWN))  # dropped
        self.assertEqual(svc.lookup(A), CoverLookup(CoverStatus.PENDING))  # running kept
        self.assertEqual(svc.lookup(C), CoverLookup(CoverStatus.PENDING))
        self.assertEqual(svc.lookup(D), CoverLookup(CoverStatus.PENDING))
        gate.set()
        self.idle(svc)
        self.assertEqual(self.fetch.calls, [A, C, D])
        self.assertEqual(svc.lookup(A).status, CoverStatus.READY)
        self.assertEqual(svc.lookup(B).status, CoverStatus.UNKNOWN)
        # a later prefetch queues the dropped URL again
        self.assertEqual(svc.prefetch(B), CoverStatus.PENDING)
        self.idle(svc)
        self.assertEqual(svc.lookup(B).status, CoverStatus.READY)


class StopTest(ServiceCase):
    def test_stop_idle_is_prompt_and_idempotent(self):
        svc = self.make()
        t0 = time.monotonic()
        self.assertTrue(svc.stop(timeout=2.0))
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertTrue(svc.stop(timeout=2.0))
        self.assertFalse(any(t.name == "mass80d-cover" for t in threading.enumerate()))

    def test_stop_never_started(self):
        svc = self.make(start=False)
        self.assertTrue(svc.stop())

    def test_stop_with_job_in_flight_returns_false_and_warns(self):
        svc = self.make()
        gate = self.fetch.gate(A)
        svc.prefetch(A)
        svc.prefetch(B)  # queued behind A: stop must clear it
        self.assertTrue(self.fetch.started[A].wait(2.0))
        with self.assertLogs("mass80d.cover_service", level="WARNING") as cm:
            self.assertFalse(svc.stop(timeout=0.2))
        self.assertTrue(any("cover worker did not stop in time" in m for m in cm.output))
        gate.set()
        self.assertTrue(svc.stop(timeout=2.0))
        self.assertEqual(self.fetch.calls, [A])  # B was cleared, never fetched
        self.assertFalse(any(t.name == "mass80d-cover" for t in threading.enumerate()))

    def test_start_twice_one_thread(self):
        svc = self.make()
        svc.start()
        svc.start()
        names = [t.name for t in threading.enumerate() if t.name == "mass80d-cover"]
        self.assertEqual(len(names), 1)
        thread = next(t for t in threading.enumerate() if t.name == "mass80d-cover")
        self.assertTrue(thread.daemon)

    def test_no_thread_at_construction(self):
        before = {t.ident for t in threading.enumerate()}
        self.make(start=False)
        self.assertEqual({t.ident for t in threading.enumerate()}, before)


class DefaultFetchTest(unittest.TestCase):
    def test_default_fetch_passes_cfg_and_cache(self):
        cfg = CoverConfig()
        with mock.patch.object(cover_service, "get_jpeg", return_value=JPEG) as gj:
            svc = CoverService(cfg)
            self.addCleanup(svc.stop)
            svc.start()
            self.assertEqual(svc.prefetch(A), CoverStatus.PENDING)
            self.assertTrue(svc.wait_idle(2.0))
        gj.assert_called_once_with(A, cfg, svc.cache)
        self.assertEqual(svc.lookup(A), CoverLookup(CoverStatus.READY, JPEG))

    def test_default_fetch_none_is_failed(self):
        with mock.patch.object(cover_service, "get_jpeg", return_value=None):
            svc = CoverService(CoverConfig())
            self.addCleanup(svc.stop)
            svc.start()
            svc.prefetch(A)
            self.assertTrue(svc.wait_idle(2.0))
        self.assertEqual(svc.lookup(A).status, CoverStatus.FAILED)

    def test_injected_partial_receives_url(self):
        seen = []

        def fetch(prefix, url):
            seen.append((prefix, url))
            return JPEG

        svc = CoverService(CoverConfig(), fetch=functools.partial(fetch, "p"))
        self.addCleanup(svc.stop)
        svc.start()
        svc.prefetch(A)
        self.assertTrue(svc.wait_idle(2.0))
        self.assertEqual(seen, [("p", A)])


class ThreadSafetyTest(ServiceCase):
    def test_hammer(self):
        urls = [f"https://i.scdn.co/image/h{i}" for i in range(5)]
        svc = self.make(FakeFetch(), queue_size=8)
        errors = []

        def work(seed):
            try:
                for i in range(200):
                    u = urls[(seed + i) % 5]
                    if i % 2:
                        svc.prefetch(u)
                    else:
                        svc.lookup(u)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(s,)) for s in range(8)]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(5.0)
        self.assertFalse(any(t.is_alive() for t in threads), "deadlock")
        self.assertLess(time.monotonic() - t0, 5.0)
        self.assertEqual(errors, [])
        self.idle(svc)
        for u in urls:
            svc.prefetch(u)
        self.idle(svc)
        for u in urls:
            self.assertEqual(svc.lookup(u).status, CoverStatus.READY)
            # once READY it is never fetched again; queue drops may cause re-queues only
            # before the first success, never after
        ready_calls = {u: self.fetch.count(u) for u in urls}
        for u in urls:
            svc.prefetch(u)
        self.idle(svc)
        self.assertEqual({u: self.fetch.count(u) for u in urls}, ready_calls)


class LoggingTest(ServiceCase):
    def test_no_url_at_info_or_above(self):
        records = []

        class H(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = H(level=logging.DEBUG)
        logger = logging.getLogger("mass80d")
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, old_level)
        self.addCleanup(logger.removeHandler, handler)
        secret = "https://i.scdn.co/image/SECRETPATH"
        svc = self.make(
            FakeFetch(results={secret: JPEG, B: None, C: RuntimeError("boom")}), queue_size=4
        )
        for u in (secret, B, C):
            svc.prefetch(u)
            self.idle(svc)
        svc.stop()
        self.assertTrue(records)
        for r in records:
            if r.levelno >= logging.INFO:
                text = r.getMessage()
                if r.exc_info:
                    text += repr(r.exc_info[1])
                self.assertNotIn("SECRETPATH", text)
                self.assertNotIn("bbbb", text)
                self.assertNotIn(JPEG.decode("latin-1"), text)
        infos = [r.getMessage() for r in records if r.levelno == logging.INFO]
        self.assertTrue(any("cover art ready (" in m for m in infos))
        self.assertTrue(any("cover art failed" in m for m in infos))


if __name__ == "__main__":
    unittest.main()
