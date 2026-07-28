#!/usr/bin/env python3
"""find_scielo.py — enumerate SciELO Brazil's AEC journals (ArticleMeta API).

SciELO is the open-access backbone of Latin-American science: ~1,700 journals across 16 national
collections, nearly all CC-licensed, nearly all with a direct PDF. It is the biggest body of
PORTUGUESE built-environment literature on the open web — Ambiente Construído (ANTAC's
built-environment journal), Revista IBRACON de Estruturas e Materiais (concrete structures),
Soils and Rocks (geotechnics), urbe (urban management) — and the quality gate's DOMAIN vocabulary
already covers pt, so this vein lands inside the existing curation, not beside it.

EACH NATIONAL HOST HAS ITS OWN robots.txt AND THEY DISAGREE — check before adding a collection.
Only `scl` (www.scielo.br, `Allow: /`) is mined here. Chile (www.scielo.cl) and Peru
(www.scielo.org.pe) both serve `User-agent: * Disallow: /`, Peru additionally naming anthropic-ai
and Claude-Web, so their 7 journals are NO-GO and the 150 entries a first pass appended were
reverted the same round. Colombia (10 journals, ~4,600 CC-BY articles — the biggest loss) and
Uruguay refuse/time out the connection from this network, and Venezuela's PDF host
(www.scielo.org.ve) does not resolve at all.

Two API calls per article, both on articlemeta.scielo.org:
  article/identifiers/  -> the journal's article PIDs, paginated by `offset` (the rotation pointer)
  article/              -> one article's metadata; `fulltexts.pdf` carries the PDF URL outright
When `fulltexts.pdf` is absent (most pre-2015 records) the PDF path is DERIVED from the SciELO
markup path in field v702 — legacy Windows paths ("C:\\SciELO\\Serial\\ric\\v22n1\\markup\\art06.html")
and modern relative ones ("ac/v26/1678-8621-AC-26-e149370.xml") both reduce to
<host>/pdf/<acron>/<issue>/<file>.pdf. The derivation itself was checked against four national hosts
before the robots.txt review narrowed the vein to Brazil (34/34 sampled URLs returned a real PDF).

Ids are `sci-<pid>` rather than a title slug: the PID is stable and collision-free, so a round can
skip an already-held article WITHOUT spending a metadata request on it.

Only journals licensed CC-BY / CC-BY-SA are listed below. SciELO's NC and ND journals are
deliberately left out — the registry's license vocabulary has no category for them and AGENTS.md
says prefer public-domain / cc-by / cc-by-sa.

    python scripts/find_scielo.py                             # propose (offset 0)
    python scripts/find_scielo.py --offset 200 --max 400 --append
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from urllib.parse import urlparse

import requests
import yaml

import registry

API = "https://articlemeta.scielo.org/api/v1"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")

# Canonical per-collection PDF host. Deliberately ONE entry: a host missing here makes derive_pdf
# return None, so no collection can be mined by accident before someone has read its robots.txt.
# Adding one back means checking that host's robots.txt first (see the module docstring).
HOSTS = {"scl": "www.scielo.br"}

# (collection, issn, topic) — chosen by WoS subject category from a full scan of all 16
# collections (workspace/scan_scielo_journals.py), then hand-checked. Trailing comment is the
# journal and its article count at the time of the scan.
JOURNALS = [
    ("scl", "1413-4152", "infrastructure"),    # Engenharia Sanitaria e Ambiental (1587)
    ("scl", "1679-7825", "structures_civil"),  # Latin American J. of Solids and Structures (1355)
    ("scl", "1678-8621", "construction"),      # Ambiente Construído (1293)
    ("scl", "1983-4195", "structures_civil"),  # Revista IBRACON de Estruturas e Materiais (1032)
    ("scl", "2175-3369", "urban"),             # urbe. Rev. Brasileira de Gestão Urbana (689)
    ("scl", "2448-167X", "materials"),         # REM - International Engineering Journal (599)
    ("scl", "2236-9996", "urban"),             # Cadernos Metrópole (583)
    ("scl", "2318-0331", "infrastructure"),    # RBRH (water resources) (554)
    ("scl", "2675-5475", "structures_civil"),  # Soils and Rocks (geotechnics) (379)
    ("scl", "2238-1031", "infrastructure"),    # Journal of Transport Literature (236)
    # Last on purpose: broad materials-science title, so the per-run --max cap is spent on the
    # AEC-dedicated journals above before it reaches the metallurgy/physics tail the pruner drops.
    ("scl", "1516-1439", "materials"),         # Materials Research (4368)
]


def _get(path: str, **params):
    r = requests.get(f"{API}/{path}", params=params, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    return r.json()


def _vals(a: dict, key: str) -> list:
    return a.get(key) or []


def pick_title(a: dict):
    """Article title, preferring the original language (v40) then English then whatever exists."""
    cands = [t for t in _vals(a, "v12") if t.get("_")]
    if not cands:
        return None
    orig = (_vals(a, "v40") or [{}])[0].get("_")
    for want in (orig, "en"):
        for t in cands:
            if want and t.get("l") == want:
                return t["_"]
    return cands[0]["_"]


def derive_pdf(a: dict, collection: str):
    """<host>/pdf/<acron>/<issue>/<file>.pdf from the v702 markup path (legacy or modern)."""
    raw = (_vals(a, "v702") or [{}])[0].get("_") or ""
    host = HOSTS.get(collection)
    if not raw or not host:
        return None
    p = raw.replace("\\", "/")
    m = re.search(r"(?i)/serial/(.+)$", p)
    if m:
        p = m.group(1)
    p = re.sub(r"(?i)/markup/", "/", p)
    p = re.sub(r"\.[A-Za-z0-9]+$", ".pdf", p)
    p = p.lstrip("/")
    if not p.endswith(".pdf") or p.count("/") < 2:
        return None
    # The acronym segment is served lowercase even when v702 spells it "MR"/"AC"; the FILENAME is
    # case-sensitive the other way (1678-8621-AC-26-e149370.pdf), so lowercase only that one part.
    acron, rest = p.split("/", 1)
    return f"https://{host}/pdf/{acron.lower()}/{rest}"


def pdf_url(rec: dict, collection: str):
    """Prefer the PDF link the API states outright; fall back to deriving it from v702.

    Whichever path produced it, the result must sit on a host in HOSTS. `fulltexts.pdf` is filled
    in by SciELO and happily points at a national host we have NOT robots-cleared (it is how the
    reverted Venezuela entries got a www.scielo.org.ve URL), so it does not get to bypass the
    one gate derive_pdf already enforces.
    """
    pdfs = ((rec.get("fulltexts") or {}).get("pdf")) or {}
    a = rec.get("article") or {}
    url = None
    if pdfs:
        orig = (_vals(a, "v40") or [{}])[0].get("_")
        for want in (orig, "en", "pt", "es"):
            if want and pdfs.get(want):
                url = pdfs[want]
                break
        url = (url or list(pdfs.values())[0]).replace("http://", "https://")
    else:
        url = derive_pdf(a, collection)
    if not url:
        return None
    return url if urlparse(url).netloc.lower() in set(HOSTS.values()) else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offset", type=int, default=0, help="article offset within each journal")
    ap.add_argument("--per", type=int, default=25, help="articles per journal per run")
    ap.add_argument("--max", type=int, default=400, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true", help="append into the registry (registry/scielo.yaml)")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    out, seen = [], set()
    for collection, issn, topic in JOURNALS:
        if len(out) >= args.max:
            break
        try:
            ids = _get("article/identifiers/", collection=collection, issn=issn,
                       offset=args.offset, limit=args.per)
        except Exception as e:
            print(f"# scielo {collection}/{issn} identifiers @{args.offset} failed: {e}",
                  file=sys.stderr)
            continue
        for obj in ids.get("objects", []):
            if len(out) >= args.max:
                break
            pid = obj.get("code") or ""
            sid = f"sci-{pid.lower()}"
            if not pid or sid in reg_ids:      # already held — skip WITHOUT a metadata request
                continue
            try:
                rec = _get("article/", collection=collection, code=pid, format="json")
            except Exception as e:
                print(f"# scielo {pid} metadata failed: {e}", file=sys.stderr)
                continue
            title = pick_title(rec.get("article") or {})
            url = pdf_url(rec, collection)
            if not title or not url:
                continue
            u, t = url.rstrip("/"), registry.norm(title)
            if u in urls or t in titles or u in seen:
                continue
            seen.add(u)
            titles.add(t)
            out.append({"id": sid, "title": title.strip()[:150], "url": url,
                        "source": f"scielo_{collection}", "license": "cc-by",
                        "topic": topic, "format": "pdf"})
            time.sleep(0.15)

    registry.uniquify_ids(out, reg_ids)

    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# {len(out)} NEW SciELO AEC articles (offset {args.offset}; cc-by, pt/es/en, "
          f"deduped vs manifest + registry + blocklist)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
