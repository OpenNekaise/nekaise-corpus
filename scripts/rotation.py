#!/usr/bin/env python3
"""rotation.py — the growth loop's SHARED excavation state (registry/rotation.json, committed).

Every discovery backend mines a paginated universe (OSTI pages, Google-Patents weekly buckets,
OAPEN offsets, …). Which page comes next used to live in one agent's session memory — invisible to
the nightly cron, to marathon.sh, and to any other agent or machine continuing the work. This file
fixes that: dig/marathon read the pointer, run the finder, then advance it, so ANY operator resumes
exactly where the last one stopped.

    python scripts/rotation.py next find_osti      # print the finder's next CLI arg, e.g. "--page 33"
    python scripts/rotation.py advance find_osti   # bump the pointer (call AFTER a successful run)
    python scripts/rotation.py show                # dump the whole state

Pointer kinds: integers advance by `step`; Google-Patents buckets ("YYYY-WNN") advance to the
PREVIOUS ISO week, walking history backwards. Edit registry/rotation.json by hand to re-aim a vein.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
PATH = ROOT / "registry" / "rotation.json"


def load() -> dict:
    return json.loads(PATH.read_text()) if PATH.exists() else {}


def save(state: dict) -> None:
    PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def next_arg(name: str) -> str:
    e = load()[name]
    return f"{e['flag']} {e['next']}"


def _prev_week(bucket: str) -> str:
    m = re.fullmatch(r"(\d{4})-W(\d{2})", bucket)
    if not m:
        raise ValueError(f"not a weekly bucket: {bucket}")
    year, week = int(m.group(1)), int(m.group(2))
    if week > 1:
        return f"{year}-W{week - 1:02d}"
    return f"{year - 1}-W52"


def advance(name: str) -> str:
    state = load()
    e = state[name]
    if isinstance(e["next"], int):
        e["next"] += e.get("step", 1)
    else:
        e["next"] = _prev_week(e["next"])
    save(state)
    return f"{e['flag']} {e['next']}"


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "show":
        print(json.dumps(load(), indent=2, ensure_ascii=False))
        return
    if len(sys.argv) != 3 or sys.argv[1] not in ("next", "advance"):
        print(__doc__)
        sys.exit(2)
    cmd, name = sys.argv[1], sys.argv[2]
    if name not in load():
        print(f"unknown finder '{name}' — see registry/rotation.json", file=sys.stderr)
        sys.exit(1)
    print(next_arg(name) if cmd == "next" else advance(name))


if __name__ == "__main__":
    main()
