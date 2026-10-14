import logging
import unittest
from unittest import mock

import mass80d.transport as transport_mod
from mass80d.transport import DEFAULT_PORT, SerialTransport, TransportError

try:
    from serial import SerialException
except ImportError:  # pragma: no cover
    SerialException = OSError

try:
    from serial import SerialTimeoutException
except ImportError:  # pragma: no cover
    SerialTimeoutException = OSError


class FakeSerial:
    """Scripted stand-in for serial.Serial. Never touches a device node."""

    def __init__(self, script):
        # script: list of bytes (returned by read) or Exception (raised by read)
        self.script = list(script)
        self.closed = False
        self.written = []

    def read(self, n=1):
        if not self.script:
            return b""
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def in_waiting(self):
        return 0

    def write(self, data):
        self.written.append(bytes(data))
        return len(data)

    def close(self):
        self.closed = True


class Factory:
    """Each call consumes one outcome: a FakeSerial, or an Exception to raise on open."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, port, baudrate=None, timeout=None, **kw):
        self.calls.append((port, baudrate, timeout))
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


def make(outcomes, **kw):
    clock = Clock()
    factory = Factory(outcomes)
    t = SerialTransport(serial_factory=factory, sleep=clock.sleep, clock=clock.time, **kw)
    return t, factory, clock


MSG = b'{"action":"music_play"}\n'


class TransportTests(unittest.TestCase):
    def test_default_port_is_by_id_path(self):
        self.assertEqual(
            DEFAULT_PORT,
            "/dev/serial/by-id/usb-Espressif_Systems_Espressif_Device_123456-if00",
        )

    def test_opens_default_port_and_yields_events(self):
        ser = FakeSerial([MSG])
        t, f, _ = make([ser])
        evs = t.read_events()
        self.assertEqual([e.action for e in evs], ["music_play"])
        self.assertEqual(f.calls[0][0], DEFAULT_PORT)

    def test_fragments_across_reads(self):
        ser = FakeSerial([b'{"action":"mus', b'ic_play"}', b"\n"])
        t, _, _ = make([ser])
        got = []
        for _ in range(3):
            got.extend(t.read_events())
        self.assertEqual([e.action for e in got], ["music_play"])

    def test_open_failure_backs_off_exponentially_and_caps(self):
        errs = [SerialException("no such file") for _ in range(6)]
        t, f, clock = make(errs, backoff_initial=0.5, backoff_factor=2.0, backoff_max=3.0)
        for _ in range(6):
            self.assertEqual(t.read_events(), [])
        # First attempt is immediate, subsequent ones wait: 0.5, 1, 2, 3, 3 (capped).
        self.assertEqual(len(f.calls), 6)
        self.assertEqual(clock.sleeps, [0.5, 1.0, 2.0, 3.0, 3.0])

    def test_reconnects_after_read_error_and_resets_parser(self):
        first = FakeSerial([b'{"action":"mus', SerialException("error -71")])
        second = FakeSerial([b'ic_pause"}\n', MSG])
        t, f, clock = make([first, SerialException("gone"), second], backoff_initial=0.1)
        out = []
        for _ in range(8):
            out.extend(t.read_events())
        self.assertTrue(first.closed)
        # Partial line from the dead connection must NOT be glued to the new stream.
        self.assertEqual([e.action for e in out], ["music_play"])
        self.assertEqual(len(f.calls), 3)

    def test_oserror_on_read_is_survived(self):
        first = FakeSerial([OSError(5, "Input/output error")])
        second = FakeSerial([MSG])
        t, _, _ = make([first, second], backoff_initial=0.1)
        out = []
        for _ in range(4):
            out.extend(t.read_events())
        self.assertEqual([e.action for e in out], ["music_play"])

    def test_backoff_resets_after_successful_data(self):
        a = FakeSerial([MSG, SerialException("drop")])
        b = FakeSerial([SerialException("drop again")])
        t, f, clock = make(
            [SerialException("x"), SerialException("y"), a, SerialException("z"), b],
            backoff_initial=1.0,
            backoff_factor=2.0,
            backoff_max=60.0,
        )
        for _ in range(6):
            t.read_events()
        # fails x (immediate), sleep 1; y fails, sleep 2; a opens, data read (reset);
        # a drops, sleep 1 (reset, not 4); z fails, sleep 2; b opens ...
        self.assertEqual(clock.sleeps[:4], [1.0, 2.0, 1.0, 2.0])

    def test_backoff_not_reset_by_open_without_data(self):
        a = FakeSerial([SerialException("drop")])
        b = FakeSerial([SerialException("drop")])
        t, _, clock = make([a, b, FakeSerial([])], backoff_initial=1.0, backoff_factor=2.0)
        for _ in range(6):
            t.read_events()
        self.assertEqual(clock.sleeps[:2], [1.0, 2.0])

    def test_events_generator_stops(self):
        ser = FakeSerial([MSG, MSG])
        t, _, _ = make([ser])
        stop = {"n": 0}

        def should_stop():
            stop["n"] += 1
            return stop["n"] > 2

        got = list(t.events(should_stop))
        self.assertEqual(len(got), 2)

    def test_write_requires_connection_and_writes_bytes(self):
        ser = FakeSerial([MSG])
        t, _, _ = make([ser])
        with self.assertRaises(TransportError):
            t.write(b"x")
        t.read_events()
        t.write(b'{"action":"get_time"}\n')  # fake object only
        self.assertEqual(ser.written, [b'{"action":"get_time"}\n'])

    def test_write_failure_closes_and_raises_transport_error(self):
        ser = FakeSerial([MSG])
        ser.write = lambda d: (_ for _ in ()).throw(SerialException("dead"))
        t, _, _ = make([ser])
        t.read_events()
        with self.assertRaises(TransportError):
            t.write(b"x")
        self.assertTrue(ser.closed)

    def test_write_timeout_closes_drops_port_and_reconnects(self):
        bad = FakeSerial([MSG])

        def timeout_write(d):
            raise SerialTimeoutException("write timeout")

        bad.write = timeout_write
        good = FakeSerial([MSG])
        t, f, _ = make([bad, good])
        t.read_events()
        with (
            self.assertLogs("mass80d.transport", level="WARNING") as cm,
            self.assertRaises(TransportError),
        ):
            t.write(b"x")
        self.assertTrue(bad.closed)
        self.assertFalse(t.connected)
        text = "\n".join(r.getMessage() for r in cm.records)
        self.assertIn("write failed, dropping port", text)
        self.assertIn("SerialTimeoutException", text)
        t.read_events()
        self.assertEqual(len(f.calls), 2)
        self.assertTrue(t.connected)

    def test_close_is_idempotent(self):
        ser = FakeSerial([MSG])
        t, _, _ = make([ser])
        t.read_events()
        t.close()
        t.close()
        self.assertTrue(ser.closed)


class DefaultFactoryTests(unittest.TestCase):
    def test_default_factory_passes_write_timeout(self):
        seen = []

        class Recorder:
            def __init__(self, *args, **kwargs):
                seen.append((args, kwargs))

        with mock.patch("serial.Serial", Recorder):
            transport_mod._default_factory("/dev/fake", 115200, 0.1)
        self.assertEqual(len(seen), 1)
        args, kwargs = seen[0]
        self.assertEqual(args, ("/dev/fake", 115200))
        self.assertEqual(kwargs["timeout"], 0.1)
        self.assertEqual(kwargs["write_timeout"], transport_mod.WRITE_TIMEOUT_S)

    def test_write_timeout_constant_is_5_seconds(self):
        self.assertEqual(transport_mod.WRITE_TIMEOUT_S, 5.0)


class SilenceWatchdogTests(unittest.TestCase):
    def test_default_timeout_is_10_seconds(self):
        t, _, _ = make([])
        self.assertEqual(t.silence_timeout, 10.0)

    def test_silent_open_port_reconnects_after_timeout_and_not_before(self):
        first, second = FakeSerial([]), FakeSerial([])
        t, f, clock = make([first, second], silence_timeout=10.0, backoff_initial=0.5)
        t.read_events()  # opens, nothing received
        clock.now += 9.9
        t.read_events()
        self.assertFalse(first.closed)
        self.assertEqual(len(f.calls), 1)
        clock.now += 0.2  # 10.1 s of silence
        t.read_events()
        self.assertTrue(first.closed)
        self.assertFalse(t.connected)
        t.read_events()  # reconnect goes through the backoff sleep
        self.assertEqual(len(f.calls), 2)
        self.assertEqual(clock.sleeps, [0.5])

    def test_data_resets_the_silence_timer(self):
        ser = FakeSerial([b"", b"debug text\n"])
        t, _, clock = make([ser], silence_timeout=10.0)
        t.read_events()  # opens at t=0
        clock.now += 9.0
        t.read_events()  # receives bytes at t=9
        clock.now += 9.0  # 18 s since open, 9 s since data
        t.read_events()
        self.assertFalse(ser.closed)
        clock.now += 1.1  # 10.1 s since data
        t.read_events()
        self.assertTrue(ser.closed)

    def test_non_event_bytes_count_as_data(self):
        # Silence means no bytes, not no events: debug text alone keeps the link alive.
        ser = FakeSerial([b"[CDC-TX] debug\n"] * 3)
        t, _, clock = make([ser], silence_timeout=10.0)
        for _ in range(3):
            clock.now += 8.0
            self.assertEqual(t.read_events(), [])
        self.assertFalse(ser.closed)

    def test_parser_is_reset_on_silence_reconnect(self):
        first = FakeSerial([b'{"action":"mus'])
        second = FakeSerial([b'ic_pause"}\n', MSG])
        t, _, clock = make([first, second], silence_timeout=10.0, backoff_initial=0.1)
        t.read_events()  # partial line buffered
        clock.now += 10.5
        t.read_events()  # watchdog fires
        out = []
        for _ in range(3):
            out.extend(t.read_events())
        self.assertEqual([e.action for e in out], ["music_play"])

    def test_silence_restarts_from_each_new_connection(self):
        first, second = FakeSerial([]), FakeSerial([])
        t, _, clock = make([first, second], silence_timeout=10.0, backoff_initial=0.1)
        t.read_events()
        clock.now += 10.5
        t.read_events()  # dropped
        t.read_events()  # reopened (clock now includes backoff sleep)
        self.assertFalse(second.closed)
        clock.now += 5.0
        t.read_events()
        self.assertFalse(second.closed)

    def test_invalid_timeout_rejected(self):
        for bad in (0, -1, True, "10", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                make([], silence_timeout=bad)


LOGGER = "mass80d.transport"


def _drive(t, n):
    for _ in range(n):
        t.read_events()


class TransportLoggingTests(unittest.TestCase):
    def test_first_open_failure_logs_once_with_details(self):
        t, _, _ = make([SerialException("no such file")], backoff_initial=0.5)
        with self.assertLogs(LOGGER, "WARNING") as cm:
            t.read_events()
        self.assertEqual(len(cm.records), 1)
        msg = cm.records[0].getMessage()
        self.assertIn(DEFAULT_PORT, msg)
        self.assertIn("no such file", msg)
        self.assertIn("retry in 0.5 s", msg)
        self.assertIn("consecutive failures=1", msg)

    def test_open_failures_are_rate_limited_with_milestones(self):
        # 20 failures with a 0.01 s backoff take far less than 30 s of injected time.
        errs = [SerialException(f"gone {i}") for i in range(20)]
        t, _, clock = make(errs, backoff_initial=0.01, backoff_factor=1.0, backoff_max=0.01)
        with self.assertLogs(LOGGER, "WARNING") as cm:
            _drive(t, 20)
        self.assertLess(clock.now, 30.0)
        msgs = [r.getMessage() for r in cm.records]
        self.assertEqual(len(msgs), 4, msgs)
        for msg, n in zip(msgs, (1, 5, 10, 20), strict=True):
            self.assertIn(f"consecutive failures={n}\n", msg + "\n")

    def test_non_milestone_failures_within_window_add_no_lines(self):
        errs = [SerialException("x") for _ in range(4)]
        t, _, _ = make(errs, backoff_initial=0.01, backoff_factor=1.0, backoff_max=0.01)
        with self.assertLogs(LOGGER, "WARNING") as cm:
            _drive(t, 4)
        self.assertEqual(len(cm.records), 1)  # failures 2, 3, 4 are silent

    def test_failure_after_30s_logs_again(self):
        errs = [SerialException("x") for _ in range(3)]
        t, _, clock = make(errs, backoff_initial=0.01, backoff_factor=1.0, backoff_max=0.01)
        with self.assertLogs(LOGGER, "WARNING") as cm:
            _drive(t, 2)  # failure 1 logged, failure 2 silent
            self.assertEqual(len(cm.records), 1)
            clock.now += 30.0
            _drive(t, 1)  # failure 3, more than 30 s after the last line
        self.assertEqual(len(cm.records), 2)

    def test_read_error_logs_warning_with_uptime(self):
        ser = FakeSerial([b"", SerialException("device reports readiness")])
        t, _, clock = make([ser])
        t.read_events()  # opens at t=0, empty read
        clock.now += 7.0
        with self.assertLogs(LOGGER, "WARNING") as cm:
            t.read_events()
        self.assertEqual(len(cm.records), 1)
        msg = cm.records[0].getMessage()
        self.assertIn("SerialException", msg)
        self.assertIn("device reports readiness", msg)
        self.assertIn("up 7.0 s", msg)

    def test_reconnect_logs_info_with_away_time_and_attempts(self):
        first = FakeSerial([b"", SerialException("drop")])
        second = FakeSerial([])
        t, _, clock = make(
            [first, SerialException("still gone"), second],
            backoff_initial=2.0,
            backoff_factor=1.0,
            backoff_max=2.0,
        )
        t.read_events()  # opens at t=0
        clock.now += 1.0
        with self.assertLogs(LOGGER, "INFO") as cm:
            t.read_events()  # drop at t=1
            t.read_events()  # sleeps 2 s, open fails (t=3)
            t.read_events()  # sleeps 2 s, opens (t=5)
        infos = [r.getMessage() for r in cm.records if r.levelname == "INFO"]
        self.assertEqual(len(infos), 1)  # the first open happened before assertLogs
        self.assertIn(DEFAULT_PORT, infos[0])
        self.assertIn("away 4.0 s", infos[0])
        self.assertIn("attempt 2", infos[0])

    def test_first_successful_open_logs_info(self):
        t, _, _ = make([FakeSerial([])])
        with self.assertLogs(LOGGER, "INFO") as cm:
            t.read_events()
        self.assertEqual(len(cm.records), 1)
        self.assertEqual(cm.records[0].levelname, "INFO")
        self.assertIn(DEFAULT_PORT, cm.records[0].getMessage())
        self.assertIn("attempt 1", cm.records[0].getMessage())

    def test_silence_watchdog_logs_once_when_it_fires(self):
        t, _, clock = make([FakeSerial([]), FakeSerial([])], silence_timeout=10.0)
        t.read_events()
        with self.assertLogs(LOGGER, "WARNING") as cm:
            logging.getLogger(LOGGER).warning("sentinel")
            for _ in range(5):  # polls before the timeout log nothing
                clock.now += 1.0
                t.read_events()
            self.assertEqual(len(cm.records), 1)
            clock.now += 6.0  # 11 s of silence in total
            t.read_events()
        self.assertEqual(len(cm.records), 2)
        msg = cm.records[1].getMessage()
        self.assertIn("11.0 s", msg)
        self.assertIn("timeout 10.0 s", msg)
        self.assertIn("reconnecting", msg)

    def test_short_silence_logs_no_warning(self):
        t, _, clock = make([FakeSerial([])], silence_timeout=10.0)
        t.read_events()
        clock.now += 9.9
        with self.assertNoLogs(LOGGER, "WARNING"):
            t.read_events()

    def test_write_failure_logs_warning(self):
        ser = FakeSerial([MSG])
        ser.write = lambda d: (_ for _ in ()).throw(SerialException("write dead"))
        t, _, _ = make([ser])
        t.read_events()
        with self.assertLogs(LOGGER, "WARNING") as cm, self.assertRaises(TransportError):
            t.write(b"x")
        self.assertEqual(len(cm.records), 1)
        self.assertIn("write dead", cm.records[0].getMessage())

    def test_no_log_line_contains_device_bytes(self):
        payload = b'{"action":"music_play","marker":"ZZQ-PAYLOAD-123"}\n'
        a = FakeSerial([payload, b"ZZQ-DEBUG-456\n", SerialException("drop")])
        b = FakeSerial([b""])
        t, _, clock = make([a, b], silence_timeout=10.0, backoff_initial=0.1)
        with self.assertLogs(LOGGER, "DEBUG") as cm:
            logging.getLogger(LOGGER).info("sentinel")
            _drive(t, 6)
            clock.now += 11.0
            t.read_events()
        self.assertGreater(len(cm.records), 2)
        text = "\n".join(r.getMessage() for r in cm.records)
        self.assertNotIn("ZZQ", text)

    def test_logging_does_not_change_call_sequence(self):
        def scenario():
            first = FakeSerial([b"", SerialException("drop")])
            second = FakeSerial([MSG])
            outcomes = [
                SerialException("a"),
                SerialException("b"),
                first,
                SerialException("c"),
                second,
            ]
            t, f, clock = make(outcomes, silence_timeout=10.0)
            out = []
            for _ in range(10):
                out.extend(e.action for e in t.read_events())
                clock.now += 1.0
            return f.calls, clock.sleeps, out

        plain = scenario()
        with self.assertLogs(LOGGER, "DEBUG") as cm:
            logging.getLogger(LOGGER).info("sentinel")
            captured = scenario()
        self.assertGreater(len(cm.records), 1)
        self.assertEqual(plain, captured)


if __name__ == "__main__":
    unittest.main()
