# Vendor `get_music_info` reply: static decode (T16)

Date: 2026-10-09. Method: read-only static reading of the saved disassembly `vendor/x/sdk.asm` (arm64, `libKeyboardSDK.dylib`), plus `llvm-objdump --macho -s` on the dylib to resolve CFString constants. Nothing executed, no device touched, no git. Symbols are shortened by hand: `J` = `nlohmann::basic_json<std::map,...>`, `JREF` = `nlohmann::detail::json_ref<J>`.

Confidence labels: CONFIRMED BY CODE (instruction cited), INFERRED (basis stated), UNKNOWN.

## Call path (context for every answer)

- Dispatcher `SpotifyOAuthServices::handleJsonCommand`, branch at 0x2ee74-0x2eec4: compares the device `action` with `"get_music_info"` (literal 0x2ee80); loads `this+0x158` (the `CSMTCMusicplayer*`, stored at 0x37308); if it is null, sets an error code and sends NOTHING (0x2ee98-0x2eea8); else calls virtual slot at `vtable+0x58` (0x2eeb4-0x2eebc). Vtable `0xaa200` slot 11 (`0xaa258`) = `0x4470` = `CSMTCMusicplayer::get_music_info` (rebase table / `llvm-objdump -s` of `__const`). CONFIRMED BY CODE.
- The builder is therefore `get_music_info` at **0x4470-0x53b0** (not 0x4020, which is `CSMTCMusicplayer::start`). The string filter `$_0` is at 0x5824-0x5d74.
- The send callback (`std::function` at `this+8`) is set in `runService` at 0x372dc to lambda `$_1` (0x636f8), which just calls `writeData(ptr, c_str, length, true)` (0x63744). Same `writeData` as the get_time and volume replies.

## 1. Flat or nested? FLAT, with `"action":"get_music_info"`. CONFIRMED BY CODE

```
48c4 "action"          -> JREF<char[7]>    48e4 "get_music_info" -> JREF<char[15]>
48f8 mov x8,#2 ; 490c  JREF(initializer_list)        ; pair 0 = {"action","get_music_info"}
4934 "progress" ...    4954 add x1,x8,#0xf4 ; 4958 JREF<int&>   ; pair 1
49a4 "duration" ...    49c4 add x1,x8,#0xf0 ; 49c8 JREF<int&>   ; pair 2
4a14 "title"    ...    4a30 add x1,sp,#0x3b0 ; 4a34 JREF<std::string&>  ; pair 3
4a80 "artist"   ...    4a9c add x1,sp,#0x398 ; 4aa0 JREF<std::string&>  ; pair 4
4aec "index"    ...    4b0c str wzr,[sp,#0x2dc] ; 4b10 JREF<int>        ; pair 5
4b5c "state"    ...    4b7c ldrb w9,[x8,#0x85] ... 4b98 JREF<int>       ; pair 6
4be0 mov x9,#0x7 ; 4bf4 and w3,w8,#1 (=1) ; 4bf8 mov w4,#2 (object) ; 4bfc basic_json(initializer_list,bool,value_t)
```
Every pair is `{const char[N], scalar-or-string}`; each pair is built by `JREF(initializer_list)` with count 2 (`mov x8,#0x2`). No nested list. Seven keys, flat.

## 2. JSON types. CONFIRMED BY CODE for all four

| Key | json_ref instantiation (mangled arg) | Type | Evidence |
|---|---|---|---|
| `progress` | `JREF::JREF<int&>` (`IJRiE`) at 0x4958, object field `this+0xf4` | integer | 0x4954 |
| `duration` | `JREF::JREF<int&>` at 0x49c8, field `this+0xf0` | integer | 0x49c4 |
| `index` | `JREF::JREF<int>` (`IJiE`) at 0x4b10 of a stack int zeroed at 0x4b0c | integer, ALWAYS 0 | `str wzr,[sp,#0x2dc]` |
| `state` | `JREF::JREF<int>` (`IJiE`) at 0x4b98 | integer 0 or 1, NOT bool | see below |

`state` detail: `4b7c ldrb w9,[x8,#0x85]`, `4b84 and w9,w9,#1`, `4b88 ands w9,w9,#1`, `4b8c csinc w8,w8,wzr,eq` (w8=0 beforehand), `4b94 str w8,[sp,#0x2bc]`, `4b98 JREF<int>`. So `state` = 1 if the bit is set, else 0. The template argument is `int` (`IJiE`), not `bool` (`IJbE`), so the wire form is `1`/`0`, not `true`/`false`. No `scvtf`/`fcvt` for any of the four (the only float ops are the earlier `fmul` and `fcvtzs` described in section 3). No `title`/`artist` numeric conversions. `title`/`artist` are `JREF<std::string&>` (`IJRSC_E`, `SC_` = the basic_string substitution), i.e. JSON strings.

