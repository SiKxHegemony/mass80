# Mass80 screen module: device -> host protocol (T3)

Date: 2026-10-09. Status: Phase 0 analysis of captured traffic. Feeds the Phase 0 decision gate.

Scope: the DEVICE -> HOST direction, from passive captures. Host -> device reply shapes are in `docs/protocol-host-messages.md` (get_time verified on hardware; get_performance / get_music_info inferred from disassembly, untested). Not duplicated here.

## 1. Sources and method

| File | Span | Content |
|---|---|---|
| `captures/capture-20261009-000850.log` | 4 s | smoke test, idle, 5 chunks (only `get_time`) |
| `captures/capture-20261009-001149.log` | 68.6 s | first capture; 1 label (typo `swipe lfft` at 24.9 s); includes `light` events; ends with a capture-tool `error` record (see 7) |
| `captures/capture-20261009-001911.log` | 259.1 s | 21 labels, no crash; main evidence |
| `captures/write-test-*.log` | n/a | host writes for the get_time test; used only in section 6 |

- Format of the logs: JSON lines `{"t":epoch,"kind":"start|connected|rx|label|error|stop","hex":...,"text":...}`. `rx` chunks are arbitrary slices of the serial byte stream, so I concatenated all `rx` bytes per capture and parsed the result (throwaway scripts in the scratch dir, not committed).
- Every line starting with `{"action"` parsed as valid JSON: 148 of 148 (capture 001149) and 170 of 170 (capture 001911). No U+FFFD replacement characters.
- Timestamps in the tables are seconds since the capture's first record, taken from the `rx` chunk containing the event. Resolution is bounded by chunking, about 0.1-0.3 s.
- Labels are typed by operator. In every case where a device event followed a label, the label came 0.5-0.9 s BEFORE the event (e.g. `play music` 71.2 s -> `music_play` 72.1 s). Labels therefore mark the intent, not the instant. For page changes the alignment is less clear (see 5).

## 2. Stream structure (what a parser must handle)

The serial port carries two things interleaved on one byte stream, with no framing between them:

1. **Firmware debug text**, mostly prefixed `[CDC-TX]`: memory reports (`==== MEMORY STATUS ====`, `[SRAM] Free: ...`, `[PSRAM] Free: ...`), per-core task load lines (`[C0]lvgl-1pct,IDLE0-70pct,...` and `[C1]usb_cdc_task-0pct,IDLE1-98pct,mjpeg_ui-25pct,...`, partly UTF-8 Chinese: `内部RAM: 已用=378 KB, 剩余=123 KB`). These are NOT commands.
2. **JSON events**, one object per line, in two copies:
   - a debug echo with parentheses instead of braces: `[CDC-TX] ("action":"send_key","key":"Print")\n`
   - immediately followed by the real JSON line: `\r\n{"action":"send_key","key":"Print"}\n`

   So every event appears as `[CDC-TX] (<body>)\n\r\n{<body>}\n`. Only the `{...}` copy is JSON; the `(...)` copy is not parseable. A consumer must not count both.

Observed facts a parser must tolerate:
- Debug text and JSON can be concatenated with no newline between them, e.g. `...剩余=123 KB (最大块=47 KB)[CDC-TX] ("action":"volume")\n\r\n{"action":"volume"}\n[CDC-TX] [C1]mjpeg_ui...`. Chunks split mid-line. Use a line buffer and search each completed line for a `{` that begins a JSON object; do not assume the JSON starts at column 0.
- Terminators: `get_time` JSON ends in `\r\n`; every other action ends in `\n` (verified by regex on both captures).
- Each capture's first chunk can begin mid-stream (capture 000850 starts with a bare `{"action":"get_time",...}\r\n` with no echo prefix).
- Lone `[CDC-RX]` appears once in capture 001911 at about 0.3 s, inside a garbled debug line (`[[CDC-RX] [CDC-TX] ("actio`). It is debug-output interleaving, not an observed host message. No `[CDC-RX]` echo of any host write was otherwise present in the captures reviewed, which have no host writes.
- Observed JSON key set across ALL events in both captures: `action`, `value`, `key`, `version`. Nothing else.

## 3. Event schema

All events are flat JSON objects with a string `action`. Direction: device -> host.

### 3.1 Polls (device asks the host for data)

