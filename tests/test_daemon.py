import contextlib
import io
import json
import logging
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mass80d import daemon
from mass80d.config import default_config, load_config_text
from mass80d.daemon import (
    EXIT_BAD_ARGS,
    EXIT_OK,
    EXIT_PORT_BUSY,
    ActionGate,
    ReplayTransport,
    StopFlag,
    WriteGate,
    main,
    run,
)
from mass80d.parser import KIND_INPUT, KIND_POLL, Event
from mass80d.transport import DEFAULT_PORT, SerialTransport
from mass80d.volume_reader import VolumeReading

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPLAY = str(FIXTURES / "replay_excerpt_001911.log")


class FakeTransport:
    """Hands out scripted event batches, then asks the loop to stop."""

    def __init__(self, batches, stop=None):
        self.batches = list(batches)
        self.stop = stop
        self.closed = False
        self.write_calls = []

    def read_events(self):
        if self.batches:
            return self.batches.pop(0)
        if self.stop is not None:
            self.stop.set()
        return []

    def write(self, data):
        self.write_calls.append(data)
        raise AssertionError("SerialTransport.write must not be reached in this mode")

    def close(self):
        self.closed = True


class RecordingTransport(FakeTransport):
    def write(self, data):
        self.write_calls.append(data)


def poll(action="get_time"):
    return Event(KIND_POLL, action)


def run_main(argv, **kw):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(io.StringIO()):
        code = main(argv, out=out, err=err, install_signals=False, **kw)
    return code, out.getvalue(), err.getvalue()


class NoRealWorld(unittest.TestCase):
    """Fail loudly if anything reaches a real subprocess or a real serial open."""

    def setUp(self):
        def boom(*a, **k):
            raise AssertionError("real subprocess or serial open reached")

        for target in (
            "subprocess.run",
            "subprocess.Popen",
            "subprocess.call",
            "serial.Serial",
            "os.system",
        ):
            p = mock.patch(target, side_effect=boom)
            p.start()
            self.addCleanup(p.stop)


