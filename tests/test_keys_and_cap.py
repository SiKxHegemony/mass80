"""T11: volume cap (D3), key injection (D5), debounce. Every subprocess is a fake."""

import logging
import unittest

from mass80d.actions import Actions, CommandRunner
from mass80d.config import (
    DEFAULT_KEYSYM,
    KEYSYM_WHITELIST,
    KNOWN_KEYS,
    Binding,
    Config,
    ConfigError,
    default_config,
    load_config_text,
)
from tests.test_actions import Clock, FakeRun

NINE = ("Print", "Scroll_Lock", "Pause", "Insert", "Home", "Prior", "Delete", "End", "Next")


def key_cfg(name="Page Up", keysym=None, debounce=0.25):
    body = f'[keys."{name}"]\ntype = "key"\n'
    if keysym is not None:
        body += f'keysym = "{keysym}"\n'
    return load_config_text(
        f"[keys]\ndebounce = {debounce}\n" + body.replace("[keys.", "\n[keys.", 1)
    )


class CapConfigTest(unittest.TestCase):
    def test_default_is_half(self):
        self.assertEqual(default_config().volume_max_fraction, 0.5)

    def test_valid_values(self):
        for v in ("0.3", "1.0", "1", "0.01"):
            c = load_config_text(f"[volume]\nmax_fraction = {v}\n")
            self.assertEqual(c.volume_max_fraction, float(v))

    def test_bad_values_rejected(self):
        for v in ("0", "0.0", "-0.5", "1.01", "2", "true", "false", '"0.5"', "[0.5]"):
            with self.assertRaises(ConfigError, msg=v):
                load_config_text(f"[volume]\nmax_fraction = {v}\n")


class CapBehaviourTest(unittest.TestCase):
    def make(self, cap):
        run, clock = FakeRun(), Clock()
        cfg = Config(volume_max_fraction=cap)
        return Actions(cfg, CommandRunner(run), clock), run, clock

    def test_slider_maximum_sends_the_cap(self):
        a, run, _ = self.make(0.3)
        a.volume(100)
        self.assertEqual(run.argvs, [["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.3"]])

    def test_above_range_still_capped(self):
        a, run, _ = self.make(0.3)
        a.volume(5000)
        self.assertEqual(run.argvs[-1][-1], "0.3")

    def test_below_cap_unchanged(self):
        a, run, _ = self.make(0.5)
        a.volume(10)  # slider fraction 0.10, rescaled onto 0..0.5
        self.assertEqual(run.argvs[-1][-1], "0.05")

    def test_cap_is_a_rescale(self):
        a, run, clock = self.make(0.5)
        a.volume(100)  # slider maximum -> exactly the cap
        clock.t += 1
        a.volume(52)  # 0.52 * 0.5 = 0.26
        self.assertEqual([c[-1] for c in run.argvs], ["0.5", "0.26"])

    def test_cap_one_means_no_cap(self):
        a, run, _ = self.make(1.0)
        a.volume(100)
        self.assertEqual(run.argvs[-1][-1], "1.0")


