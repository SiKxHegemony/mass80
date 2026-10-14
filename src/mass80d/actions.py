"""Action layer: turns decoded events into system commands.

All commands are run as argument lists through an injectable runner (default:
subprocess.run without a shell), so tests never start a process.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable
from typing import Any

from .config import DEFAULT_KEYSYM, KEYSYM_WHITELIST, Config
from .player_select import PlayerSelector

log = logging.getLogger("mass80d.actions")

COMMAND_TIMEOUT_S = 2.0


def volume_fraction(value: int, vmin: int, vmax: int) -> float:
    """Map an absolute slider position onto 0.0-1.0, clamped, rounded to 1 percent."""
    frac = (value - vmin) / (vmax - vmin)
    frac = min(1.0, max(0.0, frac))
    return round(frac, 2)


class CommandRunner:
    """Runs an argv list. Returns True on exit status 0; never raises."""

    def __init__(self, run: Callable[..., Any] | None = None) -> None:
        self._run = run  # None -> subprocess.run, resolved at call time

    def __call__(self, argv: list[str]) -> bool:
        run = self._run or subprocess.run
        try:
            res = run(
                list(argv),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=COMMAND_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("command %s failed: %s", argv[0], exc)
            return False
        if res.returncode != 0:
            log.warning("command %s exited %s", argv[0], res.returncode)
            return False
        return True


class Actions:
    RETRY_BACKOFF_S = 1.0  # minimum seconds between wpctl launches after a failure

    def __init__(
        self,
        config: Config,
        runner: Callable[[list[str]], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        key_sender: Callable[[str], None] | None = None,
        selector: PlayerSelector | None = None,
    ) -> None:
        self._cfg = config
        self._selector = selector  # shared with the reply layer; None -> old argv, no -p
        self._no_player_logged = False  # log "no player" once per run of presses
        self._run = runner or CommandRunner()
        self._clock = clock
        # Injectable so tests never inject a real key. Default: wtype through self._run,
        # which in the daemon is the ActionGate, so --no-actions only logs it.
        self._key_sender = key_sender or self._wtype_send
        self._last_key_at: dict[str, float] = {}
        self._last_run: float | None = None
        self._last_sent: float | None = None  # last fraction wpctl accepted
        self._pending: float | None = None
        self._retry_at: float | None = None  # set after a failed wpctl call

    # --- volume -------------------------------------------------------------
    def volume(self, value: Any) -> None:
        # Marker events (value None) and non-integers are ignored.
        if isinstance(value, bool) or not isinstance(value, int):
            return
        # The slider is absolute and the first touch of a gesture can jump straight to its
        # position, so the 0..1 slider fraction is RESCALED onto 0..cap (cap 1.0 = no cap).
        # The final min() is a belt-and-braces guard: no argument may ever exceed the cap.
        cap = self._cfg.volume_max_fraction
        frac = round(volume_fraction(value, self._cfg.volume_min, self._cfg.volume_max) * cap, 2)
        self._pending = min(frac, cap)
        self._flush_volume()

    def tick(self) -> None:
        """Call regularly so the final value of a burst is not left coalesced."""
        self._flush_volume()

    def _flush_volume(self) -> None:
        frac = self._pending
        if frac is None:
            return
        if frac == self._last_sent:
            self._pending = None
            return
        now = self._clock()
        if self._last_run is not None and now - self._last_run < self._cfg.volume_min_interval:
            return  # keep only the latest value; sent on a later call
        if self._retry_at is not None and now < self._retry_at:
            return  # previous attempt failed: wait out the backoff
        self._last_run = now
        argv = ["wpctl", "set-volume", self._cfg.volume_sink, str(frac)]
        if self._run(argv):
            self._pending = None
            self._retry_at = None
            self._last_sent = frac
        else:
            # Keep the value pending so tick() retries it, but not in a tight loop.
            self._retry_at = now + self.RETRY_BACKOFF_S

    # --- music --------------------------------------------------------------
    def music(self, action: str) -> None:
        # music_prev / music_next always map to previous / next; music_mode only concerns
        # play and pause. Any other action is ignored. With a selector (T18) the command
        # targets the selected player's exact instance with `-p`, recomputed NOW; without one
        # the old argv (no -p) is kept.
        if action == "music_prev":
            cmd = "previous"
        elif action == "music_next":
            cmd = "next"
        elif action in ("music_play", "music_pause"):
            if self._cfg.music_mode == "toggle":
                cmd = "play-pause"
            else:
                cmd = "play" if action == "music_play" else "pause"
        else:
            return
        if self._selector is None:
            self._run(["playerctl", cmd])
            return
        try:
            player = self._selector.current()
        except Exception:  # a selector bug must not stop the dispatcher
            log.exception("player selection failed; %s not run", action)
            return
        if player is None:
            if not self._no_player_logged:
                self._no_player_logged = True
                log.info("no music player to control; %s not run", action)
            return
        self._no_player_logged = False
        self._run(["playerctl", "-p", player.instance, cmd])

    # --- keys / light -------------------------------------------------------
    def send_key(self, key: str | None) -> None:
        if key is None:
            return
        b = self._cfg.keys.get(key)
        if b is None or b.type == "log":
            log.info("send_key %r (no action bound)", key)
            return
        if b.type == "hyprctl":
            self._run(["hyprctl", "dispatch", *b.args])
        elif b.type == "playerctl":
            self._run(["playerctl", *b.args])
        elif b.type == "key":
            self._inject_key(key, b.keysym)

    def _wtype_send(self, keysym: str) -> None:
        # wtype(1): "-k KEY  Type (press and release) key KEY". One press+release, no
        # modifiers, no repeat.
        self._run(["wtype", "-k", keysym])

    def _inject_key(self, name: str, keysym: object) -> None:
        """Validate (again, at call time), debounce, then hand ONE keysym to the sender.

        Never raises: a failing sender must not stop the dispatcher.
        """
        if keysym is None and name in DEFAULT_KEYSYM:
            keysym = DEFAULT_KEYSYM[name]
        if name not in DEFAULT_KEYSYM or not isinstance(keysym, str):
            log.error("refusing key injection for %r: invalid binding %r", name, keysym)
            return
        if keysym not in KEYSYM_WHITELIST:
            log.error("refusing key injection for %r: keysym %r not whitelisted", name, keysym)
            return
        now = self._clock()
        last = self._last_key_at.get(name)
        if last is not None and now - last < self._cfg.keys_debounce:
            log.info("debounce: dropped repeat of %r after %.3f s", name, now - last)
            return
        self._last_key_at[name] = now
        try:
            self._key_sender(keysym)
        except Exception:
            log.exception("key sender failed for %r", keysym)

    def light(self, value: Any) -> None:
        # Meaning of `light` is UNKNOWN: log only.
        log.debug("light %r (log only)", value)
