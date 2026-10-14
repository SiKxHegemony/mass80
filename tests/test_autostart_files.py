"""T29: pin the autostart files (configs/daily.toml, systemd/mass80d.service, docs/runbook.md)."""

import configparser
import dataclasses
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from mass80d import daemon
from mass80d.config import DEFAULT_KEYSYM, default_config, load_config
from mass80d.daemon import EXIT_OK, run_replay

REPO = Path(__file__).resolve().parent.parent
DAILY = REPO / "configs" / "daily.toml"


def expected_keys():
    """Every device key is enabled as type 'key' with the default keysym (Stage 4c)."""
    from mass80d.config import DEFAULT_KEYSYM, Binding

    return {k: Binding("key", (), DEFAULT_KEYSYM[k]) for k in DEFAULT_KEYSYM}


UNIT = REPO / "systemd" / "mass80d.service"
RUNBOOK = REPO / "docs" / "runbook.md"
EXEC_START = (
    "/home/shane/Repos/mass80/.venv/bin/python -B -m mass80d "
    "--config configs/daily.toml --log-level WARNING"
)
KILL_SWITCH = "systemctl --user stop mass80d"


def parse_unit():
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str  # type: ignore[assignment,method-assign]  # keys are case-sensitive
    cp.read_string(UNIT.read_text(encoding="utf-8"))
    return cp


def unit_values(cp, section):
    """All values of a section, repeated keys kept (systemd allows Environment= twice)."""
    out = []
    key = None
    for raw in UNIT.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            key = line[1:-1]
        elif key == section and "=" in line and not line.startswith(("#", ";")):
            k, v = line.split("=", 1)
            out.append((k.strip(), v.strip()))
    return out


class DailyConfigTest(unittest.TestCase):
    def test_equals_defaults_plus_the_verified_replies(self):
        expected = dataclasses.replace(
            default_config(),
            reply_get_music_info=True,
            reply_get_cover=True,
            reply_get_performance=True,
            reply_volume_query=True,
            cover_local_dirs=("/home/shane/.zen/firefox-mpris",),
            keys=expected_keys(),
            volume_max_fraction=1.0,  # slider 100 = system 100% (Shane, 2026-10-09)
        )
        self.assertEqual(load_config(str(DAILY)), expected)

    def test_spot_checks_of_the_values_that_matter(self):
        cfg = load_config(str(DAILY))
        self.assertTrue(cfg.reply_get_time)
        self.assertEqual(cfg.volume_max_fraction, 1.0)
        self.assertEqual(cfg.perf_max_replies, 0)
        self.assertEqual(cfg.cover_test_image, "")
        self.assertEqual(
            (cfg.cover_size, cfg.cover_canvas_width, cfg.cover_canvas_height), (480,) * 3
        )
        self.assertEqual(cfg.music_players, ("spotify", "firefox"))
        self.assertEqual(cfg.cover_hosts, ("i.scdn.co",))

    def test_every_key_binding_is_its_default_keysym(self):
        # Stage 4c (Shane approved 2026-10-14): every device key injects its default keysym.
        cfg = load_config(str(DAILY))
        self.assertEqual(set(cfg.keys), set(default_config().keys))
        for name, binding in cfg.keys.items():
            self.assertEqual(binding.type, "key", name)
            self.assertEqual(binding.keysym, DEFAULT_KEYSYM[name], name)

    def test_first_line_is_a_comment_with_daily(self):
        first = DAILY.read_text(encoding="utf-8").splitlines()[0]
        self.assertTrue(first.startswith("#"))
        self.assertIn("daily", first)

    def test_mentions_the_kill_switch(self):
        self.assertIn(KILL_SWITCH, DAILY.read_text(encoding="utf-8"))

    def test_keys_section_enables_all_nine_keys(self):
        headers = [
            ln.strip()
            for ln in DAILY.read_text(encoding="utf-8").splitlines()
            if ln.startswith("[")
        ]
        self.assertEqual(len([h for h in headers if h.startswith("[keys.")]), 9)