## 3. Sources and units of `progress` and `duration`. CONFIRMED BY CODE (mapping); units INFERRED

Adapter keys are NSString constants. `adrp x2,0xaa000 ; add x2,x2,#OFF` passes a `__cfstring` object; each cfstring at `0xaa000+OFF` points to a cstring (checked with `llvm-objdump --macho -s --section=__DATA_CONST,__cfstring` and the `__cstring` bytes):

| Code address | CFString | Points to (len) | Used for |
|---|---|---|---|
| 0x45a0 | 0xaae90 | 0x99e14 `title` (5) | title -> `this+0x100` (std::string, 0x4618) |
| 0x4628 | 0xaaeb0 | 0x99e1b `artist` (6) | artist -> `this+0x118` (0x46a0) |
| 0x46b0 | 0xaaed0 | 0x99e22 `duration` (8) | `doubleValue`, `*1000.0` (0x4710), `fcvtzs w8,d0` (0x4734), `str w8,[x9,#0xf0]` (0x4738) |
| 0x4768 | 0xaaef0 | 0x99e2b `elapsedTime` (11) | `doubleValue`, `*1000.0` (0x47a0), `fcvtzs` (0x47c4), `str w8,[x9,#0xf4]` (0x47c8) |
| 0x47f8 | 0xaaf10 | 0x99e37 `playing` (7) | `boolValue` (0x4814), `and w8,w8,#1`, `strb w8,[x9,#0x85]` (0x4844) |

The constant at 0x4704-0x470c is `mov x8,#0x400000000000 ; movk x8,#0x408f,lsl#48` = 0x408F400000000000 = 1000.0 (double). Combined with the JSON pairs in section 1:

- `progress` <- `this+0xf4` <- `elapsedTime` x 1000.0, truncated toward zero to int32 (`fcvtzs`, saturating).
- `duration` <- `this+0xf0` <- `duration` x 1000.0, truncated to int32.

The earlier note said the wiring was untraced; it is now resolved: progress=elapsedTime, duration=duration. The integer goes to the JSON as that int32 (`JREF<int&>`), no other transform. Units: the SDK multiplies by 1000, so the integers are **milliseconds if the adapter reports seconds** (MediaRemote `elapsedTime`/`duration` are seconds as doubles: INFERRED from general knowledge of that API; the adapter output itself is not available here, it is an external helper launched by `runMediaRemoteAdapter`, 0x5340+). If the field is missing or the key is absent, the value is `0.0` (`movi d0,#0` at 0x471c and 0x47ac). The device's expected unit is UNKNOWN (never observed); ms is what the vendor SDK emits.

## 4. `state` and `index`

- `state`: CONFIRMED BY CODE that it is `playing` (adapter, `boolValue`) stored into the byte at `this+0x85` (0x4844) and read back at 0x4b7c. Constructor initialises `this+0x85 = 0` (0x6124c `strb wzr,[x8,#0x85]`). Missing `playing` key -> 0 (0x482c). The meaning on the device (1 = playing) is INFERRED from the name only.
- `index`: CONFIRMED BY CODE that it is the constant `0` (0x4b0c). It is NOT read from any field. `index` in `get_cover*` is a different thing (requested cover index, clamped to 1024) and unrelated. What `index` means to the device is UNKNOWN; the SDK always sends 0.

## 5. `title` and `artist` transformation. CONFIRMED BY CODE, and it is strict

Lambda `$_0` (0x5824-0x5d74) is applied to both strings (calls at 0x487c and 0x4894):

1. Converts UTF-8 to a wide string (`wstring_convert<codecvt_utf8<wchar_t>>::from_bytes`, 0x586c). On exception (0x58c8) it returns `""`.
2. Walks every wide char. A char is KEPT (`push_back`, 0x5bd8) only if it is in this whitelist, else the lambda immediately returns an EMPTY string (`5bf4`..`5c08`, jump to the epilogue; no further chars read):
   - CJK range `0x4e00 <= c < 0xa000` (0x5970-0x5988)
   - ASCII `0-9` (0x30-0x39), `a-z` (0x61-0x7a), `A-Z` (0x41-0x5a)
   - punctuation: `~ ! @ # $ % ^ & * ( ) , . ; : " ' [ ] < > { } - _ | / ` and SPACE (0x7e 0x21 0x40 0x23 0x24 0x25 0x5e 0x26 0x2a 0x28 0x29 0x2c 0x2e 0x3b 0x3a 0x22 0x27 0x5b 0x5d 0x3c 0x3e 0x7b 0x7d 0x2d 0x5f 0x7c 0x20 0x2f; checked one `subs w8,w8,#imm / b.eq 0x5bd0` per char, 0x59f0-0x5bc8)
