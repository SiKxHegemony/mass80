# Vendor `get_cover` reply: static decode (T19)

Date: 2026-10-09. Method: read-only static reading of `vendor/x/sdk.asm` (arm64, `libKeyboardSDK.dylib`), plus `llvm-objdump --macho -s` on the dylib to resolve CFString and vtable constants. Nothing executed, no device, no git, no network. Symbols shortened by hand: `J` = `nlohmann::basic_json<std::map,...>`, `JREF` = `nlohmann::detail::json_ref<J>`.

Labels: CONFIRMED BY CODE (instruction cited), INFERRED (basis stated), UNKNOWN.

Calibration read first: `docs/music-info-decode.md` (flat object, compact dump, std::map key order, `writeData` adds one `\n`, 0x200000 limit) and `docs/volume-reply-decode.md`.

## Call path

- Dispatcher `SpotifyOAuthServices::handleJsonCommand`, `"get_cover"` literal at 0x2eed4 (compare), branch 0x2eed4-0x2f190. CONFIRMED BY CODE.
- Player object = `this+0x158`. Null -> `w8=1` stored to `[sp,#0x1ecc]`, jump to cleanup, NOTHING sent (0x2eee8-0x2eefc).
- Vtable `0xaa200` (`llvm-objdump -s --section=__DATA_CONST,__const`): slot 0x58 (`0xaa258`) = 0x4470 `get_music_info`; slot 0x60 (`0xaa260`) = **0x6334 `CSMTCMusicplayer::get_cover`**; slot 0x68 (`0xaa268`) = 0x737c `get_cover_prev`; slot 0x70 (`0xaa270`) = 0x7398 `get_cover_next`. The `get_cover` branch does `2f16c ldr x0,[x8,#0x158]`, `2f174 ldr x8,[x8,#0x60]`, `2f188 blr x8` with x1=&index, x2=&width, x3=&height (see 5).
- The builder is 0x6334-0x7018 (not 0x6380; 0x6334 is the prologue).
- Send callback: the `std::function` at `this+8`, same lambda `$_1` -> `writeData(..., true)` as in T14/T16 (see section 7). The builder tests it at 0x6ca4 (`function::operator bool`, `tbz w0,#0,0x6f88`) and sends at 0x6cdc (`function::operator()`).

## Request parsing in the dispatcher (needed for 3 and 5)

```
2ef10 contains("width")        2ef38 operator[]("width")  2ef48 is_number_integer  (tbz -> 2effc)
2ef64 contains("height")       ...                          is_number_integer
2efb8 contains("index")        ...                          is_number_integer
2effc mov w8,#1 ; str w8,[sp,#0x1ecc] ; b 0x36e10        ; any missing / non-integer field -> nothing sent
2f018-2f064  width  = get<int>; clamp(width,  lo=1 (2f050), hi=0x400 (2f05c))
2f0b0-2f0d8  height = get<int>; clamp(height, lo=1 (2f0c0), hi=0x400 (2f0d0))
2f120-2f148  index  = get<int>; clamp(index,  lo=0 (2f134 str wzr), hi=0x400 (2f140))
```
CONFIRMED BY CODE. The three fields are all required integers. The dispatcher bounds are width/height 1..1024 and index 0..1024. The builder then clamps width/height again to a tighter maximum (section 3).

## 1. Reply object: FLAT, THREE keys. CONFIRMED BY CODE

```
6a54 "action"      6a74 "get_cover"      6a88 mov x8,#2 ; 6a9c JREF(initializer_list)   ; pair 0
6ac8 "index"       6ae0 ldr x1,[sp,#0x400] ; 6ae4 JREF<const int&>                        ; pair 1
6b30 "image_data"  6b4c add x1,sp,#0x3d8   ; 6b50 JREF<std::string&>                      ; pair 2
6b98 mov x9,#0x3   ; 6bb0 mov w4,#2 (object) ; 6bb4 basic_json(initializer_list,bool,value_t)
```
- Outer initializer list size is 3 (`mov x9,#0x3` at 0x6b98), each inner pair size 2 (`mov x8,#0x2` at 0x6a88, 0x6af8, 0x6b64). No nesting beyond the key/value pairs. No mime, width, height or format key is echoed: only `action`, `index`, `image_data`.
- Types: `action` JSON string (`JREF<const char(&)[10]>`, `RA10_Kc`, 0x6a78). `index` JSON integer (`JREF<const int&>`, `IJRKiE`, 0x6ae4; value = the clamped request index, section 5). `image_data` JSON string (`JREF<std::string&>`, 0x6b50), never a JSON `null` (section 4).