| Event | Wire form | Fields | Observed cadence |
|---|---|---|---|
| `get_time` | `{"action":"get_time","version":"2.1.5"}\r\n` | `version` (string). Probably the firmware/UI version, UNCONFIRMED. | median 2.0 s between polls, min 2.0 s (capture 001911), while polling is active |
| `get_music_info` | `{"action":"get_music_info"}\n` | none | median 1.0 s (min 0.9 s) |
| `get_performance` | `{"action":"get_performance"}\n` | none | 3.0 s exactly (6 polls, 142.5-157.5 s) |

Counts: capture 001149 had 21 `get_time` and 21 `get_music_info`, no `get_performance`. Capture 001911 had 7 `get_time`, 84 `get_music_info`, 6 `get_performance`.

Polling is NOT constant. Polls come in windows and stop for long stretches (capture 001911: `get_time` 20.4-28.4 s and 160.5-162.5 s with a 132 s gap; `get_music_info` 0.1, 13.5-18.5, 30.5-105.5, 159.6; `get_performance` only 142.5-157.5; no polls at all after 162.5 s while the key screen was in use). This is consistent with the device only polling for the page it is showing. That mapping (clock page -> `get_time`, music page -> `get_music_info`, CPU page -> `get_performance`) is a HYPOTHESIS from timing, not verified by observing the screen.

Host reply shapes: see `docs/protocol-host-messages.md`.

### 3.2 Input events (device tells the host something happened)

| Event | Wire form | Fields | Meaning |
|---|---|---|---|
| `volume` (begin marker) | `{"action":"volume"}\n` | none | Sent once at the start of a slider gesture, 0.1-0.2 s before the first valued event. Seen 3 times (1 in capture 001149, 2 in 001911). **It is the device asking the host for the current volume (docs/protocol-host-messages.md): the host should answer with `{"query":"volume","result":"ok","value":N}`, N = percent 0..100. Verified on hardware 2026-10-09 (Stages 3b, 3c).** |
| `volume` (value) | `{"action":"volume","value":N}\n` | `value`: integer | Slider position, streamed while the finger moves. |
| `light` | `{"action":"light","value":N}\n` | `value`: integer | Same shape as `volume`, but a different slider/control. Seen only in capture 001149 (one burst, 48 events). No begin marker was seen before it. |
| `send_key` | `{"action":"send_key","key":"<name>"}\n` | `key`: string | A touch key on the screen sends a keystroke request. One event per tap. |
| `music_play` | `{"action":"music_play"}\n` | none | Play touched. |
| `music_pause` | `{"action":"music_pause"}\n` | none | Pause touched. |
| `music_prev` | `{"action":"music_prev"}\n` | none | Observed in capture 20261009-111333 (6 events). Hardware test 2026-10-09 (daemon Stage 5b): each `music_prev` event, run as `playerctl previous`, moved Spotify back (or restarted the track early in it), and no press produced two commands within 2.4 s. Which physical button sends which name is still unobserved. The daemon now maps it to `playerctl previous`. |
| `music_next` | `{"action":"music_next"}\n` | none | Observed in capture 20261009-111333 (1 event). Hardware test 2026-10-09 (Stage 5b): each `music_next` event, run as `playerctl next`, moved Spotify forward. Which physical button sends which name is still unobserved. The daemon now maps it to `playerctl next`. |

`send_key` `key` values observed (exact strings, note the space in the two Page keys): `Print`, `Scroll`, `Pause`, `Insert`, `Home`, `Page Up`, `Delete`, `End`, `Page Down`. These look like X11 / xkb / GDK key names truncated (`Print` = PrintScreen, `Scroll` = Scroll Lock). That mapping is inferred. The 9 keys were each seen in both captures.

### 3.3 Value ranges

| Quantity | Observed | Notes |
|---|---|---|
| `volume.value` | 4 to 50 (capture 001911: 4-42; capture 001149: 6-50) **(superseded, see correction)** | integer, step of 1 or occasionally 2-3 between events. The true range (0? 100?) is UNKNOWN; the extremes were not deliberately swept. |
| `light.value` | 20 to 57 | integer. True range UNKNOWN. |
| event rate while sliding | about 10 events/s (about 100 ms apart) | e.g. 46 events in 1.7 s |

**Correction (Stage 3b, 2026-10-09):** the 4-50 range above came from only 60 values and is wrong. Across all captures 781 `volume.value` samples span 0 to 98, and the vendor SDK checks the range 0..100 (docs/protocol-host-messages.md, "volume handler: STATIC FINDINGS"). mass80d now defaults to `volume.min = 0`, `volume.max = 100`.

