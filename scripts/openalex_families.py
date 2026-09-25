#!/usr/bin/env python3
"""openalex_families.py — independently versioned OpenAlex query families for find_sources.py.

The legacy OpenAlex family (find_sources.QUERIES, 105 frozen queries, integer cursor) stays as
it is. A FAMILY is a separately configured query list with its own dynamic rotation cursor, run
as `find_sources.py --family <name> --family-cursor <cursor>` (backend find_openalex_sim):

    sim1 t=<tick> q=<query> w=<window> p=<page> k=<consumed> sq=… sw=… sp=… sk=…

* ONE OpenAlex budget. OpenAlex meters searches (10 credits each; anonymous 1,000 credits/day),
  and every OpenAlex backend shares it: each round spends at most ONE search across the legacy
  family and this one. The tick walks SCHEDULE — 7/10 simulation, 2/10 legacy, 1/10 SSRN-origin
  — and the legacy backend (--budget-partner) yields its round unless the tick says "legacy".
  Singleton work lookups and Crossref/Unpaywall calls are free supplementary lookups, capped per
  run (--lookup-max).
* Position is never lost. A page is (query, date window, page); `k` counts results of that page
  already consumed. Hitting --max accepted documents stops mid-page and keeps the page with its
  new `k`; running out of lookups parks the item in the resolution record (pending_lookup)
  instead. An upstream failure exits non-zero or holds (the committed cursor stays); it is never
  reported as exhaustion. A query/window that is fully read moves on; after the last query the
  walk starts a new pass (new literature appears in the newest window).
* Unresolved works (no copy with accepted rights yet, e.g. SSRN preprints) are kept in a
  SEPARATE resolution record (workspace/openalex-resolution.jsonl, a retry cache — losing it
  loses retry hints, never committed state) instead of title-blocking pointer rows in the fetch
  registry, and are re-resolved later with bounded supplementary lookups.

Changing SIM_QUERIES, WINDOWS or SCHEDULE requires a new FAMILY_VERSION and a reviewed cursor
migration (parse_cursor refuses a cursor of another version).
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import bes_relevance
import dedup
import oa_resolution as oar
import ops
import registry

FAMILY_VERSION = "sim1"
SOURCE = "openalex_sim"
OPENALEX = "https://api.openalex.org/works"
SSRN_SOURCE_ID = "S4210172589"  # OpenAlex source "SSRN Electronic Journal"
SEARCH_CREDITS = 10             # x-ratelimit-cost: 0.001 USD = 10 credits per search (2026-09-25)
MAX_PAGE_DEPTH = 10_000         # OpenAlex page-based paging limit (page * per-page)
LEDGER_CAP = 20_000
PENDING_CAP = 500

# (key, OpenAlex title_and_abstract.search expression, topic). No commas: they separate filters.
SIM_QUERIES: tuple[tuple[str, str, str], ...] = (
    ("modelica", '(Modelica OR OpenModelica OR Dymola) AND (building OR buildings OR HVAC OR '
                 '"heat pump" OR "district heating" OR "thermal zone")', "building_energy"),
    ("modelica-libraries", '"Modelica Buildings" OR "Buildings library" OR AixLib OR '
                           '"IDEAS library" OR "IBPSA Project" OR BuildingSystems OR BuildSysPro',
     "building_energy"),
    ("energyplus", '(EnergyPlus OR "Spawn of EnergyPlus" OR BOPTEST OR OpenStudio OR TRNSYS OR '
                   '"IDA ICE" OR "ESP-r" OR DOE-2) AND (building OR buildings OR HVAC)',
     "building_energy"),
    ("calibration", '("building energy simulation" OR "building performance simulation" OR '
                    '"building energy model" OR "building energy modeling" OR '
                    '"building energy modelling") AND (calibration OR calibrated OR validation)',
     "building_energy"),
    ("hvac-mpc", '("model predictive control" OR MPC) AND (HVAC OR building OR buildings) AND '
                 '(simulation OR co-simulation OR emulator OR "building model")', "controls_bas"),
    ("digital-twin", '"digital twin" AND (building OR buildings OR HVAC) AND (simulation OR '
                     '"energy model" OR "physics-based" OR "building energy")', "building_energy"),
    ("llm-simulation", '("large language model" OR "large language models" OR LLM OR LLMs) AND '
                       '(EnergyPlus OR Modelica OR "building energy" OR "building simulation" OR '
                       'HVAC OR "energy model")', "building_energy"),
    ("co-simulation", '(co-simulation OR "functional mock-up" OR FMU) AND (building OR buildings '
                      'OR HVAC OR "district heating")', "building_energy"),
    ("de", 'Gebäudesimulation OR "thermische Gebäudesimulation" OR "energetische '
           'Gebäudesimulation" OR Anlagensimulation OR "dynamische Gebäudesimulation"',
     "building_energy"),
    ("fr", '("simulation énergétique" OR "simulation thermique dynamique") AND (bâtiment OR '
           'bâtiments OR logement)', "building_energy"),
    ("es-pt-it", '"simulación energética" OR "simulação energética" OR "simulazione energetica" '
                 'OR "simulación térmica" OR "simulação termoenergética" OR "simulazione '
                 'dinamica"', "building_energy"),
    ("zh", '建筑能耗模拟 OR 建筑能耗仿真 OR 建筑能源模拟 OR 建筑热环境模拟 OR 暖通空调仿真',
     "building_energy"),
    ("ja-ko", '建築 シミュレーション OR 熱負荷計算 OR 空調 シミュレーション OR "건물 에너지 시뮬레이션" OR '
              '"건물 에너지 해석"', "building_energy"),
    ("nl-nordic", 'gebouwsimulatie OR "energiesimulatie" OR bygningssimulering OR '
                  'byggnadssimulering OR energisimulering OR rakennussimulointi',
     "building_energy"),
)
# Queries whose expression itself requires a simulation/tool term in the title or abstract: a
# match proves the simulation anchor even when OpenAlex serves no abstract (it withholds many
# publishers' abstracts). The BUILDING anchor is never implied by a query.
SIM_IMPLIED = frozenset({"modelica", "modelica-libraries", "energyplus", "calibration",
                         "hvac-mpc", "co-simulation", "de", "fr", "es-pt-it", "zh", "ja-ko",
                         "nl-nordic"})
WINDOWS: tuple[str, ...] = (
    "publication_year:>2024", "publication_year:2020-2024", "publication_year:2015-2019",
    "publication_year:2005-2014", "publication_year:<2005",
)
SCHEDULE: tuple[str, ...] = ("sim", "sim", "legacy", "sim", "sim", "ssrn", "sim", "sim",
                             "legacy", "sim")
TYPES = "type:article|preprint"

# --- relevance ------------------------------------------------------------------------------------
SIM_ANCHOR = re.compile(
    r"simulat|co-?simulat|modelica|dymola|energy ?plus|\bspawn\b|boptest|openstudio|trnsys|"
    r"ida ice|esp-r|doe-2|functional mock-?up|\bfmus?\b|digital twin|model predictive|\bmpc\b|"
    r"calibrat|energy model|thermal model|building model|aixlib|\bideas\b|ibpsa|buildingsystems|"
    r"simulación|simulação|simulazione|simulatie|simulering|simulointi|仿真|模拟|模擬|"
    r"シミュレーション|시뮬레이션|моделирован",
    re.I,
)
TOOL_ANCHOR = re.compile(
    r"modelica buildings|buildings library|aixlib|ideas library|ibpsa|buildingsystems|"
    r"energy ?plus|\bspawn\b|boptest|openstudio|trnsys|ida ice|esp-r|doe-2",
    re.I,
)
MULTILINGUAL_BUILT = re.compile(
    r"gebäude|bâtiment|edifici|edificaç|建筑|建築|暖通|空調|空调|건물|건축|gebouw|bygning|byggnad|"
    r"rakennu|здани",
    re.I,
)


def sim_relevant(work: dict, openalex_relevant, sim_implied: bool = False) -> bool:
    """Simulation AND built-environment evidence. Generic "agents", "digital twin" or "energy"
    alone is not enough: a simulation/tool anchor must meet a building anchor — in the title, in
    OpenAlex's AEC subfields, or in the abstract when the text also names a building-simulation
    tool."""
    title = (work.get("title") or work.get("display_name") or "").strip()
    if not title or bes_relevance.vetoed(title):
        return False
    abstract = oar.abstract_text(work)
    text = f"{title} {abstract}"
    if not sim_implied and not SIM_ANCHOR.search(text):
        return False
    if openalex_relevant(work, title) or bes_relevance.relevant(title, strict=True):
        return True
    if MULTILINGUAL_BUILT.search(title):
        return True
    return bool(TOOL_ANCHOR.search(text) and (bes_relevance.BUILT.search(abstract)
                                               or MULTILINGUAL_BUILT.search(abstract)))


# --- cursor ---------------------------------------------------------------------------------------

@dataclass
class Position:
    q: int = 0
    w: int = 0
    p: int = 1
    k: int = 0

    def advance_window(self) -> None:
        self.p, self.k = 1, 0
        self.w += 1
        if self.w >= len(WINDOWS):
            self.w = 0
            self.q = (self.q + 1) % len(SIM_QUERIES)


@dataclass
class Cursor:
    t: int = 0
    sim: Position = field(default_factory=Position)
    ssrn: Position = field(default_factory=Position)

    def render(self) -> str:
        s, r = self.sim, self.ssrn
        return (f"{FAMILY_VERSION} t={self.t} q={s.q} w={s.w} p={s.p} k={s.k} "
                f"sq={r.q} sw={r.w} sp={r.p} sk={r.k}")


_CURSOR = re.compile(
    r"(?P<v>\S+) t=(?P<t>\d+) q=(?P<q>\d+) w=(?P<w>\d+) p=(?P<p>\d+) k=(?P<k>\d+) "
    r"sq=(?P<sq>\d+) sw=(?P<sw>\d+) sp=(?P<sp>\d+) sk=(?P<sk>\d+)"
)


def parse_cursor(value: str) -> Cursor:
    m = _CURSOR.fullmatch((value or "").strip())
    if not m:
        raise ValueError(f"malformed family cursor: {value!r}")
    if m["v"] != FAMILY_VERSION:
        raise ValueError(f"family cursor is {m['v']}, this code walks {FAMILY_VERSION}: "
                         "migrate the committed cursor before changing the family")
    n = {k: int(v) for k, v in m.groupdict().items() if k != "v"}
    cur = Cursor(n["t"], Position(n["q"], n["w"], n["p"], n["k"]),
                 Position(n["sq"], n["sw"], n["sp"], n["sk"]))
    for pos in (cur.sim, cur.ssrn):
        if not (pos.q < len(SIM_QUERIES) and pos.w < len(WINDOWS) and pos.p >= 1
                and 0 <= pos.k <= 200):
            raise ValueError(f"family cursor out of range: {value!r}")
    return cur


def turn(tick: int) -> str:
    return SCHEDULE[tick % len(SCHEDULE)]


def legacy_may_search(partner_cursor: str | None, partner_enabled: bool) -> tuple[bool, str]:
    """Whether the legacy family owns this round's single OpenAlex search."""
    if not partner_enabled or not partner_cursor:
        return True, "budget partner disabled"
    try:
        cur = parse_cursor(partner_cursor)
    except ValueError as exc:
        return True, f"budget partner cursor unreadable ({exc})"
    slot = turn(cur.t)
    return slot == "legacy", f"tick {cur.t} belongs to {slot}"


