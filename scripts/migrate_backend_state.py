#!/usr/bin/env python3
"""migrate_backend_state.py — move runtime-origin backend pauses out of registry/backends.json
(ADR 0001 stage 3, step 5).

Until step 5, run_round recorded finder-reported exhaustion by editing the git-owned configuration
(disable_backend: enabled=false, reason "exhausted: <finder reason>"). Exhaustion is now runtime
state (registry/backend_state.json, written through the store), and a backend runs only when its
configuration AND its runtime state enable it. This explicit migration moves NAMED pauses:

    python scripts/migrate_backend_state.py find_kitopen            # dry run: print the plan
    python scripts/migrate_backend_state.py find_kitopen --apply    # migrate (then commit)

For each name the configuration must say enabled=false with a reason starting "exhausted: ". The
runtime state becomes {enabled: false, reason: <that reason, verbatim>} in one store transaction,
then the configuration entry becomes enabled=true without a reason. Runtime first, under the round
lock throughout: an interruption between the two leaves the backend paused in both places, never
enabled. A name already migrated is skipped, so re-running is harmless.

Only name backends whose pause the loop wrote. The prefix alone does not prove that: operators
wrote several "exhausted: ..." reasons by hand (ops commit 8c6c03bd3f, 2026-08-20), and those are
configuration decisions that stay in backends.json. See the ADR's stage-3 record for the list.
"""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import ExitStack
from pathlib import Path

import ops
import store
import store_broker

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "exhausted: "


def plan(config: dict, runtime: dict, names: list[str]) -> tuple[dict, list[str], list[str]]:
    """({name: reason} to migrate, [already migrated], [errors]) for the named backends.
    `runtime` maps names to store.BackendState."""
    moves, done, errors = {}, [], []
    for name in dict.fromkeys(names):
        cfg = config.get(name)
        state = runtime.get(name, store.BackendState())
        if name.startswith("_") or not isinstance(cfg, dict):
            errors.append(f"{name}: unknown backend")
        elif cfg.get("enabled", True) and not state.enabled and "reason" not in cfg:
            done.append(name)
        elif cfg.get("enabled", True) is not False:
            errors.append(f"{name}: configuration does not pause it (enabled is not false)")
        elif not isinstance(cfg.get("reason"), str) or not cfg["reason"].startswith(PREFIX):
            errors.append(f"{name}: configuration reason does not start with {PREFIX!r}; an "
                          "operator pause stays configuration")
        elif not state.enabled and state.reason != cfg["reason"]:
            errors.append(f"{name}: runtime already paused with a different reason "
                          f"({state.reason!r})")
        else:
            moves[name] = cfg["reason"]
    return moves, done, errors


def migrated_config(raw: dict, moves: dict) -> dict:
    """The configuration document with each moved backend enabled and its reason removed (key
    order otherwise untouched)."""
    out = {}
    for name, cfg in raw.items():
        if name in moves:
            cfg = {k: (True if k == "enabled" else v) for k, v in cfg.items() if k != "reason"}
        out[name] = cfg
    return out


def migrate(root: Path, names: list[str], *, apply: bool, timeout: float = 60,
            log=print) -> int:
    st = store.open(root=root)
    config_path = store.config_path("backends.json", root)
    with ExitStack() as stack:
        # Under the round lock throughout: a maintenance window's child writes through its
        # parent's broker (the parent holds the lock); a standalone run holds its own writer.
        writer = (None if store_broker.client() is not None
                  else stack.enter_context(st.writer(timeout=timeout)))

        def body(view, batch):
            raw = json.loads(config_path.read_text())
            moves, done, errors = plan(raw, view.backend_state_get(), names)
            if apply and not errors:
                for name, reason in moves.items():
                    batch.backend_state_set(name, store.BackendState(False, reason))
            return raw, moves, done, errors

        (raw, moves, done, errors), _ = store_broker.run_batch(
            st, "migrate-backend-state", body, writer=writer)
        for name in done:
            log(f"{name}: already migrated")
        if errors:
            for error in errors:
                log(f"ERROR {error}")
            return 1
        for name, reason in moves.items():
            log(f"{name}: runtime {{enabled: false, reason: {reason!r}}}; config enabled=true, "
                "reason removed")
        if not moves or not apply:
            if moves:
                log("dry run -- pass --apply to migrate")
            return 0
        # Configuration is owned by the repository: the store never writes it; same format as
        # the old writer. Runtime state was recorded first, so an interruption here leaves the
        # backend paused in both places.
        ops.atomic_write_text(config_path, json.dumps(
            migrated_config(raw, moves), indent=2, ensure_ascii=False) + "\n")
        with st.read(writer=writer) if writer is not None else st.read() as view:
            still = [n for n in moves if view.backend_enabled(n)]
        if still:  # cannot happen unless the store lost the runtime write
            log(f"ERROR effective enablement changed for {', '.join(still)}")
            return 1
        log(f"migrated {len(moves)} backend(s); effective enablement unchanged")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("names", nargs="+", help="backends whose pause the loop wrote")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--lock-timeout", type=float, default=60)
    args = ap.parse_args()
    return migrate(ROOT, args.names, apply=args.apply, timeout=args.lock_timeout)


if __name__ == "__main__":
    sys.exit(main())
