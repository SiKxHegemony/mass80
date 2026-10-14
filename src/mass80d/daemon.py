"""mass80d daemon entry point: CLI, wiring, logging, signals, replay mode, port guard.

Started as `python -m mass80d` (plan D1). Two independent switches (plan D2):

  --no-write    the `write` callable given to ReplyLayer only logs the frame.
  --no-actions  every system command goes through ActionGate, which only logs argv.

When a flag is ABSENT the capability is ON, and the startup banner says so loudly.
--replay implies both switches, skips the port guard and never opens a device node.

Nothing in this module has been run against the real device.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import signal
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, TextIO

from .actions import Actions, CommandRunner
from .config import Config, ConfigError, default_config, load_config
from .cover_service import CoverService
from .dispatcher import Dispatcher, pump
from .music_reader import MusicReader
from .parser import Event, EventParser
from .perf_reader import PerfReader
from .player_select import NO_PLAYER, ListResult, PlayerSelector
from .portguard import find_port_holders, make_serial_factory
from .replies import MAX_COVER_JPEG_BYTES, ReplyLayer
from .transport import DEFAULT_PORT, SerialTransport
from .volume_reader import UNREADABLE, VolumeReader, VolumeReading

log = logging.getLogger("mass80d.daemon")

EXIT_OK = 0
EXIT_PORT_BUSY = 2
EXIT_BAD_ARGS = 3

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_FRAME_MAX_BYTES = 512  # a longer frame (a cover) is logged as its first bytes only
LOG_FRAME_HEAD_BYTES = 120


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # argparse would exit 2; we reserve 2 for "port busy"
        raise _UsageError(message)


class _UsageError(Exception):
    pass


def build_arg_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="python -m mass80d", description="Mass80 screen-module daemon.")
    p.add_argument("--config", metavar="PATH", help="config TOML (explicit only; no search path)")
    p.add_argument("--port", metavar="PATH", default=DEFAULT_PORT, help="serial device path")
    p.add_argument(
        "--no-write",
        action="store_true",
        help="never write to the device; log the frame that would have been sent",
    )
    p.add_argument(
        "--no-actions",
        action="store_true",
        help="never run a system command; log the argv that would have been run",
    )
    p.add_argument(
        "--replay",
        metavar="FILE",
        help="feed a capture log (JSON lines, kind=rx with hex) through the parser and "
        "dispatcher; implies --no-write and --no-actions, skips the port guard, opens no device",
    )
    p.add_argument(
        "--log-level",
        type=str.upper,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="stderr log level (default INFO)",
    )
    p.add_argument("--print-config", action="store_true", help="print effective config and exit")
    p.add_argument(
        "--tiocexcl",
        action="store_true",
        help="also set TIOCEXCL on the port (OFF by default; NOT verified on the real device)",
    )
    p.add_argument(
        "--no-port-guard",
        action="store_true",
        help="FOR TESTS ONLY: skip the check for other processes holding the port",
    )
    return p


# --- the one switch for system side effects -----------------------------------------------
class ActionGate:
    """The single switch between "run it" and "only log it" for system commands.

    Actions gets this as its runner. T11's key sender MUST route its command (argv) through
    the same gate object, so that --no-actions also stops key injection.

    When disabled it runs nothing (no subprocess is touched) and returns True, i.e. it
    pretends success so callers do not retry in a loop.
    """

    def __init__(self, enabled: bool, runner: Callable[[list[str]], bool] | None = None) -> None:
        self.enabled = enabled
        self._runner = runner  # built lazily so a disabled gate never creates a real one
        self.suppressed: list[list[str]] = []

    def __call__(self, argv: list[str]) -> bool:
        if not self.enabled:
            self.suppressed.append(list(argv))
            log.info("actions disabled (--no-actions): would run %s", list(argv))
            return True
        if self._runner is None:
            self._runner = CommandRunner()
        log.info("running %s", list(argv))
        return self._runner(list(argv))


class WriteGate:
    """Switch for device writes; the ONLY thing handed to ReplyLayer as `write`.

    Enabled: forwards to transport.write. Disabled: logs the frame and never references
    the transport's write at all.
    """

    def __init__(self, enabled: bool, transport_write: Callable[[bytes], Any] | None) -> None:
        self.enabled = enabled
        self._send = transport_write if enabled else None
        self.suppressed: list[bytes] = []

    def __call__(self, frame: bytes) -> None:
        if self._send is None:
            self.suppressed.append(frame)
            if len(frame) > LOG_FRAME_MAX_BYTES:
                log.info(
                    "writes disabled (--no-write): would send %d bytes: %r ...",
                    len(frame),
                    frame[:LOG_FRAME_HEAD_BYTES],
                )
            else:
                log.info("writes disabled (--no-write): would send %r", frame)
            return
        if len(frame) > LOG_FRAME_MAX_BYTES:
            log.info("writing %d bytes: %r ...", len(frame), frame[:LOG_FRAME_HEAD_BYTES])
        else:
            log.info("writing %r", frame)
        self._send(frame)


class StopFlag:
    """Stop request set by signal handlers; wait() is an interruptible sleep."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def __call__(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()

    def wait(self, seconds: float) -> None:
        self._event.wait(seconds)

    def handler(self, signum: int, frame: Any = None) -> None:
        log.info("signal %s received, stopping", signum)
        self.set()


