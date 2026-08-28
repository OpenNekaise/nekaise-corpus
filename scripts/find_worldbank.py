#!/usr/bin/env python3
"""find_worldbank.py — World Bank Documents & Reports via the official WDS search API.

The Bank's public Documents & Reports API (documented at
https://documents.worldbank.org/en/publication/documents-reports/api) is a plain REST endpoint:
    https://search.worldbank.org/api/v3/wds?format=json&qterm=<q>&rows=50&os=<offset>
Response: {"total": N, "documents": {"D<id>": {docty, docdt, display_title, pdfurl, guid, ...},
"facets": {...}}} — one JSON dict per doc, `pdfurl` is a direct documents.worldbank.org PDF link
(the same host our hand-curated wb-* ESMAP PDFs already load from). This is an API built for
programmatic access — no scraping, no wall. "building energy efficiency" alone = ~17k records
(2026-07), so the vein is deep; rotate `--os` via registry/rotation.json and switch `--q` between
rounds (see the rotation note).

License: Documents & Reports mixes formal publications with operational/project-cycle documents,
including borrower-authored material.  Public disclosure is not a blanket CC-BY grant, so WDS
results are tagged ``open`` unless a future per-document rights check proves a narrower license.

Junk control: only research/sector-work major document types with an explicit multilingual AEC
title anchor are eligible.  Bulk operational paperwork (procurement plans, project documents,
disbursement letters, agreements, audits) is skipped before it ever hits the registry; the prune
gate catches the rest.

    python scripts/find_worldbank.py --q "building energy efficiency" --os 0 --pages 4   # propose
    python scripts/find_worldbank.py --os 200 --max 200 --append
"""
from __future__ import annotations

import argparse
import re
import sys
import time

import requests
import yaml

import registry

API = "https://search.worldbank.org/api/v3/wds"
UA = {"User-Agent": "nekaise-corpus/find_worldbank"}

# Major document types centered on publications, research, and sector analysis. Borrower-authored
# safeguards and other project-cycle paperwork live under Project Documents, so fail closed on
# missing or unknown values. Some API values are semicolon-separated (for example
# "Publications; Publications & Research").
ALLOWED_MAJOR_TYPES = frozenset({"Publications & Research", "Economic & Sector Work"})

# Document types that remain low-value operational/institutional furniture even when WDS places
# them under an allowed major type.  The live API calls the president memo "Memorandum &
# Recommendation of the President", not the older denylist's "memorandum of the president".
JUNK_DOCTY = re.compile(
    r"procurement plan|disbursement|agreement|auditing document|audit report|agenda|"
    r"month(ly)? operational summary|statement of loans|notice|contract|invitation|"
    r"letter|memorandum\s*(?:&|and)\s*recommendation of the president|"
    r"staff appraisal report|project information document|implementation status (?:and|&) "
    r"results report|\bISR\b|environmental and social review summary|\bESRS\b|"
    r"stakeholder engagement plan|announcement|newsletter|project completion report|"
    r"chairman summary|board summary", re.I)
JUNK_TITLE = re.compile(
    r"procurement plan|disbursement|audit(ed)? report|board meeting calendar|"
    r"memorandum\s*(?:&|and)\s*recommendation of the president", re.I)

# WDS full-text search becomes very fuzzy at depth: generic country-economic reports can rank for
# an AEC query because a phrase appears somewhere in the body.  Require title-level evidence that
# is specific enough to survive this source's development-economics background.  Unlike the
# English-only gov.uk finder, WDS is multilingual, so the anchors cover the same major language
# families as the corpus quality gate.
TITLE_RELEVANCE = re.compile(
    r"\b(?:"
    r"built environment|buildings?|construction(?: industry| sector| materials?| technology)?|"
    r"architectur(?:e|al)|housing|dwellings?|slum upgrading|"
    r"urban (?:planning|design|development|infrastructure|transport|mobility|water|sanitation|"
    r"housing|regeneration)|cities? (?:planning|infrastructure|transport|water|sanitation|housing)|"
    r"building energy|energy efficien(?:cy|t)|energy performance|retrofit|insulat|"
    r"district heat|heat pumps?|heating|cooling|ventilat|air conditioning|hvac|"
    r"roads?|highways?|bridges?|tunnels?|railways?|railroads?|mass transit|public transport|"
    r"pavements?|ports? infrastructure|water supply|water infrastructure|water networks?|"
    r"sanitation|sewerage|wastewater|drainage|stormwater|flood|irrigation|"
    r"concrete|cement|masonry|timber construction|structural engineering|geotechnical|"
    r"fire safety|building codes?|construction standards?|"
    # French
    r"bâtiments?|génie civil|\bBTP\b|travaux publics|secteur routier|logements?|urbanisme|"
    r"infrastructures?|routes?|ponts?|"
    r"assainissement|eau potable|béton|chauffage|isolation|efficacité énergétique|"
    # Spanish / Portuguese / Italian
    r"edificios?|edifícios?|construcción|construção|viviendas?|habitação|urbanización|urbanismo|"
    r"infraestructuras?|infraestruturas?|carreteras?|rodovias?|puentes?|pontes?|saneamiento|"
    r"saneamento|agua potable|abastecimento de água|hormigón|calefacción|aislamiento|"
    r"eficiencia energética|efficienza energetica|edilizia|calcestruzzo|riscaldamento|"
    # German / Dutch / Nordic
    r"gebäude|bauwesen|baustoff|heizung|lüftung|dämmung|tragwerk|brandschutz|"
    r"gebouw|bouwkunde|verwarming|byggnad|bygning|rakennus|uppvärmning|"
    # Russian / Ukrainian
    r"здания?|строительство|гражданское строительство|инфраструктура|дороги|мосты|"
    r"водоснабжение|канализация|бетон|отопление|вентиляция|энергоэффективность"
    r")\b|"
    # CJK and Arabic scripts do not use Latin word boundaries.
    r"建筑|建築|结构|構造|混凝土|暖通|空调|空調|通风|換気|節能|节能|断熱|桥梁|橋梁|隧道|"
    r"施工|城市规划|都市計画|コンクリート|건축|구조|공조|난방|단열|콘크리트|"
    r"المباني|البناء|العمارة|الهندسة المدنية|الخرسانة|كفاءة الطاقة|الطرق|الجسور|"
    r"المياه|الصرف الصحي|التخطيط الحضري",
    re.I,
)


