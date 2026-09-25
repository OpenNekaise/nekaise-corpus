#!/usr/bin/env python3
"""compliance_common.py — shared rules of the compliance/ESG programme finders
(find_boverket --mode bfs, find_regdocs, find_eurlex, find_esef).

* Licence registration (Codex decision 2026-09-25): every row carries license_url,
  license_evidence and rights_verified_at. Rows whose honest tag is outside the CURRENT registry
  vocabulary (proprietary, unverified, cc-by-nd, publisher-oa, …: the collect-all licence classes
  that are not yet on main) are NEVER appended — they are reported as held until the collect-all
  split lands, and the source definitions that produce them stay disabled.
* The host policy the discovery client enforces is the one pinned in a store view.
"""
from __future__ import annotations

import re
import sys

import lint_registry
import polite_http
import store

CURRENT_LICENSES = frozenset(lint_registry.LICENSES)
RIGHTS_FIELDS = ("license_url", "license_evidence", "rights_verified_at")
# Tags the collect-all branch introduces (state_codec.LICENSE_CLASSES there): accepted in
# configuration now, appended only after the split lands.
FUTURE_LICENSES = frozenset({"cc-by-nc", "cc-by-nc-sa", "cc-by-nd", "cc-by-nc-nd",
                             "arxiv-nonexclusive", "publisher-oa", "unverified", "proprietary"})
KNOWN_LICENSES = CURRENT_LICENSES | FUTURE_LICENSES
# The programme's id families (registry routing: state_codec.SHARDS). The loader applies the
# programme's robots/byte/pacing rules to exactly these rows.
ID_PREFIXES = ("bov-bfs-", "reg-", "eur-", "esf-")


def is_programme_id(sid: str) -> bool:
    return str(sid or "").startswith(ID_PREFIXES)


def rights_errors(entry: dict) -> list[str]:
    errors = [f"{entry.get('id')}: missing {f}" for f in RIGHTS_FIELDS if not entry.get(f)]
    if entry.get("license") not in KNOWN_LICENSES:
        errors.append(f"{entry.get('id')}: unknown licence tag {entry.get('license')!r}")
    if entry.get("license") == "proprietary-internal":
        errors.append(f"{entry.get('id')}: pointer licence on a payload candidate")
    return errors


def split_appendable(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """(appendable now, held until the collect-all split). Raises on missing rights evidence."""
    problems = [p for e in entries for p in rights_errors(e)]
    if problems:
        raise ValueError("rights evidence incomplete: " + "; ".join(problems[:5]))
    ok = [e for e in entries if e["license"] in CURRENT_LICENSES]
    held = [e for e in entries if e["license"] not in CURRENT_LICENSES]
    if held:
        tags = sorted({e["license"] for e in held})
        print(f"# {len(held)} candidate(s) HELD, licence tag(s) {tags} await the collect-all "
              f"licence classes (never appended before the split)", file=sys.stderr)
    return ok, held


def pin_host_policy(root=None) -> dict:
    """Load the pinned host policy from a store view into the discovery client."""
    st = store.open(root=root) if root is not None else store.open()
    with st.read(timeout=60) as view:
        _, policy = store.pinned_policy(view)
    polite_http.set_policy(policy)
    return policy


# --- quality profile (quality.verdict_for) --------------------------------------------------------
_CELEX_ID = re.compile(r"^celex:([0-9CE][0-9A-Z()/.\-]{4,40})$")
_RINFO = re.compile(r"^https://rinfo\.boverket\.se/(?P<grund>[A-Z]{2,5}\d{4}-\d+)/"
                    r"(?:pdf/(?P<doc>[A-Z]{2,5}\d{4}-\d+)\.pdf|"
                    r"dok/(?P<kdoc>[A-Z]{2,5}\d{4}-\d+)_Konsolidering\.pdf)$")


def quality_profile(row: dict, regdocs: dict | None) -> str | None:
    """"normative" for a VERIFIED normative instrument of the programme, else None (generic gate).

    The authority is the pinned configuration plus the row's validated identity, never a source
    label alone: an EU act must carry `celex:<C>` whose id is exactly `eur-<slug(C)>-<lang>[-pN]`
    and a Publications Office Cellar URL; a Boverket BFS must be a rinfo.boverket.se BFS or
    consolidation PDF whose path matches its id; a regdocs row must belong to a pinned
    registry/regdocs.json source marked `quality_profile: normative` (same source tag) and come
    from one of that source's declared `hosts`."""
    import host_policy
    import state_codec

    sid, url, source = str(row.get("id", "")), str(row.get("url", "")), row.get("source")
    host = host_policy.canonical_host(url)
    if sid.startswith("eur-"):
        m = _CELEX_ID.match(str(row.get("persistent_id") or ""))
        if (m and source == "eurlex" and host == "publications.europa.eu"
                and re.fullmatch(rf"eur-{re.escape(state_codec.slug(m.group(1)))}-[a-z]{{2}}"
                                 r"(?:-p\d+)?", sid)):
            return "normative"
        return None
    if sid.startswith("bov-bfs-"):
        m = _RINFO.match(url)
        if not m or source != "boverket_bfs":
            return None
        grund = state_codec.slug(m.group("grund"))
        if m.group("doc"):
            doc = state_codec.slug(m.group("doc"))
            want = f"bov-bfs-{grund}" + ("" if doc == grund else f"-{doc}")
        else:
            want = f"bov-bfs-{grund}-{state_codec.slug(m.group('kdoc'))}-kons"
        return "normative" if sid == want else None
    if sid.startswith("reg-"):
        for key, cfg in ((regdocs or {}).get("sources") or {}).items():
            if (cfg.get("quality_profile") == "normative" and cfg.get("source") == source
                    and sid.startswith(f"reg-{key}-") and host in (cfg.get("hosts") or [])):
                return "normative"
    return None