# --- HTTP with budgets ----------------------------------------------------------------------------

class UpstreamError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retry_at: float | None = None):
        super().__init__(message)
        self.status, self.retry_at = status, retry_at


class Api:
    """Counts requests; enforces the lookup cap, OpenAlex budget headers and persisted
    cooldowns. `get` is requests.get (tests replace it)."""

    def __init__(self, get, *, lookup_max: int, cooldowns: dict, save_cooldowns, now,
                 sleep=time.sleep):
        self.get, self.lookup_max = get, lookup_max
        self.cooldowns, self.save_cooldowns, self.now, self.sleep = (
            cooldowns, save_cooldowns, now, sleep)
        self.searches = 0
        self.lookups = 0
        self.seconds = 0.0
        self.budget_note = ""

    @property
    def lookups_left(self) -> int:
        return max(0, self.lookup_max - self.lookups)

    def _request(self, url, params, *, host_key: str):
        until = self.cooldowns.get(host_key, 0)
        if until > self.now():
            raise UpstreamError(f"{host_key} cooldown active for {int(until - self.now())}s",
                                429, until)
        started = time.monotonic()
        try:
            r = self.get(url, params=params, timeout=30,
                         headers={"User-Agent": f"nekaise-corpus/find_sources (mailto:{oar.MAILTO})"})
        except Exception as exc:
            raise UpstreamError(f"{host_key} request failed: {exc}") from exc
        finally:
            self.seconds += time.monotonic() - started
        headers = getattr(r, "headers", {}) or {}
        status = getattr(r, "status_code", 200)
        if host_key == "openalex":
            self._note_openalex_budget(headers)
        if status in (429, 503):
            retry = headers.get("Retry-After")
            deadline = self.now() + (int(retry) if str(retry or "").isdigit() else 3600)
            self.cooldowns[host_key] = max(self.cooldowns.get(host_key, 0), deadline)
            self.save_cooldowns(self.cooldowns)
            raise UpstreamError(f"{host_key} HTTP {status}", status, deadline)
        if status == 404:
            return None
        if status >= 400:
            raise UpstreamError(f"{host_key} HTTP {status}", status)
        try:
            return r.json()
        except ValueError as exc:
            raise UpstreamError(f"{host_key} returned non-JSON: {exc}") from exc

    def _note_openalex_budget(self, headers) -> None:
        """Persist a cooldown until the daily reset once fewer credits remain than one search."""
        remaining = headers.get("x-ratelimit-remaining") or headers.get("X-RateLimit-Remaining")
        reset = headers.get("x-ratelimit-reset") or headers.get("X-RateLimit-Reset")
        try:
            remaining = int(remaining)
        except (TypeError, ValueError):
            return
        self.budget_note = f"OpenAlex credits remaining {remaining}"
        if remaining < SEARCH_CREDITS:
            wait = int(reset) if str(reset or "").isdigit() else 3600
            self.cooldowns["openalex"] = max(self.cooldowns.get("openalex", 0),
                                             self.now() + wait)
            self.save_cooldowns(self.cooldowns)
            self.budget_note += f"; cooldown persisted for {wait}s"

    def search(self, params: dict) -> dict:
        self.searches += 1
        data = self._request(OPENALEX, {**params, "mailto": oar.MAILTO}, host_key="openalex")
        if not isinstance(data, dict):
            raise UpstreamError("OpenAlex search returned no result object")
        return data

    def lookup(self, kind: str, key: str) -> dict | None:
        """One supplementary lookup (counted): OpenAlex work, Crossref work or Unpaywall DOI."""
        if self.lookups >= self.lookup_max:
            raise LookupBudget()
        self.lookups += 1
        self.sleep(0.2 if kind == "openalex" else 1.0)
        if kind == "openalex":
            return self._request(f"{OPENALEX}/{key}", {"mailto": oar.MAILTO},
                                 host_key="openalex")
        if kind == "crossref":
            data = self._request(f"https://api.crossref.org/works/{key}",
                                 {"mailto": oar.MAILTO}, host_key="crossref")
            return (data or {}).get("message") if isinstance(data, dict) else None
        if kind == "unpaywall":
            return self._request(f"https://api.unpaywall.org/v2/{key}",
                                 {"email": oar.MAILTO}, host_key="unpaywall")
        raise ValueError(kind)


