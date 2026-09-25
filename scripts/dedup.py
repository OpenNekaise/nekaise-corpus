#!/usr/bin/env python3
"""dedup.py — how a finder asks "is this already known?" (ADR 0001 stage 3, step 3).

Finders used to call registry.existing_keys(), which materializes every URL, title and id of the
corpus (millions of strings) in each finder process. Instead a finder opens a Keys session and
asks the store about its own candidates only:

    keys = dedup.open_keys()
    urls, titles = keys.urls, keys.titles
    keys.prefetch(urls=[...page urls...], titles=[...page titles...])   # one batched known()
    for ...:
        if u in urls or t in titles:          # answered from the batch, else one small lookup
            continue
        urls.add(u); titles.add(t)            # the run's own additions stay local
    keys.uniquify_ids(out)                    # registry.uniquify_ids' -2/-3 suffixes vs the store

Membership is exactly the legacy set membership: a candidate counts as known if the finder added
it in this run, or the store's known() reports it (registry + manifest, so manifest-only ids
collide too, + the pruned-URL blocklist). Finders pass values already normalized the legacy way
(url.rstrip("/"), registry.norm(title)); the store's keys are strip().rstrip("/") urls and
registry.norm titles, so only a value that is its own normal form can equal a stored key — any
other value is answered "unknown" without a query, as `value in legacy_set` did.

Persistent identity (DOI / OpenAlex work id) is a fourth key: identity_known(entry) asks whether
any identifier the entry declares (persistent_id, origin_ids) maps to an identity id the store or
this run already holds (see identity_id below); run_round's serial proposal merge asks it too.

prefetch() is only an optimization (a whole page in MAX_KNOWN-sized known() calls); a value that
was not prefetched is looked up on its own when first tested, so a finder that checks a URL
before an expensive metadata request still avoids that request.

Each lookup batch opens its own read view: inside a round the finder inherits the round's read
access (store.INHERITED_LOCK_ENV); a standalone run takes the round lock just for the lookup,
waiting at most NEKAISE_DEDUP_LOCK_TIMEOUT seconds (default 30) and then failing with a clear
error, so a manual finder never holds the lock across its network I/O.
"""
from __future__ import annotations

import os
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Mapping

import registry
import store

LOCK_TIMEOUT_ENV = "NEKAISE_DEDUP_LOCK_TIMEOUT"
DEFAULT_LOCK_TIMEOUT = 30.0
_KINDS = ("urls", "titles", "ids")


class DedupUnavailable(RuntimeError):
    """No read view could be opened (e.g. a standalone run while a round holds the lock)."""


def lock_timeout() -> float:
    value = os.environ.get(LOCK_TIMEOUT_ENV)
    return float(value) if value else DEFAULT_LOCK_TIMEOUT


def _default_root() -> Path:
    """The repository the store lives in (tests replace this so no test reads the live repo)."""
    return store.ROOT


def _open(root: Path | None):
    return store.open(root=Path(root) if root else _default_root())


@contextmanager
def read_view(root: Path | None = None, *, st=None) -> Iterator["store.ReadView"]:
    """A store read view with a clear error when it cannot be opened."""
    st = st if st is not None else _open(root)
    with ExitStack() as stack:
        try:
            view = stack.enter_context(st.read(timeout=lock_timeout()))
        except RuntimeError as exc:  # lock busy, unverified inherited lock, pending recovery
            raise DedupUnavailable(
                f"dedup: cannot open a store read view: {exc}. Inside a round finders inherit "
                f"read access; a standalone run waits {lock_timeout():g}s "
                f"({LOCK_TIMEOUT_ENV}) for the round lock — retry when no round is running, or "
                "run discovery through run_round.py") from exc
        yield view


def scan_all(view, table: "store.Table", *, where=None, fields=None,
             page: int = store.DEFAULT_PAGE) -> Iterator[dict]:
    """Every row of a filtered, projected scan, page by page."""
    cursor = None
    while True:
        result = view.scan(table, where=where, fields=fields, cursor=cursor, limit=page)
        yield from result.rows
        if result.next_cursor is None:
            return
        cursor = result.next_cursor


