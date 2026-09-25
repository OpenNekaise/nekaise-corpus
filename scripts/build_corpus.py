#!/usr/bin/env python3
"""build_corpus.py — fetch & verify the corpus from the registry.

Reads the registry (registry/*.yaml), downloads each source into raw/<source>/<id>.<ext>, extracts
plain text into text/<id>.md, and records everything (incl. sha256 and quality metrics) in the
sharded manifest (manifest/<shard>.jsonl) through the store (scripts/store.py). The committed
manifest is the REPRODUCIBILITY record: a fresh clone runs this to fetch the SAME bytes, and the
run reports how many reproduced exactly (sha256 matches the manifest) vs drifted (the source
changed upstream) vs new.

  python scripts/build_corpus.py            # fetch missing; report reproduced / drifted / new vs manifest
  python scripts/build_corpus.py --force    # re-fetch everything
  python scripts/build_corpus.py --only controls_bas
  python scripts/build_corpus.py --workers 16 --extract-workers 8
  python scripts/build_corpus.py --verify   # no download: re-hash local raw files against the manifest

Idempotent, dedups identical bytes by sha256, checkpoints the manifest every 25 fetches — each
checkpoint one short store transaction holding exactly the rows recorded since the previous one
(ADR 0001 stage 3, step 6; through the round's broker inside a round, under this command's own
writer standalone; downloads and extraction never run inside a transaction). Downloads
are fairly interleaved across hosts with conservative host-specific request caps; extraction runs
in a separate process pool so CPU work never holds a network slot. raw/ and text/ are git-ignored;
respect each source's license (see README.md).

text/ is the VERBATIM extraction and stays that way — the cleaned, training-ready copy is built
from it by the next stage, scripts/clean_corpus.py, into corpus/. Keeping the two separate is what
lets an improved cleaning ruleset be re-applied in minutes instead of re-extracting 104k PDFs.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import io
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import (
    FIRST_COMPLETED,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

import artifact_store
import compliance_common
import host_policy
import ops
import corpus_stats
import markup_text
import quality
import registry
import store
import store_broker

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
RAW = HERE / "raw"
TEXT = HERE / "text"
# Payload access of a versioned staged run (ADR 0001 stage 4 step 3), set by run() for its
# duration: new raw and text bytes become immutable versions under artifacts/ and raw/ and text/
# are never written. None: the legacy file-authoritative behaviour (writes raw/ and text/).
ACCESS: "artifact_store.VersionedAccess | None" = None
# Browser-like UA: publisher / repository bot-walls (eScholarship, Frontiers, PMC, …) 403 a generic
# UA even for openly-licensed (CC-BY / OA) PDFs we're entitled to fetch. (MDPI sits behind Cloudflare
# and still blocks; those need a headless browser — skipped for now.)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = 45
EXTRACTION_CONTEXT = multiprocessing.get_context("spawn")
# plain-text source formats (GitHub READMEs / docs, .rst, etc.): stored verbatim, no parsing.
TEXT_FORMATS = {"md", "rst", "txt"}
# documentation markup converted to readable text by conservative strippers (scripts/markup_text.py):
# LaTeX manuals (NIST FDS / CFAST) and troff man/ms pages (Radiance). Raw bytes keep the markup.
MARKUP_FORMATS = {"tex": markup_text.tex_to_text, "troff": markup_text.troff_to_text}
# Politeness: never more than this many in-flight requests against one host, however many workers.
# Hosts listed below have handled four concurrent public-document transfers reliably.  Parsing is
# deliberately outside these limits: a 100MB PDF must not hold a network slot while pypdf works.
PER_HOST = 2
HOST_CONCURRENCY: dict[str, int] = {
    "www.osti.gov": 4,
    "library.oapen.org": 4,
    "patents.google.com": 4,
    "documents.worldbank.org": 4,
    "documents1.worldbank.org": 4,
    "www.scielo.br": 4,
    # SiteGround's rate-triggered sgcaptcha (HTTP 202 + HTML) poisoned 881 of 917 IBPSA fetches
    # on 2026-08-05; one request at a time, spaced by HOST_DELAY, keeps it off.
    "publications.ibpsa.org": 1,
    "escholarship.org": 1,  # robots Crawl-delay: 4 means serial
}
# politeness overrides for hosts that need them (currently none). HOST_DELAY: minimum seconds
# between request STARTS against a host, enforced under its semaphore — for hosts that tarpit at
# volume. HOST_UA: per-host User-Agent override, applied to both the requests call and the curl
# fallback — for hosts that block the spoofed-browser UA but pass an honest bot UA. Before adding
# a host here to work around its wall, check its ToS/robots.txt — a wall is sometimes the host
# enforcing terms we must respect (nrc-publications.canada.ca, 07-12: "systematic downloading is
# not permitted" — that vein was reverted, NO-GO).
#
# Same verdict, 07-28 — NO-GO, do not rebuild these:
#   erdc-library.erdc.dren.mil  US Army Corps ERDC Library. Content is public-domain and a
#     find_erdc.py backend worked (400 entries appended, then REVERTED). But the 403 it serves a
#     python UA is a wall, and robots.txt names the AI/dataset crawlers — ClaudeBot, Claude-Web,
#     GPTBot, PerplexityBot, img2dataset, Google-Extended — with `Disallow: /`. An LLM-training
#     corpus is precisely what that opts out of; public-domain content does not override the
#     operator's stated access policy.
#   www.scielo.cl · www.scielo.org.pe  SciELO Chile / Peru: robots.txt `User-agent: * Disallow: /`
#     (Peru additionally names anthropic-ai and Claude-Web). www.scielo.br is `Allow: /` and IS
#     mined — check each SciELO national host separately, they do not share a policy.
HOST_DELAY: dict[str, float] = {
    "www.jstage.jst.go.jp": 2.0,  # J-STAGE throttles bulk fetches; nightly ~00:00 JST 503 window
    "www.boverket.se": 10.0,      # robots.txt Crawl-delay: 10 — respect it
    "publications.ibpsa.org": 3.0,  # rate-triggered sgcaptcha poisons bulk fetches (08-05 pause);
                                    # serial (HOST_CONCURRENCY 1) at 1 req/3s keeps it off
    "escholarship.org": 4.0,      # robots.txt Crawl-delay: 4 — respect it (find_escholarship)
    "bigladdersoftware.com": 10.0,  # robots.txt Crawl-delay: 10 — respect it
}
HONEST_UA = "nekaise-corpus/build_corpus"
HOST_UA: dict[str, str] = {
    # The SiteGround WAF 403s the spoofed-Chrome UA (75 KB block page) but serves the PDF to a
    # short honest tool UA (verified 2026-09-24; UAs with "(...)" comments were also 403'd).
    "publications.ibpsa.org": HONEST_UA,
    # CloudFront refuses this identified UA (403, 2026-09-24). Presenting as a browser instead
    # would be WAF avoidance (Codex policy decision 2026-09-24), so eScholarship rows fail
    # transiently until an honest route works; they are retried, never blocklisted.
    "escholarship.org": HONEST_UA,
}
# POLITE hosts: a refusal or challenge means "stop and come back later", never "try another
# identity". For these hosts the loader uses only HOST_UA, never the browser UA or the curl
# fallback, classifies the answer BEFORE doing anything else, and after the first challenge
# (HTTP 202/403/429/503, or a captcha page served as 200) opens a per-run circuit: every later
# download for the host fails fast WITHOUT a request. Such failures and network timeouts are
# marked `transient`, stay in the registry and are retried by later rounds (bounded by
# prune_corpus.RETRY_MAX_*), and are never blocklisted.
POLITE_HOSTS = frozenset({"publications.ibpsa.org", "escholarship.org"})
CHALLENGE_STATUSES = frozenset({202, 403, 429, 503})
CHALLENGE_BODY = re.compile(
    rb"sgcaptcha|captcha|challenge-platform|cf-chl|awswaf|request blocked|access denied", re.I
)
HTML_CHALLENGE_BODY = re.compile(
    rb"sgcaptcha|g-recaptcha|hcaptcha|challenge-platform|cf-chl|awswaf|"
    rb"<title>\s*(?:access denied|request rejected|just a moment|attention required)", re.I)
# Per-run download cap for polite hosts, so a retry backlog paced at HOST_DELAY cannot stretch a
# round's fetch step: the excess stays in the registry untouched (no manifest row) until later.
HOST_RUN_CAP: dict[str, int] = {
    "publications.ibpsa.org": 80,  # 80 x 3 s = 4 min
    "escholarship.org": 60,        # 60 x 4 s = 4 min
}
# Compliance/ESG programme (Codex decision 2026-09-25; ids bov-bfs- / reg- / eur- / esf-, see
# scripts/compliance_common.py, which owns the reviewed delivery-host table): a programme row's
# EVERY hop must be on a reviewed delivery host (compliance_common.PROGRAMME_HOSTS; anything else
# — e.g. the TCFD site's assets.bbhub.io CDN — is refused before it is requested), is checked
# against robots.txt, uses the honest UA, is challenge-classified (HTML challenge markers too)
# with a per-host circuit, never falls back to curl, and is serial/paced/capped per host group
# (aliases share one budget: pace_key). Programme budgets per run — for ALL programme work,
# restorations included: PROGRAMME_RUN_CAP documents (ESEF_RUN_CAP of them ESEF), PROGRAMME_BYTES
# decoded bytes and PROGRAMME_WALL seconds, and per document a streamed byte cap and a total
# DOCUMENT_DEADLINE (waits included; elapsed time is a retryable failure). Work refused by a
# budget is DEFERRED (no manifest change; handed to the pruner like HOST_RUN_CAP deferrals); a
# held programme row whose text is not restored yet is "locally unavailable"
# (registry.programme_unavailable), so a deferred restoration resumes in a later round.
PROGRAMME_RUN_CAP = compliance_common.PROGRAMME_RUN_CAP
ESEF_RUN_CAP = compliance_common.ESEF_RUN_CAP
PROGRAMME_MAX_BYTES = 128 * 1024 * 1024
ESEF_MAX_BYTES = 64 * 1024 * 1024
PROGRAMME_BYTES = 1024 * 1024 * 1024
ESEF_BYTES = 256 * 1024 * 1024
PROGRAMME_WALL = 900.0
DOCUMENT_DEADLINE = 180.0
READ_CHUNK = 8192
for _host, (_delay, _cap) in compliance_common.PROGRAMME_HOSTS.items():
    HOST_DELAY[_host] = max(HOST_DELAY.get(_host, 0.0), _delay)
    HOST_CONCURRENCY[_host] = 1
    HOST_RUN_CAP[_host] = min(HOST_RUN_CAP.get(_host, _cap), _cap)
    HOST_UA[_host] = HONEST_UA
POLITE_HOSTS = POLITE_HOSTS | frozenset(compliance_common.PROGRAMME_HOSTS)
pace_key = compliance_common.pace_key


def is_programme_row(sid: str) -> bool:
    return compliance_common.is_programme_id(sid)


COOLDOWN_DAYS = 7


def _cooldown_path() -> Path:
    return HERE / "workspace" / "programme-cooldowns.json"  # scratch: loss only costs a retry


def _cooldowns() -> set[str]:
    """Held snapshots found unrestorable within COOLDOWN_DAYS (their version is gone upstream)."""
    try:
        data = json.loads(_cooldown_path().read_text())
    except (OSError, ValueError):
        return set()
    now = time.time()
    return {sid for sid, at in data.items()
            if isinstance(at, (int, float)) and now - at < COOLDOWN_DAYS * 86400}


_cool_lock = threading.Lock()


def _cool(sid: str) -> None:
    with _cool_lock:
        try:
            data = json.loads(_cooldown_path().read_text())
        except (OSError, ValueError):
            data = {}
        data[sid] = time.time()
        try:
            _cooldown_path().parent.mkdir(parents=True, exist_ok=True)
            ops.atomic_write_text(_cooldown_path(), json.dumps(data, sort_keys=True))
        except OSError:
            pass


def _log_restore_failure(rec: dict) -> None:
    """Scratch evidence of a failed restoration of a held programme row (its manifest row is
    kept): workspace/programme-restore-failures.jsonl."""
    try:
        path = HERE / "workspace" / "programme-restore-failures.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps({"id": rec["id"], "url": rec.get("url"),
                                 "http_status": rec.get("http_status"), "error": rec.get("error"),
                                 "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                     + "\n")
    except OSError:
        pass


class ProgrammeBudget:
    """This run's programme byte/time accounting (thread-safe)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self, now: float | None = None) -> None:
        with self.lock:
            self.bytes = self.esef_bytes = 0
            self.deadline = (now if now is not None else time.monotonic()) + PROGRAMME_WALL

    def admit(self, sid: str) -> bool:
        """Whether NEW programme work may still start."""
        with self.lock:
            if time.monotonic() > self.deadline or self.bytes >= PROGRAMME_BYTES:
                return False
            return not (sid.startswith("esf-") and self.esef_bytes >= ESEF_BYTES)

    def charge(self, sid: str, n: int) -> None:
        """Account n decoded bytes; any programme work past a budget raises BudgetExceeded
        (restorations included: see cap_per_host)."""
        with self.lock:
            self.bytes += n
            if sid.startswith("esf-"):
                self.esef_bytes += n
            over = self.bytes > PROGRAMME_BYTES or (
                sid.startswith("esf-") and self.esef_bytes > ESEF_BYTES)
        if over:
            raise BudgetExceeded(f"programme byte budget exhausted at {sid}")


