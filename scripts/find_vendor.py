#!/usr/bin/env python3
"""find_vendor.py — manufacturer product literature (HVAC / building-controls / construction products).

One config-driven finder instead of one script per company. `registry/vendors.json` describes each
vendor's literature portal — how its documents are enumerated (`sitemap`: XML sitemap or sitemap
index listing the documents themselves; `sitemap_pages`: the sitemap lists product/document PAGES
and each page carries the PDF links — the common case — scanned a bounded number of pages per
round with a visited-page memory under workspace/; `html_index`: one or more listing pages carrying
direct PDF links), which URLs count
(`pdf_pattern` / `exclude_pattern`), the default topic and keyword→topic rules, the per-host
politeness delay, and the rights review (terms-of-use excerpt, robots verdict, reviewed_at). The
finder itself only fetches sitemaps / listing pages (a handful of requests per vendor, cached for
SITEMAP_TTL_DAYS under workspace/); build_corpus.py downloads the documents later under its own
per-host caps, so a vendor with 20,000 datasheets costs the round a few seconds of discovery.

Rights: the operator decided on 2026-08-28 that freely downloadable, login-free manufacturer product
literature (catalogs, data sheets, IOM manuals, engineering / selection guides, specification texts)
is ingested as `license: open` — the corpus never redistributes bytes. Vendors whose robots.txt
disallows the literature paths or names AI/dataset crawlers, or whose terms explicitly forbid
automated access or systematic downloading, are NO-GO and must be recorded in vendors.json with
`enabled: false` and the reason (same precedent as ERDC / NRC Canada in build_corpus.py).

Rotation: `--cursor N` selects enabled vendor N % V (round-robin, one vendor per round, so no host
sees two consecutive rounds); progress within a vendor is the registry/manifest/blocklist dedup —
each visit proposes the next `--max` documents not yet known. Exhausted vendors cost one cheap
visit per cycle. No rotation hold is requested: spreading rounds across vendors is the politeness.

    python scripts/find_vendor.py --vendor carrier --max 20            # propose one vendor
    python scripts/find_vendor.py --cursor 7 --max 200 --append        # what run_round does
    python scripts/find_vendor.py --list                                # config + enabled state
"""
from __future__ import annotations

import argparse
import gzip
import html as htmllib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from xml.etree import ElementTree

import requests

import ops
import registry