def relevant(title: str, docty: str, majdocty: str) -> bool:
    """Whether one WDS result has a research-like type and explicit AEC title evidence."""
    major_types = {part.strip() for part in (majdocty or "").split(";") if part.strip()}
    if not major_types.intersection(ALLOWED_MAJOR_TYPES):
        return False
    if JUNK_DOCTY.search(docty or "") or JUNK_TITLE.search(title or ""):
        return False
    return bool(TITLE_RELEVANCE.search(title or ""))

# (title regex -> topic), first match wins; default building_energy.
TOPIC_RULES = [
    (re.compile(r"urban|city|cities|municipal", re.I), "urban"),
    (re.compile(r"district heat|heat pump|cooling|heating|boiler|chiller|appliance", re.I),
     "equipment_systems"),
    (re.compile(r"code|standard|regulation|certification|labell?ing", re.I), "standards_protocols"),
    (re.compile(r"cement|concrete|material|brick|timber", re.I), "materials"),
    (re.compile(r"construction|housing|building sector", re.I), "construction"),
    (re.compile(r"grid|transmission|infrastructure|road|transport", re.I), "infrastructure"),
]


def classify(title: str) -> str:
    for rx, topic in TOPIC_RULES:
        if rx.search(title):
            return topic
    return "building_energy"


def fetch_page(query: str, rows: int, offset: int) -> tuple[int, list[dict]]:
    """One API page -> (total, filtered document metadata)."""
    r = requests.get(API, params={
        "format": "json", "qterm": query, "rows": rows, "os": offset,
        "fl": "display_title,docdt,pdfurl,docty,majdocty,lang"}, headers=UA, timeout=30)
    r.raise_for_status()
    js = r.json()
    docs = []
    for key, d in js.get("documents", {}).items():
        if key == "facets" or not isinstance(d, dict):
            continue
        title = (d.get("display_title") or "").strip()
        pdf = (d.get("pdfurl") or "").strip()
        docty = (d.get("docty") or "").strip()
        majdocty = (d.get("majdocty") or "").strip()
        lang = (d.get("lang") or "").strip()
        if not title or not pdf:
            continue
        if not relevant(title, docty, majdocty):
            continue
        docs.append({"title": title, "pdf_url": pdf.replace("http://", "https://", 1),
                     "docty": docty, "majdocty": majdocty, "lang": lang})
    return int(js.get("total", 0)), docs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", default="building energy efficiency", help="WDS search query")
    ap.add_argument("--os", type=int, default=0, help="starting record offset")
    ap.add_argument("--rows", type=int, default=50, help="records per API page")
    ap.add_argument("--pages", type=int, default=4, help="API pages to walk this run")
    ap.add_argument("--max", type=int, default=200, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/worldbank.yaml)")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    out = []
    total = 0
    for page in range(args.pages):
        if len(out) >= args.max:
            break
        offset = args.os + page * args.rows
        try:
            total, docs = fetch_page(args.q, args.rows, offset)
        except Exception as e:
            print(f"# os={offset} fetch failed: {e}", file=sys.stderr)
            print("ERROR: refusing a partial append so rotation does not advance", file=sys.stderr)
            raise SystemExit(1)
        if offset >= total or not docs and offset > total - args.rows:
            break  # past the end of this query's results
        for d in docs:
            if len(out) >= args.max:
                break
            u, t = d["pdf_url"].rstrip("/"), registry.norm(d["title"])
            if u in urls or t in titles:
                continue
            urls.add(u)
            titles.add(t)
            sid = f"wbd-{registry.slug(d['title'])[:52]}"
            out.append({"id": sid, "title": d["title"][:150], "url": d["pdf_url"],
                        "source": "worldbank_wds", "license": "open",
                        "topic": classify(d["title"]), "format": "pdf"})
        time.sleep(1.0)  # politeness between API pages

    registry.uniquify_ids(out, reg_ids)
    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# {len(out)} NEW World Bank docs for {args.q!r} at os {args.os}..+{args.pages}x{args.rows} "
          f"of {total} total (deduped vs manifest + registry + blocklist)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
