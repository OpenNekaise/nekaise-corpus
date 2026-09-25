#!/usr/bin/env python3
"""openalex_state.py — MACHINE-level politeness state shared by every scholarly-API caller.

OpenAlex, Crossref and Unpaywall see this machine's IP, not a checkout: the legacy OpenAlex
finder, both query families (concurrent processes in one round) and any other checkout or
worktree of the repository all spend the same budget. Their shared state therefore lives in ONE
per-user directory, identical for every checkout:

    $NEKAISE_MACHINE_STATE (override; tests isolate it)  else  $XDG_CACHE_HOME/nekaise
    else ~/.cache/nekaise

* cooldowns.json — per-host cooldown deadlines (epoch seconds). Writes are a LOCKED
  read-merge-write that keeps the MAXIMUM deadline per host (one process's snapshot can never
  erase another's cooldown); readers re-read before every request.
* openalex-pace.json — the start time of the last OpenAlex request; SharedPacer spaces every
  OpenAlex request of the machine by OPENALEX_SPACING under a lock.

Throttle answers (429/503) are classified from their rate-limit headers: the daily credit
BUDGET spent (cooldown until X-RateLimit-Reset) versus request-RATE limiting (Retry-After).
"""
from __future__ import annotations

import email.utils
import json
import os
import time
from pathlib import Path

import ops

STATE_ENV = "NEKAISE_MACHINE_STATE"
OPENALEX_SPACING = 1.0  # seconds between ANY two OpenAlex requests of this machine
SEARCH_CREDITS = 10     # one OpenAlex search costs 10 credits (x-ratelimit-cost 0.001 USD)
LOCK_TIMEOUT = 300.0
RATE_HEADERS = ("retry-after", "x-ratelimit-remaining", "x-ratelimit-limit",
                "x-ratelimit-reset", "x-ratelimit-remaining-usd", "x-ratelimit-cost-usd")
LEGACY_COOLDOWN_FILE = "find-sources-cooldowns.json"  # the checkout-local file before 2026-09-25


