# Vendor `volume` QUERY reply: static decode (T14)

Date: 2026-10-09. Method: read-only static analysis of the saved disassembly `vendor/x/sdk.asm` (arm64, `libKeyboardSDK.dylib`). Nothing executed, no device touched, no git. Helper used: `tools/_asm_slice.py START END` (throwaway, prints an address range of the asm). Symbols below are shortened by hand.

Confidence labels: CONFIRMED BY CODE (instruction lines cited), INFERRED (from what stated), UNKNOWN.

## Answers

### 1. Flat or nested? FLAT. `value` is a top-level key. CONFIRMED BY CODE
The success builder (0x33918-0x33b50) creates three `json_ref` initializer-list elements, each a 2-item `{key, value}` pair, then one outer list of 3, and constructs the object with `manual_type = 2` (= `value_t::object`):

```
339ac  "result"  -> json_ref<const char[7]>   339d0  "ok"     -> json_ref<const char[3]>
339dc  mov x8,#2 ... 33a00 bl json_ref(initializer_list)   ; pair 0 at frame+0x3b0  = {"result","ok"}
33a30  "query"   -> json_ref                  33a54  "volume" -> json_ref
33a70  mov x8,#2 ... 33a80 bl json_ref(initializer_list)   ; pair 1 at +0x3b0+0x18 = {"query","volume"}
33ab0  "value"   -> json_ref                  33ad8  bl json_ref<int&>(sp+0x1634)  ; the int volume
33af0  mov x8,#2 ... 33b00 bl json_ref(initializer_list)   ; pair 2 at +0x3b0+0x30 = {"value", N}
33b2c  mov x9,#0x3 ; 33b44 and w3,w8,#1 (=1, type_deduction) ; 33b48 mov w4,#2 (object)
33b4c  bl basic_json(initializer_list<json_ref>, bool, value_t)
```
There is no nested initializer list inside a pair: every pair is `{const char*, scalar}`. So `query` holds the string `"volume"` and `value` holds the number. The alternative `{"query":{"volume":N}}` is ruled out.

### 2. Key order. Type is `nlohmann::basic_json<std::map, ...>` (NOT ordered_json). Wire order is ALPHABETICAL: `query`, `result`, `value`.
Construction order in code is result, query, value (above). The mangled type is `basic_json<std::__1::map, std::__1::vector, std::string, bool, long long, unsigned long long, double, std::allocator, adl_serializer, std::vector<unsigned char>, void>`; `std::map` with the default comparator sorts keys. Hence the serialiser emits `query` < `result` < `value`. CONFIRMED BY CODE for the type (mangled name in the ctor/dump symbols); the alphabetical output is INFERRED from standard nlohmann/`std::map` behaviour, not observed. The device very likely does not care about order (INFERRED, never tested).

### 3. Is `"action":"volume"` present? NO. CONFIRMED BY CODE
The only string literals referenced from the start of the query branch to the `writeData` call (0x33918-0x345a8) are `"result"`, `"ok"`, `"query"`, `"volume"`, `"value"`, and the log text `"Volume query succeeded: current Mac volume=%d%%.\n"`. No `"action"` literal (0x99e3f) is referenced in that range; `tools/sdk_xref.py action` prints "refs: 13" but lists only 8 addresses (0x48c4, 0x6a54, 0x2a610, 0x2e8b8, 0x2e8e0, 0x2e90c, 0x30694, 0x38a90), none in 0x33918-0x345a8; it also does not list the get_time one at 0x39398, so the xref tool is incomplete and the primary evidence is the `literal pool for:` grep over the range (12 literal references, none "action"). The set reply (0x3355c-0x33740) and failure reply (0x33f90-0x34270) also have none. By contrast `sendTimeWithCapsState` DOES emit `"action"` (0x39398, literal pool "action", value "get_time" at 0x393b8). So the volume reply differs from get_time in not echoing `action`.

### 4. Type of `value`: JSON INTEGER. CONFIRMED BY CODE
`33ad8 bl json_ref::json_ref<int&>` with `x1 = sp+0x1634`, the `int` written by `CVolume::GetSystemVolume(int&)` (`33920 str wzr,[sp,#0x1634]`, `33924 bl GetSystemVolume(int&)`). No `scvtf`/`fcvt`/string-conversion instruction in 0x33918-0x34524. Range check 0..100 at 0x3395c-0x33970 (`tbnz w8,#31` ; `subs w8,w8,#0x64 ; b.gt` branching to the failure path). So `value` is a bare integer percent in 0..100.