## 2. Image format: JPEG, quality 0.6, RAW base64, no prefix. CONFIRMED BY CODE (meaning of constants from the AppKit enum: INFERRED from general knowledge)

```
68d8 mov  w8,#0x999a
68dc movk w8,#0x3f19, lsl #16     ; w8 = 0x3f19999a
68e0 fmov s0,w8                   ; float
68e4 bl numberWithFloat:
6900 ldr x8,[x8,#0xe8] ; _NSImageCompressionFactor ; 6904 ldr x3,[x8]
6908 bl dictionaryWithObject:forKey:
6928 mov x2,#0x3                  ; type argument
692c bl representationUsingType:properties:     ; on NSBitmapImageRep
```
- Float: `0x3f19999a` is IEEE-754 single 0.6 (0.8 would be `0x3f4ccccd`). Property key is the symbol `_NSImageCompressionFactor` (imported symbol named in the literal pool comment at 0x6900). So quality = **0.6**. CONFIRMED BY CODE (value); the key's meaning (JPEG compression factor, 0..1) is INFERRED from the AppKit name.
- `x2 = 3`. `NSBitmapImageFileType`: TIFF 0, BMP 1, GIF 2, **JPEG 3**, PNG 4, JPEG2000 5. So the output is **JPEG**. The numeric value 3 is CONFIRMED BY CODE; the enum mapping is INFERRED from the AppKit header (not in the binary).
- Prefix: NONE. The only literals referenced anywhere in the builder are `"null"` (0x6380), `"action"`, `"get_cover"`, `"index"`, `"image_data"` (0x6a54-0x6b30) and the CFStrings in section 4. Bytes are base64-encoded directly: `6954 bytes`, `696c length`, `6984 bl base64_encode(const unsigned char*, unsigned)` -> std::string at `sp+0x2a8`, move-assigned into the image string at 0x6998. No `data:`, no concatenation (`operator+` appears only once, at 0x63bc, to build the cache key, section 4). So `image_data` = raw base64 text of the JPEG file.
- The `image/jpeg` (0x9dd3b) and `image/png` (0x9dd46) literals are NOT used here: `python -B tools/sdk_xref.py image/jpeg image/png artworkData --no-artwork` printed `image/jpeg ... 3 refs: _http_content_type_str, _http_content_type_enum, _http_content_type_str_by_suffix` (same three for `image/png`). They belong to the HTTP server content-type helpers. CONFIRMED BY CODE. (The tool prints 0 refs for `artworkData` and `--no-artwork`; this is its known limit with CFStrings, which are referenced through the `__cfstring` table, not ADRP+ADD of the cstring. See section 4.)
- Base64 variant: table `__ZL12base64_chars` = `ABC...Zabc...z0123456789+/` (checked with `llvm-objdump -s --section=__TEXT,__cstring`, at 0x9d3bd); `base64_encode` has a pad character `mov w1,#0x3d` ('=') at 0x7a5b0. Standard alphabet with `=` padding. CONFIRMED BY CODE. No newline constant (0x0a / 0x0d) in the 189-line function (grep count 0), so no line wrapping. INFERRED from the absence of those constants.

## 3. Size: width and height, stretch to fill, max 240, scales up. CONFIRMED BY CODE