def per_user_cache_dir() -> Path:
    """$XDG_CACHE_HOME/nekaise, else ~/.cache/nekaise: per user, the same for every checkout."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "nekaise"


def default_state_dir() -> Path:
    return per_user_cache_dir()


def state_dir() -> Path:
    """The machine-level state directory (created on demand)."""
    override = os.environ.get(STATE_ENV)
    path = Path(override) if override else default_state_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


class Cooldowns:
    """Per-host cooldown deadlines in one JSON file, shared by every process of the machine."""

    def __init__(self, path: Path | None = None, clock=None):
        self._path = Path(path) if path is not None else None
        self.clock = clock or (lambda: time.time())  # resolved per call (patchable)

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else state_dir() / "cooldowns.json"

    def _read(self) -> dict[str, float]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            print(f"# ignoring invalid cooldown state {self.path}: {exc}", flush=True)
            return {}
        out = {}
        for key, until in (data.items() if isinstance(data, dict) else ()):
            try:
                out[str(key)] = float(until)
            except (TypeError, ValueError):
                continue
        return out

    def load(self) -> dict[str, float]:
        """Every unexpired deadline, read NOW (writes are atomic, so no lock is needed)."""
        now = self.clock()
        return {k: v for k, v in self._read().items() if v > now}

    def active(self, key: str) -> float | None:
        """The persisted deadline for `key` if it is still in the future (re-read each call)."""
        until = self._read().get(key, 0.0)
        return until if until > self.clock() else None

    def raise_to(self, updates: dict[str, float]) -> dict[str, float]:
        """Locked read-merge-write keeping the MAXIMUM deadline per key; expired keys dropped."""
        with ops.named_lock("scholarly-cooldowns", timeout=LOCK_TIMEOUT,
                            workspace=self.path.parent):
            merged = self._read()
            for key, until in updates.items():
                merged[str(key)] = max(merged.get(str(key), 0.0), float(until))
            now = self.clock()
            merged = {k: v for k, v in merged.items() if v > now}
            ops.atomic_write_text(self.path, json.dumps(merged, sort_keys=True) + "\n")
            return merged


class MemoryCooldowns:
    """Cooldowns over a caller's dict (tests, and callers with their own persistence):
    same interface, `save(dict)` called after every raise."""

    def __init__(self, data: dict, save=None, clock=None):
        self.data, self.save, self.clock = data, save, clock or (lambda: time.time())

    def load(self) -> dict[str, float]:
        now = self.clock()
        return {k: v for k, v in self.data.items() if v > now}

    def active(self, key: str) -> float | None:
        until = self.data.get(key, 0.0)
        return until if until > self.clock() else None

    def raise_to(self, updates: dict[str, float]) -> dict[str, float]:
        for key, until in updates.items():
            self.data[key] = max(self.data.get(key, 0.0), float(until))
        if self.save:
            self.save(self.data)
        return dict(self.data)


def as_cooldowns(value, save=None, clock=None):
    """A Cooldowns-like store from a store, a dict (+ save), or None (the machine file)."""
    if value is None:
        return Cooldowns(clock=clock)
    if hasattr(value, "raise_to"):
        return value
    return MemoryCooldowns(value, save, clock)


def migrate_legacy_cooldowns(workspace: Path, store: Cooldowns | None = None) -> None:
    """Fold a checkout's old workspace/find-sources-cooldowns.json into the machine file once
    (max-merge), then rename it so it is never read again."""
    legacy = Path(workspace) / LEGACY_COOLDOWN_FILE
    if not legacy.exists():
        return
    store = store or Cooldowns()
    old = Cooldowns(legacy, clock=store.clock).load()
    if old:
        store.raise_to(old)
    try:
        legacy.rename(legacy.with_name(legacy.name + ".migrated"))
    except OSError:
        pass


class SharedPacer:
    """Spacing shared by every OpenAlex caller of this machine (all checkouts, worktrees and
    concurrent finders): a lock file serializes callers, a timestamp file remembers the last
    request start. `clock`/`sleep` are injectable; `directory` defaults to state_dir()."""

    def __init__(self, name: str = "openalex-pace", spacing: float = OPENALEX_SPACING, *,
                 clock=time.time, sleep=time.sleep, directory: Path | None = None,
                 timeout: float = LOCK_TIMEOUT, workspace: Path | None = None):
        self.name, self.spacing, self.clock, self.sleep = name, spacing, clock, sleep
        self.directory = directory if directory is not None else workspace
        self.timeout = timeout

    def wait(self) -> None:
        base = Path(self.directory) if self.directory is not None else state_dir()
        with ops.named_lock(self.name, timeout=self.timeout, workspace=base):
            stamp = base / f"{self.name}.json"
            try:
                last = float(json.loads(stamp.read_text())["last"])
            except (OSError, ValueError, KeyError, TypeError):
                last = 0.0
            if (delay := last + self.spacing - self.clock()) > 0:
                self.sleep(delay)
            ops.atomic_write_text(stamp, json.dumps({"last": self.clock()}) + "\n")


class LocalPacer:
    """No cross-process spacing (tests, and callers that own their pacing)."""

    def wait(self) -> None:
        return None


def headers_lower(headers) -> dict:
    try:
        return {str(k).lower(): v for k, v in dict(headers).items()}
    except (TypeError, ValueError):
        return {}


def classify_throttle(host_key: str, status: int, headers, now: float
                      ) -> tuple[str, float, dict, str]:
    """(kind, cooldown deadline, rate-limit headers, detail) of a 429/503 answer: "budget
    exhausted" when OpenAlex reports fewer credits than one search (until X-RateLimit-Reset),
    else "rate limited" (Retry-After seconds or HTTP date; one hour when absent)."""
    h = headers_lower(headers)
    throttle = {k: h[k] for k in RATE_HEADERS if k in h}

    def number(key):
        try:
            return int(float(h[key]))
        except (KeyError, TypeError, ValueError):
            return None

    remaining, reset = number("x-ratelimit-remaining"), number("x-ratelimit-reset")
    if host_key == "openalex" and remaining is not None and remaining < SEARCH_CREDITS:
        kind, deadline = "budget exhausted", now + max(0, reset if reset is not None else 3600)
    else:
        kind, retry = "rate limited", h.get("retry-after")
        deadline = None
        if retry is not None:
            try:
                deadline = now + max(0, int(float(retry)))
            except (TypeError, ValueError):
                try:
                    deadline = max(now, email.utils.parsedate_to_datetime(retry).timestamp())
                except (TypeError, ValueError, OverflowError):
                    deadline = None
        if deadline is None:
            deadline = now + 3600
    detail = ", ".join(f"{k}={v}" for k, v in throttle.items()) or "no rate headers"
    return kind, deadline, throttle, detail


def budget_deadline(headers, now: float) -> float | None:
    """The daily-reset deadline when a successful OpenAlex answer reports fewer credits than
    one search (so the next caller waits instead of spending into a 429), else None."""
    h = headers_lower(headers)
    try:
        remaining = int(h["x-ratelimit-remaining"])
    except (KeyError, TypeError, ValueError):
        return None
    if remaining >= SEARCH_CREDITS:
        return None
    reset = h.get("x-ratelimit-reset")
    return now + (int(reset) if str(reset or "").isdigit() else 3600)


class Throttled(Exception):
    """An OpenAlex request refused (active cooldown) or answered 429/503. Carries a
    `response`-like object so callers that read `.response.status_code` keep working."""

    def __init__(self, message: str, status: int, headers=None, deadline: float | None = None):
        super().__init__(message)
        self.status, self.deadline = status, deadline
        self.response = type("ThrottleAnswer", (), {"status_code": status,
                                                    "headers": dict(headers or {})})()


def openalex_get(get, url: str, *, params=None, timeout: float = 30, cooldowns=None,
                 pacer=None, clock=None, **kwargs):
    """ONE OpenAlex request for any caller: refuse during a persisted cooldown (re-read now),
    wait for the machine-wide spacing, then classify and persist a throttle answer (and a
    spent budget reported on a successful answer). Returns the response otherwise."""
    clock = clock or (lambda: time.time())
    cooldowns = as_cooldowns(cooldowns, clock=clock)
    if until := cooldowns.active("openalex"):
        raise Throttled(f"openalex cooldown active for {max(1, int(until - clock()))}s", 429,
                        deadline=until)
    (pacer if pacer is not None else SharedPacer(clock=clock)).wait()
    resp = get(url, params=params, timeout=timeout, **kwargs)
    status = getattr(resp, "status_code", 200)
    headers = getattr(resp, "headers", {}) or {}
    if status in (429, 503):
        kind, deadline, _, detail = classify_throttle("openalex", status, headers, clock())
        cooldowns.raise_to({"openalex": deadline})
        raise Throttled(f"openalex HTTP {status}: {kind} ({detail})", status, headers, deadline)
    if (deadline := budget_deadline(headers, clock())) is not None:
        cooldowns.raise_to({"openalex": deadline})
    return resp
