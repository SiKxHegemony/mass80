#!/usr/bin/env python3
"""Read-only static analysis of libKeyboardSDK.dylib (arm64 Mach-O). Executes nothing.

Parses llvm-objdump output (format: '    4020:\\tsub\\tsp, sp, #0x40', labels 'name:',
'adrp x8, 168 ; 0xac000'), finds ADRP+ADD pairs that materialise string addresses,
and prints the owning function and the surrounding instructions.

Usage: sdk_xref.py [--ctx N] STRING [STRING ...]
"""

import re
import sys
from pathlib import Path

ROOT = Path("/home/shane/Repos/mass80/vendor/x")
SDK = ROOT / "dmg/MWKEYS Portal/MWKEYS Portal.app/Contents/MacOS/libKeyboardSDK.dylib"
ASM = ROOT / "sdk.asm"
CSTR_VMA, CSTR_SIZE = 0x99E14, 0x418E

args = sys.argv[1:]
ctx = 0
if args and args[0] == "--ctx":
    ctx = int(args[1])
    args = args[2:]
targets = args or ["get_time", "get_performance", "get_music_info"]

data = SDK.read_bytes()  # __TEXT file offset == VMA for this image (strings verified below)
addr = {}
for t in targets:
    needle = b"\x00" + t.encode() + b"\x00"
    hits, start = [], 0
    while True:
        i = data.find(needle, start, CSTR_VMA + CSTR_SIZE)
        if i < 0:
            break
        if i + 1 >= CSTR_VMA:
            hits.append(i + 1)
        start = i + 1
    addr[t] = hits
    print(f"string {t!r}: {[hex(h) for h in hits]}")

func = None
ins = []
for ln in ASM.read_text().splitlines():
    m = re.match(r"^(\S+):$", ln)
    if m and not ln.startswith(" "):
        func = m.group(1)
        continue
    m = re.match(r"^\s+([0-9a-f]+):\t(.*)$", ln)
    if m:
        ins.append((int(m.group(1), 16), m.group(2).replace("\t", " "), func))

pages = {}
refs = {t: [] for t in targets}
for idx, (a, txt, fn) in enumerate(ins):
    m = re.match(r"adrp (x\d+), \d+ ; 0x([0-9a-f]+)", txt)
    if m:
        pages[m.group(1)] = int(m.group(2), 16)
        continue
    m = re.match(r"add (x\d+), (x\d+), #0x([0-9a-f]+)", txt)
    if m and m.group(2) in pages:
        full = pages[m.group(2)] + int(m.group(3), 16)
        for t, hs in addr.items():
            if full in hs:
                refs[t].append((a, fn, idx))

for t, rs in refs.items():
    print(f"\n== refs to {t!r}: {len(rs)}")
    for a, fn, idx in rs[:8]:
        print(f"  at {a:#x} in {fn}")
        if ctx:
            for j in range(max(0, idx - ctx), min(len(ins), idx + ctx + 1)):
                mark = ">>" if j == idx else "  "
                print(f"   {mark} {ins[j][0]:x}: {ins[j][1]}")
