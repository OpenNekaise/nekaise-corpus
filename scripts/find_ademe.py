#!/usr/bin/env python3
"""find_ademe.py — discover French building/energy reports on ADEME's librairie (PrestaShop).

librairie.ademe.fr is the French energy/environment agency's (ADEME) public document store —
guides, technical reports, and studies on building renovation, heating, insulation, urban
planning, and materials, almost all free PDF downloads. The corpus accepts all languages and
French building vocabulary is already in the quality gate (`scripts/quality.py`), so this is a
straight vein: walk a category's paginated listing (`?page=N`, 24 products/page, PrestaShop
`article.product-miniature`), then fetch each product page and pull the first PDF out of its
embedded `data-product` JSON blob (`attachments[]`, `id_attachment` -> direct download via
`index.php?controller=attachment&id_attachment=<ID>` — verified real %PDF bytes).

Known category slugs (numeric id + PrestaShop slug, probed 2026-07):
    3153-batiment                        building (renovation, heating, insulation) — default
    3149-energies                        energy (renewables, heat networks, electricity)
    3509-urbanisme-territoires-et-sols   urban planning / land / territories
A category page beyond the last one 200s with zero `product-miniature` articles (no error) — the
per-page loop just stops early rather than erroring.

License: librairie.ademe.fr's mentions légales grant non-commercial/pedagogical reuse with
attribution for ADEME publications -> tagged `open` (matches the `license: open` convention used
elsewhere in the registry, e.g. IBPSA proceedings).

This walks product pages one at a time — a single listing page of 24 products is 24 extra
fetches — so keep `--pages` small and the 0.7s sleep between every request (listing AND product).

    python scripts/find_ademe.py                                            # propose (page 1, 3 listing pages)
    python scripts/find_ademe.py --category 3149-energies --page 2 --pages 2 --max 100 --append
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time

import requests
import yaml
from bs4 import BeautifulSoup

import registry

BASE = "https://librairie.ademe.fr"
UA = {"User-Agent": "nekaise-corpus/find_ademe"}
SLEEP = 0.7  # politeness between EVERY request (listing pages and product pages alike)

# title regex -> topic. Checked in order (as specified); first match wins. Default: building_energy.
TOPIC_RULES = [
    (re.compile(r"isolation|rénovation|thermique|énergétique|chauffage", re.I), "building_energy"),
    (re.compile(r"pompe.{0,3}chaleur|ventilation|climatisation|brasseur", re.I), "equipment_systems"),
    (re.compile(r"urbanisme|\bville\b|quartier|territoire", re.I), "urban"),
    (re.compile(r"béton|matériaux|\bbois\b", re.I), "materials"),
    (re.compile(r"réseau.{0,3}chaleur", re.I), "equipment_systems"),
]


def topic_for(title: str) -> str:
    for rx, topic in TOPIC_RULES:
        if rx.search(title):
            return topic
    return "building_energy"


def fetch_listing_urls(category: str, page: int, session: requests.Session) -> list[str]:
    """One category listing page -> product page URLs (fragment stripped), in document order."""
    url = f"{BASE}/{category}"
    r = session.get(url, params={"page": page} if page > 1 else None, headers=UA, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    urls = []
    for art in soup.select("article.product-miniature"):
        a = art.find("a", href=True)
        if not a:
            continue
        urls.append(a["href"].split("#", 1)[0])
    return urls


def fetch_product(url: str, session: requests.Session) -> dict | None:
    """One product page -> {title, pdf_url, attachment_name} or None (no data-product / no PDF)."""
    try:
        r = session.get(url, headers=UA, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"# product fetch failed {url}: {e}", file=sys.stderr)
        return None
    m = re.search(r'data-product="([^"]*)"', r.text)
    if not m:
        return None
    try:
        data = json.loads(html.unescape(m.group(1)))  # data-product is an HTML attribute -> unescape first
    except Exception as e:
        print(f"# data-product JSON parse failed {url}: {e}", file=sys.stderr)
        return None
    title = (data.get("name") or "").strip()
    if not title:
        return None
    pdf = next((a for a in (data.get("attachments") or [])
                if (a.get("mime") or "").lower() == "application/pdf"), None)
    if not pdf or not pdf.get("id_attachment"):
        return None
    pdf_url = f"{BASE}/index.php?controller=attachment&id_attachment={pdf['id_attachment']}"
    return {"title": title, "pdf_url": pdf_url, "file_name": pdf.get("file_name") or ""}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default="3153-batiment", help="ADEME category slug, e.g. 3149-energies")
    ap.add_argument("--page", type=int, default=1, help="first listing page")
    ap.add_argument("--pages", type=int, default=3, help="number of listing pages to walk this run")
    ap.add_argument("--max", type=int, default=150, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true", help="append into the registry (registry/ademe.yaml)")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    session = requests.Session()

    product_urls: list[str] = []
    for i in range(args.pages):
        page = args.page + i
        try:
            page_urls = fetch_listing_urls(args.category, page, session)
        except Exception as e:
            print(f"# listing {args.category} page {page} failed: {e}", file=sys.stderr)
            break
        time.sleep(SLEEP)
        if not page_urls:
            print(f"# {args.category} page {page}: 0 products — stopping (last page reached)", file=sys.stderr)
            break
        product_urls.extend(page_urls)
    print(f"# {len(product_urls)} product URLs from {args.category} pages {args.page}..{args.page + args.pages - 1}",
          file=sys.stderr)

    out = []
    for purl in product_urls:
        if len(out) >= args.max:
            break
        if purl.rstrip("/") in urls:
            continue
        info = fetch_product(purl, session)
        time.sleep(SLEEP)
        if not info:
            continue
        title = info["title"]
        pdf_url = info["pdf_url"]
        u, t = pdf_url.rstrip("/"), registry.norm(title)
        if u in urls or t in titles:
            continue
        # disambiguate a generic report title with the attachment's own filename stem, if distinct
        stem = re.sub(r"\.pdf$", "", info["file_name"], flags=re.I).strip()
        full_title = title
        if stem and registry.norm(stem) not in registry.norm(title) and len(stem) > 3:
            full_title = f"{title} — {stem}"
        urls.add(u)
        titles.add(t)
        sid = f"adm-{registry.slug(title)[:52]}"
        out.append({"id": sid, "title": full_title[:150], "url": pdf_url, "source": "ademe",
                    "license": "open", "topic": topic_for(title), "format": "pdf"})

    registry.uniquify_ids(out, reg_ids)
    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# {len(out)} NEW ADEME reports from {args.category} (pages {args.page}..{args.page + args.pages - 1}, "
          f"deduped vs manifest + registry + blocklist)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
