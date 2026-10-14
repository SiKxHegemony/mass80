#!/usr/bin/env python3
"""Read-only: reassemble the stream from a capture and print a timeline of non-polling events."""

import json
import re
import sys

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    recs = [json.loads(line) for line in fh if line.strip()]
t0 = recs[0]["t"]
POLL = ("get_time", "get_music_info")
last = {}
for r in recs:
    if r["kind"] != "rx":
        continue
    # strip debug echo lines, keep only raw host-bound JSON objects
    for m in re.finditer(r'(?<!\()\{"action":"([a-z_]+)"([^{}]*)\}', r["text"]):
        act, rest = m.group(1), m.group(2)
        if act in POLL:
            continue
        key = act
        print(f"+{r['t'] - t0:6.1f}s {act}{rest}")
print("--- distinct send_key payloads / other fields")
blob = "".join(r.get("text", "") for r in recs if r["kind"] == "rx")
print(sorted(set(re.findall(r'\{"action":"(?:send_key|music_[a-z]+)"[^{}]*\}', blob))))
print("--- debug tags sample")
print(sorted(set(re.findall(r"\[(?:SRAM|PSRAM)\][^\r\n]{0,120}", blob)))[:4])
print("--- last 3 rx records")
for r in [x for x in recs if x["kind"] == "rx"][-3:]:
    print(f"+{r['t'] - t0:6.1f}s", repr(r["text"][-300:]))