### 5. Trailing newline and writeData path. Same `writeData`; newline is appended by `writeData` itself. CONFIRMED BY CODE
- Serialise: `34544 bl basic_json::dump` with `w1=-1` (indent -1 = COMPACT, no spaces/newlines), `w2=0x20`, `w3=0` (ensure_ascii false), `w4=0`. Sent: `345a8 bl SpotifyOAuthServices::writeData(const char*, unsigned, bool)` with `w3=1`.
- `writeData` (0x2aa34): copies the buffer into a local string (`2aca0`), then `2acac empty?`, `2acbc string::back()`, `2acc4 subs w8,w8,#0xa ; b.eq`, else `2acd4 mov w1,#0xa ; 2acd8 string::push_back('\n')`. I.e. if the last char is not `\n`, append `\n`. Then `2ad64 bl itas109::CSerialPort::writeData(ptr, size)`. So every frame leaves with exactly one trailing `\n`. This is the same function the verified get_time reply uses (`396e4 bl writeData`).
- `writeData` also: rejects null/zero/over 0x200000 bytes (0x2aa60-0x2aa7c), aborts if the serial port is closed (0x2aad8), and drops frames while `isReconnectSuspended()` unless the payload contains `"mac_host_sleep"` (0x2ab74-0x2ac04). Irrelevant to our daemon, noted for completeness.

### Side finding on the verified get_time reply (calibration, important)
- get_time dumps with `39648 mov w1,#0x4` = `indent=4` (PRETTY-PRINTED, multi-line, 4-space indent), whereas the volume reply uses `w1=-1` (compact). So the real SDK sends get_time as an indented multi-line JSON document. Hardware accepted our COMPACT single-line get_time frame, so the device parser tolerates compact JSON (CONFIRMED BY HARDWARE per docs/protocol-host-messages.md). This is the best available evidence that compact single-line volume frames are also parseable.
- `caps` is built from `json_ref<int>` (`394ec`, preceded by `394dc ands w9,w9,#1 ; 394e0 csinc w8,w8,wzr,eq`), i.e. the SDK would emit `"caps":0` or `"caps":1` (integer), not `false`/`true`. Our hardware test sent `"caps":false` and the device accepted it. The doc `protocol-host-messages.md` line "exact caps type on the wire not confirmed" can now be updated: INFERRED integer from the construction call (not hardware-tested with an integer). The earlier SDK key order for get_time would also be alphabetical on the wire (`action`, `caps`, `time`), again an inference.

### 6. Confidence summary

| # | Answer | Confidence |
|---|---|---|
| 1 | Flat; `value` top-level | CONFIRMED BY CODE (0x33918-0x33b50) |
| 2 | std::map-based basic_json; wire order query, result, value | type CONFIRMED; sorted output INFERRED (library behaviour) |
| 3 | No `"action"` | CONFIRMED BY CODE (no literal ref in range) |
| 4 | Integer 0..100 | CONFIRMED BY CODE |
| 5 | Compact dump, `\n` appended by writeData, same writeData | CONFIRMED BY CODE |
| - | Device accepts this reply as an answer to its query | UNKNOWN (never sent; firmware not analysed) |
| - | Device requires/ignores key order | UNKNOWN (inferred irrelevant) |
| - | Device behaviour if `action` is absent | UNKNOWN (the vendor SDK omits it, so a correct firmware must accept its absence) |

### 7. Candidate frames the evidence supports
A (SDK exact, success, value 30 for example):
```
{"query":"volume","result":"ok","value":30}\n
```
This is exactly what the vendor SDK emits (compact; alphabetical keys; integer; trailing LF). It is the only candidate the code supports.

Failure reply (SDK, when GetSystemVolume fails or is out of 0..100), for reference only, not for us to send:
```
{"message":"volume read failed","query":"volume","result":"fail"}\n
```
(0x33f90-0x34270: 3 pairs, `mov x9,#3` at 0x34110, `w4=2` at 0x3412c; `"volume read failed"` is `char[19]` = `RA19_Kc`.)

Set reply (SDK, after a device `volume` WITH `value`): single pair, `{"result":"ok"}\n` or `{"result":"fail"}\n` (0x3355c-0x33630, list count `mov x9,#1` at 0x33604).

Non-SDK variants that the code does NOT support, listed only so nobody tries them first: `{"action":"volume","result":"ok","query":"volume","value":30}`; `{"result":"ok","query":{"volume":30}}`; float or string `value`. Nothing in the binary distinguishes between A and an `action`-prefixed variant on the device side; only a hardware test (screen) could, and the code says A is what a genuine host sends.

## Recommended first frame
```
{"query":"volume","result":"ok","value":N}\n
```
with N an integer 0..100 equal to the actual host volume percent, sent once, only in direct response to a device `{"action":"volume"}` query marker, compact (no spaces), exactly one `\n`. Do not add `"action"`. Use the daemon's existing newline-terminated writer. Suggested handling per the existing guardrails: one frame, dry-run first (print, do not send), operator watching the slider, then a single real send with operator's explicit approval. Unverified on hardware: whether the slider moves to N; the screen is the only evidence (continued polling is not).

Open points for operator: (a) whether to send the reply in the SDK's compact form (recommended) or imitate the SDK's indent-4 pretty form used by get_time; code evidence says volume is compact, so no reason to pretty-print; (b) caps type (0/1 vs false/true) in our get_time sender could be aligned to the SDK, but the current form is hardware-verified, so no change is recommended without a test.
