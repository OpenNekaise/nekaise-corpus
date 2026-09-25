#!/usr/bin/env python3
"""robots_policy.py — robots.txt permission and Crawl-delay for the compliance/ESG programme.

The compliance programme (find_boverket --mode bfs, find_regdocs, find_eurlex, find_esef and the
loader rows they register — ids `bov-bfs-`, `reg-`, `eur-`, `esf-`) checks robots.txt BEFORE every
discovery request and every download hop (Codex decision 2026-09-25). Other veins keep their own
reviewed rules (the vendor-literature exception in AGENTS.md is deliberately untouched).

Semantics (RFC 9309 plus the Google extensions every host we meet uses):
  * the group whose user-agent token is a case-insensitive prefix/substring of our product token
    (`nekaise-corpus`) wins over `*`; several matching groups are merged;
  * the longest matching rule wins, `Allow` beats `Disallow` on a tie; `*` wildcards and a
    trailing `$` anchor are honoured;
  * `Crawl-delay` of the selected group is returned and callers pace at max(configured, robots);
  * 404/410 = no robots.txt (everything allowed);
  * 401/403/429, 5xx, a challenge page, a timeout or a network error = UNAVAILABLE: the caller
    defers (no request is made to the protected URL; the discovery cursor/loader row is held,
    never marked failed or exhausted).
Paths are compared in ONE normal form (normalize_path: percent-escapes of unreserved characters
decoded exactly as requests' requote_uri does, other escapes upper-cased, non-ASCII UTF-8
percent-encoded), so `/%70rivate/x` cannot slip past `Disallow: /private/`.

The robots.txt request itself follows at most MAX_REDIRECTS redirects MANUALLY, each hop checked
against the pinned host policy (set_policy) and — when a hop filter is installed (set_hop_filter,
the programme's reviewed hosts) — against it; a refused hop makes robots UNAVAILABLE (defer).
Requests are paced through an installed pacer (set_pacer: the caller's per-host clock) and
concurrent lookups of one origin are coalesced (one fetch, per-origin lock).
Results are cached per origin for CACHE_TTL (24 h) in memory and under workspace/robots-cache/
(scratch; losing it only costs one refetch).
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests

import ops

UA_TOKEN = "nekaise-corpus"
UA = {"User-Agent": "nekaise-corpus/robots (research corpus; honours robots.txt and Crawl-delay)"}
CACHE_DIR = ops.WORKSPACE / "robots-cache"
CACHE_TTL = 24 * 3600
TIMEOUT = (10, 30)
MAX_ROBOTS_BYTES = 512 * 1024
CHALLENGE = re.compile(rb"<html|captcha|challenge|cf-chl|awswaf|request rejected|access denied",
                       re.I)

MAX_REDIRECTS = 5
ROBOTS_DEADLINE = 30.0
REDIRECTS = frozenset({301, 302, 303, 307, 308})
_memory: dict[str, dict] = {}
_lock = threading.Lock()
_origin_locks: dict[str, threading.Lock] = {}
POLICY: dict[str, dict] = {}
_pacer = None      # callable(url) -> context manager held through one robots request
_hop_filter = None  # callable(url) -> bool, whether a robots redirect hop may be requested


def set_policy(policy: dict[str, dict]) -> None:
    POLICY.clear()
    POLICY.update(policy or {})


def set_pacer(pacer) -> None:
    global _pacer
    _pacer = pacer


def set_hop_filter(hop_filter) -> None:
    global _hop_filter
    _hop_filter = hop_filter


class RobotsUnavailable(RuntimeError):
    """robots.txt could not be established (refusal, challenge, 5xx, network): defer."""


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme or 'https'}://{(parts.netloc or '').lower()}"


# ------------------------------------------------------------------------------------ parsing
def parse(text: str) -> list[dict]:
    """robots.txt -> [{"agents": [...], "rules": [(allow, pattern)], "delay": float|None}]."""
    groups: list[dict] = []
    current: dict | None = None
    last_was_agent = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if current is None or not last_was_agent:
                current = {"agents": [], "rules": [], "delay": None}
                groups.append(current)
            current["agents"].append(value.lower())
            last_was_agent = True
            continue
        last_was_agent = False
        if current is None:
            continue
        if key in ("allow", "disallow"):
            if key == "disallow" and not value:
                continue  # "Disallow:" (empty) allows everything
            current["rules"].append((key == "allow", value))
        elif key == "crawl-delay":
            try:
                current["delay"] = float(value)
            except ValueError:
                pass
    return groups


def _select(groups: list[dict], token: str = UA_TOKEN) -> tuple[list[tuple[bool, str]], float | None]:
    token = token.lower()
    named = [g for g in groups if any(a != "*" and a and a in token for a in g["agents"])]
    chosen = named or [g for g in groups if "*" in g["agents"]]
    rules = [r for g in chosen for r in g["rules"]]
    delays = [g["delay"] for g in chosen if g["delay"] is not None]
    return rules, (max(delays) if delays else None)


_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def normalize_path(path: str) -> str:
    """One normal form for rule patterns and request paths (see module docstring)."""
    def esc(m: re.Match) -> str:
        ch = chr(int(m.group(1), 16))
        return ch if ch in _UNRESERVED else "%" + m.group(1).upper()
    path = re.sub(r"%([0-9A-Fa-f]{2})", esc, path)
    path = _remove_dot_segments(path)
    return "".join(c if ord(c) < 128 else "".join(f"%{b:02X}" for b in c.encode("utf-8"))
                   for c in path)


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 section 5.2.4 on the path part (the query is left alone)."""
    head, sep, query = path.partition("?")
    if "." not in head:
        return path
    out: list[str] = []
    for seg in head.split("/"):
        if seg == "..":
            if len(out) > 1:
                out.pop()
        elif seg != ".":
            out.append(seg)
    fixed = "/".join(out)
    if head.endswith(("/.", "/..")):
        fixed += "/"
    return (fixed or "/") + sep + query


