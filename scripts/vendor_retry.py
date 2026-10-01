"""Vendor connection-outage state, committed with rotation by the discovery transaction.

The finder exchanges an isolated report with run_round; workspace caches are never the
authority for deferred pages. Only ConnectTimeout trips the breaker. HTTP/read failures
retain their existing handling and never acquire a connection backoff.
"""
from __future__ import annotations

import copy
import math
import time
from urllib.parse import urlsplit

import requests

INPUT_ENV = "NEKAISE_VENDOR_RETRY_INPUT"
OUTPUT_ENV = "NEKAISE_VENDOR_RETRY_OUTPUT"
THRESHOLD = 3
CONNECT_TIMEOUT = 15
INITIAL_BACKOFF = 3600
MAX_BACKOFF = 86400
ATTENTION_AFTER = 7 * 86400


class Deferred(RuntimeError):
    pass


def validate_hosts(hosts):
    if not isinstance(hosts, dict):
        raise ValueError("vendor retry hosts must be an object")
    for host, state in hosts.items():
        if not isinstance(host, str) or not host or not isinstance(state, dict):
            raise ValueError("invalid vendor retry host")
        for field in ("failures", "first_failure_at", "next_retry_at"):
            value = state.get(field)
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or value < 0):
                raise ValueError(f"invalid vendor retry {field}")
        if not isinstance(state["failures"], int):
            raise ValueError("invalid vendor retry failures")
        pages = state.get("pages")
        if not isinstance(pages, list) or any(
                not isinstance(p, str) or urlsplit(p).scheme not in ("https", "http")
                or urlsplit(p).netloc != host for p in pages):
            raise ValueError("invalid vendor retry pages")


class Connections:
    def __init__(self, hosts, fetcher, clock=None):
        validate_hosts(hosts)
        self.hosts = copy.deepcopy(hosts)
        self.fetcher = fetcher
        self.clock = clock or time.time
        self.streaks = {}
        self.blocked = set()

    def defer(self, host):
        now = self.clock()
        old = self.hosts.get(host, {})
        failures = old.get("failures", 0) + 1
        self.hosts[host] = {
            "failures": failures,
            "first_failure_at": old.get("first_failure_at") or now,
            "next_retry_at": now + min(MAX_BACKOFF, INITIAL_BACKOFF * 2 ** min(failures - 1, 10)),
            "pages": old.get("pages", []),
        }
        self.blocked.add(host)

    def __call__(self, url, *args, **kwargs):
        host = urlsplit(url).netloc
        old = self.hosts.get(host, {})
        if host in self.blocked or old.get("next_retry_at", 0) > self.clock():
            self.blocked.add(host)
            raise Deferred(f"{host}: connection retry deferred until {old.get('next_retry_at')}")
        try:
            result = self.fetcher(url, *args, **kwargs)
        except requests.ConnectTimeout:
            self.streaks[host] = self.streaks.get(host, 0) + 1
            # A due retry is one probe; a newly failing host gets at most three attempts.
            if old.get("failures", 0) or self.streaks[host] >= THRESHOLD:
                self.defer(host)
                raise Deferred(f"{host}: connection breaker opened")
            raise
        except Exception:
            self.streaks[host] = 0
            raise
        self.streaks[host] = 0
        if old:
            old.update(failures=0, first_failure_at=0, next_retry_at=0)
        return result

    def finish_pages(self, pages, visited, fresh):
        # Save every unfinished page on a deferred host, including the first failed attempts
        # and pages beyond this round's scan budget. This survives cache loss/sitemap churn.
        for host, state in list(self.hosts.items()):
            pending = dict.fromkeys(state["pages"] + [p for p in pages
                                                     if urlsplit(p).netloc == host])
            state["pages"] = [p for p in pending if visited.get(p, 0) < fresh]
            if not state["pages"] and not state["failures"]:
                del self.hosts[host]


def apply_report(entry, report):
    """Validate the report against its parent cursor, then return one atomic rotation value."""
    import rotation
    if (not isinstance(report, dict) or report.get("cursor") != entry["next"]
            or not isinstance(report.get("vendor"), str) or not report["vendor"]):
        raise ValueError("invalid or stale vendor retry report")
    validate_hosts(report.get("hosts"))
    result = rotation.advanced("find_vendor", entry)
    retries = copy.deepcopy(entry.get("vendor_retry", {}))
    if report["hosts"]:
        retries[report["vendor"]] = report["hosts"]
    else:
        retries.pop(report["vendor"], None)
    if retries:
        result["vendor_retry"] = retries
    else:
        result.pop("vendor_retry", None)
    return result


def summaries(report, now=None):
    now = time.time() if now is None else now
    return [{"host": host, "deferred_pages": len(s["pages"]),
             "next_retry_at": s["next_retry_at"], "failures": s["failures"],
             "reason": ("connection outage needs operator attention; daily retry retained"
                        if now - s["first_failure_at"] >= ATTENTION_AFTER
                        else "connection timeout backoff")}
            for host, s in report["hosts"].items() if s["failures"]]
