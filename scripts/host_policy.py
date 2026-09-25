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

import re
from urllib.parse import urlparse

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


def canonical_host(value: str | None) -> str:
    """THE hostname every policy decision uses (selection and transport alike): parsed from a
    URL or a bare host, without userinfo and port, lower-case, trailing dots stripped
    ("papers.ssrn.com." is papers.ssrn.com), IDNA-encoded (Unicode look-alikes fold to ASCII)."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        host = urlparse(text if "//" in text else "//" + text).hostname or ""
    except ValueError:
        return ""
    host = host.rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    return host.lower().rstrip(".")


def suspended(url: str | None, policy: dict[str, dict]) -> dict | None:
    """The suspension rule covering this URL's host (exact host or a subdomain of it)."""
    host = canonical_host(url)
    for name, rule in policy.items():
        if rule.get("status") == "suspended" and (host == name or host.endswith(f".{name}")):
            return rule
    return None


def suspended_redirect(row: dict | None, policy: dict[str, dict]) -> dict | None:
    """The suspension rule covering the redirect destination a row's last fetch was refused at
    (build_corpus records it as `suspended_redirect`), while that suspension stands."""
    value = (row or {}).get("suspended_redirect")
    url = value.get("url") if isinstance(value, dict) else None
    return suspended(url, policy) if isinstance(url, str) and url else None
