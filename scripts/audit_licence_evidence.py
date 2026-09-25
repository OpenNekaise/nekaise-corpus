#!/usr/bin/env python3
"""audit_licence_evidence.py — pin authoritative licence evidence to arXiv / OpenAlex rows.

Debt this measures and (phase B) repairs: find_sources.py registered arXiv papers (`arx-`) and
OpenAlex works without a licence on their OA location (`ope-`, legacy `oa-`) as `license: open`
with no `license_evidence`, so they count as training-eligible. Project policy admits only
CC BY, CC BY-SA, CC0 and verified public domain into training. arXiv's default "non-exclusive
distribution licence" grants distribution to arXiv only (pointer-only for us) and NC/ND are
excluded, so part of these rows is not eligible.

Phase A (measure, read-only; everything lands in the git-ignored workspace/licence-audit/):

    python scripts/audit_licence_evidence.py snapshot --from /path/to/live/checkout
        git-archive the live checkout's committed tracked state (store.TRACKED_PATHS at its HEAD)
        into workspace/licence-audit/snapshots/<sha>/ — reading git objects only, so the live
        store's round lock is never taken (a FileStore read view holds the round lock for its
        whole lifetime; the continuous dig would be blocked or would block us)
    python scripts/audit_licence_evidence.py enumerate --root <snapshot>
        one store read view: the affected rows (targets.jsonl), counts by source and host, the
        same `open`-without-evidence debt in every other source, and the corpus totals
    python scripts/audit_licence_evidence.py arxiv --data-root /path/to/live/checkout [--sample 500]
        arXiv OAI-PMH GetRecord (arXivRaw) per paper: one connection, >= 3 s between requests,
        Retry-After honoured. The licence is pinned to the version actually fetched: the URL's
        version, else the arXiv stamp in the extracted text (`arXiv:<id>vN`, read-only from
        --data-root/text/), else the version dates against fetched_at. arXiv records carry the
        CURRENT licence only, so a fetched version that is not the latest stays `unresolved`.
    python scripts/audit_licence_evidence.py openalex
        OpenAlex re-query without searches: DOI and landing-page filter lists (50 values per
        request, $0.0001 each; singleton/list costs are read from the X-RateLimit headers and the
        pass stops above a reserve so the live finder's daily budget is never starved). The
        location matching the fetched URL decides. OpenAlex rows hosted on arxiv.org go through
        the arXiv pass instead (arXiv is authoritative, per version).
    python scripts/audit_licence_evidence.py report
        counts per verdict, the estimated doc/token impact on corpus/, 10 examples per verdict

Every pass is resumable: results.jsonl is append-only, keyed by id, and a row with a final
verdict is never asked again (transient failures are recorded as errors and retried).

Verdicts: eligible (CC BY / BY-SA / CC0 / public domain, pinned) · pointer-only (arXiv
non-exclusive, OA without an open licence) · excluded-nc-nd · unresolved (no pinnable evidence).

Phase B (prepared, NOT applied — the coordinator decides after review):

    python scripts/audit_licence_evidence.py apply [--apply]

Dry run by default. With --apply: bounded store transactions (store_broker.run_batch; under the
maintainer window's broker, else this command's own writer = the canonical round lock, held
throughout) that give every audited row license_evidence, license_url and rights_verified_at and
set its licence to the audited one — eligible rows to their CC tag, the rest to a
registry.RESTRICTED_USE_LICENSES tag, which makes them training-ineligible while raw/ and text/
provenance stay. Idempotent (an applied row is skipped; a row that changed since the audit is
skipped and reported); never deletes. Refuses above 1% of training-eligible docs or tokens
(AGENTS.md) unless --allow-over-1pct. Afterwards `python scripts/clean_corpus.py` clears the
corpus claims and quarantines the corpus copies of the excluded rows (never deletes them).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from contextlib import ExitStack
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote, urlparse

import licenses
import ops
import registry
import store
import store_broker
from store import And, Eq

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "workspace" / "licence-audit"
MAILTO = "corpus@opennekaise.org"   # the polite-pool contact find_sources.py already uses
USER_AGENT = f"nekaise-corpus licence audit (mailto:{MAILTO})"

ARXIV_OAI = "https://oaipmh.arxiv.org/oai"
ARXIV_MIN_INTERVAL = 3.1            # arXiv: one connection, >= 3 s between requests
OPENALEX_API = "https://api.openalex.org/works"
OPENALEX_MIN_INTERVAL = 1.0
OPENALEX_BATCH = 50                 # OR-ed filter values per list request
OPENALEX_RESERVE_USD = 0.03         # never spend the daily allowance below this (finder budget)
# A version submitted this close before fetched_at may not have been announced yet.
ANNOUNCE_MARGIN = dt.timedelta(days=4)
STAMP_WINDOW = 300_000              # chars of extracted text searched for the arXiv stamp

VERDICTS = ("eligible", "pointer-only", "excluded-nc-nd", "unresolved")
COHORTS = ("arxiv", "openalex", "openalex-arxiv")
TARGET_FIELDS = ("id", "url", "source", "license", "license_evidence", "status", "fetched_at",
                 "text_path", "text_chars", "corpus_chars", "topic")
# The licence a verdict writes (phase B). Eligible rows get their canonical CC tag.
UNRESOLVED_LICENSE = "unverified"
assert UNRESOLVED_LICENSE in registry.RESTRICTED_USE_LICENSES
MUTATED_FIELDS = ("license", "license_url", "license_evidence", "rights_verified_at")
APPLY_BATCH = 2000


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            return dt.datetime.strptime(value.strip(), fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


# --- files ---------------------------------------------------------------------------------------

def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:  # a torn last line from an interrupted append
                continue
    return out


def append_jsonl(path: Path, rows: Iterable[dict]) -> None:
    """One O_APPEND write per record, so concurrent passes (arXiv and OpenAlex) never tear a
    line into each other's."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        for r in rows:
            os.write(fd, (json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n").encode())
    finally:
        os.close(fd)


def latest_results(path: Path) -> dict[str, dict]:
    """id -> its latest result record (a final verdict is never superseded by an error)."""
    out: dict[str, dict] = {}
    for r in read_jsonl(path):
        prev = out.get(r["id"])
        if prev is not None and prev.get("verdict") and not r.get("verdict"):
            continue
        out[r["id"]] = r
    return out


# --- classification ------------------------------------------------------------------------------

_ARXIV_NONEXCLUSIVE = re.compile(
    r"^https?://arxiv\.org/licenses/(?:nonexclusive-distrib/1\.0|assumed-1991-2003)/?$", re.I)
_CC_PDD = re.compile(r"^https?://(?:www\.)?creativecommons\.org/licenses/publicdomain/?$", re.I)
_CC_RESTRICTED = re.compile(
    r"^https?://(?:www\.)?creativecommons\.org/licenses/(by-nc-nd|by-nc-sa|by-nc|by-nd)/\d\.\d", re.I)


def classify_licence_url(raw: str | None) -> tuple[str, str, str]:
    """(verdict, licence tag to record, reason) for a licence URL such as arXiv's <license>.
    CC grants are decided by licenses.cc_license (fail-closed); NC/ND are excluded."""
    value = (raw or "").strip()
    if not value:
        return ("pointer-only", "arxiv-nonexclusive",
                "no licence recorded: arXiv's default distribution licence applies")
    if _ARXIV_NONEXCLUSIVE.match(value):
        return ("pointer-only", "arxiv-nonexclusive",
                "arXiv non-exclusive distribution licence (grants distribution to arXiv only)")
    if m := _CC_RESTRICTED.match(value):
        return "excluded-nc-nd", f"cc-{m.group(1).lower()}", "NC/ND Creative Commons licence"
    if _CC_PDD.match(value):
        # The retired CC Public Domain Dedication: an explicit dedication by the submitter.
        return "eligible", "public-domain", "CC Public Domain Dedication (retired CC tool)"
    tag, _ = licenses.cc_license([value])
    if tag:
        return "eligible", tag, "canonical Creative Commons grant"
    return "unresolved", UNRESOLVED_LICENSE, f"unrecognised licence {value!r}"


_OA_ELIGIBLE = {"cc-by": "cc-by", "cc-by-sa": "cc-by-sa", "cc0": "cc0",
                "public-domain": "public-domain"}


def classify_openalex(raw: str | None) -> tuple[str, str, str]:
    """(verdict, licence tag, reason) for an OpenAlex location licence. OpenAlex reports short
    tags (cc-by, other-oa, publisher-specific-oa, ...); a URL-shaped value goes through
    classify_licence_url (licenses.cc_license)."""
    value = (raw or "").strip().lower()
    if not value:
        return "unresolved", UNRESOLVED_LICENSE, "OpenAlex location carries no licence"
    if value.startswith(("http://", "https://")):
        return classify_licence_url(raw)
    if re.search(r"(?:^|-)n[cd](?:-|$)", value):
        tag = value if value in registry.RESTRICTED_USE_LICENSES else UNRESOLVED_LICENSE
        return "excluded-nc-nd", tag, f"OpenAlex licence {value} (NC/ND)"
    if value in _OA_ELIGIBLE:
        return "eligible", _OA_ELIGIBLE[value], f"OpenAlex licence {value}"
    return ("pointer-only", "publisher-oa",
            f"OpenAlex licence {value}: open access without an open reuse licence")


# --- arXiv ---------------------------------------------------------------------------------------

_ARXIV_URL = re.compile(
    r"arxiv\.org/(?:pdf|abs)/(?P<id>[a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})"
    r"(?:v(?P<v>\d+))?(?:\.pdf)?/?$", re.I)


def arxiv_id(url: str) -> tuple[str | None, int | None]:
    """(arXiv id, explicit URL version or None) from an arxiv.org pdf/abs URL."""
    m = _ARXIV_URL.search((url or "").split("?")[0].split("#")[0])
    if not m:
        return None, None
    return m.group("id"), int(m.group("v")) if m.group("v") else None


def parse_arxiv_raw(xml: str) -> dict:
    """{'error': code} or {'license', 'versions': [(n, iso date)], 'datestamp'} from a GetRecord
    arXivRaw response."""
    if m := re.search(r"""<error\s+code=["']([^"']+)["']""", xml):
        return {"error": m.group(1)}
    if "<arXivRaw" not in xml:
        return {"error": "noArXivRaw"}
    versions = []
    for n, body in re.findall(r'<version\s+version="v(\d+)"\s*>(.*?)</version>', xml, re.S):
        date = re.search(r"<date>(.*?)</date>", body, re.S)
        when = parse_time(date.group(1)) if date else None
        versions.append((int(n), when.strftime("%Y-%m-%dT%H:%M:%SZ") if when else None))
    lic = re.search(r"<license>(.*?)</license>", xml, re.S)
    stamp = re.search(r"<datestamp>(.*?)</datestamp>", xml)
    return {"license": lic.group(1).strip() if lic else None, "versions": sorted(versions),
            "datestamp": stamp.group(1) if stamp else None}


def stamp_version(text: str, aid: str) -> int | None:
    """The version in the arXiv stamp of the fetched PDF ('arXiv:2209.13283v2 [cs.CV] ...')."""
    found = {int(v) for v in re.findall(r"arXiv:\s*" + re.escape(aid) + r"\s*v(\d+)",
                                        text[:STAMP_WINDOW])}
    return found.pop() if len(found) == 1 else None


def fetched_version(url_version: int | None, stamped: int | None, versions: list,
                    fetched_at: str | None) -> tuple[int | None, str]:
    """(version fetched, basis). Unversioned URLs serve the latest ANNOUNCED version; a version
    submitted within ANNOUNCE_MARGIN before fetched_at is ambiguous."""
    if url_version:
        return url_version, "url"
    if stamped:
        return stamped, "pdf-stamp"
    when = parse_time(fetched_at)
    if when is None or not versions:
        return None, "unknown"
    public, ambiguous = [], []
    for n, date in versions:
        d = parse_time(date)
        if d is None:
            ambiguous.append(n)
        elif d <= when - ANNOUNCE_MARGIN:
            public.append(n)
        elif d <= when:
            ambiguous.append(n)
    if ambiguous or not public:
        return None, "ambiguous-dates"
    return max(public), "dates"


def arxiv_verdict(target: dict, record: dict, text: str | None, checked_at: str) -> dict:
    """The result record for one arXiv target from its parsed OAI record."""
    aid, url_v = arxiv_id(target["url"])
    evidence_url = (f"{ARXIV_OAI}?verb=GetRecord&identifier=oai:arXiv.org:{aid}"
                    f"&metadataPrefix=arXivRaw")
    out = {"id": target["id"], "cohort": target["cohort"], "url": target["url"],
           "arxiv_id": aid, "evidence_source": "arxiv-oai-pmh:arXivRaw",
           "evidence_url": evidence_url, "checked_at": checked_at}
    if "error" in record:
        code = record["error"]
        out.update(verdict="unresolved", licence=UNRESOLVED_LICENSE, license_url=evidence_url,
                   raw_license=None, version=None, reason=f"OAI-PMH error {code}")
        return _with_evidence(out)
    versions = record["versions"]
    latest = versions[-1][0] if versions else None
    stamped = stamp_version(text, aid) if text else None
    version, basis = fetched_version(url_v, stamped, versions, target.get("fetched_at"))
    verdict, tag, reason = classify_licence_url(record["license"])
    out.update(raw_license=record["license"], versions=versions, latest_version=latest,
               version=f"v{version}" if version else None, version_basis=basis)
    if version is None:
        verdict, tag = "unresolved", UNRESOLVED_LICENSE
        reason = f"fetched version cannot be determined ({basis}); record licence: {reason}"
    elif latest is not None and version != latest:
        verdict, tag = "unresolved", UNRESOLVED_LICENSE
        reason = (f"fetched v{version} but the arXiv record's licence is v{latest}'s; "
                  f"record licence: {reason}")
    out.update(verdict=verdict, licence=tag, reason=reason,
               license_url=record["license"] or evidence_url)
    return _with_evidence(out)


def _with_evidence(out: dict) -> dict:
    """The human-readable license_evidence phase B records."""
    if out["evidence_source"].startswith("arxiv"):
        pin = f"{out.get('version') or 'version unknown'}"
        if out.get("version_basis"):
            pin += f" by {out['version_basis']}"
        out["evidence"] = (f"arXiv OAI-PMH arXivRaw oai:arXiv.org:{out['arxiv_id']} "
                           f"({pin}; latest v{out.get('latest_version')}): "
                           f"{out.get('raw_license') or 'no licence element'} -> "
                           f"{out['verdict']} ({out['reason']})")
    else:
        out["evidence"] = (f"OpenAlex {out.get('work_id') or 'work not found'} location "
                           f"{out.get('matched_url') or '-'}: licence "
                           f"{out.get('raw_license') or 'none'} -> {out['verdict']} "
                           f"({out['reason']})")
    return out


class Throttle:
    """Sequential requests over ONE session, at least `interval` seconds apart."""

    def __init__(self, interval: float, session=None, sleep=time.sleep, clock=time.monotonic):
        import requests

        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.interval, self.sleep, self.clock = interval, sleep, clock
        self.last = None

    def get(self, url: str, **kw):
        if self.last is not None and (wait := self.interval - (self.clock() - self.last)) > 0:
            self.sleep(wait)
        try:
            return self.session.get(url, timeout=60, **kw)
        finally:
            self.last = self.clock()


def fetch_oai(http: Throttle, aid: str, *, retries: int = 5, log=print) -> str:
    params = {"verb": "GetRecord", "identifier": f"oai:arXiv.org:{aid}",
              "metadataPrefix": "arXivRaw"}
    for attempt in range(retries):
        r = http.get(ARXIV_OAI, params=params)
        if r.status_code in (429, 503):
            wait = float(r.headers.get("Retry-After") or 30 * (attempt + 1))
            log(f"# arXiv {r.status_code}: Retry-After {wait:.0f}s")
            http.sleep(min(wait, 600))
            continue
        r.raise_for_status()
        return r.text
    raise RuntimeError(f"arXiv OAI kept refusing ({retries} attempts)")


def read_text(data_root: Path | None, text_path: str | None) -> str | None:
    """The extracted text of a row (read-only), confined to data_root."""
    if data_root is None or not text_path:
        return None
    path = (data_root / text_path).resolve()
    if not path.is_relative_to(data_root.resolve()) or not path.is_file():
        return None
    with path.open(errors="replace") as f:
        return f.read(STAMP_WINDOW)


def stratified_sample(targets: list[dict], n: int, seed: int) -> list[dict]:
    """A proportional random sample stratified by cohort and arXiv year (fixed seed)."""
    def stratum(t):
        aid, _ = arxiv_id(t["url"])
        if aid and re.match(r"\d{4}\.", aid):
            year = 2000 + int(aid[:2])
        elif aid and (m := re.search(r"/(\d{2})", aid)):
            year = (1900 if int(m.group(1)) > 90 else 2000) + int(m.group(1))
        else:
            year = 0
        bucket = "<2015" if year < 2015 else str(min(year, 2026) // 2 * 2)
        return t["cohort"], bucket

    groups: dict[tuple, list] = defaultdict(list)
    for t in sorted(targets, key=lambda t: t["id"]):
        groups[stratum(t)].append(t)
    rng = random.Random(seed)
    total = len(targets)
    picked = []
    for key in sorted(groups):
        rows = groups[key]
        k = min(len(rows), max(1, round(n * len(rows) / total))) if total else 0
        picked.extend(rng.sample(rows, k))
    rng.shuffle(picked)
    return picked[:n] if len(picked) > n else picked


def run_arxiv(out_dir: Path, *, data_root: Path | None, sample: int | None, seed: int,
              limit: int | None, http: Throttle | None = None, log=print) -> int:
    targets = [t for t in read_jsonl(out_dir / "targets.jsonl")
               if t["cohort"] in ("arxiv", "openalex-arxiv")]
    results_path = out_dir / "results.jsonl"
    done = {sid for sid, r in latest_results(results_path).items() if r.get("verdict")}
    todo = [t for t in targets if t["id"] not in done]
    if sample:
        picked = stratified_sample(targets, sample, seed)
        todo = [t for t in picked if t["id"] not in done]
        log(f"# stratified sample of {len(picked)} (seed {seed}): {len(todo)} still to audit")
    else:
        todo.sort(key=lambda t: t["id"])
    if limit:
        todo = todo[:limit]
    log(f"# arXiv: {len(todo)} to audit, {len(done)} already final; "
        f"~{len(todo) * ARXIV_MIN_INTERVAL / 3600:.1f} h at {ARXIV_MIN_INTERVAL}s/request")
    http = http or Throttle(ARXIV_MIN_INTERVAL)
    raw_dir = out_dir / "oai"
    raw_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for t in todo:
        aid, _ = arxiv_id(t["url"])
        if aid is None:
            append_jsonl(results_path, [_with_evidence({
                "id": t["id"], "cohort": t["cohort"], "url": t["url"], "arxiv_id": None,
                "evidence_source": "arxiv-oai-pmh:arXivRaw", "evidence_url": t["url"],
                "checked_at": now_iso(), "verdict": "unresolved", "licence": UNRESOLVED_LICENSE,
                "license_url": t["url"], "raw_license": None, "version": None,
                "reason": "URL carries no parsable arXiv id"})])
            continue
        try:
            xml = fetch_oai(http, aid, log=log)
        except Exception as exc:  # network trouble: record, retry on the next run
            append_jsonl(results_path, [{"id": t["id"], "cohort": t["cohort"], "url": t["url"],
                                         "checked_at": now_iso(), "verdict": None,
                                         "error": f"{type(exc).__name__}: {exc}"[:300]}])
            log(f"# {t['id']}: {exc}")
            continue
        checked = now_iso()
        (raw_dir / (aid.replace("/", "_") + ".xml")).write_text(xml)
        record = parse_arxiv_raw(xml)
        res = arxiv_verdict(t, record, read_text(data_root, t.get("text_path")), checked)
        append_jsonl(results_path, [res])
        n += 1
        if n % 50 == 0:
            log(f"# arXiv: {n}/{len(todo)} audited")
    log(f"# arXiv: {n} audited this run")
    return 0


# --- OpenAlex ------------------------------------------------------------------------------------

_DOI = re.compile(r"(10\.\d{4,9}/[^\s?#&\"<>|,]+)", re.I)


def doi_from_url(url: str) -> str | None:
    """A DOI carried by a publisher PDF URL (Frontiers, PLOS, BMC, Springer, Nature, doi.org)."""
    u = unquote(url or "")
    if m := re.search(r"nature\.com/articles/(s\d{5}-\d{3}-\d{5}-\w)", u):
        return f"10.1038/{m.group(1)}".lower()
    if not (m := _DOI.search(u)):
        return None
    doi = m.group(1)
    for suffix in ("/pdf", "/full", ".pdf", "/"):
        if doi.lower().endswith(suffix):
            doi = doi[: -len(suffix)]
    return doi.lower()


def landing_candidates(url: str) -> list[str]:
    """OpenAlex landing_page_url values that may belong to the work behind a PDF URL."""
    out = []
    if m := re.search(r"escholarship\.org/(?:dist/prd/)?content/qt(\w+)/", url):
        out.append(f"https://escholarship.org/uc/item/{m.group(1)}")
    if m := re.search(r"osti\.gov/servlets/purl/(\d+)", url):
        out.append(f"https://www.osti.gov/biblio/{m.group(1)}")
    if m := re.search(r"ntrs\.nasa\.gov/api/citations/(\d+)/", url):
        out.append(f"https://ntrs.nasa.gov/citations/{m.group(1)}")
    out.append(url)
    return [u for u in dict.fromkeys(out) if "," not in u and "|" not in u]


def norm_loc(url: str | None) -> str:
    if not url:
        return ""
    p = urlparse(url.strip())
    host = p.netloc.lower().removeprefix("www.")
    return f"{host}{p.path.rstrip('/')}" + (f"?{p.query}" if p.query else "")


def _loc_matches(url: str, loc: dict) -> bool:
    want = {norm_loc(url), norm_loc(url).split("?")[0]}
    landings = {norm_loc(u) for u in landing_candidates(url)}
    got_pdf = norm_loc(loc.get("pdf_url"))
    return bool((got_pdf and (got_pdf in want or got_pdf.split("?")[0] in want))
                or norm_loc(loc.get("landing_page_url")) in landings)


def openalex_verdict(target: dict, work: dict | None, checked_at: str, evidence_url: str) -> dict:
    out = {"id": target["id"], "cohort": target["cohort"], "url": target["url"],
           "evidence_source": "openalex:works", "evidence_url": evidence_url,
           "checked_at": checked_at, "version": None,
           "work_id": (work or {}).get("id")}
    if work is None:
        out.update(verdict="unresolved", licence=UNRESOLVED_LICENSE, raw_license=None,
                   license_url=evidence_url,
                   reason="no OpenAlex work found by DOI or landing page (no search spent)")
        return _with_evidence(out)
    locs = [loc for loc in [work.get("best_oa_location"), work.get("primary_location"),
                            *(work.get("locations") or [])] if loc]
    matched, seen = [], set()
    for loc in locs:
        key = (loc.get("pdf_url"), loc.get("landing_page_url"), loc.get("license"))
        if key not in seen and _loc_matches(target["url"], loc):
            seen.add(key)
            matched.append(loc)
    if not matched:
        lics = sorted({str(loc.get("license")) for loc in locs})
        out.update(verdict="unresolved", licence=UNRESOLVED_LICENSE, raw_license=None,
                   license_url=evidence_url,
                   reason=f"no OpenAlex location matches the fetched URL (work licences {lics})")
        return _with_evidence(out)
    decisions = {classify_openalex(loc.get("license")) for loc in matched}
    verdicts = {d[0] for d in decisions}
    if "excluded-nc-nd" in verdicts:
        verdict, tag, reason = next(d for d in sorted(decisions) if d[0] == "excluded-nc-nd")
    elif len(decisions) > 1:
        verdict, tag = "unresolved", UNRESOLVED_LICENSE
        reason = f"matching locations disagree: {sorted(d[2] for d in decisions)}"
    else:
        verdict, tag, reason = decisions.pop()
    loc = matched[0]
    out.update(verdict=verdict, licence=tag, reason=reason, raw_license=loc.get("license"),
               matched_url=loc.get("pdf_url") or loc.get("landing_page_url"),
               license_url=loc.get("license_id") or evidence_url)
    return _with_evidence(out)


class OpenAlexBudget(RuntimeError):
    pass


def openalex_list(http: Throttle, filter_name: str, values: list[str], *,
                  reserve: float, log=print) -> tuple[list[dict], str]:
    params = {"filter": f"{filter_name}:" + "|".join(values), "per-page": 200,
              "select": "id,doi,locations,best_oa_location,primary_location", "mailto": MAILTO}
    r = http.get(OPENALEX_API, params=params)
    if r.status_code == 429:
        raise OpenAlexBudget(f"OpenAlex 429 (Retry-After {r.headers.get('Retry-After')})")
    r.raise_for_status()
    remaining = r.headers.get("X-RateLimit-Remaining-USD") or r.headers.get(
        "x-ratelimit-remaining-usd")
    if remaining is not None and float(remaining) < reserve:
        raise OpenAlexBudget(f"OpenAlex daily allowance down to ${float(remaining):.4f} "
                             f"(< reserve ${reserve}); stopping so the finder keeps its budget")
    return r.json().get("results", []), r.url


def run_openalex(out_dir: Path, *, reserve: float = OPENALEX_RESERVE_USD,
                 http: Throttle | None = None, log=print) -> int:
    targets = [t for t in read_jsonl(out_dir / "targets.jsonl") if t["cohort"] == "openalex"]
    results_path = out_dir / "results.jsonl"
    done = {sid for sid, r in latest_results(results_path).items() if r.get("verdict")}
    todo = [t for t in targets if t["id"] not in done]
    log(f"# OpenAlex: {len(todo)} to audit, {len(done & {t['id'] for t in targets})} final")
    http = http or Throttle(OPENALEX_MIN_INTERVAL)
    works: dict[str, dict] = {}        # doi / landing url -> work
    evidence: dict[str, str] = {}      # key -> request url
    dois = {t["id"]: doi_from_url(t["url"]) for t in todo}
    try:
        wanted = sorted({d for d in dois.values() if d})
        for i in range(0, len(wanted), OPENALEX_BATCH):
            results, url = openalex_list(http, "doi", wanted[i:i + OPENALEX_BATCH],
                                         reserve=reserve, log=log)
            for w in results:
                if w.get("doi"):
                    key = w["doi"].lower().removeprefix("https://doi.org/")
                    works[key], evidence[key] = w, url
        landing = sorted({u for t in todo if not works.get(dois[t["id"]] or "")
                          for u in landing_candidates(t["url"])})
        for i in range(0, len(landing), OPENALEX_BATCH):
            chunk = landing[i:i + OPENALEX_BATCH]
            results, url = openalex_list(http, "locations.landing_page_url", chunk,
                                         reserve=reserve, log=log)
            wanted_norm = {norm_loc(u): u for u in chunk}
            for w in results:
                for loc in w.get("locations") or []:
                    if (u := wanted_norm.get(norm_loc(loc.get("landing_page_url")))):
                        works[u], evidence[u] = w, url
    except OpenAlexBudget as exc:
        log(f"# {exc}")
        return 2
    checked = now_iso()
    rows = []
    for t in todo:
        keys = [k for k in [dois[t["id"]], *landing_candidates(t["url"])] if k]
        key = next((k for k in keys if k in works), None)
        rows.append(openalex_verdict(t, works.get(key) if key else None, checked,
                                     evidence.get(key) or OPENALEX_API))
    append_jsonl(results_path, rows)
    log(f"# OpenAlex: {len(rows)} audited, {sum(1 for r in rows if r['work_id'])} works found")
    return 0


# --- enumerate (store read view) ------------------------------------------------------------------

def host(url: str | None) -> str:
    return urlparse(url or "").netloc.lower()


def cohort(row: dict) -> str | None:
    sid, src = row.get("id", ""), row.get("source")
    if sid.startswith("arx-") and src == "arxiv":
        return "arxiv"
    if sid.startswith(("ope-", "oa-")) and src == "openalex":
        return "openalex-arxiv" if host(row.get("url")).endswith("arxiv.org") else "openalex"
    return None


def tokens(row: dict) -> int:
    chars = row.get("corpus_chars", row.get("text_chars")) or 0
    return int(chars) // 4


def enumerate_targets(view, *, log=print) -> tuple[list[dict], dict]:
    """(targets, facts): every successful, training-eligible `license: open` row without
    license_evidence in the arXiv/OpenAlex cohorts, and the debt across all sources."""
    import corpus_stats

    restrictions, _ = store.pinned_policy(view)
    stats = corpus_stats.compute(view, restrictions)
    by_source: Counter = Counter()
    by_source_tokens: Counter = Counter()
    with_evidence: Counter = Counter()
    by_host: dict[str, Counter] = defaultdict(Counter)
    targets = []
    where = And(Eq("status", "ok"), Eq("license", "open"))
    for r in corpus_stats.iter_manifest(view, where=where, fields=TARGET_FIELDS):
        if not registry.is_training_eligible(r, restrictions):
            continue
        if r.get("license_evidence"):
            with_evidence[r.get("source")] += 1
            continue
        by_source[r.get("source")] += 1
        by_source_tokens[r.get("source")] += tokens(r)
        c = cohort(r)
        if c is None:
            continue
        by_host[c][host(r.get("url"))] += 1
        targets.append({**{k: r.get(k) for k in TARGET_FIELDS if k in r}, "cohort": c})
    facts = {
        "store_version": view.version().token,
        "enumerated_at": now_iso(),
        "corpus": {"documents": stats.documents, "tokens": stats.corpus_tokens,
                   "text_tokens": stats.tokens},
        "open_without_evidence_by_source": dict(by_source.most_common()),
        "open_without_evidence_tokens_by_source": dict(by_source_tokens.most_common()),
        "open_with_evidence_by_source": dict(with_evidence.most_common()),
        "targets_by_cohort": dict(Counter(t["cohort"] for t in targets)),
        "targets_by_host": {c: dict(h.most_common()) for c, h in by_host.items()},
    }
    return sorted(targets, key=lambda t: t["id"]), facts


def run_enumerate(root: Path, out_dir: Path, *, timeout: float, log=print) -> int:
    st = store.open(root=root)
    with st.read(timeout=timeout) as view:
        targets, facts = enumerate_targets(view, log=log)
    facts["root"] = str(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    ops.atomic_write_text(out_dir / "targets.jsonl", "".join(
        json.dumps(t, ensure_ascii=False, sort_keys=True) + "\n" for t in targets))
    ops.atomic_write_text(out_dir / "enumerate.json", json.dumps(facts, indent=2) + "\n")
    log(json.dumps(facts, indent=2))
    return 0


def run_snapshot(source: Path, out_dir: Path, *, rev: str = "HEAD", log=print) -> Path:
    """Extract `source`'s committed tracked state at `rev` from git objects (read-only on the
    source: no lock, no working-tree access) into out_dir/snapshots/<sha>/."""
    sha = subprocess.run(["git", "-C", str(source), "rev-parse", rev], check=True,
                         capture_output=True, text=True).stdout.strip()
    dest = out_dir / "snapshots" / sha
    if (dest / ".complete").exists():
        log(str(dest))
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.Popen(["git", "-C", str(source), "archive", sha, *store.TRACKED_PATHS],
                               stdout=subprocess.PIPE)
    subprocess.run(["tar", "-x", "-C", str(dest)], stdin=archive.stdout, check=True)
    if archive.wait() != 0:
        raise RuntimeError("git archive failed")
    (dest / ".complete").write_text(sha + "\n")
    log(str(dest))
    return dest


# --- report --------------------------------------------------------------------------------------

def build_report(targets: list[dict], results: dict[str, dict], facts: dict) -> dict:
    by_id = {t["id"]: t for t in targets}
    audited = {sid: r for sid, r in results.items() if r.get("verdict") and sid in by_id}
    report: dict = {"generated_at": now_iso(), "targets": len(targets),
                    "audited": len(audited), "cohorts": {}}
    corpus_tokens = (facts.get("corpus") or {}).get("tokens") or 0
    corpus_docs = (facts.get("corpus") or {}).get("documents") or 0
    for c in COHORTS:
        pop = [t for t in targets if t["cohort"] == c]
        done = [audited[t["id"]] for t in pop if t["id"] in audited]
        if not pop:
            continue
        counts = Counter(r["verdict"] for r in done)
        tok = Counter()
        for r in done:
            tok[r["verdict"]] += tokens(by_id[r["id"]])
        pop_tokens = sum(tokens(t) for t in pop)
        frac = len(done) / len(pop)
        est_docs = {v: round(counts[v] / frac) if frac else None for v in VERDICTS}
        est_tokens = {v: round(tok[v] / frac) if frac else None for v in VERDICTS}
        report["cohorts"][c] = {
            "population": len(pop), "population_tokens": pop_tokens, "audited": len(done),
            "counts": {v: counts[v] for v in VERDICTS},
            "audited_tokens": {v: tok[v] for v in VERDICTS},
            "estimated_population_docs": est_docs,
            "estimated_population_tokens": est_tokens,
            "unresolved_reasons": dict(Counter(
                re.sub(r"\(.*|'.*|v\d+", "", r["reason"]).strip()[:80]
                for r in done if r["verdict"] == "unresolved").most_common(8)),
            "raw_licences": dict(Counter(str(r.get("raw_license")) for r in done).most_common()),
        }
    lost_docs = sum((c["estimated_population_docs"][v] or 0)
                    for c in report["cohorts"].values() for v in VERDICTS if v != "eligible")
    lost_tokens = sum((c["estimated_population_tokens"][v] or 0)
                      for c in report["cohorts"].values() for v in VERDICTS if v != "eligible")
    report["impact"] = {
        "estimated_ineligible_docs": lost_docs, "estimated_ineligible_tokens": lost_tokens,
        "corpus_documents": corpus_docs, "corpus_tokens": corpus_tokens,
        "fraction_docs": lost_docs / corpus_docs if corpus_docs else None,
        "fraction_tokens": lost_tokens / corpus_tokens if corpus_tokens else None,
    }
    rng = random.Random(0)
    examples = {}
    for v in VERDICTS:
        pool = sorted((r for r in audited.values() if r["verdict"] == v), key=lambda r: r["id"])
        examples[v] = [{k: r.get(k) for k in ("id", "cohort", "url", "version", "raw_license",
                                               "licence", "reason")}
                       for r in rng.sample(pool, min(10, len(pool)))]
    report["examples"] = examples
    return report


def run_report(out_dir: Path, *, log=print) -> int:
    targets = read_jsonl(out_dir / "targets.jsonl")
    facts_path = out_dir / "enumerate.json"
    facts = json.loads(facts_path.read_text()) if facts_path.exists() else {}
    report = build_report(targets, latest_results(out_dir / "results.jsonl"), facts)
    ops.atomic_write_text(out_dir / "report.json", json.dumps(report, indent=2) + "\n")
    log(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


# --- phase B: apply ------------------------------------------------------------------------------

def patch_for(result: dict) -> dict:
    """The fields phase B writes for one final audit result."""
    return {"license": result["licence"], "license_url": result["license_url"],
            "license_evidence": result["evidence"],
            "rights_verified_at": result["checked_at"][:10]}


def plan_row(result: dict, entry: dict | None, row: dict | None) -> tuple[str, dict | None]:
    """('apply' | 'applied' | skip reason, patch) for one audited row — idempotent and guarded:
    only the audited state (licence `open`, no evidence, same URL) is ever changed."""
    patch = patch_for(result)
    if entry is None or row is None:
        return "skip: row or entry no longer exists", None
    if all(entry.get(k) == v for k, v in patch.items()) and all(
            row.get(k) == v for k, v in patch.items()):
        return "applied", None
    if row.get("url") != result["url"] or entry.get("url") != result["url"]:
        return "skip: URL changed since the audit", None
    for rec in (entry, row):
        if rec.get("license") != "open" or rec.get("license_evidence"):
            return "skip: licence or evidence changed since the audit", None
    if patch["license"] not in registry.RESTRICTED_USE_LICENSES | {"cc-by", "cc-by-sa", "cc0",
                                                               "public-domain"}:
        return f"skip: unexpected licence {patch['license']!r}", None
    return "apply", patch


def final_results(out_dir: Path) -> dict[str, dict]:
    return {sid: r for sid, r in latest_results(out_dir / "results.jsonl").items()
            if r.get("verdict") in VERDICTS}


def run_apply(root: Path, out_dir: Path, *, apply: bool, allow_large: bool = False,
              batch_size: int = APPLY_BATCH, timeout: float = 60, log=print, st=None) -> int:
    """Phase B. `st`: the store (default: the one authoritative for `root`)."""
    import corpus_stats

    results = final_results(out_dir)
    if not results:
        log("no final audit results")
        return 1
    st = st if st is not None else store.open(root=root)
    with ExitStack() as stack:
        # Under the round lock throughout: a maintenance window's child writes through its
        # parent's broker; a standalone run holds its own writer for every batch.
        writer = (None if store_broker.client() is not None
                  else stack.enter_context(st.writer(timeout=timeout)))
        with (st.read(writer=writer) if writer is not None else st.read()) as view:
            restrictions, _ = store.pinned_policy(view)
            stats = corpus_stats.compute(view, restrictions)
            rows = view.get_manifest(results)
            entries = view.get_entries(results)
        plan: Counter = Counter()
        lose_docs = lose_tokens = 0
        for sid, res in results.items():
            action, patch = plan_row(res, entries.get(sid), rows.get(sid))
            plan[action] += 1
            if action == "apply" and patch["license"] in registry.RESTRICTED_USE_LICENSES \
                    and registry.is_training_eligible(rows[sid], restrictions):
                lose_docs += 1
                lose_tokens += tokens(rows[sid])
        frac_docs = lose_docs / stats.documents if stats.documents else 0.0
        frac_tokens = lose_tokens / stats.corpus_tokens if stats.corpus_tokens else 0.0
        verdicts = Counter(r["verdict"] for r in results.values())
        log(f"audit results: {dict(verdicts)}")
        log(f"plan: {dict(plan)}")
        log(f"training-eligible loss: {lose_docs:,} docs ({frac_docs:.3%}), "
            f"{lose_tokens:,} tokens ({frac_tokens:.3%}) of {stats.documents:,} docs / "
            f"{stats.corpus_tokens:,} tokens")
        if max(frac_docs, frac_tokens) > 0.01 and not allow_large:
            log("REFUSED: removes more than 1% of training-eligible docs or tokens "
                "(AGENTS.md); record a proposal, or pass --allow-over-1pct on an operator "
                "decision")
            return 1
        if not apply:
            log("dry run -- pass --apply to write")
            return 0
        todo = [sid for sid, res in results.items()
                if plan_row(res, entries.get(sid), rows.get(sid))[0] == "apply"]
        written = 0
        for n, chunk in enumerate(store_broker.shard_batches(todo, batch_size), 1):
            def body(view, batch, chunk=chunk):
                cur_rows = view.get_manifest(chunk)
                cur_entries = view.get_entries(chunk)
                patches, new_entries = {}, []
                for sid in chunk:
                    action, patch = plan_row(results[sid], cur_entries.get(sid),
                                             cur_rows.get(sid))
                    if action == "apply":
                        patches[sid] = patch
                        new_entries.append({**cur_entries[sid], **patch})
                if patches:
                    batch.update_manifest_fields(patches)
                    batch.upsert_entries(new_entries)
                return len(patches)

            count, _ = store_broker.run_batch(st, "licence-audit", body, writer=writer)
            written += count
            log(f"batch {n}: {count} rows")
        log(f"applied licence evidence to {written} rows; next: python scripts/clean_corpus.py "
            "(quarantines the excluded rows' corpus copies), then the read-only gates")
        return 0


# --- CLI -----------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="audit directory (git-ignored)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("snapshot", help="extract a live checkout's committed tracked state")
    p.add_argument("--from", dest="source", type=Path, required=True)
    p.add_argument("--rev", default="HEAD")
    p = sub.add_parser("enumerate", help="list affected rows through a store read view")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--lock-timeout", type=float, default=60)
    p = sub.add_parser("arxiv", help="audit arXiv rows via OAI-PMH")
    p.add_argument("--data-root", type=Path, default=None,
                   help="checkout whose text/ holds the extracted PDFs (read-only; version stamp)")
    p.add_argument("--sample", type=int, default=None)
    p.add_argument("--seed", type=int, default=20260925)
    p.add_argument("--limit", type=int, default=None)
    p = sub.add_parser("openalex", help="audit OpenAlex rows")
    p.add_argument("--reserve-usd", type=float, default=OPENALEX_RESERVE_USD)
    sub.add_parser("report", help="summarise results")
    p = sub.add_parser("apply", help="phase B: write the verdicts (dry run by default)")
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--allow-over-1pct", action="store_true")
    p.add_argument("--batch", type=int, default=APPLY_BATCH)
    p.add_argument("--lock-timeout", type=float, default=60)
    args = ap.parse_args(argv)
    out = args.out
    if args.cmd == "snapshot":
        run_snapshot(args.source, out, rev=args.rev)
        return 0
    if args.cmd == "enumerate":
        return run_enumerate(args.root, out, timeout=args.lock_timeout)
    if args.cmd == "arxiv":
        return run_arxiv(out, data_root=args.data_root, sample=args.sample, seed=args.seed,
                         limit=args.limit)
    if args.cmd == "openalex":
        return run_openalex(out, reserve=args.reserve_usd)
    if args.cmd == "report":
        return run_report(out)
    if args.cmd == "apply":
        return run_apply(args.root, out, apply=args.apply, allow_large=args.allow_over_1pct,
                         batch_size=args.batch, timeout=args.lock_timeout)
    return 2


if __name__ == "__main__":
    sys.exit(main())