3. Everything else is NOT in the list: ASCII `? + = \` and the backtick, control chars, non-CJK non-ASCII (accented Latin, kana, hangul, emoji). One such character anywhere makes the whole string `""`.
4. Result converted back with `to_bytes` (0x5c30).

So plain ASCII that only uses the whitelist passes through unchanged. ASCII containing `?`, `+`, `=`, `\` or a backtick becomes `""`. This is a drop-all filter, not a strip.

Second transformation: after the whole reply is dumped (0x4ddc), the **entire JSON text** goes through `StringHelper::TraditionalToSimplified` (call 0x4dec), which uses `opencc` `SimpleConverter::Convert` with config `tw2sp.json` (0x61c0, 0x6248). It is always called (not locale-dependent in this function), and on a converter failure or null converter it returns the input unchanged (0x6220 `cbnz` / 0x6270 catch). Effect on ASCII text: INFERRED none (opencc maps Chinese characters/phrases only); not run. Chinese text is converted from Traditional to Simplified. If the `std::function` at `this+8` is empty the converted string is not sent at all (0x4dfc/0x4e00 `tbz w0,#0` -> 0x5348).

## 6. Length limit. No truncation in the builder. CONFIRMED BY CODE (absence) / UNKNOWN (device)

No `substr`/`resize`/length compare appears in 0x4470-0x53b0 or in `$_0`. The only size check on the way out is `writeData`'s null/zero/over-0x200000-bytes rejection (T14, 0x2aa60-0x2aa7c). A device-side limit is UNKNOWN. We keep the first frame short for that reason.

## 7. Serialisation. CONFIRMED BY CODE (compact, newline); order INFERRED from library behaviour

- `4dcc mov w1,#-1` (indent -1 = compact), `4dd0 mov w2,#0x20`, `4dd4 mov w4,#0` (error handler), `4dd8 and w3,w4,#1` (= 0, `ensure_ascii` false), `4ddc bl dump`. Compact, raw UTF-8 (CJK not `\u`-escaped). Note this differs from get_time, which dumps with indent 4 (T14); hardware accepted our compact get_time.
- `J` is `basic_json<std::map,...>` (mangled name in the ctor/dump symbols), so keys are sorted by `std::less<std::string>`: `action, artist, duration, index, progress, state, title`. The sorted order is standard map behaviour (INFERRED, not observed on the wire); the construction order in code was action, progress, duration, title, artist, index, state.
- Trailing newline: `writeData` appends `\n` if the last char is not `\n` (0x2acac-0x2acd8, T14). So exactly one `\n`. CONFIRMED BY CODE (T14), same function.

## 8. Throttle. CONFIRMED BY CODE: 400 ms, drop-silently, not change-based

```
44a8-44e8  static steady_clock::time_point at 0xac150 (guard 0xac158), initialised to now on first call
44ec  steady_clock::now() ; 4504 operator-(now, last) ; 451c duration_cast<milliseconds> ; 4534 count()
4538  subs x8,x0,#0x190   ; 0x190 = 400 ms
453c  b.ge 0x4550         ; >= 400 ms -> continue
4544  mov w8,#1 ; 454c b 0x5378   ; < 400 ms -> return (no adapter call, NO reply)
4550-4558  last = now (stored BEFORE the adapter runs)
```
So: at most one reply per 400 ms; polls inside the window get no reply at all. It is a throttle on the poll, not a timer and not "send on change": every accepted poll triggers a full reply, even if nothing changed. The device polls about once per second (captures: 42 `get_music_info` polls in `capture-20261009-111333.log`, about 1 s apart, no host reply), so the throttle would not normally bite. The very first call after process start is dropped if it comes within 400 ms of initialisation (the static starts at "now").

## 9. No player / missing data. CONFIRMED BY CODE (paths) / UNKNOWN (what the adapter prints)

