"""Serial-port guard (plan D6): refuse to start when another process holds the port.

Three layers, each with limits:

1. find_port_holders(): scan <proc_root>/*/fd for a process that has the resolved
   device open (what `fuser` does). This is the only layer that catches clients that
   open the port WITHOUT asking for exclusivity (~/mass80_volume.py, tools/capture.py).
   Limits: it cannot see other users' processes (their /proc/<pid>/fd is unreadable),
   and there is a short race between the scan and the open.
2. pyserial `exclusive=True`: an ADVISORY flock(LOCK_EX | LOCK_NB). It only excludes
   other clients that also ask for exclusive access (e.g. a second copy of this daemon).
   It does not stop, and is not seen by, a plain open().
3. TIOCEXCL (opt-in, `--tiocexcl`, default OFF): a kernel flag meant to make later
   non-root open() calls fail with EBUSY. NOT verified on the real CDC-ACM device; it
   is only checked against a pty in the unit tests.
"""

from __future__ import annotations

import fcntl
import os
import termios
from collections.abc import Callable
from typing import Any

from .transport import WRITE_TIMEOUT_S


def apply_tiocexcl(fd: int, ioctl: Callable[[int, int], Any] = fcntl.ioctl) -> None:
    """Set TIOCEXCL on an open tty file descriptor. Raises OSError on failure."""
    ioctl(fd, termios.TIOCEXCL)


def make_serial_factory(
    tiocexcl: bool = False,
    serial_cls: Callable[..., Any] | None = None,
    ioctl: Callable[[int, int], Any] = fcntl.ioctl,
) -> Callable[..., Any]:
    """Return a SerialTransport-compatible factory(port, baudrate, timeout).

    The port is always opened with exclusive=True (layer 2). With tiocexcl=True the
    TIOCEXCL ioctl is applied after the open (layer 3); if it fails the port is closed
    and the OSError propagates, so the transport treats it as a failed connect.
    `serial_cls` and `ioctl` are injectable so tests never touch a device.
    """

    def factory(port: str, baudrate: int, timeout: float) -> Any:
        cls = serial_cls
        if cls is None:
            import serial  # deferred, as in transport._default_factory

            cls = serial.Serial
        ser = cls(port, baudrate, timeout=timeout, write_timeout=WRITE_TIMEOUT_S, exclusive=True)
        if tiocexcl:
            try:
                apply_tiocexcl(ser.fileno(), ioctl)
            except OSError:
                ser.close()
                raise
        return ser

    return factory


def _cmdline(proc_root: str, pid: int) -> str:
    try:
        with open(os.path.join(proc_root, str(pid), "cmdline"), "rb") as fh:
            raw = fh.read()
    except OSError:
        return "<cmdline unreadable>"
    text = raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
    return text or "<no cmdline>"


def find_port_holders(
    device_path: str, proc_root: str = "/proc", own_pid: int | None = None
) -> list[tuple[int, str]]:
    """Return [(pid, cmdline)] of other processes that have device_path open.

    The path is resolved with os.path.realpath (by-id links are symlinks to the real node).
    Processes that vanish or whose fd directory is not readable are skipped.
    """
    target = os.path.realpath(device_path)
    me = os.getpid() if own_pid is None else own_pid
    holders: list[tuple[int, str]] = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return holders
    for name in sorted(entries, key=lambda n: (not n.isdigit(), int(n) if n.isdigit() else 0)):
        if not name.isdigit() or int(name) == me:
            continue
        pid = int(name)
        fd_dir = os.path.join(proc_root, name, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:  # vanished, PermissionError (other user), or not a process
            continue
        for fd in fds:
            try:
                link = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if link.startswith("/") and os.path.realpath(link) == target:
                holders.append((pid, _cmdline(proc_root, pid)))
                break
    return holders