class UnitFileTest(unittest.TestCase):
    def test_unit_section(self):
        unit = parse_unit()["Unit"]
        for key in ("StartLimitIntervalSec", "StartLimitBurst", "After", "PartOf"):
            self.assertIn(key, unit)
        self.assertEqual(unit["StartLimitIntervalSec"], "300")
        self.assertEqual(unit["StartLimitBurst"], "10")
        self.assertEqual(unit["After"], "graphical-session.target")
        self.assertEqual(unit["PartOf"], "graphical-session.target")

    def test_service_section(self):
        svc = parse_unit()["Service"]
        self.assertEqual(svc["ExecStart"], EXEC_START)
        self.assertEqual(svc["WorkingDirectory"], "/home/shane/Repos/mass80")
        self.assertEqual(svc["Restart"], "on-failure")
        self.assertEqual(svc["RestartPreventExitStatus"], "3")
        self.assertEqual(svc["NoNewPrivileges"], "yes")
        self.assertEqual(svc["Type"], "simple")
        self.assertEqual(svc["RestartSec"], "10")

    def test_service_section_has_no_sandboxing_limits_or_identity(self):
        svc = parse_unit()["Service"]
        for key in svc:
            for banned in ("PrivateTmp", "ProtectHome", "ProtectSystem", "StartLimit"):
                self.assertNotIn(banned, key)
            self.assertNotIn(key, ("User", "Group"))
        for key in svc:
            self.assertFalse(key.startswith(("Private", "Protect", "Restrict")), key)

    def test_install_section(self):
        self.assertEqual(parse_unit()["Install"]["WantedBy"], "graphical-session.target")

    def test_only_these_sections(self):
        self.assertEqual(parse_unit().sections(), ["Unit", "Service", "Install"])

    def test_paths_exist_relative_to_the_repo(self):
        argv = parse_unit()["Service"]["ExecStart"].split()
        interpreter = Path(argv[0])
        self.assertEqual(interpreter, REPO / ".venv" / "bin" / "python")
        if not (REPO / ".venv" / "bin" / "python").exists():
            self.skipTest(".venv/bin/python is absent")
        self.assertTrue(interpreter.exists())
        cfg_rel = argv[argv.index("--config") + 1]
        self.assertTrue((REPO / cfg_rel).is_file())
        self.assertEqual(cfg_rel, "configs/daily.toml")
        self.assertTrue((REPO / "src" / "mass80d" / "__main__.py").is_file())

    def test_daily_config_exists(self):
        self.assertTrue(DAILY.is_file())

    def test_environment_is_only_the_two_known_settings(self):
        envs = [v for k, v in unit_values(parse_unit(), "Service") if k == "Environment"]
        self.assertEqual(sorted(envs), ["PYTHONPATH=src", "PYTHONUNBUFFERED=1"])
        self.assertFalse([k for k, _ in unit_values(None, "Service") if k == "EnvironmentFile"])

    def test_no_secret_looking_token_in_any_directive(self):
        for section in ("Unit", "Service", "Install"):
            for key, value in unit_values(None, section):
                text = f"{key}={value}".lower()
                for word in ("secret", "token", "password", "passwd", "apikey", "api_key", "key="):
                    self.assertNotIn(word, text)


class RunbookTest(unittest.TestCase):
    def test_runbook_has_the_commands(self):
        text = RUNBOOK.read_text(encoding="utf-8")
        for needle in (
            "systemctl --user enable --now",
            KILL_SWITCH,
            "journalctl --user -u mass80d",
            "systemctl --user disable --now mass80d",
            "--no-write --no-actions",
        ):
            self.assertIn(needle, text)


def _rx(t, obj):
    line = json.dumps(obj) + "\n"
    return json.dumps({"kind": "rx", "t": t, "hex": line.encode().hex()})


class DailyReplayTest(unittest.TestCase):
    def test_replay_writes_nothing_runs_nothing_and_starts_nothing(self):
        config = load_config(str(DAILY))
        records = [
            _rx(0.0, {"action": "get_time"}),
            _rx(1.0, {"action": "get_music_info"}),
            _rx(2.0, {"action": "get_cover", "height": 480, "index": 0, "width": 480}),
            _rx(3.0, {"action": "get_performance"}),
            _rx(6.0, {"action": "get_performance"}),
        ]
        boom = mock.Mock(side_effect=AssertionError("PerfReader constructed"))
        no_run = mock.Mock(side_effect=AssertionError("a process was started"))
        before = threading.enumerate()
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.log")
            Path(p).write_text("\n".join(records) + "\n", encoding="utf-8")
            with (
                mock.patch.object(daemon, "PerfReader", boom),
                mock.patch.object(subprocess, "run", no_run),
                mock.patch.object(subprocess, "Popen", no_run),
                mock.patch.object(daemon.ReplayTransport, "write", side_effect=AssertionError),
            ):
                code = run_replay(config, p, out)
        self.assertEqual(code, EXIT_OK)
        boom.assert_not_called()
        no_run.assert_not_called()
        self.assertEqual(threading.enumerate(), before)
        text = out.getvalue()
        for action in ("get_time: 1", "get_music_info: 1", "get_cover: 1", "get_performance: 2"):
            self.assertIn(action, text)
        self.assertIn("would-run commands: 0", text)


class PrintConfigTest(unittest.TestCase):
    def test_print_config_for_the_daily_config(self):
        out, err = io.StringIO(), io.StringIO()
        code = daemon.main(
            ["--config", str(DAILY), "--print-config"], out=out, err=err, install_signals=False
        )
        self.assertEqual(code, EXIT_OK, err.getvalue())
        cfg = json.loads(out.getvalue())["config"]
        self.assertIs(cfg["reply_get_performance"], True)
        self.assertIs(cfg["reply_volume_query"], True)
        self.assertIs(cfg["reply_get_cover"], True)
        self.assertIs(cfg["reply_get_music_info"], True)
        self.assertEqual(cfg["perf_max_replies"], 0)


if __name__ == "__main__":
    unittest.main()
