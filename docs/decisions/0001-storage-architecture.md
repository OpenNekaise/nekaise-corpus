# ADR 0001 — Storage and state architecture for 100x scale

- **Status:** accepted 2026-09-24
- **Decided by:** Codex (gpt-6-astra, reasoning high), on the operator's instruction that Codex makes
  the architecture decisions and Claude implements them.
- **Brief:** measured state on 2026-09-24 — manifest/ 2.0 GB JSONL + registry/ 522 MB YAML tracked in
  git, .git 28 GB, ~400 automated commits/week, raw/ 738 GB local; target 100x (~160M docs).

## Operator resolutions (2026-09-24)

The decision below assumes managed infrastructure; the operator resolved the cost/irreversible
choices as follows, and these override the record where they differ:

1. **PostgreSQL runs in local docker** on the corpus host, with continuous WAL archiving to the
   backup SSD. No synchronous standby until a second host exists.
2. **Payload bytes stay local for now** in the content-addressed pack layout, behind an
   S3-compatible interface, so moving to object storage later is a configuration change. No
   provider is chosen yet.
3. **Git history rewrite is pre-approved for stage 6**, after the migration is verified: archive
   the full history as a checksummed bundle in two places, then rewrite and force-push.

---

**Decision:** PostgreSQL owns operational state; private object storage owns payloads; immutable Parquet exports publish provenance. Git owns code and policy.

The current bottleneck is architectural: whole-corpus lists and dedup sets, shard rewrites, per-round state copies, and full corpus verification. Replacing YAML alone cannot solve it.

## 1. Source of truth: PostgreSQL

Use one PostgreSQL primary with a synchronous standby. Create typed tables for documents, fetch attempts, artifacts, eligibility applications, prune decisions, blocklist keys, backend cursors, runs, and publication outbox. Use JSONB only for variable metrics and source-specific metadata.

Keep immutable provenance revisions plus indexed current-state projections. Record old/new hashes on upstream drift; pruning changes membership and appends a reason rather than deleting provenance. Every committed generation records code, policy, extractor, and cleaner versions.