def _wanted(urls: list, titles: list, ids: list) -> tuple[list, list, list]:
    # Only a value that is its own normal form can equal a stored (normalized) key.
    return ([u for u in urls if store.norm_url(u) == u],
            [t for t in titles if store.norm_title(t) == t], list(ids))


def _known(view, wanted: tuple[list, list, list]) -> tuple[set, set, set]:
    hits: tuple[set, set, set] = (set(), set(), set())
    n = store.MAX_KNOWN
    for i in range(0, max(map(len, wanted)), n):
        got = view.known(urls=wanted[0][i:i + n], titles=wanted[1][i:i + n],
                         ids=wanted[2][i:i + n])
        hits[0].update(got.urls)
        hits[1].update(got.titles)
        hits[2].update(got.ids)
    return hits


class _StoreBackend:
    """Membership from the store's known(), one read view per lookup batch."""

    def __init__(self, st):
        self.st = st
        self.round_trips = 0  # read views opened

    def lookup(self, urls: list, titles: list, ids: list) -> tuple[set, set, set]:
        wanted = _wanted(urls, titles, ids)
        if not any(wanted):
            return set(), set(), set()
        self.round_trips += 1
        with read_view(st=self.st) as view:
            return _known(view, wanted)


class _ViewBackend:
    """Membership from one open view — e.g. run_round's discovery transaction, asked before it
    writes anything, so the file store still answers from its index."""

    def __init__(self, view):
        self.view = view

    def lookup(self, urls: list, titles: list, ids: list) -> tuple[set, set, set]:
        wanted = _wanted(urls, titles, ids)
        return _known(self.view, wanted) if any(wanted) else (set(), set(), set())


class _SetBackend:
    """Membership from in-memory sets: the legacy existing_keys() path (tests, equivalence)."""

    def __init__(self, urls: set, titles: set, ids: set):
        self.sets = (urls, titles, ids)

    def lookup(self, urls: list, titles: list, ids: list) -> tuple[set, set, set]:
        return tuple({v for v in values if v in known}
                     for values, known in zip((urls, titles, ids), self.sets))


class KnownSet:
    """Set-like view of one key kind: `in` (store or local), add() (local only), prefetch()."""

    def __init__(self, keys: "Keys", kind: str):
        self._keys, self._kind = keys, kind
        self._local: set = set()
        self._cache: dict[str, bool] = {}

    def __contains__(self, value) -> bool:
        if value in self._local:
            return True
        if not isinstance(value, str) or not value:
            return False
        if value not in self._cache:
            self._keys.prefetch(**{self._kind: [value]})
        return self._cache[value]

    def add(self, value) -> None:
        self._local.add(value)

    def update(self, values: Iterable) -> None:
        self._local.update(values)

    def prefetch(self, values: Iterable) -> None:
        self._keys.prefetch(**{self._kind: values})


class Keys:
    """One finder run's dedup session (see module docstring)."""

    def __init__(self, backend):
        self._backend = backend
        self.urls, self.titles, self.ids = (KnownSet(self, k) for k in _KINDS)

    @property
    def lookups(self) -> int:
        """Store round trips (read views opened) so far, for tests and diagnostics."""
        return getattr(self._backend, "round_trips", 0)

    def prefetch(self, *, urls: Iterable = (), titles: Iterable = (), ids: Iterable = ()) -> None:
        """Answer these candidates in as few store round trips as possible."""
        pending = []
        for known_set, values in zip((self.urls, self.titles, self.ids), (urls, titles, ids)):
            fresh = dict.fromkeys(v for v in values if isinstance(v, str) and v
                                  and v not in known_set._cache)
            pending.append(list(fresh))
        if not any(pending):
            return
        hits = self._backend.lookup(*pending)
        for known_set, values, hit in zip((self.urls, self.titles, self.ids), pending, hits):
            for v in values:
                known_set._cache[v] = v in hit

    def uniquify_ids(self, entries: list[dict]) -> None:
        """registry.uniquify_ids against the store plus this run's reservations: suffix -2/-3/…
        (on a 50-char base) onto any id already known — registry or manifest-only — or repeated
        in the batch. Mutates entries' ids and reserves them for later batches of this run."""
        self.prefetch(ids=[e["id"] for e in entries])
        for e, renamed in zip(entries, store.uniquify(entries, self.ids.__contains__)):
            e["id"] = renamed["id"]
            self.ids.add(e["id"])


    # --- persistent identity (DOI / OpenAlex work id) ------------------------------------------

    def identity_known(self, entry: Mapping) -> bool:
        """Whether any persistent identifier the entry declares (persistent_id, origin_ids) is
        already held: by the store (a registry/manifest row under its identity id) or by this
        run. Entries that declare no DOI/OpenAlex identity are never "identity known"."""
        keys = identity_ids(entry)
        if not keys:
            return False
        self.prefetch(ids=keys)
        return any(k in self.ids for k in keys)

    def add_identity(self, entry: Mapping) -> None:
        """Reserve the entry's identity keys for the rest of this run (serial merge)."""
        self.ids.update(identity_ids(entry))


