#!/usr/bin/env python3
"""verify_generation.py — generation-bound verification under PostgreSQL authority (ADR 0001
stage 4 step 5).

Under PostgreSQL authority the file store's production contracts (README counts equal to the
manifest, shard layout, ledger shard files, the SQLite index gate) describe nothing that exists.
They are replaced by checks bound to generations, in two tiers:

* per run, over what the run CHANGED (run_checks: the gates `contracts` and `lint` call it
  against the frozen state; work is bounded by the run's revisions, not by the corpus):
  - revision integrity and derived keys: every revision's stored digest is the sha256 of its
    canonical text, the key is the row's id (entries/manifest), the URL digest (blocklist) or
    "<sha256 of the row>:<n>" (ledger), and the derived lookup/order columns (url/title keys,
    sha256, legacy shard/topic) equal what store_pg.revision_keys computes from the row;
  - ledger: appended rows carry id/url/reason/pruned_at, their document is gone from the frozen
    registry and manifest, a row marked blocklisted has its URL in the frozen blocklist, and
    nothing is ever removed from the ledger or the blocklist;
  - eligibility: no changed manifest row a restriction matches claims corpus data;
  - artifact consistency: every payload claim a changed row makes that its parent row did not
    make identically names a version held locally (and registered for the run when the run is
    versioned);
  - generation-bound counters: the frozen state's counters (rows per table, manifest rows per
    status, training-eligible documents, excluded rows, text/corpus characters, topics, licences
    — corpus_stats' definitions) = the parent generation's recorded counters + the contribution
    of every changed row (new minus old). The contracts gate records them in its receipt, bound
    to the frozen state, so generation G's counters are its run's contracts receipt. Without a
    parent (the first generation), without recorded parent counters, or when the run's
    configuration differs from its parent's (policy changes eligibility everywhere), the counters
    are recounted in full from the frozen state (server-side aggregates) and the full eligibility
    check runs.
* full sweeps (scripts/integrity_sweep.py), resumable and in the background: every row of a
  pinned generation (the lint checks, derived keys of projection and overlay rows, eligibility,
  every claim resolvable), counters recounted and compared with the generation's recorded ones,
  and every local artifact version re-hashed.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import registry
import store
from store import canonical_row

COUNTER_VERSION = 1
PAGE = 2000
MAX_ERRORS = 200
TABLES = tuple(t.value for t in (store.Table.ENTRIES, store.Table.MANIFEST, store.Table.BLOCKLIST,
                                  store.Table.LEDGER))
LEDGER_REQUIRED = ("id", "url", "reason", "pruned_at")
REPORT_ENV = "NEKAISE_GATE_REPORT"


class VerifyError(store.StoreError):
    """The view cannot be verified (not a staged PostgreSQL view, missing provenance)."""


# --- counters ---------------------------------------------------------------------------------------
#
# Every counter is an exact integer, so a chain of per-run deltas equals a recount exactly: the
# character sums add each row's value only when it is an integral JSON number (ints, and floats
# like 12.0); a non-integral number is counted in `fractional` instead (real rows never have
# one; corpus_stats would truncate the sum, which no delta arithmetic can reproduce). The
# document, exclusion, topic and licence counts are corpus_stats.compute's definitions.

def new_counters() -> dict:
    return {"v": COUNTER_VERSION, "rows": {t: 0 for t in TABLES}, "status": {},
            "documents": 0, "excluded": 0, "text_chars": 0, "corpus_chars": 0,
            "fractional": {"text_chars": 0, "corpus_chars": 0}, "topics": {}, "licenses": {}}


def _chars(value) -> int | None:
    """An integral JSON number as an int; None for a non-integral number; 0 for anything else
    (strings, null, booleans, a missing field: corpus_stats' sums count numbers only)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value)


def _label(value) -> str:
    """A group value as a counter key, injective: a string as itself (prefixed when it could be
    mistaken for a label of another type), null and booleans as "json:<JSON>". Other values
    cannot be grouped (store.check_group_value refuses them in the full recount too), so the
    gate fails on them in both modes."""
    if isinstance(value, str):
        return f"str:{value}" if value.startswith(("json:", "str:")) else value
    if value is None or isinstance(value, bool):
        return f"json:{json.dumps(value)}"
    raise VerifyError(f"a status/topic/license must be a string, boolean or null, not {value!r}")


def _bump(d: dict, key: str, n) -> None:
    d[key] = d.get(key, 0) + n


def add_manifest_row(c: dict, row: dict, eligible_pred, sign: int) -> None:
    """Add (sign=1) or remove (sign=-1) one manifest row's contribution: documents = ok AND
    in the default view (store.default_corpus_where, the predicate corpus_stats uses); corpus_chars
    falls back to text_chars where the row has no corpus_chars field."""
    c["rows"]["manifest"] += sign
    _bump(c["status"], _label(row.get("status")), sign)
    if row.get("status") != "ok":
        return
    if not store.evaluate(eligible_pred, row):
        c["excluded"] += sign
        return
    c["documents"] += sign
    for field_, value in (("text_chars", row.get("text_chars")),
                          ("corpus_chars", row["corpus_chars"] if "corpus_chars" in row
                           else row.get("text_chars"))):
        n = _chars(value)
        if n is None:
            c["fractional"][field_] += sign
        else:
            c[field_] += sign * n
    _bump(c["topics"], _label(row.get("topic")), sign)
    _bump(c["licenses"], _label(row.get("license")), sign)


def finalize(c: dict) -> dict:
    """Drop zero groups (canonical, comparable)."""
    out = dict(c)
    for k in ("status", "topics", "licenses"):
        out[k] = {key: n for key, n in sorted(c[k].items()) if n}
    out["fractional"] = dict(c["fractional"])
    return out


# ONE pass over the manifest: every counter of corpus_stats.compute's definitions plus the exact
# character sums, grouped by (status, topic, license, ok, eligible) — a handful of groups
_FULL_SQL = """
SELECT s, t, l, ok, elig, count(*),
       COALESCE(sum(CASE WHEN jsonb_typeof(tc) = 'number' AND mod((tc #>> '{{}}')::numeric, 1) = 0
                         THEN (tc #>> '{{}}')::numeric END), 0),
       count(*) FILTER (WHERE jsonb_typeof(tc) = 'number' AND mod((tc #>> '{{}}')::numeric, 1) <> 0),
       COALESCE(sum(CASE WHEN jsonb_typeof(cc) = 'number' AND mod((cc #>> '{{}}')::numeric, 1) = 0
                         THEN (cc #>> '{{}}')::numeric END), 0),
       count(*) FILTER (WHERE jsonb_typeof(cc) = 'number' AND mod((cc #>> '{{}}')::numeric, 1) <> 0)
FROM (SELECT COALESCE(row -> 'status', 'null'::jsonb) AS s,
             COALESCE(row -> 'topic', 'null'::jsonb) AS t,
             COALESCE(row -> 'license', 'null'::jsonb) AS l,
             {ok} AS ok, {elig} AS elig,
             row -> 'text_chars' AS tc,
             CASE WHEN row ? 'corpus_chars' THEN row -> 'corpus_chars' ELSE row -> 'text_chars'
             END AS cc
      FROM {src}) x
GROUP BY s, t, l, ok, elig
"""


def full_counters(view, restrictions: dict) -> dict:
    """Counters of the whole view recounted in the server — for the first generation, a policy
    change, or a sweep — in ONE pass over the manifest (at 160M rows each pass is a scan of the
    whole table) plus a row count of the other tables. The same definitions as the per-row
    arithmetic above (tests pin both against corpus_stats.compute)."""
    import store_pg
    from psycopg import sql
    c = new_counters()
    for table in TABLES:
        if table != "manifest":
            c["rows"][table] = int(view._q(
                _sql("SELECT count(*) FROM {}", view._src(table))).fetchone()[0])
    ok, ok_params = store_pg.compile_predicate(store.Eq("status", "ok"), sql.SQL("row"))
    elig, elig_params = store_pg.compile_predicate(store.default_corpus_where(restrictions),
                                                   sql.SQL("row"))
    for s, t, lic, is_ok, is_elig, n, text, text_frac, corpus, corpus_frac in view._q(
            sql.SQL(_FULL_SQL).format(ok=ok, elig=elig, src=view._src("manifest")),
            ok_params + elig_params).fetchall():
        n = int(n)
        c["rows"]["manifest"] += n
        _bump(c["status"], _label(s), n)
        if not is_ok:
            continue
        if not is_elig:
            c["excluded"] += n
            continue
        c["documents"] += n
        c["text_chars"] += int(text)
        c["corpus_chars"] += int(corpus)
        c["fractional"]["text_chars"] += int(text_frac)
        c["fractional"]["corpus_chars"] += int(corpus_frac)
        _bump(c["topics"], _label(t), n)
        _bump(c["licenses"], _label(lic), n)
    return finalize(c)


def _sql(template: str, *parts):
    from psycopg import sql
    return sql.SQL(template).format(*parts)


# --- scope of a staged view ------------------------------------------------------------------------------

@dataclass
class Scope:
    """What a frozen (or live) staged view is: its run and staging sequence, the parent
    generation, the run's artifact policy, and whether its configuration equals the parent's."""
    run_id: str
    seq: int
    parent: int | None
    artifact_policy: str
    config_changed: bool
    parent_vis: object = None     # store_staging.Visibility of the parent generation (None: table)


def scope_of(view) -> Scope:
    import store_staging
    if getattr(view, "stage", None) is None:
        raise VerifyError("not a staged view: run checks verify one run's frozen state "
                          "(the gates read it through NEKAISE_STORE_STAGE)")
    run_id, seq = view.stage
    row = view._q("SELECT parent_generation, artifact_policy, config_digest FROM runs WHERE "
                  "run_id = %s", [run_id]).fetchone()
    if row is None:
        raise VerifyError(f"run {run_id} is unknown")
    parent, policy, digest = row
    changed = True
    if parent is not None:
        pdigest = view._q("SELECT config_digest FROM generations WHERE generation = %s",
                          [parent]).fetchone()
        changed = pdigest is None or pdigest[0] != digest
    vis = view._visibility
    pvis = store_staging.Visibility(vis.lo, vis.hi, vis.runs)
    return Scope(run_id, seq, parent, policy, changed, None if pvis.empty else pvis)


def _parent_src(view, scope: Scope, table: str):
    from psycopg import sql
    return sql.Identifier(table) if scope.parent_vis is None else scope.parent_vis.source(table)


def changed_keys(view, scope: Scope, tbl: str, *, page: int = PAGE) -> Iterator[list[str]]:
    """The run's changed keys of revision table `tbl` up to its sequence, in key order, a page
    at a time (the unique (run, tbl, key, batch_seq) index serves it)."""
    after = ""
    while True:
        keys = [k for (k,) in view._q(
            "SELECT DISTINCT key FROM revisions WHERE run_id = %s AND tbl = %s AND batch_seq <= %s "
            "AND key > %s ORDER BY key LIMIT %s",
            [scope.run_id, tbl, scope.seq, after, page]).fetchall()]
        if not keys:
            return
        yield keys
        after = keys[-1]


def recorded_counters(view, generation: int | None) -> dict | None:
    """Generation `generation`'s recorded counters: the report of its run's passed contracts
    receipt at exactly the generation's frozen state, or None."""
    if generation is None:
        return None
    row = view._q(
        "SELECT gr.detail_text FROM generations g JOIN gate_receipts gr ON gr.run_id = g.run_id "
        "AND gr.gate = 'contracts' AND gr.verdict = 'passed' AND gr.frozen_seq = g.frozen_seq "
        "AND gr.frozen_digest = g.frozen_digest WHERE g.generation = %s", [generation]).fetchone()
    if row is None:
        return None
    counters = (json.loads(row[0]).get("report") or {}).get("counters")
    if not isinstance(counters, dict) or counters.get("v") != COUNTER_VERSION:
        return None
    return counters


# --- per-run checks ------------------------------------------------------------------------------------

@dataclass
class Report:
    mode: str = "delta"
    counters: dict | None = None
    run: str | None = None        # the frozen state the report describes
    seq: int | None = None
    changed: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    full_checks: bool = False

    def error(self, text: str) -> None:
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(text)

    def as_report(self) -> dict:
        return {"run": self.run, "seq": self.seq, "counters": self.counters,
                "counter_mode": self.mode, "changed": self.changed,
                "full_checks": self.full_checks, "errors": len(self.errors)}


def _revision_errors(view, scope: Scope, tbl: str, keys: list[str], rep: Report) -> None:
    """Stored digests, canonical text, identity of the key and the derived columns of the run's
    revisions of `keys`."""
    import store_pg
    rows = view._q(
        "SELECT key, op, row_text, row_sha256, reason, url_norm, url_key, title_norm, title_key, "
        "sha256, shard, topic_key, pids FROM revisions WHERE run_id = %s AND tbl = %s AND batch_seq <= "
        "%s AND key = ANY(%s)", [scope.run_id, tbl, scope.seq, keys]).fetchall()
    for key, op, text, digest, reason, *derived in rows:
        where = f"{tbl} {key}"
        if op == "tombstone":
            if tbl in ("ledger", "blocklist"):
                rep.error(f"{where}: the {tbl} is append-only, but the run removes a row")
            if text is not None or digest is not None:
                rep.error(f"{where}: a tombstone carries row text")
            continue
        if text is None or digest != hashlib.sha256(text.encode()).hexdigest():
            rep.error(f"{where}: stored digest does not match its row text")
            continue
        try:
            row = json.loads(text)
        except ValueError:
            rep.error(f"{where}: row text is not JSON")
            continue
        if canonical_row(row) != text:
            rep.error(f"{where}: row text is not canonical")
        if tbl in ("entries", "manifest"):
            if row.get("id") != key:
                rep.error(f"{where}: key is not the row's id ({row.get('id')!r})")
            got = [bytes(v) if isinstance(v, memoryview) else v for v in derived]
            want = [*store_pg.revision_keys(tbl, row), store_pg.pids_for(row)]
            if got != want:
                rep.error(f"{where}: derived columns differ from the row "
                          f"(stored {got[:1] + got[4:]} != {want[:1] + want[4:]})")
        elif tbl == "blocklist":
            if key != store.key_digest(row.get("url") or ""):
                rep.error(f"{where}: key is not the digest of the URL")
        elif tbl == "ledger":
            digest_part, _, n = key.partition(":")
            if digest_part != store.key_digest(text) or not (n.isdigit() and len(n) == 10):
                rep.error(f"{where}: key is not '<sha256 of the row>:<n>'")
        elif any(v is not None for v in derived):
            rep.error(f"{where}: derived columns on a {tbl} revision")


def _rows(view, src, keys: list[str]) -> dict[str, dict]:
    return {i: json.loads(t) for i, t in view._q(
        _sql("SELECT id, row_text FROM {} WHERE id = ANY(%s)", src), [keys]).fetchall()}


def run_checks(view, restrictions: dict, *, root: Path, full: bool = False,
               page: int = PAGE) -> Report:
    """The per-run checks and counters of a staged view (module docstring)."""
    import artifact_store
    scope = scope_of(view)
    rep = Report(run=scope.run_id, seq=scope.seq)
    eligible = store.default_corpus_where(restrictions)
    parent_counters = recorded_counters(view, scope.parent)
    rep.full_checks = full or scope.config_changed or scope.parent is None
    delta = new_counters()
    local = artifact_store.LocalArtifacts(Path(root))
    for tbl in TABLES:
        n = 0
        for keys in changed_keys(view, scope, tbl, page=page):
            n += len(keys)
            _revision_errors(view, scope, tbl, keys, rep)
            if tbl in ("entries", "manifest"):
                now = _rows(view, view._src(tbl), keys)
                before = _rows(view, _parent_src(view, scope, tbl), keys)
                if tbl == "entries":   # manifest rows are counted by add_manifest_row
                    delta["rows"][tbl] += (sum(k in now for k in keys)
                                           - sum(k in before for k in keys))
                else:
                    for k in keys:
                        if k in before:
                            add_manifest_row(delta, before[k], eligible, -1)
                        if k in now:
                            add_manifest_row(delta, now[k], eligible, 1)
                            _claim_checks(view, scope, k, now[k], before.get(k), local, rep)
                            # collect-all: every class keeps its cleaned copy, but only in its
                            # OWN view (a restricted-use or policy-held row never in corpus/)
                            claim = now[k].get("corpus_path")
                            if claim and not claim.startswith(registry.view_root(
                                    registry.view_of(now[k], restrictions)) + "/"):
                                rep.error(f"manifest {k}: corpus claim {claim!r} is outside its "
                                          "use view")
            elif tbl == "blocklist":
                src_now, src_before = view._src("blocklist"), _parent_src(view, scope, "blocklist")
                now = {k for (k,) in view._q(_sql("SELECT key FROM {} WHERE key = ANY(%s)",
                                                  src_now), [keys]).fetchall()}
                before = {k for (k,) in view._q(_sql("SELECT key FROM {} WHERE key = ANY(%s)",
                                                     src_before), [keys]).fetchall()}
                delta["rows"]["blocklist"] += len(now) - len(before)
            else:
                _ledger_checks(view, scope, keys, delta, rep)
        rep.changed[tbl] = n
    if rep.full_checks or parent_counters is None:
        rep.mode = "full"
        rep.counters = full_counters(view, restrictions)
        if rep.full_checks:
            import corpus_stats
            count, first = corpus_stats.misplaced_view_claims(view, restrictions)
            if count:
                rep.error(f"{count:,} manifest rows claim corpus data outside their use view "
                          f"(first: {first})")
    else:
        rep.counters = combine(parent_counters, delta)
        if bad := negative(rep.counters):
            rep.error(f"counters went negative ({bad}): the parent's recorded counters do not "
                      "describe the state this run was staged on")
    return rep


def _corpus_fields() -> tuple:
    import registry
    return registry.CORPUS_FIELDS


def combine(parent: dict, delta: dict) -> dict:
    out = new_counters()
    for part in (parent, delta):
        for t in TABLES:
            out["rows"][t] += part["rows"].get(t, 0)
        for k in ("documents", "excluded", "text_chars", "corpus_chars"):
            out[k] += part[k]
        for k in ("text_chars", "corpus_chars"):
            out["fractional"][k] += part["fractional"][k]
        for k in ("status", "topics", "licenses"):
            for key, n in part[k].items():
                _bump(out[k], key, n)
    return finalize(out)


def negative(c: dict) -> list[str]:
    bad = [f"rows.{t}" for t, n in c["rows"].items() if n < 0]
    bad += [k for k in ("documents", "excluded", "text_chars", "corpus_chars") if c[k] < 0]
    bad += [f"fractional.{k}" for k, n in c["fractional"].items() if n < 0]
    bad += [f"{k}.{key}" for k in ("status", "topics", "licenses")
            for key, n in c[k].items() if n < 0]
    return bad


def _claim_checks(view, scope: Scope, key: str, row: dict, parent: dict | None, local,
                  rep: Report) -> None:
    """A payload claim the row makes that its parent row did not make identically must name a
    version held here (and, in a versioned run, registered for the run)."""
    import artifact_store
    for stage, path, sha in artifact_store.changed_claims(row, parent):
        if not (isinstance(sha, str) and artifact_store.is_identity(sha)):
            if scope.artifact_policy == "versioned":
                rep.error(f"manifest {key}: its new {stage} claim has no identity")
            continue
        if scope.artifact_policy != "versioned":
            continue
        if local.size(stage, sha) is None:
            rep.error(f"manifest {key}: its {stage} version {sha[:12]} is not held locally")
        elif view._q("SELECT 1 FROM run_artifacts WHERE run_id = %s AND stage = %s AND "
                     "sha256 = %s", [scope.run_id, stage, sha]).fetchone() is None:
            rep.error(f"manifest {key}: its {stage} version {sha[:12]} is not registered for "
                      "the run")


def _ledger_checks(view, scope: Scope, keys: list[str], delta: dict, rep: Report) -> None:
    import blocklist
    digests = sorted({k.partition(":")[0] for k in keys})
    now = {f"{k}:{n:010d}": t for k, n, t in view._q(
        _sql("SELECT key, n, row_text FROM {} WHERE key = ANY(%s)", view._src("ledger")),
        [digests]).fetchall()}
    before = {f"{k}:{n:010d}" for k, n in view._q(
        _sql("SELECT key, n FROM {} WHERE key = ANY(%s)", _parent_src(view, scope, "ledger")),
        [digests]).fetchall()}
    added = [k for k in keys if k in now and k not in before]
    delta["rows"]["ledger"] += sum(k in now for k in keys) - sum(k in before for k in keys)
    rows = {k: json.loads(now[k]) for k in added}
    ids = sorted({r.get("id") for r in rows.values() if isinstance(r.get("id"), str)})
    present = set(view.get_manifest(ids)) | set(view.get_entries(ids)) if ids else set()
    urls = sorted({blocklist.normalize(r.get("url")) for r in rows.values()
                   if r.get("blocklisted") is True and r.get("url")})
    listed = {u for (u,) in view._q(_sql("SELECT url FROM {} WHERE key = ANY(%s)",
                                         view._src("blocklist")),
                                    [[store.key_digest(u) for u in urls]]).fetchall()} \
        if urls else set()
    for k, r in rows.items():
        if missing := [f for f in LEDGER_REQUIRED if not r.get(f)]:
            rep.error(f"ledger {k}: missing {', '.join(missing)}")
            continue
        if r["id"] in present:
            rep.error(f"ledger {k}: {r['id']} is recorded as pruned but is still in the "
                      "registry or manifest")
        if r.get("blocklisted") is True and blocklist.normalize(r["url"]) not in listed:
            rep.error(f"ledger {k}: {r['id']} is recorded as blocklisted but its URL is not")


# --- the gate report ----------------------------------------------------------------------------------

def write_report(report: dict, env=None) -> None:
    """Hand the gate's report to its coordinator (run_round/staged_runs record it in the gate
    receipt): the path is REPORT_ENV, set per gate by the coordinator; nothing when unset."""
    import os
    import ops
    path = (os.environ if env is None else env).get(REPORT_ENV)
    if path:
        ops.atomic_write_text(Path(path), json.dumps(report, sort_keys=True) + "\n")


# Gates whose passing verdict must carry a report (the contracts gate's counters): a pass without
# one is recorded as a failure — the generation would otherwise be promoted without counters.
REPORTING_GATES = ("contracts",)
MAX_REPORT_BYTES = 256 * 1024


def read_gate_report(gate: str, path: Path | None, passed: bool, *,
                     expect: dict | None = None) -> tuple[bool, dict | None, str | None]:
    """(passed, report, why it was failed) for a gate's handed-back report at `path` (None: the
    gate was given no report path). `expect` ({"run": run id, "seq": frozen sequence}) binds a
    reporting gate's report to the frozen state being recorded: a report about another run or
    sequence fails the gate."""
    try:
        data = None if path is None else Path(path).read_bytes()
    except FileNotFoundError:
        data = None
    if data is None:
        if passed and gate in REPORTING_GATES:
            return False, None, f"gate {gate} passed without handing back its report"
        return passed, None, None
    if len(data) > MAX_REPORT_BYTES:
        return False, None, f"gate {gate}'s report exceeds {MAX_REPORT_BYTES} bytes"
    try:
        report = json.loads(data)
    except ValueError:
        return False, None, f"gate {gate}'s report is not JSON"
    if not isinstance(report, dict):
        return False, None, f"gate {gate}'s report is not an object"
    if gate in REPORTING_GATES and expect is not None and \
            (report.get("run"), report.get("seq")) != (expect.get("run"), expect.get("seq")):
        return False, None, (f"gate {gate}'s report describes run {report.get('run')} at "
                             f"{report.get('seq')}, not the frozen {expect.get('run')} at "
                             f"{expect.get('seq')}")
    return passed, report, None