```
6644 ldr x0,[sp,#0x3f8]   (3rd arg = width)    6648 add x1,sp,#0x358 ; 664c w8=1 -> lo = 1
6654 add x2,sp,#0x354 ; 6658 mov w8,#0xf0 ; 665c str -> hi = 240
6660 bl std::clamp<int>   ; result -> [sp,#0x35c]        = clamped width
6678 ldr x0,[sp,#0x3f0]   (4th arg = height)   667c-6688 lo = 1, hi = 0xf0 (6688 mov w8,#0xf0)
6694 bl std::clamp<int>   ; result -> [sp,#0x350]        = clamped height
66ac-66cc scvtf d1,<w> ; scvtf d0,<h>  -> NSSize{w,h} as doubles
6738 bl initWithSize:     (NSImage alloc'd at 6710)
6754 bl lockFocus
67ac..6850  dest rect = (0,0,w,h) ; fromRect = NSZeroRect (6808 _NSZeroRect) ; 6848 mov x2,#1 (operation) ; 6840 fmov d16,#1.0 (fraction)
684c bl drawInRect:fromRect:operation:fraction:
685c bl unlockFocus
686c bl TIFFRepresentation
```
- Arguments: the dispatcher passes x1=&index (0x1e14), x2=&width (0x1e34), x3=&height (0x1e24) (0x2f160-0x2f184 and the stores at 0x2f078 / 0x2f0ec / 0x2f16c), and the builder spills them to `[sp,#0x400]`, `[sp,#0x3f8]`, `[sp,#0x3f0]` (0x6358-0x6360). So `[sp,#0x3f8]` = width, `[sp,#0x3f0]` = height. libc++ `clamp(v, lo, hi)` takes `x0=&v, x1=&lo, x2=&hi`, so **both `0xf0` constants are the maximum (240), the low bound is 1**. They are not a default and not a minimum.
- Both `width` and `height` are used: the output NSImage size is (clamped width) x (clamped height). Aspect ratio is NOT kept and there is NO padding: the source is drawn into the whole destination rect `(0,0,w,h)` from `NSZeroRect` (whole source image), i.e. stretched. The operation value is 1 (`NSCompositingOperationCopy` in the AppKit enum: INFERRED from the header) with fraction 1.0. Because the destination size is the requested size, a small source is scaled UP and a large source scaled down.
- For a 480 x 480 request: 0x1e34 = clamp(480,1,1024) = 480, then the builder clamps 480 to 240 -> a **240 x 240** image (points). CONFIRMED BY CODE (the clamp). The pixel dimensions of the resulting JPEG are UNKNOWN: `NSImage lockFocus` + `TIFFRepresentation` use the display backing scale, so on a Retina Mac the bitmap may be 480 x 480 pixels for a 240 x 240 point image. That is INFERRED from AppKit behaviour, not from the binary, and was never observed. Whether the keyboard screen wants 240 or 480 is UNKNOWN.
- Non-square requests (e.g. 100 x 400): output 100 x 240, source stretched (CONFIRMED BY CODE).

## 4. Source of the artwork, failure paths, cache. CONFIRMED BY CODE (mostly)

Source:
```
64b0 mov w8,#1 ; 64b4 bl runMediaRemoteAdapter(bool)     ; argument TRUE (get_music_info passes false)
64e4 objectForKeyedSubscript: x2 = 0xaaf30
6504-650c  same key again (second lookup)
6520 NSNull class ; 6534 isKindOfClass:            ; value of NSNull -> skip
6578-6584  x2 = 0xaaf50 ("\n") ; x3 = 0xaaf70 ("")
6588 bl stringByReplacingOccurrencesOfString:withString:
65c0 bl initWithBase64EncodedString:options:   (x3 = 0 options)
```
CFString objects (from `llvm-objdump --macho -s --section=__DATA_CONST,__cfstring`; the object at 0xaaf30 has cstring ptr `0x99e6f` len 11, the object at 0xaaf50 has ptr `0x99e7b` len 1, the object at 0xaaf70 has ptr `0x99e1a` len 0; `__cstring` dump: `0x99e6f artworkData`, `0x99e7b` (one newline char), `0x99e1a` empty):
- dictionary key = `artworkData`. The dictionary is the JSON from the external adapter (`runMediaRemoteAdapter`, T16 section 9). CONFIRMED BY CODE (key). What the adapter prints (format of the field, whether it is a JPEG or PNG) is UNKNOWN (external perl helper, not analysed).
- Replacement: every `"\n"` is replaced by `""` (strips newlines from the base64 text). CONFIRMED BY CODE.
- `runMediaRemoteAdapter(true)`: the `bool` is the arg `[x29,#-0x29]` (0x54bc); at 0x5648-0x566c `tbnz w8,#0,0x566c` skips `addObject:` of CFString `0xab030` (ptr `0x99f76` = `--no-artwork`, len 12) when the bool is TRUE. So `get_cover` runs the adapter WITH artwork, `get_music_info` (arg false) adds `--no-artwork`. CONFIRMED BY CODE.
- Source image format can be anything `NSImage initWithData:` accepts; it is re-encoded to JPEG anyway.

