"""The discovery write path as it was before ADR 0001 stage 3, step 5 (run_round at c841503eb0),
kept verbatim in logic as the reference for the store path's equivalence tests: merge through
registry.existing_keys()/append_entries(), github passes through a store transaction, rotation
pointers through rotation.json read-modify-writes, exhaustion by editing backends.json.

`root` is a repository copy; registry/blocklist module paths must point into it (the caller
monkeypatches registry.REG_DIR / MAN_DIR / blocklist.PATH)."""
from __future__ import annotations

import json
from pathlib import Path

import ops
import registry
import rotation
import store


def _save_rotation(path: Path, state: dict) -> None:
    ops.atomic_write_text(path, json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def legacy_advance(path: Path, name: str) -> str:
    state = json.loads(path.read_text())
    e = state[name]
    if e.get("dynamic"):
        raise ValueError(f"{name}: dynamic pointer must be replaced with set_next()")
    if isinstance(e["next"], int):
        if "skip" in e:
            raise ValueError(f"{name}: skip ranges require a weekly rotation pointer")
        e["next"] += e.get("step", 1)
    else:
        e["next"] = rotation._prev_unskipped_week(e["next"], e.get("skip", []))
    _save_rotation(path, state)
    return f"{e['flag']} {e['next']}"


def legacy_set_next(path: Path, name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise ValueError(f"{name}: dynamic pointer must be one non-empty line")
    value = value.strip()
    if len(value) > 4096:
        raise ValueError(f"{name}: dynamic pointer exceeds 4096 characters")
    state = json.loads(path.read_text())
    e = state[name]
    if not e.get("dynamic"):
        raise ValueError(f"{name}: set_next() requires dynamic rotation")
    e["next"] = value
    _save_rotation(path, state)
    return f"{e['flag']} {e['next']}"


def legacy_disable_backend(path: Path, name: str, reason: str) -> None:
    raw = json.loads(path.read_text())
    if name not in raw:
        raise KeyError(f"unknown backend {name}")
    raw[name]["enabled"] = False
    raw[name]["reason"] = f"exhausted: {reason}"
    ops.atomic_write_text(path, json.dumps(raw, indent=2, ensure_ascii=False) + "\n")


def legacy_apply(root: Path, successful: list[dict], selected: list[str], backends: dict,
                 rotation_state: dict) -> tuple[int, dict, list[tuple]]:
    """Old merge_proposals + github_passes_applier + the rotation/exhaustion loop. Returns
    (total, accepted, [(event, fields)])."""
    root = Path(root)
    events: list[tuple] = []
    urls, titles, ids = registry.existing_keys()
    merged = []
    accepted: dict[str, int] = {}
    passes: dict = {}
    for result in sorted(successful, key=lambda r: r["index"]):
        doc = registry.read_proposal(result["proposal"])
        entries = doc["entries"]
        passes = registry.merge_github_passes(passes, doc.get("github_passes") or {})
        count = 0
        for entry in entries:
            missing = [field for field in registry.REQUIRED_FIELDS if not entry.get(field)]
            if missing:
                raise RuntimeError(
                    f"{result['name']} proposed {entry.get('id', '<no id>')} without "
                    f"{', '.join(missing)}"
                )
            url = entry["url"].rstrip("/")
            title = registry.norm(entry["title"])
            if url in urls or title in titles:
                continue
            registry.uniquify_ids([entry], ids)
            urls.add(url)
            titles.add(title)
            merged.append(entry)
            count += 1
        accepted[result["name"]] = count
    if merged:
        registry.append_entries(merged)
    if passes:
        st = store.FileStore(root)
        with st.read() as view:
            current = view.control_get("github_passes.json") or {}
        if registry.merge_github_passes(current, passes) != current:
            with st.writer() as w:
                with st.transaction("legacy.discover.github-passes",
                                    expected_version=st.version(), writer=w) as tx:
                    current = tx.control_get("github_passes.json") or {}
                    tx.control_set("github_passes.json",
                                   registry.merge_github_passes(current, passes))
    rotation_path = root / "registry" / "rotation.json"
    by_name = {r["name"]: r for r in successful}
    for name in selected:
        if name not in by_name:
            continue
        if backends[name].get("rotation", True):
            if by_name[name]["rotation_hold"]:
                detail = by_name[name]["rotation_hold_detail"]
                events.append(("rotation_held", {"backend": name, "reason": "finder_requested",
                                                 **({"detail": detail} if detail else {})}))
                continue
            if rotation_state[name].get("dynamic"):
                pointer = legacy_set_next(rotation_path, name,
                                          by_name[name]["rotation_next"].read_text().strip())
            else:
                pointer = legacy_advance(rotation_path, name)
            events.append(("rotation_advanced", {"backend": name, "next": pointer}))
        exhausted = by_name[name]["backend_exhausted"]
        if exhausted.exists():
            reason = exhausted.read_text().strip()
            legacy_disable_backend(root / "registry" / "backends.json", name, reason)
            events.append(("backend_disabled", {"backend": name, "reason": reason}))
    return len(merged), accepted, events
