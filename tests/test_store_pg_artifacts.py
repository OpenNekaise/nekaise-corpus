"""Stage 4 step 3 (ADR 0001): local artifacts compatible with atomic metadata — immutable artifact
versions, schema v6's claim contract, resolution through generation membership, the versioned
loader/pruner/cleaner of a staged run, and corpus/ as a generation-stamped materialization.
Opt-in: NEKAISE_PG_TEST_DSN.

The acceptance gate: crash injection around writes, pruning, cleaning, promotion and
materialization — the committed generation G stays readable (every claimed payload resolves to
bytes with its identity) — and the multilingual / numeric-table / equation outputs of the staged
path are byte-identical to the legacy file-authoritative pipeline's."""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

import artifact_store
import build_corpus
import clean_corpus
import materialize
import pipeline_repo
import prune_corpus
import quality
import store
import store_broker
import store_staging
from pipeline_repo import entry_of, manifest_rows, write_repo
from runids import rid
from test_pipeline_store import HEADER, TEXT, _seed_postgres, clock, lentry  # noqa: F401 (fixture)

pytestmark = pytest.mark.skipif(not os.environ.get("NEKAISE_PG_TEST_DSN"),
                                reason="NEKAISE_PG_TEST_DSN not set")
DSN = os.environ.get("NEKAISE_PG_TEST_DSN", "")
REPO = Path(__file__).resolve().parents[1]
V5_COMMIT = "e03860403a"  # last commit whose store_pg.py writes schema version 5 (stage 4 step 2)
SHA = "0" * 40
RULES = "toc_leaders,patent_id_soup,patent_furniture,site_chrome,ocr_debris,code_annotations"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def q(st, statement, params=()):
    with st._connect(autocommit=True) as conn:
        return conn.execute(statement, params).fetchall()


def sqlerr():
    import psycopg
    return psycopg.Error


# --- the documents: golden multilingual prose, numeric tables, equations, and cleaning targets ----

GOLDEN = {
    "cjk": ("測定は 2019 年 3 月 14（暖房期）と 2021 年 10 月 22（冷房期）に行った。\n"
            "本研究では，幅 3.0m× 高さ 2.4m の試験体を用いて熱負荷を測定した。\n"
            "室温は 22.5 ℃，相対湿度は 45 % に保持された。\n— 186 —\n"),
    "table": ("Asphalt workers 2.81 (1.11-7.13) \nBricklayers 2.14 (1.08-4.25) \n"
              "Concrete C25/30 25 30 2400 31 \n1,023 611 789 914 1,059 1,104 1,446\n"
              "(−1.81) (−2.10) (2.50) (2.25) (3.00) (3.04)\n"),
    "modelica": ("  Q_flow = m_flow * cp * (T_in - T_out);\nequation\n"
                 "  der(T) = (Q_flow - UA * (T - T_amb)) / (V * rho * cp);\n"
                 "          extent={{-50,48},{50,-42}},\n          fillColor={255,255,255},\n"),
    "german": ("1 Kurzfassung ................................ ...................... 9 \n"
               "Die Wärmepumpe versorgt das Gebäude; Lüftung und Dämmung wurden gemessen.\n"
               "US20150157190A1\nUS 20150157190 A1\nCN208382619U\n"),
    "crlf": "Windows line endings\r\nsurvive the text mode read\r\nexactly like before.\r\n",
}


def body_for(sid: str) -> bytes:
    kind = sid.rsplit("-", 1)[-1]
    if sid.endswith("thin"):
        return b"tiny"
    extra = GOLDEN.get(kind, "")
    return (f"{sid}: {TEXT}\n{extra}").encode()


def gentry(sid, **kw):
    return lentry(sid, **kw)


def url_of(sid: str) -> str:
    return gentry(sid)["url"]


IDS = [f"ost-g-{k}" for k in GOLDEN] + ["ost-g-thin", "ost-g-404"]
OLD_BODY = ("old bytes of ost-g-have, fetched long ago. " + TEXT).encode()


def build_repo(root: Path) -> None:
    """A repository as the file-authoritative pipeline left it: one document already held (its
    raw/text/corpus files are legacy committed paths), one whose row failed transiently but whose
    raw file an earlier attempt left in place, and new entries to fetch."""
    have_text = HEADER.encode() + OLD_BODY
    old_corpus = HEADER.encode() + b"cleaned under an older ruleset"
    have = {**entry_of(gentry("ost-g-have")), "status": "ok", "http_status": 200,
            "sha256": sha(OLD_BODY), "bytes": len(OLD_BODY), "raw_path": "raw/osti/ost-g-have.md",
            "text_path": "text/ost-g-have.md", "text_chars": len(OLD_BODY),
            "text_sha256": sha(have_text), "corpus_path": "corpus/ost-g-have.md",
            "corpus_chars": 30, "corpus_sha256": sha(old_corpus),
            "cleaner_version": "clean_corpus/2;rules=none", "error": None,
            "fetched_at": "2026-09-01T00:00:00Z", "extractor_version": "old",
            "quality": quality.metrics(OLD_BODY.decode())}
    retry = {**entry_of(gentry("ost-g-retry")), "status": "failed", "http_status": 503,
             "error": "503", "transient": True, "retry_attempts": 1,
             "first_failed_at": "2026-09-24T00:00:00Z"}
    entries = sorted([gentry(s) for s in IDS] + [gentry("ost-g-have"), gentry("ost-g-retry")],
                     key=lambda e: e["id"])
    write_repo(root, entries=entries, manifest=[have, retry], policy={})
    for rel, data in (("raw/osti/ost-g-have.md", OLD_BODY), ("text/ost-g-have.md", have_text),
                      ("corpus/ost-g-have.md", old_corpus),
                      ("raw/osti/ost-g-retry.md", b"a partial leftover of an earlier attempt")):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)


def serve_golden(monkeypatch):
    from test_pipeline_store import Resp

    def get(url, **_kw):
        sid = url.rsplit("/", 1)[1].rsplit(".", 1)[0]
        if sid.endswith("404"):
            return Resp(404, b"missing")
        return Resp(200, body_for(sid))
    monkeypatch.setattr(build_corpus.requests, "get", get)
    monkeypatch.setattr(build_corpus, "HOST_DELAY", {})
    monkeypatch.setattr(build_corpus, "_tripped_hosts", {})


def run_step(monkeypatch, root, module, *args):
    pipeline_repo.point(monkeypatch, root, policy=None)
    serve_golden(monkeypatch)
    mod = {"fetch": build_corpus, "prune": prune_corpus, "clean": clean_corpus}[module]
    argv = {"fetch": ["build_corpus.py", "--workers", "1"],
            "prune": ["prune_corpus.py", "--apply"],
            "clean": ["clean_corpus.py", "--workers", "1"]}[module]
    monkeypatch.setattr(sys, "argv", [*argv, *args])
    mod.main()


def files(root: Path, dirs=("raw", "text", "corpus", "artifacts")) -> dict[str, bytes]:
    root = Path(root)
    return {str(p.relative_to(root)): p.read_bytes()
            for d in dirs if (root / d).exists()
            for p in sorted((root / d).rglob("*")) if p.is_file() and ".incoming" not in p.parts}


# --- stores and staged rounds -------------------------------------------------------------------------

def new_pg(root: Path):
    import store_pg
    st = store_pg.PgStore(root, dsn=DSN, schema=f"ar_{uuid.uuid4().hex[:12]}")
    st.pin_config_from_files()
    return st


@pytest.fixture
def repo(tmp_path):
    """(root, PostgreSQL store seeded with root's legacy state as the pre-generation base)."""
    root = tmp_path / "b"
    build_repo(root)
    pg = new_pg(root)
    _seed_postgres(pg, root)
    yield root, pg
    pg.drop()


@contextmanager
def staged(pg, name, *, ruleset=RULES):
    run_id = rid(name)
    with pg.writer(round_id=run_id) as w:
        with store_broker.staged_round(pg, w, run_id, producer_commit=SHA,
                                       extractor_version="x", cleaning_ruleset=ruleset) as rnd:
            yield w, rnd


def child_env(m, pg, rnd):
    """This process acts as the round's mutating child (the broker serves in a thread)."""
    for k, v in {**rnd.broker.env(), "NEKAISE_STORE": "postgres", "NEKAISE_PG_DSN": DSN,
                 "NEKAISE_PG_SCHEMA": pg.schema, "NEKAISE_RUN_ID": rnd.run.run_id}.items():
        m.setenv(k, v)


def abandon(pg, rnd) -> None:
    """A crashed round's coordinator gives up: drain its broker and abort the run (unpromoted
    runs default to abort)."""
    rnd.broker.drain()
    pg.abort_run(rnd.writer, rnd.run.run_id, reason="test: a step crashed")