class LookupBudget(Exception):
    """The per-run supplementary lookup cap is spent."""


# --- resolution record ----------------------------------------------------------------------------

def default_ledger_path() -> Path:
    return ops.WORKSPACE / "openalex-resolution.jsonl"


class Ledger:
    """The resolution record: unresolved/pending works kept for later retry, keyed by their
    first normalized persistent id. A git-ignored retry cache, written atomically."""

    def __init__(self, path: Path | None):
        self.path = path
        self.rows: dict[str, dict] = {}
        if path and path.exists():
            for line in path.read_text().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("key"):
                    self.rows[row["key"]] = row

    def upsert(self, key: str, **fields) -> dict:
        row = self.rows.setdefault(key, {"key": key, "attempts": 0,
                                         "first_seen": fields.get("last_tried")})
        row.update({k: v for k, v in fields.items() if v is not None})
        return row

    def due(self, now_iso: str, limit: int) -> list[dict]:
        rows = [r for r in self.rows.values()
                if r.get("status") in ("pending_lookup", "unresolved")
                and (r.get("next_retry_at") or "") <= now_iso]
        rows.sort(key=lambda r: (r.get("status") != "pending_lookup", r.get("next_retry_at") or "",
                                 r["key"]))
        return rows[:limit]

    def pending(self) -> int:
        return sum(r.get("status") == "pending_lookup" for r in self.rows.values())

    def save(self) -> None:
        if self.path is None:
            return
        rows = list(self.rows.values())
        if len(rows) > LEDGER_CAP:  # forget settled rows first, then the oldest
            rows.sort(key=lambda r: (r.get("status") in ("unresolved", "pending_lookup"),
                                     r.get("last_tried") or ""))
            rows = rows[len(rows) - LEDGER_CAP:]
        rows.sort(key=lambda r: r["key"])
        ops.atomic_write_text(self.path, "".join(
            json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows))


