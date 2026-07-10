#!/usr/bin/env python3
"""find_purdue.py — harvest Purdue e-Pubs Herrick conference proceedings via OAI-PMH.

docs.lib.purdue.edu (Digital Commons) exposes the Ray W. Herrick Laboratories conference
series over OAI-PMH (`/do/oai/`): the International Compressor Engineering Conference
(`icec`, since 1972), the International Refrigeration and Air Conditioning Conference
(`iracc`, since 1986), and the International High Performance Buildings Conference
(`ihpbc`, since 2010) — several thousand free-to-download conference papers and the
deepest open equipment/HVAC vein we know of (compressors, refrigerants, heat pumps,
chillers, system modeling and field performance).

Pages ListRecords with metadataPrefix=oai_dc via the OAI resumptionToken (pass `--token`
to continue a prior run; the script prints the NEXT token at the end so a caller can
persist it — tokens are per-`--pub`, so keep one pointer per series). A record is kept
only when one of its `dc:identifier` values is the direct-PDF link
(`/context/<pub>/article/<n>/viewcontent/*.pdf`); landing-page-only records are skipped.
The proceedings are open access (free to read/download from Purdue e-Pubs, no CC grant)
-> license=open.

PAUSED (2026-07-10, see registry/rotation.json): the PDF paths answer plain HTTP with an
empty 202 + `x-amzn-waf-action: challenge` (AWS WAF JavaScript challenge), which
build_corpus.py's requests-based fetcher cannot pass. Discovery (this script) works fine;
do NOT `--append` until the loader grows a browser-assisted / waf-token fetch path.

    python scripts/find_purdue.py --pub iracc --max 15                        # propose, page 1
    python scripts/find_purdue.py --pub icec --token '2272249/oai_dc/100//' --max 300 --append
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import xml.etree.ElementTree as ET

import requests
import yaml

import registry

OAI = "https://docs.lib.purdue.edu/do/oai/"
UA = {"User-Agent": "nekaise-corpus/find_purdue"}
PDF_RE = re.compile(r"^https?://docs\.lib\.purdue\.edu/context/[^/]+/article/\d+/viewcontent/.+\.pdf$", re.I)

NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# conference series -> default topic; ihpbc titles that are clearly controls/FDD get re-tagged.
PUBS = {
    "icec": "equipment_systems",   # compressor engineering
    "iracc": "equipment_systems",  # refrigeration + air conditioning
    "ihpbc": "building_energy",    # high performance buildings
}
TOPIC_RULES = [
    (re.compile(r"fault|fdd|diagnos|commission", re.I), "commissioning_fdd"),
    (re.compile(r"control|mpc|bas\b|automation", re.I), "controls_bas"),
]


def topic_for(pub: str, title: str) -> str:
    for pat, topic in TOPIC_RULES:
        if pat.search(title or ""):
            return topic
    return PUBS[pub]


def pdf_url(identifiers: list[str]) -> str | None:
    for ident in identifiers:
        if PDF_RE.match(ident):
            return ident
    return None


def fetch_page(pub: str, token: str) -> str:
    params = ({"verb": "ListRecords", "resumptionToken": token} if token else
              {"verb": "ListRecords", "metadataPrefix": "oai_dc", "set": f"publication:{pub}"})
    r = requests.get(OAI, params=params, headers=UA, timeout=45)
    r.raise_for_status()
    return r.text


def parse_page(xml_text: str):
    """-> (records, next_token_or_''). records are raw dicts (title, identifiers)."""
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
        dc = metadata[0] if metadata is not None and len(metadata) else None
        if dc is None:
            continue
        title = (dc.findtext("dc:title", default="", namespaces=NS) or "").strip()
        identifiers = [(e.text or "").strip() for e in dc.findall("dc:identifier", NS)]
        if title:
            records.append({"title": title, "identifiers": identifiers})
    tok_el = root.find(".//oai:resumptionToken", NS)
    next_token = (tok_el.text or "").strip() if tok_el is not None else ""
    return records, next_token


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pub", choices=sorted(PUBS), default="iracc",
                    help="conference series to harvest (default: iracc)")
    ap.add_argument("--token", default="", help="resumptionToken to continue from (default: start fresh)")
    ap.add_argument("--max", type=int, default=200, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true", help="append into the registry (registry/purdue.yaml)")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    out = []
    scanned = 0
    token = args.token
    first = True

    while len(out) < args.max and (first or token):
        first = False
        try:
            xml_text = fetch_page(args.pub, token)
            records, token = parse_page(xml_text)
        except Exception as e:
            print(f"# OAI page failed (token={token!r}): {e}", file=sys.stderr)
            break

        for rec in records:
            if len(out) >= args.max:
                break
            scanned += 1
            title = rec["title"]
            url = pdf_url(rec["identifiers"])
            if url is None:
                continue
            u, t = url.rstrip("/"), registry.norm(title)
            if u in urls or t in titles:
                continue
            urls.add(u)
            titles.add(t)
            out.append({"id": f"pur-{args.pub}-{registry.slug(title)[:46]}", "title": title[:150],
                        "url": url, "source": f"purdue-{args.pub}", "license": "open",
                        "topic": topic_for(args.pub, title), "format": "pdf"})

        time.sleep(1)  # politeness between OAI pages

    registry.uniquify_ids(out, reg_ids)

    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    kept_ratio = f"{len(out)}/{scanned}" if scanned else "0/0"
    print(f"# {len(out)} NEW Purdue e-Pubs records (pub={args.pub}; kept {kept_ratio} scanned; "
          f"direct-PDF only, deduped vs manifest + registry + blocklist)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)

    print(f"# next-token: {token or 'END'}")


if __name__ == "__main__":
    main()
