import errno
import os
import stat
import tempfile
import unittest
from pathlib import Path

from mass80d.portguard import apply_tiocexcl, find_port_holders, make_serial_factory


class FakeProc:
    """A fake /proc tree under a temp dir, using real symlinks."""

    def __init__(self, test: unittest.TestCase) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "proc"
        self.root.mkdir()
        self.dev = Path(self._tmp.name) / "real-device"
        self.dev.write_text("")
        self.link = Path(self._tmp.name) / "by-id-link"
        self.link.symlink_to(self.dev)

    def process(self, pid, fds, cmdline=b"") -> Path:
        fd_dir = self.root / str(pid) / "fd"
        fd_dir.mkdir(parents=True)
        for n, target in fds.items():
            (fd_dir / str(n)).symlink_to(target)
        if cmdline is not None:
            (self.root / str(pid) / "cmdline").write_bytes(cmdline)
        return fd_dir


class FindPortHoldersTest(unittest.TestCase):
    def test_finds_holder_via_resolved_symlink_path(self):
        p = FakeProc(self)
        p.process(100, {3: p.dev}, b"python\0mass80_volume.py\0")
        p.process(200, {3: "/dev/null"}, b"other\0")
        got = find_port_holders(str(p.link), proc_root=str(p.root), own_pid=1)
        self.assertEqual(got, [(100, "python mass80_volume.py")])

    def test_no_holders(self):
        p = FakeProc(self)
        p.process(200, {0: "/dev/null"}, b"other\0")
        self.assertEqual(find_port_holders(str(p.dev), proc_root=str(p.root), own_pid=1), [])

    def test_skips_own_pid(self):
        p = FakeProc(self)
        p.process(100, {3: p.dev}, b"me\0")
        p.process(101, {3: p.dev}, b"them\0")
        got = find_port_holders(str(p.dev), proc_root=str(p.root), own_pid=100)
        self.assertEqual([pid for pid, _ in got], [101])

    def test_default_own_pid_is_this_process(self):
        p = FakeProc(self)
        p.process(os.getpid(), {3: p.dev}, b"me\0")
        self.assertEqual(find_port_holders(str(p.dev), proc_root=str(p.root)), [])

    def test_ignores_non_numeric_entries_and_plain_files(self):
        p = FakeProc(self)
        (p.root / "self").mkdir()
        (p.root / "cpuinfo").write_text("x")
        p.process(100, {3: p.dev}, b"x\0")
        self.assertEqual([pid for pid, _ in find_port_holders(str(p.dev), str(p.root), 1)], [100])

    def test_process_without_fd_dir_or_vanished_is_tolerated(self):
        p = FakeProc(self)
        (p.root / "300").mkdir()  # no fd directory (vanished)
        p.process(100, {3: p.dev}, b"x\0")
        self.assertEqual([pid for pid, _ in find_port_holders(str(p.dev), str(p.root), 1)], [100])

    def test_dangling_fd_link_is_tolerated(self):
        p = FakeProc(self)
        p.process(100, {3: "/nonexistent/gone", 4: p.dev}, b"x\0")
        self.assertEqual([pid for pid, _ in find_port_holders(str(p.dev), str(p.root), 1)], [100])

    def test_unreadable_fd_dir_is_tolerated(self):
        if os.geteuid() == 0:
            self.skipTest("root can read everything")
        p = FakeProc(self)
        locked = p.process(300, {3: p.dev}, b"locked\0")
        p.process(100, {3: p.dev}, b"x\0")
        locked.chmod(0)
        self.addCleanup(locked.chmod, stat.S_IRWXU)
        got = find_port_holders(str(p.dev), proc_root=str(p.root), own_pid=1)
        self.assertEqual([pid for pid, _ in got], [100])

    def test_missing_cmdline_gives_placeholder(self):
        p = FakeProc(self)
        p.process(100, {3: p.dev}, None)
        got = find_port_holders(str(p.dev), str(p.root), 1)
        self.assertEqual(got[0][0], 100)
        self.assertTrue(got[0][1])

    def test_missing_proc_root_gives_empty_list(self):
        p = FakeProc(self)
        self.assertEqual(find_port_holders(str(p.dev), str(p.root / "nope"), 1), [])

    def test_nonexistent_device_path_is_fine(self):
        p = FakeProc(self)
        p.process(100, {3: "/dev/null"}, b"x\0")
        self.assertEqual(find_port_holders("/nonexistent/device", str(p.root), 1), [])


class FakeSerialClass:
    instances: list = []

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.closed = False
        FakeSerialClass.instances.append(self)

    def fileno(self):
        return 99

    def close(self):
        self.closed = True


class SerialFactoryTest(unittest.TestCase):
    def setUp(self):
        FakeSerialClass.instances = []

    def test_opens_exclusive_by_default(self):
        f = make_serial_factory(serial_cls=FakeSerialClass)
        f("/dev/fake", 115200, 0.1)
        self.assertEqual(FakeSerialClass.instances[0].kwargs["exclusive"], True)
        self.assertEqual(FakeSerialClass.instances[0].args, ("/dev/fake", 115200))
        self.assertEqual(FakeSerialClass.instances[0].kwargs["timeout"], 0.1)

    def test_opens_with_write_timeout(self):
        from mass80d import transport

        f = make_serial_factory(serial_cls=FakeSerialClass)
        f("/dev/fake", 115200, 0.1)
        kw = FakeSerialClass.instances[0].kwargs
        self.assertEqual(kw["write_timeout"], transport.WRITE_TIMEOUT_S)
        self.assertEqual(kw["exclusive"], True)
        self.assertEqual(kw["timeout"], 0.1)

    def test_tiocexcl_off_by_default(self):
        calls = []
        f = make_serial_factory(serial_cls=FakeSerialClass, ioctl=lambda fd, req: calls.append(fd))
        f("/dev/fake", 115200, 0.1)
        self.assertEqual(calls, [])

    def test_tiocexcl_applied_when_requested(self):
        calls = []
        f = make_serial_factory(
            tiocexcl=True, serial_cls=FakeSerialClass, ioctl=lambda fd, req: calls.append((fd, req))
        )
        f("/dev/fake", 115200, 0.1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 99)

    def test_tiocexcl_failure_closes_port_and_raises_oserror(self):
        def bad(fd, req):
            raise OSError(errno.ENOTTY, "no")

        f = make_serial_factory(tiocexcl=True, serial_cls=FakeSerialClass, ioctl=bad)
        with self.assertRaises(OSError):
            f("/dev/fake", 115200, 0.1)
        self.assertTrue(FakeSerialClass.instances[0].closed)


class TiocexclPtyTest(unittest.TestCase):
    """Records what a second open() really does on a pty after TIOCEXCL.

    This is NOT the real CDC-ACM device. The result here says nothing certain about it.
    """

    def test_second_open_after_tiocexcl_on_pty(self):
        if os.geteuid() == 0:
            self.skipTest("TIOCEXCL does not stop root (CAP_SYS_ADMIN)")
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        path = os.ttyname(slave)
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        self.addCleanup(os.close, fd)

        # Control: before TIOCEXCL a second open works.
        os.close(os.open(path, os.O_RDWR | os.O_NOCTTY))

        apply_tiocexcl(fd)
        try:
            os.close(os.open(path, os.O_RDWR | os.O_NOCTTY))
            result = "SUCCEEDED"
        except OSError as exc:
            result = errno.errorcode.get(exc.errno, str(exc.errno))
        print(f"\n[pty TIOCEXCL observation] second open as non-root: {result}")
        self.assertEqual(result, "EBUSY")


if __name__ == "__main__":
    unittest.main()
