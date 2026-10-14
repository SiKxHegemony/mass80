#!/usr/bin/env python3
"""Read-only summary of captures/*.log: records, labels, errors, action counts."""

import glob
import json
import re
from collections import Counter

for path in sorted(glob.glob("/home/shane/Repos/mass80/captures/*.log")):
    with open(path, encoding="utf-8") as fh:
        recs = [json.loads(line) for line in fh if line.strip()]
    kinds = Counter(r["kind"] for r in recs)
    dur = recs[-1]["t"] - recs[0]["t"] if recs else 0
    print(f"== {path.split('/')[-1]}  records={len(recs)} kinds={dict(kinds)} dur={dur:.1f}s")
    for r in recs:
        if r["kind"] in ("label", "error", "stop", "start"):
            detail = r.get("text", r.get("chunks", ""))
            print(f"  +{r['t'] - recs[0]['t']:7.1f}s {r['kind']}: {detail}")
    blob = "".join(r.get("text", "") for r in recs if r["kind"] == "rx")
    acts = Counter(re.findall(r'\{"action":"([a-z_]+)"', blob))
    print("  JSON actions:", dict(acts))
    print("  distinct non-debug JSON:", sorted(set(re.findall(r'\{"[^{}]*\}', blob)))[:25])
    tags = Counter(re.findall(r"\[(CDC-TX|C0|C1|[A-Z_]{3,})\]", blob))
    print("  bracket tags:", dict(tags.most_common(10)))