def handoff(root: Path, rnd) -> None:
    """The loader's deferral handoff for a round whose prune runs without a fetch."""
    (Path(root) / "workspace").mkdir(exist_ok=True)
    (Path(root) / "workspace" / "fetch-deferred.json").write_text(
        json.dumps({"run_id": rnd.run.run_id, "ids": []}) + "\n")


def steps(monkeypatch, pg, rnd, root, *names, clean_args=()):
    if "prune" in names and "fetch" not in names:
        handoff(root, rnd)
    for name in names:
        with monkeypatch.context() as m:
            child_env(m, pg, rnd)
            run_step(m, root, name, *(clean_args if name == "clean" else ()))


def promote(rnd) -> int:
    rnd.freeze(["artifacts"])
    result = rnd.verify_artifacts()
    assert not result["failed"], result
    return rnd.promote()


def full_round(monkeypatch, pg, root, name, **kw) -> int:
    with staged(pg, name, **kw) as (_w, rnd):
        steps(monkeypatch, pg, rnd, root, "fetch", "prune", "clean")
        return promote(rnd)


def rows_of(view) -> dict[str, dict]:
    out, cursor = {}, None
    while True:
        page = view.scan(store.Table.MANIFEST, cursor=cursor, limit=store.MAX_PAGE)
        out.update({r["id"]: r for r in page.rows})
        if page.next_cursor is None:
            return out
        cursor = page.next_cursor


def assert_readable(pg, root: Path, generation: int | None = None) -> int:
    """Every payload the generation's rows claim with an identity resolves (through that
    generation's membership) to local bytes with exactly that identity. Returns how many."""
    opened = pg.read() if generation is None else pg.read_generation(generation)
    n = 0
    with opened as v:
        rows = rows_of(v)
        for stage in artifact_store.STAGES:
            refs = v.resolve_artifacts(list(rows), stage)
            for sid, ref in refs.items():
                if not isinstance(ref.sha256, str):
                    continue
                path = Path(root) / ref.locator.removeprefix("file:")
                assert sha(path.read_bytes()) == ref.sha256, (sid, stage, ref)
                n += 1
    return n


def snapshot_tree(root: Path) -> dict[str, str]:
    return {k: sha(v) for k, v in files(root).items()}


# --- the staged path produces the legacy pipeline's bytes ---------------------------------------------

def test_a_staged_round_is_byte_identical_to_the_legacy_pipeline(tmp_path, monkeypatch, clock,
                                                                  capsys):
    run_id = rid("eq")
    monkeypatch.setenv("NEKAISE_RUN_ID", run_id)
    legacy = tmp_path / "legacy"
    build_repo(legacy)
    staged_root = tmp_path / "staged"
    shutil.copytree(legacy, staged_root)
    # the file-authoritative reference: load, prune, clean with the production ruleset
    for name in ("fetch", "prune"):
        with monkeypatch.context() as m:
            run_step(m, legacy, name)
    with monkeypatch.context() as m:
        run_step(m, legacy, "clean", "--rules", RULES)
    before = files(staged_root)
    pg = new_pg(staged_root)
    try:
        _seed_postgres(pg, staged_root)
        with pg.writer(round_id=run_id) as w:
            with store_broker.staged_round(pg, w, run_id, producer_commit=SHA,
                                           extractor_version="x", cleaning_ruleset=RULES) as rnd:
                steps(monkeypatch, pg, rnd, staged_root, "fetch", "prune", "clean")
                # nothing under raw/, text/ or corpus/ changed: new bytes are versions only
                assert {k: v for k, v in files(staged_root).items()
                        if not k.startswith("artifacts/")} == before
                generation = promote(rnd)
        assert generation == 0
        stats = materialize.refresh(pg, staged_root)
        assert stats["mode"] == "full" and not stats.get("missing")
        want = manifest_rows(legacy)
        with pg.read() as v:
            got = rows_of(v)
        assert set(got) == set(want)
        for sid, row in want.items():
            mine = dict(got[sid])
            if "corpus_source_sha256" in mine:          # the one new, versioned-only field
                assert mine.pop("corpus_source_sha256") == row["text_sha256"]
            assert mine == row, sid
        # raw and text bytes: the legacy files, resolved from the immutable versions
        with pg.read() as v:
            for stage, field in (("raw", "raw_path"), ("text", "text_path")):
                for sid, ref in v.resolve_artifacts(list(got), stage).items():
                    data = (staged_root / ref.locator.removeprefix("file:")).read_bytes()
                    assert data == (legacy / got[sid][field]).read_bytes(), (sid, stage)
        # the cleaned corpus: the legacy cleaner's bytes, file for file (CJK, tables, equations)
        mat = {k: v for k, v in files(staged_root, ("corpus",)).items() if not
               Path(k).name.startswith(".")}
        ref = {k: v for k, v in files(legacy, ("corpus",)).items()
               if not Path(k).name.startswith(".")}
        assert mat == ref
        assert set(mat) == {f"corpus/{s}.md" for s in ("ost-g-cjk", "ost-g-table", "ost-g-modelica",
                                                          "ost-g-german", "ost-g-crlf",
                                                          "ost-g-have", "ost-g-retry")}
        cjk = mat["corpus/ost-g-cjk.md"].decode()
        assert GOLDEN["cjk"].splitlines()[0] in cjk and "— 186 —" in cjk  # page_markers is off
        assert "Concrete C25/30 25 30 2400 31" in mat["corpus/ost-g-table.md"].decode()
        assert "der(T) = (Q_flow" in mat["corpus/ost-g-modelica.md"].decode()
        assert "fillColor" not in mat["corpus/ost-g-modelica.md"].decode()
        assert "US20150157190A1" not in mat["corpus/ost-g-german.md"].decode()
        # the legacy corpus file of the held document was preserved before it was replaced, and
        # the retry's leftover raw file was never overwritten
        assert artifact_store.LocalArtifacts(staged_root).has(
            "corpus", sha(HEADER.encode() + b"cleaned under an older ruleset"))
        assert (staged_root / "raw/osti/ost-g-retry.md").read_bytes() == \
            b"a partial leftover of an earlier attempt"
        assert (legacy / "raw/osti/ost-g-retry.md").read_bytes() == body_for("ost-g-retry")
        # every materialized file is a link to its immutable version
        with pg.read() as v:
            for sid, ref in v.resolve_artifacts([p[7:-3] for p in mat], "corpus").items():
                assert os.path.samefile(staged_root / "corpus" / f"{sid}.md",
                                        staged_root / ref.locator.removeprefix("file:"))
        assert assert_readable(pg, staged_root) >= 3 * 7
        with materialize.acquire(staged_root / "corpus", generation=0):
            pass
        with materialize.acquire_current(pg, staged_root) as stamp:
            assert stamp["cleaning_ruleset"] == RULES
    finally:
        pg.drop()


# --- schema v6: the claim contract in the database ---------------------------------------------------

def mrow(sid, **kw):
    base = {**entry_of(gentry(sid)), "status": "ok", "http_status": 200, "error": None,
            "fetched_at": "2026-09-25T00:00:00Z"}
    return {**base, **kw}


def stage(pg, w, run_id, batch, fn):
    rec = store_broker.Recorder()
    fn(rec)
    with pg.read_staged(run_id, writer=w) as v:
        version = v.version()
    return pg.stage_batch(w, run_id, "fetch", batch, rec.requests, expected_version=version)


def open_run(pg, w, name, **kw):
    return pg.open_run(w, rid(name), producer_commit=SHA, extractor_version="x",
                       cleaning_ruleset="none", **kw)


