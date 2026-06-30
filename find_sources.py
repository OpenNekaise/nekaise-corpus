#!/usr/bin/env python3
"""find_sources.py — discover open-access building-energy sources (corpus growth).

Three backends, all free / no key:
  - OpenAlex  : scholarly works; we scan ALL OA locations and keep a PDF on a download-friendly host
                (publisher pages 403 bots, so we prefer repository / gov / arXiv / PMC / MDPI copies).
  - OSTI      : US DOE / national-lab reports (public-domain, downloadable via /servlets/purl).
  - arXiv API : open preprints (always downloadable at arxiv.org/pdf).

Keeps candidates with a fetchable PDF, dedups against the current manifest + registry, and PROPOSES
ready-to-paste `sources.yaml` entries. Review, then `--append` (or paste) and run the loader.

    python find_sources.py --per 20            # propose
    python find_sources.py --per 20 --append   # append under `sources:` in sources.yaml, then load + prune
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

HERE = Path(__file__).resolve().parent
MAILTO = "corpus@opennekaise.org"

# (search term -> our corpus topic). Many specific sub-topic queries -> more unique results.
QUERIES = [
    ("HVAC supervisory control sequences ASHRAE Guideline 36", "controls_bas"),
    ("model predictive control building HVAC energy", "controls_bas"),
    ("reinforcement learning HVAC control building", "controls_bas"),
    ("BACnet Modbus building automation communication protocol", "controls_bas"),
    ("demand controlled ventilation occupancy based control", "controls_bas"),
    ("PID control loop tuning air handling unit", "controls_bas"),
    ("automated fault detection diagnostics HVAC chiller", "commissioning_fdd"),
    ("building retro-commissioning energy savings", "commissioning_fdd"),
    ("air handling unit fault detection sensor diagnostics", "commissioning_fdd"),
    ("anomaly detection building energy operation", "commissioning_fdd"),
    ("monitoring based commissioning building performance", "commissioning_fdd"),
    ("rooftop unit fault detection diagnostics RTU", "commissioning_fdd"),
    ("chiller plant optimization performance modeling", "equipment_systems"),
    ("heat pump performance coefficient of performance", "equipment_systems"),
    ("variable refrigerant flow VRF system performance", "equipment_systems"),
    ("cooling tower condenser water system", "equipment_systems"),
    ("energy recovery ventilation enthalpy wheel", "equipment_systems"),
    ("boiler hydronic heating system efficiency", "equipment_systems"),
    ("building energy simulation EnergyPlus calibration", "building_energy"),
    ("building envelope thermal performance retrofit", "building_energy"),
    ("net zero energy building design renewable", "building_energy"),
    ("building electricity load forecasting machine learning", "building_energy"),
    ("building electrification heat pump decarbonization", "building_energy"),
    ("occupant thermal comfort energy efficiency building", "building_energy"),
    ("urban building energy modeling stock", "building_energy"),
    ("Brick schema building ontology metadata", "standards_protocols"),
    ("Project Haystack semantic tagging building data", "standards_protocols"),
    ("building information modeling IFC interoperability", "standards_protocols"),
    ("semantic data model building automation 223P", "standards_protocols"),
    ("digital twin building automation systems", "standards_protocols"),
    ("grid interactive efficient buildings demand flexibility", "standards_protocols"),
    # --- depth + under-pumped veins (mission: MORE good building-energy text) ---
    ("advanced rooftop unit controller retrofit savings", "controls_bas"),
    ("data center cooling control optimization efficiency", "controls_bas"),
    ("lighting controls daylighting commercial building", "controls_bas"),
    ("ASHRAE Guideline 36 high performance control sequences", "controls_bas"),
    ("ongoing commissioning energy information system", "commissioning_fdd"),
    ("chiller plant fault detection field demonstration", "commissioning_fdd"),
    ("automated fault detection diagnostics building portfolio", "commissioning_fdd"),
    ("cold climate air source heat pump field performance", "equipment_systems"),
    ("heat pump water heater field performance", "equipment_systems"),
    ("thermal energy storage building cooling load shifting", "equipment_systems"),
    ("dedicated outdoor air system DOAS design performance", "equipment_systems"),
    ("electrification gas to heat pump retrofit building", "equipment_systems"),
    ("building stock energy model ResStock ComStock", "building_energy"),
    ("commercial buildings energy consumption survey end use", "building_energy"),
    ("residential energy consumption end use load profile", "building_energy"),
    ("deep energy retrofit measured savings case study", "building_energy"),
    ("embodied carbon building life cycle assessment", "building_energy"),
    ("ASHRAE 90.1 energy savings determination", "standards_protocols"),
    ("residential energy code cost effectiveness IECC", "standards_protocols"),
    ("building energy code compliance field study", "standards_protocols"),
    ("measurement and verification IPMVP savings protocol", "standards_protocols"),
]

# hosts that reliably serve a direct PDF to a bot (publisher pages 403, so we whitelist).
WHITELIST = ("arxiv.org", ".gov", "escholarship.org", "ncbi.nlm.nih.gov", "europepmc.org",
             "mdpi.com", "plos.org", "frontiersin.org", "biomedcentral.com")
PERMISSIVE = {"cc-by", "cc-by-sa", "cc0", "public-domain"}


def norm(s: str) -> str:
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")


def downloadable(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(w in host for w in WHITELIST)


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
    except Exception:
        pass
    return urls, titles


def entry(title, url, source, license, topic):
    return {"id": f"{source[:3]}-{slug(title)[:46]}", "title": (title or "").strip()[:150],
            "url": url, "source": source, "license": license, "topic": topic, "format": "pdf"}


def from_openalex(term, topic, per):
    p = {"search": term, "filter": "open_access.is_oa:true,type:article",
         "per-page": per, "sort": "cited_by_count:desc", "mailto": MAILTO}
    r = requests.get("https://api.openalex.org/works", params=p, timeout=30)
    r.raise_for_status()
    out = []
    for w in r.json().get("results", []):
        locs = [w.get("best_oa_location")] + (w.get("locations") or [])
        pick = next((l for l in locs if l and l.get("pdf_url") and downloadable(l["pdf_url"])), None)
        if not pick:
            continue
        lic = (pick.get("license") or "").lower()
        tag = lic if lic in PERMISSIVE else "open"
        title = w.get("title") or w.get("display_name")
        if title:
            out.append(entry(title, pick["pdf_url"], "openalex", tag, topic))
    return out


def from_osti(term, topic, per):
    r = requests.get("https://www.osti.gov/api/v1/records",
                     params={"q": term, "rows": per}, timeout=30)
    r.raise_for_status()
    out = []
    for rec in r.json():
        oid = rec.get("osti_id") or rec.get("id")
        ft = any("purl" in (l.get("href") or "") or l.get("rel") == "fulltext"
                 for l in rec.get("links", []))
        if not oid or not ft:
            continue
        out.append(entry(rec.get("title"), f"https://www.osti.gov/servlets/purl/{oid}",
                         "osti", "public-domain", topic))
    return out


def from_arxiv(term, topic, per):
    time.sleep(3.1)  # arXiv API asks for >=3s between requests (429 otherwise)
    r = requests.get("https://export.arxiv.org/api/query",
                     params={"search_query": f"all:{term}", "max_results": per,
                             "sortBy": "relevance"}, timeout=30)
    r.raise_for_status()
    out = []
    for e in re.findall(r"<entry>(.*?)</entry>", r.text, re.S):
        m = re.search(r"<id>https?://arxiv\.org/abs/([^<]+)</id>", e)
        t = re.search(r"<title>(.*?)</title>", e, re.S)
        if not m:
            continue
        aid = re.sub(r"v\d+$", "", m.group(1).strip())
        out.append(entry((t.group(1) if t else "").strip(),
                         f"https://arxiv.org/pdf/{aid}", "arxiv", "open", topic))
    return out


BACKENDS = {"openalex": from_openalex, "osti": from_osti, "arxiv": from_arxiv}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per", type=int, default=15, help="results per backend per topic query")
    ap.add_argument("--backends", default="openalex,osti,arxiv")
    ap.add_argument("--append", action="store_true",
                    help="append candidates under `sources:` in sources.yaml (then load + prune)")
    args = ap.parse_args()
    backends = [b.strip() for b in args.backends.split(",") if b.strip() in BACKENDS]

    urls, titles = existing_keys()
    out, seen = [], set()
    for term, topic in QUERIES:
        for b in backends:
            try:
                hits = BACKENDS[b](term, topic, args.per)
            except Exception as e:
                print(f"# {b} [{topic}] failed: {e}", file=sys.stderr)
                continue
            for h in hits:
                u, t = h["url"].rstrip("/"), norm(h["title"])
                if not h["title"] or u in urls or t in titles or u in seen:
                    continue
                seen.add(u)
                out.append(h)

    # de-collide ids: id = source[:3]-slug[:46] can clash for distinct URLs, and the manifest is
    # id-keyed, so a clash would silently overwrite a doc. Suffix any repeat within the batch.
    used: set = set()
    for h in out:
        base = h["id"]
        i = 2
        while h["id"] in used:
            h["id"] = f"{base[:44]}-{i}"
            i += 1
        used.add(h["id"])

    by_topic, by_src, by_lic = {}, {}, {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
        by_src[h["source"]] = by_src.get(h["source"], 0) + 1
        by_lic[h["license"]] = by_lic.get(h["license"], 0) + 1
    print(f"# {len(out)} NEW candidates on download-friendly hosts (deduped vs manifest + registry)")
    print(f"# by topic:   {by_topic}")
    print(f"# by source:  {by_src}")
    print(f"# by license: {by_lic}")
    print("# --- review, then --append (or paste under `sources:`), then run build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        blk = ""
        for h in out:
            e = {k: h[k] for k in ("id", "title", "url", "source", "license", "topic", "format")}
            d = yaml.safe_dump([e], sort_keys=False, allow_unicode=True)
            blk += "".join(("  " + ln + "\n") if ln else "\n" for ln in d.splitlines())
        with open(HERE / "sources.yaml", "a") as f:
            f.write("\n  # --- discovered via find_sources.py ---\n" + blk)
        print(f"# appended {len(out)} entries to sources.yaml", file=sys.stderr)


if __name__ == "__main__":
    main()
