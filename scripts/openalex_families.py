#!/usr/bin/env python3
"""openalex_families.py — independently versioned OpenAlex query families for find_sources.py.

The legacy OpenAlex family (find_sources.QUERIES, 105 frozen queries, integer cursor) stays as
it is. A FAMILY is a separately configured query list with its own backend and dynamic rotation
cursor, run as `find_sources.py --family <name> --family-cursor <cursor>`:

    simulation   (find_openalex_sim, source openalex_sim)
        sim1 t=<n> q=<query> w=<date window> p=<page> k=<consumed> sq=… sw=… sp=… sk=…
        building-energy simulation; sq/sw/sp/sk walk the SSRN-origin sub-family (records with no
        known OA copy yet; SSRN itself is never fetched — only other licensed copies are)
    building-ai  (find_openalex_ai, source openalex_ai)
        ai1 t=<n> q=… w=… p=… k=…
        LLM / generative-AI / agent work on building energy and operation that has no simulation
        anchor (kept out of the simulation family on purpose)

* ONE OpenAlex budget. A search costs 10 of the 1,000 anonymous daily credits and every OpenAlex
  backend shares them: each round spends at most ONE search. The owner of a round's search is
  budget_slot(NEKAISE_RUN_ID) — a hash of the round id over SCHEDULE (6/10 simulation, 1/10
  SSRN-origin, 1/10 building-ai, 2/10 legacy). It depends on nothing a backend's progress or
  failure can freeze, so a failing family never starves another. A standalone run (no round id)
  uses its cursor tick instead. Singleton work lookups and Crossref/Unpaywall calls are free
  supplementary lookups, capped per run (--lookup-max).
* Position is never lost and never passes unfinished work. A page is (query, date window, page);
  `k` counts results of that page already FINISHED. Hitting --max accepted documents, running out
  of lookups, or a failed supplementary lookup stops at that result and keeps the page with its
  `k`; a failed or malformed search keeps the whole committed cursor (exit 1). A fully read
  query/window moves on; after the last query the walk starts a new pass — never "exhausted".
* The resolution record (workspace/openalex-resolution.jsonl) is OPTIONAL ACCELERATION: it lets a
  later run re-check an unresolved work (e.g. an SSRN preprint whose published version may get a
  licensed copy) sooner than the next pass of the walk. Losing it, or a round rolling back after
  it was written, loses nothing: a retry is marked resolved only once a later run sees the work
  registered in the store.

Changing a family's queries or windows requires a new version and a reviewed cursor migration
(parse_cursor refuses a cursor of another version).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import bes_relevance
import dedup
import oa_resolution as oar
import ops
import registry

OPENALEX = "https://api.openalex.org/works"
SSRN_SOURCE_ID = "S4210172589"  # OpenAlex source "SSRN Electronic Journal"
SEARCH_CREDITS = 10             # x-ratelimit-cost: 0.001 USD = 10 credits per search (2026-09-25)
MAX_PAGE_DEPTH = 10_000         # OpenAlex page-based paging limit (page * per-page)
LEDGER_CAP = 20_000
# The most supplementary lookups ONE work can need (Crossref + Unpaywall + the bounded related
# versions and their confirmation lookups, oa_resolution.MAX_RELATIONS): a run's --lookup-max
# must cover it, so a work always finishes within one run's budget and a cap stop always makes
# progress on the next run.
MAX_LOOKUPS_PER_WORK = 2 + 2 * oar.MAX_RELATIONS
TYPES = "type:article|preprint"
WINDOWS: tuple[str, ...] = (
    "publication_year:>2024", "publication_year:2020-2024", "publication_year:2015-2019",
    "publication_year:2005-2014", "publication_year:<2005",
)
# The shared OpenAlex budget: which walk owns a round's single search.
SCHEDULE: tuple[str, ...] = ("sim", "sim", "legacy", "sim", "ai", "ssrn", "sim", "sim",
                             "legacy", "sim")
SLOT_OWNER = {"sim": "find_openalex_sim", "ssrn": "find_openalex_sim",
              "ai": "find_openalex_ai", "legacy": "find_openalex"}


def budget_slot(run_id: str | None, tick: int = 0) -> str:
    """The walk that owns this round's OpenAlex search: from the round id when there is one
    (every backend of the round computes the same slot), else from the cursor tick."""
    if run_id:
        return SCHEDULE[int(hashlib.sha256(run_id.encode()).hexdigest()[:12], 16)
                        % len(SCHEDULE)]
    return SCHEDULE[tick % len(SCHEDULE)]


def legacy_may_search(run_id: str | None, partners_enabled: dict[str, bool]) -> tuple[bool, str]:
    """Whether the legacy family owns this round's search: its own slot, or a slot whose owning
    family backend is disabled (the budget is not wasted on a paused family)."""
    if not run_id:
        return True, "standalone run"
    slot = budget_slot(run_id)
    owner = SLOT_OWNER[slot]
    if slot == "legacy":
        return True, f"round slot {slot}"
    if owner in partners_enabled and not partners_enabled[owner]:
        return True, f"round slot {slot} belongs to disabled {owner}"
    return False, f"round slot {slot} belongs to {owner}"


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
AI_ANCHOR = re.compile(
    r"language models?|\bllms?\b|generative ai|\bgpt|chatgpt|foundation models?|\bagents?\b|"
    r"agentic|大语言模型|语言模型|大規模言語モデル|sprachmodell|modèles? de langage",
    re.I,
)
# Building anchors that are safe in an ABSTRACT of an AI paper ("building an agent" is a verb).
AI_BUILT_ABSTRACT = re.compile(
    r"\bhvac\b|building energy|\bbuildings\b|smart buildings?|building (?:automation|management|"
    r"operation|operations|performance|systems?|stock|retrofit)|indoor|thermal comfort|"
    r"heat pumps?|energy retrofit|ventilation|air[- ]condition|district heating|occupant",
    re.I,
)


def _building_title(work: dict, title: str, openalex_relevant) -> bool:
    return bool(openalex_relevant(work, title) or bes_relevance.relevant(title, strict=True)
                or MULTILINGUAL_BUILT.search(title))


def sim_relevant(work: dict, openalex_relevant, sim_implied: bool = False) -> bool:
    """Simulation AND built-environment evidence. Generic "agents", "digital twin" or "energy"
    alone is not enough: a simulation/tool anchor must meet a building anchor — in the title, in
    OpenAlex's AEC subfields, or in the abstract when the text also names a building-simulation
    tool. `sim_implied`: the query expression itself required a simulation term."""
    title = (work.get("title") or work.get("display_name") or "").strip()
    if not title or bes_relevance.vetoed(title):
        return False
    abstract = oar.abstract_text(work)
    text = f"{title} {abstract}"
    if not sim_implied and not SIM_ANCHOR.search(text):
        return False
    if _building_title(work, title, openalex_relevant):
        return True
    return bool(TOOL_ANCHOR.search(text) and (bes_relevance.BUILT.search(abstract)
                                               or MULTILINGUAL_BUILT.search(abstract)))


def ai_relevant(work: dict, openalex_relevant, ai_implied: bool = False) -> bool:
    """AI/LLM AND building evidence: a building anchor in the title/AEC subfields, or a
    building-operation phrase in the abstract (never the verb "building")."""
    title = (work.get("title") or work.get("display_name") or "").strip()
    if not title or bes_relevance.vetoed(title):
        return False
    abstract = oar.abstract_text(work)
    if not ai_implied and not AI_ANCHOR.search(f"{title} {abstract}"):
        return False
    # bes_relevance's title gate is not used here: its \bbuilding\b also matches the verb
    # ("Building LLM agents for …"), which AI titles are full of
    return bool(openalex_relevant(work, title) or MULTILINGUAL_BUILT.search(title)
                or AI_BUILT_ABSTRACT.search(title) or AI_BUILT_ABSTRACT.search(abstract)
                or MULTILINGUAL_BUILT.search(abstract))


# --- families -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Family:
    name: str
    version: str
    source: str
    backend: str
    queries: tuple[tuple[str, str, str], ...]  # (key, title_and_abstract.search expr, topic)
    implied: frozenset                          # query keys whose expression proves the anchor
    relevant: Callable
    walks: tuple[str, ...]                      # schedule slots this family owns, main walk first


SIMULATION = Family(
    name="simulation", version="sim1", source="openalex_sim", backend="find_openalex_sim",
    queries=(
        ("modelica", '(Modelica OR OpenModelica OR Dymola) AND (building OR buildings OR HVAC OR '
                     '"heat pump" OR "district heating" OR "thermal zone")', "building_energy"),
        ("modelica-libraries", '"Modelica Buildings" OR "Buildings library" OR AixLib OR '
                               '"IDEAS library" OR "IBPSA Project" OR BuildingSystems OR '
                               'BuildSysPro', "building_energy"),
        ("energyplus", '(EnergyPlus OR "Spawn of EnergyPlus" OR BOPTEST OR OpenStudio OR TRNSYS OR '
                       '"IDA ICE" OR "ESP-r" OR DOE-2) AND (building OR buildings OR HVAC)',
         "building_energy"),
        ("calibration", '("building energy simulation" OR "building performance simulation" OR '
                        '"building energy model" OR "building energy modeling" OR '
                        '"building energy modelling") AND (calibration OR calibrated OR '
                        'validation)', "building_energy"),
        ("hvac-mpc", '("model predictive control" OR MPC) AND (HVAC OR building OR buildings) AND '
                     '(simulation OR co-simulation OR emulator OR "building model")',
         "controls_bas"),
        ("digital-twin", '"digital twin" AND (building OR buildings OR HVAC) AND (simulation OR '
                         '"energy model" OR "physics-based" OR "building energy")',
         "building_energy"),
        ("llm-simulation", '("large language model" OR "large language models" OR LLM OR LLMs) '
                           'AND (EnergyPlus OR Modelica OR "building energy" OR "building '
                           'simulation" OR HVAC OR "energy model")', "building_energy"),
        ("co-simulation", '(co-simulation OR "functional mock-up" OR FMU) AND (building OR '
                          'buildings OR HVAC OR "district heating")', "building_energy"),
        ("de", 'Gebäudesimulation OR "thermische Gebäudesimulation" OR "energetische '
               'Gebäudesimulation" OR Anlagensimulation OR "dynamische Gebäudesimulation"',
         "building_energy"),
        ("fr", '("simulation énergétique" OR "simulation thermique dynamique") AND (bâtiment OR '
               'bâtiments OR logement)', "building_energy"),
        ("es-pt-it", '"simulación energética" OR "simulação energética" OR "simulazione '
                     'energetica" OR "simulación térmica" OR "simulação termoenergética" OR '
                     '"simulazione dinamica"', "building_energy"),
        ("zh", '建筑能耗模拟 OR 建筑能耗仿真 OR 建筑能源模拟 OR 建筑热环境模拟 OR 暖通空调仿真',
         "building_energy"),
        ("ja-ko", '建築 シミュレーション OR 熱負荷計算 OR 空調 シミュレーション OR "건물 에너지 시뮬레이션" '
                  'OR "건물 에너지 해석"', "building_energy"),
        ("nl-nordic", 'gebouwsimulatie OR "energiesimulatie" OR bygningssimulering OR '
                      'byggnadssimulering OR energisimulering OR rakennussimulointi',
         "building_energy"),
    ),
    implied=frozenset({"modelica", "modelica-libraries", "energyplus", "calibration",
                       "hvac-mpc", "co-simulation", "de", "fr", "es-pt-it", "zh", "ja-ko",
                       "nl-nordic"}),
    relevant=sim_relevant,
    walks=("sim", "ssrn"),
)

BUILDING_AI = Family(
    name="building-ai", version="ai1", source="openalex_ai", backend="find_openalex_ai",
    queries=(
        ("llm-building", '("large language model" OR "large language models" OR LLM OR LLMs OR '
                         '"generative AI" OR GPT OR ChatGPT OR "foundation model") AND ("building '
                         'energy" OR HVAC OR "smart building" OR "smart buildings" OR "building '
                         'operation" OR "building management" OR "energy retrofit" OR "building '
                         'automation")', "building_energy"),
        ("agents", '("AI agent" OR "AI agents" OR "LLM agent" OR "LLM agents" OR agentic OR '
                   '"multi-agent") AND (HVAC OR "building energy" OR "building management" OR '
                   '"building automation" OR "building operation")', "controls_bas"),
        ("llm-operations", '("large language model" OR LLM OR "generative AI") AND ("fault '
                           'detection" OR "fault diagnosis" OR commissioning OR "energy '
                           'management") AND (building OR buildings OR HVAC)',
         "commissioning_fdd"),
        ("zh", '大语言模型 AND (建筑 OR 暖通 OR 空调)', "building_energy"),
        ("de-fr", '(Sprachmodell OR Sprachmodelle OR "modèle de langage" OR "modèles de langage") '
                  'AND (Gebäude OR Gebäudetechnik OR bâtiment OR bâtiments)', "building_energy"),
    ),
    implied=frozenset({"llm-building", "agents", "llm-operations", "zh", "de-fr"}),
    relevant=ai_relevant,
    walks=("ai",),
)
FAMILIES = {f.name: f for f in (SIMULATION, BUILDING_AI)}
# kept for callers/tests of the first version
SIM_QUERIES = SIMULATION.queries
SIM_IMPLIED = SIMULATION.implied
FAMILY_VERSION = SIMULATION.version
SOURCE = SIMULATION.source


# --- cursor ---------------------------------------------------------------------------------------

@dataclass
class Position:
    q: int = 0
    w: int = 0
    p: int = 1
    k: int = 0

    def advance_window(self, n_queries: int) -> None:
        self.p, self.k = 1, 0
        self.w += 1
        if self.w >= len(WINDOWS):
            self.w = 0
            self.q = (self.q + 1) % n_queries


WALK_PREFIX = {"sim": "", "ssrn": "s", "ai": ""}


@dataclass
class Cursor:
    family: Family
    t: int = 0
    walks: dict = field(default_factory=dict)  # walk name -> Position

    def render(self) -> str:
        parts = [self.family.version, f"t={self.t}"]
        for walk in self.family.walks:
            pos, pre = self.walks[walk], WALK_PREFIX[walk]
            parts += [f"{pre}q={pos.q}", f"{pre}w={pos.w}", f"{pre}p={pos.p}", f"{pre}k={pos.k}"]
        return " ".join(parts)

    def copy(self) -> "Cursor":
        return Cursor(self.family, self.t,
                      {w: Position(p.q, p.w, p.p, p.k) for w, p in self.walks.items()})

    # compatibility accessors
    @property
    def sim(self) -> Position:
        return self.walks["sim"]

    @property
    def ssrn(self) -> Position:
        return self.walks["ssrn"]


def parse_cursor(value: str, family: Family = SIMULATION) -> Cursor:
    tokens = (value or "").strip().split()
    if not tokens:
        raise ValueError(f"malformed family cursor: {value!r}")
    if tokens[0] != family.version:
        raise ValueError(f"family cursor is {tokens[0]}, this code walks {family.version}: "
                         "migrate the committed cursor before changing the family")
    fields_ = {}
    for token in tokens[1:]:
        m = re.fullmatch(r"([a-z]+)=(\d+)", token)
        if not m or m[1] in fields_:
            raise ValueError(f"malformed family cursor: {value!r}")
        fields_[m[1]] = int(m[2])
    keys = ["t"] + [WALK_PREFIX[w] + k for w in family.walks for k in "qwpk"]
    if sorted(fields_) != sorted(keys):
        raise ValueError(f"malformed family cursor: {value!r}")
    cur = Cursor(family, fields_["t"])
    for walk in family.walks:
        pre = WALK_PREFIX[walk]
        pos = Position(*(fields_[pre + k] for k in "qwpk"))
        if not (pos.q < len(family.queries) and pos.w < len(WINDOWS) and pos.p >= 1
                and 0 <= pos.k <= 200):
            raise ValueError(f"family cursor out of range: {value!r}")
        cur.walks[walk] = pos
    return cur


# --- HTTP with budgets and schema checks ----------------------------------------------------------

class UpstreamError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retry_at: float | None = None):
        super().__init__(message)
        self.status, self.retry_at = status, retry_at


class LookupBudget(Exception):
    """The per-run supplementary lookup cap is spent."""


def check_search(data, per: int, page: int = 1) -> tuple[list[dict], int]:
    """(results, meta.count) of a well-formed OpenAlex list response for `page`, else
    UpstreamError: an error object or a malformed page must never read as an empty (finished)
    page. A page is short only where the count says the result list ends: its length must be
    exactly what meta.count leaves for this position (bounded by the paging depth)."""
    if not isinstance(data, dict) or "error" in data:
        raise UpstreamError(f"OpenAlex search returned an error or no object: {str(data)[:200]}")
    results, meta = data.get("results"), data.get("meta")
    if not isinstance(results, list) or not isinstance(meta, dict):
        raise UpstreamError("OpenAlex search response lacks results/meta")
    count = meta.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise UpstreamError(f"OpenAlex search meta.count is not a count: {count!r}")
    if len(results) > per or any(not isinstance(r, dict) or not r.get("id") for r in results):
        raise UpstreamError("OpenAlex search results are malformed")
    if results and count < len(results):
        raise UpstreamError("OpenAlex search meta.count is smaller than the page")
    if meta.get("page") not in (None, page):
        raise UpstreamError(f"OpenAlex answered page {meta.get('page')!r} for page {page}")
    expected = min(per, max(0, min(count, MAX_PAGE_DEPTH) - (page - 1) * per))
    if len(results) != expected:
        raise UpstreamError(f"OpenAlex page {page} has {len(results)} results where "
                            f"meta.count={count} implies {expected}")
    return results, count


OPENALEX_SPACING = 1.0  # seconds between ANY two OpenAlex requests of this machine's finders


class SharedPacer:
    """Spacing shared by every process on this machine (the family finders of one round run
    concurrently): a lock file serializes callers, a timestamp file remembers the last request
    start. `clock`/`sleep` are injectable; the files live in workspace/ (never committed)."""

    def __init__(self, name: str = "openalex-pace", spacing: float = OPENALEX_SPACING, *,
                 clock=time.time, sleep=time.sleep, workspace: Path | None = None,
                 timeout: float = 300.0):
        self.name, self.spacing, self.clock, self.sleep = name, spacing, clock, sleep
        self.workspace, self.timeout = workspace, timeout

    def wait(self) -> None:
        ws = Path(self.workspace) if self.workspace is not None else ops.WORKSPACE
        with ops.named_lock(self.name, timeout=self.timeout, workspace=ws):
            stamp = ws / f"{self.name}.json"
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


RATE_HEADERS = ("retry-after", "x-ratelimit-remaining", "x-ratelimit-limit",
                "x-ratelimit-reset", "x-ratelimit-remaining-usd", "x-ratelimit-cost-usd")


def _headers_lower(headers) -> dict:
    try:
        return {str(k).lower(): v for k, v in dict(headers).items()}
    except (TypeError, ValueError):
        return {}


class Api:
    """Counts requests; enforces the lookup cap, OpenAlex budget headers, persisted cooldowns
    and the machine-wide OpenAlex spacing; validates every response's shape. `get` is
    requests.get (tests replace it). `now` is a LIVE clock: every cooldown deadline is computed
    when the answer arrives, never from the run's start."""

    def __init__(self, get, *, lookup_max: int, cooldowns: dict, save_cooldowns, now,
                 sleep=time.sleep, pacer=None):
        self.get, self.lookup_max = get, lookup_max
        self.cooldowns, self.save_cooldowns, self.now, self.sleep = (
            cooldowns, save_cooldowns, now, sleep)
        self.pacer = pacer if pacer is not None else LocalPacer()
        self.searches = 0
        self.lookups = 0
        self.seconds = 0.0
        self.budget_note = ""
        self.throttle: dict | None = None  # the rate-limit headers of the last 429/503

    @property
    def lookups_left(self) -> int:
        return max(0, self.lookup_max - self.lookups)

    def _request(self, url, params, *, host_key: str):
        until = self.cooldowns.get(host_key, 0)
        if until > self.now():
            raise UpstreamError(f"{host_key} cooldown active for {int(until - self.now())}s",
                                429, until)
        if host_key == "openalex":
            try:
                self.pacer.wait()
            except RuntimeError as exc:
                raise UpstreamError(f"OpenAlex pacing lock unavailable: {exc}") from exc
        started = time.monotonic()
        try:
            r = self.get(url, params=params, timeout=30,
                         headers={"User-Agent": f"nekaise-corpus/find_sources "
                                                f"(mailto:{oar.MAILTO})"})
        except Exception as exc:
            raise UpstreamError(f"{host_key} request failed: {exc}") from exc
        finally:
            self.seconds += time.monotonic() - started
        headers = getattr(r, "headers", {}) or {}
        status = getattr(r, "status_code", 200)
        if host_key == "openalex":
            self._note_openalex_budget(headers)
        if status in (429, 503):
            raise self._throttled(host_key, status, headers)
        if status == 404:
            return None
        if status >= 400:
            raise UpstreamError(f"{host_key} HTTP {status}", status)
        try:
            return r.json()
        except ValueError as exc:
            raise UpstreamError(f"{host_key} returned non-JSON: {exc}") from exc

    def _throttled(self, host_key: str, status: int, headers) -> "UpstreamError":
        """Classify a 429/503 from its rate-limit headers — the daily credit BUDGET spent
        (cooldown until the reset) versus request-RATE limiting (cooldown for Retry-After) —
        persist the cooldown from the live clock, and keep the headers for the run record."""
        h = _headers_lower(headers)
        self.throttle = {k: h[k] for k in RATE_HEADERS if k in h}

        def number(key):
            try:
                return int(float(h[key]))
            except (KeyError, TypeError, ValueError):
                return None

        remaining, reset, retry = (number("x-ratelimit-remaining"),
                                   number("x-ratelimit-reset"), number("retry-after"))
        if host_key == "openalex" and remaining is not None and remaining < SEARCH_CREDITS:
            kind, wait = "budget exhausted", reset if reset is not None else 3600
        else:
            kind, wait = "rate limited", retry if retry is not None else 3600
        deadline = self.now() + max(0, wait)
        self.cooldowns[host_key] = max(self.cooldowns.get(host_key, 0), deadline)
        self.save_cooldowns(self.cooldowns)
        detail = ", ".join(f"{k}={v}" for k, v in self.throttle.items()) or "no rate headers"
        self.budget_note = f"{host_key} HTTP {status}: {kind} ({detail})"
        return UpstreamError(f"{host_key} HTTP {status}: {kind} ({detail})", status, deadline)

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

    def search(self, params: dict, per: int) -> tuple[list[dict], int]:
        self.searches += 1
        return check_search(
            self._request(OPENALEX, {**params, "mailto": oar.MAILTO}, host_key="openalex"), per,
            int(params.get("page", 1)))

    def lookup(self, kind: str, key: str) -> dict | None:
        """One supplementary lookup (counted), schema-checked: an OpenAlex work, a Crossref
        work message or an Unpaywall DOI record; None only for a clean 404."""
        if self.lookups >= self.lookup_max:
            raise LookupBudget()
        self.lookups += 1
        if kind != "openalex":  # OpenAlex requests are spaced by the shared pacer
            self.sleep(1.0)
        if kind == "openalex":
            data = self._request(f"{OPENALEX}/{key}", {"mailto": oar.MAILTO},
                                 host_key="openalex")
            if data is not None and not (isinstance(data, dict) and "error" not in data
                                         and oar.normalize_openalex(data.get("id"))):
                raise UpstreamError(f"malformed OpenAlex work for {key}")
            return data
        if kind == "crossref":
            data = self._request(f"https://api.crossref.org/works/{key}",
                                 {"mailto": oar.MAILTO}, host_key="crossref")
            if data is None:
                return None
            if not (isinstance(data, dict) and isinstance(data.get("message"), dict)):
                raise UpstreamError(f"malformed Crossref record for {key}")
            return data["message"]
        if kind == "unpaywall":
            data = self._request(f"https://api.unpaywall.org/v2/{key}",
                                 {"email": oar.MAILTO}, host_key="unpaywall")
            if data is not None and not (isinstance(data, dict) and "error" not in data
                                         and isinstance(data.get("doi"), str)
                                         and isinstance(data.get("oa_locations", []), list)):
                raise UpstreamError(f"malformed Unpaywall record for {key}")
            return data
        raise ValueError(kind)