PROGRAMME = ProgrammeBudget()


# Every request hop — the registry URL, each HTTP redirect (DOI resolvers, download/CDN links,
# mirrors), each curl-fallback hop — goes through ONE path (_get_hops / _curl_follow) that, BEFORE
# requesting it: checks the pinned host policy (registry/host_policy.json, set by _run) on the
# canonical hostname (host_policy.canonical_host: no port/userinfo, no trailing dot, IDNA); for
# rows whose licence evidence is bound to one scholarly COPY (COPY_BOUND_SOURCES) also refuses
# NO-GO hosts and any hop that is not provably the same copy (oa_resolution.same_copy: the same
# repository identifier on the same host or a configured host pair), because the evidence does
# not transfer to another copy; and applies that hop host's semaphore, delay, User-Agent and
# polite-host circuit. The chain is recorded on the row (redirect_chain, final_url). A redirect to
# a SUSPENDED host is recorded as `suspended_redirect` (never requested, no retry ageing, and the
# pruner protects the row while the suspension stands).
HOST_POLICY: dict[str, dict] = {}
MAX_REDIRECTS = 10
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
COPY_BOUND_SOURCES = frozenset({"openalex", "openalex_sim", "openalex_ai"})
ACCEPT = "application/pdf,text/html;q=0.9,*/*;q=0.8"
# Scholarly-metadata sources fetch from many hosts this loader has never seen: for NEW work on a
# host with no configured cap, one connection and 2 s between request starts (a longer declared
# HOST_DELAY wins). Restoring rows already held is not slowed.
PACED_SOURCES = COPY_BOUND_SOURCES
PACED_DELAY = 2.0


class HopRefused(Exception):
    """A request hop was refused before it was requested."""

    def __init__(self, url: str, why: str):
        super().__init__(f"{why}; not requested")
        self.url = url


class HostSuspended(HopRefused):
    """The hop's host is fetch-suspended (registry/host_policy.json)."""

    def __init__(self, url: str, rule: dict):
        host = host_policy.canonical_host(url) or url
        super().__init__(url, f"redirect to fetch-suspended host {host} refused "
                              f"(registry/host_policy.json, {rule.get('decided_at')})")
        self.host, self.rule = host, rule


class CopyChanged(HopRefused):
    """A copy-bound row's hop leaves the licensed copy (or reaches a NO-GO host)."""


class RobotsRefused(HopRefused):
    """robots.txt disallows the hop (compliance programme rows): a hard, policy failure."""


class RouteRefused(HopRefused):
    """A programme row's hop leaves the reviewed delivery hosts (compliance_common): refused."""


class TooLarge(Exception):
    """The decoded response body exceeded the row's byte cap (or its total deadline)."""


class BudgetExceeded(Exception):
    """The programme's per-run byte/time budget is spent: the NEW row is deferred."""


class DeadlineExceeded(Exception):
    """The document's total time budget ran out (waits included): retryable, never durable."""


class ChallengeRefused(Exception):
    """A polite host answered with a challenge, or its per-run circuit is open."""

    def __init__(self, message: str, status: int | None = None, requested: bool = True):
        super().__init__(message)
        self.status, self.requested = status, requested


class Hops:
    """One download chain: the origin, whether its rights are copy-bound, whether new hosts get
    the paced defaults, the chain of requested hops, and the chain's OWN cookie session (a
    302 + Set-Cookie + relative Location chain needs the cookie on the next hop)."""

    def __init__(self, origin: str, copy_bound: bool, paced: bool = False,
                 robots: bool = False, max_bytes: int | None = None, sid: str = ""):
        self.origin, self.copy_bound, self.paced = origin, copy_bound, paced
        # compliance programme rows: every hop on a reviewed host, robots.txt checked, honest
        # UA, challenge-classified, decoded body capped and charged to the programme budget
        self.robots, self.max_bytes = robots, max_bytes
        self.sid = sid
        self.started = time.monotonic()
        self.chain: list[str] = []
        self.session = ChainSession()


class ChainSession:
    """One download chain's session: a persistent cookie jar (cookielib domain/path rules)
    shared by every hop, while each hop still carries its own host's headers and User-Agent.
    Requests go through requests.get with the jar, so the transport stays the module's."""

    def __init__(self):
        self.cookies = requests.cookies.RequestsCookieJar()

    def get(self, url, **kwargs):
        resp = requests.get(url, cookies=self.cookies, **kwargs)
        # Apply the response's Set-Cookie headers to THIS jar exactly as requests.Session does
        # (cookielib: deletions, Max-Age/Expires, Path and Domain rules), not a merge of
        # resp.cookies, which can only add. A response without raw headers sets nothing.
        request = getattr(resp, "request", None)
        raw = getattr(resp, "raw", None)
        if request is not None and raw is not None:
            requests.cookies.extract_cookies_to_jar(self.cookies, request, raw)
        return resp


_hops = threading.local()


def _current_hops() -> "Hops | None":
    return getattr(_hops, "value", None)