Failure paths. All the early exits jump to 0x6a28 -> 0x6a2c, i.e. the code that BUILDS AND SENDS the reply. They do not return early and do not send an error object. The image string is initialised at 0x6380-0x6384 from the literal `"null"` (std::string ctor with `"null"`) and is only overwritten on success (0x6998), so every failure sends **`"image_data":"null"`** (the 4-character string `null`, not JSON null). CONFIRMED BY CODE for the common flow (the string type is `JREF<std::string&>`).

| Exit | Condition | Lands | Sent |
|---|---|---|---|
| 0x64cc `cbz x8` | adapter returned nil dictionary | 0x6a28 | `"null"` |
| 0x64f4 `cbz x8` | no `artworkData` entry | 0x6a28 | `"null"` |
| 0x6544 `tbnz` | `artworkData` is NSNull | 0x6a28 | `"null"` |
| 0x65d8 `cbz x8` | base64 decode failed (nil NSData) | 0x6a24 -> 0x6a28 | `"null"` |
| 0x65fc `b.ls` | decoded NSData length 0 | 0x6a24 -> 0x6a28 | `"null"` |
| 0x663c `cbz x8` | `NSImage initWithData:` nil | 0x6a20 -> 0x6a28 | `"null"` |
| 0x6884 `cbz x8` | `TIFFRepresentation` nil | 0x6a1c -> 0x6a28 | `"null"` |
| 0x6944 `cbz x8` | `representationUsingType:` nil | 0x6a18 -> 0x6a28 | `"null"` |

Cache (lock_guard on the mutex at `this+0x88`, strings at `this+0x100` title, `this+0x118` artist, `this+0x178` cached key, `this+0x190` cached image):
```
639c-63a0  lock_guard(this+0x88)
63b0-63bc  key = title(this+0x100) + artist(this+0x118)   -> sp+0x3c0   (operator+ at 63bc)
63f0-63f4  lock_guard again
6400 empty(key)           tbnz -> 6498   ; empty key -> no cache lookup
6418 key == this+0x178 ?  tbz  -> 6498   ; different key -> miss
6428 empty(this+0x190)    tbnz -> 6498   ; nothing cached -> miss
6444 image = this+0x190 ; 644c-6450 hit flag = 1
64a4 hit flag set -> 0x6a2c (build reply WITHOUT calling the adapter)
69c0 key empty -> skip; else 69d4 this+0x178 = key ; 69e8 this+0x190 = image   ; stored only after a successful encode
```
- The cache is keyed ONLY by title+artist (concatenated, no separator, no size). A repeat request for the same track returns the cached `image_data`, whatever `width`/`height` the new request asks for. CONFIRMED BY CODE. (An odd consequence: the first size requested wins until the track changes.)
- Failures are not cached (the store at 0x69d4-0x69e8 is skipped by every failure exit) so a missing artwork re-runs the adapter on every request. CONFIRMED BY CODE.
- The cache key uses the title/artist from the last `get_music_info` update (`this+0x100/0x118`, T16 section 3). A `get_cover` arriving before the `get_music_info` has stored the new title would key on the old track. INFERRED.
- Unsolicited send on track change: none seen. `get_cover` is reached through the vtable slot 0x60 only by the dispatcher branch above (the other `ldr x8,[x8,#0x60]` hits in the asm are in unrelated functions such as streambuf `xsputn` at 0x3eb28). No timer or change callback in the function. INFERRED (absence; a full caller search of all `blr` sites was not done). Consistent with the hardware log: the screen asked, we did not answer, nothing else arrived.

## 5. `index`. CONFIRMED BY CODE

- Request `index` is parsed as an integer and clamped to 0..1024 in the dispatcher (0x2f134 `str wzr` low, 0x2f140 `#0x400` high).
- Inside `get_cover` the argument is spilled to `[sp,#0x400]` (0x6358) and used once: at 0x6ae0-0x6ae4 as the value of `"index"` in the reply. It is not used to select a track, a queue item, or the artwork. So the reply echoes the clamped request index, and the artwork is always the current player's current artwork. What the device means by `index` is UNKNOWN. The device sent 0 in the logged request.

## 6. `get_cover_prev` and `get_cover_next`. CONFIRMED BY CODE

