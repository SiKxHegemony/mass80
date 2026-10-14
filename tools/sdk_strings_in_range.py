#!/usr/bin/env python3
"""Read-only: list the string literals referenced (in order) within an address range of the SDK.

Usage: sdk_strings_in_range.py START END   (hex addresses)
Also prints calls (bl targets) so JSON-builder structure is visible, plus immediate
integers loaded with mov, to expose numeric constants (e.g. scaling factors).
"""

import re
import sys
from pathlib import Path

ROOT = Path("/home/shane/Repos/mass80/vendor/x")
SDK = ROOT / "dmg/MWKEYS Portal/MWKEYS Portal.app/Contents/MacOS/libKeyboardSDK.dylib"
ASM = ROOT / "sdk.asm"
lo, hi = int(sys.argv[1], 16), int(sys.argv[2], 16)
data = SDK.read_bytes()


def cstr(a: int) -> str:
    end = data.find(b"\x00", a)
    return data[a:end].decode("utf-8", "replace")


pages = {}
for ln in ASM.read_text().splitlines():
    m = re.match(r"^\s+([0-9a-f]+):\t(.*)$", ln)
    if not m:
        continue
    a = int(m.group(1), 16)
    if a < lo or a > hi:
        continue
    txt = m.group(2).replace("\t", " ")
    mm = re.match(r"adrp (x\d+), \d+ ; 0x([0-9a-f]+)", txt)
    if mm:
        pages[mm.group(1)] = int(mm.group(2), 16)
        continue
    mm = re.match(r"add (x\d+), (x\d+), #0x([0-9a-f]+)", txt)
    if mm and mm.group(2) in pages:
        full = pages[mm.group(2)] + int(mm.group(3), 16)
        if 0x99E14 <= full < 0x99E14 + 0x418E:
            print(f"{a:x}  STR  {cstr(full)!r}")
        continue
    mm = re.match(r"bl (\S+)", txt)
    if mm:
        t = mm.group(1)
        short = re.sub(r"__ZN8nlohmann16json_abi_v3_12_0", "json::", t)
        print(f"{a:x}  call {short[:110]}")
        continue
    mm = re.match(r"(mov|fmov|movk) (\w+), #(\S+)", txt)
    if mm and mm.group(1) == "mov":
        print(f"{a:x}  imm  {mm.group(2)} = {mm.group(3)}")