def backoff_iso(now: float, attempts: int) -> str:
    days = min(90, 7 * 2 ** max(0, attempts - 1))
    return iso(now + days * 86400)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- one run --------------------------------------------------------------------------------------

@dataclass
class Stats:
    tick: int = 0
    slot: str = ""
    searched: str = ""
    results: int = 0
    considered: int = 0
    relevance_rejected: int = 0
    excluded: int = 0
    duplicates: dict = field(default_factory=dict)
    known_not_held: int = 0
    resolved: int = 0
    resolved_via: dict = field(default_factory=dict)
    unresolved: int = 0
    rights_rejected: int = 0
    pending_lookup: int = 0
    retried: int = 0
    retry_resolved: int = 0
    stopped_at_max: bool = False
    reasons: dict = field(default_factory=dict)
    examples: dict = field(default_factory=dict)

    def dup(self, kind: str) -> None:
        self.duplicates[kind] = self.duplicates.get(kind, 0) + 1

    def example(self, kind: str, text: str) -> None:
        self.examples.setdefault(kind, [])
        if len(self.examples[kind]) < 5:
            self.examples[kind].append(text[:200])


def work_pids(work: dict) -> list[str]:
    out = []
    if doi := oar.normalize_doi(work.get("doi")):
        out.append(f"doi:{doi}")
    if w := oar.normalize_openalex(work.get("id")):
        out.append(f"openalex:{w}")
    return out