def check_hop(url: str, hops: "Hops | None" = None) -> None:
    """Refuse a hop before it is requested (see the block comment above); log it otherwise."""
    import oa_resolution

    hops = hops if hops is not None else _current_hops()
    if rule := host_policy.suspended(url, HOST_POLICY):
        raise HostSuspended(url, rule)
    if hops is not None and hops.robots:
        import robots_policy
        url = requests.Request("GET", url).prepare().url  # dot segments, escapes: as sent
        if not compliance_common.reviewed_host(url):
            raise RouteRefused(url, f"{host_policy.canonical_host(url)} is not a reviewed "
                                    "delivery host of the compliance programme")
        try:
            ok, delay = robots_policy.decision(url)
        except robots_policy.RobotsUnavailable as exc:
            # robots.txt cannot be established now: defer, never request the document
            raise ChallengeRefused(f"robots.txt unavailable ({exc})", requested=False) from exc
        if not ok:
            raise RobotsRefused(url, f"robots.txt disallows {url}")
        if delay:
            key = pace_key(host_policy.canonical_host(url))
            with _host_sems_lock:
                HOST_DELAY[key] = max(HOST_DELAY.get(key, 0.0), float(delay))
    if hops is not None and hops.copy_bound:
        host = host_policy.canonical_host(url)
        never = oa_resolution.host_matches(
            host, oa_resolution.NEVER_FETCH_HOSTS | oa_resolution.WORK_EXCLUDED_HOSTS)
        if never:
            raise CopyChanged(url, f"hop to NO-GO host {host} refused")
        if not oa_resolution.same_copy(hops.origin, url):
            raise CopyChanged(url, f"redirect leaves the licensed copy "
                                   f"({host_policy.canonical_host(hops.origin)} -> {host}); "
                                   "its rights evidence does not transfer")
    if hops is not None:
        hops.chain.append(url)


def _header(resp, name: str) -> str | None:
    headers = getattr(resp, "headers", None) or {}
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _pace_host(host: str) -> None:
    """The paced defaults for a host with no configured cap: one connection, PACED_DELAY
    between request starts (a longer configured delay wins). Installed BEFORE the host's
    semaphore is first created, under the same lock."""
    with _host_sems_lock:
        if host not in _host_sems:
            HOST_CONCURRENCY.setdefault(host, 1)
        HOST_DELAY[host] = max(HOST_DELAY.get(host, 0.0), PACED_DELAY)


def _get_hops(url: str, fmt: str, *, session=None, headers: dict | None = None):
    """requests with MANUAL redirects inside ONE session per chain (the caller's, else the
    download's ChainSession, so cookies set by a hop reach the next): every hop is checked
    (check_hop), paced (its host's semaphore and delay; a paced chain installs the paced
    defaults for a NEW destination host first), identified (its host's HOST_UA) and, on a polite
    host, circuit-checked and challenge-classified — the same rules as a registry URL there."""
    hops = _current_hops()
    if session is None:
        session = hops.session if hops is not None else ChainSession()
    get = session.get
    hop = url
    for _ in range(MAX_REDIRECTS + 1):
        check_hop(hop)
        host = pace_key(host_policy.canonical_host(hop))
        if hops is not None and hops.paced:
            _pace_host(host)
        with _host_sem(hop):
            if why := _tripped(host):
                raise ChallengeRefused(f"challenge circuit open for {host} ({why})",
                                       requested=False)
            _wait_for_host(host)
            if why := _tripped(host):  # opened by another worker while this one waited
                raise ChallengeRefused(f"challenge circuit open for {host} ({why})",
                                       requested=False)
            programme = hops is not None and hops.robots
            if programme:  # the waits above count: recheck the budgets BEFORE requesting
                if time.monotonic() - hops.started > DOCUMENT_DEADLINE:
                    raise DeadlineExceeded(f"{DOCUMENT_DEADLINE:.0f} s document deadline spent "
                                           "before the request")
                if not PROGRAMME.admit(hops.sid):
                    raise BudgetExceeded("programme budget spent while waiting")
            capped = hops is not None and hops.max_bytes is not None
            ua = HONEST_UA if programme else HOST_UA.get(host, UA)
            if capped:
                # one watchdog over the whole hop: header reception and every body read
                import stream_guard
                try:
                    with stream_guard.Deadline(hops.started + DOCUMENT_DEADLINE,
                                               "document") as guard:
                        resp = get(hop, headers={"User-Agent": ua, "Accept": ACCEPT,
                                                 **(headers or {})},
                                   timeout=TIMEOUT, allow_redirects=False, stream=True)
                        _read_capped(resp, hops.max_bytes, hops, guard)
                except stream_guard.DeadlineExceeded as exc:
                    raise DeadlineExceeded(f"{DOCUMENT_DEADLINE:.0f} s document deadline: "
                                           f"{exc}") from exc
            else:
                resp = get(hop, headers={"User-Agent": ua, "Accept": ACCEPT, **(headers or {})},
                           timeout=TIMEOUT, allow_redirects=False)
        status = getattr(resp, "status_code", 200)
        if (host in POLITE_HOSTS or programme) and is_challenge(
                status, getattr(resp, "content", b"") or b"", fmt):
            why = f"HTTP {status} challenge"
            _trip_host(host, why, force=programme)
            raise ChallengeRefused(f"{why} (polite host: no fallback)", status)
        location = _header(resp, "location")
        if status in REDIRECT_STATUSES and location:
            hop = urljoin(getattr(resp, "url", None) or hop, location)
            continue
        return resp
    raise requests.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects from {url}")


PACED_IDS: set[str] = set()  # this run's new work of PACED_SOURCES (pace_new_hosts)
HELD_OK_IDS: set[str] = set()  # programme rows whose manifest row is ok (restored or forced)


def pace_new_hosts(todo: list[dict], manifest: dict) -> None:
    """One connection and PACED_DELAY between requests for new work of PACED_SOURCES on hosts
    without a configured cap (called before any download starts); the same defaults reach
    every redirect destination of such a download (_get_hops -> _pace_host)."""
    for src in todo:
        if src.get("source") not in PACED_SOURCES:
            continue
        if (manifest.get(src["id"]) or {}).get("status") == "ok":
            continue
        PACED_IDS.add(src["id"])
        host = host_policy.canonical_host(src["url"])
        HOST_CONCURRENCY.setdefault(host, 1)
        HOST_DELAY[host] = max(HOST_DELAY.get(host, 0.0), PACED_DELAY)


try:  # vendor-literature hosts declare their politeness delay once, in registry/vendors.json
    import find_vendor
    HOST_DELAY.update(find_vendor.host_delays(find_vendor.load_vendors()))
except Exception as exc:  # a broken vendors.json fails the contracts gate; fetching stays usable
    print(f"vendors.json host delays not applied: {exc}", file=sys.stderr)


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "missing"


def _binary_version(name: str) -> str:
    if not shutil.which(name):
        return "missing"
    try:
        out = subprocess.run(
            [name, "-v"], capture_output=True, text=True, timeout=5,
        )
        line = (out.stdout or out.stderr).splitlines()[0]
        return line.strip().replace(";", ",")
    except Exception:
        return "unknown"


# Stored on newly extracted rows. Old rows remain valid and gain it only when re-extracted.
EXTRACTOR_VERSION = (
    f"build_corpus/3;pypdf={_version('pypdf')};beautifulsoup4={_version('beautifulsoup4')};"
    f"pdftotext={_binary_version('pdftotext')}"
)

_host_sems: dict[str, threading.BoundedSemaphore] = {}
_host_sems_lock = threading.Lock()
_host_last: dict[str, float] = {}  # the last reserved request START per host group
_host_next_lock = threading.Lock()
_tripped_hosts: dict[str, str] = {}
_tripped_lock = threading.Lock()


def _trip_host(host: str, why: str, force: bool = False) -> None:
    if host not in POLITE_HOSTS and not force:
        return
    with _tripped_lock:
        if host in _tripped_hosts:
            return
        _tripped_hosts[host] = why
    print(f"challenge circuit OPEN for {host}: {why}; skipping its remaining downloads this run",
          file=sys.stderr, flush=True)


def _tripped(host: str) -> str | None:
    with _tripped_lock:
        return _tripped_hosts.get(host)


@contextlib.contextmanager
def _robots_pace(url: str):
    """A robots.txt request of a programme row runs under its host group's semaphore (held
    through the transport) and clock. check_hop — and so this — runs before the document hop
    takes the same semaphore, never inside it: no nested acquisition."""
    host = pace_key(host_policy.canonical_host(url))
    with _host_sem(url):
        _wait_for_host(host)
        yield


def _host_sem(url: str) -> threading.BoundedSemaphore:
    host = pace_key(host_policy.canonical_host(url))
    with _host_sems_lock:
        limit = HOST_CONCURRENCY.get(host, PER_HOST)
        return _host_sems.setdefault(host, threading.BoundedSemaphore(limit))


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def clean_text(s: str) -> str:
    """Drop lone surrogates etc. that pypdf sometimes emits — they crash write_text()."""
    return s.encode("utf-8", "replace").decode("utf-8")


def extract_text_plain(data: bytes) -> str:
    """Decode an already-human-readable text file (markdown / rst / txt) verbatim."""
    return data.decode("utf-8", "ignore").strip()


def extract_for(fmt: str, data: bytes) -> str:
    if fmt == "pdf":
        return extract_pdf(data)
    if fmt == "html":
        return extract_html(data)
    if fmt in TEXT_FORMATS:
        return extract_text_plain(data)
    if fmt in MARKUP_FORMATS:
        return MARKUP_FORMATS[fmt](extract_text_plain(data)).strip()
    return ""


def extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for i, page in enumerate(reader.pages):
            try:
                parts.append(page.extract_text() or "")
            except Exception as e:  # keep going on a bad page
                parts.append(f"[page {i} extract error: {e}]")
        txt = "\n\n".join(parts).strip()
    except Exception:  # broken xref/trailer — pypdf can't even open it; poppler usually can
        txt = ""
    # pdftotext (poppler) rescues three pypdf failure classes: (a) legacy scans whose OCR text
    # layer has no space glyphs ("ThermalAnalysisofEffect...", 259 NBS docs wrongly pruned 07-09),
    # (b) CID-keyed CJK fonts where pypdf extracts NOTHING (413 Japanese NILIM PDFs, 07-10), and
    # (c) subset CJK fonts where pypdf extracts PLENTY of text but maps glyphs to WRONG codepoints
    # (2019 AIJ kouzou PDFs, 07-23: "⪏ⅆ⿕そCFT..." — never trips the length/glue checks). For (c)
    # the tell is a CJK-ish doc with a depressed alpha ratio; the arbiter is which extractor
    # yields more true-CJK chars.
    if shutil.which("pdftotext"):
        if len(txt) < 500 or _word_glued(txt):
            alt = _pdftotext(data)
            if len(alt) > max(len(txt), 400) and not _word_glued(alt):
                return alt
        else:
            head = txt[:20_000]
            cjk = len(quality.CJK.findall(head))
            alpha = sum(c.isalpha() for c in head) / max(len(head), 1)
            if cjk > 50 and alpha < 0.60:
                alt = _pdftotext(data)
                if len(quality.CJK.findall(alt[:20_000])) > cjk * 1.3:
                    return alt
    return txt


def _word_glued(t: str) -> bool:
    head = t[:20_000]
    if not head:
        return False
    if len(quality.CJK.findall(head)) / len(head) > 0.10:
        return False  # CJK scripts don't space-separate — that's not gluing
    return head.count(" ") / len(head) < 0.05  # spaced prose runs ~15-18%


def _pdftotext(data: bytes) -> str:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        out = subprocess.run(["pdftotext", f.name, "-"], capture_output=True, timeout=300)
    return out.stdout.decode("utf-8", "ignore").strip() if out.returncode == 0 else ""