Slider behavior from the sequences (capture 001911, `volume up` label at 39.5 s): first gesture begin marker at 40.9 s, then values `27,26,...,17,18,...,28` (21 events, finger went down then up), then `29...42` in a second burst after a 1 s gap. The label `volume down` gesture (marker at 58.3 s) started at **30**, not at 42 where the previous gesture ended. So `value` does not appear to be an accumulated state; it looks like the absolute touch position on the slider (the first value of a gesture is where the finger landed). This is an INFERENCE from two gestures. The POC delta approach in `~/mass80_volume.py` would drift for that reason, which agrees with the plan's note.

Capture 001149 `light` burst: `55,53,...,20,22,...,57` (48 events, 47.8-49.4 s), the same down-then-up pattern.

## 4. Label-to-event timeline (capture 001911)

Seconds since start. "none" means no device->host event other than the normal polls appeared in the window between this label and the next one.

| Label time | Label | Device->host event | Delay |
|---|---|---|---|
| 12.1 | swipe left | none | n/a |
| 27.9 | swipe right | none | n/a |
| 39.5 | volume up | bare `volume` at 40.9, then 34 `volume` values 41.0-45.1 s | 1.4 s to first |
| 55.5 | volume down | bare `volume` at 58.3, then 26 `volume` values 58.5-60.7 s | 2.8 s |
| 71.2 | play music | `music_play` at 72.1 | 0.9 s |
| 77.1 | pause music | `music_pause` at 77.9 | 0.8 s |
| 100.1 | click music track | none | n/a |
| 112.7 | timer play | none | n/a |
| 118.4 | timer stop | none | n/a |
| 127.8 | increase timer | none | n/a |
| 135.6 | decrease timer | none | n/a |
| 146.6 | cpu screen | none (but `get_performance` polling runs 142.5-157.5 s, started 4 s BEFORE the label) | n/a |
| 178.4 | touch screen print key | `send_key` Print at 179.0 | 0.6 s |
| 185.5 | scroll key | `send_key` Scroll at 186.2 | 0.7 s |
| 189.4 | pause key | `send_key` Pause at 190.0 | 0.6 s |
| 195.0 | insert key | `send_key` Insert at 195.5 | 0.5 s |
| 199.3 | home key | `send_key` Home at 199.8 | 0.5 s |
| 205.5 | page up key | `send_key` Page Up at 206.1 | 0.6 s |
| 210.5 | delete key | `send_key` Delete at 211.1 | 0.6 s |
| 215.8 | end key | `send_key` End at 216.5 | 0.7 s |
| 221.8 | page down key | `send_key` Page Down at 222.5 | 0.7 s |

Observations:
- CORRECTED 2026-10-09 (Stage 11b): a swipe on the music page DOES send `music_prev` or `music_next` when a track is loaded (8 and 1 in one run, all executed). The sentence below is from the early captures, whose swipe labels were loose, and is not true for that page. Swipes, the music-track click, timer controls and the CPU screen produced NO device->host event in those captures. They are local to the device UI (the timer is almost certainly implemented on the device). This is "not observed in these captures", not proof that no event exists.
- Swipes do appear to change which poll is active (see 3.1), which is how a host can tell the page indirectly. Alignment between swipe labels and poll windows is loose (the `swipe left` label at 12.1 s precedes `get_music_info` at 13.5 s, but `get_time` polling occurs at 20.4-28.4 s and the page at t=0 is unknown), so no page-name mapping is claimed.
- `volume up` / `volume down` labels name the intended direction. The events contain only absolute values, so direction comes from comparing consecutive values.

Capture 001149 (unlabeled except the typo `swipe lfft` at 24.9 s), seconds: `send_key` for Print, Scroll, Pause, Page Up, Home, Insert, Delete, End, Page Down at 37.3, 38.7, 39.2, 39.6, 40.1, 40.5, 41.1, 41.6, 42.0; bare `volume` at 44.1 then 46 values (44.3-46.0, 23 down to 6 then up to 50); `light` 48 values 47.8-49.4 (55 down to 20 up to 57); `music_pause` 55.0, `music_play` 56.1. Which physical control or screen control produced `light` is NOT known because no label was entered for it.

## 5. Reproduction status (plan criterion: each documented event twice)

| Event | Reproduced in 2 captures? |
|---|---|
| `get_time`, `get_music_info` | yes |
| `get_performance` | NO, one capture only (6 polls) |
| `volume` (marker + values) | yes |
| `light` | NO, one burst, one capture; cause not labeled |
| `send_key` x9 | yes (all 9 keys in both) |
| `music_play`, `music_pause` | yes |

## 6. Can the host send display data?

Evidence-based statement, in three tiers.

