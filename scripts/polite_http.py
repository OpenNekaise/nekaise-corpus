#!/usr/bin/env python3
"""polite_http.py — the discovery-side HTTP client of the compliance/ESG programme finders.

Every request a programme finder makes (find_boverket --mode bfs, find_regdocs, find_eurlex,
find_esef) goes through get() / head(): per hop, on the PREPARED URL (query parameters included,
exactly what requests will send) and BEFORE the request,
  * the hop must be on a reviewed programme host (compliance_common.PROGRAMME_HOSTS) — any other
    destination (an unexpected CDN, a login portal) is refused;
  * the pinned host policy (registry/host_policy.json) — a suspended host is never asked;
  * robots.txt (scripts/robots_policy.py) — a disallowed path is refused, an unavailable
    robots.txt defers the finder (Deferred: the caller writes a rotation HOLD);
  * pacing — one clock per host GROUP (aliases share it: compliance_common.pace_key), at
    max(configured delay, programme host delay, robots Crawl-delay) between request starts; the
    robots.txt requests themselves go through the same clock;
then an honest User-Agent, manual redirects (each hop re-checked), a decoded-body byte cap
(16 MiB by default), a total deadline, and a challenge check: an HTML answer where the caller
expected XML/JSON/PDF/text, a challenge page, or a refusal status (401/403/429/503/202) is a
Deferred, never data.
"""
from __future__ import annotations

import contextlib
import re
import threading
import time
from urllib.parse import urljoin

import requests

import compliance_common
import host_policy
import robots_policy
import stream_guard

UA = {"User-Agent": "nekaise-corpus/compliance-discovery (research corpus; robots.txt honoured)"}
MAX_BYTES = 16 * 1024 * 1024
MAX_REDIRECTS = 8
TIMEOUT = (10, 45)
DEADLINE = 180.0
REFUSAL_STATUSES = frozenset({401, 403, 429, 503, 202})
REDIRECTS = frozenset({301, 302, 303, 307, 308})
CHALLENGE_BODY = re.compile(
    rb"captcha|challenge-platform|cf-chl|awswaf|request rejected|access denied|"
    rb"please enable javascript and cookies", re.I)

_last: dict[str, float] = {}  # last reserved request START per host group
_lock = threading.Lock()
POLICY: dict[str, dict] = {}   # set by the finder from its store view (store.pinned_policy)


class Deferred(RuntimeError):
    """Access could not be established now (robots unavailable, refusal, challenge): hold."""


class Refused(RuntimeError):
    """Policy forbids the request (unreviewed or suspended host, robots Disallow)."""


class TooLarge(RuntimeError):
    """The response exceeded the byte cap (or the total deadline)."""


def set_policy(policy: dict[str, dict]) -> None:
    POLICY.clear()
    POLICY.update(policy or {})
    robots_policy.set_policy(policy)
    robots_policy.set_pacer(_robots_slot)
    robots_policy.set_hop_filter(compliance_common.reviewed_host)


def _group(url: str) -> str:
    return compliance_common.pace_key(host_policy.canonical_host(url))


def pace(url: str, delay: float) -> None:
    """Wait for this host group's clock: the next start is max(now, previous START + the delay
    that applies NOW = max(delay, the group's programme delay, any robots Crawl-delay learnt))."""
    group = _group(url)
    delay = max(delay, compliance_common.PROGRAMME_HOSTS.get(group, (0.0, 0))[0],
                _crawl_delay.get(group, 0.0))
    with _lock:
        last = _last.get(group)
        start = time.monotonic() if last is None else max(time.monotonic(), last + delay)
        _last[group] = start
    wait = start - time.monotonic()
    if wait > 0:
        time.sleep(wait)


_crawl_delay: dict[str, float] = {}  # robots Crawl-delay learnt per host group


@contextlib.contextmanager
def _robots_slot(url: str):
    """robots.txt requests take the same host-group clock as payload requests."""
    pace(url, 0.0)
    yield


def prepared(url: str, params: dict | None = None) -> str:
    """The exact URL requests will send (parameters encoded, unreserved escapes requoted)."""
    return requests.Request("GET", url, params=params).prepare().url


