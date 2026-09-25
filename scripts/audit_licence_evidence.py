#!/usr/bin/env python3
"""audit_licence_evidence.py — pin authoritative licence evidence to arXiv / OpenAlex rows, and
reclassify them (collect-all directive 2026-09-25: a licence classifies, it never removes bytes).

Debt this measures and (phase B) repairs: find_sources.py registered arXiv papers (`arx-`, and
the hand-curated `arxiv-` ids) and OpenAlex works without an open licence on their OA location
(`ope-`, legacy `oa-`) as `license: open` with no (or only discovery-time) evidence, so they sit
in the default corpus/ view. That view admits only CC BY, CC BY-SA, CC0, verified public domain
and the project's existing `open` sources. arXiv's default "non-exclusive distribution licence"
grants distribution to arXiv only, NC/ND and unverified rights belong to classified views.

Phase A (measure, read-only; everything lands in the git-ignored workspace/licence-audit/):

    python scripts/audit_licence_evidence.py snapshot --from <checkout> [--rev main]
        git-archive the committed tracked state (store.TRACKED_PATHS) into
        workspace/licence-audit/snapshots/<sha>/ — git objects only, so the live store's round
        lock is never taken (a FileStore read view holds the round lock for its whole lifetime)
    python scripts/audit_licence_evidence.py enumerate --root <snapshot>
        one store read view: the targets (targets.jsonl, each bound to its payload sha256 and the
        snapshot identity), counts by source and host, the same debt in every other source, the
        complete lint-rule scope inventory (registry-only, failed, CC-without-verification rows),
        and the corpus totals
    python scripts/audit_licence_evidence.py arxiv --data-root <checkout> [--sample 500]
        arXiv OAI-PMH GetRecord (arXivRaw) per paper: one connection, >= 3 s between requests,
        Retry-After honoured. The licence is pinned to the version actually fetched: the URL's
        version, else the arXiv stamp in the extracted text (`arXiv:<id>vN`, trusted only when
        that text's sha256 is the row's text_sha256), else the version dates against fetched_at.
        arXiv records carry the CURRENT licence only, so a fetched version that is not the
        latest stays `unresolved`.
    python scripts/audit_licence_evidence.py openalex
        OpenAlex re-query without searches: DOI and landing-page filter lists (50 values per
        request, $0.0001 each; the pass stops above a reserve so the live finder's daily budget
        is never starved). The location matching the fetched URL — identity-bearing query
        parameters kept — decides, and is recorded. A publisher licence on ANOTHER location is
        recorded as a candidate only (identical version not established). OpenAlex rows hosted on
        arxiv.org go through the arXiv pass (arXiv is authoritative, per version).
    python scripts/audit_licence_evidence.py bind --data-root <checkout>
        bind results recorded before payload binding existed to their targets' payload sha256 and
        snapshot, re-verifying each PDF-stamp pin against the text's current sha256 (a changed
        text makes the result stale: re-audited, never applied)
    python scripts/audit_licence_evidence.py report
        final / missing / transient / stale counts, verdicts, class transitions, the default-view
        impact, 10 examples per verdict

Every pass is resumable: results.jsonl is append-only; a target is audited again only when it
has no final result bound to its CURRENT payload (sha256) under the current resolver version.

Verdicts -> licence tag -> use class: eligible (cc-by / cc-by-sa / cc0 / public-domain -> open)
· pointer-only (arxiv-nonexclusive, publisher-oa) · excluded-nc-nd (cc-by-nc* / cc-by-nd ->
nc / nd / nc-nd) · unresolved (unverified). None of them affects collection.

Phase B (prepared, NOT applied — the coordinator decides after review):

    python scripts/audit_licence_evidence.py apply [--apply]

Dry run by default. Refuses until every target has a final, payload-bound result (missing,
transient and stale results are listed). With --apply, ONE step session ("reclassify") —
this command's own writer (the round lock, held throughout), the maintenance window's broker,
or under PostgreSQL authority one standalone staged run whose clean step re-paths the claims and
whose promotion refreshes every view — writes, per row, license (the audited tag),
license_url, license_evidence and rights_verified_at to the registry entry and the manifest row,
and (file authority) moves the cleaned file into its classified view: hard link first, claim
committed, default copy removed last, a corpus/.reclassifying marker failing --check meanwhile.
collection_delta is 0 by construction and verified; class transitions and default-view changes
are reported; raw/text claims and hashes never change; nothing is erased. Idempotent; a row whose
payload, URL or licence changed since the audit is skipped (re-audit it). Default-view removals
are measured cumulatively in the use_view_changes.json control document against its first
baseline; above 1% it refuses unless an operator decision is named (--operator-decision), which
the maintainer window can never supply.

    python scripts/audit_licence_evidence.py pointer-transition [--probe] [--apply]

Phase 2, prepared only: proprietary-internal pointers whose URL serves public bytes become
`proprietary` (collected, classified); catalogue pages, logins and paywalls stay pointers.
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
from pathlib import Path
from typing import Iterable
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
                 "text_path", "text_chars", "corpus_chars", "topic", "sha256", "text_sha256",
                 "raw_path", "corpus_path", "corpus_sha256")
# Resolver versions: a result from an older resolver is re-audited (OpenAlex v2 keeps
# identity-bearing query parameters and records the deciding location).
RESOLVER = {"arxiv-oai-pmh:arXivRaw": 1, "openalex:works": 2}
# discovery-time evidence the audit may replace (find_sources' OpenAlex note)
REPLACEABLE_EVIDENCE = ("OpenAlex OA location license:",)
CONTROL_DOC = "use_view_changes.json"
LIMIT_FRACTION = 0.01
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
    """id -> its latest result record (a final verdict is never superseded by an error of the
    same payload; a stale marker does supersede it)."""
    out: dict[str, dict] = {}
    for r in read_jsonl(path):
        prev = out.get(r["id"])
        if prev is not None and prev.get("verdict") and not r.get("verdict") \
                and not r.get("stale") and r.get("payload_sha256") in (
                    None, prev.get("payload_sha256")):
            continue
        out[r["id"]] = r
    return out


def result_state(target: dict, result: dict | None) -> str:
    """final | missing | transient | stale for one target: a final result is bound to the
    target's CURRENT payload (sha256) and produced by the current resolver version."""
    if result is None:
        return "missing"
    if result.get("stale"):
        return "stale"
    if not result.get("verdict"):
        return "transient"
    if "payload_sha256" not in result:
        return "stale"            # never bound to a payload: run `bind` (or re-audit)
    if result["payload_sha256"] != target.get("sha256"):
        return "stale"            # the fetched bytes changed since the audit
    if result.get("resolver", 1) != RESOLVER.get(result.get("evidence_source"), 1):
        return "stale"
    return "final"