def work_urls(work: dict) -> list[str]:
    urls = []
    for loc in oar.work_locations(work):
        for key in ("pdf_url", "landing_page_url"):
            if isinstance(loc.get(key), str) and loc[key].strip():
                urls.append(loc[key].strip().rstrip("/"))
    if doi := oar.normalize_doi(work.get("doi")):
        urls.append(oar.doi_url(doi))
    return list(dict.fromkeys(urls))


def build_entry(work: dict, res: oar.Resolution, *, topic: str, source: str, family: str,
                today: str, origin_extra: list[str] = (), relation: str = "") -> dict:
    """A registry entry for the resolved copy, carrying its rights evidence and identity."""
    doi = oar.normalize_doi(work.get("doi"))
    wid = oar.normalize_openalex(work.get("id"))
    pid = oar.doi_url(doi) if doi else f"https://openalex.org/{wid}"
    origin = list(dict.fromkeys([*work_pids(work), *origin_extra]))
    copy, rights = res.copy, res.rights
    entry = {
        "id": dedup.identity_id(pid),
        "title": (work.get("title") or work.get("display_name") or "").strip()[:150],
        "url": copy.url, "source": source, "license": rights.tag, "topic": topic,
        "format": "pdf",
        "language": work.get("language") or None,
        "published_at": work.get("publication_date") or None,
        "document_type": work.get("type") or None,
        "persistent_id": pid,
        "license_url": rights.url,
        "license_evidence": (f"{rights.evidence} [copy {copy.url}; version "
                             f"{copy.version or 'unstated'}]")[:600],
        "rights_verified_at": today,
        "selected_version": copy.version or "unstated",
        "origin_ids": " ".join(origin),
        "resolution": (f"{family}: {'+'.join(dict.fromkeys(copy.sources))} location"
                       + (f"; {relation}" if relation else ""))[:300],
    }
    return {k: v for k, v in entry.items() if v not in (None, "")}