def _no_reader() -> VolumeReading:
    """Replay reader: replay starts no process, so the volume is always 'unreadable'."""
    return UNREADABLE


def _no_players() -> ListResult:
    """Replay listing: replay starts no process, so there is never a player."""
    return NO_PLAYER


# --- wiring -------------------------------------------------------------------------------
def build_dispatcher(
    config: Config,
    write: Callable[[bytes], Any],
    runner: Callable[[list[str]], bool],
    clock: Callable[[], float] = time.monotonic,
    now_func: Callable[[], datetime] | None = None,
    volume_reader: Callable[[], VolumeReading] | None = None,
    list_players: Callable[[], ListResult] | None = None,
    cover_service: CoverService | None = None,
    cover_test_jpeg: bytes | None = None,
    start_services: bool = True,
    performance_provider: Callable[[], Any] | None = None,
) -> Dispatcher:
    # ONE selector, shared by the reply layer (screen) and Actions (buttons), so they always
    # agree. Building it runs nothing: playerctl is only called when a poll or a button asks.
    # The listing is read-only (`playerctl -a metadata`), so it is NOT behind the ActionGate.
    selector = PlayerSelector(
        list_players or MusicReader().list_players, config.music_players, clock
    )
    actions = Actions(config, runner, clock, selector=selector)
    # The volume reader is read-only (`wpctl get-volume`) and only exists when enabled.
    if volume_reader is None and config.reply_volume_query:
        volume_reader = VolumeReader(config.volume_sink)
    # The cover worker thread exists only for a live run with get_cover on and no test image.
    if (
        cover_service is None
        and start_services
        and config.reply_get_cover
        and config.cover_test_image == ""
    ):
        cover_service = CoverService(config.cover_config())
        cover_service.start()
    # The performance reader reads /proc and /sys only; never built in replay.
    if performance_provider is None and start_services and config.reply_get_performance:
        performance_provider = PerfReader(interfaces=config.perf_interfaces)
    cover_kw: dict[str, Any] = {
        "cover_service": cover_service,
        "cover_test_jpeg": cover_test_jpeg,
        "performance_provider": performance_provider,
    }
    if now_func is None:
        replies = ReplyLayer(
            write,
            config,
            clock=clock,
            volume_reader=volume_reader,
            music_selector=selector,
            **cover_kw,
        )
    else:
        replies = ReplyLayer(
            write,
            config,
            clock=clock,
            now_func=now_func,
            volume_reader=volume_reader,
            music_selector=selector,
            **cover_kw,
        )
    return Dispatcher(actions, replies)