HERE = Path(__file__).resolve().parents[1]
VENDORS_PATH = registry.REG_DIR / "vendors.json"
CACHE_DIR = ops.WORKSPACE / "vendor-sitemaps"
SITEMAP_TTL_DAYS = 7
UA = {"User-Agent": "nekaise-corpus/find_vendor (research corpus; sitemap/listing discovery only)"}
MECHANISMS = {"sitemap", "sitemap_pages", "html_index"}
DEFAULT_PAGES_PER_RUN = 40
VISITED_TTL_DAYS = 90
MAX_CHILD_SITEMAPS = 300
DEFAULT_PDF_PATTERN = r"\.pdf(?:$|[?#])"
HREF_RE = re.compile(r"""href=["']([^"'#]+)["']""", re.I)
ANCHOR_RE = re.compile(r"""<a\s[^>]*?href=["']([^"'#]+)["'][^>]*>(.*?)</a>""", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
# document URLs embedded in page JSON/scripts (Episerver/React pages carry them as "url":"https:\/\/..."):
RAW_URL_RE = re.compile(r"""https?://[^\s"'<>\\]+?\.pdf(?:\?[^\s"'<>\\]*)?""", re.I)
HEXID_RE = re.compile(r"^[0-9a-f]{8}(?:-?[0-9a-f]{4}){3,}|^[0-9a-f]{16,}$|^\d{5,}$", re.I)

TOPIC_RULES_DEFAULT = [  # URL/title keyword -> registry topic; vendor rules are tried first
    (r"bacnet|modbus|knx|lonworks|controller|thermostat|actuator|sensor|bms|building automation|"
     r"metasys|desigo|niagara|ecostruxure", "controls_bas"),
    (r"chiller|boiler|heat pump|rooftop|air handl|ahu|fan coil|vrf|vrv|split|condens|furnace|"
     r"cooling tower|ventilat|hvac|damper|diffuser|grille|refriger|humidif|dehumid|pump|valve", "equipment_systems"),
    (r"energy|efficien|kwh|seer|eer|cop\b|commission", "building_energy"),
    (r"insulat|gypsum|plaster|concrete|cement|mortar|admixture|membrane|roofing|sealant|adhesive|"
     r"anchor|fastener|steel|timber|wood|brick|block|glass|glazing|facade|façade|cladding", "materials"),
    (r"structural|\bbeam|\bcolumn|\bslab|\bloads?\b|seismic|\bbridge", "structures_civil"),
    (r"elevator|escalator|\bdoors?\b|\blocks?\b|\bwindows?\b|\bfire\b|acoustic|lighting|luminaire", "architecture"),
    (r"install|\biom\b|manual|catalog|catalogue|datasheet|data sheet|submittal|\bspec", "construction"),
]


# ------------------------------------------------------------------------------------ config
def load_vendors(path: Path | None = None) -> dict[str, dict]:
    """Vendor configs keyed by `key`; fails closed on schema problems (contracts call this too)."""
    path = Path(path or VENDORS_PATH)
    data = json.loads(path.read_text())
    errors = validate_vendors(data)
    if errors:
        raise ValueError(f"invalid {path}: " + "; ".join(errors))
    return data["vendors"]


def validate_vendors(data: object) -> list[str]:
    if not isinstance(data, dict):
        return ["top level must be an object"]
    errors: list[str] = []
    vendors = data.get("vendors")
    if not isinstance(vendors, dict):
        return ["vendors must be an object keyed by vendor key"]
    for key, cfg in vendors.items():
        label = f"vendors.{key}"
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", key or ""):
            errors.append(f"{label}: key must be a short lowercase slug")
        if not isinstance(cfg, dict):
            errors.append(f"{label}: must be an object")
            continue
        for field in ("name", "source", "mechanism", "topic", "rights"):
            if not cfg.get(field):
                errors.append(f"{label}: missing {field}")
        mech = cfg.get("mechanism")
        if mech not in MECHANISMS:
            errors.append(f"{label}: mechanism must be one of {sorted(MECHANISMS)}")
        urls_key = {"sitemap": "sitemaps", "sitemap_pages": "sitemaps",
                    "html_index": "index_urls"}.get(mech)
        if urls_key:
            urls = cfg.get(urls_key)
            if (not isinstance(urls, list) or not urls
                    or any(not isinstance(u, str) or not u.startswith("https://") for u in urls)):
                errors.append(f"{label}: {urls_key} must be a non-empty HTTPS URL list")
        if cfg.get("topic") and cfg["topic"] not in registry_topics():
            errors.append(f"{label}: unknown topic {cfg['topic']!r}")
        if mech == "sitemap_pages" and not cfg.get("page_pattern"):
            errors.append(f"{label}: sitemap_pages needs page_pattern (which sitemap URLs are pages)")
        ppr = cfg.get("pages_per_run", DEFAULT_PAGES_PER_RUN)
        if not isinstance(ppr, int) or ppr < 1:
            errors.append(f"{label}: pages_per_run must be a positive integer")
        for pat_key in ("pdf_pattern", "exclude_pattern", "lang_pattern", "page_pattern", "sitemap_filter"):
            if pat := cfg.get(pat_key):
                try:
                    re.compile(pat)
                except re.error as exc:
                    errors.append(f"{label}: {pat_key} does not compile: {exc}")
        for rule in cfg.get("topic_rules") or []:
            if (not isinstance(rule, list) or len(rule) != 2
                    or rule[1] not in registry_topics()):
                errors.append(f"{label}: topic_rules entries must be [regex, topic]")
        rights = cfg.get("rights")
        if isinstance(rights, dict):
            for field in ("tos_url", "reviewed_at", "decision"):
                if not rights.get(field):
                    errors.append(f"{label}: rights.{field} is required")
            if rights.get("decision") not in ("go", "no-go"):
                errors.append(f"{label}: rights.decision must be 'go' or 'no-go'")
            if rights.get("decision") == "no-go" and cfg.get("enabled", True):
                errors.append(f"{label}: rights.decision is no-go but vendor is enabled")
        elif rights:
            errors.append(f"{label}: rights must be an object")
        delay = cfg.get("crawl_delay", 0)
        if not isinstance(delay, (int, float)) or delay < 0:
            errors.append(f"{label}: crawl_delay must be a non-negative number")
    return errors


def registry_topics() -> set[str]:
    import lint_registry
    return set(lint_registry.TOPICS)


def enabled_vendors(vendors: dict[str, dict]) -> list[str]:
    return [k for k, cfg in vendors.items() if cfg.get("enabled", True)]


def host_delays(vendors: dict[str, dict]) -> dict[str, float]:
    """host -> seconds between request starts; build_corpus merges this into its HOST_DELAY."""
    out: dict[str, float] = {}
    for cfg in vendors.values():
        delay = float(cfg.get("crawl_delay", 0) or 0)
        if delay <= 0:
            continue
        for u in (cfg.get("sitemaps") or []) + (cfg.get("index_urls") or []):
            out[urlsplit(u).netloc] = max(out.get(urlsplit(u).netloc, 0), delay)
        for h in cfg.get("hosts") or []:
            out[h] = max(out.get(h, 0), delay)
    return out


# ------------------------------------------------------------------------------------ fetch
def fetch(url: str, delay: float = 0.0) -> bytes:
    if delay:
        time.sleep(delay)
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    data = r.content
    if data[:2] == b"\x1f\x8b":  # by magic, not suffix: some hosts serve *.xml.gz already inflated
        data = gzip.decompress(data)
    return data


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def cached_universe(key: str) -> list[str] | None:
    p = _cache_path(key)
    if not p.exists():
        return None
    try:
        blob = json.loads(p.read_text())
    except json.JSONDecodeError:
        return None
    if time.time() - blob.get("fetched_at", 0) > SITEMAP_TTL_DAYS * 86400:
        return None
    return blob.get("urls")


def store_universe(key: str, urls: list[str]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ops.atomic_write_text(_cache_path(key), json.dumps({"fetched_at": time.time(), "urls": urls}))


def _state_path(key: str, kind: str) -> Path:
    return CACHE_DIR / f"{key}.{kind}.json"


def load_state(key: str, kind: str) -> dict:
    p = _state_path(key, kind)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def store_state(key: str, kind: str, state: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ops.atomic_write_text(_state_path(key, kind), json.dumps(state))


# ------------------------------------------------------------------------------------ enumerate
def _xml_locs(data: bytes) -> tuple[str, list[str]]:
    """(kind, locs): kind is 'index' for a sitemapindex, 'urlset' otherwise."""
    root = ElementTree.fromstring(data)
    tag = root.tag.rsplit("}", 1)[-1]
    locs = [
        (el.text or "").strip()
        for el in root.iter()
        if el.tag.rsplit("}", 1)[-1] == "loc" and (el.text or "").strip()
    ]
    return ("index" if tag == "sitemapindex" else "urlset"), locs


def enumerate_sitemap(cfg: dict, fetcher=fetch) -> list[str]:
    delay = float(cfg.get("crawl_delay", 0) or 0)
    seen: list[str] = []
    children_budget = MAX_CHILD_SITEMAPS
    child_filter = re.compile(cfg["sitemap_filter"]) if cfg.get("sitemap_filter") else None
    for top in cfg["sitemaps"]:
        kind, locs = _xml_locs(fetcher(top, delay))
        if kind == "urlset":
            seen.extend(locs)
            continue
        for child in locs:
            if child_filter and not child_filter.search(child):
                continue
            if children_budget <= 0:
                print(f"# {cfg['name']}: sitemap index exceeds {MAX_CHILD_SITEMAPS} children — "
                      "narrow it with sitemap_filter", file=sys.stderr)
                break
            children_budget -= 1
            _, locs2 = _xml_locs(fetcher(child, delay))
            seen.extend(locs2)
    return seen


def enumerate_html_index(cfg: dict, fetcher=fetch) -> list[str]:
    delay = float(cfg.get("crawl_delay", 0) or 0)
    out: list[str] = []
    for page in cfg["index_urls"]:
        text = fetcher(page, delay).decode("utf-8", errors="replace")
        for href in HREF_RE.findall(text):
            out.append(urljoin(page, htmllib.unescape(href)))
    return out


def pdf_links(page_url: str, text: str) -> list[tuple[str, str]]:
    """(absolute url, anchor text) for every link on a page; the anchor text is usually the
    document's own title ('Product data sheet EN'), far better than a slug."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, inner in ANCHOR_RE.findall(text):
        url = urljoin(page_url, htmllib.unescape(href))
        label = re.sub(r"\s+", " ", htmllib.unescape(TAG_RE.sub(" ", inner))).strip()[:120]
        if url not in seen:
            seen.add(url)
            out.append((url, label))
    for href in HREF_RE.findall(text):  # links without an <a> wrapper we could parse
        url = urljoin(page_url, htmllib.unescape(href))
        if url not in seen:
            seen.add(url)
            out.append((url, ""))
    for url in RAW_URL_RE.findall(text.replace("\\/", "/")):  # JSON-embedded document URLs
        url = htmllib.unescape(url)
        if url not in seen:
            seen.add(url)
            out.append((url, ""))
    return out


def scan_pages(key: str, cfg: dict, pages: list[str], budget: int, fetcher=fetch) -> list[str]:
    """sitemap_pages: visit up to `budget` not-yet-visited pages, remember them (successful fetches
    only), and accumulate every document link ever seen for this vendor in a docs memory — so a
    run capped by --max proposes the remainder next time without re-fetching pages, and a wiped
    workspace merely costs a re-scan (dedup makes that harmless)."""
    delay = float(cfg.get("crawl_delay", 0) or 0)
    page_re = re.compile(cfg["page_pattern"], re.I)
    visited = load_state(key, "visited")
    docs = load_state(key, "docs")
    now = time.time()
    fresh = now - VISITED_TTL_DAYS * 86400
    todo = [p for p in pages if page_re.search(p) and visited.get(p, 0) < fresh][:budget]
    fetched = failed = 0
    for page in todo:
        try:
            text = fetcher(page, delay).decode("utf-8", errors="replace")
        except Exception as exc:
            failed += 1
            print(f"# page fetch failed {page}: {exc}", file=sys.stderr)
            continue
        fetched += 1
        visited[page] = now
        for link, label in pdf_links(page, text):
            docs.setdefault(link, {"t": now, "title": label, "page": page})
    if todo and fetched == 0:
        raise RuntimeError(f"all {len(todo)} page fetches failed")
    store_state(key, "visited", visited)
    store_state(key, "docs", docs)
    remaining = sum(1 for p in pages if page_re.search(p) and visited.get(p, 0) < fresh)
    print(f"# {cfg['name']}: scanned {fetched} pages ({failed} failed), {remaining} pages still "
          f"unvisited, {len(docs)} document links known")
    return list(docs)


def known_titles_for(key: str) -> dict[str, str]:
    """url -> anchor text remembered by scan_pages (empty for sitemap/html_index vendors)."""
    return {u: (v.get("title") or "") for u, v in load_state(key, "docs").items()
            if isinstance(v, dict)}


def select_documents(cfg: dict, universe: list[str]) -> list[str]:
    """Apply the vendor's URL filters; order preserved, duplicates removed."""
    keep = re.compile(cfg.get("pdf_pattern") or DEFAULT_PDF_PATTERN, re.I)
    drop = re.compile(cfg["exclude_pattern"], re.I) if cfg.get("exclude_pattern") else None
    lang = re.compile(cfg["lang_pattern"], re.I) if cfg.get("lang_pattern") else None
    out: list[str] = []
    seen: set[str] = set()
    for u in universe:
        u = u.strip()
        if not u.startswith("http") or not keep.search(u):
            continue
        if drop and drop.search(u):
            continue
        if lang and not lang.search(u):
            continue
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


# ------------------------------------------------------------------------------------ entries
def title_for(cfg: dict, url: str, label: str = "") -> str:
    """Vendor name + (anchor text if a page gave us one) + de-slugged path tail. The parent
    segment keeps same-named files ('datasheet.pdf') from colliding in title dedup; opaque
    ids (uuids, hex, bare numbers) are dropped from the text but still make the id unique."""
    parts = [unquote(p) for p in urlsplit(url).path.split("/") if p]
    tail = parts[-2:] if len(parts) >= 2 else parts
    words = []
    for seg in tail:
        seg = re.sub(r"\.(pdf|html?)$", "", seg, flags=re.I)
        if HEXID_RE.match(seg) and seg is not tail[-1]:
            continue
        words.append(re.sub(r"[-_+.]+", " ", seg).strip())
    slug = " — ".join(w for w in words if w)
    label = re.sub(r"\s+", " ", label).strip()
    text = f"{label} ({slug})" if label and label.lower() not in slug.lower() else slug
    return f"{cfg['name']}: {text}"[:150]


def topic_for(cfg: dict, url: str, title: str) -> str:
    hay = f"{url} {title}".lower()
    for pattern, topic in (cfg.get("topic_rules") or []) + TOPIC_RULES_DEFAULT:
        if re.search(pattern, hay, re.I):
            return topic
    return cfg["topic"]


def doc_id(key: str, url: str) -> str:
    parts = [p for p in urlsplit(url).path.split("/") if p]
    tail = re.sub(r"\.(pdf|html?)$", "", parts[-1], flags=re.I) if parts else "doc"
    return f"vnd-{key}-{registry.slug(tail)}"[:90]


def entries_for(key: str, cfg: dict, docs: list[str], known_urls: set, known_titles: set,
                cap: int, labels: dict[str, str] | None = None) -> list[dict]:
    out: list[dict] = []
    labels = labels or {}
    for url in docs:
        if len(out) >= cap:
            break
        u = url.rstrip("/")
        if u in known_urls:
            continue
        title = title_for(cfg, url, labels.get(url, ""))
        t = registry.norm(title)
        if t in known_titles:
            continue
        known_urls.add(u)
        known_titles.add(t)
        fmt = "html" if re.search(r"\.html?(?:$|[?#])", url, re.I) else "pdf"
        entry = {
            "id": doc_id(key, url), "title": title, "url": url,
            "source": cfg["source"], "license": "open",
            "topic": topic_for(cfg, url, title), "format": fmt,
            "document_type": "product-literature",
            "license_url": cfg["rights"]["tos_url"],
            "license_evidence": cfg["rights"].get("tos_excerpt", "")[:200]
            or "publicly downloadable manufacturer literature, no login (operator decision 2026-08-28)",
            "rights_verified_at": cfg["rights"]["reviewed_at"],
        }
        if cfg.get("language"):
            entry["language"] = cfg["language"]
        out.append(entry)
    return out


# ------------------------------------------------------------------------------------ main
def universe_for(key: str, cfg: dict, refresh: bool = False, fetcher=fetch) -> list[str]:
    """Document URLs (sitemap / html_index) or page URLs (sitemap_pages), cached SITEMAP_TTL_DAYS."""
    urls = None if refresh else cached_universe(key)
    if urls is None:
        if cfg["mechanism"] == "html_index":
            urls = enumerate_html_index(cfg, fetcher)
        else:
            urls = enumerate_sitemap(cfg, fetcher)
        store_universe(key, urls)
    return urls


def candidate_documents(key: str, cfg: dict, universe: list[str], pages: int,
                        fetcher=fetch) -> list[str]:
    if cfg["mechanism"] == "sitemap_pages":
        budget = int(cfg.get("pages_per_run") or pages)
        return select_documents(cfg, scan_pages(key, cfg, universe, budget, fetcher))
    return select_documents(cfg, universe)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", type=int, help="stable automated cursor: enabled vendor N %% V")
    ap.add_argument("--vendor", help="vendor key (manual runs)")
    ap.add_argument("--max", type=int, default=200, help="cap on new entries this run")
    ap.add_argument("--pages", type=int, default=DEFAULT_PAGES_PER_RUN,
                    help="sitemap_pages: pages to scan this run (vendor pages_per_run overrides)")
    ap.add_argument("--refresh", action="store_true", help="ignore the cached sitemap universe")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--append", action="store_true", help="append into the registry (vendor.yaml)")
    args = ap.parse_args()

    vendors = load_vendors()
    if args.list:
        for k, cfg in vendors.items():
            print(f"{k:16} enabled={str(cfg.get('enabled', True)):5} {cfg['mechanism']:10} "
                  f"{cfg['rights']['decision']:5} {cfg['name']}")
        return
    enabled = enabled_vendors(vendors)
    if args.vendor:
        if args.vendor not in vendors:
            ap.error(f"unknown vendor {args.vendor!r}")
        key = args.vendor
    elif args.cursor is not None:
        if not enabled:
            print("# no enabled vendors — nothing to do")
            return
        key = enabled[args.cursor % len(enabled)]
    else:
        ap.error("pass --vendor KEY or --cursor N")
    cfg = vendors[key]

    urls, titles, reg_ids = registry.existing_keys()
    try:
        universe = universe_for(key, cfg, refresh=args.refresh)
        docs = candidate_documents(key, cfg, universe, args.pages)
    except Exception as exc:
        print(f"# ERROR: {cfg['name']}: enumeration failed: {exc}", file=sys.stderr)
        raise SystemExit(1)  # abort WITHOUT advancing rotation
    out = entries_for(key, cfg, docs, urls, titles, args.max, known_titles_for(key))
    registry.uniquify_ids(out, reg_ids)

    by_topic: dict[str, int] = {}
    for e in out:
        by_topic[e["topic"]] = by_topic.get(e["topic"], 0) + 1
    print(f"# {len(out)} NEW {cfg['name']} documents (cursor->{key}; {len(docs)} candidate URLs in "
          f"a universe of {len(universe)}; deduped vs manifest + registry + blocklist)")
    print(f"# by topic: {by_topic}")
    if not args.append:
        for e in out:
            print(registry.emit_entry(e), end="")
        print("# --- review, then --append, then scripts/build_corpus.py ---")
        return
    if out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}")


if __name__ == "__main__":
    main()