- Dispatcher branches at 0x2f1a0 (`"get_cover_prev"`) and 0x2f46c (`"get_cover_next"`): same parse and clamp (width 1..0x400 at 0x2f328, height at 0x2f39c, index 0..0x400 at 0x2f400-0x2f40c for prev; same for next at 0x2f5f4-0x2f6d8), same null-player and missing-field exits. They call vtable slot 0x68 (`2f440 ldr x8,[x8,#0x68]`, `2f454 blr`) and slot 0x70 (`2f70c ldr x8,[x8,#0x70]`, `2f720 blr`).
- Slot 0x68 = 0x737c `CSMTCMusicplayer::get_cover_prev`, slot 0x70 = 0x7398 `get_cover_next`. Each body is 7 instructions: `sub sp,sp,#0x20; str x0..x3 to the stack; add sp,sp,#0x20; ret` (0x737c-0x7394 and 0x7398-0x73b0). They store their arguments and return. They are no-ops for this macOS player: no reply, no state change. The base-class `IAbstractMusicplayer::get_cover*` bodies (0xd77c, 0xd798, 0xd7b4) are the same stub.
- So the SDK sends NOTHING for `get_cover_prev` / `get_cover_next`; there is no `get_cover` object for them. If the device ever sends them, the vendor SDK stays silent.

## 7. Serialisation. CONFIRMED BY CODE (compact, newline); key order INFERRED

```
6cb8 mov w1,#-1    ; indent -1 = compact
6cbc mov w2,#0x20  ; indent char
6cc0 mov w4,#0     ; error handler (strict)
6cc4 and w3,w4,#1  ; ensure_ascii = false
6cc8 bl J::dump
6cdc bl std::function::operator()   ; send, no TraditionalToSimplified call in this function
```
- Compact, raw UTF-8 (irrelevant here: base64 text is ASCII). No `StringHelper::TraditionalToSimplified` call: the list of all `bl` targets in 0x6334-0x7018 contains no such call (I printed the whole set, 24 `nlohmann` calls, no opencc).
- Key order on the wire: `J` is `basic_json<std::map,...>` (mangled), so keys are sorted: `action`, `image_data`, `index` (`image_data` < `index` since `'m' (0x6d) < 'n' (0x6e)` at the second character). This is INFERRED from `std::map` ordering, never observed on the wire (same status as T16).
- Trailing newline: `writeData` appends one `\n` (T14). Same sender (`std::function` at `this+8`, set at `runService`, T16). INFERRED for this builder (the `std::function` object is the same one; I did not re-trace it).
- Size limit: `writeData` rejects null/zero/over-0x200000 (2,097,152) bytes (T14/T16, not re-read here). The builder itself has no cap. A 240 x 240 JPEG at 0.6 base64 is far below 2 MiB.
- ESTIMATE (not a measurement): a 240 x 240 px photographic JPEG at quality 0.6 is typically about 8-25 KB, so about 11-34 KB of base64; if the bitmap is 480 x 480 px (Retina scaling, section 3), about 25-80 KB, so about 34-110 KB of base64. Frame JSON overhead is about 50 bytes. Real covers vary a lot; this is a reasoned range only.

## Confidence table