class FamilyRun:
    """One invocation of the simulation family (see module docstring)."""

    def __init__(self, *, api: Api, policy: dict, keys, ledger: Ledger, per: int, max_docs: int,
                 openalex_relevant, now: float, held_rows=dedup.held_rows):
        self.api, self.policy, self.keys, self.ledger = api, policy, keys, ledger
        self.per, self.max_docs = per, max_docs
        self.openalex_relevant = openalex_relevant
        self.now = now
        self.today = iso(now)[:10]
        self.held_rows = held_rows
        self.out: list[dict] = []
        self.stats = Stats()

    # -- dedup ---------------------------------------------------------------------------------
    def _known(self, work: dict, extra_pids: list[str] = ()) -> str | None:
        pids = list(dict.fromkeys([*work_pids(work), *extra_pids]))
        ids = [i for i in map(dedup.identity_id, pids) if i]
        self.keys.prefetch(ids=ids)
        hit_ids = [i for i in ids if i in self.keys.ids]
        if hit_ids:
            rows = self.held_rows(hit_ids)
            if any(r.get("status") == "ok" for r in rows.values()):
                return "identity_held"
            return "identity_known_not_held"
        title = registry.norm(work.get("title") or work.get("display_name") or "")
        if title and title in self.keys.titles:
            return "title"
        urls = work_urls(work)
        self.keys.prefetch(urls=urls)
        if any(u in self.keys.urls for u in urls):
            return "url"  # includes pruned_urls.txt: prune decisions are never undone here
        return None

    def _accept(self, work: dict, res: oar.Resolution, topic: str, *, origin_extra=(),
                relation: str = "") -> bool:
        entry = build_entry(work, res, topic=topic, source=SOURCE, family=FAMILY_VERSION,
                            today=self.today, origin_extra=list(origin_extra), relation=relation)
        url, title = entry["url"].rstrip("/"), registry.norm(entry["title"])
        if url in self.keys.urls or title in self.keys.titles or self.keys.identity_known(entry):
            self.stats.dup("selected_copy")
            return False
        self.keys.urls.add(url)
        self.keys.titles.add(title)
        self.keys.add_identity(entry)
        self.out.append(entry)
        self.stats.example("retain", f"{entry['id']} | {entry['license']} | {entry['url']} | "
                                     f"{entry['title']}")
        return True

    # -- resolution ------------------------------------------------------------------------------
    def resolve(self, work: dict) -> tuple[oar.Resolution, dict | None, list[str], str]:
        """select_copy with bounded supplementary evidence. Returns (resolution, the work whose
        copy was chosen, extra origin pids, relation note). Raises LookupBudget when a lookup
        that could still change the outcome is unaffordable."""
        res = oar.select_copy(work, self.policy)
        if res.status != "unresolved":
            return res, work, [], ""
        doi = oar.normalize_doi(work.get("doi"))
        if not doi:
            return res, work, [], ""
        # Lookups only where they can change the outcome: a preprint may have an explicitly
        # related published version (Crossref relations); a PDF on an allowed host whose licence
        # is merely UNKNOWN may gain evidence (Crossref licence for the DOI's own copy, Unpaywall
        # for any copy). A work with no allowed PDF at all is not looked up: Unpaywall mirrored
        # OpenAlex's (absent) PDF locations for every such DOI sampled on 2026-09-25.
        preprint = work.get("type") == "preprint" or doi.startswith("10.2139/")
        fixable = any(r.startswith("rights_unknown:") for r in res.reasons)
        crossref = unpaywall = None
        if preprint or fixable:
            crossref = self.api.lookup("crossref", doi)
            res = oar.select_copy(work, self.policy, crossref=crossref)
            if res.status == "resolved":
                return res, work, [], "crossref licence evidence"
        if fixable and not doi.startswith("10.2139/"):
            unpaywall = self.api.lookup("unpaywall", doi)
            res = oar.select_copy(work, self.policy, crossref=crossref, unpaywall=unpaywall)
            if res.status == "resolved":
                return res, work, [], "unpaywall fallback"
        if crossref:
            record = oar.work_record(work)
            fetched: dict[str, dict | None] = {}

            def fetch(other_doi):
                if other_doi not in fetched:
                    fetched[other_doi] = self.api.lookup("openalex", f"doi:{other_doi}")
                return fetched[other_doi]

            def lookup_record(other_doi):
                other = fetch(other_doi)
                return oar.work_record(other) if other else None

            for rel_type, other_doi in oar.crossref_related_dois(crossref, record,
                                                                 lookup_record)[:2]:
                other = fetch(other_doi)
                if not other:
                    continue
                if (why := oar.work_excluded(other)) is not None:
                    return oar.Resolution("excluded", [why]), None, [], ""
                other_res = oar.select_copy(other, self.policy)
                if other_res.status == "resolved":
                    note = f"crossref relation {rel_type} {doi} -> {other_doi}"
                    return other_res, other, work_pids(work), note
                res.reasons.extend(f"related:{r}" for r in other_res.reasons)
        return res, work, [], ""

    def _record_unresolved(self, work: dict, res: oar.Resolution, topic: str, *,
                           pending: bool = False, family: str = "sim") -> None:
        pids = work_pids(work)
        if not pids:
            return
        existing = self.ledger.rows.get(pids[0], {})
        attempts = int(existing.get("attempts") or 0) + (0 if pending else 1)
        pending = pending and self.ledger.pending() < PENDING_CAP
        rec = oar.work_record(work)
        self.ledger.upsert(
            pids[0], ids=pids, title=rec["title"], authors=rec["authors"], year=rec["year"],
            type=work.get("type"), topic=topic, family=family,
            status="pending_lookup" if pending else "unresolved",
            reasons=sorted(set(res.reasons))[:12], attempts=attempts,
            last_tried=iso(self.now),
            next_retry_at=iso(self.now) if pending else backoff_iso(self.now, attempts),
        )

    def consider(self, work: dict, topic: str, family: str, sim_implied: bool = False) -> None:
        """Gate, dedup and resolve one work from a page (never raises LookupBudget)."""
        st = self.stats
        st.considered += 1
        title = (work.get("title") or work.get("display_name") or "").strip()
        if not sim_relevant(work, self.openalex_relevant, sim_implied):
            st.relevance_rejected += 1
            st.example("relevance_drop", title)
            return
        if why := oar.work_excluded(work):
            st.excluded += 1
            st.example("excluded", f"{why} | {title}")
            return
        if kind := self._known(work):
            st.dup(kind)
            if kind == "identity_known_not_held":
                st.known_not_held += 1
                pids = work_pids(work)
                if pids:  # replacing it is an explicit, provenance-preserving transaction
                    self.ledger.upsert(pids[0], ids=pids, title=title, status="known_not_held",
                                       last_tried=iso(self.now),
                                       reasons=["registered row without held eligible content; "
                                                "replacement needs an explicit transaction"])
            return
        try:
            res, chosen, extra, note = self.resolve(work)
        except (LookupBudget, UpstreamError) as exc:
            # out of lookups, or a supplementary upstream failed: park the work for a later
            # retry (the page position still moves on; nothing about it is lost)
            if isinstance(exc, UpstreamError):
                st.reasons["lookup_failed"] = st.reasons.get("lookup_failed", 0) + 1
            res = oar.select_copy(work, self.policy)
            st.pending_lookup += 1
            self._tally(res)
            self._record_unresolved(work, res, topic, pending=True, family=family)
            return
        self._tally(res)
        if res.status == "excluded":
            st.excluded += 1
            return
        if res.status == "resolved" and chosen is not None:
            if chosen is not work and self._known(chosen, extra):
                st.dup("related_version")
                return
            if self._accept(chosen, res, topic, origin_extra=extra, relation=note):
                st.resolved += 1
                via = note.split(" ", 2)[:2] if note else ["openalex", "location"]
                st.resolved_via[" ".join(via)] = st.resolved_via.get(" ".join(via), 0) + 1
            return
        st.unresolved += 1
        if any(r.startswith("rights_rejected") for r in res.reasons):
            st.rights_rejected += 1
            st.example("rights_drop", f"{'; '.join(sorted(set(res.reasons)))[:120]} | {title}")
        else:
            st.example("unresolved", f"{'; '.join(sorted(set(res.reasons)))[:120]} | {title}")
        self._record_unresolved(work, res, topic, family=family)

    def _tally(self, res: oar.Resolution) -> None:
        for kind in res.reason_kinds():
            self.stats.reasons[kind] = self.stats.reasons.get(kind, 0) + 1

    # -- the page --------------------------------------------------------------------------------
    def search_page(self, pos: Position, family: str) -> None:
        """Consume the page at `pos` from pos.k; update pos in place (see module docstring).
        Raises UpstreamError on a failed search (the caller keeps the committed cursor)."""
        key, expr, topic = SIM_QUERIES[pos.q]
        filters = [f"title_and_abstract.search:{expr}", WINDOWS[pos.w], TYPES]
        if family == "ssrn":
            filters.append(f"primary_location.source.id:{SSRN_SOURCE_ID}")
        else:
            filters.append("open_access.is_oa:true")
        params = {"filter": ",".join(filters), "per-page": self.per, "page": pos.p,
                  "sort": "cited_by_count:desc"}
        self.stats.searched = f"{family} q={pos.q}:{key} w={WINDOWS[pos.w]} p={pos.p} k={pos.k}"
        data = self.api.search(params)
        results = data.get("results") or []
        count = int((data.get("meta") or {}).get("count") or 0)
        self.stats.results = len(results)
        for index in range(pos.k, len(results)):
            if len(self.out) >= self.max_docs:
                pos.k = index  # unfinished page: revisit it, skipping what was consumed
                self.stats.stopped_at_max = True
                return
            work = results[index]
            if isinstance(work, dict):
                self.consider(work, topic, family, key in SIM_IMPLIED)
        if (len(results) < self.per or pos.p * self.per >= min(count, MAX_PAGE_DEPTH)):
            pos.advance_window()
        else:
            pos.p, pos.k = pos.p + 1, 0

    def retry_due(self) -> None:
        """Re-resolve due works from the resolution record with the lookups that remain."""
        for row in self.ledger.due(iso(self.now), limit=max(0, self.api.lookups_left)):
            if len(self.out) >= self.max_docs or self.api.lookups_left <= 0:
                return
            wid = next((p.split(":", 1)[1] for p in row.get("ids", [])
                        if p.startswith("openalex:")), None)
            try:
                work = self.api.lookup("openalex", wid) if wid else None
            except (LookupBudget, UpstreamError):
                return  # the row stays due
            self.stats.retried += 1
            if not work:
                row.update(status="unresolved", attempts=int(row.get("attempts") or 0) + 1,
                           last_tried=iso(self.now),
                           next_retry_at=backoff_iso(self.now, int(row.get("attempts") or 0) + 1))
                continue
            if kind := self._known(work):
                row.update(status="known_not_held" if kind == "identity_known_not_held"
                           else "resolved", last_tried=iso(self.now), reasons=[f"dedup:{kind}"])
                continue
            try:
                res, chosen, extra, note = self.resolve(work)
            except (LookupBudget, UpstreamError):
                return  # the row stays due
            if res.status == "resolved" and chosen is not None and not (
                    chosen is not work and self._known(chosen, extra)):
                if self._accept(chosen, res, row.get("topic") or "building_energy",
                                origin_extra=extra, relation=note or "resolution retry"):
                    self.stats.retry_resolved += 1
                row.update(status="resolved", last_tried=iso(self.now))
                continue
            if res.status == "excluded":
                row.update(status="excluded", reasons=res.reasons, last_tried=iso(self.now))
                continue
            attempts = int(row.get("attempts") or 0) + 1
            row.update(status="unresolved", attempts=attempts, reasons=sorted(set(res.reasons))[:12],
                       last_tried=iso(self.now), next_retry_at=backoff_iso(self.now, attempts))

    def run(self, cursor: Cursor) -> Cursor:
        """One scheduled step. Returns the next cursor; raises UpstreamError (cursor kept)."""
        nxt = Cursor(cursor.t, Position(**asdict(cursor.sim)), Position(**asdict(cursor.ssrn)))
        slot = turn(cursor.t)
        self.stats.tick, self.stats.slot = cursor.t, slot
        if slot in ("sim", "ssrn"):
            self.search_page(nxt.sim if slot == "sim" else nxt.ssrn, slot)
        # legacy ticks spend no search here; every tick drains due retries with free lookups
        self.retry_due()
        nxt.t = cursor.t + 1
        return nxt


