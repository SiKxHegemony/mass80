"""Stage 3b: read-only `wpctl get-volume` reader. Every subprocess is a fake."""

import subprocess
import unittest

from mass80d.volume_reader import VolumeReader, VolumeReading, parse_get_volume


class FakeRun:
    def __init__(self, stdout=b"", returncode=0, exc=None):
        self.calls = []
        self.stdout = stdout
        self.returncode = returncode
        self.exc = exc

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, b"")


class ParseTest(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(parse_get_volume("Volume: 0.30\n"), VolumeReading(0.30, False))

    def test_muted(self):
        self.assertEqual(parse_get_volume("Volume: 0.30 [MUTED]\n"), VolumeReading(0.30, True))

    def test_garbage_and_empty(self):
        for text in ("", "\n", "garbage", "Volume: abc", "Volume:", "Volume: 0.3 0.4", "vol 0.3"):
            self.assertEqual(parse_get_volume(text), VolumeReading(None, False), repr(text))

    def test_nan_inf_and_negative_rejected(self):
        for text in ("Volume: nan", "Volume: inf", "Volume: -0.5", "Volume: -inf"):
            self.assertIsNone(parse_get_volume(text).volume, text)


class ReaderTest(unittest.TestCase):
    def test_argv_is_a_list_without_shell_and_read_only(self):
        run = FakeRun(b"Volume: 0.30\n")
        res = VolumeReader("@DEFAULT_AUDIO_SINK@", run)()
        self.assertEqual(res, VolumeReading(0.30, False))
        argv, kw = run.calls[0]
        self.assertEqual(argv, ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
        self.assertIsInstance(argv, list)
        self.assertFalse(kw.get("shell", False))
        self.assertEqual(kw["timeout"], 2)
        self.assertTrue(kw["capture_output"])
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertIs(kw["check"], False)

    def test_muted(self):
        res = VolumeReader("s", FakeRun(b"Volume: 0.55 [MUTED]\n"))()
        self.assertEqual(res, VolumeReading(0.55, True))

    def test_garbage_empty_nonzero_never_raise(self):
        bad = VolumeReading(None, False)
        self.assertEqual(VolumeReader("s", FakeRun(b"junk"))(), bad)
        self.assertEqual(VolumeReader("s", FakeRun(b""))(), bad)
        self.assertEqual(VolumeReader("s", FakeRun(b"\xff\xfe"))(), bad)
        self.assertEqual(VolumeReader("s", FakeRun(b"Volume: 0.30\n", returncode=1))(), bad)

    def test_missing_binary_and_timeout_and_other_errors(self):
        bad = VolumeReading(None, False)
        for exc in (
            FileNotFoundError("wpctl"),
            PermissionError("x"),
            subprocess.TimeoutExpired("wpctl", 2),
            subprocess.SubprocessError("x"),
            OSError("x"),
        ):
            self.assertEqual(VolumeReader("s", FakeRun(exc=exc))(), bad, repr(exc))

    def test_stdout_none_or_str(self):
        self.assertEqual(VolumeReader("s", FakeRun(None))(), VolumeReading(None, False))
        self.assertEqual(VolumeReader("s", FakeRun("Volume: 0.2\n"))(), VolumeReading(0.2, False))

    def test_default_run_is_subprocess_run_with_list_argv(self):
        from unittest import mock

        with mock.patch("mass80d.volume_reader.subprocess.run") as m:
            m.return_value = subprocess.CompletedProcess([], 0, b"Volume: 0.10\n", b"")
            res = VolumeReader("@DEFAULT_AUDIO_SINK@")()
        self.assertEqual(res, VolumeReading(0.10, False))
        self.assertEqual(m.call_args.args[0], ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
        self.assertNotIn("shell", m.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