| # | Answer | Confidence |
|---|---|---|
| 1 | Flat, 3 keys `action`/`index`/`image_data`; list size `mov x9,#3` | CONFIRMED BY CODE |
| 1b | `action` string `"get_cover"`, `index` integer (clamped request value), `image_data` string | CONFIRMED BY CODE (`JREF` types) |
| 2a | `numberWithFloat 0x3f19999a` = 0.6 under `NSImageCompressionFactor` | CONFIRMED BY CODE; key meaning INFERRED |
| 2b | Type constant 3 = JPEG | value CONFIRMED BY CODE; enum meaning INFERRED (AppKit header) |
| 2c | `image_data` is raw base64, no `data:` prefix; `image/jpeg`/`image/png` belong to HTTP helpers | CONFIRMED BY CODE (xref output) |
| 2d | Standard alphabet, `=` padding, no line wrapping | alphabet/padding CONFIRMED BY CODE; no wrapping INFERRED (no 0x0a/0x0d in function) |
| 3a | width and height both clamped to 1..240 in the builder (0xf0 = max) | CONFIRMED BY CODE |
| 3b | Output NSImage = w x h, source stretched, no aspect ratio, no padding, upscales | CONFIRMED BY CODE; Copy operation name INFERRED |
| 3c | 480 x 480 request -> 240 x 240 (points) | CONFIRMED BY CODE |
| 3d | Pixel size of the JPEG (240 or 480) | UNKNOWN (Retina behaviour INFERRED only) |
| 4a | Source = adapter JSON key `artworkData`, newlines stripped, `--no-artwork` NOT passed | CONFIRMED BY CODE |
| 4b | Adapter output format of `artworkData`, whether the adapter returns it at all | UNKNOWN |
| 4c | Every failure path still sends a reply with `"image_data":"null"` (string) | CONFIRMED BY CODE |
| 4d | Cache by title+artist only, size ignored on hit, failures not cached | CONFIRMED BY CODE |
| 4e | No unsolicited send on track change | INFERRED (no caller search of every `blr`) |
| 5 | `index` clamped 0..1024, echoed, otherwise unused | CONFIRMED BY CODE |
| 6 | prev/next = 7-line no-op stubs, nothing is sent | CONFIRMED BY CODE |
| 7a | Compact dump, `w1=-1`, no opencc | CONFIRMED BY CODE |
| 7b | Key order `action,image_data,index`; trailing `\n` | INFERRED (std::map; T14 writeData) |
| 7c | Reply size ~11-110 KB base64 | ESTIMATE |
| - | The keyboard screen displays this reply, and which size/format it accepts | UNKNOWN (never sent) |

## What we would have to send for a 480 x 480 request

Request seen on hardware: `{"action": "get_cover", "height": 480, "index": 0, "width": 480}`.

SDK-exact reply (one line, then one `\n` added by the writer), `<B64>` is the standard base64 (padded, no newlines) of a JPEG file:

```
{"action":"get_cover","image_data":"<B64>","index":0}
```

- `index`: integer, the request's value (0).
- Image: JPEG, quality 0.6 (`NSImageCompressionFactor`), the vendor produces **240 x 240** for a 480 x 480 request (width and height each capped at 240), stretched to fill the requested box, no padding. The decision whether to send 240 x 240 (SDK-exact) or 480 x 480 (what the request asked) needs a hardware test.
- No-artwork / any failure, SDK-exact: `{"action":"get_cover","image_data":"null","index":0}` (the string `null`; no error object, never silence).
- Not to send: nothing for `get_cover_prev` / `get_cover_next` (the SDK sends nothing).

Facts that remain unconfirmed:
1. That the keyboard screen renders this reply at all, and whether it accepts the string `"null"` as "no cover".
2. Which pixel size it expects: the SDK yields 240 pt (maybe 480 px on Retina); the request asked 480.
3. Whether the device needs the `index`/`action` keys, key order, or the trailing `\n` (the SDK sends sorted keys, compact, plus `\n`; never observed).
4. The format and size of the adapter's `artworkData` on this Linux host (our source of art is a different pipeline, e.g. MPRIS `mpris:artUrl`); how to re-encode it to JPEG q0.6 is a new-dependency question (Pillow is not confirmed to be installed) and is out of scope here.
5. Whether the device re-sends `get_cover` after an unanswered one, and how long it waits.
6. Whether the device ever sends `get_cover_prev`/`get_cover_next` and what it expects back (SDK: no reply).
7. Whether a reply of 30-100 KB in one write is accepted by the device's CDC receive path (SDK limit 0x200000 is host-side only).