def _load_cover_test_image(path: str) -> bytes:
    """Read the stage 9b test picture. The path is RELATIVE TO THE CURRENT DIRECTORY (the stage
    commands run from the repo root). Reads at most MAX_COVER_JPEG_BYTES + 1 bytes; raises
    ConfigError if it is not a JPEG (FF D8 FF) or is over the cap, OSError if unreadable."""
    with open(path, "rb") as fh:
        data = fh.read(MAX_COVER_JPEG_BYTES + 1)
    if data[:3] != b"\xff\xd8\xff":
        raise ConfigError(f"{path} is not a JPEG (no FF D8 FF)")
    if len(data) > MAX_COVER_JPEG_BYTES:
        raise ConfigError(f"{path} is larger than {MAX_COVER_JPEG_BYTES} bytes")
    return data


def run(
    config: Config,
    transport: Any,
    should_stop: Callable[[], bool],
    *,
    write_enabled: bool,
    actions_enabled: bool,
    clock: Callable[[], float] = time.monotonic,
    now_func: Callable[[], datetime] | None = None,
    action_runner: Callable[[list[str]], bool] | None = None,
    volume_reader: Callable[[], VolumeReading] | None = None,
    list_players: Callable[[], ListResult] | None = None,
    cover_service: CoverService | None = None,
    cover_test_jpeg: bytes | None = None,
) -> int:
    """Wire the layers together and pump until should_stop(). Always closes the transport
    (and first the cover worker, if any)."""
    write = WriteGate(write_enabled, transport.write if write_enabled else None)
    gate = ActionGate(actions_enabled, action_runner)
    dispatcher = build_dispatcher(
        config,
        write,
        gate,
        clock,
        now_func,
        volume_reader,
        list_players,
        cover_service=cover_service,
        cover_test_jpeg=cover_test_jpeg,
    )
    try:
        pump(transport, dispatcher, should_stop)
    finally:
        try:
            dispatcher.close()
        finally:
            transport.close()
    return EXIT_OK


# --- replay -------------------------------------------------------------------------------
class ReplayTransport:
    """Feeds the rx chunks of a capture log through the real EventParser.

    Opens no device. write() must never be called (it raises), and `now` follows the
    capture's own timestamps so rate limits behave as they did when it was recorded.
    """

    def __init__(self, lines: Sequence[str]) -> None:
        self.parser = EventParser()
        self.now = 0.0
        self.events_by_action: Counter[str] = Counter()
        self.skipped_lines = 0
        self._chunks: list[tuple[float | None, bytes]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if not isinstance(rec, dict) or rec.get("kind") != "rx":
                    continue
                data = bytes.fromhex(rec["hex"])
            except ValueError, KeyError, TypeError:
                self.skipped_lines += 1
                continue
            t = rec.get("t")
            ts = float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) else None
            self._chunks.append((ts, data))
        self._i = 0
        if self._chunks and self._chunks[0][0] is not None:
            self.now = float(self._chunks[0][0])

    @classmethod
    def from_file(cls, path: str) -> ReplayTransport:
        with open(path, encoding="utf-8") as fh:
            return cls(fh.read().splitlines())

    @property
    def done(self) -> bool:
        return self._i >= len(self._chunks)

    def clock(self) -> float:
        return self.now

    def read_events(self) -> list[Event]:
        if self.done:
            return []
        ts, data = self._chunks[self._i]
        self._i += 1
        if ts is not None:
            self.now = ts
        events = self.parser.feed(data)
        for e in events:
            self.events_by_action[e.action or "<none>"] += 1
        return events

    def write(self, data: bytes) -> None:
        raise RuntimeError("replay transport must never be written to")

    def close(self) -> None:
        pass


def replay_summary(rt: ReplayTransport) -> str:
    lines = [f"replay summary: {sum(rt.events_by_action.values())} events"]
    for action, n in sorted(rt.events_by_action.items()):
        lines.append(f"  {action}: {n}")
    if rt.skipped_lines:
        lines.append(f"  (skipped {rt.skipped_lines} unreadable record(s))")
    return "\n".join(lines)