def extract_html(data: bytes) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(data, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    # main content: MediaWiki first, then common doc-site (sphinx/readthedocs/mkdocs) containers, else body
    main = (soup.select_one("div.mw-parser-output") or soup.select_one("[role=main]")
            or soup.select_one("main") or soup.select_one("article")
            or soup.select_one("div.body") or soup.select_one("div.document")
            or soup.select_one(".md-content") or soup.select_one(".rst-content")
            or soup.body or soup)
    # MediaWiki citation markers are <sup class="reference">. A bare ".reference" also matched
    # every Sphinx hyperlink (<a class="reference internal">), silently deleting link text from
    # readthedocs pages ("See <Running a Project> for ..." -> "See for ...").
    drop = ("sup.reference", ".mw-editsection", "table.navbox", ".navbox",
            ".vertical-navbox", ".reflist", "#toc", ".toc",
            ".navigation-not-searchable", ".hatnote", ".ambox", "table.ambox",
            ".mbox-small", ".metadata", ".sistersitebox", ".shortdescription",
            ".noprint", ".mw-empty-elt", ".mw-jump-link", "#References",
            "#External_links", "#Further_reading", "#See_also",
            # doc-site chrome (sphinx / readthedocs / mkdocs):
            "nav", "header", "footer", ".sphinxsidebar", ".wy-nav-side",
            ".toctree-wrapper", ".headerlink", ".md-sidebar", ".md-header",
            ".md-footer", ".rst-footer-buttons", ".related", "#searchbox",
            ".breadcrumbs", ".wy-breadcrumbs", "[role=navigation]")
    for sel in drop:
        for t in main.select(sel):
            t.decompose()
    text = main.get_text("\n")
    out, blanks = [], 0
    for ln in (l.strip() for l in text.splitlines()):
        if ln:
            out.append(ln)
            blanks = 0
        else:
            blanks += 1
            if blanks <= 1:
                out.append("")
    return "\n".join(out).strip()


def load_entries(view) -> list[dict]:
    """Every registry entry, in the store's order (by id). The legacy loader read shard files in
    registry order; the store keeps no file order, so per-host caps now pick by id (see
    cap_per_host)."""
    out, cursor = [], None
    while True:
        page = view.scan(store.Table.ENTRIES, cursor=cursor, limit=store.MAX_PAGE)
        out.extend(page.rows)
        if page.next_cursor is None:
            return out
        cursor = page.next_cursor


def load_manifest(view) -> dict:
    """Every manifest row by id, in the legacy manifest order (which row serves as the
    extraction template for shared bytes depends on it)."""
    return {r["id"]: r for r in corpus_stats.iter_manifest(view)}


class Checkpoints:
    """The loader's manifest writes: every CHECKPOINT_EVERY recorded results become ONE short
    store transaction (upsert_manifest of exactly the rows recorded since the previous one), named
    ckpt-0001, ckpt-0002, ... within the step — "<round>.fetch.ckpt-NNNN" through the round's
    broker — so an interrupted run loses fewer than CHECKPOINT_EVERY results and a lost reply
    can be retried exactly. Nothing is written when nothing was recorded."""

    def __init__(self, session, prefix: str = "ckpt"):
        self.session, self.prefix = session, prefix
        self.pending: dict[str, dict] = {}
        self.count = 0

    def record(self, rec: dict) -> None:
        self.pending[rec["id"]] = rec

    def flush(self) -> None:
        if not self.pending:
            return
        rows = [json.loads(json.dumps(r)) for r in self.pending.values()]  # immutable copies
        self.count += 1
        with self.session.batch(f"{self.prefix}-{self.count:04d}") as b:
            b.upsert_manifest(rows)
        self.pending.clear()


CHECKPOINT_EVERY = 25
# Rows per --reextract transaction (bounded, in manifest shard order).
REEXTRACT_BATCH_ROWS = 5_000


def _fetch_ec_deliverable(url: str) -> requests.Response:
    """EC 'Documents download module' (Horizon project deliverables, eud- ids): the stable public
    URL returns a JS interstitial whose window.location points at a session-bound tokenized URL —
    follow it with the same cookie jar to get the actual PDF."""
    with requests.Session() as s:
        s.headers.update({"User-Agent": UA})
        first = _get_hops(url, "pdf", session=s)
        if not (_header(first, "content-type") or "").startswith("text/html"):
            return first
        m = re.search(r"window\.location='(https://ec\.europa\.eu[^']+)'", first.text)
        if not m:
            return first
        return _get_hops(m.group(1), "pdf", session=s)


def _fetch_publications_gc_ca(url: str) -> requests.Response:
    """Fetch archived Government of Canada PDFs.

    publications.gc.ca redirects older documents to a bilingual archive notice.  Continuing to
    the publication requires the notice's session cookie and Referer; a stateless retry receives
    the notice forever and fails the PDF magic-byte check.
    """
    with requests.Session() as s:
        s.headers.update({"User-Agent": UA, "Accept": ACCEPT})
        first = _get_hops(url, "pdf", session=s)
        if first.content.startswith(b"%PDF-"):
            return first
        if "/site/archivee-archived.html" not in first.url:
            return first
        return _get_hops(url, "pdf", session=s, headers={"Referer": first.url})


def _new_record(src: dict) -> tuple[dict, str]:
    sid = src["id"]
    fmt = src.get("format", "pdf")
    source = src.get("source", "misc")
    ext = {"pdf": "pdf", "html": "html"}.get(
        fmt, fmt if fmt in TEXT_FORMATS or fmt in MARKUP_FORMATS else "bin")
    rec = {
        "id": sid, "title": src.get("title", sid), "url": src["url"],
        "source": source, "license": src.get("license", "unknown"),
        "topic": src.get("topic", "misc"), "format": fmt,
        "status": "failed", "http_status": None, "sha256": None, "bytes": 0,
        "raw_path": None, "text_path": None, "text_chars": 0,
        # corpus_path / corpus_chars are written by the next stage (clean_corpus.py), not here.
        "corpus_path": None, "corpus_chars": 0,
        "error": None, "fetched_at": None,
    }
    for key in registry.OPTIONAL_FIELDS:
        if src.get(key) not in (None, ""):
            rec[key] = src[key]
    return rec, ext


def _wait_for_host(host: str) -> None:
    """Reserve this host's next request start: max(now, previous start + the delay that applies
    NOW) — a delay raised meanwhile (a robots Crawl-delay learnt between two requests) is honoured
    against the previous actual start, not the delay that was in force when it was reserved."""
    delay = HOST_DELAY.get(host) or 0.0
    with _host_next_lock:
        last = _host_last.get(host)
        start = time.monotonic() if last is None else max(time.monotonic(), last + delay)
        _host_last[host] = start
    if (wait := start - time.monotonic()) > 0:
        time.sleep(wait)


def is_challenge(status: int, body: bytes, fmt: str) -> bool:
    """A polite host's refusal/captcha: challenge status, or a captcha page served as 200 —
    for a PDF row any HTML-ish challenge body, for an HTML/text row only unambiguous challenge
    markers in the head of the page (prose may mention "challenge")."""
    if status in CHALLENGE_STATUSES:
        return True
    if status != 200:
        return False
    if fmt == "pdf":
        return not body.startswith(b"%PDF-") and bool(CHALLENGE_BODY.search(body[:4000]))
    return bool(HTML_CHALLENGE_BODY.search(body[:4000]))


def _read_capped(resp, max_bytes: int, hops: "Hops | None" = None, guard=None) -> None:
    """Materialise a streamed response's decoded body, refusing it past `max_bytes` or past the
    chain's DOCUMENT_DEADLINE, and charging every chunk to the programme byte budget."""
    declared = _header(resp, "content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        resp.close()
        raise TooLarge(f"declared {declared} bytes exceeds the {max_bytes}-byte cap")
    import stream_guard

    # a watchdog enforces the deadline even when a trickling stream keeps one read blocked
    deadline = (hops.started if hops is not None else time.monotonic()) + DOCUMENT_DEADLINE
    try:
        stream_guard.read_body(
            resp, max_bytes=max_bytes, deadline=deadline, chunk=READ_CHUNK, guard=guard,
            on_chunk=(lambda n: PROGRAMME.charge(hops.sid, n)) if hops is not None else None)
    except stream_guard.BodyTooLarge as exc:
        raise TooLarge(str(exc)) from exc
    except stream_guard.DeadlineExceeded as exc:
        raise DeadlineExceeded(f"{DOCUMENT_DEADLINE:.0f} s document deadline: {exc}") from exc


def download_one(src: dict) -> dict:
    """Download and persist original bytes; every hop holds its own host slot (see _get_hops)."""
    rec, ext = _new_record(src)
    sid = rec["id"]
    fmt = rec["format"]
    source = rec["source"]
    url = src["url"]
    host = host_policy.canonical_host(url)
    programme = is_programme_row(sid)
    if programme and not PROGRAMME.admit(sid):
        rec["_deferred"] = "programme budget (bytes/time) spent this run"
        return rec
    hops = Hops(url, source in COPY_BOUND_SOURCES, paced=sid in PACED_IDS or programme,
                robots=programme,
                max_bytes=(ESEF_MAX_BYTES if sid.startswith("esf-") else PROGRAMME_MAX_BYTES)
                if programme else None, sid=sid)
    _hops.value = hops
    try:
        if "ec.europa.eu/research/participants/documents/downloadPublic" in url:
            data = _fetch_with_fallback(url, fmt, rec, _fetch_ec_deliverable(url))
        elif (host.removeprefix("www.") == "publications.gc.ca"
              and "/collections/" in urlparse(url).path):
            data = _fetch_with_fallback(url, fmt, rec, _fetch_publications_gc_ca(url))
        else:
            data = _fetch_with_fallback(url, fmt, rec)
        if compliance_common.snapshot_matches(src, data) is False:
            # upstream serves only the CURRENT consolidation: a held or restoring snapshot keeps
            # its manifest row and bytes untouched (deferred); a never-held one is superseded
            if sid in HELD_OK_IDS:
                rec["_deferred"] = ("snapshot version no longer served upstream; the held "
                                    "provenance and bytes are kept")
                _cool(sid)  # do not spend the next rounds' allocation on it again
            else:
                rec["error"] = (f"snapshot-superseded: upstream no longer serves "
                                f"{compliance_common.snapshot_token(src['url'])}")
            return rec
        if fmt == "pdf" and not data.startswith(b"%PDF-"):
            # a 200 that isn't a PDF is a WAF interstitial / captcha / error page — without this
            # check it lands in the corpus as an ok row with 0 text chars (IBPSA sgcaptcha, 07-09)
            rec["error"] = f"not-a-pdf (got {data[:12]!r})"
            if rec["http_status"] in RECOVERABLE_STATUSES:
                rec["transient"] = True  # a 202/429/503 interstitial, not a fake PDF
            return rec
        rec["sha256"] = sha256_bytes(data)
        rec["bytes"] = len(data)

        raw_dir = RAW / source
        raw_path = raw_dir / f"{sid}.{ext}"
        access = ACCESS
        if access is not None:
            # an immutable version; raw_path stays the logical name the row always carried
            art = access.local.put_bytes("raw", data)
            rec["raw_path"] = str(raw_path.relative_to(HERE))
            rec["_raw_file"] = str(access.local.path("raw", art.sha256))
            rec["_root"] = str(HERE)
            rec["_text_dir"] = str(TEXT)
            rec["_versions"] = str(access.root)
            return rec
        raw_dir.mkdir(parents=True, exist_ok=True)
        target = raw_path
        if sid in HELD_OK_IDS and raw_path.exists():
            # a refresh of a HELD programme document: stage the new bytes; they replace the held
            # raw only after extraction succeeds (settle_held), never before
            target = raw_path.with_name(raw_path.name + ".incoming")
            rec["_incoming"], rec["_final_raw"] = str(target), str(raw_path)
        target.write_bytes(data)
        rec["raw_path"] = str(raw_path.relative_to(HERE))
        # Absolute process-local paths are removed by extract_downloaded before the row can reach
        # the manifest. Carrying them makes the extraction stage safe under both fork and spawn.
        rec["_raw_file"] = str(target)
        rec["_root"] = str(HERE)
        rec["_text_dir"] = str(TEXT)
    except HostSuspended as e:
        rec["error"] = str(e)
        rec["transient"] = True
        rec["_not_requested"] = True  # the suspended host was never asked: no retry ageing
        rec["suspended_redirect"] = {"host": e.host, "url": e.url,
                                     "decided_at": e.rule.get("decided_at")}
        rec["refused_hop"] = e.url
    except CopyChanged as e:
        rec["error"] = str(e)  # a hard failure: this registry URL does not serve the licensed copy
        rec["refused_hop"] = e.url
    except (RobotsRefused, RouteRefused) as e:
        rec["error"] = str(e)  # policy: robots.txt or an unreviewed route (never requested)
        rec["refused_hop"] = e.url
    except TooLarge as e:
        rec["error"] = f"too-large: {e}"
    except BudgetExceeded as e:
        rec["_deferred"] = str(e)  # not judged: no manifest row, handed to the pruner
    except DeadlineExceeded as e:
        rec["error"] = f"deadline: {e}"
        rec["transient"] = True  # slow now is not bad forever: retried by later rounds
    except ChallengeRefused as e:
        rec["error"] = str(e)
        rec["transient"] = True
        if e.status is not None:
            rec["http_status"] = e.status
        if not e.requested:
            rec["_not_requested"] = True
    except Exception as e:
        rec["error"] = str(e)
        if isinstance(e, (requests.Timeout, requests.ConnectionError)):
            rec["error"] = f"network: {e}"
        if recoverable_failure(e, rec.get("http_status")):
            rec["transient"] = True
    finally:
        _hops.value = None
        if len(hops.chain) > 1 or rec.get("refused_hop"):
            rec["redirect_chain"] = list(hops.chain)  # the hops actually requested
            if hops.chain:
                rec["final_url"] = hops.chain[-1]
    return rec


# Recoverable failures on ANY host: the discovery cursor has usually advanced already, so these
# rows are kept and retried (prune_corpus.retry_pending) instead of being pruned. Hard failures
# (404/410, fake PDFs, non-polite 403s, TLS errors) and DNS failures (which have their own
# repeated-evidence blocklist rule in prune_corpus) keep today's behaviour.
RECOVERABLE_STATUSES = frozenset({202, 429, 503})


def recoverable_failure(exc: BaseException, status: int | None) -> bool:
    if status in RECOVERABLE_STATUSES:
        return True
    if isinstance(exc, requests.exceptions.SSLError):
        return False
    if isinstance(exc, (requests.Timeout, requests.ConnectionError, subprocess.TimeoutExpired)):
        import prune_corpus  # lazy: the pruner owns the DNS-failure classification

        return not prune_corpus._is_dns_resolution_error(str(exc))
    return False


def _fetch_with_fallback(url: str, fmt: str, rec: dict, resp=None) -> bytes:
    """requests over _get_hops, then — only when no hop is a polite host — a bounded curl retry
    on 403/410/429/503 that follows the same per-hop rules (_curl_follow)."""
    if resp is None:
        resp = _get_hops(url, fmt)
    rec["http_status"] = resp.status_code
    if resp.status_code not in (403, 410, 429, 503):
        resp.raise_for_status()
        return resp.content
    hops = _current_hops()
    if (hops is not None and hops.robots) or any(
            pace_key(host_policy.canonical_host(h)) in POLITE_HOSTS
            for h in (hops.chain if hops else [])):
        resp.raise_for_status()  # a polite host's refusal is never retried with another client
        return resp.content
    # WAFs (Akamai/Cloudflare/Google) block the python client's TLS fingerprint but pass curl's.
    # Only accept a fallback with the expected content. Do not capture curl stdout through a
    # pipe: extraction workers are spawned while downloads are active, and a fork can inherit the
    # pipe's write end, so communicate() would never observe EOF. A temporary file also avoids
    # buffering large patent HTML responses in a pipe.
    if hops is not None:
        hops.chain.clear()  # the fallback re-walks (and re-logs) the chain from the start
    body = _curl_follow(url)
    good = len(body) > 512 and (
        body[:5] == b"%PDF-" if fmt == "pdf"
        else (
            b"automated queries" not in body[:4000]
            and b"unusual traffic" not in body[:4000]
            and b"Too many requests" not in body[:4000]
            and b"too many requests" not in body[:4000]
        )
    )
    if good:
        rec["http_status"] = 200
        return body
    resp.raise_for_status()
    return resp.content


def _curl_follow(url: str, ua: str | None = None) -> bytes:
    """The curl fallback, following redirects ITSELF (no -L): each hop is checked (check_hop),
    paced (host semaphore + delay) and identified (its host's HOST_UA, else `ua`/UA); a polite
    host is never reached through curl. Returns the final body, or b"" on a curl error, a polite
    hop or too many redirects."""
    hop = url
    hops = _current_hops()
    with tempfile.TemporaryDirectory(prefix="curl-") as tmp:
        headers = Path(tmp) / "headers"
        cookies = Path(tmp) / "cookies"  # one cookie engine for the whole chain
        for _ in range(MAX_REDIRECTS + 1):
            check_hop(hop)
            host = pace_key(host_policy.canonical_host(hop))
            if host in POLITE_HOSTS:
                return b""
            if hops is not None and hops.paced:
                _pace_host(host)
            headers.write_bytes(b"")
            with _host_sem(hop), tempfile.TemporaryFile() as curl_body:
                _wait_for_host(host)
                out = subprocess.run(
                    ["curl", "-sS", "--max-time", str(TIMEOUT), "-A",
                     HOST_UA.get(host, ua or UA), "-b", str(cookies), "-c", str(cookies),
                     "-D", str(headers), hop],
                    stdout=curl_body,
                    stderr=subprocess.DEVNULL,
                    timeout=TIMEOUT + 15,
                )
                curl_body.seek(0)
                body = curl_body.read() if out.returncode == 0 else b""
            if out.returncode != 0:
                return b""
            status, location = _last_status_and_location(headers.read_bytes())
            if status in REDIRECT_STATUSES and location:
                hop = urljoin(hop, location)
                continue
            return body
    return b""


def _last_status_and_location(raw: bytes) -> tuple[int | None, str | None]:
    """Status and Location of the LAST response block curl dumped with -D."""
    status, location = None, None
    for line in raw.decode("latin-1").splitlines():
        if m := re.match(r"HTTP/\S+\s+(\d{3})", line):
            status, location = int(m.group(1)), None
        elif line.lower().startswith("location:"):
            location = line.split(":", 1)[1].strip()
    return status, location


def extract_downloaded(rec: dict) -> dict:
    """Extract one already-downloaded record. Safe to run in a separate process."""
    sid = rec["id"]
    try:
        root = Path(rec.pop("_root", HERE))
        text_dir = Path(rec.pop("_text_dir", TEXT))
        versions = rec.pop("_versions", None)
        raw_path = Path(rec.pop("_raw_file", root / rec["raw_path"]))
        data = raw_path.read_bytes()
        try:
            txt = clean_text(extract_for(rec["format"], data))
        except Exception as e:
            txt, rec["error"] = "", f"text-extract: {e}"
        if txt:
            header = (f"# {rec['title']}\n\n"
                      f"source: {rec['url']}\nlicense: {rec['license']}\n"
                      f"topic: {rec['topic']}\n\n---\n\n")
            tp = text_dir / f"{sid}.md"
            rendered = header + txt
            if versions is not None:   # a versioned staged run: text/ is never written
                artifact_store.LocalArtifacts(Path(versions)).put_bytes("text", rendered.encode())
            else:
                text_dir.mkdir(parents=True, exist_ok=True)
                tp.write_text(rendered)
            rec["text_path"] = str(tp.relative_to(root))
            rec["text_chars"] = len(txt)
            rec["text_sha256"] = sha256_bytes(rendered.encode())
            rec["extractor_version"] = EXTRACTOR_VERSION
            rec["quality"] = metrics_for(rec, txt)  # prune verdicts read this, not the file

        rec["status"] = "ok"
        rec["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    except Exception as e:
        rec["error"] = str(e)
    return rec


def metrics_for(rec: dict, txt: str) -> dict:
    """Quality metrics of one extraction; programme rows also record whether the text carries
    their instrument's identifiers (`anchor`, compliance_common.instrument_anchor)."""
    return anchored(rec, None, txt)


def anchored(rec: dict, template_metrics: dict | None, txt: str) -> dict:
    m = dict(template_metrics) if template_metrics else quality.metrics(txt)
    m.pop("anchor", None)  # a byte-identical template's anchor belongs to ITS identity
    if is_programme_row(rec.get("id", "")):
        anchor = compliance_common.instrument_anchor(rec, txt)
        if anchor is not None:
            m["anchor"] = anchor
    return m


def reuse_extraction(rec: dict, template: dict) -> dict:
    """Reuse text for identical bytes while rendering this record's own provenance header."""
    root = Path(rec.pop("_root", HERE))
    text_dir = Path(rec.pop("_text_dir", TEXT))
    versions = rec.pop("_versions", None)
    rec.pop("_raw_file", None)
    template_path = template.get("text_path")
    if versions is not None:
        source = artifact_store.VersionedAccess(Path(versions)).path(template, "text")
    else:
        source = root / template_path if template_path else None
    if template_path and source is not None and source.exists():
        txt = quality.body(source.read_text())
        if txt:
            header = (
                f"# {rec['title']}\n\n"
                f"source: {rec['url']}\nlicense: {rec['license']}\n"
                f"topic: {rec['topic']}\n\n---\n\n"
            )
            rendered = header + txt
            target = text_dir / f"{rec['id']}.md"
            if versions is not None:   # a versioned staged run: text/ is never written
                artifact_store.LocalArtifacts(Path(versions)).put_bytes("text", rendered.encode())
            else:
                text_dir.mkdir(parents=True, exist_ok=True)
                target.write_text(rendered)
            rec["text_path"] = str(target.relative_to(root))
            rec["text_chars"] = len(txt)
            rec["text_sha256"] = sha256_bytes(rendered.encode())
            rec["quality"] = anchored(rec, template.get("quality"), txt)
            if template.get("extractor_version"):
                rec["extractor_version"] = template["extractor_version"]
    rec["status"] = "ok"
    rec["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return rec


def fetch_one(src: dict) -> dict:
    """Compatibility path for callers/tests that fetch and extract one document synchronously."""
    rec = download_one(src)
    return extract_downloaded(rec) if rec.get("raw_path") else rec


def note_retry(rec: dict, previous: dict | None) -> None:
    """Carry transient-failure bookkeeping across rounds (read by prune_corpus.retry_pending).

    Only a real request starts or extends the retry window: a download skipped by an open
    challenge circuit keeps the previous count, and a candidate that was never requested keeps
    retry_attempts 0 with no first_failed_at, so it is preserved until actually attempted.
    Successful and hard-failed rows carry no retry state.
    """
    not_requested = rec.pop("_not_requested", False)
    if rec.get("status") == "ok" or not rec.get("transient"):
        rec.pop("transient", None)
        return
    previous = previous if previous and previous.get("transient") else {}
    attempts = int(previous.get("retry_attempts") or 0) + (0 if not_requested else 1)
    rec["retry_attempts"] = attempts
    if attempts:
        rec["first_failed_at"] = (previous.get("first_failed_at")
                                  or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def cap_per_host(srcs: list[dict], manifest: dict | None = None) -> tuple[list[dict], list[str]]:
    """Apply HOST_RUN_CAP to NEW work, in the order of `srcs` (the store's entry order, by id);
    return (kept sources, deferred ids).

    Restoring a previously successful row (manifest status ok, local files missing, e.g. a fresh
    clone) is never capped: it would otherwise stay "ok without text" and be pruned as no-text.
    Compliance-programme rows are the exception: their budgets bound restorations too, because
    an ok programme row without local text is "locally unavailable" (registry.
    programme_unavailable) — the cleaner does not expect it and the pruner keeps it — so a
    deferred restoration simply resumes in a later round.
    """
    manifest = manifest or {}
    counts: dict[str, int] = defaultdict(int)
    kept, deferred = [], []
    programme = esef = 0
    for src in srcs:
        host = pace_key(host_policy.canonical_host(src["url"]))
        cap = HOST_RUN_CAP.get(host)
        programme_row = is_programme_row(src["id"])
        restoring = (manifest.get(src["id"]) or {}).get("status") == "ok" and not programme_row
        if programme_row:
            # the compliance programme's own budgets (count; bytes are capped per download)
            if programme >= PROGRAMME_RUN_CAP or (
                    src["id"].startswith("esf-") and esef >= ESEF_RUN_CAP):
                deferred.append(src["id"])
                continue
        if cap is not None and not restoring:
            if counts[host] >= cap:
                deferred.append(src["id"])
                continue
            counts[host] += 1
        if programme_row:
            programme += 1
            esef += src["id"].startswith("esf-")
        kept.append(src)
    return kept, deferred


def deferred_path() -> Path:
    return HERE / "workspace" / "fetch-deferred.json"  # == ops.WORKSPACE; tests move HERE


def write_deferred(ids: list[str], path: Path | None = None) -> None:
    """Tell this round's pruner which rows were only deferred (never judged this run).

    Keyed by the round's non-empty NEKAISE_RUN_ID; a standalone load (no run id) writes nothing,
    and its pruner ignores handoffs entirely.
    """
    run_id = os.environ.get("NEKAISE_RUN_ID") or ""
    if not run_id:
        if ids:
            print(f"standalone load: {len(ids)} deferred rows (no run id, no pruner handoff)")
        return
    path = path or deferred_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ops.atomic_write_text(path, json.dumps({"run_id": run_id, "ids": sorted(ids)}) + "\n")


def fair_sources(srcs: list[dict]) -> list[dict]:
    """Round-robin sources by host so semaphore waiters cannot starve unrelated hosts."""
    queues: dict[str, deque] = defaultdict(deque)
    for src in srcs:
        queues[urlparse(src["url"]).netloc.lower()].append(src)
    ordered = []
    hosts = deque(queues)
    while hosts:
        host = hosts.popleft()
        ordered.append(queues[host].popleft())
        if queues[host]:
            hosts.append(host)
    return ordered


def reextract_selector(sources: str = "", formats: str = "", ids_from: str = "") -> dict:
    """Parse --reextract row filters into {"source": set, "format": set, "id": set} (only the
    filters that were given). An empty dict selects every eligible row, as before."""
    selection: dict[str, set[str]] = {}
    if sources.strip():
        selection["source"] = {v.strip() for v in sources.split(",") if v.strip()}
    if formats.strip():
        selection["format"] = {v.strip() for v in formats.split(",") if v.strip()}
    if ids_from.strip():
        ids = set()
        for line in Path(ids_from).read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                ids.add(line)
        if not ids:
            raise SystemExit(f"--ids-from {ids_from}: no ids")
        selection["id"] = ids
    return selection


def reextract(manifest: dict, restrictions: dict, selection: dict | None = None,
              topics: set[str] | None = None,
              touched: list | None = None) -> tuple[int, int]:
    """Re-extract text/ (and manifest text metadata) for SELECTED eligible rows from their raw
    bytes. All given filters must match (AND). Returns (docs re-extracted, total ok chars over
    the selected rows). Never downloads; rows without raw bytes are skipped. The rows it changed
    (in place) are appended to `touched`."""
    selection = selection or {}
    access = ACCESS
    if access is None:
        TEXT.mkdir(parents=True, exist_ok=True)
    chosen = [
        r for r in manifest.values()
        if registry.is_training_eligible(r, restrictions)
        and all(r.get(key) in values for key, values in selection.items())
        and (not topics or r.get("topic") in topics)
    ]
    done = 0
    for r in sorted(chosen, key=lambda x: x["id"]):
        rp = r.get("raw_path")
        if access is not None:
            raw_file = access.path(r, "raw")
            if raw_file is None:
                continue
            data = raw_file.read_bytes()
        else:
            if not rp or not (HERE / rp).exists():
                continue
            data = (HERE / rp).read_bytes()
        fmt = r.get("format", "pdf")
        try:
            txt = clean_text(extract_for(fmt, data))
        except Exception as e:
            txt, r["error"] = "", f"reextract: {e}"
        if txt:
            header = (f"# {r['title']}\n\nsource: {r['url']}\n"
                      f"license: {r['license']}\ntopic: {r['topic']}\n\n---\n\n")
            if access is not None:   # an immutable version; text/ is never written
                access.local.put_bytes("text", (header + txt).encode())
            else:
                (TEXT / f"{r['id']}.md").write_text(header + txt)
            r["text_path"] = f"text/{r['id']}.md"
            r["text_chars"] = len(txt)
            r["text_sha256"] = sha256_bytes((header + txt).encode())
            r["extractor_version"] = EXTRACTOR_VERSION
            r["quality"] = metrics_for(r, txt)
        done += 1
        if touched is not None:
            touched.append(r)
    tot = sum(r["text_chars"] for r in chosen if r.get("status") == "ok")
    return done, tot


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-fetch everything")
    ap.add_argument("--workers", type=int, default=16,
                    help="parallel downloads (host-specific caps still apply; default 16)")
    ap.add_argument(
        "--extract-workers",
        type=int,
        default=min(8, max(1, os.cpu_count() or 1)),
        help="separate PDF/text extraction processes (default min(8, CPU count))",
    )
    ap.add_argument("--only", default="", help="comma-separated topics to limit to")
    ap.add_argument("--reextract", action="store_true",
                    help="re-extract text from existing raw files; no download")
    ap.add_argument("--source", default="",
                    help="with --reextract: comma-separated source tags to limit to")
    ap.add_argument("--format", default="",
                    help="with --reextract: comma-separated registry formats (e.g. html)")
    ap.add_argument("--ids-from", default="",
                    help="with --reextract: file of document ids, one per line ('#' comments)")
    ap.add_argument("--verify", action="store_true",
                    help="re-hash local raw files against the manifest sha256; no download")
    ap.add_argument("--lock-timeout", type=float, default=30,
                    help="standalone runs wait this long for the round lock (default 30 s)")
    args = ap.parse_args()
    only = {t.strip() for t in args.only.split(",") if t.strip()}
    selection = reextract_selector(args.source, args.format, args.ids_from)
    if selection and not args.reextract:
        ap.error("--source/--format/--ids-from select rows for --reextract only")

    st = store.open(root=HERE)
    if args.verify and not args.reextract:  # read-only: a plain view (inherited in a round)
        with st.read(timeout=args.lock_timeout) as view:
            run(view, None, args, only, selection)
        return
    # Inside a round: the inherited view and the round's broker. Standalone: this command's own
    # writer — the round lock, held for the whole run, so nothing else writes between its reads
    # and its checkpoints (a round started meanwhile waits or fails, as it would on any writer).
    with store_broker.step_session(st, "fetch", timeout=args.lock_timeout) as session:
        run(session.view, session, args, only, selection)


def run(view, session, args, only: set[str], selection: dict) -> None:
    """The loader over one read view; `session` (None for --verify) receives its writes. Every
    read happens before the first write."""
    global ACCESS, HOST_POLICY
    ACCESS = artifact_store.for_view(view, HERE)
    pacing = dict(HOST_CONCURRENCY), dict(HOST_DELAY)
    try:
        if ACCESS is not None:
            ACCESS.local.sweep_incoming()
        _run(view, session, args, only, selection)
    finally:
        ACCESS = None
        HOST_POLICY = {}
        PACED_IDS.clear()
        for table, saved in zip((HOST_CONCURRENCY, HOST_DELAY), pacing):
            table.clear()
            table.update(saved)


def _have(row: dict, stage: str) -> bool:
    """Whether this machine holds the row's `stage` payload (its raw_path / text_path claim)."""
    if ACCESS is not None:
        return ACCESS.exists(row, stage)
    rel = row.get("raw_path" if stage == "raw" else "text_path")
    return bool(rel) and (HERE / rel).exists()


def _run(view, session, args, only: set[str], selection: dict) -> None:
    global HOST_POLICY
    restrictions, policy = store.pinned_policy(view)  # the view's pinned configuration
    HOST_POLICY = policy  # enforced on every request hop (check_hop)
    import robots_policy
    robots_policy.set_policy(policy)  # robots.txt redirects obey the same suspensions
    robots_policy.set_hop_filter(compliance_common.reviewed_host)
    robots_policy.set_pacer(_robots_pace)
    all_srcs = load_entries(view)
    pointer_only = sum(
        source.get("license") in registry.POINTER_ONLY_LICENSES for source in all_srcs
    )
    policy_restricted = sum(
        source.get("license") not in registry.POINTER_ONLY_LICENSES
        and registry.restriction_for(source, restrictions) is not None
        for source in all_srcs
    )
    srcs = [
        source for source in all_srcs
        if registry.is_training_eligible(source, restrictions)
    ]
    if pointer_only:
        print(f"pointer-only sources: {pointer_only} skipped by license policy")
    if policy_restricted:
        print(f"policy-restricted sources: {policy_restricted} skipped by eligibility policy")
    manifest = load_manifest(view)

    if args.reextract:
        touched: list[dict] = []
        done, tot = reextract(manifest, restrictions, selection, only, touched)
        by_id = {r["id"]: r for r in touched}
        for n, chunk in enumerate(store_broker.shard_batches(by_id, REEXTRACT_BATCH_ROWS), 1):
            with session.batch(f"reextract-{n:04d}") as b:
                b.upsert_manifest([by_id[sid] for sid in chunk])
        print(f"re-extracted {done} docs | total text {tot / 1e6:.2f} M chars")
        return

    if args.verify:
        # reproducibility check: re-hash local raw files against the committed manifest sha256.
        match = miss = mismatch = 0
        for r in manifest.values():
            if r.get("status") != "ok" or not r.get("sha256"):
                continue
            rp = r.get("raw_path")
            if not rp or not _have(r, "raw"):
                miss += 1
                continue
            raw_file = ACCESS.path(r, "raw") if ACCESS is not None else HERE / rp
            if sha256_bytes(raw_file.read_bytes()) == r["sha256"]:
                match += 1
            else:
                mismatch += 1
                print(f"  MISMATCH {r['id']}")
        n_ok = sum(1 for r in manifest.values() if r.get("status") == "ok")
        print(f"verify: {match} match | {mismatch} sha256 MISMATCH | {miss} not downloaded "
              f"(of {n_ok} ok docs in manifest)")
        return

    todo = []
    suspended: dict[str, int] = defaultdict(int)
    for s in srcs:
        if only and s.get("topic") not in only:
            continue
        cur = manifest.get(s["id"])
        if cur and cur.get("status") == "ok" and not args.force:
            if cur.get("raw_path") and _have(cur, "raw"):
                continue
        if host_policy.suspended(s["url"], policy) or host_policy.suspended_redirect(cur, policy):
            # Fetch suspension: never requested, no failure row, nothing ages toward pruning.
            suspended[urlparse(s["url"]).hostname or ""] += 1
            continue
        todo.append(s)
    if suspended:
        print(f"host fetch suspended by registry/host_policy.json (not requested): "
              f"{dict(suspended)}")

    # a held programme row whose raw bytes are here but whose text is missing is REPAIRED by
    # local re-extraction (no network, no review needed); it is never "unavailable"
    repair = sorted(r["id"] for r in manifest.values()
                    if is_programme_row(r["id"]) and r.get("status") == "ok"
                    and registry.is_training_eligible(r, restrictions)
                    and _have(r, "raw") and not _have(r, "text"))
    if repair and not args.force:
        touched: list[dict] = []
        done_repair, _ = reextract(manifest, restrictions, {"id": set(repair)}, None, touched)
        if touched:
            with session.batch("repair-text") as b:
                b.upsert_manifest([dict(r) for r in touched])
        print(f"compliance programme: re-extracted {done_repair} held documents whose text "
              "was missing locally")

    # the committed manifest's sha256 = what WE fetched; compare to detect upstream drift.
    expected = {sid: r.get("sha256") for sid, r in manifest.items() if r.get("sha256")}
    extraction_templates = {
        row["sha256"]: row
        for row in manifest.values()
        if (
            row.get("sha256")
            and row.get("status") == "ok"
            and row.get("text_path")
            and _have(row, "text")
        )
    }
    repro = drift = new = done = 0
    checkpoints = Checkpoints(session)
    print(
        f"sources: {len(srcs)} total, {len(todo)} to fetch "
        f"({'forced' if args.force else 'missing only'}, {args.workers} download workers, "
        f"{args.extract_workers} extract workers)"
    )

    def settle_held(rec: dict) -> dict | None:
        """A HELD programme row's refresh/restoration is recorded only when it produced text:
        otherwise its successful row (and held raw) stay, the failure is logged and cooled down,
        and the row is handed to the pruner as deferred. Staged bytes of a success replace the
        held raw file now."""
        incoming, final = rec.pop("_incoming", None), rec.pop("_final_raw", None)
        if rec["id"] in HELD_OK_IDS and not rec.get("text_path"):
            if incoming:
                Path(incoming).unlink(missing_ok=True)
            rec.setdefault("error", "refresh produced no text")
            _log_restore_failure(rec)
            _cool(rec["id"])
            budget_deferred.append(rec["id"])
            return None
        if incoming and final:
            os.replace(incoming, final)
        return rec

    def record_extracted(rec: dict) -> None:
        settled = settle_held(rec)
        if settled is not None:
            record_result(settled)

    def record_result(rec: dict) -> None:
        nonlocal done, repro, drift, new
        done += 1
        note_retry(rec, manifest.get(rec["id"]))
        manifest[rec["id"]] = rec
        checkpoints.record(rec)
        if rec["status"] == "ok":
            exp = expected.get(rec["id"])
            tag = "reproduced" if exp == rec["sha256"] else ("DRIFTED" if exp else "new")
            repro += exp == rec["sha256"]
            drift += bool(exp) and exp != rec["sha256"]
            new += not exp
            print(
                f"[{done}/{len(todo)}] {rec['id']}  ok  {rec['bytes'] // 1024}KB  "
                f"{rec['text_chars']} chars  [{tag}]",
                flush=True,
            )
        else:
            print(
                f"[{done}/{len(todo)}] {rec['id']}  FAIL http={rec['http_status']} "
                f"{rec.get('error')}",
                flush=True,
            )
        if done % CHECKPOINT_EVERY == 0:
            checkpoints.flush()  # an interrupted run loses <25 extractions

    documents = view.config_get().documents  # the programme configuration pinned with the rows
    # an access-terms review older than RIGHTS_REVIEW_DAYS stops EVERY network request of the
    # source — new work, retries, restorations and --force refreshes alike; held artifacts stay
    stale = [s["id"] for s in todo if is_programme_row(s["id"])
             and compliance_common.review_due_for_row(s, documents)]
    if stale:
        print(f"compliance programme: {len(stale)} rows wait for an access-terms re-review "
              f"(older than {compliance_common.RIGHTS_REVIEW_DAYS} days; not requested)")
    cooling = [s["id"] for s in todo if s["id"] in _cooldowns()]
    if cooling:
        print(f"compliance programme: {len(cooling)} held snapshots whose version is no longer "
              f"served upstream are not retried before {COOLDOWN_DAYS} days")
    skip = set(stale) | set(cooling)
    todo = [s for s in todo if s["id"] not in skip]
    stale = sorted(skip)
    todo, deferred_ids = cap_per_host(todo, manifest)
    deferred_ids = [*deferred_ids, *stale]
    pace_new_hosts(todo, manifest)
    HELD_OK_IDS.clear()
    HELD_OK_IDS.update(s["id"] for s in todo if is_programme_row(s["id"])
                       and (manifest.get(s["id"]) or {}).get("status") == "ok")
    PROGRAMME.reset()
    write_deferred(deferred_ids)
    if deferred_ids:
        print(f"deferred by per-run host caps (left in the registry for later rounds): "
              f"{len(deferred_ids)} sources")
    ordered = fair_sources(todo)
    with (
        ThreadPoolExecutor(max_workers=max(1, args.workers)) as downloads,
        # Spawned workers cannot inherit sockets or subprocess bookkeeping pipes from active
        # download threads.  Forking the pool lazily while curl/requests work is in flight can
        # otherwise keep an internal Popen pipe alive forever after its child exits.
        ProcessPoolExecutor(
            max_workers=max(1, args.extract_workers),
            mp_context=EXTRACTION_CONTEXT,
        ) as extractors,
    ):
        download_futures = {downloads.submit(download_one, src) for src in ordered}
        budget_deferred: list[str] = []
        extract_futures = set()
        extract_sha = {}
        waiting_by_sha: dict[str, list[dict]] = defaultdict(list)
        # Drain both stages continuously: extraction overlaps downloads, progress remains visible,
        # and the manifest checkpoint still bounds hard-kill rework to fewer than 25 completions.
        while download_futures or extract_futures:
            completed, _ = wait(
                download_futures | extract_futures,
                return_when=FIRST_COMPLETED,
            )
            for future in completed:
                if future in download_futures:
                    download_futures.remove(future)
                    rec = future.result()
                    if not rec.get("_deferred") and not rec.get("raw_path") \
                            and rec["id"] in HELD_OK_IDS:
                        # a restoration/refresh of a HELD programme document failed: its
                        # successful provenance row stays; the failure is logged, cooled down
                        _log_restore_failure(rec)
                        _cool(rec["id"])
                        rec["_deferred"] = f"restoration failed: {rec.get('error')}"
                    if rec.get("_deferred"):
                        budget_deferred.append(rec["id"])  # never judged: no manifest change
                        continue
                    if rec.get("raw_path"):
                        digest = rec["sha256"]
                        if digest in extraction_templates:
                            record_extracted(reuse_extraction(rec, extraction_templates[digest]))
                        elif digest in extract_sha.values():
                            waiting_by_sha[digest].append(rec)
                        else:
                            extract_future = extractors.submit(extract_downloaded, rec)
                            extract_futures.add(extract_future)
                            extract_sha[extract_future] = digest
                    else:
                        record_result(rec)
                else:
                    extract_futures.remove(future)
                    digest = extract_sha.pop(future)
                    extracted = future.result()
                    extraction_templates[digest] = extracted
                    record_extracted(extracted)
                    for duplicate in waiting_by_sha.pop(digest, []):
                        record_extracted(reuse_extraction(duplicate, extracted))
    checkpoints.flush()
    if budget_deferred:
        write_deferred(sorted(set(deferred_ids) | set(budget_deferred)))
        print(f"deferred by the programme byte/time budget (left in the registry for later "
              f"rounds): {len(budget_deferred)} sources")

    seen: dict = {}
    for r in manifest.values():
        if r.get("sha256"):
            seen.setdefault(r["sha256"], []).append(r["id"])
    dups = {h: ids for h, ids in seen.items() if len(ids) > 1}

    ok = sum(1 for r in manifest.values() if r["status"] == "ok")
    eligible_ok = [
        r for r in manifest.values()
        if r["status"] == "ok" and registry.is_training_eligible(r, restrictions)
    ]
    by_topic: dict = {}
    for r in eligible_ok:
        by_topic[r["topic"]] = by_topic.get(r["topic"], 0) + 1
    print(f"\nmanifest: {len(manifest)} rows | {ok} ok | {len(manifest) - ok} failed")
    print(f"training eligible: {len(eligible_ok)} | policy restricted: {policy_restricted}")
    print("eligible by topic:", by_topic)
    if repro or drift or new:
        print(f"reproducibility vs manifest: {repro} reproduced (sha256 match) | "
              f"{drift} DRIFTED (source changed) | {new} new")
    if dups:
        print("duplicate bytes (same sha256):", dups)


if __name__ == "__main__":
    main()