## Hardware follow-up (added 2026-10-09 after Stages 9b, 9c and 9d)
- The unknowns above were tested with the daemon built from this decode (`docs/plans/2026-10-09-cover-art.md`). Confirmed on the device: the screen draws a reply in this shape (`{"action":"get_cover","image_data":"<B64>","index":0}`, a 240x240 JPEG at quality 60, 5,953 to 26,461 bytes per frame); a single write of 15,781 to 26,461 bytes is accepted, with no glitch and no USB problem; the request seen was `{"action": "get_cover", "height": 480, "index": 0, "width": 480}`, once per track change, about 0.5 s after the first frame of a new track. Item 1 of the unconfirmed list (does the screen render it) and item 7 (a 30-100 KB write) are answered for sizes up to 26 KB; item 2 (the pixel size it expects) is partly answered: a 240x240 image is drawn as a square inside a wider box with white sides (operator's description); items 3, 5 and 6 are partly answered (the screen did not re-ask after an unanswered request in Stage 8 within the minutes watched, and sent no `get_cover_prev` or `get_cover_next` in 15 requests); item 4 is irrelevant to this host (the art comes from `mpris:artUrl`, not the adapter).
- Still unknown: the real size of the cover container, the `"null"` shape on the device, and whether the official app shows the same white sides.

## Hardware follow-up 2 (added 2026-10-09 after Stages 9e to 11b): the layout, the theme and 480x480
- **The earlier layout reading was wrong.** Stages 9b to 9d suggested a middle section with placeholder bands above and below. Those observations were of a 240x240 cover in a theme whose cover element is that size, plus a half-offset state after a swipe. Operator's later description: the placeholder is full screen until a swipe leaves it offset (the bottom of one image at the top, the top of the next at the bottom).
- **Theme (read from `vendor/themes.zip` and the vendor guide `vendor/x/zip/createThemes/Themes_EN.md`).** The screen is 480x480 (guide section 10). The music page `cover` element is a fixed `img`, sized by the theme: `dark` 480x480 at (0,0) with the title, artist and a progress slider drawn over it and ONE play/pause button top right; `asuka` 240x240 at (45,45); `snowMonster` 240x240; `pixelStyle` 120x120; `ReelToReel` 80x80. No theme JSON describes previous or next cover elements. Operator first answered Asuka, then said he thinks it is Dark: the idle music page shows a full-screen record image, and while playing he sees only a play/pause button top right (matches `dark`, not Asuka). Treated as Dark; not read from the device.
- **Stage 9e (480x240 grid, accepted):** drawn without glitch. **Stage 9f (240 box on a 480x240 canvas, flat grey 32):** the grey was there and covers loaded quickly after skipping, but 32 was much too dark. Grey and hue tuning, six rounds of swatch pictures (stages 9h to 9m): neutral (49,49,49) looked green, tints with red 5 to 6 levels above green looked red or brown, E (50,50,50) was the closest but too light and F (48,48,48) too green. A finding from that work: JPEG quality 60 with default 4:2:0 chroma shifts a flat tinted colour by 1 to 2 levels (for example (51,48,51) comes out (51,46,52)); neutral greys survive within 1. That work is superseded by the 480x480 result below and the grey is no longer used by the 11a config.
- **Stage 9n (one 480x480 test JPEG, 14,894 bytes, 19,909 as a frame):** accepted and drawn whole (four coloured quadrants, red border intact) but the first load was offset (only the top half of the screen) with a glitch of about a second, and operator had to drag it into position. Host log: request and write 0 ms apart, music frames every 1.0 s, 0 warnings, 0 USB problems.
- **Stage 11a (real art, `configs/stage11a-cover-480.toml`, size 480, canvas 480x480, no grey, `--no-actions`, 21:46:57 to 21:51:30):** operator: "images look perfect taking up whole screen"; skipping tracks in Spotify loads new art quickly. 5 requests, 5 fetches, 5 frames of 19,701, 46,869, 34,261, 16,377 and 22,929 bytes (largest accepted so far, 46,869), 0 warnings, 0 USB problems. The 9n offset did NOT appear on real art. Why it appeared in 9n is not established (the test picture, or the first load after a daemon start, are both possible).
- **Stage 11b (same config, actions ON, 21:51:43 to 21:53:50):** operator swiped on the screen and reported "it works and changes songs and loads new cover art". Log: 9 commands, `previous` 8 and `next` 1, all `playerctl -p spotify`, 5 cover requests, 4 fetched, frames 16,377, 22,929, 16,377, 34,261, 19,701, 0 warnings, 0 USB problems. So a swipe on the music page sends `music_prev` or `music_next` (the earlier statement that swipes send nothing, `docs/protocol.md` section 4, is WRONG for the music page with a track loaded and needs correcting; the earlier capture labels were loose). I read the first no-actions 11a log wrongly (searching for the word "swipe") before checking the would-run lines.
- **Still unknown:** whether the screen ever asks for `get_cover_prev` or `get_cover_next` (0 in all runs; with the Dark theme they may not exist), what the screen shows while it scrolls, how the 9n offset arises, a long soak, the `"null"` shape on the device.