Hash-partition document tables by document ID into 64 partitions initially; partition exact-key tables by key digest. Unique constraints must include partition keys. [PostgreSQL partitioning](https://www.postgresql.org/docs/18/ddl-partitioning.html)

- **Reject SQLite as authority:** suitable locally, but shared failover and cross-machine coordination would require additional machinery.
- **Reject DuckDB:** use for export analysis, not operational coordination.
- **Reject Parquet/Delta/Iceberg/Lance as authority:** indexed transactional lookups dominate this workload.
- **Reject JSONL plus compaction:** would recreate database indexing, transactions, and recovery.

## 2. Git: control plane only

Keep code, tests, schema migrations, dependency locks, documentation, bounded curated seeds, backend/vendor configuration, eligibility policy, explicit cleaning policy, and `dataset.lock.json`.

Remove machine-generated registry entries, manifests, prune ledger, blocklist, and cursor values. Separate configured backend pauses from runtime exhaustion state.

Stop per-round Git commits and README rewrites. Rounds commit database generations; the six-hour maintainer commits the publication pointer and corresponding summary, alongside reviewed code/policy changes.

**Reject Git LFS, additional shards, and less frequent bulk commits:** all retain dataset growth inside repository infrastructure.

## 3. Publication: immutable Parquet on public S3

Publish metadata-only releases every six hours through the maintainer. Each release comprises a weekly compacted checkpoint plus ordered six-hour deltas, including tombstones, provenance revisions, decisions, cursors, and policy/config copies. Use Zstandard-compressed Parquet, targeting 256 MiB files.

Export from committed generation sequences through the transactional outbox. Weekly compaction replays exports independently of growth. Retain all files referenced by published releases.

`dataset.lock.json` contains:

- Dataset UUID, generation, schema version.
- Immutable release descriptor URL and SHA-256.
- Producer code commit and policy/config digests.

The descriptor lists every checkpoint/delta checksum and row count. Publish it last, verify it, then update Git. A failed upload leaves the previous release valid; retry publication without rolling back growth.

Provide a streaming importer and reproduction command. Record unavailable or drifted upstream documents explicitly: metadata cannot guarantee that upstream bytes remain available.

**Reject Hugging Face as canonical storage:** optional discovery mirror only.  
**Reject GitHub Releases:** unsuitable as the primary continuously growing provenance store.

## 4. Concurrency and recovery: fenced generations

Allow one mutating coordinator globally; parallel workers perform bounded tasks.

Replace filesystem ownership with a database lease: heartbeat every 20 seconds, expire after 120 seconds, increment a fencing epoch on takeover. Every mutation verifies the current epoch under transaction locking. Maintain global per-host download limits across workers.

Checkpoint work into run-scoped staging tables using short transactions. Upload immutable artifacts first. After all required gates pass, one transaction applies staged metadata, decisions, exact keys, cursor changes, counters, outbox records, and the new generation pointer.

Readers see only committed generations. Failed rounds leave cursors unchanged; another machine resumes staged work or aborts it idempotently. Late workers cannot publish. Preserve owned-process termination on cancellation.

Maintainer triage reads a settled generation without holding the lease; action reacquires ownership and revalidates it. Authorized operators connect to the shared service; outsiders import a published release into an independent database.

**Reject local `flock` as global coordination:** it cannot fence another machine.  
**Reject a transaction spanning downloads:** excessive lock duration and recovery exposure.

## 5. Payloads and backups: private S3, persistent originals

Keep raw originals long-term for every document ever admitted to a committed corpus. Preserve existing restricted raw/text provenance. Keep verbatim text for inexpensive recleaning; retain cleaned artifacts referenced by retained generations.

Use logical addresses `(stage, SHA-256)` over uncompressed bytes. Physically pack small artifacts into approximately 256 MiB immutable objects with per-artifact offsets, lengths, codecs, and hashes; large artifacts remain standalone. Compress text/corpus with independent Zstandard frames; avoid recompressing PDFs. Hash pack objects too.

Local disks become bounded caches. `corpus/` becomes an explicit materialized subset; full-scale training streams generation membership and packed artifacts. Never infer eligibility from object listings. Use conditional object creation to prevent overwrites. [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)

Back up objects incrementally to a separate account and region, with independent deletion protection. Archive PostgreSQL WAL continuously, take daily incremental/weekly full backups, retain 35 days of point-in-time recovery, and test restoration monthly. [PostgreSQL recovery](https://www.postgresql.org/docs/current/continuous-archiving.html)

Track replication freshness; target disaster loss below 15 minutes. Garbage-collect unreferenced staging/rejected bytes only after 30 days and reference checks.

**Reject SSD tarballs as primary backup:** repeated full copies and round-lock duration cannot scale.  
**Reject deleting accepted raw bytes:** upstream drift would destroy the reproduction anchor.

## 6. Dedup: transactional exact keys, rebuildable LSH

Store indexed URL/title digests, full normalized values for collision verification, document IDs, and artifact hashes in PostgreSQL. Preserve normalization and curated precedence during migration. Query candidate batches; never load all keys into Python.

Store versioned MinHash signatures against body hashes in PostgreSQL: initially 128 × 64-bit values, approximately **164 GB at 160M documents**. Exclude provenance headers; version normalization, shingling, and seed.

Build LSH postings in RocksDB across 256 movable logical shards, using 32 bands of four values. That implies **5.12 billion postings**: budget disk, not RAM. Feed indexes from the outbox; require their generation watermark before dedup-dependent promotion, and compare candidates within the pending batch.

LSH proposes candidates; verify similarity before decisions. Oversized buckets enter deferred comparison rather than silently truncating results. Deploy in shadow mode until multilingual drop/retain evaluation approves thresholds.

**Reject Redis/in-memory LSH:** unnecessary RAM cost and another durability problem.  
**Reject automatic pruning from band collisions:** collisions are evidence, not decisions.

## 7. Migration: independently shippable stages

1. **Introduce interfaces.** Add paginated iteration, batch membership, targeted upserts, explicit tombstones, and artifact resolution behind `registry.py`. Keep file storage authoritative.  
   **Rollback:** select the legacy implementation.

2. **Build PostgreSQL shadow state.** Import a locked settled snapshot while production resumes; capture subsequent successful-round deltas durably and replay idempotently. Compare canonical row hashes, decisions, restrictions, cursors, and counts.  
   **Rollback:** discard/rebuild shadow state.

3. **Convert every production caller.** Include `rotation.py`, `blocklist.py`, direct prune-ledger appends, stats, contracts, and maintainer recovery—not just `registry.py` consumers. Preserve old list/set APIs temporarily for compatibility. Preserve `write_manifest_rows` replacement semantics; never silently reinterpret it as upsert.  
   **Rollback:** retain legacy callers/backend.

4. **Cut over between rounds.** Drain one round, reconcile shadow state, enable PostgreSQL authority and generation promotion. Stop Git-based round snapshots. Keep verified legacy exports for seven days.  
   **Rollback:** fence writers, export the latest committed generation, switch backend; never restore a stale cutover copy.

5. **Move artifacts incrementally.** Upload and verify hashes, prefer object reads with local fallback, then evict replicated local copies. Introduce streaming training and incremental verification.  
   **Rollback:** restore local cache from immutable objects.

6. **Publish and retire bulk Git state.** Verify an independent release import before removing tracked data. Enable LSH separately in shadow mode.  
   **Rollback:** previous valid release/code; disable near-dup decisions.

Acceptance requires existing behavioral tests, adapted storage-layout contracts, byte-identical cleaning, multilingual/table preservation, eligibility exclusion, failure-injection recovery, stale-writer rejection, and successful backup restoration. At 160M synthetic metadata rows, require bounded memory and a 400-document round’s metadata work below 30 seconds p95 on documented hardware. Verify changed artifacts each round; run resumable background integrity sweeps.

Archive the existing history as a checksummed Git bundle in two locations. Then perform one coordinated history rewrite removing bulk data paths and empty automated commits, publish the commit mapping, replace affected refs, and require fresh clones.

## 8. Accepted risks and exclusions

This requires database operations, object-storage funding, export compaction, and index monitoring. Capacity remains an acceptance test, not a promise inferred from engine choice.

No active-active writers, Kubernetes requirement, vector database, corpus bytes in public exports, or cleaning-policy change accompanies this migration. Pausing patents changes backend configuration only.

---

## Stage 2 implementation record (2026-09-24)

Decided with Codex in the stage-2 design and implementation reviews; recorded here so stages 3–6
build on the same facts.

- **Server.** PostgreSQL 18.6 from conda-forge (`~/miniconda3/envs/nekaise-pg`), run as the
  systemd user service `nekaise-postgres` (linger enabled, so it starts at boot), unix socket only
  (`~/.local/share/nekaise-pg/run`), peer authentication, no TCP listener. Docker needed root on
  this host; the conda build is the same server and its data directory can move into a container
  later unchanged.
- **Backups.** `archive_command` copies every WAL segment to `/media/zengp/ssd/nekaise-pg-wal`
  (atomic, never overwrites; a missing SSD makes the server retain WAL and retry).
  `scripts/pg_backup.py base` writes verified compressed base backups to
  `/media/zengp/ssd/nekaise-pg-base`; `restore-test` restores the newest base plus archived WAL into
  a scratch instance and compares counts and watermark with the live server (first drill
  2026-09-24: identical, `pg_verifybackup` clean).
- **Representation.** Rows are stored as canonical JSON text (`store.canonical_row`), with a
  generated `jsonb` column for predicates only; long lookup values are indexed by sha256 and
  checked against the full value. Both backends reject NaN/Infinity, NUL and non-JSON values, treat
  `1`, `1.0` and `true` as different rows, group aggregates only by string/boolean/null values and
  sum exactly.
- **Temporary ADR exception: advisory lock instead of a lease.** A writer holds a session advisory
  lock and fences every transaction on a writer epoch; all its mutations run on that session, so
  a token dies with the session. This is safe on one host. The lease with heartbeat and fencing
  epoch from section 4 is **required before any second host writes**.
- **Shadow replication.** `scripts/pg_shadow.py` imports a commit and then replays each
  first-parent commit (git objects only, never the working tree) in one transaction that checks
  and advances the watermark and writes a replication receipt. It writes rows verbatim and creates
  no store events, so both stores export byte-identically. `verify` compares order-independent
  per-table multiset digests of git at the watermark with one PostgreSQL snapshot. While
  `workspace/.pg-shadow` exists, `run_round.py` refuses rounds without `--commit`, because the
  shadow only sees commits. First real import (1.62M entries and manifest rows) took 326 s;
  verify 178 s, all tables identical; one dig commit syncs in about 2 s.
- **Still open for stages 3–4.** Converting callers must preserve replacement semantics and
  deletions (the store API has both). A round is not one database transaction: stage 4 stages a
  round's metadata in run-scoped tables while downloads, cleaning and gates run, then promotes it
  in one short transaction that also bumps the generation and writes the publication outbox row.
  Until then a store transaction's atomicity does not make a whole round atomic.

## Stage 3, step 3 record: finder membership (2026-09-24)

- Every `find_*.py` and `crawl_docs.py` dedups through `scripts/dedup.py`: a `Keys` session sends
  candidate pages to `known()` (MAX_KNOWN per call, one read view per batch) and keeps the run's
  own additions and id reservations in small local sets. Membership equals the legacy
  `value in existing_keys()` sets; a value that is not its own normal form is "unknown" without a
  query (the one theoretical gap: a stored URL whose `strip().rstrip("/")` is not idempotent,
  e.g. ending in `"/ /"`). `tests/test_dedup.py` forbids finders from materializing key sets.
- `find_github` reads gh- rows and raw-GitHub blocklist URLs with filtered scans; the file store
  answers an id-prefix scan from the shard files that prefix routes to (routing is linted).
- `find_wiki` origin titles come from a filtered scan in entry-id order (registry file order is
  not a store property), which can reorder its langlinks batches.
- A proposal file is a JSON list of entries, or `{"entries": [...], "github_passes": {...}}`
  when `find_github` staged completed passes; `run_round` records them through the store
  (`control_set("github_passes.json")`, since step 5 inside `<round>.discover.merge`) only for
  successful finders. A standalone `--append` still writes the file directly, like
  `registry.append_entries`.
- Still materializing: `run_round.merge_proposals` and its index warm-up call `existing_keys()`
  (resolved in step 5: both ask the store).

## Stage 3 progress record (2026-09-24)

Plan decided by Codex: convert access, not authority — FileStore stays authoritative, per-round git
commits and the commit-replay shadow continue until stage 4.

- **Done:** legacy manifest ordering (`scan(order="legacy")`, PG schema v3); the round broker
  (`scripts/store_broker.py`: run_round holds `store.writer(round_id=…)`; fetch/prune/clean submit
  batches through it; every child gets verified inherited read access; the pytest gate runs with
  the plain environment); finders ask the store (`scripts/dedup.py` over `known()`, github passes
  staged in proposals and applied by the coordinator); headline statistics (`scripts/corpus_stats.py`)
  for README, round summaries and contracts; `lint_registry` through the store with per-backend
  `validate_layout()` (FileStore: unparsable shards, routing, duplicate registry AND manifest ids).
- **Intentional deviation:** `find_wiki` reads its origin titles in entry-id order (the store does
  not keep registry file order). This can change which titles share a langlinks request and which
  candidates survive `--max`; accepted because file order was never a meaningful priority.
- **Deferred within stage 3:** coverage/coverage_matrix and the cleaner's check/report modes still
  read legacy files; eligibility and host/vendor/backend contracts are still read from files
  outside the view (they must be validated against the view's pinned configuration before
  stage 4); backend-owned ledger and size validation; steps 5–7 (discovery writes and control state,
  cleaner/loader/pruner through the broker, shared recovery and retiring legacy access) — steps 5
  and 6 are done (records below).

## Stage 3, step 5 record: discovery writes and control state (2026-09-24)

Decided by Codex: the coordinator merges successful proposals in backend order and records the
whole discovery phase in one transaction; runtime exhaustion is separated from configuration.

- **One discovery transaction.** After the finders exit and the side-channel protocol checks pass,
  `run_round` opens `broker.local_batch("discover", "merge")` (transaction
  `<round>.discover.merge`, serialized with broker batches) and `apply_discovery` records, in this
  order: the merged accepted entries (`insert_entries`), find_github's staged passes
  (`control_set("github_passes.json")`, only when something new), each successful rotating
  finder's pointer move (`rotation_set`: integer step, weekly walk past skip ranges, or the dynamic
  cursor it reported; holds and failed finders keep theirs), and finder-reported exhaustion
  (`backend_state_set(name, BackendState(False, "exhausted: <reason>"))`). Required/optional
  failure and hold semantics are unchanged (`check_finder_results` runs before the transaction).
  Dedup asks the transaction itself, before it writes, through `dedup.from_view` — the legacy
  `existing_keys()`/`uniquify_ids()` membership and suffixes, answered by the index — so no
  finder or coordinator materializes key sets any more; the index warm-up is a `known()` call.
  A discovery phase that changes nothing (empty successful passes, no rotating finder) records no
  transaction at all. Events and summaries are reported after the commit, in the legacy order.
- **Files a round writes in discovery:** the routed `registry/*.yaml` shards (appended exactly as
  `registry.append_entries` did), `registry/rotation.json`, `registry/github_passes.json`,
  `registry/backend_state.json` (only on exhaustion) and `registry/journal/<UTC day>.jsonl`.
  `registry/backends.json` is never written by a round. Equivalence tests
  (`tests/test_discovery_store.py` against `tests/legacy_discovery.py`, the pre-step-5 path) prove
  byte-identical tracked files apart from the journal and that one exhaustion difference, for
  cross-finder collisions, empty passes, integer/weekly/dynamic cursors, holds, optional failures,
  github passes and exhaustion; the same discovery against PostgreSQL yields the same state.
- **Routed inserts.** A FileStore write view whose entries table is not loaded inserts by reading
  only the shard each id routes to (like `append_entries`; routing is linted every round). Parsing
  every shard instead costs ~127 s and ~2.2 GB at 1.62M entries (measured 2026-09-24). A later full
  load in the same transaction absorbs the routed inserts (and catches a mis-routed clash).
- **Journal per UTC day.** The journal carries whole before/after rows; a round's merge now writes
  hundreds of entry rows, so a monthly file would outgrow a git host's per-file limit. Files are
  `registry/journal/YYYY-MM-DD.jsonl` (older names stay valid: order is by `seq`), the commit
  lookup parses only commit rows naming the run, and `oversized_control_files` covers the journal
  and `registry/*.json`. The whole journal is still read per transaction; stage 4 removes it.
- **Effective enablement.** Selection uses the view: configuration (`backends.json`) AND runtime
  state (`backend_state.json`); an explicit `--backend` still overrides both, as it overrode
  configuration before. `validate_backends` also validates runtime state (unknown backend names,
  malformed values, a disabled state without a reason, and policy reasons, which belong in
  configuration). Contracts: eligibility rules still require the configuration itself to disable a
  restricted backend with a policy-blocked reason (runtime exhaustion never satisfies policy);
  patent-country and suspended-host rules check the effective enablement, i.e. what may run.
- **Standalone writers.** `rotation.py advance|next|show` keep their CLI and output. Every
  converted standalone mutation — `rotation.advance`/`set_next`, `blocklist.add`,
  `migrate_backend_state` — goes through `store_broker.run_batch(st, step, body)`: `body(view,
  batch)` reads a view and records mutations; under a broker (a round's mutating step, or a child
  of the maintainer's window) the view is the inherited read view and the batch is submitted with
  its version (a concurrent change refuses it); otherwise the command takes its own writer for one
  transaction (`<step>-<UTC stamp>-<hex>`), waiting at most 30 s for a running round instead of
  interleaving with it (the old private `rotation` lock did not exclude rounds). Nothing recorded
  means no transaction. Reads (`rotation.load`, `blocklist.load`) stay plain file reads.
- **Maintenance window (Codex review, P2).** `maintainer.maintenance_window` takes the canonical
  round lock as a store writer (`FileStore.writer`), serves a `store_broker.Broker` for the window
  (round id `maint-<UTC stamp>-<phase>`) and exports its environment, next to the inherited read
  entry `ops.named_lock` already exports, to every child it launches, for the window only. So an
  agent's `prune_corpus --apply` (its blocklist), `rotation.py advance` or
  `migrate_backend_state.py` in the action window runs as a store transaction instead of waiting
  on its own coordinator's lock and failing (regression test: real child processes under a real
  window, and the same child without the broker refused).
- **Draining is cancellation-safe (second review, P1).** `Broker.drain()` (run by `serving()` on
  exit, idempotent) stops accepting, cuts connections, waits for an executing transaction and
  removes the socket. On the main thread it swaps the Python handlers of SIGTERM/SIGINT/
  SIGALRM/SIGHUP for one that only records the signal for the whole drain (third review: a
  per-step retry still let a signal delivered between steps escape), then restores every
  original handler and replays the recorded signals, re-raising the first interrupt once the
  broker is drained — so neither the round nor the maintainer releases its writer or locks while
  a transaction runs. Restoration does not rely on a thread mask (fourth review: a signal landing
  on an unblocked worker thread still runs the restored SIGTERM handler on the main thread and
  aborted the remaining restores, leaving later signals swallowed by the recorder):
  `_SignalDeferral.finish()` is a resumable state machine that retries each restore until it is
  recorded as done, consumes each replay before raising it, catches every exception and keeps the
  first, and `drain()` resumes it until done. Off the main thread no handler can run in the
  draining thread (Python runs handlers on the main thread only); brokers are served and drained
  on their owner's thread. Regressions: a signal before and after each drain step, and a SIGTERM
  sent to a live worker thread right after each handler restore. A cut connection loses
  only the reply; the transaction's outcome stands and an identical retry is a no-op.
- **Settled state is judged after draining (second review, P2).** Both maintenance phases drain
  their broker before `update_growth_block()` and before recording the outcome, still under both
  locks: killing a timed-out agent does not stop a batch the parent's broker is executing for it.
- **No nested rounds (second review, P2).** `run_round.py` (including `--recover`) exits 2 at once
  when an ancestor holds the corpus-round lock it would need (a verified inherited entry for that
  lock, or a broker in the environment), naming the read-only gates to validate with instead
  (`clean_corpus.py --check`, `lint_registry.py`, `check_contracts.py`, `pytest tests/`).
  Documented in AGENTS.md and the maintainer's action prompt.
- **Backend health (Codex review, P3).** The maintainer's snapshot reads configuration and runtime
  state through one store view (the window's writer token; files only if no view can open, and
  `state_source` says which), reports backends by effective enablement, lists configured backends
  paused at runtime under `runtime_paused` with their reason, and parses `backend_disabled`
  events (from completed rounds only) to name the round that disabled them.
- **Representation migration (decision).** `scripts/migrate_backend_state.py NAME… --apply` moves a
  named backend's config pause `enabled=false, reason="exhausted: …"` to runtime state with the
  reason verbatim (runtime first, then config `enabled=true` without a reason, under the round lock;
  idempotent, resumable). The `exhausted:` prefix does NOT prove the loop wrote a pause: of the five
  such entries on 2026-09-24 only `find_kitopen` was written by `disable_backend` (dig commit
  2a317cff44); `find_sdz`, `find_boverket`, `find_iea` and `find_scielo` were retired by hand in
  ops commit 8c6c03bd3f and stay configuration, like `find_zenodo` ("exhausted") and `find_ademe`.
  So nothing migrates implicitly and no contract rejects `exhausted:` in configuration: every
  existing pause keeps its effective state and reason. The branch ships the tool but does not run
  it, because a migration transaction journals on the branch while main keeps committing rounds
  (and would duplicate journal sequence numbers on merge). After merging, the maintainer runs
  `python scripts/migrate_backend_state.py find_kitopen --apply` under the drained loop and
  commits `registry/backends.json`, `registry/backend_state.json` and the journal together.
- **Rollback.** A failed round still restores every tracked byte from the round snapshot
  (journal and runtime state included; tested). A failed discovery transaction writes nothing.
- **Stage-4 note (Codex): the discovery transaction is not replayable after commit.**
  `<round>.discover.merge` records requests computed against the state it read (membership, id
  suffixes, cursors); replaying the round after the commit recomputes them against the new state,
  so the request digest differs and the store refuses the run id as "already committed different
  requests". That is fine for today's runner, which rolls a failed round back and restarts it
  under a new round id. Stage-4 staging recovery must either preserve the computed batch as an
  immutable staged artifact and replay exactly it, or explicitly recognize a completed discovery
  step (its commit row) and skip to the next step.

## Stage 3, step 6 record: loader, pruner and cleaner through the store (2026-09-24)

Decided by Codex: one short transaction per logical metadata batch (usually one per step, several
for loader checkpoints and bulk patches), never one across a round or across downloads;
immutable request batches with distinct round/step/batch identities for exact retries; artifact
side effects kept explicitly recoverable.

- **Step sessions.** `store_broker.step_session(st, step)` gives a step its read view and ordered
  batches. Under a broker (a round's mutating step, or a child of the maintainer's window) the
  view is the inherited one and batch `b` runs as `<round>.<step>.<b>`; standalone the step takes
  its own writer (the round lock, `--lock-timeout`, default 30 s) for the whole run and batch `b`
  runs as `<step>-<UTC stamp>-<hex>.<b>`. The first batch expects the view's version, each later
  one the version its predecessor committed. Every read happens before the first batch. Under a
  broker that is not run_round's own round (the maintainer's window serves several commands)
  every invocation adds a random token, `<window>.<step>.i<hex>-<b>`, fixed for the invocation,
  so its batches retry exactly while a second invocation never collides with the first's
  committed identities (Codex review, P2); inside a round (broker round == `NEKAISE_RUN_ID`)
  identities stay deterministic. Policy comes from the view's pinned configuration:
  `store.pinned_policy(view)` returns the validated eligibility restrictions and host policy
  (fail closed), and the cleaner reads its ruleset stamp under its writer (Codex review, P1) —
  an operator editing the files meanwhile cannot change a running step's policy.
  None of the three scripts calls `registry.write_manifest_rows`/`remove_ids`/`append_entries`/
  `load_manifest_rows`/`load_entries`/`load_eligibility`, `host_policy.load`, `blocklist.add` or
  `ops.append_jsonl` any more (`tests/test_pipeline_store.py` guards it with an AST check).
