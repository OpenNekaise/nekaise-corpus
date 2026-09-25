#!/usr/bin/env python3
"""find_eurlex.py — EU building and sustainability-reporting law in every official language.

The compliance/ESG programme (Codex decision 2026-09-25) needs the EU acts behind building-code
compliance (EPBD, Construction Products Regulation, European Accessibility Act, Energy Efficiency
Directive, Taxonomy delegated acts, Ecodesign, RED) and behind ESG report extraction (CSRD, ESRS,
Omnibus I, SFDR, CSDDD) in all 24 EU languages. eur-lex.europa.eu answers every client with an AWS
WAF challenge (HTTP 202, empty body) and stays untouched (host_policy suspension). The same
documents are published by the Publications Office's Cellar, whose documented machine interface
is used here:

  1. SPARQL (publications.europa.eu/webapi/rdf/sparql) expands each seed CELEX from
     registry/eurlex.json by its amending acts, corrigenda and consolidated versions
     (cdm:resource_legal_amends_resource_legal / resource_legal_corrects_resource_legal /
     act_consolidated_consolidates_resource_legal);
  2. per work, SPARQL lists every language expression's manifestation ITEMS as Cellar returns
     them (…/cellar/<uuid>.<expr>.<manif>/DOC_n — never manufactured): XHTML preferred, else the
     PDF family; a manifestation with several items (annexes) yields one entry per item.

Rights: the EUR-Lex legal notice — "you can re-use the legal documents published in EUR-Lex for
commercial or non-commercial purposes" (Commission Decision 2011/833/EU) — so acts, proposals and
corrigenda are `open` with that evidence; consolidated texts (CELEX sector 0) are CC BY 4.0 per
the same notice (`cc-by`).

Identity: `eur-<celex>-<lang>[-p<n>]` (work + language + document part), titles carry CELEX,
language and part before truncation so translations never collapse under title dedup.

Rotation (dynamic cursor, JSON): {"s": seed index, "k": CELEX of the work in progress, "i": item
offset within it} | watch:<YYYY-MM-DD> after the last seed (re-walked at most every WATCH_DAYS days:
new amendments/consolidations enter through dedup). `--max` hard-caps proposed entries,
`--acts` the works visited and `--max-requests` the SPARQL requests of one run; a SPARQL failure
or deferral reports HOLD.

    python scripts/find_eurlex.py --celex 32024L1275 --max 30        # one act, propose only
    python scripts/find_eurlex.py --cursor START --acts 1 --max 24 --max-requests 6 --append
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import requests

import compliance_common
import dedup
import finder_protocol
import polite_http
import registry
import store

CONFIG_PATH = store.config_path("eurlex.json", registry.ROOT)
SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
DELAY = 2.0
WATCH_DAYS = 7
CELEX_RE = re.compile(r"[0-9CE][0-9A-Z()/.\-]{4,40}")
LANGS = {
    "BUL": "bg", "CES": "cs", "DAN": "da", "DEU": "de", "ELL": "el", "ENG": "en", "SPA": "es",
    "EST": "et", "FIN": "fi", "FRA": "fr", "GLE": "ga", "HRV": "hr", "HUN": "hu", "ITA": "it",
    "LIT": "lt", "LAV": "lv", "MLT": "mt", "NLD": "nl", "POL": "pl", "POR": "pt", "RON": "ro",
    "SLK": "sk", "SLV": "sl", "SWE": "sv",
}
HTML_TYPES = ("xhtml", "html")
PDF_TYPES = ("pdfa2a", "pdfa1a", "pdfa1b", "pdfa2b", "pdf", "pdfx")
RELATIONS = {
    "amends": "resource_legal_amends_resource_legal",
    "corrects": "resource_legal_corrects_resource_legal",
    "consolidates": "act_consolidated_consolidates_resource_legal",
}
LEGAL_NOTICE = "https://eur-lex.europa.eu/content/legal-notice/legal-notice.html"
EVIDENCE_OPEN = ("EUR-Lex legal notice: 'you can re-use the legal documents published in EUR-Lex "
                 "for commercial or non-commercial purposes' (Commission Decision 2011/833/EU on "
                 "the reuse of Commission documents); copy served by the Publications Office "
                 "Cellar item {item}")
EVIDENCE_CONSOLIDATED = ("EUR-Lex legal notice: consolidated texts are licensed under Creative "
                         "Commons Attribution 4.0 (CC BY 4.0); copy served by the Publications "
                         "Office Cellar item {item}")


# ------------------------------------------------------------------------------------ config
def load_config(path: Path | None = None) -> dict:
    data = json.loads(Path(path or CONFIG_PATH).read_text())
    errors = validate(data)
    if errors:
        raise ValueError("invalid eurlex.json: " + "; ".join(errors))
    return data


def validate(data: object) -> list[str]:
    if not isinstance(data, dict):
        return ["top level must be an object"]
    errors = []
    topics = set(__import__("lint_registry").TOPICS)
    seeds = data.get("seeds")
    if not isinstance(seeds, list) or not seeds:
        errors.append("seeds must be a non-empty list")
        seeds = []
    seen = set()
    for n, s in enumerate(seeds):
        if not isinstance(s, dict) or not CELEX_RE.fullmatch(str(s.get("celex", ""))):
            errors.append(f"seeds[{n}]: needs a CELEX number")
            continue
        if s["celex"] in seen:
            errors.append(f"seeds[{n}]: duplicate {s['celex']}")
        seen.add(s["celex"])
        if s.get("topic") not in topics:
            errors.append(f"seeds[{n}]: unknown topic {s.get('topic')!r}")
        if not s.get("name"):
            errors.append(f"seeds[{n}]: needs a name")
    langs = data.get("languages")
    if not isinstance(langs, list) or not set(langs) <= set(LANGS.values()) or not langs:
        errors.append("languages must be a non-empty list of EU ISO 639-1 codes")
    expand = data.get("expand", [])
    if not isinstance(expand, list) or not set(expand) <= set(RELATIONS):
        errors.append(f"expand must be a subset of {sorted(RELATIONS)}")
    if not data.get("rights_reviewed_at"):
        errors.append("rights_reviewed_at is required")
    return errors


# ------------------------------------------------------------------------------------ SPARQL
def _lit(celex: str) -> str:
    if not CELEX_RE.fullmatch(celex):
        raise ValueError(f"not a CELEX number: {celex!r}")
    return f'"{celex}"^^<http://www.w3.org/2001/XMLSchema#string>'


def sparql(query: str) -> list[dict]:
    resp = polite_http.get(SPARQL, delay=DELAY, expect="json", params={"query": query},
                           headers={"Accept": "application/sparql-results+json"})
    data = resp.json()
    return [{k: v.get("value") for k, v in b.items()}
            for b in data.get("results", {}).get("bindings", [])]


def related_query(celex: str, relations: list[str]) -> str:
    unions = " UNION ".join(f"{{ ?w cdm:{RELATIONS[r]} ?seed . BIND(\"{r}\" AS ?rel) }}"
                            for r in relations)
    return ("PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
            f"SELECT DISTINCT ?rel ?celex WHERE {{ ?seed cdm:resource_legal_id_celex {_lit(celex)} . "
            f"{unions} ?w cdm:resource_legal_id_celex ?celex . }}")


def items_query(celex: str) -> str:
    return ("PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
            "SELECT ?lang ?mtype ?item ?title ?mime WHERE { "
            f"?work cdm:resource_legal_id_celex {_lit(celex)} . "
            "?expr cdm:expression_belongs_to_work ?work ; cdm:expression_uses_language ?lang . "
            "OPTIONAL { ?expr cdm:expression_title ?title } "
            "?manif cdm:manifestation_manifests_expression ?expr ; cdm:manifestation_type ?mtype . "
            "?item cdm:item_belongs_to_manifestation ?manif . "
            "OPTIONAL { ?item <http://publications.europa.eu/ontology/cdm/cmr#manifestationMimeType>"
            " ?mime } }")


def works_for(seed: dict, relations: list[str], rows: list[dict]) -> list[tuple[str, str]]:
    """[(CELEX, relation)]: the seed first ("seed"), then its related works sorted by CELEX
    (deduplicated, seed excluded; a work related several ways keeps the first relation name in
    RELATIONS order)."""
    rel_of: dict[str, str] = {}
    for r in sorted(rows, key=lambda x: list(RELATIONS).index(x.get("rel"))
                    if x.get("rel") in RELATIONS else 99):
        c = r.get("celex")
        if c and CELEX_RE.fullmatch(c) and c != seed["celex"] and r.get("rel") in relations:
            rel_of.setdefault(c, r["rel"])
    return [(seed["celex"], "seed"), *sorted(rel_of.items())]


TEXT_MIMES = {"xhtml": ("application/xhtml+xml", "text/html"), "pdf": ("application/pdf",)}


def _document_item(row: dict) -> bool:
    """Whether a Cellar item is a DOCUMENT of its manifestation: an XHTML manifestation also
    carries its embedded images (and old special editions scanned pages) as items — their
    cmr:manifestationMimeType is image/*. Items without a declared mime type are kept (the loader
    still verifies the bytes)."""
    mime = (row.get("mime") or "").split(";")[0].strip().lower()
    if not mime:
        return True
    family = "xhtml" if row.get("mtype") in HTML_TYPES else "pdf"
    return mime in TEXT_MIMES[family]


def select_items(rows: list[dict], languages: list[str]) -> list[dict]:
    """Per language: XHTML document items when the expression has any, else the PDF-family
    ones (one manifestation type: the first available in PDF_TYPES order); image items are never
    documents. Sorted by (language, DOC number)."""
    by_lang: dict[str, dict[str, list[dict]]] = {}
    for r in rows:
        code = (r.get("lang") or "").rsplit("/", 1)[-1]
        lang = LANGS.get(code)
        if lang not in languages or not r.get("item") or not _document_item(r):
            continue
        by_lang.setdefault(lang, {}).setdefault(r.get("mtype", ""), []).append(r)
    out = []
    for lang in sorted(by_lang):
        types = by_lang[lang]
        chosen = next((t for t in (*HTML_TYPES, *PDF_TYPES) if t in types), None)
        if chosen is None:
            continue
        seen: dict[str, dict] = {}
        for r in types[chosen]:
            seen.setdefault(r["item"], r)
        for item in sorted(seen, key=_doc_number):
            out.append({"lang": lang, "mtype": chosen, "item": item, "doc": _doc_number(item),
                        "parts": len(seen), "title": seen[item].get("title") or ""})
    return out


def _doc_number(item: str) -> int:
    m = re.search(r"/DOC_(\d+)$", item)
    return int(m.group(1)) if m else 0


def note_missing_languages(celex: str, langs: list[str], items: list[dict], today: date) -> None:
    """Record, never paper over, languages Cellar has no expression for (coverage evidence in
    workspace/eurlex-missing-languages.jsonl; scratch)."""
    missing = sorted(set(langs) - {i["lang"] for i in items})
    if not missing:
        return
    print(f"# {celex}: no expression in {' '.join(missing)}", file=sys.stderr)
    try:
        import ops
        path = ops.WORKSPACE / "eurlex-missing-languages.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps({"celex": celex, "missing": missing,
                                 "checked": today.isoformat()}) + "\n")
    except OSError:
        pass


def items_fingerprint(items: list[dict]) -> str:
    import hashlib
    return hashlib.sha1("\n".join(i["item"] for i in items).encode()).hexdigest()[:10]


def https(url: str) -> str:
    return re.sub(r"^http://publications\.europa\.eu/", "https://publications.europa.eu/", url)


def entry_for(seed: dict, celex: str, it: dict, rights_date: str, rel: str = "seed") -> dict:
    """One candidate. Identity is work + language + Cellar document part: DOC_1 is the base id,
    DOC_<n> adds `-d<n>` — stable when parts are added later, independent of list position and
    of the preferred manifestation."""
    lang, doc = it["lang"], it["doc"]
    suffix = f" [{celex}, {lang}" + (f", DOC_{doc}" if it["parts"] > 1 or doc != 1 else "") + "]"
    base = re.sub(r"\s+", " ", it["title"]).strip() or seed["name"]
    room = 240 - len(suffix)
    title = (base[:room].rstrip() + suffix)
    consolidated = celex.startswith("0")
    url = https(it["item"])
    entry = {
        "id": f"eur-{registry.slug(celex)}-{lang}" + (f"-d{doc}" if doc != 1 else ""),
        "title": title, "url": url, "source": "eurlex",
        "license": "cc-by" if consolidated else "open",
        "topic": seed["topic"], "format": "html" if it["mtype"] in HTML_TYPES else "pdf",
        "language": lang, "jurisdiction": "EU",
        "document_type": ("consolidated-act" if consolidated else
                          "proposal" if celex.startswith("5") else
                          "corrigendum" if re.search(r"R\(\d+\)$", celex) else "legal-act"),
        "persistent_id": f"celex:{celex}",
        "resolution": f"cellar seed={seed['celex']} rel={rel} item=DOC_{doc}",
        "license_url": LEGAL_NOTICE,
        "license_evidence": (EVIDENCE_CONSOLIDATED if consolidated else EVIDENCE_OPEN).format(
            item=url),
        "rights_verified_at": rights_date,
    }
    return entry


# ------------------------------------------------------------------------------------ rotation
def parse_cursor(value: str) -> dict:
    if value in ("", "START"):
        return {"s": 0, "k": "", "i": 0, "f": ""}
    if value.startswith("watch:"):
        return {"watch": value.split(":", 1)[1]}
    data = json.loads(value)
    return {"s": int(data.get("s", 0)), "k": str(data.get("k", "")), "i": int(data.get("i", 0)),
            "f": str(data.get("f", ""))}


def dump_cursor(cur: dict) -> str:
    return json.dumps(cur, separators=(",", ":"), sort_keys=True)


class _Stop(Exception):
    """A per-run cap was reached; the cursor records where to resume."""


class Budget:
    def __init__(self, n: int):
        self.left = n

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def run(cursor: str, maxn: int, max_acts: int, max_requests: int, cfg: dict, keys, report,
        today: date | None = None, query=None) -> list[dict]:
    today = today or date.today()
    query = query or sparql
    cur = parse_cursor(cursor)
    if "watch" in cur:
        if today - date.fromisoformat(cur["watch"]) < timedelta(days=WATCH_DAYS):
            report.next(cursor)
            return []
        cur = parse_cursor("START")
    seeds, langs, relations = cfg["seeds"], cfg["languages"], cfg.get("expand", [])
    rights_date = cfg["rights_reviewed_at"]
    budget = Budget(max_requests)
    out: list[dict] = []
    acts = 0
    s, k, i, f = cur["s"], cur["k"], cur["i"], cur["f"]
    try:
        while s < len(seeds):
            seed = seeds[s]
            if not budget.take():
                break
            rows = query(related_query(seed["celex"], relations)) if relations else []
            works = works_for(seed, relations, rows)
            names = [c for c, _ in works]
            if k in names:
                w = names.index(k)
            else:
                w, i, f = 0, 0, ""
            while w < len(works):
                celex, rel = works[w]
                k = celex
                if acts >= max_acts or not budget.take():
                    raise _Stop
                acts += 1
                items = select_items(query(items_query(celex)), langs)
                note_missing_languages(celex, langs, items, today)
                fp = items_fingerprint(items)
                if f and f != fp:
                    i = 0  # the item list changed: restart this work (dedup makes it safe)
                f = fp
                cands = [entry_for(seed, celex, it, rights_date, rel) for it in items]
                keys.prefetch(urls=[c["url"] for c in cands[i:]], ids=[c["id"] for c in cands[i:]])
                while i < len(cands):
                    c = cands[i]
                    if c["url"] not in keys.urls and c["id"] not in keys.ids:
                        if len(out) >= maxn:
                            raise _Stop
                        keys.urls.add(c["url"])
                        keys.ids.add(c["id"])
                        out.append(c)
                    i += 1
                w, i, f = w + 1, 0, ""
                k = works[w][0] if w < len(works) else ""
            s, k, i, f = s + 1, "", 0, ""
    except _Stop:
        pass
    except (polite_http.Deferred, polite_http.Refused, polite_http.TooLarge,
            requests.RequestException, ValueError) as exc:
        if not out:
            report.hold(f"Cellar SPARQL: {exc}")
            return []
        print(f"# stopping early after {len(out)} candidates: {exc}", file=sys.stderr)
    if s >= len(seeds):
        report.next(f"watch:{today.isoformat()}")
    elif not out and (s, k, i, f) == (cur["s"], cur["k"], cur["i"], cur["f"]):
        report.hold("no progress possible this run (request/act budget)")
    else:
        report.next(dump_cursor({"s": s, "k": k, "i": i, "f": f}))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", default="START")
    ap.add_argument("--celex", default="", help="walk one CELEX (manual, propose-only pilots)")
    ap.add_argument("--acts", type=int, default=1, help="works visited this run")
    ap.add_argument("--max", type=int, default=24, help="hard cap on proposed entries")
    ap.add_argument("--max-requests", type=int, default=6, help="SPARQL request budget")
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args()
    cfg = compliance_common.pinned_config("eurlex.json", validate)  # the view's pinned copy
    report = finder_protocol.Report()
    if compliance_common.review_due(cfg.get("rights_reviewed_at")):
        report.hold(f"registry/eurlex.json rights review older than "
                    f"{compliance_common.RIGHTS_REVIEW_DAYS} days: re-review before collecting")
        return
    if args.celex:
        seed = next((s for s in cfg["seeds"] if s["celex"] == args.celex),
                    {"celex": args.celex, "name": args.celex, "topic": "standards_protocols"})
        cfg = {**cfg, "seeds": [seed]}
        args.cursor = "START"
    keys = dedup.open_keys()
    out = run(args.cursor, args.max, args.acts, args.max_requests, cfg, keys, report)
    ok, _held = compliance_common.split_appendable(out)
    langs = sorted({e["language"] for e in out})
    print(f"# {len(out)} NEW EU-law documents from Cellar ({len(langs)} languages: "
          f"{' '.join(langs)})")
    for e in out:
        print(f"#   {e['id']}  {e['format']}  {e['title'][:100]}")
    if args.append and ok:
        counts = registry.append_entries(ok)
        print(f"# appended {len(ok)} entries: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