def report(stats: Stats, api: Api, cursor_in: str, cursor_out: str | None,
           elapsed: float) -> None:
    s = asdict(stats)
    examples = s.pop("examples")
    print(f"# family {FAMILY_VERSION}: tick {stats.tick} slot={stats.slot} {stats.searched}")
    print(f"# cursor {cursor_in} -> {cursor_out or '(held)'}")
    print(f"# requests: {api.searches} OpenAlex search, {api.lookups} supplementary lookups, "
          f"{api.seconds:.1f}s network, {elapsed:.1f}s total; {api.budget_note}")
    print("# stats: " + json.dumps(s, sort_keys=True, ensure_ascii=False))
    for kind, rows in examples.items():
        for row in rows:
            print(f"# example {kind}: {row}")


def main_family(args, *, policy: dict, keys, cooldowns: dict, save_cooldowns, get,
                openalex_relevant, append_entries, request_hold, report_next,
                now: float | None = None, sleep=time.sleep) -> int:
    """find_sources.py --family simulation. Returns the process exit code."""
    started = time.monotonic()
    now = time.time() if now is None else now
    cursor = parse_cursor(args.family_cursor)
    ledger_path = Path(args.resolution_file) if args.resolution_file else (
        default_ledger_path() if args.append else None)
    ledger = Ledger(ledger_path)
    api = Api(get, lookup_max=args.lookup_max, cooldowns=cooldowns,
              save_cooldowns=save_cooldowns, now=lambda: now, sleep=sleep)
    run = FamilyRun(api=api, policy=policy, keys=keys, ledger=ledger, per=args.per,
                    max_docs=args.max, openalex_relevant=openalex_relevant, now=now)
    slot = turn(cursor.t)
    if slot != "legacy" and cooldowns.get("openalex", 0) > now:
        request_hold(f"OpenAlex cooldown active for {int(cooldowns['openalex'] - now)}s")
        report(run.stats, api, cursor.render(), None, time.monotonic() - started)
        return 0
    try:
        nxt = run.run(cursor)
    except UpstreamError as exc:
        # An upstream failure is never exhaustion: the committed cursor stays and nothing is
        # proposed (a partial page must not advance past unread results).
        print(f"# ERROR: {exc}; cursor kept at {cursor.render()}", file=sys.stderr)
        report(run.stats, api, cursor.render(), None, time.monotonic() - started)
        return 1
    # ids are identity ids (dedup.identity_id), already checked against the store and this run
    report(run.stats, api, cursor.render(), nxt.render(), time.monotonic() - started)
    print(f"# {len(run.out)} NEW candidates with accepted rights for the selected copy")
    import yaml
    print(yaml.safe_dump(run.out, sort_keys=False, allow_unicode=True))
    if args.append:
        if run.out:
            append_entries(run.out)
        ledger.save()
        report_next(nxt.render())
    elif args.resolution_file:
        ledger.save()
    return 0