def _pattern_regex(pattern: str) -> re.Pattern:
    anchored = pattern.endswith("$")
    body = normalize_path(pattern[:-1] if anchored else pattern)
    rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile(rx + ("$" if anchored else ""))


def _path_of(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path or "/"
    return normalize_path(path + (f"?{parts.query}" if parts.query else ""))


def permits(rules: list[tuple[bool, str]], url: str) -> bool:
    """Longest-match rule decision; Allow wins ties; no matching rule = allowed."""
    path = _path_of(url)
    best: tuple[int, bool] | None = None
    for allow, pattern in rules:
        if not pattern:
            continue
        if _pattern_regex(pattern).match(path):
            # specificity of the NORMALIZED pattern (as matched), not of its spelling
            length = len(normalize_path(pattern.rstrip("$")).replace("*", ""))
            if best is None or length > best[0] or (length == best[0] and allow):
                best = (length, allow)
    return True if best is None else best[1]


# ------------------------------------------------------------------------------------ fetching
def _cache_file(org: str) -> Path:
    return CACHE_DIR / (re.sub(r"[^a-z0-9.-]+", "_", org.lower()) + ".json")


def _fetch(org: str, fetcher=None) -> dict:
    """{"status": "ok"|"absent", "text": str, "fetched_at": float} or raises RobotsUnavailable."""
    url = org + "/robots.txt"
    try:
        if fetcher is not None:
            status, body = fetcher(url)
        else:
            status, body = _get_manual(url)
    except requests.RequestException as exc:
        raise RobotsUnavailable(f"{url}: network error {exc}") from exc
    if status in (404, 410):
        return {"status": "absent", "text": "", "fetched_at": time.time()}
    if status != 200:
        raise RobotsUnavailable(f"{url}: HTTP {status}")
    head = body[:2000].lstrip()
    if head[:1] == b"<" and CHALLENGE.search(head):
        raise RobotsUnavailable(f"{url}: HTML/challenge page instead of robots.txt")
    return {"status": "ok", "text": body.decode("utf-8", "replace"), "fetched_at": time.time()}


def _hop_allowed(url: str) -> bool:
    import host_policy
    if host_policy.suspended(url, POLICY):
        return False
    return _hop_filter is None or bool(_hop_filter(url))


def _get_manual(url: str) -> tuple[int, bytes]:
    """GET robots.txt following redirects by hand; every hop policy-checked and paced."""
    from urllib.parse import urljoin
    import stream_guard

    hop = url
    deadline = time.monotonic() + ROBOTS_DEADLINE
    for _ in range(MAX_REDIRECTS + 1):
        if not _hop_allowed(hop):
            raise RobotsUnavailable(f"robots.txt redirect to a refused host: {hop}")
        from contextlib import nullcontext
        with (_pacer(hop) if _pacer is not None else nullcontext()):
            r = requests.get(hop, headers=UA, timeout=TIMEOUT, allow_redirects=False,
                             stream=True)
            if r.status_code in REDIRECTS and r.headers.get("location"):
                hop = urljoin(hop, r.headers["location"])
                r.close()
                continue
            if r.status_code != 200:
                r.close()
                return r.status_code, b""
            try:  # a hard total deadline, even on a trickling answer
                body = stream_guard.read_body(r, max_bytes=MAX_ROBOTS_BYTES, deadline=deadline)
            except (stream_guard.BodyTooLarge, stream_guard.DeadlineExceeded) as exc:
                raise RobotsUnavailable(f"{hop}: {exc}") from exc
        return r.status_code, body
    raise RobotsUnavailable(f"{url}: more than {MAX_REDIRECTS} redirects")


def load(url: str, fetcher=None, *, use_disk: bool = True) -> dict:
    """The cached robots record for url's origin (fetched when missing or older than 24 h)."""
    org = origin(url)
    now = time.time()
    with _lock:
        rec = _memory.get(org)
    if rec and now - rec["fetched_at"] < CACHE_TTL:
        return rec
    if use_disk and fetcher is None:
        path = _cache_file(org)
        try:
            rec = json.loads(path.read_text())
            if now - rec["fetched_at"] < CACHE_TTL:
                with _lock:
                    _memory[org] = rec
                return rec
        except (OSError, ValueError, KeyError):
            pass
    with _lock:
        origin_lock = _origin_locks.setdefault(org, threading.Lock())
    with origin_lock:  # concurrent cold lookups of one origin: one fetch, the rest wait
        with _lock:
            rec = _memory.get(org)
        if rec and time.time() - rec["fetched_at"] < CACHE_TTL:
            return rec
        rec = _fetch(org, fetcher)
        with _lock:
            _memory[org] = rec
    if use_disk and fetcher is None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            ops.atomic_write_text(_cache_file(org), json.dumps(rec))
        except OSError:
            pass
    return rec


def decision(url: str, fetcher=None) -> tuple[bool, float | None]:
    """(allowed, crawl delay) for url; raises RobotsUnavailable (callers defer)."""
    rec = load(url, fetcher)
    if rec["status"] == "absent":
        return True, None
    rules, delay = _select(parse(rec["text"]))
    return permits(rules, url), delay


def allowed(url: str, fetcher=None) -> bool:
    return decision(url, fetcher)[0]


def crawl_delay(url: str, fetcher=None) -> float | None:
    return decision(url, fetcher)[1]


def clear_memory() -> None:
    """Tests: forget the in-process cache."""
    with _lock:
        _memory.clear()