**Established (verified on hardware).** The host can change what the screen shows by writing JSON on the same CDC port. Two single-frame tests, each sent once, with operator watching the screen (logs `captures/write-test-20261009-003905.log` and `-004217.log`; details in `docs/protocol-host-messages.md`):
- `{"action":"get_time","time":"2026-10-09 00:39:06","caps":false}\n` -> clock unchanged.
- `{"action":"get_time","time":"2026-10-09 00:42:17-04:00","caps":false}\n` -> **clock updated to the correct time** (screen observation reported by operator).
So a host reply to a device poll does update on-screen text. Those two writes were already done before this task; this task made no writes of any kind.

**Inferred; the `get_music_info` half is now tested (2026-10-09, Stages 6a and 6b: our reply was drawn by the device, see docs/protocol-host-messages.md and docs/music-info-decode.md); `get_performance` is still untested.** The device polls `get_music_info` (about every 1 s) and `get_performance` (every 3 s). The SDK strings (`title`, `artist`, `artworkData`, `image_data`, `get_cover`, `cpu_usage`, ...) indicate the screen displays host-supplied metadata and cover art. Cover requests (`get_cover*`) did NOT occur in either capture, including during the music page window, so the image path was never exercised and its payload format is unknown. The firmware's debug task list includes `mjpeg_ui` (25% load on core 1) and `swdraw`, which hints at an MJPEG-based UI path. A task name is not evidence that the host can stream frames.

**Not established.**
- (Added 2026-10-09 after Stages 8 to 9d.) The device also asks for album art: `{"action": "get_cover", "height": 480, "index": 0, "width": 480}`, once per track change, about 0.5 s after the first `get_music_info` frame of a new track (not on play or pause). Earlier captures never contained it because we had not answered `get_music_info` yet. A host reply with a base64 JPEG was drawn by the screen (Stages 9b to 9d; `docs/cover-decode.md`).
- Whether the device accepts arbitrary host-initiated images, drawing commands, text slots or a framebuffer. The captures contain no sign of such a command (observed actions are only those in section 3), and no write beyond `get_time` was tested.
- Whether any of the replies other than `get_time` are accepted.

Decision-gate implication (for the operator, not a decision): data-driven updates to the built-in pages (clock, music title/artist/progress, performance stats) are supported by evidence or strong inference, so Option A plus feeding host data back is viable. Option B (pseudo-monitor) and Option C (widget renderer with our own images) are NOT supported by any evidence yet. Settling them requires either (a) a operator-approved, watched, single-frame test of `get_music_info`/`get_performance` replies (cheap, already planned) and later of the cover-art path, or (b) more static reading of the SDK for the image payload format. Neither was done here.

## 7. Unknowns and caveats

1. Full value range of `light` (the `volume` range is 0..100, settled in Stage 3b); which on-screen/physical control `light` is.
2. Meaning of `version:"2.1.5"` in `get_time` (assumed firmware version, unverified).
3. Page-to-poll mapping and what page the screen showed at t=0; label timing on page changes is unreliable.
4. Whether other events exist that these captures did not trigger: SDK names not yet seen in any capture are `get_cover_prev`, `get_cover_next`, `music_seek`, `change_theme`, `caps_lock`, `query`, `mute`, `toggle_mute`, `light_computer`, `open_url`, `enter_udisk_mode`, `cat_clap`, `boot_report`, `login`. (`music_prev` and `music_next` were seen in capture 20261009-111333, see the event table.) Multi-finger and long-press gestures were not in the capture script.
5. Capture 001149 ended with the capture tool's own record `error: device reports readiness to read but returned no data (device disconnected or multiple access on port?)` at 68.6 s. Cause unverified (the tool message itself offers two candidates). Capture 001911 had no error.
6. Whether firmware tolerates malformed or oversized host writes: unknown.
7. HID interface 1 (vendor, EP 0x82/0x03) and the keyboard `4d57:0080` channel were not examined; the SDK also uses HID, so a second control channel might exist.
8. The capture tool timestamps whole `rx` chunks; per-event timing is approximate (0.1-0.3 s).
9. Debug text includes system data about the device only (memory, task load). No secrets were seen in the captures.

## 8. How to repeat this analysis

Read-only: concatenate the bytes of all `rx` records in a capture, split on `\n`, take lines containing `{"action"`, `json.loads` from the first `{`, and group by `action`. `tools/timeline.py` and `tools/summarize_captures.py` already exist in the repo for this (I did not run or modify them; I used throwaway scripts in the profile scratch directory instead).
