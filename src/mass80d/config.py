"""Configuration for mass80d (TOML, stdlib tomllib only).

The real config is placed by the user; this module never writes any file and has no
built-in search path. load_config(path) reads exactly the path it is given.
Unknown sections or keys are rejected so a typo cannot silently disable a guard.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from typing import Any

from .cover_art import CoverConfig

KNOWN_KEYS = ("Print", "Scroll", "Pause", "Insert", "Home", "Page Up", "Delete", "End", "Page Down")
BINDING_TYPES = frozenset({"log", "hyprctl", "playerctl", "key"})
# D5: the ONLY keysyms the daemon may ever inject. Anything else is refused at config load
# and again at call time, so a typo can never inject Return or a modifier.
KEYSYM_WHITELIST = frozenset(
    {"Print", "Scroll_Lock", "Pause", "Insert", "Home", "Prior", "Delete", "End", "Next"}
)
# Device key name -> default keysym. INFERRED, not observed (plan D5; Stage 4b checks it).
DEFAULT_KEYSYM = {
    "Print": "Print",
    "Scroll": "Scroll_Lock",
    "Pause": "Pause",
    "Insert": "Insert",
    "Home": "Home",
    "Page Up": "Prior",
    "Delete": "Delete",
    "End": "End",
    "Page Down": "Next",
}
MUSIC_MODES = frozenset({"separate", "toggle"})


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Binding:
    """What a send_key does. type 'log' = log only (the harmless default)."""

    type: str = "log"
    args: tuple[str, ...] = ()
    # Only for type 'key'. None means "use DEFAULT_KEYSYM for the device key name".
    keysym: str | None = None


def _default_keys() -> dict[str, Binding]:
    return {k: Binding("log", ()) for k in KNOWN_KEYS}


@dataclass(frozen=True)
class Config:
    # volume.value is an ABSOLUTE slider position. 781 values across all captures span
    # 0..98 and the vendor SDK range is 0..100 (docs/protocol.md section 3.3).
    volume_min: int = 0
    volume_max: int = 100
    volume_min_interval: float = 0.15  # seconds between wpctl launches (events come ~10/s)
    volume_sink: str = "@DEFAULT_AUDIO_SINK@"
    # D3 (revised, Stage 3b): the slider 0..100 is RESCALED onto system volume 0..cap, so the
    # cap is also a hard ceiling: no wpctl set-volume argument ever exceeds it. 1.0 = no cap.
    volume_max_fraction: float = 0.5
    # 'separate': music_play -> playerctl play, music_pause -> playerctl pause.
    # 'toggle': both -> playerctl play-pause.
    music_mode: str = "separate"
    # Player priority for the screen and the buttons (T18): a name matches itself and
    # `name.<instance>` (so `firefox` matches `firefox.instance_1_53`). Listed players come
    # first in this order; players not listed are the lowest priority, alphabetical.
    music_players: tuple[str, ...] = ("spotify", "firefox")
    # light meaning is UNKNOWN. Only 'log' (no-op) is allowed.
    light_action: str = "log"
    keys: dict[str, Binding] = field(default_factory=_default_keys)
    # Minimum seconds between two accepted injections of the same key (D5).
    keys_debounce: float = 0.25
    reply_get_time: bool = True  # verified on hardware
    reply_get_performance: bool = False  # shape inferred, never sent to the device
    reply_get_music_info: bool = False  # shape inferred, never sent to the device
    reply_volume_query: bool = False  # shape inferred from disassembly, never sent to the device
    # get_cover: INFERRED from disassembly, NEVER sent to the device. Fetches art from
    # cover_hosts (an EXTERNAL request) and reads local files only from cover_local_dirs.
    reply_get_cover: bool = False
    cover_hosts: tuple[str, ...] = ("i.scdn.co",)
    cover_local_dirs: tuple[str, ...] = ()
    cover_size: int = 480  # 240 is the SDK size; the screen's Dark theme cover is 480x480
    cover_quality: int = 60
    cover_fetch_timeout_s: float = 3.0
    cover_wait_s: float = 3.0  # a request still unanswered after this long gets the null frame
    cover_reply_null: bool = True  # send the SDK failure shape ("null") instead of nothing
    cover_test_image: str = ""  # stage 9b: answer with this fixed JPEG, no network
    cover_test_max_replies: int = 1  # 0 = unlimited
    cover_canvas_width: int = 480  # JPEG sent is canvas_width x canvas_height, 1..480
    cover_canvas_height: int = 480
    cover_fill_grey: int = 32  # flat grey 0..255 around the size x size cover box
    # get_performance reader (reads /proc and /sys only). Empty = the default-route interface.
    perf_interfaces: tuple[str, ...] = ()
    perf_max_replies: int = 0  # 0 = unlimited; else stop answering after this many SENT frames
    # Reconnect if no bytes at all (debug text included) arrive for this many seconds.
    serial_silence_timeout: float = 10.0

    def cover_config(self) -> CoverConfig:
        """The CoverConfig for the CoverService (other limits keep their defaults)."""
        return CoverConfig(
            hosts=self.cover_hosts,
            local_dirs=self.cover_local_dirs,
            size=self.cover_size,
            quality=self.cover_quality,
            fetch_timeout_s=self.cover_fetch_timeout_s,
            canvas_width=self.cover_canvas_width,
            canvas_height=self.cover_canvas_height,
            fill_grey=self.cover_fill_grey,
        )


def default_config() -> Config:
    return Config()


def _section(data: dict[str, Any], name: str, allowed: tuple[str, ...]) -> dict[str, Any]:
    sec = data.get(name, {})
    if not isinstance(sec, dict):
        raise ConfigError(f"[{name}] must be a table")
    extra = set(sec) - set(allowed)
    if extra:
        raise ConfigError(f"unknown key(s) in [{name}]: {sorted(extra)}")
    return sec


def _int(sec: dict[str, Any], name: str, key: str, default: int) -> int:
    v = sec.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ConfigError(f"[{name}] {key} must be an integer")
    return v


def _bool(sec: dict[str, Any], name: str, key: str, default: bool) -> bool:
    v = sec.get(key, default)
    if not isinstance(v, bool):
        raise ConfigError(f"[{name}] {key} must be true or false")
    return v


def _str_list(sec: dict[str, Any], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    v = sec.get(key, list(default))
    if (
        not isinstance(v, list)
        or not all(isinstance(s, str) for s in v)
        or any(s == "" or s != s.strip() for s in v)
    ):
        raise ConfigError(
            f"[cover] {key} must be a list of non-empty strings without surrounding spaces"
        )
    return tuple(v)


_IFACE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,15}")


def _perf_interfaces(sec: dict[str, Any], default: tuple[str, ...]) -> tuple[str, ...]:
    v = sec.get("interfaces", list(default))
    if (
        not isinstance(v, list)
        or len(v) > 8
        or not all(isinstance(s, str) and _IFACE_RE.fullmatch(s) for s in v)
    ):
        raise ConfigError(
            "[performance] interfaces must be a list of at most 8 interface names "
            "(1-15 characters of A-Z a-z 0-9 _ . : -)"
        )
    return tuple(v)


def _number(sec: dict[str, Any], key: str, default: float) -> float:
    v = sec.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ConfigError(f"[cover] {key} must be a number")
    return float(v)


def load_config_text(text: str) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    base = default_config()
    extra = set(data) - {
        "volume",
        "music",
        "light",
        "keys",
        "replies",
        "serial",
        "cover",
        "performance",
    }
    if extra:
        raise ConfigError(f"unknown section(s): {sorted(extra)}")

    ser = _section(data, "serial", ("silence_timeout",))
    silence = ser.get("silence_timeout", base.serial_silence_timeout)
    if isinstance(silence, bool) or not isinstance(silence, (int, float)) or silence <= 0:
        raise ConfigError("[serial] silence_timeout must be a number > 0")

    vol = _section(data, "volume", ("min", "max", "min_interval", "sink", "max_fraction"))
    vmin = _int(vol, "volume", "min", base.volume_min)
    vmax = _int(vol, "volume", "max", base.volume_max)
    if vmin >= vmax:
        raise ConfigError("[volume] min must be less than max")
    interval = vol.get("min_interval", base.volume_min_interval)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval < 0:
        raise ConfigError("[volume] min_interval must be a number >= 0")
    sink = vol.get("sink", base.volume_sink)
    if not isinstance(sink, str) or not sink:
        raise ConfigError("[volume] sink must be a non-empty string")

    cap = vol.get("max_fraction", base.volume_max_fraction)
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not 0 < cap <= 1.0:
        raise ConfigError("[volume] max_fraction must be a number with 0 < x <= 1.0")

    mus = _section(data, "music", ("mode", "players"))
    mode = mus.get("mode", base.music_mode)
    if mode not in MUSIC_MODES:
        raise ConfigError(f"[music] mode must be one of {sorted(MUSIC_MODES)}")
    players = mus.get("players", list(base.music_players))
    if (
        not isinstance(players, list)
        or not all(isinstance(p, str) for p in players)
        or any(p == "" or p != p.strip() for p in players)
    ):
        raise ConfigError(
            "[music] players must be a list of non-empty strings without surrounding spaces"
        )

    lig = _section(data, "light", ("action",))
    light = lig.get("action", base.light_action)
    if light != "log":
        raise ConfigError("[light] action must be 'log' (its meaning is unknown)")

    keys = _default_keys()
    kt = data.get("keys", {})
    if not isinstance(kt, dict):
        raise ConfigError("[keys] must be a table")
    debounce = kt.get("debounce", base.keys_debounce)
    if isinstance(debounce, bool) or not isinstance(debounce, (int, float)) or debounce <= 0:
        raise ConfigError("[keys] debounce must be a number > 0")
    for name, b in kt.items():
        if name == "debounce":
            continue
        if not isinstance(b, dict):
            raise ConfigError(f"[keys.{name}] must be a table")
        extra = set(b) - {"type", "args", "keysym"}
        if extra:
            raise ConfigError(f"unknown key(s) in [keys.{name}]: {sorted(extra)}")
        typ = b.get("type", "log")
        if typ not in BINDING_TYPES:
            raise ConfigError(f"[keys.{name}] type must be one of {sorted(BINDING_TYPES)}")
        args = b.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ConfigError(f"[keys.{name}] args must be a list of strings")
        keysym: str | None = None
        if "keysym" in b and typ != "key":
            raise ConfigError(f"[keys.{name}] keysym is only valid with type = 'key'")
        if typ == "key":
            if name not in DEFAULT_KEYSYM:
                raise ConfigError(
                    f"[keys.{name}] type 'key' is only allowed for {list(KNOWN_KEYS)}"
                )
            if args:
                raise ConfigError(f"[keys.{name}] type 'key' takes no args")
            keysym = b.get("keysym", DEFAULT_KEYSYM[name])
            if not isinstance(keysym, str) or keysym not in KEYSYM_WHITELIST:
                raise ConfigError(f"[keys.{name}] keysym must be one of {sorted(KEYSYM_WHITELIST)}")
        keys[name] = Binding(typ, tuple(args), keysym)

    rep = _section(
        data,
        "replies",
        (
            "reply_get_time",
            "reply_get_performance",
            "reply_get_music_info",
            "volume_query",
            "get_cover",
        ),
    )
    cov = _section(
        data,
        "cover",
        (
            "hosts",
            "local_dirs",
            "size",
            "quality",
            "fetch_timeout_s",
            "wait_s",
            "reply_null",
            "test_image",
            "test_max_replies",
            "canvas_width",
            "canvas_height",
            "fill_grey",
        ),
    )
    perf = _section(data, "performance", ("interfaces", "max_replies"))
    perf_interfaces = _perf_interfaces(perf, base.perf_interfaces)
    perf_max = _int(perf, "performance", "max_replies", base.perf_max_replies)
    if not 0 <= perf_max <= 100:
        raise ConfigError("[performance] max_replies must be an integer 0..100")
    canvas_width = _int(cov, "cover", "canvas_width", base.cover_canvas_width)
    canvas_height = _int(cov, "cover", "canvas_height", base.cover_canvas_height)
    fill_grey = _int(cov, "cover", "fill_grey", base.cover_fill_grey)
    hosts = _str_list(cov, "hosts", base.cover_hosts)
    local_dirs = _str_list(cov, "local_dirs", base.cover_local_dirs)
    size = _int(cov, "cover", "size", base.cover_size)
    quality = _int(cov, "cover", "quality", base.cover_quality)
    fetch_timeout = _number(cov, "fetch_timeout_s", base.cover_fetch_timeout_s)
    wait_s = _number(cov, "wait_s", base.cover_wait_s)
    if not 0 < wait_s <= 10:
        raise ConfigError("[cover] wait_s must be a number with 0 < x <= 10")
    max_replies = _int(cov, "cover", "test_max_replies", base.cover_test_max_replies)
    if not 0 <= max_replies <= 100:
        raise ConfigError("[cover] test_max_replies must be an integer 0..100")
    test_image = cov.get("test_image", base.cover_test_image)
    if not isinstance(test_image, str):
        raise ConfigError("[cover] test_image must be a string")
    if test_image and not test_image.lower().endswith((".jpg", ".jpeg")):
        raise ConfigError("[cover] test_image must end with .jpg or .jpeg")
    try:  # one source of truth for the values CoverConfig already validates
        CoverConfig(
            hosts=hosts,
            local_dirs=local_dirs,
            size=size,
            quality=quality,
            fetch_timeout_s=fetch_timeout,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            fill_grey=fill_grey,
        )
    except ValueError as exc:
        raise ConfigError(f"[cover] {exc}") from exc
    return Config(
        volume_min=vmin,
        volume_max=vmax,
        volume_min_interval=float(interval),
        volume_sink=sink,
        volume_max_fraction=float(cap),
        music_mode=mode,
        music_players=tuple(players),
        light_action=light,
        keys=keys,
        keys_debounce=float(debounce),
        reply_get_time=_bool(rep, "replies", "reply_get_time", base.reply_get_time),
        reply_get_performance=_bool(
            rep, "replies", "reply_get_performance", base.reply_get_performance
        ),
        reply_get_music_info=_bool(
            rep, "replies", "reply_get_music_info", base.reply_get_music_info
        ),
        reply_volume_query=_bool(rep, "replies", "volume_query", base.reply_volume_query),
        reply_get_cover=_bool(rep, "replies", "get_cover", base.reply_get_cover),
        cover_hosts=hosts,
        cover_local_dirs=local_dirs,
        cover_size=size,
        cover_quality=quality,
        cover_fetch_timeout_s=fetch_timeout,
        cover_wait_s=wait_s,
        cover_reply_null=_bool(cov, "cover", "reply_null", base.cover_reply_null),
        cover_test_image=test_image,
        cover_test_max_replies=max_replies,
        cover_canvas_width=canvas_width,
        cover_canvas_height=canvas_height,
        cover_fill_grey=fill_grey,
        perf_interfaces=perf_interfaces,
        perf_max_replies=perf_max,
        serial_silence_timeout=float(silence),
    )


def load_config(path: str) -> Config:
    with open(path, encoding="utf-8") as fh:
        return load_config_text(fh.read())
