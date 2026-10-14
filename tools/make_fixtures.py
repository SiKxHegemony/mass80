#!/usr/bin/env python3
"""Copy the rx chunks of two captures into tests/fixtures/ (captures are read-only).

Each output line is {"hex": "<chunk bytes in hex>"}: only the raw serial bytes, in
original chunk order. Timestamps, labels and the tool's own records are dropped.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES = {
    "capture-20261009-001911.log": "capture_001911.chunks.jsonl",
    "capture-20261009-001149.log": "capture_001149.chunks.jsonl",
}

out_dir = ROOT / "tests" / "fixtures"
out_dir.mkdir(parents=True, exist_ok=True)
for src_name, dst_name in SOURCES.items():
    n = 0
    with (
        open(ROOT / "captures" / src_name, encoding="utf-8") as src,
        open(out_dir / dst_name, "w", encoding="utf-8") as dst,
    ):
        for line in src:
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("kind") != "rx":
                continue
            dst.write(json.dumps({"hex": rec["hex"]}) + "\n")
            n += 1
    print(f"{src_name} -> {dst_name}: {n} chunks")
