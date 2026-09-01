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
PREVIOUS ISO week, walking history backwards. Weekly entries may declare inclusive `skip` ranges
for buckets already mined. Opaque API cursors declare `dynamic: true` and are replaced with the
successful finder's reported next value via `set_next`. Edit registry/rotation.json by hand to
re-aim a vein.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import ops

ROOT = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
PATH = ROOT / "registry" / "rotation.json"


def load() -> dict:
    return json.loads(PATH.read_text()) if PATH.exists() else {}


def save(state: dict) -> None:
    ops.atomic_write_text(PATH, json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def next_arg(name: str) -> str:
    e = load()[name]
    return f"{e['flag']} {e['next']}"


def _prev_week(bucket: str) -> str:
    m = re.fullmatch(r"(\d{4})-W(\d{2})", bucket)
    if not m:
        raise ValueError(f"not a weekly bucket: {bucket}")
    year, week = int(m.group(1)), int(m.group(2))
    try:
        current = date.fromisocalendar(year, week, 1)
    except ValueError as exc:
        raise ValueError(f"not a valid ISO weekly bucket: {bucket}") from exc
    previous = (current - timedelta(weeks=1)).isocalendar()
    return f"{previous.year}-W{previous.week:02d}"


def _week_date(bucket: str) -> date:
    """Return the Monday represented by an ISO-week bucket."""
    m = re.fullmatch(r"(\d{4})-W(\d{2})", bucket)
    if not m:
        raise ValueError(f"not a weekly bucket: {bucket}")
    try:
        return date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except ValueError as exc:
        raise ValueError(f"not a valid ISO weekly bucket: {bucket}") from exc


def _skip_ranges(value: object) -> list[tuple[date, date]]:
    """Validate inclusive [newest, oldest] ISO-week ranges."""
    if not isinstance(value, list):
        raise ValueError("skip must be a list of [newest, oldest] weekly buckets")
    ranges = []
    for index, item in enumerate(value):
        if (not isinstance(item, list) or len(item) != 2
                or not all(isinstance(bucket, str) for bucket in item)):
            raise ValueError(f"skip[{index}] must be [newest, oldest] weekly buckets")
        newest, oldest = map(_week_date, item)
        if newest < oldest:
            raise ValueError(f"skip[{index}] newest bucket must not precede oldest bucket")
        ranges.append((newest, oldest))
    return ranges


def _prev_unskipped_week(bucket: str, skip: object) -> str:
    ranges = _skip_ranges(skip)
    candidate = _prev_week(bucket)
    while any(oldest <= _week_date(candidate) <= newest for newest, oldest in ranges):
        candidate = _prev_week(candidate)
    return candidate


def validate_entry(name: str, entry: dict) -> list[str]:
    """Return control-plane errors for optional rotation features."""
    if "dynamic" in entry and not isinstance(entry["dynamic"], bool):
        return [f"{name}: dynamic must be true or false"]
    if entry.get("dynamic"):
        if not isinstance(entry.get("next"), str) or not entry["next"].strip():
            return [f"{name}: dynamic rotation requires a non-empty string pointer"]
        if "skip" in entry:
            return [f"{name}: dynamic rotation cannot use weekly skip ranges"]
        return []
    if "skip" not in entry:
        return []
    if isinstance(entry.get("next"), int):
        return [f"{name}: skip ranges require a weekly rotation pointer"]
    try:
        _skip_ranges(entry["skip"])
    except ValueError as exc:
        return [f"{name}: {exc}"]
    return []


def advance(name: str) -> str:
    # Standalone operators may advance a pointer while another process is doing the same.  Keep
    # the read-modify-write under its own lock; run_round's broader repo lock also prevents this.
    with ops.named_lock("rotation", timeout=30):
        state = load()
        e = state[name]
        if e.get("dynamic"):
            raise ValueError(f"{name}: dynamic pointer must be replaced with set_next()")
        if isinstance(e["next"], int):
            if "skip" in e:
                raise ValueError(f"{name}: skip ranges require a weekly rotation pointer")
            e["next"] += e.get("step", 1)
        else:
            e["next"] = _prev_unskipped_week(e["next"], e.get("skip", []))
        save(state)
        return f"{e['flag']} {e['next']}"


def set_next(name: str, value: str) -> str:
    """Replace an opaque dynamic cursor after its finder completed successfully."""
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise ValueError(f"{name}: dynamic pointer must be one non-empty line")
    value = value.strip()
    if len(value) > 4096:
        raise ValueError(f"{name}: dynamic pointer exceeds 4096 characters")
    with ops.named_lock("rotation", timeout=30):
        state = load()
        e = state[name]
        if not e.get("dynamic"):
            raise ValueError(f"{name}: set_next() requires dynamic rotation")
        e["next"] = value
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