class KeyConfigTest(unittest.TestCase):
    def test_whitelist_is_exactly_nine(self):
        self.assertEqual(KEYSYM_WHITELIST, frozenset(NINE))
        self.assertEqual(len(KEYSYM_WHITELIST), 9)

    def test_default_keysym_mapping(self):
        self.assertEqual(
            DEFAULT_KEYSYM,
            {
                "Print": "Print",
                "Scroll": "Scroll_Lock",
                "Pause": "Pause",
                "Insert": "Insert",
                "Home": "Home",
                "Page Up": "Prior",
                "Delete": "Delete",
                "End": "End",
                "Page Down": "Next",
            },
        )
        self.assertEqual(set(DEFAULT_KEYSYM), set(KNOWN_KEYS))

    def test_defaults_stay_log(self):
        for k in KNOWN_KEYS:
            self.assertEqual(default_config().keys[k].type, "log")

    def test_debounce_default(self):
        self.assertEqual(default_config().keys_debounce, 0.25)

    def test_key_binding_explicit_keysym(self):
        c = key_cfg("Page Up", "Prior")
        self.assertEqual(c.keys["Page Up"], Binding("key", (), "Prior"))

    def test_keysym_omitted_uses_default(self):
        c = key_cfg("Page Down")
        self.assertEqual(c.keys["Page Down"].keysym, "Next")

    def test_every_name_has_default(self):
        for name, sym in DEFAULT_KEYSYM.items():
            self.assertEqual(key_cfg(name).keys[name].keysym, sym)

    def test_non_whitelisted_keysym_rejected_at_load(self):
        for bad in ("Return", "Escape", "Super_L", "Control_L", "Delete ", ""):
            text = f'[keys.Home]\ntype = "key"\nkeysym = "{bad}"\n'
            with self.assertRaises(ConfigError, msg=repr(bad)):
                load_config_text(text)

    def test_non_string_keysym_rejected_at_load(self):
        for bad in ("5", "true", "['Home']"):
            with self.assertRaises(ConfigError, msg=bad):
                load_config_text(f'[keys.Home]\ntype = "key"\nkeysym = {bad}\n')

    def test_whitelisted_keysym_on_other_device_key_allowed(self):
        # whitelist is on the keysym; mapping is only the default
        c = load_config_text('[keys.Home]\ntype = "key"\nkeysym = "End"\n')
        self.assertEqual(c.keys["Home"].keysym, "End")

    def test_key_type_only_for_the_nine_device_names(self):
        for name in ("F13", "Return", "home"):
            with self.assertRaises(ConfigError, msg=name):
                load_config_text(f'[keys."{name}"]\ntype = "key"\n')

    def test_keysym_only_valid_with_key_type(self):
        with self.assertRaises(ConfigError):
            load_config_text('[keys.Home]\ntype = "log"\nkeysym = "Home"\n')

    def test_existing_types_still_load(self):
        c = load_config_text(
            '[keys.Home]\ntype="hyprctl"\nargs=["workspace","1"]\n[keys.End]\ntype="playerctl"\nargs=["next"]\n'
        )
        self.assertEqual(c.keys["Home"].type, "hyprctl")
        self.assertEqual(c.keys["End"].type, "playerctl")

    def test_debounce_validation(self):
        self.assertEqual(load_config_text("[keys]\ndebounce = 1\n").keys_debounce, 1.0)
        self.assertEqual(load_config_text("[keys]\ndebounce = 0.5\n").keys_debounce, 0.5)
        for bad in ("0", "-1", "true", '"0.2"', "[1]"):
            with self.assertRaises(ConfigError, msg=bad):
                load_config_text(f"[keys]\ndebounce = {bad}\n")

    def test_keys_table_still_rejects_non_tables(self):
        with self.assertRaises(ConfigError):
            load_config_text("[keys]\nHome = 1\n")


class KeyInjectionTest(unittest.TestCase):
    def make(self, cfg, sender=None):
        run, clock = FakeRun(), Clock()
        kwargs = {} if sender is None else {"key_sender": sender}
        return Actions(cfg, CommandRunner(run), clock, **kwargs), run, clock

    def test_default_sender_builds_wtype_argv(self):
        a, run, _ = self.make(key_cfg("Page Up"))
        a.send_key("Page Up")
        self.assertEqual(run.argvs, [["wtype", "-k", "Prior"]])

    def test_all_nine_default_argv(self):
        cfg = load_config_text("".join(f'[keys."{n}"]\ntype = "key"\n' for n in KNOWN_KEYS))
        a, run, clock = self.make(cfg)
        for n in KNOWN_KEYS:
            a.send_key(n)
        self.assertEqual(run.argvs, [["wtype", "-k", DEFAULT_KEYSYM[n]] for n in KNOWN_KEYS])

    def test_injected_sender_used_and_no_process(self):
        sent = []
        a, run, _ = self.make(key_cfg("End"), sender=sent.append)
        a.send_key("End")
        self.assertEqual(sent, ["End"])
        self.assertEqual(run.argvs, [])

    def test_log_binding_sends_nothing(self):
        sent = []
        a, run, _ = self.make(default_config(), sender=sent.append)
        a.send_key("Home")
        self.assertEqual((sent, run.argvs), ([], []))

    def test_one_event_one_press_release_no_combo(self):
        a, run, _ = self.make(key_cfg("Home"))
        a.send_key("Home")
        (argv,) = run.argvs
        self.assertEqual(len(argv), 3)  # wtype -k KEY only: no -M/-P/-p/-d
        self.assertEqual(argv[:2], ["wtype", "-k"])

    def test_call_time_refusal(self):
        for bad in ("Return", "Escape", "Super_L", "Control_L", "Delete ", "", 5, None, ["Home"]):
            cfg = Config(keys={**default_config().keys, "Home": Binding("key", (), bad)})
            sent = []
            a, run, _ = self.make(cfg, sender=sent.append)
            a.send_key("Home")
            if bad is None:  # None means 'use the default for the name'
                self.assertEqual(sent, ["Home"])
            else:
                self.assertEqual((sent, run.argvs), ([], []), repr(bad))

    def test_call_time_refusal_default_sender_runs_nothing(self):
        for bad in ("Return", "Escape", "Super_L", "Control_L", "Delete ", "", 5):
            cfg = Config(keys={**default_config().keys, "Home": Binding("key", (), bad)})
            a, run, _ = self.make(cfg)
            a.send_key("Home")
            self.assertEqual(run.argvs, [], repr(bad))

    def test_call_time_refusal_for_non_device_key_name(self):
        cfg = Config(keys={"F13": Binding("key", (), "Home")})
        a, run, _ = self.make(cfg)
        a.send_key("F13")
        self.assertEqual(run.argvs, [])

    def test_wtype_failure_is_logged_not_raised(self):
        run, clock = FakeRun(returncode=1), Clock()
        a = Actions(key_cfg("Home"), CommandRunner(run), clock)
        with self.assertLogs("mass80d", level="WARNING"):
            a.send_key("Home")
        self.assertEqual(len(run.argvs), 1)

    def test_wtype_timeout_and_missing_binary_do_not_raise(self):
        import subprocess

        for exc in (subprocess.TimeoutExpired("wtype", 2), FileNotFoundError("wtype")):
            run, clock = FakeRun(exc=exc), Clock()
            a = Actions(key_cfg("Home"), CommandRunner(run), clock)
            with self.assertLogs("mass80d", level="WARNING"):
                a.send_key("Home")

    def test_sender_that_raises_does_not_propagate(self):
        def boom(_):
            raise RuntimeError("x")

        a, _, _ = self.make(key_cfg("Home"), sender=boom)
        with self.assertLogs("mass80d", level="ERROR"):
            a.send_key("Home")


