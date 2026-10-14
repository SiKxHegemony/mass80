# Host-message findings (T2b): static read of vendor macOS SDK

Date: 2026-10-08. Status: PARTIAL. Field NAMES are known; exact reply JSON SHAPES are NOT yet confirmed.

## Method and limits
- Source: `libKeyboardSDK.dylib` from `MWKEYS_Portal.dmg` (macOS host software 1.3.4, arm64), extracted with `7z`, inspected with `strings -a -n 4` only.
- Nothing was executed. The Windows installer was only listed: `7z` shows a single 50 MB `backend.dll.x64.dll` entry and an "unexpected end of archive" error, so it was not unpacked.
- `strings` returns a pooled, de-duplicated string table. Adjacent strings are suggestive, NOT proof of which field belongs to which message. Anything below marked "inferred" comes from adjacency or naming only.
- Evidence file: `vendor/x/sdk_strings.txt` (gitignored; line numbers below refer to it).

## Confirmed by observation (our captures) AND present in the SDK
`get_time`, `get_music_info`, `get_performance`, `volume`, `music_play`, `music_pause`, `send_key`.

## Message names found in the SDK (lines 620-700)
Device -> host (seen in captures or named in SDK): `volume` (field `value`), `get_time`, `get_music_info`, `get_performance`, `get_cover_prev`, `get_cover_next`, `music_next`, `music_prev`, `music_play`, `music_pause`, `music_seek`, `send_key`, `change_theme`, `caps_lock`, `query`, `mute`, `toggle_mute`, `light`, `light_computer`, `open_url`, `enter_udisk_mode`, `cat_clap`, `boot_report`, `login`.

Names that look like host -> device or reply fields (inferred):

| Likely message | Field names found nearby | Confidence |
|---|---|---|
| `get_music_info` reply | `title`, `artist`, `duration`, `elapsedTime`, `playing`, `progress`, `index`, `state`, `artworkData`, `image_data`, `get_cover` (lines 330-345) | medium: the block contains the string `get_music_info` itself |
| `get_performance` reply | `memory_usage`, `cpu_usage`, `cpu_freq`, `cpu_temp`, `net_upload`, `net_download` (lines 658-665) | medium |
| `get_time` reply | `time` (line ~690), `width`, `height` appear next to it but may belong to cover/image handling | LOW |
| generic reply/ack | `result`, `message`, `ignore`, `fail`, `not support`, `invalid url` | low |

## What this tells us
- Protocol is JSON over the CDC serial port. The SDK also uses HID (`libhidapi`, VID 4D57 PID 0080) so there may be a second control channel on the keyboard's vendor HID interface; not investigated.
- The SDK drops oversized packets ("Dropping oversized inbound JSON packet", "Rejected invalid outbound packet") and has a bounded send queue, so malformed or oversized writes are validated at least on the host side. Whether the firmware validates is unknown.
- `http://127.0.0.1:5293` appears: the vendor software may run a local HTTP service. Purpose unknown. Not investigated.
- `enter_udisk_mode` releases the serial port and suspends reconnect (string: "release serial+HID, suspend reconnect"). **It is a state-change command. Never send it casually.**
- Music cover art goes through `get_cover`, `artworkData`, `image_data`, which suggests the device DOES accept image payloads from the host. That is relevant to the pseudo-monitor question but is not proof of a framebuffer or arbitrary-frame interface.

## Disassembly findings (llvm-objdump 22.1.8, arm64, static only; tools/sdk_xref.py, tools/sdk_strings_in_range.py)
Dispatcher: `SpotifyOAuthServices::handleJsonCommand` (the class name is misleading; it handles the serial JSON commands). It compares the incoming `action` against each name and builds a reply with nlohmann::json from an initializer list.

### get_time reply: VERIFIED ON HARDWARE (2026-10-09)
Two single-frame write tests, each sent once right after the device's own `get_time` poll, with operator watching the screen. Logs: `captures/write-test-20261009-003905.log`, `captures/write-test-20261009-004217.log`.