- `runMediaRemoteAdapter(false)` (0x5340-0x5794) launches an `NSTask` helper from the app bundle, reads stdout, runs `NSJSONSerialization JSONObjectWithData` and returns the dictionary, or nil (empty output, parse error, launch failure: 0x5758).
- Nil dictionary (0x457c `cbz x8, 0x486c`): the update block is skipped and the reply is built from the object's **previous** values (stale title/artist/times/state). The reply is still sent.
- Dictionary present but a key missing: title -> `""` (0x45fc), artist -> `""` (0x4684), duration/elapsedTime -> 0 (0x471c/0x47ac), playing -> 0 (0x482c).
- Initial object state (never updated yet): `song` at `this+0xf0` zero-initialised (0x619f0-0x61a0c, string members empty), `this+0x85 = 0` (0x6124c). So before any player data the SDK sends `{"action":"get_music_info","artist":"","duration":0,"index":0,"progress":0,"state":0,"title":""}`.
- If `this+0x158` (the player object) is null the dispatcher sends nothing (section "Call path"). Not expected on macOS.
- What the adapter prints with no player active is UNKNOWN (external helper, not analysed); the likely reading is that "no player" most likely yields the stale-or-default frame above, never silence (INFERRED).

## Confidence table

| # | Answer | Confidence |
|---|---|---|
| 1 | Flat, 7 keys, `action`=`"get_music_info"` | CONFIRMED BY CODE |
| 2 | progress/duration/index/state are JSON integers; state is 0/1 not bool | CONFIRMED BY CODE (`JREF<int&>`, `JREF<int>`) |
| 3 | progress=elapsedTime, duration=duration, x1000.0, trunc int32, sent as that int | CONFIRMED BY CODE |
| 3b | Adapter values are seconds, so output is ms | INFERRED |
| 4 | state = `playing` byte; index = constant 0 | CONFIRMED BY CODE; meaning on device UNKNOWN |
| 5 | title/artist whitelist filter; whole string -> `""` on any other char; opencc tw2sp on the whole dump | CONFIRMED BY CODE; ASCII-untouched by opencc INFERRED |
| 6 | No length cap in builder; writeData limit 0x200000 | CONFIRMED BY CODE (absence); device limit UNKNOWN |
| 7 | Compact dump, raw UTF-8, one trailing `\n`, alphabetical key order | compact/newline CONFIRMED; sorted order INFERRED |
| 8 | 400 ms drop-throttle, every accepted poll answered | CONFIRMED BY CODE |
| 9 | Missing data -> defaults / stale values, still sent | CONFIRMED BY CODE; adapter output with no player UNKNOWN |
| - | Device accepts this frame at all / which fields it renders | UNKNOWN (never sent) |
| - | Device unit for progress/duration (ms assumed) | UNKNOWN |
| - | Whether device requires key order, accepts a missing `action` | UNKNOWN |

## Recommended first frame

SDK-exact (compact, alphabetical, integers, ASCII whitelist-safe text), followed by one `\n`:

```
{"action":"get_music_info","artist":"Test Artist","duration":215000,"index":0,"progress":42000,"state":1,"title":"Test Title"}
```

Types: `action` string; `artist`, `title` string; `duration`, `progress` integer milliseconds; `index` integer 0; `state` integer 1 (playing) or 0.

Alternative the evidence does not rule out for the device, NOT what the SDK emits: `"state":true`. It would need a hardware test showing the device renders `1` incorrectly; the SDK itself emits `1`. Our existing `build_get_music_info` in `src/mass80d/replies.py` (read only, not changed) already uses int 0/1 for `state` and int32 for the numbers; its key insertion order is action, progress, duration, title, artist, index, state (not alphabetical); whether `_encode` sorts keys was not checked.

Rules to follow if any real text is sent: keep title/artist to the whitelist in section 5 (no `? + = \` backtick, no accents/kana/emoji), otherwise the SDK would have sent `""` for that field, so the device has never seen such text.

Facts that remain unconfirmed (all need a hardware test with operator watching the screen):
1. The device accepts a `get_music_info` reply at all and which fields it renders.
2. The unit of `progress`/`duration` on the device (ms from the SDK arithmetic; seconds-in-adapter is INFERRED).
3. What `index` and `state` mean to the device (SDK sends constant 0 and the playing bit).
4. Whether key order or a missing `action` matters (SDK sends sorted keys with `action`).
5. Whether compact single-line is parsed for this message (it is for get_time and volume; this builder itself dumps compact, so this is the lowest risk).
6. Whether the device has its own title/artist length limit.
7. Alphabetical wire order is an inference from `std::map`, never observed.