class GateTests(NoRealWorld):
    def test_action_gate_disabled_runs_nothing_and_logs_argv(self):
        gate = ActionGate(False)
        with self.assertLogs("mass80d.daemon", "INFO") as cm:
            ok = gate(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.5"])
        self.assertTrue(ok)
        self.assertEqual(gate.suppressed, [["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.5"]])
        self.assertIn("would run", cm.output[0])
        self.assertIn("wpctl", cm.output[0])

    def test_action_gate_enabled_uses_runner(self):
        seen = []
        gate = ActionGate(True, lambda argv: seen.append(argv) or True)
        self.assertTrue(gate(["x", "y"]))
        self.assertEqual(seen, [["x", "y"]])

    def test_write_gate_disabled_never_touches_transport_write(self):
        t = FakeTransport([])
        gate = WriteGate(False, t.write)
        with self.assertLogs("mass80d.daemon", "INFO") as cm:
            gate(b'{"action":"get_time"}\n')
        self.assertEqual(t.write_calls, [])
        self.assertIn("would send", cm.output[0])

    def test_write_gate_enabled_forwards(self):
        t = RecordingTransport([])
        WriteGate(True, t.write)(b"abc\n")
        self.assertEqual(t.write_calls, [b"abc\n"])


class RunWiringTests(NoRealWorld):
    def test_no_write_means_transport_write_never_called(self):
        stop = StopFlag()
        t = FakeTransport([[poll()], [poll()]], stop)
        run(
            default_config(),
            t,
            stop,
            write_enabled=False,
            actions_enabled=False,
        )
        self.assertEqual(t.write_calls, [])
        self.assertTrue(t.closed)

    def test_write_enabled_sends_get_time_through_transport(self):
        stop = StopFlag()
        t = RecordingTransport([[poll()]], stop)
        run(default_config(), t, stop, write_enabled=True, actions_enabled=False)
        self.assertEqual(len(t.write_calls), 1)
        self.assertEqual(json.loads(t.write_calls[0])["action"], "get_time")

    def test_no_actions_runs_no_subprocess(self):
        stop = StopFlag()
        events = [Event(KIND_INPUT, "volume", value=30), Event(KIND_INPUT, "music_play")]
        t = FakeTransport([events], stop)
        # NoRealWorld makes any subprocess call raise AssertionError; Dispatcher would log
        # that, so also check the gate saw the commands.
        with mock.patch.object(daemon.ActionGate, "__call__", autospec=True) as gate_call:
            gate_call.return_value = True
            # T18: music buttons ask the selector; inject a one-player listing (no process).
            run(
                default_config(),
                t,
                stop,
                write_enabled=False,
                actions_enabled=False,
                list_players=lambda: PlayerListing((_pi("spotify"),)),
            )
        argvs = [c.args[1] for c in gate_call.call_args_list]
        self.assertIn("wpctl", [a[0] for a in argvs])
        self.assertIn("playerctl", [a[0] for a in argvs])

    def test_no_actions_logs_would_run_for_prev_next_and_runs_nothing(self):
        stop = StopFlag()
        events = [Event(KIND_INPUT, "music_prev"), Event(KIND_INPUT, "music_next")]
        t = FakeTransport([events], stop)
        seen = []
        with self.assertLogs("mass80d.daemon", "INFO") as cm:
            run(
                default_config(),
                t,
                stop,
                write_enabled=False,
                actions_enabled=False,
                action_runner=lambda argv: seen.append(argv) or True,
                list_players=lambda: PlayerListing((_pi("spotify"),)),  # T18
            )
        out = "\n".join(cm.output)
        self.assertIn("would run ['playerctl', '-p', 'spotify', 'previous']", out)
        self.assertIn("would run ['playerctl', '-p', 'spotify', 'next']", out)
        self.assertEqual(seen, [])

    def test_actions_enabled_uses_injected_runner(self):
        stop = StopFlag()
        seen = []
        t = FakeTransport([[Event(KIND_INPUT, "volume", value=100)]], stop)
        run(
            load_config_text("[volume]\nmax_fraction = 1.0\n"),
            t,
            stop,
            write_enabled=False,
            actions_enabled=True,
            action_runner=lambda argv: seen.append(argv) or True,
        )
        self.assertEqual(seen, [["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "1.0"]])

    def test_transport_closed_even_if_pump_raises(self):
        class Boom(FakeTransport):
            def read_events(self):
                raise KeyboardInterrupt

        t = Boom([])
        with self.assertRaises(KeyboardInterrupt):
            run(default_config(), t, lambda: False, write_enabled=False, actions_enabled=False)
        self.assertTrue(t.closed)

    def test_bad_line_from_device_does_not_stop_the_loop(self):
        """B1 at daemon level: nested JSON and garbage go through the real transport + pump."""
        deep = b"{" + b'"a":{' * 3000 + b"}" * 3000 + b"}\n"
        good = b'{"action":"send_key","key":"Print"}\n'
        chunks = [b"\xff\xfe garbage {not json\n", deep, good, b""]

        class FakeSerial:
            in_waiting = 0

            def __init__(self):
                self.i = 0

            def read(self, n):
                if self.i < len(chunks):
                    self.i += 1
                    return chunks[self.i - 1]
                stop.set()
                return b""

            def close(self):
                pass

        stop = StopFlag()
        tr = SerialTransport(serial_factory=lambda *a, **k: FakeSerial(), sleep=lambda s: None)
        with self.assertLogs("mass80d.actions", "INFO") as cm:
            run(default_config(), tr, stop, write_enabled=False, actions_enabled=False)
        self.assertTrue(any("send_key 'Print'" in line for line in cm.output))


class VolumeQueryDaemonTests(NoRealWorld):
    CFG = "[volume]\nmax_fraction = 0.3\n[replies]\nvolume_query = true\n"

    def reader(self):
        self.reads = getattr(self, "reads", 0) + 1
        return VolumeReading(0.30, False)

    def test_no_write_logs_frame_and_never_writes(self):
        stop = StopFlag()
        t = FakeTransport([[Event(KIND_INPUT, "volume")]], stop)
        with self.assertLogs("mass80d.daemon", "INFO") as cm:
            run(
                load_config_text(self.CFG),
                t,
                stop,
                write_enabled=False,
                actions_enabled=False,
                volume_reader=self.reader,
            )
        self.assertEqual(t.write_calls, [])
        text = "\n".join(cm.output)
        self.assertIn("would send", text)
        self.assertIn("query", text)

    def test_no_write_frame_is_recorded_by_gate(self):
        write = WriteGate(False, None)
        d = daemon.build_dispatcher(
            load_config_text(self.CFG), write, ActionGate(False), volume_reader=self.reader
        )
        d.handle(Event(KIND_INPUT, "volume"))
        self.assertEqual(write.suppressed, [b'{"query":"volume","result":"ok","value":100}\n'])

    def test_write_enabled_sends_the_frame_through_transport(self):
        stop = StopFlag()
        t = RecordingTransport([[Event(KIND_INPUT, "volume")]], stop)
        run(
            load_config_text(self.CFG),
            t,
            stop,
            write_enabled=True,
            actions_enabled=False,
            volume_reader=self.reader,
        )
        self.assertEqual(t.write_calls, [b'{"query":"volume","result":"ok","value":100}\n'])

    def test_no_actions_still_reads_but_runs_no_set_volume(self):
        stop = StopFlag()
        events = [Event(KIND_INPUT, "volume"), Event(KIND_INPUT, "volume", value=100)]
        t = FakeTransport([events], stop)
        self.reads = 0
        gate_runs = []
        with mock.patch.object(daemon.ActionGate, "__call__", autospec=True) as gate_call:
            gate_call.side_effect = lambda self_, argv: gate_runs.append(list(argv)) or True
            run(
                load_config_text(self.CFG),
                t,
                stop,
                write_enabled=False,
                actions_enabled=False,
                volume_reader=self.reader,
            )
        self.assertEqual(self.reads, 1)  # the read-only reader ran (Q1)
        # set-volume went to the gate (which, disabled, only logs); the reader never did
        self.assertEqual(gate_runs, [["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.3"]])
        self.assertFalse(any("get-volume" in a for argv in gate_runs for a in argv))

    def test_default_reader_is_the_volume_reader_on_the_configured_sink(self):
        cfg = load_config_text(self.CFG)
        d = daemon.build_dispatcher(cfg, WriteGate(False, None), ActionGate(False))
        calls = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, b"Volume: 0.15\n", b"")

        with mock.patch("mass80d.volume_reader.subprocess.run", fake_run):
            d.handle(Event(KIND_INPUT, "volume"))
        self.assertEqual(calls, [["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]])

    def test_reader_not_built_or_run_when_query_disabled(self):
        d = daemon.build_dispatcher(default_config(), WriteGate(False, None), ActionGate(False))
        with mock.patch("mass80d.volume_reader.subprocess.run") as m:
            d.handle(Event(KIND_INPUT, "volume"))
        m.assert_not_called()

    def test_banner_line_only_when_query_on(self):
        on = daemon.banner("x", "p", False, False, 10.0, "live", volume_query=True)
        off = daemon.banner("x", "p", False, False, 10.0, "live")
        line = "  volume query reads: wpctl get-volume (read-only)"
        self.assertIn(line, on)
        self.assertNotIn(line, off)

    def test_main_logs_banner_line_and_print_config_exposes_key(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.toml")
            Path(p).write_text(self.CFG)
            stop = StopFlag()
            t = FakeTransport([], stop)
            with self.assertLogs("mass80d.daemon", "INFO") as cm:
                main(
                    ["--config", p, "--no-write", "--no-actions", "--no-port-guard"],
                    transport_factory=lambda **kw: t,
                    stop=stop,
                    install_signals=False,
                    out=io.StringIO(),
                    err=io.StringIO(),
                )
            self.assertIn("volume query reads: wpctl get-volume (read-only)", "\n".join(cm.output))
            code, out, _ = run_main(["--config", p, "--print-config"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["config"]["reply_volume_query"])

    def test_stage0_replay_with_stage3b_config_writes_nothing_and_runs_nothing(self):
        cfg_path = str(
            Path(__file__).resolve().parent.parent / "configs" / "stage3b-volume-query.toml"
        )
        with mock.patch("mass80d.volume_reader.subprocess.run") as m:
            code, out, err = run_main(["--replay", REPLAY, "--config", cfg_path])
        m.assert_not_called()  # replay never starts a process, not even the read-only one
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(err, "")
        self.assertIn("replay summary:", out)
        self.assertIn("would-write frames: ", out)


class MusicInfoDaemonTests(NoRealWorld):
    CFG = "[replies]\nreply_get_music_info = true\n"
    EMPTY = (
        b'{"action":"get_music_info","artist":"","duration":0,"index":0,'
        b'"progress":0,"state":0,"title":""}\n'
    )

    def test_default_reader_runs_playerctl_metadata_read_only(self):
        d = daemon.build_dispatcher(
            load_config_text(self.CFG), WriteGate(False, None), ActionGate(False)
        )
        calls = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, b"No players found\n", b"")

        with (
            mock.patch("mass80d.music_reader.subprocess.run", fake_run),
            self.assertLogs("mass80d", "INFO"),
        ):
            d.handle(poll("get_music_info"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:4], ["playerctl", "-a", "metadata", "--format"])  # T18: -a

    def test_no_reader_when_option_off(self):
        d = daemon.build_dispatcher(default_config(), WriteGate(False, None), ActionGate(False))
        with mock.patch("mass80d.music_reader.subprocess.run") as m:
            d.handle(poll("get_music_info"))
        m.assert_not_called()

    def test_no_actions_still_reads_but_runs_no_command(self):
        stop = StopFlag()
        t = FakeTransport([[poll("get_music_info")]], stop)
        gate_runs = []
        write = []

        def fake_run(argv, **kw):
            write.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, b"No players found\n", b"")

        with (
            mock.patch("mass80d.music_reader.subprocess.run", fake_run),
            mock.patch.object(daemon.ActionGate, "__call__", autospec=True) as gate_call,
            self.assertLogs("mass80d", "INFO") as cm,
        ):
            gate_call.side_effect = lambda self_, argv: gate_runs.append(list(argv)) or True
            run(
                load_config_text(self.CFG),
                t,
                stop,
                write_enabled=False,
                actions_enabled=False,
            )
        self.assertEqual(len(write), 1)  # the read-only reader ran
        self.assertEqual(gate_runs, [])  # no command went to the action gate
        self.assertEqual(t.write_calls, [])
        self.assertIn("would send", "\n".join(cm.output))

    def test_banner_line_only_when_option_on(self):
        on = daemon.banner("x", "p", False, False, 10.0, "live", music_info=True)
        off = daemon.banner("x", "p", False, False, 10.0, "live")
        line = "  music info reads: playerctl metadata (read-only)"
        self.assertIn(line, on)
        self.assertNotIn(line, off)

    def test_main_logs_banner_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.toml")
            Path(p).write_text(self.CFG)
            stop = StopFlag()
            t = FakeTransport([], stop)
            with self.assertLogs("mass80d.daemon", "INFO") as cm:
                main(
                    ["--config", p, "--no-write", "--no-actions", "--no-port-guard"],
                    transport_factory=lambda **kw: t,
                    stop=stop,
                    install_signals=False,
                    out=io.StringIO(),
                    err=io.StringIO(),
                )
            self.assertIn("music info reads: playerctl metadata (read-only)", "\n".join(cm.output))

    def test_replay_logs_would_send_empty_frames_and_starts_no_process(self):
        cfg_path = str(
            Path(__file__).resolve().parent.parent / "configs" / "stage6a-music-info.toml"
        )
        with (
            mock.patch("mass80d.music_reader.subprocess.run") as m,
            self.assertLogs("mass80d", "INFO") as cm,
        ):
            code, out, err = run_main(["--replay", REPLAY, "--config", cfg_path])
        m.assert_not_called()
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(err, "")
        sent = [line for line in cm.output if "would send" in line and "get_music_info" in line]
        self.assertTrue(sent)
        for line in sent:
            self.assertIn(repr(self.EMPTY), line)
        self.assertNotIn("music info reads", "\n".join(cm.output))  # banner: not live


# --- T18: one shared selector, banner, replay and --no-actions ------------------------------
# firefox.instance_1_53 is a SYNTHETIC instance name (not observed on this machine).
from mass80d.player_select import NO_PLAYER, PlayerInfo, PlayerListing  # noqa: E402


def _pi(instance, status="Playing"):
    return PlayerInfo(instance, instance.split(".")[0], status, "A", "T", 1000, 0)


class PlayerSelectorDaemonTests(NoRealWorld):
    CFG = "[replies]\nreply_get_music_info = true\n"
    TWO = PlayerListing((_pi("firefox.instance_1_53", "Paused"), _pi("spotify", "Playing")))

    def test_one_selector_is_shared_by_reply_layer_and_actions(self):
        d = daemon.build_dispatcher(
            load_config_text(self.CFG), WriteGate(False, None), ActionGate(False)
        )
        self.assertIsNotNone(d._actions._selector)
        self.assertIs(d._actions._selector, d._replies._music_selector)

    def test_selector_is_shared_even_when_the_music_reply_is_off(self):
        d = daemon.build_dispatcher(default_config(), WriteGate(False, None), ActionGate(False))
        self.assertIs(d._actions._selector, d._replies._music_selector)
        self.assertIsNotNone(d._actions._selector)

    def test_selector_gets_the_configured_priority(self):
        cfg = load_config_text('[music]\nplayers = ["mpv", "firefox"]\n')
        d = daemon.build_dispatcher(cfg, WriteGate(False, None), ActionGate(False))
        self.assertEqual(d._actions._selector.priority, ("mpv", "firefox"))

    def test_building_runs_nothing(self):
        with mock.patch("mass80d.music_reader.subprocess.run") as m:
            daemon.build_dispatcher(
                load_config_text(self.CFG), WriteGate(False, None), ActionGate(False)
            )
        m.assert_not_called()

    def test_default_listing_is_one_playerctl_a_metadata_call_on_a_poll(self):
        d = daemon.build_dispatcher(
            load_config_text(self.CFG), WriteGate(False, None), ActionGate(False)
        )
        calls = []

        def fake_run(argv, **kw):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 1, b"", b"No players found\n")

        with (
            mock.patch("mass80d.music_reader.subprocess.run", fake_run),
            self.assertLogs("mass80d", "INFO"),
        ):
            d.handle(poll("get_music_info"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:4], ["playerctl", "-a", "metadata", "--format"])

    def test_no_music_poll_and_no_button_means_no_process(self):
        d = daemon.build_dispatcher(default_config(), WriteGate(False, None), ActionGate(False))
        with mock.patch("mass80d.music_reader.subprocess.run") as m:
            d.handle(poll("get_music_info"))
            d.handle(poll("get_time"))
        m.assert_not_called()

    def test_no_actions_still_lists_but_runs_no_command(self):
        stop = StopFlag()
        t = FakeTransport([[Event(KIND_INPUT, "music_next")]], stop)
        gate_runs = []
        listed = []

        def provider():
            listed.append(1)
            return self.TWO

        with mock.patch.object(daemon.ActionGate, "__call__", autospec=True) as gate_call:
            gate_call.side_effect = lambda self_, argv: gate_runs.append(list(argv)) or True
            run(
                load_config_text(self.CFG),
                t,
                stop,
                write_enabled=False,
                actions_enabled=False,
                list_players=provider,
            )
        self.assertEqual(listed, [1])  # the read-only listing ran
        self.assertEqual(gate_runs, [["playerctl", "-p", "spotify", "next"]])  # gate only logs

    def test_no_actions_gate_disabled_logs_would_run_with_p(self):
        stop = StopFlag()
        t = FakeTransport([[Event(KIND_INPUT, "music_pause")]], stop)
        seen = []
        with self.assertLogs("mass80d.daemon", "INFO") as cm:
            run(
                default_config(),
                t,
                stop,
                write_enabled=False,
                actions_enabled=False,
                action_runner=lambda argv: seen.append(argv) or True,
                list_players=lambda: self.TWO,
            )
        self.assertIn("would run ['playerctl', '-p', 'spotify', 'pause']", "\n".join(cm.output))
        self.assertEqual(seen, [])

    def test_banner_player_line_only_when_music_reply_on(self):
        on = daemon.banner(
            "x",
            "p",
            False,
            False,
            10.0,
            "live",
            music_info=True,
            music_players=("spotify", "firefox"),
        )
        off = daemon.banner("x", "p", False, False, 10.0, "live", music_players=("spotify",))
        line = "  music players: spotify > firefox (playing first, unlisted last)"
        self.assertIn(line, on)
        self.assertFalse([entry for entry in off if "music players" in entry])

    def test_banner_shows_the_configured_list(self):
        lines = daemon.banner(
            "x", "p", False, False, 10.0, "live", music_info=True, music_players=("mpv", "vlc", "a")
        )
        self.assertIn("  music players: mpv > vlc > a (playing first, unlisted last)", lines)

    def test_main_logs_the_player_banner_line_from_the_config(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.toml")
            Path(p).write_text(self.CFG + '[music]\nplayers = ["mpv", "firefox"]\n')
            stop = StopFlag()
            t = FakeTransport([], stop)
            with self.assertLogs("mass80d.daemon", "INFO") as cm:
                main(
                    ["--config", p, "--no-write", "--no-actions", "--no-port-guard"],
                    transport_factory=lambda **kw: t,
                    stop=stop,
                    install_signals=False,
                    out=io.StringIO(),
                    err=io.StringIO(),
                )
        self.assertIn(
            "music players: mpv > firefox (playing first, unlisted last)", "\n".join(cm.output)
        )

    def test_replay_starts_no_process_and_buttons_run_nothing(self):
        cfg_path = str(Path(__file__).resolve().parent.parent / "configs" / "stage7-players.toml")
        with (
            mock.patch("mass80d.music_reader.subprocess.run") as m,
            self.assertLogs("mass80d", "INFO") as cm,
        ):
            code, out, err = run_main(["--replay", REPLAY, "--config", cfg_path])
        m.assert_not_called()
        self.assertEqual((code, err), (EXIT_OK, ""))
        self.assertNotIn("would run ['playerctl'", "\n".join(cm.output))
        sent = [x for x in cm.output if "would send" in x and "get_music_info" in x]
        self.assertTrue(sent)

    def test_replay_selector_provider_returns_no_player(self):
        self.assertIs(daemon._no_players(), NO_PLAYER)


class SignalTests(NoRealWorld):
    def test_handler_sets_flag_and_loop_exits_and_closes(self):
        stop = StopFlag()
        self.assertFalse(stop())

        class T(FakeTransport):
            def read_events(self):
                stop.handler(signal.SIGTERM)  # call the handler directly; no real signal
                return []

        t = T([])
        with self.assertLogs("mass80d.daemon", "INFO"):
            code = run(default_config(), t, stop, write_enabled=False, actions_enabled=False)
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(stop())
        self.assertTrue(t.closed)

    def test_main_installs_and_restores_sigint_sigterm_handlers(self):
        stop = StopFlag()
        t = FakeTransport([], stop)
        installed = {}
        real_signal = signal.signal

        def spy(sig, handler):
            installed.setdefault(sig, []).append(handler)
            return real_signal(sig, handler)

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("signal.signal", side_effect=spy),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = main(
                ["--no-write", "--no-actions", "--no-port-guard"],
                transport_factory=lambda **kw: t,
                stop=stop,
                out=out,
                err=err,
            )
        self.assertEqual(code, EXIT_OK)
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.assertEqual(installed[sig][0], stop.handler)
            self.assertEqual(len(installed[sig]), 2)  # installed, then restored
        self.assertTrue(t.closed)


class MainCliTests(NoRealWorld):
    def test_print_config_exits_zero_without_opening_anything(self):
        factory = mock.Mock(side_effect=AssertionError("transport must not be built"))
        holders = mock.Mock(side_effect=AssertionError("guard must not run"))
        code, out, _ = run_main(
            ["--print-config", "--no-write"], transport_factory=factory, find_holders=holders
        )
        self.assertEqual(code, EXIT_OK)
        doc = json.loads(out)
        self.assertEqual(doc["config_source"], "built-in defaults")
        self.assertEqual(doc["port"], DEFAULT_PORT)
        self.assertFalse(doc["writes_enabled"])
        self.assertTrue(doc["actions_enabled"])
        self.assertEqual(doc["config"]["serial_silence_timeout"], 10.0)

    def test_default_port_is_by_id_path(self):
        args = daemon.build_arg_parser().parse_args([])
        self.assertEqual(args.port, DEFAULT_PORT)
        self.assertIsNone(args.config)
        self.assertFalse(args.tiocexcl)
        self.assertFalse(args.no_port_guard)

    def test_bad_argument_exits_3(self):
        code, _, err = run_main(["--nonsense"])
        self.assertEqual(code, EXIT_BAD_ARGS)
        self.assertIn("bad arguments", err)

    def test_bad_log_level_exits_3(self):
        self.assertEqual(run_main(["--log-level", "LOUD"])[0], EXIT_BAD_ARGS)

    def test_missing_config_file_exits_3(self):
        code, _, err = run_main(["--config", "/nonexistent/mass80.toml", "--print-config"])
        self.assertEqual(code, EXIT_BAD_ARGS)
        self.assertIn("bad config", err)

    def test_invalid_config_exits_3(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.toml")
            Path(p).write_text("[nope]\nx = 1\n")
            code, _, err = run_main(["--config", p])
        self.assertEqual(code, EXIT_BAD_ARGS)
        self.assertIn("unknown section", err)

    def test_explicit_config_is_used_and_no_implicit_search(self):
        opened = []
        real_open = open

        def spy(path, *a, **k):
            opened.append(str(path))
            return real_open(path, *a, **k)

        with mock.patch("builtins.open", side_effect=spy):
            run_main(["--print-config"])
        self.assertFalse([p for p in opened if ".config" in p])

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.toml")
            Path(p).write_text("[serial]\nsilence_timeout = 7\n")
            code, out, _ = run_main(["--config", p, "--print-config"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["config"]["serial_silence_timeout"], 7.0)

    def test_help_exits_zero_and_documents_no_port_guard_as_test_only(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code, _, _ = run_main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("FOR TESTS ONLY", buf.getvalue())

    def test_banner_says_which_capabilities_are_on(self):
        stop = StopFlag()
        t = FakeTransport([], stop)
        with self.assertLogs("mass80d.daemon", "INFO") as cm:
            main(
                ["--no-port-guard"],
                transport_factory=lambda **kw: t,
                stop=stop,
                install_signals=False,
                out=io.StringIO(),
                err=io.StringIO(),
            )
        text = "\n".join(cm.output)
        self.assertIn("WRITES to device:   ON", text)
        self.assertIn("system ACTIONS:     ON", text)
        self.assertIn("silence timeout: 10.0", text)
        self.assertIn(DEFAULT_PORT, text)
        self.assertIn("built-in defaults", text)

    def test_banner_off_when_flags_given(self):
        stop = StopFlag()
        t = FakeTransport([], stop)
        with self.assertLogs("mass80d.daemon", "INFO") as cm:
            main(
                ["--no-write", "--no-actions", "--no-port-guard"],
                transport_factory=lambda **kw: t,
                stop=stop,
                install_signals=False,
                out=io.StringIO(),
                err=io.StringIO(),
            )
        text = "\n".join(cm.output)
        self.assertIn("off (--no-write)", text)
        self.assertIn("off (--no-actions)", text)

    def test_port_busy_exits_2_before_opening_anything(self):
        factory = mock.Mock(side_effect=AssertionError("must not open"))
        code, _, err = run_main(
            ["--no-write"],
            transport_factory=factory,
            find_holders=lambda port: [(4242, "python mass80_volume.py")],
        )
        self.assertEqual(code, EXIT_PORT_BUSY)
        self.assertIn("4242", err)
        self.assertIn("mass80_volume.py", err)
        factory.assert_not_called()

    def test_port_guard_checks_the_requested_port(self):
        seen = []
        stop = StopFlag()
        t = FakeTransport([], stop)
        run_main(
            ["--port", "/dev/fake0", "--no-write", "--no-actions"],
            transport_factory=lambda **kw: t,
            find_holders=lambda port: seen.append(port) or [],
            stop=stop,
        )
        self.assertEqual(seen, ["/dev/fake0"])

    def test_transport_is_built_with_exclusive_factory_and_interruptible_sleep(self):
        stop = StopFlag()
        t = FakeTransport([], stop)
        kwargs = {}

        def factory(**kw):
            kwargs.update(kw)
            return t

        run_main(["--no-port-guard", "--tiocexcl"], transport_factory=factory, stop=stop)
        self.assertEqual(kwargs["port"], DEFAULT_PORT)
        self.assertEqual(kwargs["sleep"], stop.wait)
        self.assertEqual(kwargs["silence_timeout"], 10.0)
        self.assertTrue(callable(kwargs["serial_factory"]))

    def test_missing_port_at_startup_keeps_retrying_through_backoff(self):
        stop = StopFlag()
        attempts, sleeps = [], []

        def failing(*a, **k):
            attempts.append(1)
            if len(attempts) >= 4:
                stop.set()
            raise OSError("no such device")

        def make(**kw):
            kw["sleep"] = lambda s: sleeps.append(s)
            kw["serial_factory"] = failing
            return SerialTransport(**kw)

        code, _, _ = run_main(
            ["--no-write", "--no-actions", "--no-port-guard"],
            transport_factory=make,
            stop=stop,
        )
        self.assertEqual(code, EXIT_OK)
        self.assertGreaterEqual(len(attempts), 4)
        self.assertEqual(sleeps[:3], [0.5, 1.0, 2.0])


class ReplayTests(NoRealWorld):
    def test_replay_counts_match_excerpt_and_summary_printed(self):
        code, out, _ = run_main(["--replay", REPLAY, "--no-port-guard"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("replay summary:", out)
        self.assertIn("get_music_info:", out)

        # Independent count of the same file with the real parser.
        from mass80d.parser import EventParser

        p, expected = EventParser(), {}
        for line in Path(REPLAY).read_text().splitlines():
            rec = json.loads(line)
            if rec.get("kind") == "rx":
                for e in p.feed(bytes.fromhex(rec["hex"])):
                    expected[e.action] = expected.get(e.action, 0) + 1
        self.assertTrue(expected)
        for action, n in expected.items():
            self.assertIn(f"  {action}: {n}\n", out)

    def test_replay_implies_no_write_no_actions_and_skips_guard_and_transport(self):
        factory = mock.Mock(side_effect=AssertionError("no transport in replay"))
        holders = mock.Mock(side_effect=AssertionError("no guard in replay"))
        with self.assertLogs("mass80d.daemon", "INFO") as cm:
            code, out, _ = run_main(
                ["--replay", REPLAY], transport_factory=factory, find_holders=holders
            )
        self.assertEqual(code, EXIT_OK)
        text = "\n".join(cm.output)
        self.assertIn("off (--no-write)", text)
        self.assertIn("off (--no-actions)", text)
        self.assertIn("REPLAY", text)
        self.assertIn("would-write frames:", out)

    def test_replay_cannot_reach_subprocess_serial_or_a_device_node(self):
        """NoRealWorld.setUp patches subprocess.*, serial.Serial and os.system to raise.
        Additionally, record every path opened and assert none is a device node."""
        opened = []
        real_open = open
        real_os_open = os.open

        def spy(path, *a, **k):
            opened.append(str(path))
            return real_open(path, *a, **k)

        def spy_os(path, *a, **k):
            opened.append(str(path))
            return real_os_open(path, *a, **k)

        with (
            mock.patch("builtins.open", side_effect=spy),
            mock.patch("os.open", side_effect=spy_os),
        ):
            code, _, _ = run_main(["--replay", REPLAY])
        self.assertEqual(code, EXIT_OK)
        self.assertIn(REPLAY, opened)
        self.assertFalse([p for p in opened if p.startswith("/dev/")], opened)

    def test_replay_would_run_and_would_write_are_logged_not_executed(self):
        # The excerpt has volume and send_key input plus get_time polls.
        with self.assertLogs("mass80d", "INFO") as cm:
            code, out, _ = run_main(["--replay", REPLAY])
        self.assertEqual(code, EXIT_OK)
        text = "\n".join(cm.output)
        self.assertIn("would send", text)  # get_time reply suppressed
        self.assertNotIn("running [", text)

    def test_replay_transport_write_raises(self):
        rt = ReplayTransport([])
        with self.assertRaises(RuntimeError):
            rt.write(b"x")

    def test_replay_skips_non_rx_and_bad_records(self):
        lines = [
            json.dumps({"t": 1.0, "kind": "label", "text": "x"}),
            "not json",
            json.dumps({"t": 2.0, "kind": "rx", "hex": "zz"}),
            json.dumps({"t": 3.0, "kind": "rx", "hex": b'{"action":"get_time"}\n'.hex()}),
        ]
        rt = ReplayTransport(lines)
        events = rt.read_events()
        self.assertEqual([e.action for e in events], ["get_time"])
        self.assertEqual(rt.skipped_lines, 2)
        self.assertTrue(rt.done)

    def test_replay_file_missing_exits_3(self):
        code, _, err = run_main(["--replay", "/nonexistent/capture.log"])
        self.assertEqual(code, EXIT_BAD_ARGS)
        self.assertIn("replay", err)

    def test_replay_get_time_replies_follow_capture_time(self):
        """Rate limit uses the capture clock: polls 5 s apart both produce a would-write."""
        lines = [
            json.dumps({"t": 100.0, "kind": "rx", "hex": b'{"action":"get_time"}\n'.hex()}),
            json.dumps({"t": 100.5, "kind": "rx", "hex": b'{"action":"get_time"}\n'.hex()}),
            json.dumps({"t": 105.0, "kind": "rx", "hex": b'{"action":"get_time"}\n'.hex()}),
        ]
        rt = ReplayTransport(lines)
        write = WriteGate(False, None)
        d = daemon.build_dispatcher(
            default_config(),
            write,
            ActionGate(False),
            rt.clock,
            lambda: daemon.datetime.now().astimezone(),
        )
        with self.assertLogs("mass80d", "INFO"):
            daemon.pump(rt, d, lambda: rt.done)
        self.assertEqual(len(write.suppressed), 2)  # 100.5 is inside the 1.5 s window


class LoggingTests(unittest.TestCase):
    def test_logging_goes_to_stderr_and_is_removed_afterwards(self):
        logger = logging.getLogger("mass80d")
        before = list(logger.handlers)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(
                ["--replay", REPLAY],
                out=io.StringIO(),
                err=io.StringIO(),
                install_signals=False,
            )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("mass80d starting (REPLAY)", err.getvalue())
        self.assertEqual(logger.handlers, before)


class NoSubprocessImportedWrongly(unittest.TestCase):
    def test_subprocess_module_untouched_in_gate_when_disabled(self):
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("touched")):
            ActionGate(False)(["wpctl", "x"])


if __name__ == "__main__":
    unittest.main()