def test_new_claims_need_a_written_version_and_are_registered_with_the_batch(repo):
    root, pg = repo
    local = artifact_store.LocalArtifacts(root)
    raw = local.put_bytes("raw", b"%PDF new bytes")
    with pg.writer() as w:
        run = open_run(pg, w, "claims")
        # a claim on bytes nobody wrote: refused, nothing staged or registered
        ghost = "e" * 64
        with pytest.raises(store.StoreError, match="was not written"):
            stage(pg, w, run.run_id, "b1", lambda b: b.upsert_manifest([mrow(
                "ost-n1", raw_path="raw/osti/ost-n1.pdf", sha256=ghost, bytes=3)]))
        for bad, why in ((dict(sha256="sha-x"), "lowercase sha256"),
                         (dict(sha256=None), "lowercase sha256"),
                         (dict(sha256=raw.sha256, bytes=1), "not the row's"),
                         (dict(sha256=raw.sha256, bytes="14"), "integer"),
                         (dict(raw_path="", sha256=raw.sha256), "non-empty path")):
            with pytest.raises(store.StoreError, match=why):
                stage(pg, w, run.run_id, "b1", lambda b, bad=bad: b.upsert_manifest([mrow(
                    "ost-n1", **{"raw_path": "raw/osti/ost-n1.pdf", "bytes": raw.size, **bad})]))
        assert q(pg, "SELECT count(*) FROM artifacts")[0][0] == 0
        assert q(pg, "SELECT staged_seq FROM runs")[0][0] == 0
        got = stage(pg, w, run.run_id, "b1", lambda b: b.upsert_manifest([mrow(
            "ost-n1", raw_path="raw/osti/ost-n1.pdf", sha256=raw.sha256, bytes=raw.size)]))
        assert got.status == "applied"
        assert q(pg, "SELECT stage, sha256, size, first_run FROM artifacts") == [
            ("raw", raw.sha256, raw.size, run.run_id)]
        assert q(pg, "SELECT locator, kind FROM artifact_locators") == [(raw.locator, "local")]
        assert q(pg, "SELECT run_id, stage, sha256, batch_seq FROM run_artifacts") == [
            (run.run_id, "raw", raw.sha256, 1)]
        # an exact retry answers from the receipt and registers nothing twice
        again = stage(pg, w, run.run_id, "b1", lambda b: b.upsert_manifest([mrow(
            "ost-n1", raw_path="raw/osti/ost-n1.pdf", sha256=raw.sha256, bytes=raw.size)]))
        assert again.retried
        # an unchanged legacy claim needs no version (the seeded row's files are legacy paths)
        stage(pg, w, run.run_id, "b2", lambda b: b.update_manifest_fields(
            {"ost-g-have": {"quality": {"re": 1}}}))
        assert q(pg, "SELECT count(*) FROM run_artifacts")[0][0] == 1


def test_the_database_refuses_a_batch_whose_new_claim_was_not_registered(repo, monkeypatch):
    """With the client-side check switched off, sealing still refuses the batch: the rule lives
    in schema v6, not only in the stager."""
    root, pg = repo
    local = artifact_store.LocalArtifacts(root)
    art = local.put_bytes("text", b"text bytes")
    monkeypatch.setattr(store_staging.StagedWriteView, "_claim_artifacts", lambda self, items: None)
    with pg.writer() as w:
        run = open_run(pg, w, "db-rule")
        with pytest.raises(sqlerr(), match="neither unchanged nor an artifact registered"):
            stage(pg, w, run.run_id, "b1", lambda b: b.update_manifest_fields(
                {"ost-g-have": {"text_sha256": art.sha256}}))
        # unchanged claims (a metrics update) pass without any registration
        stage(pg, w, run.run_id, "b1", lambda b: b.update_manifest_fields(
            {"ost-g-have": {"quality": {"x": 1}}}))
        # a claim registered for ANOTHER run does not count for this one
        other = open_run(pg, w, "db-rule-other")
        monkeypatch.undo()
        stage(pg, w, other.run_id, "b1", lambda b: b.upsert_manifest([mrow(
            "ost-o", text_path="text/ost-o.md", text_sha256=art.sha256)]))
        monkeypatch.setattr(store_staging.StagedWriteView, "_claim_artifacts",
                            lambda self, items: None)
        with pytest.raises(sqlerr(), match="neither unchanged"):
            stage(pg, w, run.run_id, "b2", lambda b: b.upsert_manifest([mrow(
                "ost-p", text_path="text/ost-p.md", text_sha256=art.sha256)]))
        # a JSON null identity next to a new path is never a registered identity
        with pytest.raises(sqlerr(), match="neither unchanged"):
            stage(pg, w, run.run_id, "b2", lambda b: b.upsert_manifest([mrow(
                "ost-p", corpus_path="corpus/ost-p.md", corpus_sha256=None)]))
    # an unchecked run (metadata only) keeps the step-2 rules
    with pg.writer() as w:
        run = open_run(pg, w, "unchecked", artifacts="unchecked")
        stage(pg, w, run.run_id, "b1", lambda b: b.upsert_manifest([mrow(
            "ost-q", text_path="text/ost-q.md", text_sha256="sha-q")]))


def test_a_failed_batch_rolls_its_registrations_back_and_the_retry_registers(repo, monkeypatch):
    root, pg = repo
    art = artifact_store.LocalArtifacts(root).put_bytes("corpus", b"cleaned")
    real = store_staging.StagedWriteView._register_artifacts

    def then_fail(self, need):
        real(self, need)
        raise KeyboardInterrupt("killed after registering")
    with pg.writer() as w:
        run = open_run(pg, w, "rollback")
        with monkeypatch.context() as m:
            m.setattr(store_staging.StagedWriteView, "_register_artifacts", then_fail)
            with pytest.raises(KeyboardInterrupt):
                stage(pg, w, run.run_id, "b1", lambda b: b.update_manifest_fields(
                    {"ost-g-have": {"corpus_sha256": art.sha256}}))
        for t in ("artifacts", "artifact_locators", "run_artifacts", "batches", "revisions"):
            assert q(pg, f"SELECT count(*) FROM {t}")[0][0] == 0, t
        assert (root / art.locator).exists()           # the version stays for the retry
        stage(pg, w, run.run_id, "b1", lambda b: b.update_manifest_fields(
            {"ost-g-have": {"corpus_sha256": art.sha256}}))
        assert q(pg, "SELECT count(*) FROM run_artifacts")[0][0] == 1


def test_contract_tables_of_v6(repo):
    root, pg = repo
    art = artifact_store.LocalArtifacts(root).put_bytes("raw", b"x" * 9)
    with pg.writer() as w:
        run = open_run(pg, w, "contracts")
        stage(pg, w, run.run_id, "b1", lambda b: b.upsert_manifest([mrow(
            "ost-c", raw_path="raw/osti/ost-c.pdf", sha256=art.sha256, bytes=9)]))
    with pg._connect(autocommit=True) as conn:
        for statement in ("UPDATE artifact_locators SET locator = 'x'",
                          "DELETE FROM artifact_locators",
                          "UPDATE artifact_locators SET verified_at = NULL",
                          "UPDATE run_artifacts SET batch_seq = 2", "DELETE FROM run_artifacts",
                          "UPDATE runs SET artifact_policy = 'unchecked'",
                          "INSERT INTO artifact_locators (stage, sha256, locator, kind) VALUES "
                          f"('raw', '{art.sha256}', 'raw/osti/ost-c.pdf', 'local')"):
            with pytest.raises(sqlerr()):
                conn.execute(statement)
        # a reference is added only by the transaction applying a batch ...
        other = "d" * 64
        conn.execute("INSERT INTO artifacts (stage, sha256, size) VALUES ('text', %s, 1)",
                     [other])
        with pytest.raises(sqlerr(), match="no batch being applied"):
            conn.execute("INSERT INTO run_artifacts VALUES (%s, 'text', %s, 1)",
                         [run.run_id, other])
        # the verification time is the one thing a locator may change, forward only
        conn.execute("UPDATE artifact_locators SET verified_at = now()")
        with pytest.raises(sqlerr()):
            conn.execute("UPDATE artifact_locators SET verified_at = verified_at - interval '1 d'")
        # a new run's policy defaults to versioned
        assert conn.execute("SELECT artifact_policy FROM runs").fetchone()[0] == "versioned"
    # an aborted run's references are purged; the identity, locator and file stay
    with pg.writer() as w:
        pg.abort_run(w, run.run_id, reason="test")
        while store_staging.purge_run(pg, w, run.run_id):
            pass
    assert q(pg, "SELECT count(*) FROM run_artifacts")[0][0] == 0
    assert q(pg, "SELECT count(*) FROM artifact_locators")[0][0] == 1
    assert (root / art.locator).exists()


def test_a_reference_needs_a_located_artifact(repo, monkeypatch):
    """... and only to an artifact with a locator (the stager adds it; here it does not)."""
    root, pg = repo
    art = artifact_store.LocalArtifacts(root).put_bytes("text", b"located?")
    q_ = "INSERT INTO artifacts (stage, sha256, size) VALUES ('text', %s, %s)"
    with pg._connect(autocommit=True) as conn:
        conn.execute(q_, [art.sha256, art.size])

    def references_only(self, need):
        self._q("INSERT INTO run_artifacts (run_id, stage, sha256, batch_seq) SELECT %s, s, h, %s "
                "FROM unnest(%s::text[], %s::text[]) AS u(s, h)",
                [self.run_id, self.seq, [k[0] for k in need], [k[1] for k in need]])
    monkeypatch.setattr(store_staging.StagedWriteView, "_register_artifacts", references_only)
    with pg.writer() as w:
        run = open_run(pg, w, "unlocated")
        with pytest.raises(sqlerr(), match="no local locator"):
            stage(pg, w, run.run_id, "b1", lambda b: b.upsert_manifest([mrow(
                "ost-u", text_path="text/ost-u.md", text_sha256=art.sha256)]))
        monkeypatch.undo()
        stage(pg, w, run.run_id, "b1", lambda b: b.upsert_manifest([mrow(
            "ost-u", text_path="text/ost-u.md", text_sha256=art.sha256)]))
    assert q(pg, "SELECT locator FROM artifact_locators") == [(art.locator,)]