# --- resolution record (optional acceleration) ----------------------------------------------------

def default_ledger_path() -> Path:
    return ops.WORKSPACE / "openalex-resolution.jsonl"


class Ledger:
    """Unresolved works a later run may re-check early, keyed by their first normalized
    persistent id. A git-ignored ACCELERATION cache (see module docstring), written atomically."""

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

    def due(self, now_iso: str, limit: int, family: str) -> list[dict]:
        rows = [r for r in self.rows.values()
                if r.get("status") in ("unresolved", "proposed")
                and r.get("family_name", "simulation") == family
                and (r.get("next_retry_at") or "") <= now_iso]
        rows.sort(key=lambda r: (r.get("next_retry_at") or "", r["key"]))
        return rows[:limit]

    def save(self) -> None:
        if self.path is None:
            return
        rows = list(self.rows.values())
        if len(rows) > LEDGER_CAP:  # forget settled rows first, then the oldest
            rows.sort(key=lambda r: (r.get("status") in ("unresolved", "proposed"),
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
    retried: int = 0
    retry_resolved: int = 0
    stopped: str = ""
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
    """A registry entry for the resolved copy, carrying its rights evidence and identity.

    It assumes ACCEPTED rights (license = rights.tag). The collect-all follow-up cannot just
    widen oa_resolution.copy_acceptable: a rejected/unknown/conflicting copy has no tag, so this
    entry would lose `license` and fail lint. That change needs an explicit rights
    classification, a persisted rights status, and lint / licence-class folder handling."""
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


class StopPage(Exception):
    """Stop consuming the page at the current result (it stays unfinished)."""


class FamilyRun:
    """One invocation of a query family (see module docstring)."""

    def __init__(self, *, api: Api, policy: dict, keys, ledger: Ledger, per: int, max_docs: int,
                 openalex_relevant, now: float, held_rows=dedup.held_rows,
                 family: Family = SIMULATION):
        self.api, self.policy, self.keys, self.ledger = api, policy, keys, ledger
        self.per, self.max_docs = per, max_docs
        self.openalex_relevant = openalex_relevant
        self.now = now
        self.today = iso(now)[:10]
        self.held_rows = held_rows
        self.family = family
        self.out: list[dict] = []
        self.stats = Stats()

    # -- dedup ---------------------------------------------------------------------------------
    def _prefetch(self, works: list[dict]) -> None:
        """Answer the whole page's dedup keys in one store round trip (known_pids is an
        unindexed scan on PostgreSQL: never one call per work)."""
        pids = [p for w in works for p in work_pids(w)]
        self.keys.prefetch(
            pids=pids, ids=[i for i in map(dedup.identity_id, pids) if i],
            titles=[registry.norm(w.get("title") or w.get("display_name") or "") for w in works],
            urls=[u for w in works for u in work_urls(w)])

    def _known(self, work: dict, extra_pids: list[str] = ()) -> str | None:
        pids = list(dict.fromkeys([*work_pids(work), *extra_pids]))
        ids = [i for i in map(dedup.identity_id, pids) if i]
        self.keys.prefetch(ids=ids, pids=pids)
        hit_ids = [i for i in ids if i in self.keys.ids]
        if hit_ids:
            rows = self.held_rows(hit_ids)
            if any(r.get("status") == "ok" for r in rows.values()):
                return "identity_held"
            return "identity_known_not_held"
        if any(p in self.keys.pids for p in pids):
            return "persistent_id"  # declared by a row of any source (persistent_id/origin_ids)
        title = registry.norm(work.get("title") or work.get("display_name") or "")
        if title and title in self.keys.titles:
            return "title"
        urls = work_urls(work)
        self.keys.prefetch(urls=urls)
        if any(u in self.keys.urls for u in urls):
            return "url"  # includes pruned_urls.txt: prune decisions are never undone here
        return None

    def _accept(self, work: dict, res: oar.Resolution, topic: str, *, origin_extra=(),
                relation: str = "") -> dict | None:
        entry = build_entry(work, res, topic=topic, source=self.family.source,
                            family=self.family.version, today=self.today,
                            origin_extra=list(origin_extra), relation=relation)
        url, title = entry["url"].rstrip("/"), registry.norm(entry["title"])
        if url in self.keys.urls or title in self.keys.titles or self.keys.identity_known(entry):
            self.stats.dup("selected_copy")
            return None
        self.keys.urls.add(url)
        self.keys.titles.add(title)
        self.keys.add_identity(entry)
        self.out.append(entry)
        self.stats.example("retain", f"{entry['id']} | {entry['license']} | {entry['url']} | "
                                     f"{entry['title']}")
        return entry

    # -- resolution ------------------------------------------------------------------------------
    def resolve(self, work: dict) -> tuple[oar.Resolution, dict | None, list[str], str]:
        """select_copy with bounded supplementary evidence. Returns (resolution, the work whose
        copy was chosen, extra origin pids, relation note). Raises LookupBudget / UpstreamError
        when a lookup that could still change the outcome is unaffordable or fails."""
        res = oar.select_copy(work, self.policy)
        if res.status != "unresolved":
            return res, work, [], ""
        doi = oar.normalize_doi(work.get("doi"))
        if not doi:
            return res, work, [], ""
        # Crossref where it can change the outcome: a preprint may have an explicitly related
        # published version; a PDF on an allowed host with a merely UNKNOWN licence may gain
        # version-bound evidence for the DOI's own copy.
        preprint = work.get("type") == "preprint" or doi.startswith("10.2139/")
        fixable = any(r.startswith("rights_unknown:") for r in res.reasons)
        crossref = None
        if preprint or fixable:
            crossref = self.api.lookup("crossref", doi)
            res = oar.select_copy(work, self.policy, crossref=crossref)
            if res.status == "resolved":
                return res, work, [], "crossref licence evidence"
        # Unpaywall: a bounded metadata fallback for every unresolved DOI — works with no OpenAlex
        # PDF and SSRN (10.2139) DOIs included. It is a lookup at api.unpaywall.org only; its
        # locations pass the same host, NO-GO and per-copy rights checks (an SSRN copy never).
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

    def _record_unresolved(self, work: dict, res: oar.Resolution, topic: str,
                           walk: str) -> None:
        pids = work_pids(work)
        if not pids:
            return
        existing = self.ledger.rows.get(pids[0], {})
        attempts = int(existing.get("attempts") or 0) + 1
        rec = oar.work_record(work)
        self.ledger.upsert(
            pids[0], ids=pids, title=rec["title"], authors=rec["authors"], year=rec["year"],
            type=work.get("type"), topic=topic, family=walk, family_name=self.family.name,
            status="unresolved", reasons=sorted(set(res.reasons))[:12], attempts=attempts,
            last_tried=iso(self.now), next_retry_at=backoff_iso(self.now, attempts),
        )

    def consider(self, work: dict, topic: str, walk: str, implied: bool = False) -> None:
        """Gate, dedup and resolve one work from a page. Raises StopPage when the work needs a
        lookup that is unaffordable (cap) or failed: the page stays unfinished at this work."""
        st = self.stats
        title = (work.get("title") or work.get("display_name") or "").strip()
        if not self.family.relevant(work, self.openalex_relevant, implied):
            st.considered += 1
            st.relevance_rejected += 1
            st.example("relevance_drop", title)
            return
        if why := oar.work_excluded(work):
            st.considered += 1
            st.excluded += 1
            st.example("excluded", f"{why} | {title}")
            return
        if kind := self._known(work):
            st.considered += 1
            st.dup(kind)
            if kind == "identity_known_not_held":
                st.known_not_held += 1
                pids = work_pids(work)
                if pids:  # replacing it is an explicit, provenance-preserving transaction
                    self.ledger.upsert(pids[0], ids=pids, title=title, status="known_not_held",
                                       family_name=self.family.name, last_tried=iso(self.now),
                                       reasons=["registered row without held eligible content; "
                                                "replacement needs an explicit transaction"])
            return
        try:
            res, chosen, extra, note = self.resolve(work)
        except LookupBudget:
            raise StopPage("lookup cap")
        except UpstreamError as exc:
            st.reasons["lookup_failed"] = st.reasons.get("lookup_failed", 0) + 1
            raise StopPage(f"lookup failed: {exc}")
        st.considered += 1
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
                via = " ".join(note.split(" ", 2)[:2]) if note else "openalex location"
                st.resolved_via[via] = st.resolved_via.get(via, 0) + 1
            return
        st.unresolved += 1
        if any(r.startswith("rights_rejected") for r in res.reasons):
            st.rights_rejected += 1
            st.example("rights_drop", f"{'; '.join(sorted(set(res.reasons)))[:120]} | {title}")
        else:
            st.example("unresolved", f"{'; '.join(sorted(set(res.reasons)))[:120]} | {title}")
        self._record_unresolved(work, res, topic, walk)

    def _tally(self, res: oar.Resolution) -> None:
        for kind in res.reason_kinds():
            self.stats.reasons[kind] = self.stats.reasons.get(kind, 0) + 1

    # -- the page --------------------------------------------------------------------------------
    def search_page(self, pos: Position, walk: str) -> None:
        """Consume the page at `pos` from pos.k; update pos in place (see module docstring).
        Raises UpstreamError on a failed or malformed search (the caller keeps the cursor)."""
        key, expr, topic = self.family.queries[pos.q]
        filters = [f"title_and_abstract.search:{expr}", WINDOWS[pos.w], TYPES]
        if walk == "ssrn":
            filters.append(f"primary_location.source.id:{SSRN_SOURCE_ID}")
        else:
            filters.append("open_access.is_oa:true")
        params = {"filter": ",".join(filters), "per-page": self.per, "page": pos.p,
                  "sort": "cited_by_count:desc"}
        self.stats.searched = f"{walk} q={pos.q}:{key} w={WINDOWS[pos.w]} p={pos.p} k={pos.k}"
        results, count = self.api.search(params, self.per)
        self.stats.results = len(results)
        self._prefetch(results[pos.k:])
        for index in range(pos.k, len(results)):
            if len(self.out) >= self.max_docs:
                pos.k = index  # unfinished page: revisit it, skipping what was finished
                self.stats.stopped = "max"
                return
            try:
                self.consider(results[index], topic, walk, key in self.family.implied)
            except StopPage as stop:
                pos.k = index
                self.stats.stopped = str(stop)
                return
        if len(results) < self.per or pos.p * self.per >= min(count, MAX_PAGE_DEPTH):
            pos.advance_window(len(self.family.queries))
        else:
            pos.p, pos.k = pos.p + 1, 0

    def retry_due(self) -> None:
        """Re-check due works from the resolution record with the lookups that remain. A work
        found here is proposed but NOT marked resolved: that happens only once a later run sees
        it registered (a rolled-back round simply proposes it again)."""
        for row in self.ledger.due(iso(self.now), max(0, self.api.lookups_left),
                                   self.family.name):
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
                attempts = int(row.get("attempts") or 0) + 1
                row.update(status="unresolved", attempts=attempts, last_tried=iso(self.now),
                           next_retry_at=backoff_iso(self.now, attempts))
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
                # re-checked tomorrow: "resolved" only once the store holds it
                row.update(status="proposed", last_tried=iso(self.now),
                           next_retry_at=iso(self.now + 86400))
                continue
            if res.status == "excluded":
                row.update(status="excluded", reasons=res.reasons, last_tried=iso(self.now))
                continue
            attempts = int(row.get("attempts") or 0) + 1
            row.update(status="unresolved", attempts=attempts,
                       reasons=sorted(set(res.reasons))[:12], last_tried=iso(self.now),
                       next_retry_at=backoff_iso(self.now, attempts))

    def run(self, cursor: Cursor, slot: str) -> Cursor:
        """One scheduled step for `slot`. Returns the next cursor; raises UpstreamError (the
        committed cursor is kept)."""
        nxt = cursor.copy()
        self.stats.tick, self.stats.slot = cursor.t, slot
        if slot in self.family.walks:
            self.search_page(nxt.walks[slot], slot)
        # a slot owned by another walk spends no search here; retries use free lookups
        self.retry_due()
        nxt.t = cursor.t + 1
        return nxt


def report(stats: Stats, api: Api, family: Family, cursor_in: str, cursor_out: str | None,
           elapsed: float) -> None:
    s = dict(stats.__dict__)
    examples = s.pop("examples")
    print(f"# family {family.version}: tick {stats.tick} slot={stats.slot} {stats.searched}")
    print(f"# cursor {cursor_in} -> {cursor_out or '(held)'}")
    print(f"# requests: {api.searches} OpenAlex search, {api.lookups} supplementary lookups, "
          f"{api.seconds:.1f}s network, {elapsed:.1f}s total; {api.budget_note}")
    print("# stats: " + json.dumps(s, sort_keys=True, ensure_ascii=False))
    for kind, rows in examples.items():
        for row in rows:
            print(f"# example {kind}: {row}")


def main_family(args, *, policy: dict, keys, cooldowns: dict, save_cooldowns, get,
                openalex_relevant, append_entries, request_hold, report_next,
                now: float | None = None, sleep=time.sleep, run_id: str | None = None,
                clock=time.time, pacer=None) -> int:
    """find_sources.py --family NAME. Returns the process exit code. `now` is the run's
    snapshot timestamp (run records, retry schedules); `clock` is the live clock every API
    cooldown is computed from."""
    started = time.monotonic()
    now = clock() if now is None else now
    family = FAMILIES[getattr(args, "family", None) or "simulation"]
    cursor = parse_cursor(args.family_cursor, family)
    if args.lookup_max < MAX_LOOKUPS_PER_WORK:
        print(f"# ERROR: --lookup-max must be at least {MAX_LOOKUPS_PER_WORK} (one work's worst "
              "case), or a work could stall its walk", file=sys.stderr)
        return 2
    ledger_path = Path(args.resolution_file) if args.resolution_file else (
        default_ledger_path() if args.append else None)
    ledger = Ledger(ledger_path)
    api = Api(get, lookup_max=args.lookup_max, cooldowns=cooldowns,
              save_cooldowns=save_cooldowns, now=clock, sleep=sleep,
              pacer=pacer if pacer is not None else SharedPacer(clock=clock, sleep=sleep))
    run = FamilyRun(api=api, policy=policy, keys=keys, ledger=ledger, per=args.per,
                    max_docs=args.max, openalex_relevant=openalex_relevant, now=now,
                    family=family)
    slot = budget_slot(run_id, cursor.t)
    if slot in family.walks and cooldowns.get("openalex", 0) > clock():
        request_hold(f"OpenAlex cooldown active for "
                     f"{max(1, int(cooldowns['openalex'] - clock()))}s")
        report(run.stats, api, family, cursor.render(), None, time.monotonic() - started)
        return 0
    try:
        nxt = run.run(cursor, slot)
    except UpstreamError as exc:
        # An upstream failure (or a malformed answer) is never exhaustion: the committed cursor
        # stays and nothing is proposed.
        print(f"# ERROR: {exc}; cursor kept at {cursor.render()}", file=sys.stderr)
        report(run.stats, api, family, cursor.render(), None, time.monotonic() - started)
        return 1
    # ids are identity ids (dedup.identity_id), already checked against the store and this run
    report(run.stats, api, family, cursor.render(), nxt.render(), time.monotonic() - started)
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
