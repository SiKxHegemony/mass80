#!/usr/bin/env python3
"""Create the mass80 Kanban graph. Creates cards only; starts no workers."""

import json
import subprocess

BOARD = "mass80"
WS = "dir:/home/shane/Repos/mass80"
PLAN = "/home/shane/Repos/mass80/docs/plans/2026-10-08-mass80-omarchy-integration.md"

GUARD = f"""
## Guardrails (mandatory; these override anything else)
- Read the plan first: {PLAN}
- Write ONLY inside /home/shane/Repos/mass80. Read-only elsewhere.
- Run NO git commands of any kind.
- Do NOT install packages, edit config/.env, delete files, or touch cron/systemd/udev.
- Do NOT write to /dev/ttyACM0 or any device node. Reading is only allowed where the card says so.
- Never assume: if a fact is missing, call kanban_block with a precise question instead of guessing.
- Never print or store secrets. Never fabricate output; quote real command output as evidence.
- Do not modify ~/mass80_volume.py.
- Report: files touched, exact commands run with real output, and what was NOT verified.
"""

CARDS = [
    dict(
        key="T1",
        title="T1 Scaffold repo, capture tool, pyserial venv",
        assignee=None,
        body="Owner: Shane's interactive session (needs pip approval prompt). Create .gitignore, README, tools/capture.py (read-only serial logger), .venv with pyserial. No git init.",
    ),
    dict(
        key="T2",
        title="T2 HUMAN GATE: labeled capture session on /dev/ttyACM0",
        assignee=None,
        parents=["T1"],
        body="Owner: Shane. Stop ~/mass80_volume.py, run tools/capture.py, perform and label: slider up/down, taps, swipes, multi-finger, long press, each on-screen page, replug. Completing this card means captures/*.log exist.",
    ),
    dict(
        key="T3",
        title="T3 Analyze captures and document protocol",
        assignee="engineer",
        parents=["T2"],
        body="Read captures/*.log. Write docs/protocol.md: event schema, field meanings, value ranges, unknowns, and an evidence-backed statement on whether the host can send display data (do NOT test by writing to the device). Output feeds the Phase 0 decision gate."
        + GUARD,
    ),
    dict(
        key="T4",
        title="T4 HUMAN GATE: Phase 0 decision (A only / A+C / A+B)",
        assignee=None,
        parents=["T3"],
        body="Owner: Shane. Review docs/protocol.md and choose the display direction.",
    ),
    dict(
        key="T5",
        title="T5 Phase 1: event parser + transport with tests",
        assignee="engineer",
        parents=["T4"],
        body="Tests first (stdlib unittest), fixtures from captures. Implement src/mass80d parser (line-buffered, fragment tolerant) and serial transport (by-id path, reconnect with backoff) using only pyserial. No new dependencies."
        + GUARD,
    ),
    dict(
        key="T6",
        title="T6 Phase 1: dispatcher + action layer",
        assignee="engineer",
        parents=["T5"],
        body="Config-driven mapping (config.example.toml in repo only). Actions via subprocess: wpctl absolute volume (clamped, rate limited), playerctl, hyprctl dispatch. Unit tests with mocked subprocess. Do not place any file outside the repo."
        + GUARD,
    ),
    dict(
        key="T7",
        title="T7 Review: compile, lint, typecheck findings",
        assignee="engineer",
        parents=["T6"],
        model="anthropic/claude-haiku-5.5",
        provider="nous",
        body="Read-only review. Run byte-compile and unit tests; run ruff/mypy ONLY if already available (do not install). Write findings to docs/reports/phase1-review.md. Do not fix anything."
        + GUARD,
    ),
]

ids = {}
for c in CARDS:
    cmd = [
        "hermes",
        "kanban",
        "--board",
        BOARD,
        "create",
        c["title"],
        "--body",
        c["body"],
        "--workspace",
        WS,
        "--json",
    ]
    if c.get("assignee"):
        cmd += ["--assignee", c["assignee"]]
    for p in c.get("parents", []):
        cmd += ["--parent", ids[p]]
    if c.get("model"):
        cmd += ["--model", c["model"], "--provider", c["provider"]]
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(c["key"], r.returncode, r.stdout.strip()[:300], r.stderr.strip()[:300])
    if r.returncode != 0:
        raise SystemExit(f"failed on {c['key']}")
    try:
        d = json.loads(r.stdout)
        ids[c["key"]] = d.get("id") or d.get("task", {}).get("id")
    except Exception as e:
        raise SystemExit(f"could not parse id for {c['key']}: {e}") from e
print(json.dumps(ids))
