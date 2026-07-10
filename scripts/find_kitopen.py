#!/usr/bin/env python3
"""find_kitopen.py — harvest KIT's open repository (KITopen) via OAI-PMH.

publikationen.bibliothek.kit.edu exposes KIT's institutional repository over OAI-PMH
(`/oai`). Set `ddc:690` (Bauwesen/construction) alone holds ~9,300 records — German- and
English-language theses, reports, and articles spanning structures, materials, urban
planning, and building energy/HVAC. The corpus takes all languages, so this backend keeps
both.

Pages ListRecords with metadataPrefix=oai_dc via the OAI resumptionToken (pass `--token` to
continue a prior run; the script prints the NEXT token at the end so a caller — a human or
rotation.py — can persist it). Each record's `dc:rights` values are scanned for a
redistributable Creative Commons grant: `cc-by[-sa]` or `cc0` are kept; `by-nc*`/`by-nd*`,
the custom "KITopen License", `info:eu-repo/semantics/openAccess` alone (an access-rights
flag, not a license), or absent rights are all skipped — FAIL-CLOSED, since KITopen mixes
open and all-rights-reserved deposits in the same set. A record is only kept if one of its
`dc:identifier` values is a publikationen.bibliothek.kit.edu URL with path depth >= 2
(`/{recordId}/{fulltextId}` — the direct-PDF link); the bare record-landing URL (depth 1)
and DOI-only identifiers are not fulltext and are skipped.

    python scripts/find_kitopen.py --max 15                          # propose, page 1
    python scripts/find_kitopen.py --token EenPRXLJ7MdbRHrr --max 300 --append
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import requests
import yaml

import registry

OAI = "https://publikationen.bibliothek.kit.edu/oai"
UA = {"User-Agent": "nekaise-corpus/find_kitopen"}
FULLTEXT_HOST = "publikationen.bibliothek.kit.edu"

NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# (title regex -> topic), first match wins; default structures_civil.
TOPIC_RULES = [
    (re.compile(r"energie|wärme|heiz", re.I), "building_energy"),
    (re.compile(r"beton|holz|baustoff|werkstoff", re.I), "materials"),
    (re.compile(r"stadt|urban|quartier", re.I), "urban"),
    (re.compile(r"brücke|tunnel|tragwerk|seismic|erdbeben", re.I), "structures_civil"),
    (re.compile(r"klima|lüftung|hvac", re.I), "equipment_systems"),
]

# rights substring -> license tag, checked in this order (most specific first).
LICENSE_RULES = [
    (re.compile(r"creativecommons\.org/publicdomain/zero", re.I), "cc0"),
    (re.compile(r"creativecommons\.org/licenses/by-sa/", re.I), "cc-by-sa"),
    (re.compile(r"creativecommons\.org/licenses/by/", re.I), "cc-by"),
]


def topic_for(title: str) -> str:
    for pat, topic in TOPIC_RULES:
        if pat.search(title or ""):
            return topic
    return "structures_civil"


def license_for(rights: list[str]) -> str | None:
    """Redistributable CC tag, or None (fail-closed) — by-nc*/by-nd*, the custom KITopen
    License, a bare openAccess flag, or no rights at all all return None."""
    blob = " ".join(rights)
    for pat, tag in LICENSE_RULES:
        if pat.search(blob):
            return tag
    return None


def fulltext_url(identifiers: list[str]) -> str | None:
    """The identifier that's a direct KITopen fulltext link: same host, path depth >= 2
    (`/{recordId}/{fulltextId}`). Bare record pages (depth 1) and DOIs are not fulltext."""
    for ident in identifiers:
        u = urlparse(ident)
        if u.netloc != FULLTEXT_HOST:
            continue
        parts = [p for p in u.path.split("/") if p]
        if len(parts) >= 2:
            return ident
    return None


def fetch_page(set_: str, token: str) -> str:
    params = ({"verb": "ListRecords", "resumptionToken": token} if token else
              {"verb": "ListRecords", "metadataPrefix": "oai_dc", "set": set_})
    r = requests.get(OAI, params=params, headers=UA, timeout=45)
    r.raise_for_status()
    return r.text


def parse_page(xml_text: str):
    """-> (records, next_token_or_None). records is a list of raw dicts (title, rights,
    identifiers); next_token is '' when the harvest is exhausted."""
    root = ET.fromstring(xml_text)
    err = root.find("oai:error", NS)
    if err is not None:
        raise RuntimeError(f"OAI error {err.get('code')}: {err.text}")
    records = []
    for rec in root.findall(".//oai:record", NS):
        header = rec.find("oai:header", NS)
        if header is not None and header.get("status") == "deleted":
            continue
        metadata = rec.find("oai:metadata", NS)
        dc = metadata[0] if metadata is not None and len(metadata) else None  # the oai_dc:dc container
        if dc is None:
            continue
        title = (dc.findtext("dc:title", default="", namespaces=NS) or "").strip()
        rights = [(e.text or "").strip() for e in dc.findall("dc:rights", NS)]
        identifiers = [(e.text or "").strip() for e in dc.findall("dc:identifier", NS)]
        if title:
            records.append({"title": title, "rights": rights, "identifiers": identifiers})
    tok_el = root.find(".//oai:resumptionToken", NS)
    next_token = (tok_el.text or "").strip() if tok_el is not None else ""
    return records, next_token


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default="ddc:690", help="OAI set (default: ddc:690, Bauwesen/construction)")
    ap.add_argument("--token", default="", help="resumptionToken to continue from (default: start fresh)")
    ap.add_argument("--max", type=int, default=300, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true", help="append into the registry (registry/kitopen.yaml)")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    out = []
    scanned = 0
    token = args.token
    first = True

    while len(out) < args.max and (first or token):
        first = False
        try:
            xml_text = fetch_page(args.set, token)
            records, token = parse_page(xml_text)
        except Exception as e:
            print(f"# OAI page failed (token={token!r}): {e}", file=sys.stderr)
            break

        for rec in records:
            if len(out) >= args.max:
                break
            scanned += 1
            title = rec["title"]
            tag = license_for(rec["rights"])
            if tag is None:
                continue
            url = fulltext_url(rec["identifiers"])
            if url is None:
                continue
            u, t = url.rstrip("/"), registry.norm(title)
            if u in urls or t in titles:
                continue
            urls.add(u)
            titles.add(t)
            out.append({"id": f"kit-{registry.slug(title)[:52]}", "title": title[:150],
                        "url": url, "source": "kitopen", "license": tag,
                        "topic": topic_for(title), "format": "pdf"})

        time.sleep(1)  # politeness between OAI pages

    registry.uniquify_ids(out, reg_ids)

    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    kept_ratio = f"{len(out)}/{scanned}" if scanned else "0/0"
    print(f"# {len(out)} NEW KITopen records (set={args.set}; kept {kept_ratio} scanned; "
          f"CC-BY/CC-BY-SA/CC0 fulltext only, deduped vs manifest + registry + blocklist)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)

    print(f"# next-token: {token or 'END'}")


if __name__ == "__main__":
    main()