- **Loader.** Reads entries (id order) and the manifest (legacy order: it picks extraction
  templates) through the view. Every 25 recorded results is one transaction `ckpt-NNNN` upserting
  exactly the rows recorded since the previous checkpoint (plus a final one); retry bookkeeping,
  the per-run deferral handoff (`workspace/fetch-deferred.json`), drift reporting and extraction
  reuse are unchanged; `--reextract` patches in bounded `reextract-NNNN` batches; `--verify` only
  reads. **Intentional deviation:** a binding per-run host cap (`HOST_RUN_CAP`) now defers by
  entry id instead of registry file order (the store keeps no file order), like find_wiki.
- **Pruner.** Decisions are computed exactly as before over the legacy manifest order, then ONE
  transaction `apply`: survivor metric updates (`update_manifest_fields`), registry and manifest
  deletions per reason (tombstones `prune: <reason>`), blocklist additions and ledger rows.
  **Bytes:** before the transaction the dropped documents' raw/text/corpus files are *moved* to
  `workspace/prune-quarantine/<transaction>/` (`record.json`: the (document id, path) items and a
  diagnostic state moving → moved → committed). Standalone (and in the maintainer's window) they
  are deleted right after the commit. Otherwise they are settled against a store state by ONE
  rule per file, whatever the recorded state (Codex review, P1): a file goes back when its
  document's manifest row exists in that state and its path is free, else it is deleted. Inside a
  round the quarantine is kept until the round ends: after success it is settled against the
  final state (all pruned rows gone: deleted); on rollback the snapshot is restored, the
  quarantine settled against the restored pre-round state (pre-round documents come back,
  documents new in the round are discarded) and only then is the snapshot discarded — if
  settling fails, `rollback_failed` is reported and the snapshot kept. `run_round --recover` does
  the same under a *recovering* writer (`FileStore.writer(round_id=…, recovering=True)`, which
  may read while the round's snapshot exists), returns 1 and keeps the snapshot until settling
  finishes. Leftovers of a killed standalone prune are settled by the next round before it
  fetches and by the next prune before it decides. Tests drive the real entrypoints (rollback and
  `--recover`) with a child prune killed while moving, after moving, after committing, and
  after a later step failed, with a mix of pre-round and new-in-round documents.
- **Cleaner.** corpus/ files are written first, outside transactions; then only the corpus fields
  that changed are patched (`meta-NNNN`) and restricted rows' corpus fields unset
  (`restricted-NNNN`), in batches of at most 20 000 rows in manifest shard order; the ruleset
  stamp is published only after the last batch committed (IN-PROGRESS until then, so a crash
  makes the next run rebuild and re-patch what did not commit; an unchanged ruleset reproduces
  the same bytes, so an incremental run with nothing new commits no transaction).
- **Routed manifest writes (performance).** A FileStore write view no longer loads the whole
  manifest (~30-40 s, several GB) for targeted mutations: `upsert_manifest`,
  `update_manifest_fields` and `delete_manifest` (and read views' `get_manifest` /
  `resolve_artifact`) read only the shards their ids route to (`registry.manifest_shard`), and
  `delete_entries` only the routed registry shards (like step 5's inserts). A shard is read as
  text and parsed only where asked: rows are found by their canonical `"id": <json>` pair, and a
  change is spliced in (changed lines removed, new lines inserted at their (topic, id) position by
  binary search, every other line kept byte for byte), exactly the text a full re-render
  produces; changes above 1/32 of a shard re-render it. `validate_layout` (lint, every round) now
  checks what this relies on: every manifest row routed to its file, canonical, and in (topic, id)
  order (+11 s on 164 s). Scans, aggregates, membership and `replace_manifest` still load the whole
  table (documented), absorbing routed changes made earlier in the transaction. Routed registry
  shards work the same way (`_RegistryShard`): entries are located by the `  - id:` walk
  `remove_ids` uses, only the affected entry blocks are parsed (the before-images), removals cut
  those blocks and appends are parsed back — instead of parsing whole shards (0.8 s each, three
  times per shard in the first cut: 268 s for a 400-document prune over 52 shards).
- **Journal: versioned compact events (Codex decision, review P1).** `store.journal_events` is the
  one event contract both backends emit. Version 2 events (`"v": 2`) are (a) a tombstone per
  deleted row — table, id, reason and `before_sha256` (sha256 of the deleted row's canonical
  JSON) — and (b) one receipt per transaction, the commit row: run id, timestamp, request digest
  (the replay identity, unchanged) and `counts` of operations per table and op. Inserted and
  updated rows are no longer imaged (the tables and the prune ledger hold them; git keeps the
  history of the tracked files until stage 4). Version 1 events already committed (whole
  before/after rows, no `"v"`) stay valid and are read back unchanged; sequence numbers continue
  across both. The PG shadow still copies journal files verbatim and verify still compares full
  exports including events; PgStore emits byte-identical v2 events. Files still roll by size
  within a UTC day (`<day>.jsonl`, `<day>.001.jsonl`, … at 32 MiB); the commit lookup caches each
  file's commit rows by file identity, and the last sequence number is read from file tails.
  Measured on the real-data copy: a typical round's metadata (16 loader checkpoints, a
  100-document prune, 400 cleaner patches: 18 transactions) journals 73 KB (v1: several MB); a full
  re-clean (1.62M patched rows, 81 transactions, 234 s) adds 27 KB (v1: 4.27 GB).
- **Measured** on a copy of the committed data (1,620,815 manifest rows, 2.0 GB; 1.62M entries):
  a 25-row loader checkpoint touching 25 shards (~550 MB) commits in ~3 s (it took 14 s with
  whole-shard parsing and ~30-40 s as a whole-manifest rewrite; the first transaction of a process
  adds ~3 s to index the commit rows of a 4.3 GB journal, later ones use the cache); a 400-document
  prune over 52 manifest + 52 registry shards applies in 14.6 s including its ledger-evidence scan
  (268 s with whole-shard YAML parsing); a full re-clean patching every row takes 81
  transactions of 20 000 rows, 234 s with v2 events (324 s and 4.27 GB of journal with v1). Reading
  through a view costs more than the legacy readers: entries 143 s vs 130 s, the manifest in legacy
  order 54 s vs 26 s (sort + per-row copies); the loader no longer rewrites the whole manifest per
  checkpoint, which outweighs it. Lint (`validate_layout`) 175 s vs 164 s.
- **Equivalence and recovery tests** (`tests/test_pipeline_store.py` against
  `tests/legacy_pipeline.py`, the step-5 code): identical registry, manifest, blocklist and ledger
  bytes (journal aside) and identical raw/text/corpus artifacts and hashes for the loader
  (checkpoints, reuse, drift, retries, handoff, `--reextract`), the pruner (every drop reason,
  DNS evidence, protected rows, survivor metrics) and the cleaner (pass-through and a ruleset
  change, restricted rows, orphans, incremental re-runs); a failed checkpoint, a failed metadata
  batch, a failed prune transaction and a prune killed after its commit each leave a consistent
  state that the next run completes to the uninterrupted result; a real child prune under a round
  broker; the same steps against PostgreSQL leave the same tables and artifacts.
- **Accepted by Codex:** the id-order host cap is an explicit compatibility exception; the slower
  full-view reads (entries +13 s, manifest +28 s per step) are accepted performance debt.
- **Not done:** optional local (untracked, bounded) diagnostic journals with full row images.