def test_a_versioned_run_freezes_only_with_the_artifact_gate(repo):
    root, pg = repo
    with pg.writer() as w:
        run = open_run(pg, w, "gate-required")
        with pytest.raises(store.StoreError, match='"artifacts"'):
            pg.freeze(w, run.run_id, required_gates=["tests"])
    freeze = ("UPDATE runs SET status = 'frozen', frozen_seq = staged_seq, frozen_digest = "
              "nk_chain_origin(run_id), required_gates = %s WHERE run_id = %s")
    with pg._connect(autocommit=True) as conn:
        for gates in ('["tests"]', "[]", '"artifacts"'):
            with pytest.raises(sqlerr(), match="artifacts|gate"):
                conn.execute(freeze, [gates, run.run_id])
        conn.execute(freeze, ['["artifacts","tests"]', run.run_id])
        # and a locator is never created as already verified
        conn.execute("INSERT INTO artifacts (stage, sha256, size) VALUES ('raw', %s, 1)",
                     ["c" * 64])
        with pytest.raises(sqlerr(), match="unverified"):
            conn.execute("INSERT INTO artifact_locators (stage, sha256, locator, kind, "
                          "verified_at) VALUES ('raw', %s, %s, 'local', now())",
                          ["c" * 64, artifact_store.local_locator("raw", "c" * 64)])


def test_resolution_follows_generation_membership(repo, monkeypatch, clock):
    root, pg = repo
    g0 = full_round(monkeypatch, pg, root, "res-1")
    with pg.read() as v:
        have = v.resolve_artifact("ost-g-have", store.Stage.RAW)
        assert have.locator == "file:raw/osti/ost-g-have.md"   # an unchanged legacy claim
        cjk = v.resolve_artifact("ost-g-cjk", store.Stage.TEXT)
        assert cjk.locator == "file:" + artifact_store.local_locator("text", cjk.sha256)
        assert v.resolve_artifact("ost-g-404", store.Stage.RAW) is None   # pruned
        assert v.provenance()["generation"] == g0
    # a staged change is visible to its run only, and resolves to its new version
    new = artifact_store.LocalArtifacts(root).put_bytes("text", HEADER.encode() + b"v2 " * 99)
    with pg.writer(round_id=rid("res-2")) as w:
        run = pg.open_run(w, rid("res-2"), producer_commit=SHA, extractor_version="x",
                          cleaning_ruleset=RULES)
        stage(pg, w, run.run_id, "b1", lambda b: b.update_manifest_fields(
            {"ost-g-cjk": {"text_sha256": new.sha256}}))
        with pg.read_staged(run.run_id, writer=w) as v:
            assert v.resolve_artifact("ost-g-cjk", "text").sha256 == new.sha256
            assert v.provenance()["run"] == run.run_id
            assert artifact_store.for_view(v, root) is not None
        with pg.read() as v:
            assert v.resolve_artifact("ost-g-cjk", "text").sha256 == cjk.sha256
            assert artifact_store.for_view(v, root) is None     # committed views: no writes
        pg.abort_run(w, run.run_id, reason="test")
    assert assert_readable(pg, root) > 0


# --- crash injection: G stays readable ---------------------------------------------------------------

def _g0(monkeypatch, pg, root):
    g0 = full_round(monkeypatch, pg, root, "base")
    materialize.refresh(pg, root)
    with pg.writer() as w:
        store_staging.pin_generation(pg, w, g0, holder="test", reason="keep G0 reconstructible")
    return g0


STEP_CHILD = r"""
import json, os, sys
sys.path[:0] = ["scripts", "tests"]
import pipeline_repo
root, spec = sys.argv[1], json.loads(sys.argv[2])
pipeline_repo.point(None, root)
import artifact_store, build_corpus, store_broker
class Resp:
    def __init__(self, status, content=b""):
        self.status_code, self.content, self.headers = status, content, {}
    def raise_for_status(self):
        if self.status_code >= 400:
            raise build_corpus.requests.HTTPError(f"{self.status_code} Client Error")
bodies = spec["bodies"]
build_corpus.requests.get = lambda url, **kw: Resp(200, bodies[url].encode())
build_corpus.HOST_DELAY = {}
seen = {"n": 0}
def crash(point):
    if point == spec["point"]:
        seen["n"] += 1
        if seen["n"] == spec["after"]:
            os._exit(9)
artifact_store._crash = crash
if spec["point"] == "submit":
    real = store_broker.Client.submit
    def submit(self, *a, **k):
        seen["n"] += 1
        if seen["n"] == spec["after"]:
            os._exit(9)
        return real(self, *a, **k)
    store_broker.Client.submit = submit
sys.argv = ["build_corpus.py", "--workers", "1", *spec.get("args", [])]
build_corpus.main()
"""


@pytest.mark.parametrize("point,after", [("published", 3), ("published", 9), ("linked", 2),
                                         ("synced", 1), ("submit", 1)])
def test_a_killed_loader_leaves_G_readable_and_a_new_round_converges(repo, monkeypatch, clock,
                                                                    point, after):
    root, pg = repo
    g0 = _g0(monkeypatch, pg, root)
    readable = assert_readable(pg, root)
    materialized = files(root, ("corpus",))
    # a later round: every held document is re-fetched with new bytes (upstream drift)
    legacy_files = files(root, ("raw", "text"))
    new = {url_of(s): body_for(s).decode() + "\nrevised upstream\n"
           for s in [e for e in IDS if not e.endswith("404")] + ["ost-g-have", "ost-g-retry"]}
    with staged(pg, "crash") as (_w, rnd):
        env = {**{k: v for k, v in os.environ.items() if not k.startswith("NEKAISE_STORE")},
               **rnd.broker.env(), "NEKAISE_STORE": "postgres", "NEKAISE_PG_DSN": DSN,
               "NEKAISE_PG_SCHEMA": pg.schema, "NEKAISE_RUN_ID": rnd.run.run_id}
        spec = {"bodies": new, "point": point, "after": after, "args": ["--force"]}
        got = subprocess.run([sys.executable, "-c", STEP_CHILD, str(root), json.dumps(spec)],
                             env=env, capture_output=True, text=True, cwd=REPO)
        assert got.returncode == 9, got.stderr[-2000:]
        # G is untouched: every claim still resolves to its bytes, corpus/ is as materialized,
        # and the killed loader wrote nothing but immutable versions
        assert files(root, ("raw", "text")) == legacy_files
        assert assert_readable(pg, root) == readable
        assert files(root, ("corpus",)) == materialized
        with materialize.acquire_current(pg, root):
            pass
        abandon(pg, rnd)
    with staged(pg, "crash-retry") as (_w, rnd):
        spec = {"bodies": new, "point": "none", "after": 0, "args": ["--force"]}
        env["NEKAISE_RUN_ID"] = rnd.run.run_id
        env.update(rnd.broker.env())
        got = subprocess.run([sys.executable, "-c", STEP_CHILD, str(root), json.dumps(spec)],
                             env=env, capture_output=True, text=True, cwd=REPO)
        assert got.returncode == 0, got.stderr[-2000:]
        steps(monkeypatch, pg, rnd, root, "prune", "clean")
        g1 = promote(rnd)
    assert g1 == g0 + 1
    materialize.refresh(pg, root)
    assert assert_readable(pg, root) > 0
    assert assert_readable(pg, root, g0) == readable       # the retained generation too
    with pg.read() as v:
        row = v.get_manifest(["ost-g-have"])["ost-g-have"]
    assert row["sha256"] == sha(new[url_of("ost-g-have")].encode())    # re-fetched, drifted
    assert (root / "raw/osti/ost-g-have.md").read_bytes() == OLD_BODY  # never overwritten
    assert (root / "text/ost-g-have.md").read_bytes().endswith(OLD_BODY)


