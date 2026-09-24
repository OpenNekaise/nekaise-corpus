#!/usr/bin/env python3
"""licenses.py — the one strict, fail-closed parser for Creative Commons / public-domain rights
evidence, shared by every finder that reads per-record rights (find_ojs, find_escholarship).

Accepts only canonical creativecommons.org licence / public-domain tool URLs (never a CC-looking
path on another host, never a URL embedded in prose), rejects any NC/ND/all-rights-reserved or
negation evidence in any value, and rejects unverifiable licence prose.
"""
from __future__ import annotations

import re

# Canonical Creative Commons licence / public-domain tool URLs. A rights value is DECISIVE only if
# it is exactly one of these URLs (whitespace trimmed) — a CC-looking path on another host, or a
# URL embedded in prose ("not licensed under https://creativecommons.org/...") never is.
_CC_URL = re.compile(
    r"https?://(?:www\.)?creativecommons\.org/"
    r"(?:licenses/(?P<lic>by|by-sa|by-nc|by-nd|by-nc-sa|by-nc-nd)/(?P<ver>\d\.\d)"
    r"(?:/[a-z]{2}(?:-[a-z]{2})?)?"
    r"|publicdomain/(?P<pd>zero|mark)/1\.0)"
    r"(?:/(?:legalcode(?:\.[a-z]{2})?|deed\.[a-z]{2}(?:-[a-z]{2})?)?)?/?", re.I)
_OPEN_TAGS = {"by": "cc-by", "by-sa": "cc-by-sa", "zero": "cc0", "mark": "public-domain"}
# Any of these anywhere in ANY rights value is conflicting evidence and rejects the record.
_RESTRICTIVE = re.compile(
    r"licenses/by-(?:nc|nd)|\bby-n[cd]\b|non-?commercial|no-?deriv|\bnc\b|\bnd\b"
    r"|all rights reserved|not (?:be )?(?:licen[cs]ed|re-?used|redistribut)", re.I)
# A free-text rights value that talks about licensing without being a canonical URL is
# unverifiable (e.g. "CC BY-like terms", a negated statement): fail closed.
_LICENSE_TALK = re.compile(r"creative\s*commons|creativecommons|licen[cs]|\bcc[ -]?(?:by|0)\b",
                           re.I)

def cc_license(rights: list[str]) -> tuple[str, str] | tuple[None, None]:
    """(licence tag, canonical licence URL) or (None, None) — FAIL-CLOSED, order-independent.

    Accept only when (a) at least one rights value is exactly a canonical CC BY / BY-SA / CC0 /
    PDM URL, (b) no rights value carries NC / ND / all-rights-reserved / negation evidence, and
    (c) every other value is a plain copyright line, not unverifiable licence prose. Two
    different open grants resolve to the more restrictive tag (BY-SA over BY)."""
    decided: dict[str, str] = {}
    for value in rights:
        value = value.strip()
        if _RESTRICTIVE.search(value):
            return None, None
        m = _CC_URL.fullmatch(value)
        if m:
            key = m.group("lic") or m.group("pd")
            tag = _OPEN_TAGS.get(key.lower())
            if tag is None:
                return None, None
            decided.setdefault(tag, value)
        elif _LICENSE_TALK.search(value):
            return None, None
    for tag in ("cc-by-sa", "cc-by", "cc0", "public-domain"):
        if tag in decided:
            return tag, decided[tag]
    return None, None