def audit_state(targets: list[dict], results: dict[str, dict]) -> dict[str, str]:
    return {t["id"]: result_state(t, results.get(t["id"])) for t in targets}


def bound(target: dict, out: dict) -> dict:
    """Bind a result to the payload and snapshot it was audited for."""
    out["payload_sha256"] = target.get("sha256")
    out["snapshot"] = target.get("snapshot")
    out["resolver"] = RESOLVER.get(out.get("evidence_source"), 1)
    return out


# --- classification ------------------------------------------------------------------------------

_ARXIV_NONEXCLUSIVE = re.compile(
    r"^https?://arxiv\.org/licenses/(?:nonexclusive-distrib/1\.0|assumed-1991-2003)/?$", re.I)
# The retired CC Public Domain Dedication (and the deed it redirects to)
_CC_PDD = re.compile(r"^https?://(?:www\.)?creativecommons\.org/(?:licenses/publicdomain"
                     r"|publicdomain/certification/1\.0/us)/?$", re.I)
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
        # Retirement does not revoke it, but it is a US-law instrument: never CC0.
        return ("eligible", "public-domain",
                "CC Public Domain Dedication (retired CC tool; jurisdiction: US law — a "
                "dedication under US copyright law, possibly limited elsewhere; not CC0)")
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


def arxiv_verdict(target: dict, record: dict, text: str | None, checked_at: str,
                  text_sha256: str | None = None) -> dict:
    """The result record for one arXiv target from its parsed OAI record, bound to its payload.
    `text` (with its file's sha256) pins the version only when it IS the row's extraction."""
    aid, url_v = arxiv_id(target["url"])
    evidence_url = (f"{ARXIV_OAI}?verb=GetRecord&identifier=oai:arXiv.org:{aid}"
                    f"&metadataPrefix=arXivRaw")
    out = {"id": target["id"], "cohort": target["cohort"], "url": target["url"],
           "arxiv_id": aid, "evidence_source": "arxiv-oai-pmh:arXivRaw",
           "evidence_url": evidence_url, "checked_at": checked_at}
    bound(target, out)
    if text is not None and target.get("text_sha256") \
            and text_sha256 != target["text_sha256"]:
        out["stamp_ignored"] = "text/ does not hold the row's extraction (sha256 differs)"
        text = None
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
               version=f"v{version}" if version else None, version_basis=basis,
               text_sha256_read=text_sha256 if basis == "pdf-stamp" else None)
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