def run_replay(config: Config, path: str, out: TextIO, cover_test_jpeg: bytes | None = None) -> int:
    rt = ReplayTransport.from_file(path)
    if cover_test_jpeg is None and config.reply_get_cover and config.cover_test_image:
        cover_test_jpeg = _load_cover_test_image(config.cover_test_image)
    write = WriteGate(False, None)
    gate = ActionGate(False)
    # Replay starts no process and no thread: no volume reader, no cover worker.
    dispatcher = build_dispatcher(
        config,
        write,
        gate,
        rt.clock,
        lambda: datetime.fromtimestamp(rt.now).astimezone(),
        volume_reader=_no_reader,
        list_players=_no_players,
        cover_test_jpeg=cover_test_jpeg,
        start_services=False,
    )
    try:
        pump(rt, dispatcher, lambda: rt.done)
    finally:
        dispatcher.close()
    print(replay_summary(rt), file=out)
    print(
        f"  would-write frames: {len(write.suppressed)}, would-run commands: "
        f"{len(gate.suppressed)}",
        file=out,
    )
    return EXIT_OK


# --- main ---------------------------------------------------------------------------------
def banner(
    config_source: str,
    port: str,
    write_on: bool,
    actions_on: bool,
    silence: float,
    mode: str,
    volume_query: bool = False,
    music_info: bool = False,
    music_players: Sequence[str] = (),
    cover: bool = False,
    cover_size: int = 240,
    cover_hosts: Sequence[str] = (),
    cover_local_dirs: Sequence[str] = (),
    cover_test_image: str = "",
    cover_test_max_replies: int = 1,
    cover_canvas_width: int = 240,
    cover_canvas_height: int = 240,
    cover_fill_grey: int = 32,
    performance: bool = False,
    performance_interfaces: Sequence[str] = (),
    performance_max_replies: int = 0,
) -> list[str]:
    writes = "ON  <-- frames WILL be sent" if write_on else "off (--no-write)"
    actions = "ON  <-- commands WILL run" if actions_on else "off (--no-actions)"
    lines = [
        f"mass80d starting ({mode})",
        f"  config:  {config_source}",
        f"  port:    {port}",
        f"  WRITES to device:   {writes}",
        f"  system ACTIONS:     {actions}",
        f"  silence timeout: {silence} s",
    ]
    if volume_query:
        lines.append("  volume query reads: wpctl get-volume (read-only)")
    if music_info:
        lines.append("  music info reads: playerctl metadata (read-only)")
        lines.append(f"  music players: {' > '.join(music_players)} (playing first, unlisted last)")
    if cover:
        if cover_test_image:
            lines.append(
                f"  cover art: TEST IMAGE {cover_test_image}, answers at most "
                f"{cover_test_max_replies} request(s), no network"
            )
        else:
            dirs = ", ".join(cover_local_dirs) if cover_local_dirs else "(none)"
            if (cover_canvas_width, cover_canvas_height) == (cover_size, cover_size):
                shape = f"size {cover_size}x{cover_size}"
            else:
                shape = (
                    f"size {cover_size}x{cover_size} on a "
                    f"{cover_canvas_width}x{cover_canvas_height} canvas (grey {cover_fill_grey})"
                )
            lines.append(
                f"  cover art: ON, {shape}, fetch https from "
                f"{', '.join(cover_hosts)}  (EXTERNAL network request), local files only "
                f"from {dirs}"
            )
    if performance:
        names = ", ".join(performance_interfaces) if performance_interfaces else "default route"
        limit = (
            f"{performance_max_replies} frame(s) at most"
            if performance_max_replies
            else "unlimited"
        )
        lines.append(
            f"  performance: ON, reads /proc and /sys (read-only), interface {names}, {limit}"
        )
    return lines


def _configure_logging(level: str) -> Callable[[], None]:
    logger = logging.getLogger("mass80d")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(level)

    def undo() -> None:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    return undo