def test_a_failed_prune_moves_no_bytes_and_pruning_keeps_retained_payloads(repo, monkeypatch,
                                                                          clock):
    root, pg = repo
    g0 = _g0(monkeypatch, pg, root)
    tree = snapshot_tree(root)
    with pg.read() as v:
        held = v.get_manifest(["ost-g-cjk"])["ost-g-cjk"]
    # make one held document prunable (a reviewed off-topic drop)
    ids_file = root / "workspace" / "drop.txt"
    ids_file.write_text("ost-g-cjk\n")
    with staged(pg, "prune-fail") as (_w, rnd):
        with monkeypatch.context() as m:
            child_env(m, pg, rnd)
            m.setattr(store_broker.StepSession, "submit",
                      lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt("killed")))
            with pytest.raises(KeyboardInterrupt):
                handoff(root, rnd)
                run_step(m, root, "prune", "--drop-ids-from", str(ids_file))
        assert snapshot_tree(root) == tree            # nothing moved, nothing deleted
        assert not (root / "workspace" / "prune-quarantine").exists()
        abandon(pg, rnd)
    with staged(pg, "prune-ok") as (_w, rnd):
        with monkeypatch.context() as m:
            child_env(m, pg, rnd)
            handoff(root, rnd)
            run_step(m, root, "prune", "--drop-ids-from", str(ids_file))
        assert snapshot_tree(root) == tree            # membership changed, bytes did not
        g1 = promote(rnd)
    with pg.read() as v:
        assert "ost-g-cjk" not in v.get_manifest(["ost-g-cjk"])
    stats = materialize.refresh(pg, root)
    assert stats["mode"] == "diff" and stats["removed"] == 1
    assert not (root / "corpus" / "ost-g-cjk.md").exists()
    # the pruned document's payloads are all still there for the retained generation
    local = artifact_store.LocalArtifacts(root)
    for stage in artifact_store.STAGES:
        c = artifact_store.claim(held, stage)
        assert local.has(stage, c[1]) or (root / c[0]).exists()
    assert assert_readable(pg, root, g0) > 0 and g1 == g0 + 1