def read_text(data_root: Path | None, text_path: str | None) -> tuple[str | None, str | None]:
    """(the head of a row's extracted text, the sha256 of the whole file), read-only and
    confined to data_root; (None, None) when it is not there."""
    import hashlib
    if data_root is None or not text_path:
        return None, None
    path = (data_root / text_path).resolve()
    if not path.is_relative_to(data_root.resolve()) or not path.is_file():
        return None, None
    data = path.read_bytes()
    return data.decode(errors="replace")[:STAMP_WINDOW], hashlib.sha256(data).hexdigest()


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
    state = audit_state(targets, latest_results(results_path))
    done = {sid for sid, st_ in state.items() if st_ == "final"}
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
            append_jsonl(results_path, [_with_evidence(bound(t, {
                "id": t["id"], "cohort": t["cohort"], "url": t["url"], "arxiv_id": None,
                "evidence_source": "arxiv-oai-pmh:arXivRaw", "evidence_url": t["url"],
                "checked_at": now_iso(), "verdict": "unresolved", "licence": UNRESOLVED_LICENSE,
                "license_url": t["url"], "raw_license": None, "version": None,
                "reason": "URL carries no parsable arXiv id"}))])
            continue
        try:
            xml = fetch_oai(http, aid, log=log)
        except Exception as exc:  # network trouble: record, retry on the next run
            append_jsonl(results_path, [{"id": t["id"], "cohort": t["cohort"], "url": t["url"],
                                         "checked_at": now_iso(), "verdict": None,
                                         "payload_sha256": t.get("sha256"),
                                         "error": f"{type(exc).__name__}: {exc}"[:300]}])
            log(f"# {t['id']}: {exc}")
            continue
        checked = now_iso()
        (raw_dir / (aid.replace("/", "_") + ".xml")).write_text(xml)
        record = parse_arxiv_raw(xml)
        text, text_sha = read_text(data_root, t.get("text_path"))
        res = arxiv_verdict(t, record, text, checked, text_sha)
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


# Query parameters that only select a presentation (cache busters, tracking, download flags);
# every other parameter (PLOS ?id=10.1371/..., DSpace ?sequence=) identifies the document.
_PRESENTATION_PARAMS = {"t", "download", "dl", "inline", "isallowed", "utm_source",
                        "utm_medium", "utm_campaign", "utm_content", "utm_term"}
_PRESENTATION_VALUES = {("type", "printable")}


def norm_loc(url: str | None) -> str:
    """host + path + the identity-bearing query parameters (sorted) of a URL."""
    from urllib.parse import parse_qsl, urlencode
    if not url:
        return ""
    p = urlparse(url.strip())
    host = p.netloc.lower().removeprefix("www.")
    params = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                    if k.lower() not in _PRESENTATION_PARAMS
                    and (k.lower(), v.lower()) not in _PRESENTATION_VALUES)
    return f"{host}{p.path.rstrip('/')}" + (f"?{urlencode(params)}" if params else "")


def _loc_matches(url: str, loc: dict) -> bool:
    """The location IS the fetched copy: the same PDF URL (identity parameters kept), or the
    landing page derived from the fetched URL."""
    landings = {norm_loc(u) for u in landing_candidates(url)}
    got_pdf = norm_loc(loc.get("pdf_url"))
    return bool((got_pdf and got_pdf == norm_loc(url))
                or norm_loc(loc.get("landing_page_url")) in landings)