| Test | Frame | Screen result (reported by operator) |
|---|---|---|
| 1 | `{"action":"get_time","time":"2026-10-09 00:39:06","caps":false}\n` (no offset) | clock unchanged |
| 2 | `{"action":"get_time","time":"2026-10-09 00:42:17-04:00","caps":false}\n` (with UTC offset) | **clock updated to the correct time** |

- Conclusion: the `time` string needs the `-04:00` style offset suffix. Terminator `\n` and `caps:false` were accepted. Kernel log stayed clean after both writes (no USB errors).
- Correction to an earlier note: the device kept polling `get_time` every ~2 s even after the accepted reply, so continued polling is NOT evidence that a reply was rejected. Only the screen is evidence.
- Not tested: whether the offset is required or merely sufficient (only two data points), behaviour with `caps:true`, and how often the clock needs refreshing.
- (Status as of the first write tests; see below for what has since been tested.) Not tested at all then: `get_performance`, `get_music_info`, `get_cover*`.
Keys built, in order: `action` = `"get_time"`, `time` = result of `currentDateTime()`, `caps` = an atomic bool. Then serialised and passed to `writeData(ptr, len, bool)`.
- `currentDateTime()` (0x39fb0): `strftime`-style `%Y-%m-%d %H:%M:%S`, then a second format `%c%02ld:%02ld` (sign, hours, minutes) which looks like a UTC offset suffix. INFERRED shape: `{"action":"get_time","time":"2026-10-09 00:30:00-04:00","caps":false}`. Whether the offset is really appended, and the exact `caps` type on the wire, are NOT confirmed.
- This is one of the replies we need. Confidence: medium-high for the key set, medium for the time string format.