class DebounceTest(unittest.TestCase):
    def make(self, debounce=0.25, name="Home"):
        sent = []
        clock = Clock()
        a = Actions(
            key_cfg(name, debounce=debounce),
            CommandRunner(FakeRun()),
            clock,
            key_sender=sent.append,
        )
        return a, sent, clock

    def test_repeat_within_window_dropped_and_logged(self):
        a, sent, clock = self.make()
        a.send_key("Home")
        clock.t += 0.1
        with self.assertLogs("mass80d", level="INFO") as cm:
            a.send_key("Home")
        self.assertEqual(sent, ["Home"])
        self.assertTrue(any("debounce" in m.lower() for m in cm.output))

    def test_after_window_accepted(self):
        a, sent, clock = self.make()
        a.send_key("Home")
        clock.t += 0.25
        a.send_key("Home")
        self.assertEqual(sent, ["Home", "Home"])

    def test_dropped_event_does_not_extend_window(self):
        a, sent, clock = self.make()
        a.send_key("Home")
        clock.t += 0.2
        a.send_key("Home")  # dropped
        clock.t += 0.1  # 0.3 after the first accepted press
        a.send_key("Home")
        self.assertEqual(len(sent), 2)

    def test_debounce_is_per_key(self):
        cfg = load_config_text(
            '[keys]\ndebounce = 0.25\n[keys.Home]\ntype = "key"\n[keys.End]\ntype = "key"\n'
        )
        sent = []
        a = Actions(cfg, CommandRunner(FakeRun()), Clock(), key_sender=sent.append)
        a.send_key("Home")
        a.send_key("End")
        self.assertEqual(sent, ["Home", "End"])

    def test_refused_key_does_not_start_window(self):
        cfg = Config(keys={**default_config().keys, "Home": Binding("key", (), "Return")})
        sent = []
        clock = Clock()
        a = Actions(cfg, CommandRunner(FakeRun()), clock, key_sender=sent.append)
        a.send_key("Home")
        self.assertEqual(sent, [])


class NoActionsTest(unittest.TestCase):
    def test_no_actions_gate_turns_wtype_into_a_log_line(self):
        from mass80d.daemon import ActionGate

        real = FakeRun()
        gate = ActionGate(False, CommandRunner(real))
        a = Actions(key_cfg("Page Up"), gate, Clock())
        with self.assertLogs("mass80d", level="INFO") as cm:
            a.send_key("Page Up")
        self.assertEqual(real.argvs, [])
        self.assertEqual(gate.suppressed, [["wtype", "-k", "Prior"]])
        self.assertTrue(any("would run" in m for m in cm.output))


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