def check(url: str) -> float:
    """Reviewed-host + policy + robots gate for one hop; returns the robots Crawl-delay."""
    if not compliance_common.reviewed_host(url):
        raise Refused(f"{host_policy.canonical_host(url)} is not a reviewed programme host")
    if rule := host_policy.suspended(url, POLICY):
        raise Refused(f"{host_policy.canonical_host(url)} is fetch-suspended "
                      f"(registry/host_policy.json, {rule.get('decided_at')})")
    try:
        ok, delay = robots_policy.decision(url)
    except robots_policy.RobotsUnavailable as exc:
        raise Deferred(str(exc)) from exc
    if not ok:
        raise Refused(f"robots.txt disallows {url}")
    if delay:
        group = _group(url)
        with _lock:
            _crawl_delay[group] = max(_crawl_delay.get(group, 0.0), float(delay))
    return float(delay or 0.0)


def get(url: str, *, delay: float = 1.0, expect: str = "any", max_bytes: int = MAX_BYTES,
        headers: dict | None = None, params: dict | None = None,
        not_found_ok: bool = False, prefix: int | None = None) -> requests.Response | None:
    """GET with the programme's per-hop checks. `expect`: "xml" | "json" | "html" | "pdf" |
    "text" | "any" — an HTML body where XML/JSON/PDF/text was expected is a Deferred (a WAF or
    login page is never parsed as data). Returns None for 404/410 when not_found_ok.
    `prefix`: read only the first `prefix` bytes (a version probe) and close the stream."""
    hop = prepared(url, params)
    started = time.monotonic()
    for _ in range(MAX_REDIRECTS + 1):
        robots_delay = check(hop)
        pace(hop, max(delay, robots_delay))
        if time.monotonic() - started > DEADLINE:  # the waits count: never request late
            raise Deferred(f"{hop}: the {DEADLINE:.0f} s request deadline passed while waiting")
        guard = stream_guard.Deadline(started + DEADLINE, hop)
        try:
            with guard:  # headers under the same watchdog as the body
                resp = requests.get(hop, headers={**UA, **(headers or {})},
                                    timeout=TIMEOUT, allow_redirects=False, stream=True)
        except stream_guard.DeadlineExceeded as exc:
            raise Deferred(f"{hop}: {exc}") from exc
        status = resp.status_code
        if status in REDIRECTS and resp.headers.get("location"):
            hop = prepared(urljoin(resp.url or hop, resp.headers["location"]))
            resp.close()
            continue
        if status in (404, 410) and not_found_ok:
            resp.close()
            return None
        if status in REFUSAL_STATUSES:
            resp.close()
            raise Deferred(f"HTTP {status} from {hop}")
        if status >= 400:
            resp.close()
            resp.raise_for_status()
        try:
            body = stream_guard.read_body(resp, max_bytes=max_bytes, deadline=started + DEADLINE,
                                          prefix=prefix)  # a guard of its own, same deadline
        except stream_guard.BodyTooLarge as exc:
            raise TooLarge(f"{hop}: {exc}") from exc
        except stream_guard.DeadlineExceeded as exc:
            raise Deferred(f"{hop}: {exc}") from exc  # slow now: retry later, never data
        ctype = (resp.headers.get("content-type") or "").lower()
        head = bytes(body[:4000]).lstrip()
        looks_html = "text/html" in ctype or head[:15].lower().startswith((b"<!doctype html",
                                                                            b"<html"))
        if expect in ("xml", "json", "pdf", "text") and looks_html:
            raise Deferred(f"{hop}: HTML page where {expect} was expected (login/challenge?)")
        if expect == "pdf" and not body.startswith(b"%PDF-"):
            raise Deferred(f"{hop}: not a PDF")
        if looks_html and CHALLENGE_BODY.search(head):
            raise Deferred(f"{hop}: challenge page")
        return resp
    raise Deferred(f"more than {MAX_REDIRECTS} redirects from {url}")


def head(url: str, *, delay: float = 1.0) -> requests.Response:
    """HEAD with the same per-hop gate (no redirects followed: a 3xx is returned as is)."""
    hop = prepared(url)
    started = time.monotonic()
    robots_delay = check(hop)
    pace(hop, max(delay, robots_delay))
    if time.monotonic() - started > DEADLINE:
        raise Deferred(f"{hop}: the {DEADLINE:.0f} s request deadline passed while waiting")
    try:
        with stream_guard.Deadline(started + DEADLINE, hop):
            return requests.head(hop, headers=UA, timeout=TIMEOUT, allow_redirects=False)
    except stream_guard.DeadlineExceeded as exc:
        raise Deferred(f"{hop}: {exc}") from exc