def test_a_clean_killed_between_batches_leaves_G_readable_and_a_new_round_converges(
        repo, monkeypatch, clock, tmp_path):
    root, pg = repo
    g0 = _g0(monkeypatch, pg, root)
    readable = assert_readable(pg, root)
    materialized = files(root, ("corpus",))
    ruleset = "toc_leaders,page_markers"                 # a policy change re-cleans everything
    monkeypatch.setattr(clean_corpus, "METADATA_BATCH_ROWS", 2)
    monkeypatch.setattr(clean_corpus, "GROUP_COMMIT", 3)      # groups and batches interleave
    monkeypatch.setattr(clean_corpus, "CLEAN_CHUNK", 2)
    calls = {"n": 0}
    real = store_broker.StepSession.submit

    def third_fails(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt("killed between metadata batches")
        return real(self, *a, **k)
    with staged(pg, "clean-fail", ruleset=ruleset) as (_w, rnd):
        with monkeypatch.context() as m:
            child_env(m, pg, rnd)
            m.setattr(store_broker.StepSession, "submit", third_fails)
            with pytest.raises(KeyboardInterrupt):
                run_step(m, root, "clean")
        assert calls["n"] == 3
        assert assert_readable(pg, root) == readable
        assert files(root, ("corpus",)) == materialized
        abandon(pg, rnd)
    with staged(pg, "clean-ok", ruleset=ruleset) as (_w, rnd):
        with monkeypatch.context() as m:
            child_env(m, pg, rnd)
            run_step(m, root, "clean")
            # a versioned staged run's --check verifies claims, not corpus/
            m.setattr(sys, "argv", ["clean_corpus.py", "--check"])
            clean_corpus.main()
            with pytest.raises(SystemExit, match="pinned"):
                m.setattr(sys, "argv", ["clean_corpus.py", "--rules", "none"])
                clean_corpus.main()
        g1 = promote(rnd)
    stats = materialize.refresh(pg, root)
    assert stats["mode"] == "diff"
    # the same result as one uninterrupted re-clean: compare with a fresh full materialization
    other = tmp_path / "fresh-corpus"
    materialize.refresh(pg, root, corpus_dir=other)
    assert files(root, ("corpus",)).keys() >= {f"corpus/{p.name}" for p in other.glob("*.md")}
    for p in other.glob("*.md"):
        assert (root / "corpus" / p.name).read_bytes() == p.read_bytes()
    assert assert_readable(pg, root) > 0 and assert_readable(pg, root, g0) == readable
    assert g1 == g0 + 1
    with pg.read() as v:
        assert all(r["cleaner_version"] == f"clean_corpus/2;rules={ruleset}"
                   for r in rows_of(v).values() if r.get("corpus_path"))


def test_a_failed_promotion_leaves_G_and_the_retry_promotes(repo, monkeypatch, clock):
    root, pg = repo
    g0 = _g0(monkeypatch, pg, root)
    readable = assert_readable(pg, root)
    with staged(pg, "promo") as (w, rnd):
        with monkeypatch.context() as m:
            child_env(m, pg, rnd)
            (root / "workspace" / "drop.txt").write_text("ost-g-table\n")
            handoff(root, rnd)
            run_step(m, root, "prune", "--drop-ids-from", str(root / "workspace" / "drop.txt"))
        rnd.freeze(["artifacts"])
        rnd.verify_artifacts()
        real = store_staging._head

        def head_then_die(conn, *, lock=False):
            if lock:   # promotion's own row lock: fail inside its transaction
                conn.execute("INSERT INTO generations (generation) VALUES (-1)")
            return real(conn, lock=lock)
        with monkeypatch.context() as m:
            m.setattr(store_staging, "_head", head_then_die)
            with pytest.raises(sqlerr()):
                rnd.promote()
        assert assert_readable(pg, root) == readable
        with pg.read() as v:
            assert v.generation == g0 and "ost-g-table" in v.get_manifest(["ost-g-table"])
        assert rnd.promote() == g0 + 1
    materialize.refresh(pg, root)
    assert not (root / "corpus" / "ost-g-table.md").exists()
    assert assert_readable(pg, root, g0) == readable


@pytest.mark.parametrize("point", ["stamped", "installed", "swept", "before-complete",
                                   "adopt-synced"])
def test_a_crashed_materialization_is_refused_and_the_next_refresh_converges(
        repo, monkeypatch, clock, tmp_path, point):
    root, pg = repo
    g0 = full_round(monkeypatch, pg, root, "mat")
    legacy_corpus = (root / "corpus" / "ost-g-have.md").read_bytes()

    def crash(p):
        if p == point:
            raise KeyboardInterrupt(f"killed at {p}")
    if point == "adopt-synced":
        monkeypatch.setattr(artifact_store, "_crash", lambda p: crash("adopt-" + p))
    else:
        monkeypatch.setattr(materialize, "_crash", crash)
    with pytest.raises(KeyboardInterrupt):
        materialize.refresh(pg, root)
    monkeypatch.undo()
    with pytest.raises(materialize.MaterializeError, match="not a complete materialization"):
        with materialize.acquire(root / "corpus", generation=g0):
            pass
    assert materialize.read_stamp(root / "corpus")["state"] == "refreshing"
    materialize.refresh(pg, root)
    fresh = tmp_path / "fresh"
    materialize.refresh(pg, root, corpus_dir=fresh)
    got = {p.name: p.read_bytes() for p in (root / "corpus").glob("*.md")}
    assert got == {p.name: p.read_bytes() for p in fresh.glob("*.md")}
    assert not [p for p in (root / "corpus").iterdir() if p.name.startswith(".mat-")]
    with materialize.acquire(root / "corpus", generation=g0):
        pass
    # the legacy cleaned file it replaced is kept as an immutable version
    assert artifact_store.LocalArtifacts(root).has("corpus", sha(legacy_corpus))


def test_materialization_follows_eligibility_and_needs_a_matching_generation(repo, monkeypatch,
                                                                            clock, tmp_path):
    root, pg = repo
    g0 = full_round(monkeypatch, pg, root, "elig")
    materialize.refresh(pg, root)
    # a stray file that no row of G claims is removed (after being preserved)
    stray = root / "corpus" / "ost-stray.md"
    stray.write_bytes(b"unprovenanced")
    stats = materialize.refresh(pg, root, full=True)
    assert stats["removed"] == 1 and not stray.exists()
    assert artifact_store.LocalArtifacts(root).has("corpus", sha(b"unprovenanced"))
    # a newer generation makes the materialization stale for consumers until refreshed
    with staged(pg, "elig-2") as (_w, rnd):
        with monkeypatch.context() as m:
            child_env(m, pg, rnd)
            (root / "workspace" / "drop.txt").write_text("ost-g-german\n")
            handoff(root, rnd)
            run_step(m, root, "prune", "--drop-ids-from", str(root / "workspace" / "drop.txt"))
        g1 = promote(rnd)
    with pytest.raises(materialize.MaterializeError, match=f"generation {g0}"):
        with materialize.acquire_current(pg, root):
            pass
    # an older generation can still be materialized elsewhere while it is retained
    with pg.writer() as w:
        store_staging.pin_generation(pg, w, g0, holder="test", reason="audit")
    old = tmp_path / "g0"
    materialize.refresh(pg, root, corpus_dir=old, generation=g0)
    assert (old / "ost-g-german.md").exists()
    materialize.refresh(pg, root)
    assert not (root / "corpus" / "ost-g-german.md").exists()
    with materialize.acquire_current(pg, root) as stamp:
        assert stamp["generation"] == g1
    # a refresh never runs from a staged run's view
    with staged(pg, "elig-3") as (_w, rnd):
        with monkeypatch.context() as m:
            child_env(m, pg, rnd)
            import store as store_mod
            with pytest.raises(materialize.MaterializeError, match="staged run"):
                materialize.refresh(store_mod.open(root=root), root)
        abandon(pg, rnd)


def test_the_artifact_gate_detects_a_damaged_version(repo, monkeypatch, clock):
    root, pg = repo
    with staged(pg, "gate") as (_w, rnd):
        steps(monkeypatch, pg, rnd, root, "fetch")
        rnd.freeze(["artifacts"])
        with pg.read_staged(rnd.run.run_id, writer=rnd.writer) as v:
            ref = v.resolve_artifact("ost-g-cjk", "raw")
        path = root / ref.locator.removeprefix("file:")
        os.chmod(path, 0o644)
        path.write_bytes(b"damaged")
        result = rnd.verify_artifacts()
        assert result["failed"] == [("raw", ref.sha256, "missing or damaged")]
        with pytest.raises(store.StoreError, match="not passed every required gate"):
            rnd.promote()
    # never promoted (recovery aborts it: step 4)
    assert q(pg, "SELECT status FROM runs")[0][0] == "frozen"


# --- Codex review of step 3 (MERGE AFTER FIXES): regressions --------------------------------------------

def _legacy_baseline_generation(pg, root, name="legacy-g0") -> int:
    """A generation whose rows still claim the legacy files (a metadata-only batch)."""
    with staged(pg, name) as (w, rnd):
        stage(pg, w, rnd.run.run_id, "b1", lambda b: b.upsert_manifest([mrow(
            "ost-meta-only", status="failed", error="x")]))
        return promote(rnd)


def test_p1_a_retained_legacy_generation_resolves_to_its_own_bytes_after_replacement(
        repo, monkeypatch, clock):
    """Review P1 #1: G0 claims legacy corpus/ost-g-have.md; G1 re-cleans it and the refresh
    replaces that file (after adopting it); G2 prunes the document. G0 — through read_generation
    and through VersionedAccess — must keep resolving to G0's bytes, also after folding."""
    root, pg = repo
    legacy = (root / "corpus" / "ost-g-have.md").read_bytes()
    g0 = _legacy_baseline_generation(pg, root)
    with pg.writer() as w:
        store_staging.pin_generation(pg, w, g0, holder="test", reason="retain G0")
    with pg.read_generation(g0) as v:
        g0_row = v.get_manifest(["ost-g-have"])["ost-g-have"]
        assert v.resolve_artifact("ost-g-have", "corpus").locator == \
            "file:corpus/ost-g-have.md"                 # nothing adopted yet: the legacy path
    readable = assert_readable(pg, root, g0)
    full_round(monkeypatch, pg, root, "legacy-g1")      # re-cleans ost-g-have
    materialize.refresh(pg, root)
    assert (root / "corpus" / "ost-g-have.md").read_bytes() != legacy   # replaced
    access = artifact_store.VersionedAccess(root)

    def resolves_to_g0():
        with pg.read_generation(g0) as v:
            ref = v.resolve_artifact("ost-g-have", "corpus")
        assert ref.sha256 == sha(legacy) == g0_row["corpus_sha256"]
        assert ref.locator == "file:" + artifact_store.local_locator("corpus", ref.sha256)
        assert (root / ref.locator[5:]).read_bytes() == legacy
        assert access.path(g0_row, "corpus") == root / ref.locator[5:]  # both APIs agree
        assert assert_readable(pg, root, g0) == readable
    resolves_to_g0()
    with staged(pg, "legacy-g2") as (_w, rnd):            # prune the document
        with monkeypatch.context() as m:
            child_env(m, pg, rnd)
            handoff(root, rnd)
            (root / "workspace" / "drop.txt").write_text("ost-g-have\n")
            run_step(m, root, "prune", "--drop-ids-from", str(root / "workspace" / "drop.txt"))
        promote(rnd)
    materialize.refresh(pg, root)
    assert not (root / "corpus" / "ost-g-have.md").exists()
    resolves_to_g0()
    with pg.writer() as w:
        store_staging.fold_all(pg, w)                    # folds G0, stops at the pin
    assert q(pg, "SELECT generation FROM projection_state")[0][0] == g0
    resolves_to_g0()


def test_p1_a_same_size_damaged_version_never_costs_the_intact_original(repo, monkeypatch, clock):
    """Review P1 #2: the canonical address of the legacy corpus identity holds same-size
    damaged bytes; the refresh must not accept it as the adoption of the intact legacy file and
    then replace that file: it fails, the original stays."""
    root, pg = repo
    legacy = (root / "corpus" / "ost-g-have.md").read_bytes()
    full_round(monkeypatch, pg, root, "damage-g0")
    local = artifact_store.LocalArtifacts(root)
    target = local.path("corpus", sha(legacy))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(bytes(len(legacy)))               # same size, wrong bytes
    with pytest.raises(artifact_store.ArtifactError, match="does not hold the bytes"):
        materialize.refresh(pg, root)
    assert (root / "corpus" / "ost-g-have.md").read_bytes() == legacy
    assert materialize.read_stamp(root / "corpus")["state"] == "refreshing"


def test_p1_retried_publication_makes_the_reused_directory_chain_durable(repo, monkeypatch):
    """Review P1 #3: a group commit creates fresh fan-out directories, links, and dies before its
    final sync; a single-file retry then reuses the address and a small batch references it.
    Every ancestor directory up to the root is fsynced along the way."""
    root, pg = repo
    data = b"a version whose directories a crashed group created"
    local = artifact_store.LocalArtifacts(root)
    artifact_store._DURABLE_DIRS.clear()

    def crash(p):
        if p == "group-linked":
            raise KeyboardInterrupt(p)
    with monkeypatch.context() as m:
        m.setattr(artifact_store, "_crash", crash)
        with pytest.raises(KeyboardInterrupt):
            local.commit([artifact_store.write_pending(root, "text", data)])
    leaf = local.path("text", sha(data)).parent
    assert local.path("text", sha(data)).exists()
    chain = {leaf, leaf.parent, leaf.parent.parent, local.base, root}
    synced = []
    real = artifact_store._fsync_dir
    monkeypatch.setattr(artifact_store, "_fsync_dir", lambda p: (synced.append(Path(p).resolve()),
                                                                 real(p)))
    artifact_store._DURABLE_DIRS.clear()               # a new process retries
    local.put_bytes("text", data)
    assert {p.resolve() for p in chain} <= set(synced), synced
    synced.clear()
    artifact_store._DURABLE_DIRS.clear()               # and another one stages the reference
    with pg.writer() as w:
        run = open_run(pg, w, "chain")
        stage(pg, w, run.run_id, "b1", lambda b: b.upsert_manifest([mrow(
            "ost-chain", text_path="text/ost-chain.md", text_sha256=sha(data))]))
    assert {p.resolve() for p in chain} <= set(synced), synced


def _forge(pg, run_id, key, row, before_sha):
    """Direct SQL: one batch of `run_id` applying a manifest put with a chosen before-image."""
    text = store.canonical_row(row)
    request = "[]"
    with pg._connect(autocommit=True) as conn, conn.transaction():
        seq, = conn.execute("SELECT staged_seq FROM runs WHERE run_id = %s", [run_id]).fetchone()
        conn.execute("INSERT INTO batches (run_id, step, batch, request_digest, request_text, "
                     "basis_seq) VALUES (%s, 'forge', %s, %s, %s, %s)",
                     [run_id, f"b{seq}", sha(request.encode()), request, seq])
        conn.execute("UPDATE batches SET status = 'applied', seq = %s, applied_at = now() WHERE "
                     "run_id = %s AND batch = %s", [seq + 1, run_id, f"b{seq}"])
        conn.execute("INSERT INTO revisions (run_id, batch_seq, tbl, key, op, row_text, "
                     "row_sha256, before_sha256) VALUES (%s, %s, 'manifest', %s, 'put', %s, %s, "
                     "%s)", [run_id, seq + 1, key, text, sha(text.encode()), before_sha])
        conn.execute("UPDATE batches SET sealed = true WHERE run_id = %s AND batch = %s",
                     [run_id, f"b{seq}"])


def test_p2_a_revision_cannot_supply_its_own_unchanged_before_image(repo):
    """Review P2 #4: the seal compares with the row actually visible at the batch's basis, not
    with whatever row a revision's before_sha256 names — its own, or one an unrelated (aborted)
    run staged."""
    root, pg = repo
    with pg.read() as v:
        have = v.get_manifest(["ost-g-have"])["ost-g-have"]
    forged = {**have, "corpus_sha256": "e" * 64}             # bytes nobody wrote
    digest = sha(store.canonical_row(forged).encode())
    with pg.writer() as w:
        run = open_run(pg, w, "forge")
        other = open_run(pg, w, "forge-other", artifacts="unchecked")
        stage(pg, w, other.run_id, "b1", lambda b: b.upsert_manifest([forged]))
        pg.abort_run(w, other.run_id, reason="unrelated")
    for before in (digest, None):
        with pytest.raises(sqlerr(), match="neither unchanged"):
            _forge(pg, run.run_id, "ost-g-have", forged, before)
    # a new key claiming unwritten bytes, "superseding" a row only the aborted run staged
    new = {**forged, "id": "ost-g-forged"}
    with pytest.raises(sqlerr(), match="neither unchanged"):
        _forge(pg, run.run_id, "ost-g-forged", new, sha(store.canonical_row(new).encode()))
    # an honest unchanged claim still passes without any registration
    fine = {**have, "quality": {"re": 2}}
    _forge(pg, run.run_id, "ost-g-have", fine, sha(store.canonical_row(have).encode()))
    assert q(pg, "SELECT count(*) FROM run_artifacts")[0][0] == 0


def test_p2_references_without_a_local_locator_are_refused_and_never_verified(repo, monkeypatch):
    """Review P2 #5: an identity located only in an object store is not a readable version
    before stage 5: referencing it is refused, and a reference that got in anyway fails the
    artifact gate instead of being skipped."""
    root, pg = repo
    sha_x = "f" * 64
    with pg._connect(autocommit=True) as conn:
        conn.execute("INSERT INTO artifacts (stage, sha256, size) VALUES ('corpus', %s, 3)",
                     [sha_x])
        conn.execute("INSERT INTO artifact_locators (stage, sha256, locator, kind) VALUES "
                     "('corpus', %s, 's3://bucket/x', 'object')", [sha_x])

    def references_only(self, need):
        self._q("INSERT INTO run_artifacts (run_id, stage, sha256, batch_seq) SELECT %s, s, h, %s "
                "FROM unnest(%s::text[], %s::text[]) AS u(s, h)",
                [self.run_id, self.seq, [k[0] for k in need], [k[1] for k in need]])
    monkeypatch.setattr(store_staging.StagedWriteView, "_register_artifacts", references_only)
    row = mrow("ost-obj", corpus_path="corpus/ost-obj.md", corpus_sha256=sha_x)
    with pg.writer() as w:
        run = open_run(pg, w, "object-only")
        with pytest.raises(sqlerr(), match="no local locator"):
            stage(pg, w, run.run_id, "b1", lambda b: b.upsert_manifest([row]))
        with pg._connect(autocommit=True) as conn:   # suppose it got in regardless
            conn.execute("ALTER TABLE run_artifacts DISABLE TRIGGER run_artifacts_insert")
        try:
            stage(pg, w, run.run_id, "b1", lambda b: b.upsert_manifest([row]))
        finally:
            with pg._connect(autocommit=True) as conn:
                conn.execute("ALTER TABLE run_artifacts ENABLE TRIGGER run_artifacts_insert")
        frozen = pg.freeze(w, run.run_id, required_gates=["artifacts"])
        result = artifact_store.verify_run(pg, w, run.run_id)
        assert result["failed"] == [("corpus", sha_x, "no readable local locator")]
        pg.record_gate(w, frozen, "artifacts", passed=not result["failed"])
        with pytest.raises(store.StoreError, match="not passed every required gate"):
            pg.promote(w, frozen)


def test_p2_versioned_cleaning_pages_bounds_and_stages_as_it_goes(repo, monkeypatch, clock):
    """Review P2 #6: build_versioned reads the manifest page by page, keeps a bounded number of
    documents in flight, and stages each durable group's metadata before cleaning the rest."""
    root, pg = repo
    extra = [mrow(f"ost-b-{i:02d}", text_path=f"text/ost-b-{i:02d}.md") for i in range(12)]
    for r in extra:
        data = HEADER.encode() + f"body {r['id']} {TEXT}".encode()
        (root / r["text_path"]).write_bytes(data)
        r["text_sha256"] = sha(data)
    with pg.writer() as w:
        with pg.transaction("more", expected_version=pg.version(), writer=w) as tx:
            tx.upsert_manifest(extra)
    monkeypatch.setattr(store, "MAX_PAGE", 4)
    monkeypatch.setattr(clean_corpus, "METADATA_BATCH_ROWS", 3)
    monkeypatch.setattr(clean_corpus, "GROUP_COMMIT", 3)
    monkeypatch.setattr(clean_corpus, "CLEAN_CHUNK", 2)
    monkeypatch.setattr(clean_corpus, "IN_FLIGHT_PER_WORKER", 1)
    events = []
    real_clean = clean_corpus._clean_many_versioned
    monkeypatch.setattr(clean_corpus, "_clean_many_versioned",
                        lambda tasks: (events.append(("clean", len(tasks))), real_clean(tasks))[1])
    real_submit = store_broker.StepSession.submit

    def submit(self, batch, requests):
        events.append(("batch", batch))
        return real_submit(self, batch, requests)
    monkeypatch.setattr(store_broker.StepSession, "submit", submit)
    real_commit = artifact_store.LocalArtifacts.commit
    monkeypatch.setattr(artifact_store.LocalArtifacts, "commit",
                        lambda self, p: (events.append(("durable", len(p))),
                                         real_commit(self, p))[1])
    with staged(pg, "bounded", ruleset="none") as (_w, rnd):
        with monkeypatch.context() as m:
            child_env(m, pg, rnd)
            run_step(m, root, "clean")
        promote(rnd)
    kinds = [e[0] for e in events]
    cleaned = [i for i, k in enumerate(kinds) if k == "clean"]
    batches = [i for i, k in enumerate(kinds) if k == "batch"]
    durable = [i for i, k in enumerate(kinds) if k == "durable"]
    assert len(batches) >= 4 and batches[0] < cleaned[-1]     # staged while still cleaning
    assert durable[0] < batches[0]                            # versions durable before metadata
    assert all(e[1] <= 2 for e in events if e[0] == "clean")
    with pg.read() as v:
        rows = rows_of(v)
    for r in extra:
        got = rows[r["id"]]
        assert got["corpus_source_sha256"] == r["text_sha256"]
        assert artifact_store.LocalArtifacts(root).has("corpus", got["corpus_sha256"])


def test_review2_pointer_only_rows_never_reach_the_corpus(repo, monkeypatch, clock, capsys):
    """Second review P1: the versioned builder, its check and the materializer apply the full
    training predicate (registry.is_training_eligible: pointer-only licences AND eligibility.json
    restrictions), like the legacy partition. A successful pointer-only row with extracted text
    and a stale corpus claim is not cleaned, loses its corpus metadata (raw/text provenance
    kept), fails the check while it still claims corpus data, and is never materialized."""
    root, pg = repo
    body = HEADER.encode() + b"private vendor text " + TEXT.encode()
    stale = HEADER.encode() + b"a stale cleaned copy"
    for rel, data in (("text/ost-private.md", body), ("corpus/ost-private.md", stale)):
        (root / rel).write_bytes(data)
    private = mrow("ost-private", license="proprietary-internal", text_path="text/ost-private.md",
                   text_sha256=sha(body), corpus_path="corpus/ost-private.md",
                   corpus_sha256=sha(stale), corpus_chars=20,
                   cleaner_version="clean_corpus/2;rules=none")
    with pg.writer() as w:
        with pg.transaction("private", expected_version=pg.version(), writer=w) as tx:
            tx.upsert_manifest([private])
    with staged(pg, "private-check") as (_w, rnd):     # the check flags the stale claim
        with monkeypatch.context() as m:
            child_env(m, pg, rnd)
            pipeline_repo.point(m, root, policy=None)
            m.setattr(sys, "argv", ["clean_corpus.py", "--check"])
            with pytest.raises(SystemExit):
                clean_corpus.main()
        assert "training-ineligible row (license or policy) has corpus metadata: ost-private" \
            in capsys.readouterr().out
        abandon(pg, rnd)
    g = full_round(monkeypatch, pg, root, "private")
    with pg.read() as v:
        row = v.get_manifest(["ost-private"])["ost-private"]
    import registry
    assert not any(f in row for f in registry.CORPUS_FIELDS)
    assert row["text_path"] == "text/ost-private.md" and row["text_sha256"] == sha(body)
    local = artifact_store.LocalArtifacts(root)
    cleaned = clean_corpus.clean_body(clean_corpus.split_header(body.decode())[1],
                                      clean_corpus.parse_rules(RULES))[0]
    assert not local.has("corpus", sha((HEADER + cleaned).encode()))   # never cleaned
    stats = materialize.refresh(pg, root)
    assert not (root / "corpus" / "ost-private.md").exists()
    assert stats["removed"] >= 1 and local.has("corpus", sha(stale))  # preserved, not served
    # even a row that claims a held version is not materialized while it is pointer-only
    held = local.put_bytes("corpus", b"claimed but ineligible")
    with staged(pg, "private-claim") as (w, rnd):
        stage(pg, w, rnd.run.run_id, "b1", lambda b: b.update_manifest_fields(
            {"ost-private": {"corpus_path": "corpus/ost-private.md",
                             "corpus_sha256": held.sha256}}))
        promote(rnd)
    materialize.refresh(pg, root)
    assert not (root / "corpus" / "ost-private.md").exists()
    with materialize.acquire_current(pg, root) as stamp:
        assert stamp["generation"] == g + 1


# --- the v5 -> v6 migration ------------------------------------------------------------------------------

def _load_v5(name: str):
    got = subprocess.run(["git", "-C", str(REPO), "show", f"{V5_COMMIT}:scripts/{name}.py"],
                         capture_output=True)
    if got.returncode:
        pytest.skip(f"{V5_COMMIT} not in this clone")
    spec = importlib.util.spec_from_loader(f"{name}_v5", loader=None)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # its dataclasses resolve their module there
    try:
        exec(compile(got.stdout, f"{name}_v5.py", "exec"), mod.__dict__)  # noqa: S102
    finally:
        del sys.modules[spec.name]
    return mod


def _v5_module():
    """store_pg.py exactly as step 2 shipped it (schema version 5)."""
    mod = _load_v5("store_pg")
    assert mod.SCHEMA_VERSION == 5
    return mod


@contextmanager
def v5_code(monkeypatch):
    """The step-2 store_pg AND store_staging, bound to each other, for as long as the block
    runs (store_pg imports store_staging lazily; the current one would write v6 columns)."""
    v5 = _v5_module()
    staging = _load_v5("store_staging")
    staging.store_pg = v5
    with monkeypatch.context() as m:
        m.setitem(sys.modules, "store_staging", staging)
        yield v5


def quiet(*_):
    pass


def test_a_v5_shadow_migrates_to_v6_and_keeps_replicating(tmp_path):
    import pg_shadow
    import store_pg
    import test_pg_shadow as shadow
    from test_store_pg_staging import export_bytes
    v5 = _v5_module()
    repo_ = shadow.Repo(tmp_path / "repo")
    repo_.write("registry/backends.json", json.dumps({"find_x": {"script": "x.py", "args": []}}))
    repo_.write("registry/eligibility.json", json.dumps({"version": 1, "restrictions": {}}))
    repo_.write("registry/rotation.json", json.dumps({"find_x": {"flag": "--page", "next": 1}}))
    repo_.write("registry/books.yaml", shadow.yaml_shard("books", [shadow.entry("oer-a")]))
    repo_.write("manifest/books.jsonl", shadow.manifest([shadow.mrow("oer-a"),
                                                         shadow.mrow("oer-b", text_chars=1e20)]))
    repo_.write("pruned_urls.txt", "https://e.org/old\n")
    c1 = repo_.commit("c1")
    schema = f"m6_{uuid.uuid4().hex[:12]}"
    old = v5.PgStore(repo_.path, dsn=DSN, schema=schema)       # the step-2 code
    try:
        pg_shadow.do_import(old, c1, repo_.path, log=quiet)
        repo_.write("pruned_urls.txt", "https://e.org/old\nhttps://e.org/v5\n")
        c2 = repo_.commit("c2")
        assert pg_shadow.do_sync(old, c2, repo_.path, log=quiet) == 1
        with old.read() as v:
            before = export_bytes(old, v, tmp_path / "v5")
        before_digests = pg_shadow.pg_digests(old)
        auth = old.authority()

        new = store_pg.PgStore(repo_.path, dsn=DSN, schema=schema)   # migrates 5 -> 6
        assert q(new, "SELECT schema_version FROM state")[0][0] == 6
        assert pg_shadow.pg_digests(new) == before_digests
        with new.read() as v:
            assert export_bytes(new, v, tmp_path / "v6") == before
        assert new.authority() == auth
        assert pg_shadow.do_verify(new, repo_.path, log=quiet)
        repo_.write("pruned_urls.txt", "https://e.org/old\nhttps://e.org/v5\nhttps://e.org/v6\n")
        c3 = repo_.commit("c3")
        assert pg_shadow.do_sync(new, c3, repo_.path, log=quiet) == 1
        assert pg_shadow.do_verify(new, repo_.path, log=quiet)
        with pytest.raises(store.StoreError, match="version 6, code expects 5"):
            v5.PgStore(repo_.path, dsn=DSN, schema=schema)
        with pytest.raises(store.StoreError, match="restart with matching code"):
            with old.writer():
                pass
        # re-opening does not re-run the migration
        again = store_pg.PgStore(repo_.path, dsn=DSN, schema=schema)
        assert again.authority() == auth
    finally:
        store_pg.PgStore(repo_.path, dsn=DSN, schema=schema, create=False).drop()


def test_v5_runs_are_backfilled_unchecked_and_keep_their_rules(tmp_path, monkeypatch):
    """Runs staged by v5 code (open with applied batches whose rows claim unregistered bytes,
    promoted) migrate as 'unchecked': the open one keeps staging and promotes under the step-2
    rules; runs opened afterwards are 'versioned'."""
    import store_pg
    from test_store_contract import write_config
    root = tmp_path / "r"
    write_config(root)
    schema = f"m6r_{uuid.uuid4().hex[:12]}"
    old_open, old_done = rid("v5-open"), rid("v5-done")
    try:
        with v5_code(monkeypatch) as v5:
            old = v5.PgStore(root, dsn=DSN, schema=schema)
            old.pin_config_from_files()
            with old.writer() as w:
                for run_id in (old_done, old_open):
                    old.open_run(w, run_id, producer_commit=SHA, extractor_version="x",
                                 cleaning_ruleset="none")
                    rec = store_broker.Recorder()
                    rec.upsert_manifest([mrow(f"ost-{run_id}", raw_path="raw/x.pdf",
                                              sha256="sha-unregistered")])
                    with old.read_staged(run_id, writer=w) as v:
                        version = v.version()
                    old.stage_batch(w, run_id, "fetch", "b1", rec.requests,
                                    expected_version=version)
                    if run_id == old_done:
                        frozen = old.freeze(w, run_id, required_gates=["tests"])
                        old.record_gate(w, frozen, "tests", passed=True)
                        old.promote(w, frozen)
        new =store_pg.PgStore(root, dsn=DSN, schema=schema)
        assert dict(q(new, "SELECT run_id, artifact_policy FROM runs")) == {
            old_open: "unchecked", old_done: "unchecked"}
        with new.read() as v:   # the v5 generation reads as before
            assert v.generation == 0 and f"ost-{old_done}" in rows_of(v)
        with new.writer() as w:
            rec = store_broker.Recorder()
            rec.upsert_manifest([mrow("ost-more", raw_path="raw/y.pdf", sha256="sha-y")])
            with pytest.raises(store.WriterError):     # its owner is gone (adoption: step 4)
                new.stage_batch(w, old_open, "fetch", "b2", rec.requests,
                                expected_version=store_staging.stage_version(old_open, 1))
            new.abort_run(w, old_open, reason="owner gone")
            while store_staging.purge_run(new, w, old_open):
                pass
            later = new.open_run(w, rid("v6-run"), producer_commit=SHA, extractor_version="x",
                                 cleaning_ruleset="none")
            assert later.artifact_policy == "versioned"
            with new.read_staged(later.run_id, writer=w) as v:
                version = v.version()
            with pytest.raises(store.StoreError, match="lowercase sha256"):
                new.stage_batch(w, later.run_id, "fetch", "b1", rec.requests,
                                expected_version=version)
        assert dict(q(new, "SELECT run_id, artifact_policy FROM runs")) == {
            old_open: "unchecked", old_done: "unchecked", rid("v6-run"): "versioned"}
    finally:
        store_pg.PgStore(root, dsn=DSN, schema=schema, create=False).drop()