def main(
    argv: Sequence[str] | None = None,
    *,
    transport_factory: Callable[..., Any] | None = None,
    find_holders: Callable[[str], list[tuple[int, str]]] = find_port_holders,
    install_signals: bool = True,
    stop: StopFlag | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        args = build_arg_parser().parse_args(argv)
    except _UsageError as exc:
        print(f"mass80d: bad arguments: {exc}", file=err)
        return EXIT_BAD_ARGS
    except SystemExit as exc:  # --help
        return int(exc.code or 0)

    try:
        config = load_config(args.config) if args.config else default_config()
    except (ConfigError, OSError) as exc:
        print(f"mass80d: bad config: {exc}", file=err)
        return EXIT_BAD_ARGS
    source = args.config if args.config else "built-in defaults"

    replay = args.replay is not None
    write_on = not (args.no_write or replay)
    actions_on = not (args.no_actions or replay)

    if args.print_config:
        doc = {
            "config_source": source,
            "port": args.port,
            "writes_enabled": write_on,
            "actions_enabled": actions_on,
            "replay": args.replay,
            "config": dataclasses.asdict(config),
        }
        print(json.dumps(doc, indent=2), file=out)
        return EXIT_OK

    # The stage 9b test picture is read (and checked) BEFORE the port is touched.
    test_jpeg: bytes | None = None
    if config.reply_get_cover and config.cover_test_image:
        try:
            test_jpeg = _load_cover_test_image(config.cover_test_image)
        except (ConfigError, OSError) as exc:
            print(f"mass80d: bad cover test image: {exc}", file=err)
            return EXIT_BAD_ARGS

    undo_logging = _configure_logging(args.log_level)
    try:
        for line in banner(
            source,
            "(none: replay)" if replay else args.port,
            write_on,
            actions_on,
            config.serial_silence_timeout,
            "REPLAY" if replay else "live",
            config.reply_volume_query and not replay,
            config.reply_get_music_info and not replay,
            config.music_players,
            cover=config.reply_get_cover and not replay,
            cover_size=config.cover_size,
            cover_hosts=config.cover_hosts,
            cover_local_dirs=config.cover_local_dirs,
            cover_test_image=config.cover_test_image,
            cover_test_max_replies=config.cover_test_max_replies,
            cover_canvas_width=config.cover_canvas_width,
            cover_canvas_height=config.cover_canvas_height,
            cover_fill_grey=config.cover_fill_grey,
            performance=config.reply_get_performance and not replay,
            performance_interfaces=config.perf_interfaces,
            performance_max_replies=config.perf_max_replies,
        ):
            log.info("%s", line)

        if replay:
            try:
                return run_replay(config, args.replay, out, cover_test_jpeg=test_jpeg)
            except OSError as exc:
                print(f"mass80d: cannot read replay file: {exc}", file=err)
                return EXIT_BAD_ARGS

        if not args.no_port_guard:
            holders = find_holders(args.port)
            if holders:
                for pid, cmd in holders:
                    print(f"mass80d: port {args.port} is held by pid {pid}: {cmd}", file=err)
                print("mass80d: refusing to start; stop that process first", file=err)
                return EXIT_PORT_BUSY

        flag = stop or StopFlag()
        factory = transport_factory or SerialTransport
        transport = factory(
            port=args.port,
            serial_factory=make_serial_factory(tiocexcl=args.tiocexcl),
            sleep=flag.wait,
            silence_timeout=config.serial_silence_timeout,
        )
        old_handlers: dict[int, Any] = {}
        if install_signals:
            for sig in (signal.SIGINT, signal.SIGTERM):
                old_handlers[sig] = signal.signal(sig, flag.handler)
        try:
            return run(
                config,
                transport,
                flag,
                write_enabled=write_on,
                actions_enabled=actions_on,
                cover_test_jpeg=test_jpeg,
            )
        finally:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
    finally:
        undo_logging()