### get_performance reply (handler region 0x30600-0x30a40)
Initializer-list count is 7 (`mov x9, #0x7` at 0x30a14), matching `action` + 6 fields: `memory_usage`, `cpu_usage`, `cpu_freq`, `cpu_temp`, `net_upload`, `net_download`. Flat object. INFERRED: `{"action":"get_performance","memory_usage":N,"cpu_usage":N,"cpu_freq":N,"cpu_temp":N,"net_upload":N,"net_download":N}`.
- Value TYPES and sources (resolved by reading `runService` lambda `$_4`, `UpdateAndPrintRates`, and the handler's struct offsets; all disassembly, nothing run). All six are stored as **32-bit signed integers** (`str w8, [x, #off]` after `fcvtzs`), so they serialise as JSON **integers**, not floats:

| Key | Struct offset | Source (as read) | Unit status |
|---|---|---|---|
| `memory_usage` | +0x2e4 | return value of an inner lambda (`ENKUlvE_clEv`), not read | UNKNOWN (percent likely, unconfirmed) |
| `cpu_usage` | +0x2e8 | double from the second inner lambda (`ENKUlvE0_clEv`), truncated to int, not read | UNKNOWN (percent likely, unconfirmed) |
| `cpu_freq` | +0x2ec | `WbemidlHelper::GetCpuProcessorFrequency()` | UNKNOWN (likely MHz, unconfirmed; the helper name is a Windows WMI one, which is odd inside a macOS dylib, so this image may be a shared code base) |
| `cpu_temp` | +0x2f0 | **computed, not measured:** `int(cpu_usage * 0.25) + 38 + (pseudo-random 0..3)` | The value is synthetic, derived from cpu_usage by an LCG (`x*0x41C64E6D + 0x3039`). Unit presumably degrees C. |
| `net_upload` | +0x2f4 | `UpdateAndPrintRates`: byte-counter delta / 1024 / elapsed seconds, truncated | PROBABLY KB/s, unconfirmed |
| `net_download` | +0x2f8 | same | PROBABLY KB/s, unconfirmed |

- Refresh cadence on the host: the worker loop sleeps `0x7d0` = **2000 ms** between updates (`sleep_for` 2000 ms at 0x6d2f8). The device's own `get_performance` poll rate was observed in captures (6 polls in 259 s); the host does not appear to push on its own rhythm beyond answering the poll.
- Practical consequence: on Linux we can supply real `cpu_temp` instead of the vendor's synthetic value. Whether the device tolerates any integer is unconfirmed.

### get_performance: still unknown
Exact units and ranges (is `memory_usage` a percentage or MB? does the UI clamp at 100?). Only a controlled test with you watching the screen can settle these.
- Retraction: an earlier tool line showing a string `'emote'` here was a false positive (stale ADRP page), not a real key.

### get_music_info reply (function `CSMTCMusicplayer::get_music_info`, 0x48e4 region, 0x4020-0x4e00)
Initializer list size is 7 (`mov x9, #0x7` at 0x4be0), keys in order: `action` = `"get_music_info"`, `progress`, `duration`, `title`, `artist`, `index`, `state`. INFERRED shape:
`{"action":"get_music_info","progress":?,"duration":?,"title":"...","artist":"...","index":?,"state":?}`
- Types, from the builder's visible code (disassembly only):
  - `progress` and `duration`: the macOS code reads two doubles from the adapter's dictionary (via `objectForKeyedSubscript:` / `doubleValue`), **multiplies each by 1000.0** (constant `0x408f400000000000`), truncates to **int32** and stores them in the player object at +0xf0 and +0xf4. So they are very likely **integer milliseconds**. Which of the two goes to `progress` vs `duration` was NOT confirmed (the key-to-field wiring sits behind stack shuffles I did not trace).
  - `state`: serialised from a **single bit** (`ldrb [obj,#0x85]; and #1`), so it is a **boolean (0/1)**; JSON true/false vs 0/1 on the wire is unconfirmed.
  - `title`, `artist`: strings. A string-conversion lambda (`$_0`) is applied, and the string table contains `opencc` (Chinese variant conversion), so some text may be converted (traditional/simplified); effect on ASCII text is not known.
  - `index`: NOT resolved (likely the current track/queue index; source field not traced).
- NOT traced: which adapter key (`elapsedTime`, `duration`, `playing`) feeds which JSON key. The two doubles are read via objc selector refs at 0xaa000+0xed0 and +0xef0 and the bool via +0xf10; I did not resolve those selector strings. Treat any mapping of `elapsedTime`->`progress` or `playing`->`state` as a hypothesis, not a finding. The `state` JSON value specifically comes from a bit at `[obj+0x85]` (`ldrb`, `and #1`), and how that bit is set was not traced.
- This builder is macOS-specific (MediaRemote framework). On Linux the equivalent source is MPRIS (`playerctl`), which we would have to map ourselves.
- A `lastSendTime` static in the function suggests a send throttle. Unknown interval.
- `get_cover`, `get_cover_prev`, `get_cover_next` take `width`, `height`, `index` and clamp the values to 1024 (`0x400`). That implies the device asks for album art at a given size and the host responds with image data. The image data shape is not yet read.
  - **Update 2026-10-09:** `get_cover` is decoded in `docs/cover-decode.md` and VERIFIED ON HARDWARE (Stages 9b to 9d): the screen sends `{"action": "get_cover", "height": 480, "index": 0, "width": 480}` about 0.5 s after the first frame of a new track and draws our reply `{"action":"get_cover","image_data":"<base64 of a 240x240 JPEG>","index":0}\n` (5,953 to 26,461 bytes per frame, all accepted). `get_cover_prev` and `get_cover_next` were never sent by the screen in 15 requests; the SDK answers neither.

### get_music_info reply: DECODED (T16) AND PARTLY VERIFIED ON HARDWARE (2026-10-09, Stages 6a and 6b)
- The decode is `docs/music-info-decode.md` (task T16, re-checked against `vendor/x/sdk.asm`). It supersedes the inferred notes above: the builder is at 0x4470 (not 0x4020), the reply is flat with seven keys, `progress` is `elapsedTime` x 1000 and `duration` is `duration` x 1000 truncated to int32 (both JSON integers), `state` is the integer 0 or 1, `index` is always 0, title and artist pass a character whitelist that blanks the whole field on any other character, the dump is compact, and the SDK throttles to one reply per 400 ms.
- Frame the daemon sends (task T17): `{"action":"get_music_info","artist":"TOOL","duration":306853,"index":0,"progress":150307,"state":1,"title":"Sober"}` plus `\n` (keys alphabetical; progress and duration in milliseconds from `playerctl` microseconds; `state` 1 only while `Playing`).
- Hardware (Stage 6a and 6b, operator's observation plus logs): the device drew the title on line one, the artist or channel under it and a progress bar that tracked the track; the bar stepped about every 3 seconds while we sent a new value every second; the play/pause indicator changed when the player paused and the bar stopped. Settled: shape, key names, millisecond units and integer `state` are accepted. Not tested on the device: the SDK-style empty frame (no player), titles containing blanked characters, CJK text (we do not apply the SDK's opencc conversion), long titles, and a non-zero `index`.
- Known imperfection: Firefox's player sometimes gives no length or position while playing, so some frames say Playing with `duration` 0 and `progress` 0, which the SDK never sends for an active track (report R14).

### volume handler: STATIC FINDINGS (2026-10-09, NOT verified on hardware, nothing was written to the device)
Source: `SpotifyOAuthServices::handleJsonCommand` (the SDK's dispatcher for JSON arriving from the device), `vendor/x/sdk.asm`, branch for action `"volume"` at 0x33498-0x34270. Read with `python3` over the saved disassembly; no binary executed.

1. **A `volume` message WITH `value` is a "set" command.** Branch at 0x334b4-0x33730: it reads `value` (int), calls `CVolume::SetSystemVolume(bool, int)` with it, then builds a reply with keys `"result"` and a value `"ok"` or `"fail"` and sends it with `writeData`. This is what the daemon's `wpctl` path already does on the Linux side. The reply to a "set" is probably unnecessary for us; not verified.
2. **A `volume` message WITHOUT `value` is a "query".** Branch at 0x33918-0x33c98: it calls `CVolume::GetSystemVolume(int&)`, and only if the result is in **0..100 inclusive** (`tbnz w8,#31` negative check, `subs w8,w8,#0x64; b.gt` at 0x33960-0x33970) it builds a reply and logs `Volume query succeeded: current Mac volume=%d%%.` The string constants used in the reply are `"result"`, `"ok"`, `"query"`, `"volume"`, `"value"`. On failure (0x33f90-0x34270) the reply uses `"result"`, `"fail"`, `"query"`, `"volume"`, `"message"`, `"volume read failed"`.
3. **This matches what the device sends:** the bare `{"action":"volume"}` marker at the start of every slider gesture (docs/protocol.md) is, by this reading, the device asking the host for the current volume. The Linux daemon does not answer it, which fits operator's observation on 2026-10-09 that the slider position resets on every touch. This is a hypothesis; the reply has never been sent. **Verified on hardware (Stage 3b and 3c, 2026-10-09):** answering the marker made the slider start at the reply value.
4. **The reply value is a percent, 0..100** (the SDK rejects anything else).
5. **Resolved by T14 (`docs/volume-reply-decode.md`) and the hardware tests.** The reply is flat, has no `action` key, `value` is an integer, and the SDK serialises keys alphabetically. The frame `{"query":"volume","result":"ok","value":N}` was accepted by the device (Stage 3b and 3c). Still unconfirmed: whether key order matters to the firmware, and what the device does with a `"result":"fail"` reply or with mute.
6. **Also present:** `toggle_mute` (calls `CVolume::ToggleMute` then `IsMuted`, replies with key `"muted"`) and `query` strings; mute is not mapped in our daemon. Not traced further.

### volume query reply: VERIFIED ON HARDWARE (2026-10-09, Stages 3b and 3c)
- Sent by the daemon (task T13) in direct response to each bare `{"action":"volume"}` marker: `{"query":"volume","result":"ok","value":N}` plus `\n`, N an integer percent 0..100 of the configured cap (so a system volume equal to the cap reports 100).
- Stage 3b: 5 replies, all `value`=100 (system volume 0.30, cap 0.3). Operator: the slider jumps to the top and states 100.
- Stage 3c: replies 100, 50, 3 as the system volume went 0.30, 0.15, 0.01. Operator: the slider started at 100, then 50, and stayed under 10 where he left it.
- Not tested: mute, long holds, reconnect mid-gesture. See report R9.

### Correction to docs/protocol.md section 3.3 and the daemon default range
Our earlier note said `volume.value` ranges 4 to 50 (from the first two captures, 60 values, max 42). The later captures, `capture-20261009-111333.log` (control 2) and the others combined, hold **781 values from 0 to 98**; 18% are above 50. The SDK's own range check of 0..100 says the same. So the slider is a **0..100 percent**, and the daemon's `volume_min=4`, `volume_max=50` defaults (src/mass80d/config.py) clip everything above 50 to the cap and map the rest wrongly. This is a bug in our default, found by Stage 3 and this trace. Fixed in Stage 3b (T13): the daemon defaults are now 0..100 and the cap rescales the slider onto 0..cap.

## NOT known (do not guess)
1. The exact JSON of every reply (key order, types, units, whether `"action"` is echoed, string vs number).
2. How `get_time` reply is encoded (epoch? formatted string? separate `time`/`date` fields?).
3. Framing: newline-terminated? Raw JSON? Length limits.
4. Whether the firmware tolerates unsolicited/malformed writes.
5. Whether a frame/image streaming mode exists (MJPEG task name seen in the device's debug output).

## Next steps
- Dig the exact reply shapes out of the binary: look for format strings or JSON templates in `__cstring`/`__const` of `libKeyboardSDK.dylib` and the main `MWKEYS Portal` executable (still static only).
- Optionally disassemble the reply-building functions (`objdump`/`otool` availability to be checked; no installs).
- Only after the shapes are known, propose a controlled write plan: one message, dry-run first, you watching the screen and approving each write.

## Update 2026-10-09 (late): cover size and swipes
- `get_cover` replies of 480x480 JPEG (quality 60, 16 to 47 KB per frame, real Spotify art) are accepted and drawn full screen with the Dark theme (Stages 11a and 11b). A 480x480 test picture in Stage 9n loaded offset once. The SDK builder itself caps the size at 240 (`docs/cover-decode.md` section 3); our reply is deliberately off-spec and it works. See `docs/cover-decode.md` "Hardware follow-up 2".
- A swipe on the music page sends `music_prev` or `music_next`, the same events as the buttons (11b: 8 `previous`, 1 `next`, all executed as `playerctl -p spotify ...`).

## Update 2026-10-09 (night): get_performance VERIFIED ON HARDWARE (Stages 12a and 12b)
- The six-field reply `{"action":"get_performance","memory_usage":N,"cpu_usage":N,"cpu_freq":N,"cpu_temp":N,"net_upload":N,"net_download":N}` (int32 values, compact, one `\n`) is accepted and drawn by the screen's performance page. Units confirmed by operator's reading of the screen: memory and CPU in percent, frequency in MHz, temperature in degrees C, network in KB/s. Stage 12a (one frame, 22:17:28): Operator read 3 / 38 / 3897 / 27 / 2 / 44, exactly the values sent; text, units and bars correct; no flicker or freeze.
- Stage 12b (no frame limit, 22:23:07 to about 22:29:30, 129 frames, 0 warnings, 0 USB problems): under load the CPU %, temperature, memory and network values and bars all moved correctly (sent ranges: CPU 1 to 54 %, 38 to 55 C, memory 27 to 36 %, upload up to 927 KB/s, download up to 39,346 KB/s).
- **Open: the CPU clock bar.** At 3,897 MHz it was about 80 % full (Stage 12a). Across 3,503 to 4,727 MHz sent in 12b it did not move (operator saw it unchanged at about 3,500 and 5,000 MHz; the log's maximum sent frequency was 4,727, so the 5,000 reading did not come from our frames). The label is right. The SDK reply has no field for a maximum or a scale (all keys checked), the vendor guide gives the bar a fixed 0..100 range, and the screen firmware (which would define the scale) is not among the vendor files. Operator chose to leave the bar as it is. Not tested: a sweep of test frequencies to map the bar.