def open_keys(root: Path | None = None) -> Keys:
    """A dedup session against the configured store (NEKAISE_STORE) at `root`."""
    return Keys(_StoreBackend(_open(root)))


def from_view(view) -> Keys:
    """A dedup session answered by one already-open store view (no view per lookup)."""
    return Keys(_ViewBackend(view))


def from_sets(urls: set, titles: set, ids: set) -> Keys:
    """A dedup session over legacy in-memory key sets (registry.existing_keys()' shape)."""
    return Keys(_SetBackend(urls, titles, ids))


# --- persistent identifiers -----------------------------------------------------------------------
# URLs and titles miss one work under two DOIs or two copies (preprint vs published, publisher PDF
# vs repository copy). Works found through scholarly metadata therefore carry normalized
# persistent identifiers, and each identifier maps to a deterministic IDENTITY ID in the id space
# the store already indexes (known(ids=...), both backends): a work registered under identity id
# X is known to every later candidate that names the same DOI or OpenAlex work — directly, or
# through an explicit version relation recorded in `origin_ids` — without a new store index.
IDENTITY_PREFIX = "oas-"


def normalize_pid(value) -> str | None:
    """"doi:10.x/y" (lower-case) or "openalex:W123", from any common spelling; else None."""
    import oa_resolution

    if doi := oa_resolution.normalize_doi(value):
        return f"doi:{doi}"
    if work := oa_resolution.normalize_openalex(value):
        return f"openalex:{work}"
    return None


def identity_id(pid: str) -> str | None:
    """The deterministic registry id for one normalized persistent identifier."""
    import hashlib

    pid = normalize_pid(pid)
    if pid is None:
        return None
    kind, value = pid.split(":", 1)
    if kind == "openalex":
        return f"{IDENTITY_PREFIX}{value.lower()}"
    digest = hashlib.sha1(pid.encode()).hexdigest()[:8]
    return f"{IDENTITY_PREFIX}{registry.slug(value)[:40].strip('-')}-{digest}"


def entry_pids(entry: Mapping) -> list[str]:
    """Every normalized persistent identifier an entry declares, in declaration order."""
    values = [entry.get("persistent_id")]
    origin = entry.get("origin_ids")
    if isinstance(origin, str):
        values += origin.split()
    elif isinstance(origin, (list, tuple)):
        values += list(origin)
    return list(dict.fromkeys(p for p in map(normalize_pid, values) if p))


def identity_ids(entry: Mapping) -> list[str]:
    return list(dict.fromkeys(i for i in map(identity_id, entry_pids(entry)) if i))


def held_rows(ids: Iterable[str], root: Path | None = None) -> dict[str, dict]:
    """Manifest rows for these ids (one read view): lets a finder tell "known metadata" (a
    registry row, a failed or pointer-only row) from "eligible content already held" (status ok).
    Only called for ids that already hit, so it stays a handful of point reads."""
    ids = [i for i in ids if isinstance(i, str) and i]
    if not ids:
        return {}
    with read_view(root) as view:
        return view.get_manifest(ids)


def page_keys(items: Iterable[Mapping], url_field: str = "url",
              title_field: str = "title") -> dict[str, list[str]]:
    """prefetch() arguments for a page of candidate dicts, normalized the legacy way."""
    urls, titles = [], []
    for item in items:
        if isinstance(u := item.get(url_field), str):
            urls.append(u.rstrip("/"))
        if isinstance(t := item.get(title_field), str):
            titles.append(registry.norm(t))
    return {"urls": urls, "titles": titles}
