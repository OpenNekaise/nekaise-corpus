#!/usr/bin/env python3
"""host_policy.py — committed per-host fetch policy (registry/host_policy.json).

A host whose status is ``suspended`` is never requested by the loader. Its registry rows are
skipped without writing a manifest failure row, so they neither fail nor age toward pruning, and
prune_corpus leaves every existing row on the host alone (documents already held stay as they
are). This is a FETCH suspension decided for access-policy reasons (for example, a WAF that refuses
an honest identity), not a training exclusion: registry/eligibility.json handles exclusions.

Malformed policy fails closed for the loader and the pruner, and check_contracts reports it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

import store

ROOT = Path(__file__).resolve().parents[1]
PATH = store.config_path("host_policy.json", ROOT)
STATUSES = frozenset({"suspended"})


def validate(data: object) -> list[str]:
    if not isinstance(data, dict):
        return ["top level must be an object"]
    errors = []
    if data.get("version") != 1:
        errors.append("version must be 1")
    hosts = data.get("hosts")
    if not isinstance(hosts, dict):
        return errors + ["hosts must be an object"]
    for host, rule in hosts.items():
        label = f"hosts.{host}"
        if not host or host != host.lower() or "/" in host:
            errors.append(f"{label}: host must be a lowercase hostname")
        if not isinstance(rule, dict):
            errors.append(f"{label} must be an object")
            continue
        if rule.get("status") not in STATUSES:
            errors.append(f"{label}.status must be one of {sorted(STATUSES)}")
        if not isinstance(rule.get("reason"), str) or not rule["reason"].strip():
            errors.append(f"{label}.reason must be a non-empty string")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(rule.get("decided_at", ""))):
            errors.append(f"{label}.decided_at must be YYYY-MM-DD")
        backends = rule.get("backends", [])
        if not isinstance(backends, list) or any(not isinstance(b, str) for b in backends):
            errors.append(f"{label}.backends must be a list of backend names")
    return errors


def load(path: Path | None = None) -> dict[str, dict]:
    data = json.loads(Path(path or PATH).read_text())
    if errors := validate(data):
        raise ValueError(f"invalid host policy: {'; '.join(errors)}")
    return data["hosts"]


def suspended(url: str | None, policy: dict[str, dict]) -> dict | None:
    """The suspension rule covering this URL's host (exact host or a subdomain of it)."""
    host = (urlparse(url or "").hostname or "").lower()
    for name, rule in policy.items():
        if rule.get("status") == "suspended" and (host == name or host.endswith(f".{name}")):
            return rule
    return None
