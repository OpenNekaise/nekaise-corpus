#!/usr/bin/env python3
"""find_books.py — discover open-access CC-BY(-SA)/CC0 books from OAPEN (corpus growth).

OAPEN (library.oapen.org) hosts thousands of open-access academic BOOKS; the built-environment / AEC
subject areas are a dense, cleanly-licensed vein (whole books, hundreds of pages of prose = CPT gold).
This backend queries the OAPEN REST search API across built-environment subjects, keeps English books
with a direct PDF bitstream whose license is CC-BY / CC-BY-SA / CC0, and appends ready-to-load
`sources.yaml` entries.

The license is NOT in the REST metadata — it lives in the OAI-PMH `xoai` record as a
creativecommons.org/licenses/<code> URL. We fetch that per candidate and keep ONLY `by` / `by-sa` /
`publicdomain/zero`; ANY `nc` or `nd` marker rejects the record (fail-closed).

Entries use the `oer-` id prefix, so they land ABOVE the `# --- discovered` marker (curated region)
and are never touched by `prune_corpus.py` — inserted, not appended at EOF, for that reason.

    python find_books.py                       # propose
    python find_books.py --append              # insert under `sources:` (then load + prune)
    python find_books.py --per 12 --max 120 --append
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent
SEARCH = "https://library.oapen.org/rest/search"
OAI = "https://library.oapen.org/oai/request"
UA = {"User-Agent": "nekaise-corpus/find_books"}

# (OAPEN subject term -> our corpus topic). Broad built-environment coverage.
QUERIES = [
    ("architecture design", "architecture"), ("architectural history", "architecture"),
    ("architectural theory", "architecture"), ("landscape architecture", "architecture"),
    ("heritage conservation", "architecture"), ("building acoustics", "architecture"),
    ("fire safety engineering", "architecture"), ("housing design", "architecture"),
    ("building construction", "construction"), ("construction management", "construction"),
    ("construction technology", "construction"), ("building information modeling", "construction"),
    ("prefabrication building", "construction"), ("building materials", "materials"),
    ("concrete technology", "materials"), ("timber engineering", "materials"),
    ("masonry construction", "materials"), ("steel structures", "structures_civil"),
    ("structural engineering", "structures_civil"), ("structural mechanics", "structures_civil"),
    ("earthquake engineering", "structures_civil"), ("bridge engineering", "structures_civil"),
    ("geotechnical engineering", "structures_civil"), ("soil mechanics", "structures_civil"),
    ("rock mechanics", "structures_civil"), ("tunnelling", "structures_civil"),
    ("foundation engineering", "structures_civil"), ("wind engineering", "structures_civil"),
    ("building physics", "building_energy"), ("energy efficient buildings", "building_energy"),
    ("sustainable architecture", "building_energy"), ("daylighting", "building_energy"),
    ("green building", "building_energy"), ("thermal comfort", "building_energy"),
    ("ventilation indoor air", "building_energy"), ("urban planning", "urban"),
    ("urban design", "urban"), ("smart cities", "urban"), ("urban climate", "urban"),
    ("transport planning", "infrastructure"), ("railway engineering", "infrastructure"),
    ("pavement engineering", "infrastructure"), ("hydraulic engineering", "infrastructure"),
    ("water resources engineering", "infrastructure"), ("coastal engineering", "infrastructure"),
    ("flood risk management", "infrastructure"), ("infrastructure resilience", "infrastructure"),
    ("geodesy surveying", "infrastructure"), ("life cycle assessment building", "standards_protocols"),
]


def slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")


def norm(s: str) -> str:
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def existing_keys():
    urls, titles = set(), set()
    mp = HERE / "manifest.jsonl"
    if mp.exists():
        for line in mp.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                urls.add((r.get("url") or "").rstrip("/"))
                titles.add(norm(r.get("title")))
    try:
        for s in yaml.safe_load((HERE / "sources.yaml").read_text())["sources"]:
            urls.add((s.get("url") or "").rstrip("/"))
            titles.add(norm(s.get("title")))
    except Exception:
        pass
    return urls, titles


def pdf_link(item) -> str | None:
    for b in item.get("bitstreams") or []:
        name = (b.get("name") or "").lower()
        if b.get("mimeType") == "application/pdf" and name.endswith(".pdf") and b.get("retrieveLink"):
            return "https://library.oapen.org" + b["retrieveLink"]
    return None


def is_english_book(item) -> bool:
    md = item.get("metadata") or []
    typ = " ".join(m["value"] for m in md if m.get("key") == "dc.type").lower()
    lang = " ".join(m["value"] for m in md if m.get("key") == "dc.language").lower()
    return ("book" in typ) and (not lang or "english" in lang or lang.strip() in ("en", "eng"))


def license_of(handle: str) -> str | None:
    """Return cc-by / cc-by-sa / cc0 from the OAPEN xoai record, or None (fail-closed) — ANY nc/nd rejects."""
    try:
        r = requests.get(OAI, params={"verb": "GetRecord", "metadataPrefix": "xoai",
                                      "identifier": f"oai:library.oapen.org:{handle}"},
                         headers=UA, timeout=30)
        if r.status_code != 200:
            return None
        t = r.text
        if re.search(r"creativecommons\.org/publicdomain/zero", t):
            return "cc0"
        codes = set(re.findall(r"creativecommons\.org/licenses/([a-z-]+)", t))
        if not codes or any(("nc" in c or "nd" in c) for c in codes):
            return None
        if "by-sa" in codes:
            return "cc-by-sa"
        if "by" in codes:
            return "cc-by"
        return None
    except Exception:
        return None


def emit(e) -> str:
    e = {k: e[k] for k in ("id", "title", "url", "source", "license", "topic", "format")}
    d = yaml.safe_dump([e], sort_keys=False, allow_unicode=True)
    return "".join(("  " + ln + "\n") if ln else "\n" for ln in d.splitlines())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per", type=int, default=12, help="results per subject query")
    ap.add_argument("--max", type=int, default=120, help="cap new books appended per run")
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args()

    urls, titles = existing_keys()
    out, seen = [], set()
    for term, topic in QUERIES:
        if len(out) >= args.max:
            break
        try:
            r = requests.get(SEARCH, params={"query": term, "expand": "bitstreams,metadata",
                                             "limit": args.per}, headers=UA, timeout=45)
            r.raise_for_status()
            items = r.json()
        except Exception as e:
            print(f"# search '{term}' failed: {e}", file=sys.stderr)
            continue
        for it in items:
            if len(out) >= args.max:
                break
            title, url, handle = it.get("name"), pdf_link(it), it.get("handle")
            if not (title and url and handle):
                continue
            u, t = url.rstrip("/"), norm(title)
            if u in urls or t in titles or u in seen:
                continue
            if not is_english_book(it):
                continue
            lic = license_of(handle)
            time.sleep(0.25)  # be polite to the OAI-PMH endpoint
            if not lic:
                continue
            seen.add(u)
            out.append({"id": f"oer-{slug(title)[:52]}", "title": title.strip()[:150], "url": url,
                        "source": "oapen", "license": lic, "topic": topic, "format": "pdf"})
        time.sleep(0.4)

    used: set = set()
    for h in out:
        base, i = h["id"], 2
        while h["id"] in used:
            h["id"] = f"{base[:50]}-{i}"
            i += 1
        used.add(h["id"])

    by_lic: dict = {}
    for h in out:
        by_lic[h["license"]] = by_lic.get(h["license"], 0) + 1
    print(f"# {len(out)} NEW OAPEN books (English, CC-BY/-SA/0, deduped vs manifest + registry)")
    print(f"# by license: {by_lic}")
    print("# --- review, then --append (inserts ABOVE the discovered marker), then build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        text = (HERE / "sources.yaml").read_text()
        block = "\n  # --- curated books via find_books.py (OAPEN CC-BY) ---\n" + "".join(emit(h) for h in out)
        marker = "# --- discovered"
        idx = text.find(marker)
        if idx == -1:
            with open(HERE / "sources.yaml", "a") as f:
                f.write(block)
        else:
            # insert before the first discovered marker so these curated books survive prune
            line_start = text.rfind("\n", 0, idx) + 1
            new = text[:line_start] + block.lstrip("\n") + "\n" + text[line_start:]
            assert len(yaml.safe_load(new)["sources"]) == len(yaml.safe_load(text)["sources"]) + len(out)
            (HERE / "sources.yaml").write_text(new)
        print(f"# inserted {len(out)} book entries into sources.yaml", file=sys.stderr)


if __name__ == "__main__":
    main()
