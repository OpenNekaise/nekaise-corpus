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

Writes go through the store (ADR 0001 stage 3, step 5): a round records its pointer moves inside
its discovery transaction (run_round, using `advanced` / `with_next`), and the standalone
`advance` / `set_next` below run one store transaction under their own writer, which waits at most
LOCK_TIMEOUT seconds for a running round instead of interleaving with it. Reads (`load`, `next`,
`show`) stay plain file reads so they never wait for a round.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import store

ROOT = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
PATH = ROOT / "registry" / "rotation.json"
LOCK_TIMEOUT = 30.0
MAX_POINTER = 4096


def load() -> dict:
    return json.loads(PATH.read_text()) if PATH.exists() else {}


def pointer_arg(entry: dict) -> str:
    return f"{entry['flag']} {entry['next']}"


def next_arg(name: str) -> str:
    return pointer_arg(load()[name])


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


def advanced(name: str, entry: dict) -> dict:
    """The entry after one successful run: integers step, weekly buckets walk back past skips."""
    e = dict(entry)
    if e.get("dynamic"):
        raise ValueError(f"{name}: dynamic pointer must be replaced with set_next()")
    if isinstance(e["next"], int):
        if "skip" in e:
            raise ValueError(f"{name}: skip ranges require a weekly rotation pointer")
        e["next"] += e.get("step", 1)
    else:
        e["next"] = _prev_unskipped_week(e["next"], e.get("skip", []))
    return e


def check_pointer(name: str, value: str) -> str:
    """A reported dynamic cursor, validated and stripped."""
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise ValueError(f"{name}: dynamic pointer must be one non-empty line")
    value = value.strip()
    if len(value) > MAX_POINTER:
        raise ValueError(f"{name}: dynamic pointer exceeds {MAX_POINTER} characters")
    return value


def with_next(name: str, entry: dict, value: str) -> dict:
    """The dynamic entry with its opaque cursor replaced by a successful finder's report."""
    value = check_pointer(name, value)
    if not entry.get("dynamic"):
        raise ValueError(f"{name}: set_next() requires dynamic rotation")
    return {**entry, "next": value}


def _update(name: str, change) -> str:
    """Apply change(name, entry) -> entry in one standalone store transaction."""
    st = store.open(root=PATH.parent.parent)
    with store.standalone_transaction(st, "rotation", timeout=LOCK_TIMEOUT) as tx:
        entry = change(name, tx.rotation_get(name))
        tx.rotation_set(name, entry)
    return pointer_arg(entry)


def advance(name: str) -> str:
    return _update(name, advanced)


def set_next(name: str, value: str) -> str:
    """Replace an opaque dynamic cursor after its finder completed successfully."""
    value = check_pointer(name, value)
    return _update(name, lambda n, e: with_next(n, e, value))


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
