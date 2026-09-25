#!/usr/bin/env python3
"""polite_http.py — the discovery-side HTTP client of the compliance/ESG programme finders.

Every request a programme finder makes (find_boverket --mode bfs, find_regdocs, find_eurlex,
find_esef) goes through get(): per hop, BEFORE the request,
  * the pinned host policy (registry/host_policy.json) — a suspended host is never asked;
  * robots.txt (scripts/robots_policy.py) — a disallowed path is refused, an unavailable
    robots.txt defers the finder (Deferred: the caller writes a rotation HOLD);
  * pacing — one request per host at max(configured delay, robots Crawl-delay) between starts;
then an honest User-Agent, manual redirects (each hop re-checked), a decoded-body byte cap
(16 MiB by default) and a challenge check: an HTML answer where the caller expected XML/JSON/
PDF, or a refusal status (401/403/429/503), is a Deferred, never data.
"""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import urljoin

import requests

import host_policy
import robots_policy

UA = {"User-Agent": "nekaise-corpus/compliance-discovery (research corpus; robots.txt honoured)"}
MAX_BYTES = 16 * 1024 * 1024
MAX_REDIRECTS = 8
TIMEOUT = (10, 45)
REFUSAL_STATUSES = frozenset({401, 403, 429, 503, 202})
REDIRECTS = frozenset({301, 302, 303, 307, 308})
CHALLENGE_BODY = re.compile(
    rb"captcha|challenge-platform|cf-chl|awswaf|request rejected|access denied|"
    rb"please enable javascript and cookies", re.I)

_next: dict[str, float] = {}
_lock = threading.Lock()
POLICY: dict[str, dict] = {}   # set by the finder from its store view (store.pinned_policy)


class Deferred(RuntimeError):
    """Access could not be established now (robots unavailable, refusal, challenge): hold."""


class Refused(RuntimeError):
    """Policy forbids the request (suspended host or robots Disallow): never retried blindly."""


class TooLarge(RuntimeError):
    """The response exceeded the byte cap."""


def set_policy(policy: dict[str, dict]) -> None:
    POLICY.clear()
    POLICY.update(policy or {})


def _pace(host: str, delay: float) -> None:
    if delay <= 0:
        return
    with _lock:
        start = max(time.monotonic(), _next.get(host, 0.0))
        _next[host] = start + delay
    wait = start - time.monotonic()
    if wait > 0:
        time.sleep(wait)


def check(url: str) -> float:
    """Policy + robots gate for one hop; returns the robots Crawl-delay (0 when none)."""
    if rule := host_policy.suspended(url, POLICY):
        raise Refused(f"{host_policy.canonical_host(url)} is fetch-suspended "
                      f"(registry/host_policy.json, {rule.get('decided_at')})")
    try:
        ok, delay = robots_policy.decision(url)
    except robots_policy.RobotsUnavailable as exc:
        raise Deferred(str(exc)) from exc
    if not ok:
        raise Refused(f"robots.txt disallows {url}")
    return float(delay or 0.0)


def get(url: str, *, delay: float = 1.0, expect: str = "any", max_bytes: int = MAX_BYTES,
        headers: dict | None = None, params: dict | None = None,
        not_found_ok: bool = False) -> requests.Response | None:
    """GET with the programme's per-hop checks. `expect`: "xml" | "json" | "html" | "pdf" |
    "text" | "any" — an HTML body where XML/JSON/PDF/text was expected is a Deferred (a WAF or
    login page is never parsed as data). Returns None for 404/410 when not_found_ok."""
    hop = url
    first = True
    for _ in range(MAX_REDIRECTS + 1):
        robots_delay = check(hop)
        host = host_policy.canonical_host(hop)
        _pace(host, max(delay, robots_delay))
        resp = requests.get(hop, headers={**UA, **(headers or {})},
                            params=params if first else None,
                            timeout=TIMEOUT, allow_redirects=False, stream=True)
        first = False
        status = resp.status_code
        if status in REDIRECTS and resp.headers.get("location"):
            hop = urljoin(resp.url or hop, resp.headers["location"])
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
        body = bytearray()
        for chunk in resp.iter_content(65536):
            body.extend(chunk)
            if len(body) > max_bytes:
                resp.close()
                raise TooLarge(f"{hop}: body exceeds {max_bytes} bytes")
        resp._content = bytes(body)  # noqa: SLF001 — materialise the capped stream once
        resp._content_consumed = True  # noqa: SLF001
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