def openalex_verdict(target: dict, work: dict | None, checked_at: str, evidence_url: str) -> dict:
    out = bound(target, {"id": target["id"], "cohort": target["cohort"], "url": target["url"],
                         "evidence_source": "openalex:works", "evidence_url": evidence_url,
                         "checked_at": checked_at, "version": None,
                         "work_id": (work or {}).get("id")})
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
    decided = [(classify_openalex(loc.get("license")), loc) for loc in matched]
    decisions = {d for d, _ in decided}
    if any(d[0] == "excluded-nc-nd" for d in decisions):
        (verdict, tag, reason), loc = next(x for x in sorted(decided, key=lambda x: x[0])
                                           if x[0][0] == "excluded-nc-nd")
    elif len(decisions) > 1:
        verdict, tag = "unresolved", UNRESOLVED_LICENSE
        reason = f"matching locations disagree: {sorted(d[2] for d in decisions)}"
        loc = None
    else:
        (verdict, tag, reason), loc = decided[0]
    # a publisher grant on ANOTHER location is only a candidate: the identical version is not
    # established by the same DOI / work (Codex decision 3); resolution records it for later
    others = [x for x in locs if x not in matched and x.get("license")
              and x.get("version") == "publishedVersion"]
    if others:
        out["publisher_candidate"] = {"license": others[0].get("license"),
                                      "landing_page_url": others[0].get("landing_page_url")}
    if loc is None:
        out.update(verdict=verdict, licence=tag, reason=reason, raw_license=None,
                   deciding_location=None,
                   matched_url=matched[0].get("pdf_url") or matched[0].get("landing_page_url"),
                   license_url=evidence_url)
        return _with_evidence(out)
    out.update(verdict=verdict, licence=tag, reason=reason, raw_license=loc.get("license"),
               matched_url=loc.get("pdf_url") or loc.get("landing_page_url"),
               deciding_location={k: loc.get(k) for k in ("pdf_url", "landing_page_url",
                                                          "license", "license_id", "version")},
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
    state = audit_state(targets, latest_results(results_path))
    todo = [t for t in targets if state[t["id"]] != "final"]
    log(f"# OpenAlex: {len(todo)} to audit, {len(targets) - len(todo)} final")
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
    """The audit cohort of a successful `license: open` row, or None."""
    sid, src = row.get("id", ""), row.get("source")
    if sid.startswith(("arx-", "arxiv-")) and src == "arxiv":
        return "arxiv"
    if sid.startswith(("ope-", "oa-")) and src == "openalex":
        return "openalex-arxiv" if host(row.get("url")).endswith("arxiv.org") else "openalex"
    return None


def replaceable(evidence: str | None) -> bool:
    """No evidence, or only discovery-time evidence the audit supersedes."""
    return not evidence or str(evidence).startswith(REPLACEABLE_EVIDENCE)


def tokens(row: dict) -> int:
    chars = row.get("corpus_chars", row.get("text_chars")) or 0
    return int(chars) // 4


def snapshot_identity(root: Path, view) -> dict:
    marker = Path(root) / ".complete"
    commit = marker.read_text().strip() if marker.exists() else None
    return {"commit": commit, "store_version": view.version().token}


def enumerate_targets(view, *, snapshot: dict | None = None,
                      log=print) -> tuple[list[dict], dict]:
    """(targets, facts): every successful default-view `license: open` row of the arXiv /
    OpenAlex cohorts without authoritative evidence (none, or only discovery-time evidence),
    each bound to the snapshot; the same debt in every other source; and the complete scope of
    the opt-in lint rule (lint_registry.EVIDENCE_PREFIXES) including registry-only rows."""
    import corpus_stats
    import lint_registry

    restrictions, _ = store.pinned_policy(view)
    stats = corpus_stats.compute(view, restrictions)
    by_source: Counter = Counter()
    by_source_tokens: Counter = Counter()
    with_evidence: Counter = Counter()
    by_host: dict[str, Counter] = defaultdict(Counter)
    targets = []
    manifest_status: dict[str, str] = {}
    where = And(Eq("status", "ok"), Eq("license", "open"))
    for r in corpus_stats.iter_manifest(view, where=where, fields=TARGET_FIELDS):
        if not registry.is_default_corpus_eligible(r, restrictions):
            continue
        c = cohort(r)
        if r.get("license_evidence") and not (c and replaceable(r["license_evidence"])):
            with_evidence[r.get("source")] += 1
            continue
        by_source[r.get("source")] += 1
        by_source_tokens[r.get("source")] += tokens(r)
        if c is None:
            continue
        by_host[c][host(r.get("url"))] += 1
        targets.append({**{k: r.get(k) for k in TARGET_FIELDS if k in r}, "cohort": c,
                        "snapshot": snapshot})
    # the lint rule's whole scope: entries (any manifest state) and manifest rows
    scope: Counter = Counter()
    prefixes = lint_registry.EVIDENCE_PREFIXES
    for prefix in prefixes:
        cursor = None
        while True:
            page = view.scan(store.Table.MANIFEST, where=store.Prefix("id", prefix),
                             fields=("id", "status"), cursor=cursor, limit=store.MAX_PAGE)
            manifest_status.update({r["id"]: r.get("status") for r in page.rows})
            if (cursor := page.next_cursor) is None:
                break
        cursor = None
        while True:
            page = view.scan(store.Table.ENTRIES, where=store.Prefix("id", prefix),
                             fields=("id", "license", "license_evidence", "rights_verified_at"),
                             cursor=cursor, limit=store.MAX_PAGE)
            for e in page.rows:
                if lint_registry.rights_evidence_errors(e, "scope"):
                    state = manifest_status.get(e["id"], "registry-only")
                    scope[f"{prefix}{state}:{e.get('license')}"] += 1
            if (cursor := page.next_cursor) is None:
                break
    facts = {
        "snapshot": snapshot,
        "enumerated_at": now_iso(),
        "corpus": {"documents": stats.documents, "tokens": stats.corpus_tokens,
                   "text_tokens": stats.tokens},
        "open_without_evidence_by_source": dict(by_source.most_common()),
        "open_without_evidence_tokens_by_source": dict(by_source_tokens.most_common()),
        "open_with_evidence_by_source": dict(with_evidence.most_common()),
        "targets_by_cohort": dict(Counter(t["cohort"] for t in targets)),
        "targets_by_host": {c: dict(h.most_common()) for c, h in by_host.items()},
        "lint_scope_without_evidence": dict(sorted(scope.items())),
    }
    return sorted(targets, key=lambda t: t["id"]), facts


def run_enumerate(root: Path, out_dir: Path, *, timeout: float, log=print) -> int:
    st = store.open(root=root)
    with st.read(timeout=timeout) as view:
        snapshot = snapshot_identity(root, view)
        targets, facts = enumerate_targets(view, snapshot=snapshot, log=log)
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


# --- binding results recorded before payload binding ----------------------------------------------

def run_bind(out_dir: Path, *, data_root: Path | None, log=print) -> int:
    """Bind results recorded before payload binding existed to their target's payload sha256 and
    snapshot. A PDF-stamp version pin is kept only when the stamped text still hashes to the
    row's text_sha256; otherwise (the text changed, or the row records no text identity) the
    verdict is re-derived OFFLINE from the saved OAI-PMH response without the stamp (version
    dates only), and marked stale only when that response is not on disk. Appends new records;
    the originals stay in the log."""
    targets = {t["id"]: t for t in read_jsonl(out_dir / "targets.jsonl")}
    results = latest_results(out_dir / "results.jsonl")
    out, counts = [], Counter()
    for sid, t in sorted(targets.items()):
        r = results.get(sid)
        if r is None or result_state(t, r) == "final":
            continue
        unbound = r.get("verdict") and "payload_sha256" not in r
        if not (unbound or r.get("stale")):
            continue                     # transient, resolver-stale, changed payload: re-audit
        stamped = r.get("version_basis") == "pdf-stamp" or r.get("stale")
        if unbound and not stamped:
            out.append(bound(t, dict(r)))
            counts["bound"] += 1
            continue
        if unbound and t.get("text_sha256"):
            _text, sha = read_text(data_root, t.get("text_path"))
            if sha == t["text_sha256"]:
                out.append({**bound(t, dict(r)), "text_sha256_read": sha})
                counts["bound (stamp verified)"] += 1
                continue
        aid, _ = arxiv_id(t["url"])
        xml_path = out_dir / "oai" / f"{(aid or '').replace('/', '_')}.xml"
        if aid is None or not xml_path.is_file():
            out.append({"id": sid, "cohort": t["cohort"], "url": t["url"],
                        "checked_at": now_iso(), "verdict": None, "stale": True,
                        "payload_sha256": t.get("sha256"),
                        "error": "stale: stamp unverifiable and no saved OAI response"})
            counts["stale"] += 1
            continue
        xml = xml_path.read_text()
        when = re.search(r"<responseDate>(.*?)</responseDate>", xml)
        checked = when.group(1) if when else now_iso()
        res = arxiv_verdict(t, parse_arxiv_raw(xml), None, checked)
        res["stamp_ignored"] = ("the row records no text identity (text_sha256), so the "
                                "extraction's arXiv stamp cannot be tied to the payload"
                                if not t.get("text_sha256") else
                                "text/ does not hold the row's extraction (sha256 differs)")
        out.append(res)
        counts["re-derived without the stamp"] += 1
    append_jsonl(out_dir / "results.jsonl", out)
    log(f"# bind: {dict(counts)}")
    return 0


# --- report --------------------------------------------------------------------------------------

def old_class(target: dict) -> str:
    return registry.LICENSE_CLASSES.get(target.get("license"), "unverified")


def new_class(result: dict) -> str:
    return registry.LICENSE_CLASSES.get(result.get("licence"), "unverified")


def build_report(targets: list[dict], results: dict[str, dict], facts: dict) -> dict:
    by_id = {t["id"]: t for t in targets}
    state = audit_state(targets, results)
    audited = {sid: results[sid] for sid, st_ in state.items() if st_ == "final"}
    report: dict = {"generated_at": now_iso(), "targets": len(targets),
                    "states": dict(Counter(state.values())), "audited": len(audited),
                    "cohorts": {}}
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
            "states": dict(Counter(state[t["id"]] for t in pop)),
            "counts": {v: counts[v] for v in VERDICTS},
            "audited_tokens": {v: tok[v] for v in VERDICTS},
            "estimated_population_docs": est_docs,
            "estimated_population_tokens": est_tokens,
            "unresolved_reasons": dict(Counter(
                re.sub(r"\(.*|'.*|v\d+", "", r["reason"]).strip()[:80]
                for r in done if r["verdict"] == "unresolved").most_common(8)),
            "raw_licences": dict(Counter(str(r.get("raw_license")) for r in done).most_common()),
            "version_basis": dict(Counter(str(r.get("version_basis")) for r in done)),
            "publisher_candidates": sum(1 for r in done if r.get("publisher_candidate")),
        }
    transitions = Counter(f"{old_class(by_id[sid])} -> {new_class(r)}"
                          for sid, r in audited.items())
    leaving = [sid for sid, r in audited.items() if new_class(r) != "open"]
    lost_docs = sum((c["estimated_population_docs"][v] or 0)
                    for c in report["cohorts"].values() for v in VERDICTS if v != "eligible")
    lost_tokens = sum((c["estimated_population_tokens"][v] or 0)
                      for c in report["cohorts"].values() for v in VERDICTS if v != "eligible")
    report["class_transitions"] = dict(transitions.most_common())
    report["impact"] = {
        "collection_delta": 0,
        "audited_leaving_default_view_docs": len(leaving),
        "audited_leaving_default_view_tokens": sum(tokens(by_id[s]) for s in leaving),
        "estimated_leaving_default_view_docs": lost_docs,
        "estimated_leaving_default_view_tokens": lost_tokens,
        "corpus_documents": corpus_docs, "corpus_tokens": corpus_tokens,
        "fraction_docs": lost_docs / corpus_docs if corpus_docs else None,
        "fraction_tokens": lost_tokens / corpus_tokens if corpus_tokens else None,
    }
    rng = random.Random(0)
    examples = {}
    for v in VERDICTS:
        pool = sorted((r for r in audited.values() if r["verdict"] == v), key=lambda r: r["id"])
        examples[v] = [{k: r.get(k) for k in ("id", "cohort", "url", "version", "version_basis",
                                               "raw_license", "licence", "reason")}
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


# --- phase B: evidence + reclassification ---------------------------------------------------------

def patch_for(result: dict) -> dict:
    """The rights fields phase B writes for one final audit result."""
    return {"license": result["licence"], "license_url": result["license_url"],
            "license_evidence": result["evidence"],
            "rights_verified_at": result["checked_at"][:10]}


def plan_row(result: dict, entry: dict | None, row: dict | None) -> tuple[str, dict | None]:
    """('apply' | 'applied' | skip reason, rights patch) for one audited row — idempotent and
    guarded: only the audited state (the same payload and URL, licence `open`, no authoritative
    evidence) is ever changed."""
    patch = patch_for(result)
    if entry is None or row is None:
        return "skip: row or entry no longer exists", None
    if all(entry.get(k) == v for k, v in patch.items()) and all(
            row.get(k) == v for k, v in patch.items()):
        return "applied", None
    if row.get("sha256") != result.get("payload_sha256"):
        return "skip: payload changed since the audit (re-audit)", None
    if row.get("url") != result["url"] or entry.get("url") != result["url"]:
        return "skip: URL changed since the audit", None
    for rec in (entry, row):
        if rec.get("license") != "open" or not replaceable(rec.get("license_evidence")):
            return "skip: licence or evidence changed since the audit", None
    if patch["license"] not in registry.KNOWN_LICENSES:
        return f"skip: unexpected licence {patch['license']!r}", None
    return "apply", patch


def final_results(out_dir: Path) -> tuple[dict[str, dict], dict[str, str], list[dict]]:
    """(final results by id, the state of every target, the targets)."""
    targets = read_jsonl(out_dir / "targets.jsonl")
    results = latest_results(out_dir / "results.jsonl")
    state = audit_state(targets, results)
    return ({sid: results[sid] for sid, st_ in state.items() if st_ == "final"}, state,
            targets)


def load_changes(view) -> dict:
    doc = view.control_get(CONTROL_DOC) or {}
    return {"format": 1, "baseline": doc.get("baseline"), "changesets": doc.get("changesets", [])}


def run_apply(root: Path, out_dir: Path, *, apply: bool, operator_decision: str | None = None,
              allow_partial: bool = False, batch_size: int = APPLY_BATCH, timeout: float = 60,
              log=print, st=None) -> int:
    """Phase B in ONE step session (see the module docstring). `st`: the store (default: the one
    authoritative for `root`)."""
    import artifact_store
    import clean_corpus
    import corpus_stats

    results, state, _targets = final_results(out_dir)
    pending = Counter(v for v in state.values() if v != "final")
    log(f"audit states: {dict(Counter(state.values()))}")
    if pending and not allow_partial:
        log(f"REFUSED: {sum(pending.values())} targets have no final, payload-bound result "
            f"({dict(pending)}); finish the passes (`bind` binds older results) first")
        return 1
    if not results:
        log("no final audit results")
        return 1
    if operator_decision and store_broker.client() is not None:
        log("REFUSED: an operator decision cannot be supplied inside the maintenance window")
        return 1
    st = st if st is not None else store.open(root=root)
    with store_broker.step_session(st, "reclassify", timeout=timeout) as session:
        view = session.view
        restrictions, _ = store.pinned_policy(view)
        stats = corpus_stats.compute(view, restrictions)
        held_before = corpus_stats.compute_collection(view, restrictions).total_held
        rows = view.get_manifest(results)
        entries = view.get_entries(results)
        changes = load_changes(view)
        versioned = artifact_store.for_view(view, Path(root)) is not None

        plan: Counter = Counter()
        transitions: Counter = Counter()
        todo: dict[str, dict] = {}
        out_docs = out_tokens = in_docs = 0
        for sid, res in sorted(results.items()):
            action, patch = plan_row(res, entries.get(sid), rows.get(sid))
            plan[action] += 1
            if action != "apply":
                continue
            row = rows[sid]
            before, after = registry.view_of(row, restrictions), registry.view_of(
                {**row, **patch}, restrictions)
            transitions[f"{registry.use_class(row, restrictions)} -> "
                        f"{registry.use_class({**row, **patch}, restrictions)}"] += 1
            if before == registry.DEFAULT_VIEW and after != registry.DEFAULT_VIEW:
                out_docs += 1
                out_tokens += tokens(row)
            elif after == registry.DEFAULT_VIEW and before != registry.DEFAULT_VIEW:
                in_docs += 1
            todo[sid] = patch
        baseline = changes["baseline"] or {"documents": stats.documents,
                                           "tokens": stats.corpus_tokens, "at": now_iso()}
        prior_docs = sum(c.get("left_default_docs", 0) for c in changes["changesets"])
        prior_tokens = sum(c.get("left_default_tokens", 0) for c in changes["changesets"])
        frac_docs = (prior_docs + out_docs) / baseline["documents"] if baseline["documents"] \
            else 0.0
        frac_tokens = (prior_tokens + out_tokens) / baseline["tokens"] if baseline["tokens"] \
            else 0.0
        log(f"plan: {dict(plan)}")
        log(f"class transitions: {dict(transitions.most_common())}")
        log(f"collection delta: 0 (reclassification never removes collected bytes); "
            f"{held_before:,} held originals")
        log(f"default view: -{out_docs:,} docs / -{out_tokens:,} tokens, +{in_docs:,} docs; "
            f"cumulative since the {baseline['at']} baseline: {frac_docs:.3%} of "
            f"{baseline['documents']:,} docs, {frac_tokens:.3%} of {baseline['tokens']:,} "
            "tokens")
        if (out_docs or out_tokens) and max(frac_docs, frac_tokens) > LIMIT_FRACTION \
                and not operator_decision:
            log("REFUSED: the cumulative default-view change exceeds 1% (AGENTS.md); record a "
                "measured proposal for the operator, who may name a decision with "
                "--operator-decision")
            return 1
        if not apply or not todo:
            log("dry run -- pass --apply to write" if not apply else "nothing to apply")
            return 0

        marker = Path(root) / "corpus" / clean_corpus.RECLASSIFYING
        if not versioned:
            marker.parent.mkdir(parents=True, exist_ok=True)
            ops.atomic_write_text(marker, f"{session.identity('start')}\n")
        moved: list[tuple[Path, Path]] = []
        written = 0
        for n, chunk in enumerate(store_broker.shard_batches(todo, batch_size), 1):
            patches, new_entries = {}, []
            for sid in chunk:
                patch = dict(todo[sid])
                row = rows[sid]
                if not versioned and row.get("corpus_path"):
                    # file authority: the cleaned file moves to its view BEFORE the claim does
                    # (a hard link: the same bytes and hash); the old entry goes last
                    new_rel = registry.corpus_path_for({**row, **patch}, restrictions)
                    old = Path(root) / row["corpus_path"]
                    new = Path(root) / new_rel
                    if new_rel != row["corpus_path"] and old.is_file():
                        new.parent.mkdir(parents=True, exist_ok=True)
                        if not new.exists():
                            os.link(old, new)
                        moved.append((old, new))
                        patch["corpus_path"] = new_rel
                patches[sid] = patch
                new_entries.append({**entries[sid], **todo[sid]})
            with session.batch(f"rights-{n:04d}") as b:
                b.update_manifest_fields(patches)
                b.upsert_entries(new_entries)
            written += len(patches)
            log(f"batch {n}: {len(patches)} rows")
        with session.batch("ledger") as b:
            changes["baseline"] = baseline
            changes["changesets"].append({
                "kind": "licence-reclassification", "at": now_iso(),
                "session": session.identity("ledger"), "rows": written,
                "left_default_docs": out_docs, "left_default_tokens": out_tokens,
                "entered_default_docs": in_docs, "transitions": dict(transitions),
                "operator_decision": operator_decision})
            b.control_set(CONTROL_DOC, changes)
        for old, new in moved:          # every claim is committed: drop the default copies
            if old.exists() and new.exists() and os.path.samefile(old, new):
                old.unlink()
        if not versioned:
            marker.unlink(missing_ok=True)
    with st.read() as view:
        held_after = corpus_stats.compute_collection(view).total_held
    if held_after != held_before:
        log(f"ERROR: collection delta {held_after - held_before} (expected 0)")
        return 1
    log(f"applied rights evidence to {written} rows; {len(moved)} cleaned files moved to their "
        "classified views; collection delta 0" + ("" if not versioned else
                                                  "; the run's clean step re-paths the claims"))
    return 0


# --- phase 2, prepared only: proprietary pointers with public bytes -----------------------------

def run_pointer_transition(root: Path, *, probe: bool, apply: bool, timeout: float = 60,
                           http=None, log=print, st=None) -> int:
    """proprietary-internal pointers -> `proprietary` (collected, classified) when their URL
    serves public bytes. Without --probe nothing is requested: rows are only listed as
    candidates (a direct PDF URL) or pointers. --probe asks each candidate once (HEAD, then a
    ranged GET) with the loader's honest identity: HTTP 200 and a PDF content type without a
    redirect to a login or checkout page. Logins, paywalls, challenges and catalogue pages stay
    pointers. --apply re-tags the proven rows with the probe as evidence (phase 2)."""
    st = st if st is not None else store.open(root=root)
    with st.read(timeout=timeout) as view:
        restrictions, _ = store.pinned_policy(view)
        pointers = []
        cursor = None
        while True:
            page = view.scan(store.Table.ENTRIES, where=Eq("license", "proprietary-internal"),
                             cursor=cursor, limit=store.MAX_PAGE)
            pointers += page.rows
            if (cursor := page.next_cursor) is None:
                break
    plan = []
    for e in pointers:
        url = e.get("url") or ""
        direct = e.get("format") == "pdf" or urlparse(url).path.lower().endswith(".pdf")
        outcome = "candidate" if direct else "pointer: not a direct full-text URL"
        evidence = None
        if direct and probe:
            outcome, evidence = _probe_public(http or Throttle(ARXIV_MIN_INTERVAL), url)
        plan.append((e, outcome, evidence))
    for e, outcome, _ev in plan:
        log(f"{e['id']}: {outcome} ({e.get('url')})")
    proven = [(e, ev) for e, outcome, ev in plan if outcome == "public-bytes"]
    log(f"pointer transition: {len(pointers)} pointers, "
        f"{sum(1 for _, o, _e in plan if o == 'candidate')} unprobed candidates, "
        f"{len(proven)} proven public")
    if not apply or not proven:
        return 0

    def body(view, batch):
        entries = view.get_entries(e["id"] for e, _ in proven)
        rows = view.get_manifest(e["id"] for e, _ in proven)
        today = now_iso()
        new = []
        for e, ev in proven:
            cur = entries.get(e["id"])
            if cur is None or cur.get("license") != "proprietary-internal":
                continue
            new.append({**cur, "license": "proprietary", "license_evidence": ev,
                        "rights_verified_at": today[:10]})
        if new:
            batch.upsert_entries(new)
            manifest = {e["id"]: {"license": "proprietary"} for e in new if e["id"] in rows}
            if manifest:
                batch.update_manifest_fields(manifest)
        return len(new)

    n, _ = store_broker.run_batch(st, "pointer-transition", body, timeout=timeout)
    log(f"re-tagged {n} pointers as proprietary (collected, classified 'proprietary')")
    return 0


def _probe_public(http, url: str) -> tuple[str, str | None]:
    """('public-bytes', evidence) or ('pointer: <why>', None) for one URL."""
    try:
        r = http.get(url, headers={"Range": "bytes=0-1023"}, allow_redirects=True)
    except Exception as exc:   # an unreachable URL stays a pointer
        return f"pointer: unreachable ({type(exc).__name__})", None
    final = getattr(r, "url", url) or url
    ctype = (r.headers.get("Content-Type") or "").lower()
    body = getattr(r, "content", b"") or b""
    if r.status_code not in (200, 206):
        return f"pointer: HTTP {r.status_code}", None
    if re.search(r"login|signin|sign-in|checkout|cart|account", final, re.I):
        return "pointer: redirected to a login or checkout page", None
    if "pdf" not in ctype and not body.startswith(b"%PDF"):
        return f"pointer: not a PDF ({ctype or 'no content type'})", None
    return "public-bytes", (f"public PDF served without login at {final} (HTTP "
                            f"{r.status_code}, {ctype or 'PDF magic'}), probed {now_iso()}")


# --- CLI -----------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="audit directory (git-ignored)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("snapshot", help="extract a checkout's committed tracked state")
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
    p = sub.add_parser("bind", help="bind older results to their payloads")
    p.add_argument("--data-root", type=Path, default=None)
    sub.add_parser("report", help="summarise results")
    p = sub.add_parser("apply", help="phase B: evidence + reclassification (dry run by default)")
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--operator-decision", default=None,
                   help="the operator decision authorizing a >1%% default-view change (never "
                        "available inside the maintenance window)")
    p.add_argument("--allow-partial", action="store_true",
                   help="apply the final results although other targets are still pending")
    p.add_argument("--batch", type=int, default=APPLY_BATCH)
    p.add_argument("--lock-timeout", type=float, default=60)
    p = sub.add_parser("pointer-transition", help="phase 2: proprietary pointers with public "
                                                  "bytes (dry run by default)")
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--probe", action="store_true")
    p.add_argument("--apply", action="store_true")
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
    if args.cmd == "bind":
        return run_bind(out, data_root=args.data_root)
    if args.cmd == "report":
        return run_report(out)
    if args.cmd == "apply":
        return run_apply(args.root, out, apply=args.apply,
                         operator_decision=args.operator_decision,
                         allow_partial=args.allow_partial, batch_size=args.batch,
                         timeout=args.lock_timeout)
    if args.cmd == "pointer-transition":
        return run_pointer_transition(args.root, probe=args.probe, apply=args.apply)
    return 2


if __name__ == "__main__":
    sys.exit(main())
