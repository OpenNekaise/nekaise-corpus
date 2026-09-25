#!/usr/bin/env python3
"""find_esef.py — annual reports (with their sustainability statements) of listed AEC issuers.

The CTI half of the compliance/ESG programme (Codex decision 2026-09-25): the raw material of ESG
extraction is companies' published annual/sustainability reports. EU-listed issuers file their
annual financial report in ESEF (Inline XBRL XHTML); XBRL International's filings.xbrl.org
indexes those filings with a documented JSON:API (filings.xbrl.org/docs/api) and serves each
report package unpacked. For FY2024 onwards (CSRD wave 1) the sustainability statement is part of
the same report; Swedish issuers have included the statutory hållbarhetsrapport since 2017.

Scope is a REVIEWED issuer list (registry/esef.json: LEI verified against GLEIF, name, country,
AEC sector, fiscal-year end). Per issuer the finder pages through /api/entities/<LEI>/filings
(following `links.next`, never guessing), resolves the relative `report_url`, and keeps a filing
as an ANNUAL report only when its period_end matches the issuer's configured fiscal-year end
(month-day) — the reviewed evidence; any other period (interim reports several Danish issuers
also file) is skipped, never guessed. Every distinct package (amendments, language versions) is
its own entry: id `esf-<lei>-<period>-<report file stem>`.

Rights: issuer copyright; filings.xbrl.org states that at present there are no restrictions on
the ways that the data can be used — repository availability is not an open licence, so rows are
tagged `proprietary` and HELD until the collect-all licence classes land (backend disabled).
Rows are large Inline-XBRL XHTML (5-40 MB); the dedicated ESEF extractor is a separate,
reviewed step before enabling.

Rotation (dynamic cursor, JSON): {"e": issuer index, "p": JSON:API page} | watch:<date>.
`--entities` issuers and `--pages` API pages per run, `--max` hard cap on proposed entries,
`--max-requests` HTTP budget; access deferral -> HOLD.

    python scripts/find_esef.py --issuer 549300UINV5RINHGMG07 --max 10     # one issuer, propose
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests

import compliance_common
import dedup
import finder_protocol
import polite_http
import registry
import store

CONFIG_PATH = store.config_path("esef.json", registry.ROOT)
BASE = "https://filings.xbrl.org"
DELAY = 2.0
WATCH_DAYS = 7
PAGE_SIZE = 100
LEI_RE = re.compile(r"[A-Z0-9]{18}[0-9]{2}")
SECTORS = {"construction", "real-estate", "building-materials", "building-services",
           "engineering", "building-products"}
ABOUT = "https://filings.xbrl.org/docs/about"


def load_config(path: Path | None = None) -> dict:
    data = json.loads(Path(path or CONFIG_PATH).read_text())
    errors = validate(data)
    if errors:
        raise ValueError("invalid esef.json: " + "; ".join(errors))
    return data


def validate(data: object) -> list[str]:
    if not isinstance(data, dict) or not isinstance(data.get("issuers"), list):
        return ["top level must be an object with an `issuers` list"]
    errors, seen = [], set()
    topics = set(__import__("lint_registry").TOPICS)
    for n, it in enumerate(data["issuers"]):
        label = f"issuers[{n}]"
        if not isinstance(it, dict) or not LEI_RE.fullmatch(str(it.get("lei", ""))):
            errors.append(f"{label}: needs a 20-character LEI")
            continue
        if it["lei"] in seen:
            errors.append(f"{label}: duplicate LEI {it['lei']}")
        seen.add(it["lei"])
        if not it.get("name") or not re.fullmatch(r"[A-Z]{2}", str(it.get("country", ""))):
            errors.append(f"{label}: needs name and ISO country")
        if it.get("sector") not in SECTORS:
            errors.append(f"{label}: sector must be one of {sorted(SECTORS)}")
        if it.get("topic") not in topics:
            errors.append(f"{label}: unknown topic {it.get('topic')!r}")
        if not re.fullmatch(r"\d{2}-\d{2}", str(it.get("fiscal_year_end", ""))):
            errors.append(f"{label}: fiscal_year_end must be MM-DD")
    if data.get("license") not in compliance_common.KNOWN_LICENSES:
        errors.append("license must be a known tag")
    if not data.get("rights_reviewed_at"):
        errors.append("rights_reviewed_at is required")
    return errors


def filings_page(url: str) -> dict:
    return polite_http.get(url, delay=DELAY, expect="json",
                           headers={"Accept": "application/vnd.api+json"}).json()


def first_page_url(lei: str) -> str:
    return f"{BASE}/api/entities/{lei}/filings?page%5Bsize%5D={PAGE_SIZE}"


def annual_entries(issuer: dict, filings: list[dict], cfg: dict) -> list[dict]:
    out = []
    for f in filings:
        a = f.get("attributes") or {}
        report, period = a.get("report_url"), str(a.get("period_end") or "")
        if not report or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", period):
            continue
        if period[5:] != issuer["fiscal_year_end"]:
            continue  # interim or unexplained period: never guessed to be annual
        url = urljoin(BASE + "/", report)
        stem = registry.slug(url.rsplit("/", 1)[-1].rsplit(".", 1)[0])
        lang_m = re.search(r"-(sv|en|da|fi|no|nb|de|fr|es|it|nl|pl|pt)(?:-|$)", stem)
        lang = {"no": "nb"}.get(lang_m.group(1), lang_m.group(1)) if lang_m else ""
        entry = {
            "id": f"esf-{issuer['lei'].lower()}-{period}-{stem}"[:90].rstrip("-"),
            "title": (f"{issuer['name']} annual report {period}" + (f" ({lang})" if lang else "")
                      + f" [ESEF {a.get('fxo_id') or stem}; {stem}]"),
            "url": url, "source": "esef", "license": cfg["license"], "topic": issuer["topic"],
            "format": "html", "jurisdiction": issuer["country"],
            "document_type": "annual-financial-report-esef",
            "published_at": str(a.get("date_added") or "")[:10] or None,
            "persistent_id": f"lei:{issuer['lei']}/{a.get('fxo_id') or stem}",
            "license_url": ABOUT,
            "license_evidence": cfg["license_evidence"],
            "rights_verified_at": cfg["rights_reviewed_at"],
        }
        if lang:
            entry["language"] = lang
        out.append({k: v for k, v in entry.items() if v})
    out.sort(key=lambda e: e["id"])
    return out


def parse_cursor(value: str) -> dict:
    if value in ("", "START"):
        return {"e": 0, "p": ""}
    if value.startswith("watch:"):
        return {"watch": value.split(":", 1)[1]}
    data = json.loads(value)
    return {"e": int(data.get("e", 0)), "p": str(data.get("p", ""))}


def run(cursor: str, maxn: int, entities: int, pages: int, max_requests: int, cfg: dict, keys,
        report, today: date | None = None, fetch=None) -> list[dict]:
    today = today or date.today()
    fetch = fetch or filings_page
    cur = parse_cursor(cursor)
    if "watch" in cur:
        if today - date.fromisoformat(cur["watch"]) < timedelta(days=WATCH_DAYS):
            report.next(cursor)
            return []
        cur = {"e": 0, "p": ""}
    issuers = cfg["issuers"]
    e, page_url = cur["e"], cur["p"]
    out: list[dict] = []
    visited = pages_done = requests_left = 0
    requests_left = max_requests
    try:
        while e < len(issuers) and visited < entities:
            issuer = issuers[e]
            url = page_url or first_page_url(issuer["lei"])
            while url:
                if pages_done >= pages or requests_left <= 0:
                    raise _Stop(url)
                requests_left -= 1
                pages_done += 1
                doc = fetch(url)
                cands = annual_entries(issuer, doc.get("data") or [], cfg)
                keys.prefetch(urls=[c["url"] for c in cands], ids=[c["id"] for c in cands])
                fresh = [c for c in cands if c["url"] not in keys.urls and c["id"] not in keys.ids]
                taken = fresh[:max(0, maxn - len(out))]
                for c in taken:
                    keys.urls.add(c["url"])
                    keys.ids.add(c["id"])
                    out.append(c)
                if len(taken) < len(fresh):
                    raise _Stop(url)  # --max is a hard cap: re-read this page next run
                nxt = (doc.get("links") or {}).get("next")
                url = urljoin(BASE + "/", nxt) if nxt else ""
            e, page_url, visited = e + 1, "", visited + 1
    except _Stop as stop:
        page_url = stop.args[0]
    except (polite_http.Deferred, polite_http.Refused, polite_http.TooLarge,
            requests.RequestException, ValueError) as exc:
        if not out:
            report.hold(f"filings.xbrl.org: {exc}")
            return []
        print(f"# stopping early after {len(out)} candidates: {exc}", file=sys.stderr)
    if e >= len(issuers):
        report.next(f"watch:{today.isoformat()}")
    elif not out and e == cur["e"] and page_url == cur["p"]:
        report.hold("no progress possible this run (page/request budget)")
    else:
        report.next(json.dumps({"e": e, "p": page_url}, separators=(",", ":"), sort_keys=True))
    return out


class _Stop(Exception):
    """A per-run cap was reached; args[0] is the page URL to resume at."""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", default="START")
    ap.add_argument("--issuer", default="", help="one LEI (manual, propose-only)")
    ap.add_argument("--entities", type=int, default=1)
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--max", type=int, default=4)
    ap.add_argument("--max-requests", type=int, default=4)
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    if args.issuer:
        cfg = {**cfg, "issuers": [i for i in cfg["issuers"] if i["lei"] == args.issuer]}
        if not cfg["issuers"]:
            raise SystemExit(f"LEI {args.issuer} is not a reviewed issuer in esef.json")
        args.cursor = "START"
    compliance_common.pin_host_policy()
    keys = dedup.open_keys()
    report = finder_protocol.Report()
    out = run(args.cursor, args.max, args.entities, args.pages, args.max_requests, cfg, keys,
              report)
    ok, held = compliance_common.split_appendable(out)
    print(f"# {len(out)} NEW ESEF annual reports ({len(ok)} appendable, {len(held)} held)")
    for x in out:
        print(f"#   {x['id']}  {x['title'][:100]}")
    if args.append and ok:
        counts = registry.append_entries(ok)
        print(f"# appended {len(ok)} entries: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
