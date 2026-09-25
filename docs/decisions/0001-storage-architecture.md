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
  cleaner/loader/pruner through the broker, shared recovery and retiring legacy access) — steps 5,
  6 and 7 are done (records below; step 7 closes stage 3). Resolved since: coverage,
  coverage_matrix and the cleaner read through views; eligibility and host policy come only from
  the view's pinned configuration (step 7 review fixes).

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
  finishes. Both paths first resolve interrupted store transactions (finalize committed, roll
  back prepared) and only then restore the snapshot (second review, P1): a FileStore rollback
  restores a file only while it holds the transaction's pre- or post-image, which the restored
  pre-round shard does not once an earlier checkpoint changed it. Quarantine records of the first
  step-6 format (`ids` + `files`) are settled by attributing each file to the id its name
  carries; a record that is unrecognized, unattributable or missing while files exist raises and
  is kept (second review, P1). Leftovers of a killed standalone prune are settled by the next round before it
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

## Stage 3, step 7 record: shared recovery, legacy access retired (2026-09-24)

Decided by Codex: share recovery between the maintainer and the runner; keep registry.py's
list/set APIs as deprecated store adapters (write_manifest_rows with replacement semantics);
extract the shared codecs; allow direct state-file access only inside the store, physical
validators and git-shadow/import/export tooling, enforced by an architectural test.

- **One recovery routine** (`scripts/round_recovery.py`, `recover_round(st, writer, run_id, …)`),
  used by run_round's failure rollback, `run_round --recover` and
  `maintainer.recover_pending_round`, always under the round lock the caller holds (the round's
  writer, the recovering writer, or the maintenance window's writer scoped to the round with the
  new `FileStore.recovering(writer, run_id)`). Order: (1) stop the round's processes — the
  runner's own descendants started during the round, plus every live process of this user whose
  environment carries `NEKAISE_RUN_ID=<run id>` (orphans of a killed runner: a fetch still
  downloading, a prune still moving bytes); SIGTERM, SIGKILL after 2 s, fail if any survives;
  (2) unstage the tracked paths (`git reset -q -- <paths>`, which, unlike the former
  `git restore --staged`, does not abort on a path git does not know); (3) resolve pending store
  transactions; (4) detect an already-committed round BEFORE touching tracked state: a
  first-parent commit within 200 of HEAD whose message has the round's `Corpus run: <run id>`
  trailer. A committed round stands: its snapshot is discarded, never restored — and if its
  tracked files changed since that commit, nothing is changed and the snapshot is kept for an
  operator. The runner also passes what it knows (`known_committed`): a commit it made but git
  cannot show refuses recovery. Otherwise the snapshot is restored; (5) settle the round's prune
  quarantine against the state now served; (6) discard the snapshot last. Any failure raises and
  keeps the snapshot (run_round: `rollback_failed` / `recover_failed`, exit 1; maintainer: the
  recovery error goes to triage). Events: `round_processes_stopped`,
  `store_transaction_recovered`, `round_already_committed`, `prune_quarantine_settled`, then the
  entrypoint's `state_rolled_back` / `committed_round_kept` / `run_recovered` (with `committed`
  when kept). A push failure after the commit now runs the same routine (the commit is detected
  and kept, the quarantine settled at once) instead of a bare discard. The run ledger is not
  used as commit evidence (it is per-checkout and git-ignored); a round that completed without
  `--commit` and died before discarding its snapshot is restored, the conservative choice.
  The maintainer's recovery now also resolves store transactions and settles quarantines, which
  it did not before.
- **Codecs** (`scripts/state_codec.py`, no I/O): routing (`shard_filename`, `manifest_shard`,
  `prune_ledger_name`, SHARDS/HASH_BUCKETS), normalization (`norm`, `normalize_url`, `slug`,
  `uniquify_ids`), YAML shard text (`parse_yaml` with the C loader, `emit_entry`, `shard_header`,
  `remove_ids_from_text`), `manifest_shard_text`, and the eligibility schema. `store.py` imports
  it instead of `registry`/`blocklist`; `pg_shadow`, `corpus_index`, `store_broker` too. The
  import graph is acyclic (`registry → store → state_codec`, `blocklist → store`), so
  `blocklist.add`'s lazy `import store` workaround is gone. Byte formats are unchanged.
- **Deprecated adapters** (`registry.py`): `load_entries` (id order — the store keeps no file
  order), `load_manifest_rows` (legacy order), `write_manifest_rows(rows, *, reason=…)` = ONE
  transaction `replace_manifest(rows, reason)` (rows left out are tombstoned with the reason),
  `remove_ids` = `delete_entries`, `load_prune_ledger_rows` (ledger scan order), `existing_keys`
  (view scans) — each one view or one `store_broker.run_batch` transaction, emitting a
  `DeprecationWarning`. `append_entries` keeps its proposal staging for finders; standalone
  `--append` is one `insert_entries` transaction (an existing id is now refused rather than
  appended twice). Removed: `REG_DIR`, `MAN_DIR`, `shard_path`, `shard_files`, `manifest_files`,
  `prune_ledger_path`, `prune_ledger_files`, `write_prune_ledger_rows` (the retired monolith
  migration; the store still reads `pruned.jsonl` as input). No production caller uses the
  read/rewrite adapters. The pre-step-7 file implementations live on as the equivalence
  reference in `tests/legacy_registry.py` (legacy_pipeline, legacy_discovery, test_filestore,
  test_dedup compare against them).
- **Remaining direct readers converted.** `blocklist.load()` and `rotation.load()` are unfenced
  store reads (`FileStore.peek(table)`, small tables only: rotation, blocklist, backend_state,
  control — no lock, may observe a round in flight, never a basis for a mutation; PgStore reads a
  snapshot); `blocklist.add` decides newness inside its transaction; find_github's standalone
  pass recording is one `control_set` transaction; the maintainer's no-view fallback uses
  `peek("backend_state")` and `config_documents()`. Configuration stays git-owned policy (ADR:
  "Git owns code and policy"): its loaders locate files only through `store.config_path(name)`,
  which accepts CONFIG_FILES only. `store.TRACKED_PATHS` names the tracked layout for the round
  snapshot/commit (run_round) and backups (backup_corpus).
- **Architectural test** (`tests/test_architecture.py`): every `scripts/*.py` is parsed; outside
  the allowlist it fails on a tracked-state path in code position (path joins, Path/open/glob/
  copy/unlink calls, path constants; docstrings and f-string messages ignored), a glob of
  `*.yaml`/`*.jsonl`/`pruned-*.jsonl`, the removed legacy names or deprecated adapters
  (`registry.load_*`/`write_*`/`existing_keys`/`remove_ids`/…, `blocklist.PATH`,
  `rotation.PATH`), a store's private layout attributes (`.reg`, `.man`, `.journal_dir`, …), or
  an import of `legacy_registry`. Allowlist (each entry must still be needed —
  `test_allowlist_is_minimal`): `store.py` (FileStore internals), `pg_shadow.py` (git-shadow
  import/replay/verify from git objects), `corpus_index.py` (the FileStore's rebuildable SQLite
  membership index), and two physical validators in `check_contracts.py`
  (`oversized_control_files`, `prune_ledger_contract_errors`). `store_pg.py`, `backup_corpus.py`,
  `migrate_backend_state.py`, `lint_registry.py` and `run_round.py` needed no exemption once they
  used `config_path`/`TRACKED_PATHS`. A self-test proves the detector sees each kind of access.
- **Tests**: recovery through every entrypoint (rollback, `--recover`, maintainer) for the
  quarantine and mid-commit crash cases, and in real git repositories: an already-committed round
  is kept (not restored) by `--recover` and the maintainer; a committed round changed since is
  left alone with its snapshot; a claimed commit without its trailer is refused; an uncommitted
  round is restored and unstaged; the round's orphaned processes are stopped and other rounds'
  are not; a push failure after the commit keeps the commit; a failed uncommitted round is rolled
  back by the same routine.
- **Release gate evidence**: full suite 947 passed / 21 skipped (PG skipped), with PostgreSQL
  (`NEKAISE_PG_TEST_DSN`) 998 passed; `py_compile scripts/*.py` clean; `lint_registry.py` and
  `check_contracts.py` OK on the branch's real data (lint: 1,620,820 entries in 115 shards, 1,620,815 manifest rows, no problems, 229 s;
  contracts: 1,612,754 documents / 30 backends, 67 s). End to end in a throwaway git
  repository (this branch's code, a synthetic 11-entry registry, the branch's configuration),
  inside `unshare -rn` (loopback only; payloads from a local HTTP server), with a PG shadow
  (schema `e2e_step7` of `nekaise_test`) imported and enabled: (1) `run_round.py --skip-discovery
  --commit` fetched 6 documents, pruned the thin and the missing one, cleaned, ran every gate
  (check, index, lint, contracts, the full pytest suite) and committed; `pg_shadow sync` + `verify`
  OK. (2) Two new entries, then a round with an injected failing test gate: `state_rolled_back`,
  HEAD unchanged, clean tree. (3) A round with an 8 s-per-request payload server was SIGKILLed one
  second into its fetch; its two fetch processes lived on as orphans; `run_round.py --recover
  latest` stopped both (`round_processes_stopped`), resolved the store and restored the snapshot:
  HEAD unchanged, clean tree. (4) A normal round then committed the two new documents, and
  `pg_shadow sync` + `verify` were OK again. The equivalence tests (tests/legacy_registry.py as
  the reference) show byte-identical registry, manifest, blocklist and ledger files; only journal
  and runtime-state metadata differ, as before.

### Step 7, Codex review fixes (2026-09-25; verdict MERGE AFTER FIXES)

- **P1 — a git inspection failure never leads to a restore.** `round_recovery.repo_state(root)`
  answers "none" only when no repository encloses the root (found by looking for `.git` without
  asking git, stopping at a filesystem boundary like git; an empty stray `.git` directory does
  not count, a gitfile or any directory with HEAD/objects/refs/config does), "unborn" only when
  HEAD is a symbolic ref to a branch in a repository with no refs at all, and "head" when
  `HEAD^{commit}` resolves. Everything else — `rev-parse`, `symbolic-ref`, `for-each-ref` or
  `log` failing, a garbage or dangling HEAD, a missing branch while other refs exist — raises
  `RecoveryError` before anything is unstaged or restored, and the snapshot is kept. Regression
  tests: each git failure mode through `--recover` and the maintainer, three corrupt-HEAD
  variants, the legitimate unborn repository (restores and unstages) and no repository.
- **P2 — deletions count as changes.** The committed-state guard diffs EVERY snapshot path
  against the round's commit (git diff reports a deleted tracked path); previously missing paths
  were filtered out, so a deleted `pruned_urls.txt` let recovery discard the snapshot. Regression
  test through both entrypoints.
- **Policy pinning finished (step 4's deferral closed).** Eligibility restrictions and host
  fetch policy reach production code only as `store.pinned_policy(view)` — validated, failing
  closed, from the same view as the data: `run_round.doc_stats`, `update_readme_stats` (it
  loaded eligibility BEFORE acquiring its view, so after waiting behind a writer it could pair
  old restrictions with new state), `coverage`, `coverage_matrix`, `lint_registry` (after the
  physical layout check, inside its view), `check_contracts` (restrictions and host policy from
  its one view; `host_policy_contract_errors(backends, policy)`; invalid pinned policy is a
  contract failure), `crawl_docs` (`pinned_restrictions()` from a dedup read view),
  `corpus_stats.compute`'s default (was the unvalidated `ConfigSnapshot.eligibility`, failing
  open to no restrictions) and `corpus_stats.local_unavailable(view, root, restrictions,
  policy)`. Removed working-tree loaders: `registry.load_eligibility`,
  `registry.load_host_policy`, `host_policy.load`/`PATH`; `registry.is_fetchable` and
  `locally_unavailable_rows` now require the policy argument (no hidden file read). The
  architectural test gained a policy rule: outside `store.py` (`CONFIG_FILES`, `ConfigSnapshot`,
  `pinned_policy`) no module may name `eligibility.json`/`host_policy.json` in code, read
  `.eligibility`, or call those loaders (a self-test covers each form). Other configuration
  (`backends.json`, `vendors.json`) stays read through `view.config_get()` or `store.config_path`.
  `tests/test_policy_pinning.py` pins a policy different from the working tree and shows every
  tool follows the pinned one, and that invalid pinned policy fails lint, contracts and
  `doc_stats` closed.
- **Gates re-run**: full suite 1034 passed / 21 skipped, with PostgreSQL 1085 passed;
  `py_compile` clean; `lint_registry` (1,620,820 entries, 1,620,815 manifest rows, no problems)
  and `check_contracts` (1,612,754 documents / 30 backends) OK on the branch's data; the
  throwaway end-to-end run repeated steps (1)–(4) above with the same results and added (5): a
  round that committed and then failed its push (no remote) ran the shared routine, which found
  the commit (`round_already_committed`, `committed_round_kept`), kept it and left a clean tree;
  `pg_shadow sync` + `verify` OK after every commit.

### Step 7, Codex second review (2026-09-25): P2 and policy pinning fixed; P1 closed

- **Discovery is fail-closed.** `_enclosing_git` establishes absence only positively: each
  level is stat'ed and only ENOENT means "not here"; any other error (EIO, EACCES, a vanished
  directory, a `.git` that is neither directory nor file) raises `RecoveryError`, as does a
  failure listing loose refs or reading packed-refs.
- **"unborn" is the exact unborn outcome only**: `rev-parse --verify -q HEAD^{commit}` exits 1
  with no stdout and no stderr, `symbolic-ref -q HEAD` names a branch with no stderr,
  `for-each-ref` exits 0 silently with no ref, `.git` is a directory, and neither loose refs
  nor packed-refs (header lines aside) hold a ref. Any other exit code, output or stderr raises.
- **Regressions**: EIO injected into discovery's stat (recovery fails, committed state and
  snapshot kept); rev-parse exit 128 with and without a message, exit 1 with stderr, exit 1 with
  output — each followed by a symbolic branch and an empty ref listing — all raise and keep the
  snapshot; an unborn-looking git answer while refs exist on disk raises; a genuine unborn
  repository (also with a header-only packed-refs) still answers "unborn".
- **Gates**: full suite 1041 passed / 21 skipped, with PostgreSQL 1092 passed; `py_compile`
  clean; the throwaway end-to-end run (steps 1–5) passed again, `pg_shadow verify` OK after
  every commit.

## Stage 3 complete — what stage 4 needs

Stage 3 converted access, not authority: every production reader and writer of tracked state goes
through the store (FileStore still authoritative, per-round git commits and the commit-replay
shadow continue). Stage 4 (cut over between rounds) needs:

- **Run-scoped staging and generation promotion** in PostgreSQL: a round's metadata in staging
  tables while downloads/cleaning/gates run, promoted in one short transaction that bumps the
  generation and writes the outbox row; the discovery transaction must become replayable (keep
  the computed batch as an immutable staged artifact, or recognize a completed step by its
  commit row — step-5 note).
- **Recovery without git snapshots**: `round_recovery` restores a git-era snapshot; under PG
  authority recovery becomes "abort or resume the staged run" and "already committed" becomes
  "its generation is promoted" (the same order: stop processes, resolve staging, detect
  promotion, settle quarantine). The committed-round check by commit trailer goes away with the
  per-round commits.
- **The lease with heartbeat and fencing epoch** (section 4) before any second host writes; the
  advisory-lock writer remains a one-host exception.
- **Remove the adapters and the file backend's special paths**: `registry.py`'s deprecated
  adapters, `FileStore.peek`, `corpus_index`, the journal-in-git (every transaction still reads
  the whole journal for commit lookup), and the per-round `git add`/commit of tracked state; keep
  verified legacy exports for seven days.
- **Policy stays pinned**: eligibility and host policy are read only through
  `store.pinned_policy(view)` (done in stage 3, enforced by the architectural test); under
  PostgreSQL authority the pinned configuration must be the one promoted with the generation.
- **Accepted debt carried forward**: whole-view reads (entries +13 s, manifest +28 s per step),
  whole-manifest `replace_manifest`, and the FileStore's in-memory tables; PostgreSQL removes them.

## Stage 4 plan (Codex, 2026-09-25)

Decided by Codex: ship stage 4 in seven increments. Keep the single-host advisory-lock exception;
PostgreSQL promotion becomes the commit boundary; public provenance releases and history
rewriting stay in stage 6.

1. **Authority and generation contracts.** Dataset UUID, authority mode, runs, generations, batch
   receipts, immutable revisions, per-consumer outbox acknowledgements; canonical JSON text and
   imported events preserved; producer commit, exact configuration bytes/digests, extractor
   version and cleaning policy per generation. Runner, recovery, maintainer and diagnostics open
   their store through `store.open()`; a host-wide authority record makes missing environment
   settings, old writers and shadow replay fail closed after cutover; never fall back on a PG
   failure. Rollback: disable the new path (FileStore stays authoritative throughout).
2. **Staging with constant-size promotion.** Indexed run-scoped versioned tables, invisible to
   ordinary readers; each batch atomically stores its immutable request, digest, receipt,
   revisions and run overlay (discovery, loader checkpoints, prune decisions/tombstones, cleaner
   patches, exact keys, cursors, runtime state). Ordinary views pin committed generation G;
   pipeline children read G plus their run's overlay pinned at a staging sequence. Drain, freeze,
   bind gate results to the frozen sequence; promotion checks ownership, parent G, frozen digest
   and gates and, in one short transaction, marks the staged revisions committed and records G+1,
   counters and outbox references — never copying staged rows. The computed
   `<round>.discover.merge` request is persisted before application and replayed exactly (or its
   completed receipt skipped).
3. **Local artifacts compatible with atomic metadata.** Immutable local artifact versions for
   changed bytes, written and fsynced before their references are staged; pruning changes
   membership but keeps admitted originals and retained-generation artifacts; payloads stay
   local; artifacts resolve through generation membership; `corpus/` becomes a generation-stamped
   materialization; the global ruleset stamp stops being policy authority.
4. **Recovery and operational review.** Shared recovery over durable run status (promoted runs
   stand; unpromoted default to abort; explicit resume only with unchanged parent/config/code);
   maintainer repairs use staged promotion; review becomes generation-range review with persisted
   verdicts and a contiguous reviewed watermark; repairs are compensating generations.
5. **Verification, backups and the rehearsal.** Generation-bound counters and ledger/derived-key/
   eligibility/artifact checks; named-recovery-point restore drills; metadata RPO ≤ 15 min and
   RTO ≤ 60 min demonstrated; alerts; a full throwaway PG-authoritative rehearsal including
   rollback, and the 160M-row benchmark.
6. **Production cutover between rounds.** Pause, lock in fixed order, drain/recover, pin commit C,
   final shadow sync + verify, baseline generation and verified export; disable shadow timers and
   reject replay through authority mode; `NEKAISE_STORE=postgres` + matching DSN/schema for every
   entrypoint; one supervised round. Stop data snapshots, per-round commits, journal files and
   README rewrites. Rollback: export the latest promoted generation into a fresh legacy layout
   (a sharded FileStore materializer) and switch authority back.
7. **Close the fallback window after seven healthy days**, then retire the production file
   adapters (keeping explicit export/recovery tooling).

Fix now for stages 5–6: stable identities, immutable revision/tombstone schemas, `(stage, hash)`
artifact identities separate from locators, generation retention, independent review/
publication/index watermarks, retained unpublished outbox history; heartbeat leases before any
second host writes.

## Stage 4, step 1 record: authority and generation contracts (2026-09-25)

- **Host authority record** (`scripts/store_authority.py`). One JSON file per host,
  `~/.config/nekaise/store-authority.json`, located through the passwd database (not `$HOME`, so
  an entrypoint's environment cannot hide it), one entry per resolved data root: mode `file` |
  `postgres`, a growing epoch, and for PostgreSQL the dataset UUID, DSN and schema. Written
  atomically under a lock. `store.open()` consults it before choosing a backend and
  `FileStore(...)` checks it on construction. Switching a root to `postgres` first writes
  `<root>/workspace/.store-authority-fence` (UUID + epoch); FileStore refuses a fenced root unless
  the record says `file` at a newer epoch, and no record may be written at or below the fence's
  epoch, so a lost or deleted host record fails closed. An unreadable, corrupt or unknown-format
  record raises. CLI: `show`, `init-file [--dsn --schema]` (explicit file authority, optionally
  binding the shadow's dataset UUID). Switching to `postgres` is step 6's job
  (`PgStore.set_authority` + `store_authority.write_record`, used by the tests).
- **Database half.** Schema v4's `dataset` row holds the UUID (generated once, immutable), the
  authority mode/epoch/root (each epoch appended to `authority_log`; a trigger refuses an
  unlogged or same-epoch change) and the current promoted generation. A PgStore from
  `store.open()` is bound: under a `postgres` record the schema must say `postgres` with the
  record's UUID and epoch — checked when opened and again inside every write transaction (and
  `contracts()`), so a writer opened before an authority change is fenced; for an unbound root
  the schema must NOT be PostgreSQL-authoritative. `pg_shadow import`/`sync` check the dataset
  row inside every replay transaction (refused once it says `postgres`) and the host record
  before any work (refused under `postgres`; a file record that binds a shadow must name this
  schema, DSN and dataset UUID); `enable` refuses under `postgres`.
- **Fail-closed matrix** (`tests/test_store_authority.py`, `tests/test_store_pg_contracts.py`):

  | record \ environment | nothing or `file` | `postgres` + DSN (+ schema) |
  |---|---|---|
  | none (tests, worktrees, the live checkout today) | FileStore, unchanged | PgStore; refused if that schema is authoritative |
  | `file` | FileStore | AuthorityError: PgStore is never a production writer |
  | `postgres` | AuthorityError (missing settings) | PgStore only if DSN and schema equal the record and the schema's UUID/mode/epoch match; otherwise AuthorityError |

  Also refused: FileStore on a `postgres` or fenced root; shadow replay into an authoritative
  schema; a PG connection failure raises (no fallback); `run_round` (rounds, `--recover`), the
  maintainer's window/recovery/backend health and `backup_corpus` under any non-file authority —
  they are still the legacy file path (`store_authority.require_file_authority` /
  `require_file_mode`).
- **Old clients.** v3 code refuses schema v4 at construction ("version 4, code expects 3"), and a
  v3 instance built before the migration cannot take the writer. v4 code now also checks the
  schema version inside every write transaction (`PgStore._fence`), so a later migration fences
  writers mid-session. Pre-step-1 FileStore code cannot read the record; it exists only in
  pre-deploy checkouts (open question 2).
- **Schema v4** (`store_pg.V4_DDL`; migration 4 only adds tables — no row, event or column is
  rewritten; created with a fresh schema, never re-run on every open). The contracts are
  triggers, so no client — old, new or ad hoc SQL — can break them:
  `dataset`, `authority_log`; `config_blobs` (exact bytes, sha256-checked) + `config_sets` /
  `config_set_members` (digest = `store._digest({name: sha256})`); `runs` (kind, parent
  generation, authority/writer epoch, producer commit, config set, extractor version, cleaning
  ruleset; `open → frozen → promoted | aborted`; identity immutable; `staged_seq` grows only while
  open; freezing binds `frozen_seq = staged_seq` and a digest; `promoted` exactly when its
  generation exists); `batches` (identity `(run, step, batch)`, immutable canonical request text
  and digest, `requested → applied(seq) | abandoned`, only in an open run); `revisions`
  (immutable `put`/`tombstone` rows keyed by the stable identity `(tbl, key)`: row text + sha256,
  the superseded row's digest, the tombstone reason, the batch that staged them; deletable only
  for aborted runs); `generations` (a linear chain whose parent is the dataset's current
  generation, bound to a frozen run and copying its provenance; immutable) and
  `generation_retention` pins; `outbox` (gap-free, one reference row per generation, immutable,
  deletable only once every consumer is past it), `outbox_consumers` (`review`, `publication`,
  `index`; a watermark advances only over acknowledged rows) and `outbox_acks` (immutable
  verdicts `ok`/`finding`/`integrity`); `artifacts` keyed `(stage, sha256)` with an immutable
  size, apart from `artifact_locators` (local path, pack offset/length, object key).
  `PgStore.contracts(writer)` gives fenced exact-retry helpers: config sets, `open_run`,
  `request_batch` (same identity and digest returns the receipt, another digest raises),
  `register_artifact`, `ack`/`advance`.
- **How step 2 uses it without copying rows.** The existing tables (entries, manifest, …) become
  the materialized projection of a generation P. A batch stores its request (`requested`) before
  applying; applying is one transaction that marks it `applied` at the run's next staging
  sequence, inserts its revisions and bumps `runs.staged_seq`. A child pinned at (G, run, k) reads
  the projection, revisions of runs promoted in (P, G], and its own run's revisions with
  `batch_seq ≤ k` (unique index `(run_id, tbl, key, batch_seq)`; history index `(tbl, key,
  rev_id)`). Promotion writes a constant number of rows — freeze the run, insert generation G+1,
  mark the run promoted, advance `dataset.current_generation`, insert the outbox row (exercised by
  `test_a_run_stages_batches_and_promotes_without_copying`): a revision's visibility is its run's
  `promoted_generation`, so no revision is rewritten. Folding promoted revisions into the
  projection happens afterwards in bounded batches (a projection consumer); it changes where rows
  live, not what any generation contains. Discovery recovery replays the persisted
  `<round>.discover.merge` request text exactly, or skips it when its receipt is `applied`.
- **Entrypoints rerouted.** `run_round` (rounds, `--recover`, the nested-round check) and
  `maintainer` (window, pending-round recovery, backend health including its fallback read) open
  their store through `store.open()` and refuse non-file authority; `round_recovery` gets that
  store from them; `backup_corpus` calls `require_file_mode`; `update_readme_stats`,
  `check_contracts`, `lint_registry`, `coverage`, `coverage_matrix`, `clean_corpus --check`,
  dedup, rotation and blocklist already used `store.open()`; `pg_shadow` checks authority as
  above. New architectural test: `FileStore(...)` is constructed only in `store.py`,
  `PgStore(...)` only in `store.py`, `pg_shadow.py` and `store_authority.py` (minimal allowlist,
  detector self-test). With no record or a `file` record every entrypoint gets exactly today's
  FileStore.
- **Tests.** Selection matrix (17 record × environment cases), fence, corrupt records, epochs,
  per-root records, entrypoint refusals (run_round, maintainer, backup, shadow), architecture.
  PostgreSQL: v3 → v4 in-place migration of an imported and synced shadow (every row, derived
  column, event, receipt and the watermark identical; `pg_shadow verify` OK; sync continues); the
  real stage-3 code (`git show b188e930bd:scripts/store_pg.py`) writing a v3 schema that v4
  migrates with byte-identical exports; old-client rejection; newer-schema rejection inside
  transactions; bound, unbound and mismatched authority; an authority change fencing an open
  writer; PG failure without fallback; replay refusal after cutover; shadow binding; every
  contract trigger. conftest gives each test a private empty host record and clears
  `NEKAISE_STORE`/`NEKAISE_PG_DSN`/`NEKAISE_PG_SCHEMA`.
- **Gates.** Full suite 1143 passed / 37 skipped (PG skipped), with PostgreSQL
  (`NEKAISE_PG_TEST_DSN`) 1210 passed; `py_compile scripts/*.py` clean; on the branch's real data
  `lint_registry` OK (1,620,820 entries in 115 shards, 1,620,815 manifest rows, 224 s) and
  `check_contracts` OK (1,612,754 documents / 30 backends, 64 s).
- **Deployment.** After the merge the shadow cron's next `pg_shadow sync` migrates the live schema
  3 → 4 under the writer lock (new tables only); code older than this step is then refused, as
  intended. The live checkout has no host record, i.e. today's selection.
- **Open questions (for Codex).** (1) Generation 0 at cutover: record the projection once as the
  baseline run's revisions (≈3.2M rows, off the round path) so every generation is
  reconstructible from revisions alone, or anchor generation 0 on the projection plus a verified
  export? (2) Pre-step-1 FileStore writers cannot read the record: should step 6 also make the
  legacy tracked directories read-only so such a writer fails with EACCES? (3) Should the
  maintainer now run `store_authority.py init-file --dsn … --schema nekaise` on the live checkout,
  making file authority explicit and binding the shadow's UUID (`NEKAISE_STORE=postgres` against
  the live root is then refused)? (4) Gate receipts bound to `(run, frozen_seq, frozen_digest)`
  and a `projection` outbox consumer are left to step 2. (5) `backup_corpus` fails closed under
  PostgreSQL authority until step 5 replaces it.

### Step 1, Codex review fixes (2026-09-25; verdict MERGE AFTER FIXES)

- **P1 — authority is re-checked under the lock, not only at construction.** `FileStore.writer()`
  re-checks the host record and fence right after taking the canonical lock, `_check_writer()`
  (every transaction's start and its pre-commit check, recovery, writer views) re-checks it, and
  locked/inherited read views check it too; so a FileStore constructed before the cutover can
  neither take a writer, open or commit a transaction, nor serve a view afterwards.
  `backup_corpus` checks authority inside the corpus-round lock (`locked_backup`). Regressions:
  store constructed then authority flipped → `writer()`, `transaction()` and `read()` refuse; a
  transaction open at the cutover does not commit (no file written); the backup refuses under
  the lock.
- **P1 — nothing but verified rollback lifts a fence.** `write_record(root, "file")` over an
  existing fence is refused unless `lift_fence=True` (reserved for step 6's verified rollback
  tooling). `init-file` refuses when a fence exists (the lost-host-record case) and, with
  `--dsn/--schema`, when the database's dataset row says `postgres`; it opens the schema with
  `create=False`, so it never creates or migrates one. Regressions: both refusals, the
  non-existent schema, and a successful binding of a file-mode shadow.
- **P1 — outbox sequences are never reused.** `outbox_state (allocated, compacted)` is a durable
  allocation high-water mark and compacted-prefix boundary, maintained only by the outbox
  trigger (direct updates refused; both only grow): a new row must take `allocated + 1`;
  compaction deletes only the lowest retained row (`compacted + 1`) and only once every
  consumer's watermark is past it; watermarks are bounded by `allocated`; a new consumer may be
  registered only before any compaction (it would miss compacted history). Regression: two
  generations acknowledged and fully compacted, then a promotion gets sequence 3 and every
  consumer still has it due.
- **P2 — config sets are sealed and digest-checked.** `config_sets` carries `members_text`, the
  canonical JSON `{name: sha256}` (rebuilt and compared by the trigger), with
  `CHECK digest = sha256(members_text)`; a member row is accepted only if `members_text` lists
  that name with that hash, and a deferred constraint trigger checks at commit that every listed
  member exists — so a set is created and sealed in one transaction and nothing can be added,
  changed or removed afterwards. `Contracts.put_config_set` writes it that way. Regressions:
  wrong digest, non-canonical text, non-hash members, a late member, updates/deletes, and an
  incomplete set failing at commit.
- **Codex decisions on the open questions.** (1) Generation 0: copy the projection once into
  the baseline run's revisions, outside the round path, verified against the canonical export —
  a separate tool, **TODO (before step 6)**, never run against live by an agent. (2) **TODO (step
  6):** at cutover make the frozen legacy data read-only — the tracked directories, the paths
  atomic replacement writes through (temporary files and renames in those directories) and the
  root-level data files (`pruned_urls.txt`) — so a pre-step-1 FileStore writer fails. (3) Yes:
  after merge, migration and `pg_shadow verify`, Codex (not an agent) runs `store_authority.py
  init-file --dsn … --schema nekaise` on the live checkout. (4) Gate receipts and the projection
  consumer go to step 2; receipts must exist before any functional promotion, and the projection
  consumer must be registered before any compaction (the database now enforces the latter).
  (5) `backup_corpus` keeps refusing under PostgreSQL authority until step 5.
- **Gates re-run**: full suite 1147 passed / 41 skipped (PG skipped), with PostgreSQL
  (`nekaise_test`) 1218 passed; `py_compile` clean.

### Step 1, Codex re-review (2026-09-25): all four fixed; two outbox P2s fixed

- **A skipped insert allocates nothing.** The BEFORE INSERT trigger still takes the
  `outbox_state` row lock and checks `seq = allocated + 1`, but `allocated` now advances in an
  AFTER INSERT trigger (`nk_outbox_allocated`, conditional on `allocated = seq - 1`), which runs
  only for rows actually inserted — an `INSERT … ON CONFLICT DO NOTHING` that is skipped (e.g.
  seq 2 for an existing generation) no longer leaves a permanent gap that would block compaction.
  Regression: the skipped insert, then a promotion, full compaction, another promotion (seq 3) and
  its compaction.
- **Consumer registration serializes with compaction in the database.** Registration reads
  `compacted` with `SELECT … FOR UPDATE` on the `outbox_state` row, the same row lock compaction
  (the outbox DELETE trigger) takes; whichever waits re-reads the other's committed effect
  (READ COMMITTED: each trigger statement takes a fresh snapshot), so a registration never
  commits alongside a compaction it did not see. It does not rely on callers sharing the
  advisory writer lock. Regressions on two connections to `nekaise_test`, in both orders:
  compaction first → the waiting registration is refused; registration first → the waiting
  compaction is refused and the row stays due to the new consumer. All three regressions fail
  on the previous code.
- **Gates**: full suite 1147 passed / 44 skipped (PG skipped), with PostgreSQL 1221 passed;
  `py_compile` clean.

### Step 1, Codex third review (2026-09-25): allocation fixed; registration race closed for every isolation level

- **Registration writes the state row.** Locking alone did not help a REPEATABLE READ compactor:
  its snapshot predates the registrar's commit, so after the lock wait it still did not see the
  new consumer and deleted row 1 under it. Registration now UPDATEs `outbox_state`
  (`consumers_registered + 1`, a mark that only grows), and compaction already UPDATEs it
  (`compacted`). Two concurrent ones therefore conflict on one row under any isolation level:
  READ COMMITTED re-reads the committed effect; REPEATABLE READ and SERIALIZABLE raise a
  serialization failure instead of acting on a stale snapshot. No isolation level needs to be
  enforced. Regressions (two connections, `nekaise_test`, RR and SERIALIZABLE): Codex's exact
  schedule — registrar holds `late` uncommitted, the compactor takes its snapshot with
  `DELETE … seq = 1` and waits, the registrar commits — fails the compactor and keeps row 1 due
  to `late` (both fail on the previous code); and the reverse order fails the registrar.
- **Multi-row outbox inserts fail closed.** The second row's BEFORE trigger runs before the
  first row's AFTER trigger advances `allocated`, so it sees the wrong next sequence and the
  whole statement is refused with nothing changed (the conditional AFTER update could not
  corrupt the mark either). Rows are allocated one statement at a time; a test pins this.
- **Revising the contract DDL after deployment.** `V4_DDL` runs once per schema (at creation or
  in migration 4), not on every open. Live is still v3, so these revisions reach it with the
  migration. Once any schema is v4, a revised trigger, function or column must ship as a new
  migration (v5, …); editing `V4_DDL` alone would not reach it (noted at `_migrate_4`).
- **Gates**: full suite 1147 passed / 49 skipped (PG skipped), with PostgreSQL 1226 passed;
  `py_compile` clean.

## Stage 4, step 2 record: staging with constant-size promotion (2026-09-25)

Built as decided (Codex, stage 4 plan item 2) in `scripts/store_staging.py` over schema v5
(`store_pg.V5_DDL`, migration 5). FileStore stays authoritative; nothing here runs in
production yet — `run_round` still refuses any non-file authority (its PostgreSQL lifecycle is
wired in steps 4 and 6).

- **Model.** The existing tables (entries, manifest, blocklist, ledger, rotation, control_docs,
  backend_state) are the *projection*: they materialize generation P
  (`projection_state.generation`; NULL is the pre-generation base, i.e. today's shadow content).
  A run never writes them. Each metadata batch is ONE transaction
  (`PgStore.stage_batch(writer, run, step, batch, requests, expected_version=…)`): the batch's
  immutable request (canonical JSON text + sha256) is inserted as `requested` at the run's current
  staging sequence k (`basis_seq`), applied as revisions of the run at k+1, and sealed. Revisions
  are puts/tombstones keyed by the stable identity (table, key) — ledger rows as
  `"<row digest>:<n, 10 digits>"`, blocklist URLs by digest — carrying the derived lookup/order
  columns (url/title keys, sha256, legacy shard/topic) and `before_sha256`, the digest of the row
  the batch superseded. A batch records only its *net* changes (a key it created and deleted, or
  changed and reverted, leaves no revision); its results and per-table operation counts are stored
  in the receipt (`counts_text`), so an exact retry returns the original results (the file store
  returns 0s on replay; the broker contract only requires a no-op). Mutations have PgWriteView's
  validation and return values; bulk mutations (≥ 1000 revisions) COPY into a temporary buffer and
  apply in one statement.
- **Reading: views pin G, children read G + their run's overlay.** Every view is a REPEATABLE
  READ snapshot of the projection plus an overlay (`Visibility`): ordinary views (`read()`) pin
  the committed generation G and overlay the runs promoted in (P, G] (later generations win);
  a run's views (`read_staged(run, seq=…)`) add the run's own revisions up to a staging sequence
  (rank G+1). A projection row with any visible revision is superseded; the visible revision with
  the highest (rank, batch sequence) is the value; a tombstone hides it. Every existing
  PgReadView query runs unchanged over per-table "sources" (`_src`), except keyset scans, which
  page in bounded windows (below). Views pinned at G read G's sealed configuration set; staging
  views read the run's. Version tokens distinguish the two: committed `pg:<n>` (the counter,
  bumped by every promotion), staging `pg:stage:<run>:<k>`.
- **Who reads what.** A staged broker (`store_broker.Broker(…, stage=run)`) gives its mutating
  children `NEKAISE_STORE_STAGE=<run>:live:<token>`: their `PgStore.read()` opens the run's
  overlay at the sequence staged when the view opens, so a step sees every batch completed before
  it. `StagedRound.pinned_now()` pins discovery workers to one shared sequence, `gate_env()` pins
  gates to the frozen sequence. The token is 32 random bytes; the database stores its sha256
  (`run_access`, immutable). Without a matching token (or the run's writer) the overlay is
  refused (AuthorityError); a promoted, aborted or stale run is refused (StaleView). Children
  without the variable read committed state. conftest clears the variable.
- **Writing, freezing, gates, promotion.** Only the writer epoch that opened a run may stage,
  freeze, record gates or promote it (client-checked: `WriterError`; any writer may abort).
  `freeze(run, required_gates=[…])` drains nothing itself — `StagedRound.freeze` drains the
  broker first — and binds the run to its staged sequence and that sequence's chain digest.
  `record_gate(frozen, gate, passed=…)` stores an immutable receipt bound to (frozen sequence,
  frozen digest). `promote(frozen)` checks ownership, the parent generation, the frozen state the
  gates validated and that every required gate passed (and none failed), then writes a constant
  number of rows: generation G+1 (copying the run's provenance and a sum of its batch counts),
  run → promoted, dataset → G+1, one outbox row, the version counter. No revision is touched
  (tested: revision `xmin`s unchanged, row deltas exactly generations +1 / outbox +1).
- **Database contracts (v5).** Replaced trigger functions for the step-1 tables plus new ones;
  every cross-row rule is protected by an UPDATE of one shared row, so two concurrent operations
  conflict under any isolation level (READ COMMITTED re-reads in the trigger's fresh statement
  snapshot; REPEATABLE READ and SERIALIZABLE fail) — the class of the step-1 review findings — and
  every side effect lives in an AFTER trigger, which fires only for rows actually written:
  * a batch is requested only in an open run at its current sequence (AFTER INSERT: `runs.
    batches_open + 1` where `staged_seq = basis_seq`); it applies only at `basis_seq + 1` (AFTER:
    `staged_seq` advances by exactly one); it is sealed before commit (deferred constraint
    trigger), and sealing computes `revision_count`, `revisions_digest` (over the batch's
    revisions) and `chain_digest = sha256(previous chain : seq : step : batch : request digest :
    revisions digest)` in the database. Revisions can be written only while their batch is
    applied and unsealed — i.e. only by the transaction applying it — and never after;
    `staged_seq` and the counters move only through these triggers;
  * a run freezes only with `batches_open = 0`, at its staged sequence, with exactly that
    sequence's chain digest and a canonical, sorted, non-empty gate list; frozen values are
    immutable;
  * a gate receipt needs a frozen run and its exact (sequence, digest) (AFTER: `runs.
    gate_receipts + 1` where still frozen); receipts are immutable;
  * a generation needs every required gate passed at the frozen state and no failed receipt
    (checked on insert AND in the run's frozen → promoted transition, after the row lock); and a
    deferred constraint trigger requires it to commit together with its promoted run, the
    advanced current generation and its outbox row;
  * `projection_state` advances one generation at a time and never past an active retention pin;
    pinning writes the same row (AFTER INSERT/UPDATE: `pins + 1`, refused below the projection).
  Race tests on two connections (READ COMMITTED, REPEATABLE READ, SERIALIZABLE each): request vs
  freeze and freeze vs request, a failed gate during promotion, a receipt after promotion, a pin
  during a fold and a pin on a generation folded meanwhile — never both commit.
- **Discovery is persisted before it applies** (`Broker.computed_batch`). `run_round`'s discovery
  is now `plan_discovery(view, recorder, …)`: it reads only the view and records the complete,
  final request (assigned ids and suffixes, cursor values, exhaustion, github passes).
  On the file store it is applied as `<round>.discover.merge` exactly as before (all discovery,
  dedup, round and pipeline tests unchanged). In a staged round the request is persisted in one
  transaction — even when empty — and applied in a second; re-entering the step never calls
  `compute`: a `requested` receipt is applied exactly as persisted (only at the sequence it was
  computed at), an `applied` one is skipped. Test: a crash between the two, then the finders'
  proposal files rewritten — re-entry applies the original ids, suffix and cursor.
- **Folding: the projection consumer.** `fold(writer, limit=…)` applies the next promoted
  generation's effective revisions to the projection tables in bounded keyset batches (one
  transaction each, idempotent, resumable from `projection_state.fold_tbl/fold_key`); the last
  batch sets P = G+1 and acknowledges that generation's outbox row for consumer `projection`
  (registered by migration 5, before any compaction; compaction now also waits for it). Rows are
  written with their stored canonical text, never re-serialized. While generation P+1 is
  partially folded every view still overlays it, so it reads the same. Folding stops before an
  active `generation_retention` pin: `read_generation(g)` serves any generation the projection
  has not passed, byte-identically (export test over three generations, an aborted and purged
  run and a pending frozen one); older ones need the baseline tool (decision (1), below).
- **Legacy writes stop where staging starts.** `PgStore.transaction()` and `pg_shadow` replay
  refuse once any run is open/frozen or any generation exists
  (`store_staging.legacy_writes_refused`): the projection then belongs to the fold. The live
  shadow has neither, so its sync and verify are unchanged.
- **Keyset scans in bounded windows (performance decision).** A single `UNION ALL` over the
  projection and the overlay is planned as a hash anti-join over the whole projection plus a sort
  (O(table) per page); fencing the correlated subqueries keeps index order but Merge Append must
  still fetch each branch's first row, so with a dense overlay (a full re-clean overrides every
  row) the projection branch skipped all overridden rows to the end of the table on every page —
  quadratic for a full scan. `Visibility.scan` therefore pages in windows: fetch the next chunk of
  effective overlay puts (each flagged with the predicate; max(page, 256) rows), whose last key
  bounds the window; fetch the projection rows in the window that nothing visible overrides and
  that match the predicate, at most as many as the page still needs; merge in Python up to the
  point both sides are complete; continue. Both are single-table `ORDER BY … LIMIT` index scans
  with per-row visibility probes; a page costs its own key range plus one overlay chunk.
  Lookups, membership, aggregates and duplicate detection keep the unfenced sources (the planner
  may hash there, which is right for them).
- **Migration 5** (additive, one transaction under the writer advisory lock like 2–4; its DDL is
  idempotent so the tests' schema-version re-runs work): new nullable/defaulted columns on runs,
  batches and revisions, four revision indexes, `run_access`, `gate_receipts`,
  `projection_state`, replaced trigger functions, applied step-1 batches sealed in sequence order,
  unfinished step-1 runs' `batches_open` backfilled, consumer `projection` registered. No
  projection row, event, receipt or watermark is rewritten. Tests: a shadow imported and synced
  by the real v4 code (`git show e8ba0d581b:scripts/store_pg.py`) migrates with identical
  `pg_shadow` digests, byte-identical exports, the same dataset/authority row, `verify` OK, and
  keeps syncing; v4 clients are then refused ("version 5, code expects 4", and a v4 writer built
  before the migration cannot take the lock); a v4 schema with runs in every state (open with a
  requested batch, promoted, aborted) migrates consistently — every applied batch sealed with a
  chain, the open batch counted, the promoted generation readable through the overlay, then
  folded. Step-1 contract tests were adapted to the v5 rules (their helpers now apply, seal,
  freeze at the chain digest and pass a gate; the projection consumer acknowledges before
  compaction; the multi-row outbox test runs inside its promotion transaction).
- **Tests** (`tests/test_store_pg_staging.py`, 51 PostgreSQL tests): overlay equivalence against a
  second store to which the same random batches (seeded; every mutation kind, replace_manifest,
  floats like 1e20/-0.0/1.0 vs 1) were applied directly — every table, predicates, projections,
  key and legacy order, small pages, lookups, membership, aggregates, duplicates, control tables,
  configuration, artifacts — at every staging sequence (including pinned earlier sequences),
  after promotion, during and after a fold in 4-row steps, and on a second run; in three
  variants (literal visibility, subquery visibility, two-row scan windows with every mutation
  COPYing) plus many unfolded generations; replacement semantics; net changes; number fidelity
  through staging/promotion/fold; exact and conflicting retry; stale sequences (batches,
  persisted requests, pinned views, stale runs); failures leave nothing; request validation;
  atomic visibility (a poller during promotion sees only the before or the after state, an open
  view keeps its snapshot); constant-size promotion; freezing/gate/ownership rules; the database
  contracts against direct SQL; the races above; historical reconstruction; persisted
  discovery; a staged round through real child processes (writers, a pinned reader, a plain
  reader, a forged token, a gate at the frozen sequence, a late writer after the drain, a stale
  pin after promotion); a failing staged round is aborted; both migrations.
- **Benchmark** (`tests/test_store_pg_staging_bench.py`, opt-in `NEKAISE_PG_BENCH=1` or
  `python tests/test_store_pg_staging_bench.py --rows N`; a throwaway schema in `nekaise_test`,
  the live database refused), same host as stage 2/3:
  | 1.62M documents (1.62M entries + manifest rows, synthetic) | time |
  |---|---|
  | small round: discovery merge (400 entries, persisted + applied) | 0.067 s |
  | small round: loader checkpoint (25 rows), p50 / max of 16 | 0.012 s / 0.015 s |
  | small round: prune (100 deletes + entries + blocklist + ledger, 300 metric updates) | 0.092 s |
  | small round: cleaner patch (≈400 rows) · freeze · gate receipt | 0.057 s · 0.002 s · 0.003 s |
  | **promotion, small round (2 000 revisions)** | **0.004 s** |
  | full re-clean: staging, 82 batches of 20 000 patches (p50 / max per batch) | 194 s (2.29 s / 3.38 s) |
  | **promotion, full re-clean (1 620 300 revisions)** | **0.005 s** |
  | reads through the unfolded full-re-clean overlay: 25-id lookup · 10k-URL known() · first 2 000-row page · predicate page | 0.003 · 0.69 · 0.024 · 0.17 s |
  | whole-manifest paged scan (10 000-row pages): dense overlay / folded | 24.0 s / 10.9 s |
  | fold of the full re-clean, 82 batches of 20 000 (p50 / max) | 129 s (1.54 s / 3.07 s) |
  | small-round overlay after promotion: first page · known 10k · fold | 0.023 s · 0.29 s · 0.14 s |
  | peak client RSS for the whole benchmark | 217 MB |

  At 200k documents (the earlier plan shape, before the bounded-window scan) the dense-overlay
  full scan took 11.5 s against 1.2 s folded; with windows 2.2 s. The FileStore full re-clean
  measured in stage 3 was 234 s for the same row count (81 transactions); staging it is 194 s
  and promoting it 5 ms. Aggregates over 1.6M rows (6.7–15 s) are full scans in both shapes.
- **Decisions the plan left open** (for review): the projection is generation P and promoted
  revisions are folded afterwards, not copied (visibility = the run's promoted generation);
  ranks are generation numbers, the staging run ranking above G; batch identity (run, step,
  batch) with the request's basis sequence; receipts store results; a batch stores net changes
  only; frozen digest = the chain digest at the frozen sequence (constant-time, computed by the
  database); required gates are fixed at freeze and any failed receipt blocks promotion;
  ownership is the opening writer epoch; children are authorized by per-run tokens; legacy
  transactions and shadow replay stop once staging starts; staged runs write no legacy journal
  events (their receipts and revisions are the journal; `Table.EVENTS` shows the legacy journal);
  generation 0 on a schema without a baseline is the pre-generation projection plus the first
  run; historical reads are served while the projection has not passed a generation (pins hold
  the fold); scans page in bounded windows; bulk mutations COPY.
- **Deferred to step 3 (immutable artifact versions).** Staging covers metadata only: raw/text/
  corpus bytes are still written in place by fetch/clean before their rows are staged, so an
  aborted run can leave changed local files; `artifacts`/`artifact_locators` are not yet filled
  or consulted, and `corpus/` is not generation-stamped. (Done in step 3, record below.)
- **Deferred to step 4 (recovery replacement).** Adopting a run under a new writer epoch
  (resume only with unchanged parent/config/code) — today a restarted coordinator can only abort
  (the persisted discovery request is replayed within its owner's session); shared recovery over
  durable run status and `round_recovery` integration; wiring `run_round` and the maintainer to
  `staged_round` (open, stage, drain, freeze, gates with `gate_env`, promote, fold); standalone
  commands (`rotation.py`, `blocklist.add`, `migrate_backend_state.py`) as single-batch staged
  runs (their direct transactions are refused once staging starts); scheduling the fold and the
  purge of aborted runs; generation-range review.
- **Also open.** The baseline tool (decision (1)) — generation 0 as a full copy so every
  generation is reconstructible from revisions alone — is still TODO before step 6; ownership is
  enforced by the client, not by triggers; after a bulk re-clean the planner relies on fresh
  statistics for `revisions` (autovacuum), the scan shape keeps plans index-driven regardless;
  the 160M-row benchmark is step 5.
- **Gates**: full suite 1150 passed / 101 skipped (PG skipped), with PostgreSQL (`nekaise_test`)
  1280 passed / 1 skipped (the opt-in benchmark); `py_compile scripts/*.py` clean. Nothing ran
  against the live schema or checkout; the live shadow migrates 4 → 5 on its next
  `pg_shadow sync` after the merge (coordinator), as step 1 did.

### Step 2, Codex review fixes (2026-09-25; verdict MERGE AFTER FIXES, five P2)

v5 was not deployed anywhere but throwaway test schemas, so `V5_DDL` itself was revised (live
is still v4; the rule "ship a new migration" applies once v5 is deployed).

- **P2 1 — a partial fold is the pin floor.** After a fold batch commits a generation's first
  rows (`generation = P, fold_generation = P+1`) the projection no longer serves P. Pins are now
  validated against `COALESCE(fold_generation, generation)` (the retention trigger reads both
  from the `projection_state` row it writes), and the projection guard checks active pins when a
  fold STARTS (`fold_generation` set or changed), not only when `generation` advances; `fold()`
  already refused to start below a pin client-side. Tests: a real multi-batch fold (limit 1):
  pinning P is refused mid-fold while pinning P+1 works and the fold completes to it, then stops
  at the pin until it is released; the database refuses to start a fold below an active pin; two
  connections in both orders × READ COMMITTED / REPEATABLE READ / SERIALIZABLE (a pin taken
  while a fold starts, a fold started while a pin is taken) — never both commit.
- **P2 2 — revisions staged by v4 code get their derived columns.** Migration 5 now fills
  url/title keys, sha256 and the legacy shard/topic of every entries/manifest put already in
  `revisions` (`store_pg._backfill_revision_keys`, the same `revision_keys()` `stage_batch` uses),
  after adding the columns and inside the migration's transaction, with the revision guard
  disabled for the backfill's batched UPDATE statements and re-enabled right after (`ALTER
  TABLE … DISABLE/ENABLE TRIGGER`; the table is held exclusively until the migration commits,
  and a failure rolls the disable back); row text, digests, identities and provenance are untouched (tested
  byte-for-byte). Test: a generation promoted by the real v4 code — membership (ids, URLs,
  normalized titles), duplicate detection against projection rows and one-row legacy pages
  (one-row scan windows) read identically before folding and after.
- **P2 3 — promotion is constant in the number of batches.** Each run keeps a fixed-size
  summary, `runs.staged_counts` (`{table: {op: n}}`), added to in the batches' AFTER trigger when
  a batch seals (validated: an object of non-negative integer counts, or the seal fails; only
  while the run is open; like every run counter, only the trigger may change it). Promotion
  reads that summary and `staged_seq` (= applied batches) and nothing per batch (tested by
  intercepting the promotion's statements). Benchmark below grows batches independently of
  revisions.
- **P2 4 — one ownership check on every owner-only entrypoint.** `store_pg.require_run_owner`
  (locks the run row; `WriterError` unless the writer epoch opened the run) now guards
  `stage_batch` (before exact retries are answered), `Contracts.request_batch`, `read_staged`
  with a writer, `batch_receipt`, `abandon_batch`, `freeze`, `record_gate`, `promote` (exact
  retries included) and re-opening a run. The cross-owner recovery operations are explicit and
  named as such: `abort_run` and `purge_run`. A run's readers keep using its access token.
  **This is not a security boundary**: it keeps one trusted host's writers consistent (one
  writer at a time, fenced by the advisory lock and the writer epoch); any database client can
  bypass it, and the contracts it relies on are structural (the triggers), not authorization.
  Test: after the owner's session ended, a later epoch is refused by every one of these calls,
  including exact retries and the contract-level request; the token still reads; abort and
  purge work.
- **P2 5 — replacement and sealing are bounded.** `replace_manifest` tombstones every visible
  row it omits in ONE set-based `INSERT … SELECT` (before-image digests computed in the
  database; the kept ids are an array joined with a hash anti-join), so Python holds only the
  request's own rows; the net-no-op cleanup is one statement; the write view keeps operation
  COUNTS, not per-row records. Replacement stays one batch — its semantics need no multi-batch
  protocol once nothing about it is held in Python. The seal digest is built in two levels
  (sha256 per 4 096 revisions in (tbl, key) order, then sha256 of the chunk digests), so no value
  grows with the batch. Test: `replace_manifest` keeping one row over 60 000 rows stages 60 000
  tombstones with a Python peak below 8 MB (tracemalloc; the old expansion held ~60 MB) and a
  digest equal to an independent recomputation.
- **Benchmark additions** (same 1.62M-document run):

  | 1.62M documents | time |
  |---|---|
  | promotion after 1 / 1 000 / 50 000 batches (one revision each) | 3.6 / 2.5 / 2.7 ms |
  | staging those tiny batches through the real triggers (server-side loop, committed per 1 000) | 0.6 / 1.8 / 80 s (≈1.6 ms per batch, linear) |
  | promotion, small round / full re-clean | 5.3 ms / 3.5 ms |
  | full re-clean staging, 82 batches of 20 000 (p50 / max) | 153 s (1.90 s / 2.16 s; was 194 s — counts instead of per-row records) |
  | `replace_manifest` keeping one row over the whole manifest (1 620 300 tombstones, one batch) | 53.8 s, Python peak 0.1 MB |
  | whole-manifest paged scan: dense overlay / folded | 17.5 s / 9.9 s |
  | fold of the full re-clean | 116 s |
  | small round: discovery 400 / checkpoint 25 (p50) / prune 100 / clean 400 | 0.082 / 0.014 / 0.098 / 0.063 s |

  Found while measuring: running 50 000 batches inside ONE transaction was quadratic — the
  triggers' cached generic plans were made while `batches` held a few rows (sequential scans) and
  nothing invalidates them mid-transaction. Commits alone do not refresh those plans: a
  statistics change does (ANALYZE sends the invalidation). Production relies on autovacuum's
  ANALYZE and, since the second review, on `stage_batch` analyzing `batches` and `revisions`
  every 1 000 batches of a run (`ANALYZE_EVERY`); the benchmark commits and analyzes per 1 000
  batches.
- **Gates**: full suite 1150 passed / 113 skipped (PG skipped), with PostgreSQL (`nekaise_test`)
  1292 passed / 1 skipped (the opt-in benchmark); `tests/test_store_pg_staging.py` now has 63
  tests; `py_compile scripts/*.py` clean. Nothing ran against the live schema or checkout.

### Step 2, Codex second review (2026-09-25): the five fixed; three new findings fixed

- **P2 — the migration accepts receipts of completed v4 runs.** Migration 5 sealed legacy
  batches through the new summary trigger, which enforces NEW seals (open runs only), so a v4
  receipt with counts in a frozen, promoted or aborted run rolled the whole upgrade back. The
  legacy step is now separate from enforcement: inside the migration transaction the summary
  trigger is disabled while legacy batches are sealed, every run's summary is computed in one
  statement from its sealed receipts with the run guard disabled (a malformed legacy receipt
  refuses the migration), then both triggers are enabled again. Test: v4 runs open, frozen,
  promoted and aborted, each with a counted receipt — the upgrade succeeds and every summary
  equals its receipts.
- **P2 — a run starts with an empty summary.** The run guard's INSERT branch now also requires
  `staged_counts = '{}'` (the staging sequence and the other counters were already required to
  start at zero). Test: an INSERT seeding counts is refused.
- **P3 — counts are JSON numbers.** `nk_batch_counts()` (one definition for new seals and the
  legacy backfill) requires every count to be `jsonb_typeof = 'number'` written as a
  non-negative integer; a JSON null (which `#>>` turned into SQL NULL, slipping past the regex),
  a quoted number, a negative or a fraction refuses the seal. Test: each of those.
- **Wording.** The revision-key backfill disables the guard for several batched statements
  inside the migration transaction (comment and ADR corrected); the plan-cache note no longer
  claims commits refresh plans (see the benchmark note above: statistics changes do), and
  `stage_batch` now also analyzes the staging tables every 1 000 batches of a run.
- **Gates**: full suite 1150 passed / 116 skipped (PG skipped), with PostgreSQL (`nekaise_test`)
  1295 passed / 1 skipped (the opt-in benchmark); `tests/test_store_pg_staging.py` has 66 tests;
  `py_compile` clean. (Run concurrently, the suites then interfered: fixed test run ids —
  fixed in the third review, below.)

### Step 2, Codex third review (2026-09-25): no PG defect; test-execution isolation fixed

- **P2 — concurrent test executions could kill each other's children, and a live round's
  gate.** Round recovery stops every process of the user whose environment carries the round's
  `NEKAISE_RUN_ID`; tests tagged children with, and recovered, fixed ids (`rnd-p`, …), so a
  failing test in one execution could stop another execution's test children — including the
  pytest gate of a live round, which then rolls back — and the reverse. Every run id that
  reaches a child's `NEKAISE_RUN_ID`, a round snapshot, `--run-id`, `--recover` or a recovery
  call now comes from `tests/runids.py`: `rid(name)` appends a suffix random per pytest process
  (the same name gives the same id within an execution, so snapshots, commit trailers and event
  assertions still match). Regressions: two real concurrent executions (this process and a
  second Python process, each with its own suffix) tag children with the same round name; each
  one's recovery stops only its own children, in both directions; and a static check fails any
  test that hands a literal run id to a child environment, `--run-id`, `--recover` or
  `recover_round`. A writer's `round_id` alone (lock ownership, `NEKAISE_STORE_ROUND`) is not
  matched by recovery and stays as it was.
- **Gates**: alone — full suite 1152 passed / 116 skipped (PG skipped), with PostgreSQL
  (`nekaise_test`) 1297 passed / 1 skipped (the opt-in benchmark); run CONCURRENTLY on one host
  — the same results for both (before the fix, 11 round-recovery tests failed that way);
  `py_compile` clean.

## Stage 4, step 3 record: local artifacts compatible with atomic metadata (2026-09-25)

Plan decided by Codex: immutable local artifact versions for changed bytes, written and fsynced
before their references are staged; existing committed paths stay readable; pruning changes
membership only; payloads stay local (no packs, S3 or eviction); artifacts resolve through
generation membership; `corpus/` becomes a generation-stamped materialization with supervised
refresh that consumers acquire at a matching generation; the global ruleset stamp stops being
policy authority. FileStore stays authoritative and the legacy loader/cleaner/pruner behaviour is
unchanged (every step-6 equivalence test passes as before); the new path is active only inside a
PostgreSQL staged run whose artifact policy is `versioned`.

- **Immutable versions** (`scripts/artifact_store.py`). Identity `(stage, sha256)` of the exact
  bytes; the local locator is the content address `artifacts/<stage>/<aa>/<bb>/<sha256>` under the
  data root (git-ignored), registered apart from the identity (`artifact_locators`), so stage 5
  can add pack/object locators without changing any identity. `put_bytes/put_file/put_stream`:
  stream into a private temporary name (`artifacts/.incoming/<owner pid>-<hex>`) while hashing,
  fsync, make it read-only (0444), hard-link it to its address (`link` never replaces a name; an
  existing address is size-checked and a damaged one is never overwritten), fsync the address's
  directory (parents created durably), unlink the temporary. **Group commit** for bulk writers
  (the cleaner): `write_pending` (safe in worker processes; an unsynced temporary owned by the
  parent's pid, or nothing when the version exists) then `commit(pending)`: ONE `syncfs`, link
  every temporary to its address, a second `syncfs` (always), then drop the temporaries — the same
  guarantee (an address exists only complete, and is durable when commit returns) at two syncs
  per group instead of two fsyncs per version. `adopt(stage, file)` preserves an existing file's
  bytes as a version by hard-linking its inode (made read-only first, then hashed; a copy across
  filesystems). `sweep_incoming()` removes temporaries of dead owners or older than an hour.
  Crash hooks at every boundary (`written`, `synced`, `linked`, `published`; `group-synced`,
  `group-linked`, `group-published`).
- **Claims.** A manifest row's claim on a stage is `(path, sha256)` from `raw_path`/`sha256`,
  `text_path`/`text_sha256`, `corpus_path`/`corpus_sha256`; a path that is absent or JSON null
  is no claim. Rows keep their logical paths (`raw/<source>/<id>.<ext>`, `text/<id>.md`,
  `corpus/<id>.md`); a reader resolves a claim by identity — the immutable version when held
  locally, else the claim's legacy path (`VersionedAccess`, relative paths only). Existing
  committed files under `raw/`, `text/` and `corpus/` stay the locators of the unchanged claims
  that name them; the staged path never writes those directories.
- **Schema v6** (`store_pg.V6_DDL`, migration 6 — additive, one transaction under the writer
  advisory lock, idempotent DDL; no projection row, revision, receipt, event or watermark
  rewritten):
  * `runs.artifact_policy` — `versioned` (the default for new runs) or `unchecked`; the migration
    backfills every existing run `unchecked` (they were staged by code that knew nothing of
    artifacts: `ADD COLUMN … DEFAULT 'unchecked'`, then `SET DEFAULT 'versioned'`); immutable; a
    versioned run freezes only with the `"artifacts"` gate among its required gates (trigger;
    `freeze()` says so first).
  * `run_artifacts (run, stage, sha256, batch_seq)` — the identities a run's batches introduced;
    FK to `artifacts`; inserted only by the transaction applying a batch (applied, unsealed, open
    run — like revisions) and only for an artifact with a locator (checked per statement over the
    transition table, with keyed lookups); immutable; deletable only for an aborted run
    (`purge_run` now removes them; identities, locators and files stay).
  * `artifact_locators` — created unverified, immutable except `verified_at` (set, or moved
    forward); never deleted (stage 5 relaxes that with reference checks); a `local` locator must be
    the canonical content address.
  * The claim contract, checked when a batch of a versioned run seals (`nk_batch_artifacts`, in
    the sealing transaction): every claim of the batch's manifest puts is either exactly the claim
    of the row it superseded (found by the revision's `before_sha256` in the projection or any put
    revision: equal digests, equal text) or a valid identity (non-empty string path, string
    sha256) in `run_artifacts` for this run. One pass over the batch, one primary-key probe per
    claim, the before-image fetched once per row and only for an unregistered claim.
- **Staging** (`StagedWriteView._claim_artifacts`, versioned runs): for each manifest put, the
  claims that changed from the row it supersedes must be valid identities whose version is on
  disk (raw: with the row's `bytes`); new identities are registered with their local locator after
  a directory barrier (an fsync per directory, one `syncfs` above 64) and every changed claim is
  recorded in `run_artifacts` — all in the batch's own transaction, so a failed batch leaves no
  registration (the file stays for the retry). Exact retries answer from the receipt. Lookups are
  keyed (`LATERAL … OFFSET 0`), never scans of the growing global tables.
- **Resolution through membership.** `PgReadView.resolve_artifact(s)` resolves the visible row of
  the view (committed generation G, a staging overlay, or `read_generation(g)`): the registered
  local locator when the identity is registered, else the claim's legacy path.
  `PgReadView.provenance()` gives the view's dataset, generation and run provenance (cleaning
  ruleset, extractor version, artifact policy, config digest). FileStore is unchanged.
- **The pipeline steps** (`artifact_store.for_view(view, root)`: a `VersionedAccess` for a
  versioned staged view, else None and exactly the legacy code path):
  * loader — raw bytes `put_bytes("raw")`, extracted text `put_bytes("text")` (in the extraction
    process), extraction reuse and `--reextract`/`--verify` read by claim; `raw/` and `text/` are
    never written (a legacy file an earlier attempt left is not overwritten);
  * pruner — reads text by claim; moves no bytes (no quarantine): pruning changes membership, so
    every payload an admitted document or a retained generation claims stays;
  * cleaner — the **run's** `cleaning_ruleset` is the policy (`--rules` must equal it; the
    `corpus/.ruleset` stamp is neither read nor written); a row is up to date when its
    `cleaner_version` is the ruleset's, its new `corpus_source_sha256` equals the text identity it
    claims, and its cleaned version is held locally; otherwise it is cleaned byte for byte as the
    legacy cleaner does (`read_text` exactly as before; pass-through copies bytes; output encoded
    as `write_text` would) into pending versions committed in groups of 5 000 BEFORE the rows claim
    them; a text payload that does not hash to its row's `text_sha256` is reported, not cleaned;
    restricted rows lose their corpus fields as before (`corpus_source_sha256` joined
    `CORPUS_FIELDS`); `corpus/` is not touched. `--check` inside a staged run checks the claims,
    not `corpus/`.
- **`corpus/` as a materialization** (`scripts/materialize.py`). `refresh(st, root)` makes
  `corpus/` (or another directory) a materialization of committed generation G (default: the
  current one; a staged view is refused): `corpus/<id>.md` for every row of G that is ok, eligible
  under G's pinned policy and claims a cleaned payload — a hard link to its immutable version
  (a legacy file is adopted as a version first, its identity checked) — under the exclusive
  `corpus/.materialization.lock`, after durably stamping `corpus/.materialization.json`
  `refreshing`, installing by link-to-temporary + rename, and only at the end the directory fsync
  and the `complete` stamp (dataset, generation, config digest, ruleset). Every file replaced or
  removed is preserved as a version first. Incremental when the stamp is at (or refreshing from)
  B <= G of the same dataset and configuration: only ids revised by generations in (B, G]
  (`store_staging.changed_manifest_ids`; revisions of promoted runs are never purged) are
  revisited; otherwise a full pass plus a sweep of files outside G's membership. A member whose
  payload is missing fails the refresh and the stamp stays `refreshing`. Consumers call
  `acquire(dir, generation=…)` / `acquire_current(st, root)`: a shared lock for as long as they
  read, refused unless the stamp is `complete` at that generation of that dataset.
- **The artifact gate.** `artifact_store.verify_run` re-hashes (streamed, paged) every version the
  run referenced whose locator was never verified and marks them; `StagedRound.verify_artifacts()`
  records the `artifacts` gate at the frozen state (a damaged version fails it and promotion is
  refused).
- **Tests.** `tests/test_artifact_store.py` (25, no database): the write protocol; exceptions AND
  real process kills at each boundary (an address is absent or complete, the retry converges, a
  killed writer's temporary is swept, a live writer's is kept); the group commit and a crash at
  each of its boundaries; damaged versions never overwritten; adoption without copying; claims;
  resolution order and path safety; FileStore views keep the legacy path; the materialization
  lock/stamp protocol. `tests/test_store_pg_artifacts.py` (25, PostgreSQL): **a staged round
  (loader, pruner, cleaner with the production ruleset, artifact gate, promotion,
  materialization) against the legacy pipeline on the same repository — identical manifest rows
  (but the new field), raw/text bytes resolved from versions identical to the legacy files,
  materialized corpus identical file for file (CJK prose with a page marker, numeric tables,
  Modelica equations with diagram geometry, German with patent id soup, CRLF text), nothing under
  raw/text/corpus changed during the round, the replaced legacy corpus file preserved**; the claim
  contract (missing version, invalid or null identity, wrong raw size, empty path; registration
  and retry; unchanged legacy claims pass) and the same with the client check switched off (the
  database refuses at sealing; a claim registered for another run does not count; unchecked runs
  keep the step-2 rules); rollback of registrations with a failed batch; the v6 contract tables
  against direct SQL; a reference needs a located artifact; the artifact gate is required at
  freeze; resolution through membership (committed vs staged); **crash injection with G readable
  throughout** (every claimed payload of G resolves to bytes with its identity; the materialized
  corpus unchanged and acquirable): a loader child killed after its 3rd/9th version, after a
  link, after an fsync, or before its checkpoint submits, then a new round converges and G0
  (pinned) stays readable; a prune killed before its batch (no byte moved), then a prune that
  drops a held document (bytes all kept, the incremental refresh removes one file); a cleaner
  killed between metadata batches under a ruleset change, then a new round converging to a fresh
  full materialization; a promotion failing inside its transaction, then retried; a
  materialization killed after its stamp, after an install, after the sweep, before the complete
  stamp, or inside an adoption — refused to consumers, then converging to a fresh full refresh
  with the legacy bytes preserved; eligibility, stray files, stale generations, an older
  generation materialized elsewhere while pinned, refresh refused from a staged view; a damaged
  version fails the gate; the migration: a shadow imported and synced by the real v5 code
  (`git show e03860403a`) migrates with identical `pg_shadow` digests, byte-identical export and
  authority, `verify` OK, keeps syncing, v5 clients refused; v5 runs (promoted, and open with
  unregistered claims) backfilled `unchecked`, the promoted generation reads as before, the
  orphaned open run aborts and purges, new runs are versioned. Step-2 tests that stage synthetic
  rows (fake hashes) open `unchecked` runs; schema-version assertions follow `SCHEMA_VERSION`; the
  contract test's local locator is canonical.
- **Benchmark** (`tests/test_artifacts_bench.py`, opt-in like step 2; a throwaway schema of
  `nekaise_test` and a directory on the corpus's own NVMe disk — `/tmp` is tmpfs on this host,
  where fsync is free), 1.62M synthetic documents:

  | 1.62M documents, real disk | time |
  |---|---|
  | `put_bytes` 10 KB text / 5 MB raw / already present (p50) | 2.1 ms / 10 ms / 1.4 ms |
  | group commit of 5 000 cleaned versions (write + 2 syncfs, p50) | 0.45 s (≈ 0.09 ms each; per-file puts ≈ 11 s) |
  | loader checkpoint, 25 new documents with raw + text versions (p50): versioned / unchecked | 34 ms / 19 ms |
  | cleaner batch, 20 000 patches with new corpus versions: versioned / unchecked | 4.6 s / 1.8 s |
  | artifact gate re-hashing 20 000 versions | 1.4 s |
  | promotion (versioned) | 3.5 ms |
  | materialization: full refresh (1.62M rows scanned, 40 000 members linked) | 24 s |
  | materialization: incremental after a 400-row round / nothing changed | 0.84 s / 0.03 s |
  | peak client RSS | 227 MB |

  A full re-clean of 1.62M rows therefore stages in ≈ 82 × 4.6 s ≈ 6.3 min plus ≈ 2.5 min of group
  commits (unchecked staging ≈ 2.5 min); a typical round's versioned metadata stays well under a
  second. Found while measuring: per-row trigger checks and set joins against the growing global
  tables were planner-dependent (in an ad-hoc probe a stale-statistics nested loop over
  `run_artifacts` ran for 30 minutes), so every check now uses keyed probes (`LATERAL … OFFSET 0`,
  a statement-level insert check, a plpgsql pass over the batch); and one fsync per version made
  a 20 000-document clean take ~45 s of fsyncs, hence the group commit.
- **Decisions the plan left open** (for review): the local layout and its two-level fan-out;
  identity = sha256 of the exact bytes per stage; rows keep logical paths and gain no locator
  field; the only new manifest field is `corpus_source_sha256` (versioned cleaning only); the
  artifact policy is per run, `versioned` by default, `unchecked` kept for metadata-only runs and
  pre-v6 runs (it is also the rollback switch: an unchecked staged run is exactly step 2); the
  claim rule is "unchanged, or registered for this run", checked by the client AND at sealing;
  registration happens in the referencing batch's transaction (no separate registration step to
  crash between); the stager's barrier is a directory fsync or one `syncfs`; bulk durability by
  group commit; the artifact gate is mandatory for versioned runs and hashes only never-verified
  versions; `corpus/` stays at its path as an in-place materialization under a lock (not a
  symlink swap) with a stamp consumers must match; replaced or removed materialized files are
  adopted as versions; the materializer does not register adopted versions in PostgreSQL (the
  content address is self-describing and resolution checks it first); `.ruleset` is left in place
  (never consulted under the staged path; a rollback to file authority still finds it). Like run
  ownership, the policy and the claim rule keep one trusted host's writers consistent; they are
  not an authorization boundary.
- **Deferred to step 4 (recovery replacement).** Wiring `run_round` and the maintainer to staged
  rounds (their freeze must name the `artifacts` gate; `corpus/` is refreshed after promotion,
  supervised); resuming an adopted run (today a crashed round's run is aborted and a new one
  re-uses the versions already written); sweeping `.incoming` in recovery; the round's
  `clean --check` gate becomes the versioned claim check plus the artifact gate.
- **Deferred to step 5 (artifacts move).** Reference-checked garbage collection of versions no
  retained generation or open run references (aborted runs', adopted legacy copies, replaced
  materializations) — nothing is deleted today; periodic re-verification of verified versions;
  pack/object locators and eviction; registering adopted legacy versions in PostgreSQL (a
  background adoption of the pre-cutover raw/text/corpus files, so every generation-0 claim has a
  registered identity); `backup_corpus` for `artifacts/`.
- **Rollback.** Before cutover: open staged runs `unchecked` (step-2 behaviour) or simply keep
  FileStore authority (nothing in production uses the path yet). Created versions are retained for
  reference-checked cleanup; schema v6 stays (older code is refused, as after every migration).
- **Gates**: full suite 1183 passed / 142 skipped (PG skipped), with PostgreSQL (`nekaise_test`)
  1353 passed / 2 skipped (the two opt-in benchmarks); `py_compile scripts/*.py` clean. Nothing
  ran against the live schema or checkout.

### Step 3, Codex review fixes (2026-09-25; verdict MERGE AFTER FIXES, three P1, three P2)

v6 is deployed nowhere but throwaway test schemas (live is v5), so `V6_DDL` itself was revised;
once any schema is v6, a change ships as migration 7. Each fix has a regression test that fails
on the reviewed commit (447dfee272) and passes now.

- **P1 — a retained generation resolves to its own bytes, not to a reused legacy path.** G0
  claims legacy `corpus/x.md`; G1 changes it; the refresh adopts G0's bytes as a version and
  replaces `corpus/x.md` — adoption registers no locator, so `resolve_artifact` returned the
  logical path, now holding G1's bytes (or nothing after a prune). Resolution now lets the
  identity decide in both APIs: the canonical local version `artifacts/<stage>/…/<sha>` when this
  machine holds it (registered or not), else the registered local locator, else the legacy path
  (`PgReadView.resolve_artifacts` = `VersionedAccess.path` = the materializer's source). No
  database state is needed, so snapshots already open resolve correctly too. Test: a
  legacy-baseline G0, pinned, across its replacement (G1 re-clean + refresh), a prune (G2 +
  refresh removing the file) and a fold of G0 — `read_generation(G0)` and `VersionedAccess` both
  resolve to G0's bytes and every claim of G0 stays readable.
- **P1 — same-size damage never costs the last intact original.** An existing address was
  accepted by size; so an adoption "succeeded" against same-size damaged bytes and the refresh
  then unlinked the intact source. Every existing address is now accepted only after a hash check
  (`_check_existing`: single puts, adoptions, and group commits for versions that already existed
  or were linked by someone else); a mismatch raises, nothing is overwritten, and the refresh fails
  with the original in place and the stamp `refreshing`. Tests: put, adopt and commit against a
  same-size damaged address (no database); the materialization case (PostgreSQL).
- **P1 — reused directory chains are made durable.** A group commit that created fan-out
  directories, linked and died before its final `syncfs` left directory entries that a later
  single put or adoption (which found the directories) did not fsync — only the leaf directory
  was. Publication now makes the whole chain durable: `_durable_chain(dir)` fsyncs every
  directory's parent up to the data root, found or created, once per process (a per-process
  cache of chains already made durable — directories are never removed, so it stays true); the
  address's own directory is fsynced after every link or find. The staging barrier does the same
  for each leaf (or one `syncfs` above 64 leaves). Test: an interrupted group publication, a
  single-file retry in a "new process" (cache cleared), then a small referencing batch in another
  one — an fsync spy sees leaf, both fan-out levels, the stage directory, `artifacts/` and the
  root each time; and without a database, a second put under the same ancestors fsyncs only its
  new leaf.
- **P2 — the before-image is the actual preceding state.** The seal compared a claim with the
  row named by the revision's own `before_sha256`, found in ANY put revision — so a revision (or
  an unrelated, aborted run) could supply its own "unchanged" before-image. `nk_basis_text(run,
  seq, key)` now reads the row visible at the batch's basis: the run's latest revision of the key
  in an earlier batch, else the latest revision of a run promoted in (projection generation, the
  run's parent], else the projection row; `before_sha256` is not consulted. Test (direct SQL
  batches): a put naming its own digest, one naming an aborted run's identical row, one with no
  before-image, and a new key "superseding" a row only the aborted run staged — all refused; an
  honest unchanged claim still passes with no registration.
- **P2 — references need a readable local locator, and verification never skips one.** An
  identity located only as `object` was accepted by the reference trigger and the seal but
  skipped by verification (an inner join on local locators). The reference trigger now requires
  the canonical local locator; `unverified_run_artifacts` returns every reference with no
  canonical local locator (locator None) as well as the unverified ones, and `verify_run` fails
  them ("no readable local locator"). The stager already adds the local locator when a
  registered identity lacks one and its file is on disk. Test: an object-only identity is
  refused as a reference; one forced in (trigger disabled) fails the artifact gate and promotion.
- **P2 — versioned cleaning is bounded.** `build_versioned` held the whole manifest, every
  before-image, the task list and every patch, and staged only at the end. It now reads the
  manifest a page at a time (key order), submits documents in chunks of `CLEAN_CHUNK` with at most
  `workers × IN_FLIGHT_PER_WORKER` chunks in flight, makes each group of `GROUP_COMMIT` versions
  durable and only then turns it into patches, and stages patches (and restricted-row clears) as
  soon as `METADATA_BATCH_ROWS` are waiting. Memory: one page + the chunks in flight + one group +
  one batch, whatever the corpus size. Test: tiny page/chunk/group/batch sizes — metadata batches
  are staged while documents are still being cleaned, every group is durable before its first
  batch, no chunk exceeds its size, and the rows end up claiming held versions.
- **Found while benchmarking the P2 fix: pending versions did not survive a process pool.**
  `Pending` was a bare tuple subclass that pickling could not rebuild, so the cleaner's REAL
  worker processes failed (the pipeline tests run workers in threads). It is a `NamedTuple` now;
  a test round-trips it and cleans through a real `ProcessPoolExecutor`.
- **`build_versioned` benchmark** (`python tests/test_artifacts_bench.py --clean --rows N`, one
  size per process; every document needs cleaning under the production ruleset; 8 workers; NVMe):

  | documents | `build_versioned` (clean + group commits + staged batches) | artifact gate | peak RSS of the process |
  |---|---|---|---|
  | 20 000 | 6.9 s | 1.4 s | 320 MB |
  | 200 000 | 71 s | 8.9 s | 323 MB |
  | 1 620 000 | 609 s (≈ 2 660 docs/s) | 74 s | 338 MB |

  Peak RSS is flat in the corpus size (≈ 270 MB above the idle process, most of it one 20 000-row
  staged batch in the broker thread and one 10 000-row manifest page); the old version held the
  whole manifest and every patch.

- **Disk.** Nothing is deleted before stage 5's reference-checked collection. The first versioned
  full clean writes every cleaned document once more into `artifacts/corpus/` — about 77 GB for
  today's corpus (1.2 TiB free on the corpus disk); legacy `corpus/` files are adopted by hard link
  (no copy) when the materialization first replaces them. Later rounds add only what changed.
- **Step-3 benchmark re-run** after the fixes (1.62M documents, same host and disk): unchanged
  within noise — `put_bytes` 10 KB p50 3.3 ms, an already-present version (now hash-checked)
  1.4 ms, group commit of 5 000 0.49 s, loader checkpoint versioned/unchecked 33/16 ms, cleaner
  batch of 20 000 versioned/unchecked 4.8/1.8 s, gate 1.3 s per 20 000, promotion 4.4 ms,
  materialization full 25.6 s / incremental 0.80 s / no-op 0.024 s, peak RSS 225 MB.
- **Gates**: full suite 1186 passed / 148 skipped (PG skipped), with PostgreSQL (`nekaise_test`)
  1362 passed / 2 skipped (the two opt-in benchmarks); `py_compile scripts/*.py` clean. Nothing
  ran against the live schema or checkout.

### Step 3, Codex second review (2026-09-25): the six fixed; two new findings fixed

- **P1 — the versioned path applies the full training predicate.** The paged cleaner checked
  only `restriction_for()` and so dropped the licence half of the legacy partition
  (`partition_manifest_ok_rows` → `is_training_eligible`): a successful pointer-only
  (`proprietary-internal`) row with extracted text was cleaned, and the check and the
  materializer (restrictions only as well) let it reach `corpus/`. All three now use
  `registry.is_training_eligible` (pointer-only licences AND eligibility.json restrictions); an
  ineligible row keeps its raw/text provenance and any corpus metadata it carries is cleared (the
  check reports it until then). Audit of the other filters the legacy steps apply: `status ==
  ok` and a text claim (applied), suspended-host rows missing locally (applied, by claim), the
  loader's and pruner's eligibility decisions (shared code, unchanged on both paths). NC/ND
  licences are refused by the finders (`licenses.py`) before anything reaches the registry; no
  pipeline step filters them, on either path. Regression: a pointer-only row with text and a
  stale legacy corpus claim — the check fails on it, a staged round leaves it uncleaned with its
  corpus fields cleared and provenance kept, the refresh removes (after preserving) its legacy
  file, and a later generation in which it claims a held version still does not materialize it
  (fails on 3d5256a75f).
- **P2 — the directory cache holds only complete chains.** `_durable_chain` cached each
  directory as soon as its parent was fsynced, so a later ancestor fsync failure left entries
  that made a same-process retry stop early with no fsync. It now collects the uncached part of
  the chain, fsyncs every parent topmost first, and caches the whole part (under a lock) only
  after all succeeded; overlapping callers may sync twice but never cache an incomplete chain.
  Regressions: an ancestor fsync failure then a retry that fsyncs root, `artifacts/`, the stage
  and fan-out directories in that order; three overlapping callers (one blocked mid-chain, one
  failing, one completing) — nothing cached before a chain completes, and every cached entry's
  ancestors cached.
- **Notes for step 4/5 (no code now).** (a) The cleaner stages batches in completion order, so
  batch contents are not deterministic across attempts: same-run resume must not simply restart
  numbered cleaner batches (it must treat the applied receipts as done and recompute the rest, or
  abort and re-run, as today). (b) The seal-time basis lookup (`nk_basis_text`) walks a key's
  retained revision history across promoted, unfolded runs; its cost grows with the number of
  generations between the projection and the run's parent — benchmark it with accumulated
  unfolded generations before cutover (step 5). (c) Resolution is an existence lookup (the
  locally held canonical version, else the registered locator, else the legacy path), not an
  integrity check; integrity is the artifact gate at staging time and a future periodic
  re-verification (step 5).
- **Gates**: full suite 1188 passed / 149 skipped (PG skipped), with PostgreSQL (`nekaise_test`)
  1365 passed / 2 skipped (the two opt-in benchmarks); `py_compile scripts/*.py` clean. Nothing
  ran against the live schema or checkout.

## Stage 4, step 4 record: recovery and operational review (2026-09-25)

Plan decided by Codex: "Replace recovery and operational review. Shared recovery acquires
ownership, stops owned descendants, drains the broker, and queries durable run status. Promoted
runs stand even after lost replies; finish their materialization/cleanup. Unpromoted runs default
to abort. Explicit resume requires unchanged parent/config/code and durable batches plus verified
artifacts; otherwise start a new run. Uncertain database outcomes block mutation. Standalone
mutations and maintainer repairs use the same staged promotion lifecycle and required checks.
Preserve cancellation-safe draining. Triage pins a generation without holding growth locks;
action reacquires ownership and refreshes both generation and code evidence. Replace outgoing
data-commit review with generation-range review: revisions, decisions, quality/yield changes,
failures, gate receipts, and backup health. Persist review verdicts and a contiguous reviewed
watermark. Findings withhold endorsement/publication; integrity findings also block growth.
Repairs create compensating generations. Gate: every existing recovery entrypoint, timeout,
orphan, lost-response, and maintainer race scenario against PG. Rollback: keep legacy recovery
selected until production cutover."

Production is unchanged: FileStore stays authoritative and the legacy round (git snapshot,
per-round commit, README stats), the legacy recovery (`round_recovery.recover_round`) and the
legacy outgoing-commit review stay selected. Every new path is chosen by ONE predicate,
`staged_runs.staged_authority(st)`: the store is a PgStore bound to a host authority record whose
mode is `postgres` (an unbound PostgreSQL store is still refused by `run_round` and the maintainer,
as in step 1). Rollback before cutover is therefore "change nothing"; schema v7 stays (older code
is refused, as after every migration).

- **Schema v7** (`store_pg.V7_DDL`, migration 7 — additive, one transaction under the writer lock,
  idempotent DDL, every derived value backfilled; no projection row, revision, receipt, event or
  watermark rewritten):
  * `runs.owner_epoch` — the writer epoch that owns the run: backfilled from `writer_epoch` for
    every existing run (the run guards are disabled for that one statement inside the migration's
    transaction, as migration 5 did), set to the opener on insert (a different value is refused),
    and changed only by a logged adoption (trigger `runs_owner`: depth 2, one epoch forward, a
    matching `run_adoptions` row). `store_pg.require_run_owner` — the one ownership check of every
    owner-only operation — now reads `owner_epoch`.
  * `run_adoptions` — the immutable log of explicit resumes. The guard locks the run row and
    accepts an adoption only when the adopter is the CURRENT writer (`state.writer_epoch`), one
    epoch past the current owner, the run is open or frozen, its parent is still the dataset's
    current generation, and the producer commit, configuration set, extractor version and staged
    sequence the adopter runs with equal the run's own row (never what the adopter claims alone),
    and every artifact the run referenced has a verified canonical local locator. The AFTER
    trigger moves `owner_epoch` (conditional on the previous owner: a concurrent adoption fails).
  * `purge_queue` — aborted runs whose staging is still to be purged: an abort queues its run
    (AFTER UPDATE trigger on `runs`, so every abort path does), only aborted runs may be queued,
    an entry leaves only when the run has no revision, artifact reference, gate receipt or batch
    left. Backfilled with every aborted run that still has staging.
  * `review_state` (one row: `reviewed_through`, `endorsed_through`, verdict count, open findings
    / integrity findings) and `review_verdicts` (below). The review consumer's acknowledgements and
    watermark are written only by the verdict trigger (a direct `review` ack or watermark move is
    refused), and a new trigger keeps the `publication` watermark at or below the outbox row of the
    endorsed generation. Backfill: `reviewed_through`/`endorsed_through` from the review consumer's
    existing acknowledgements; a legacy non-ok review acknowledgement refuses the migration
    ("migrate by hand": it would need a finding row to resolve). Live has neither.
- **A round under PostgreSQL authority** (`run_round.staged_main`): writer (the database's writer
  lock), then `_complete_previous` — refused while any run is unfinished (open/frozen: `--recover`
  or `--resume` first) or an integrity finding is open; the current generation's completion caught
  up — then the run's identity (`staged_runs.identity`): a clean checkout (`git status` empty; a
  git failure raises), its HEAD as producer commit, the exact bytes of its configuration files as
  the run's sealed config set (`open_run(config_documents=…)`), `build_corpus.EXTRACTOR_VERSION`,
  and the cleaning ruleset inherited from the parent generation (before the first generation: the
  legacy `corpus/.ruleset` stamp, which is what the file-authoritative corpus was built with).
  `staged_round(kind="round")`; finders read the run pinned at sequence 0; the discovery merge is
  the persisted computed batch; fetch/prune/clean stage through the broker; `freeze` with the
  required gates `artifacts`, `check`, `contracts`, `lint`, `tests` (no `--skip-tests`); the gates
  run concurrently against the frozen state (`gate_env`) and every verdict is recorded as a gate
  receipt (`run_verify_parallel(record=…)`), the artifact gate in-process; `promote`; then
  `after_promotion`. No snapshot, commit or README: `--commit` is implied, `--push` and
  `--allow-dirty` are refused. `check` is the claim check `clean_corpus.py --check` runs inside a
  staged view (step 3), which with the artifact gate replaces the file store's corpus/ check; the
  `index` gate and the `stats` step belong to the file store only, and `check_contracts` skips its
  README checks under PostgreSQL authority. SIGTERM (dig.sh's `timeout`, an operator's kill)
  raises KeyboardInterrupt, so the round recovers itself on the way out (exit 130).
- **Completion and scheduling** (`staged_runs.after_promotion`, `housekeeping`): the incremental
  refresh of the corpus/ materialization to the current generation (step 3), then the fold of
  promoted generations into the projection and the purge of aborted runs queued more than 24 h
  ago (their staging stays inspectable for the review meanwhile), each step its own short
  transaction (≤ `FOLD_BATCH` rows), under a 60 s budget per call; what is left waits for the next
  call. It runs after every promotion (round, standalone, maintenance), at every round's start
  and in every recovery — the fold and purge schedule. The run rows of aborted runs, with their
  abort reason, are kept (failure evidence).
- **Shared recovery** (`round_recovery.recover_staged`, the staged form of the one routine): (1)
  the caller holds the writer — ownership, and proof that no coordinator of those runs is alive;
  (2) targets: the named run, or every open/frozen run (a durable query; a failure raises before
  anything is touched); (3) stop the runs' processes — the caller's new descendants and every live
  process tagged with a target's `NEKAISE_RUN_ID`; (4) drain the caller's broker, in a `finally`,
  so it is drained even when stopping failed or was interrupted; (5) per run a FRESH status query
  decides: promoted → kept (`staged_run_kept`), open/frozen → aborted and re-read to confirm,
  aborted → nothing, never opened → nothing; a status that cannot be read raises — an unknown
  database outcome never leads to an abort, a sweep or a completion; (6) sweep what the stopped
  processes left: `artifacts/.incoming` temporaries of dead writers and the runs' finder proposal
  directories (`workspace/finder-proposals-<run>-*`, which TemporaryDirectory cannot clean after
  SIGKILL; materialization temporaries are removed by the next refresh under its lock); (7)
  optionally finish (`after_promotion`). Entry points: `run_round.py --recover latest|RUN_ID`, the
  maintainer (both windows), and every staged round/standalone/maintenance run that fails before
  its promotion — `store_broker.staged_round` stops the processes in the body's `except`, lets
  `serving()` drain the broker, then calls the routine with `stop=False`. A promotion whose reply
  is lost (it committed, the client raised) is found promoted and stands; `abort_run` itself also
  refuses a promoted run.
- **Explicit resume** (`run_round.py --resume RUN_ID`): with the writer held, the run's tagged
  orphans are stopped and temporaries swept; then every refusal is checked BEFORE anything is
  adopted — the run is an open or frozen ROUND (a standalone or maintenance run is never re-run
  as a round's pipeline: recovery aborts it), no integrity finding is open, its parent is the
  current generation, the checkout's commit, configuration digest and extractor equal the run's,
  no other run is unfinished, and a frozen run's gate set equals this invocation's with no failed
  receipt — then `artifact_store.verify_run`
  re-hashes every version the run referenced that was never verified, and `adopt_run` logs the
  adoption (the database re-checks all of it) and issues a new access token. Otherwise the error
  names `--recover RUN_ID` (abort) and a new round. An open run continues with `attempt` n (1 +
  its adoptions): its steps' batches are named `a<n>-<batch>` (`NEKAISE_STORE_ATTEMPT`), so a
  re-run step never collides with an earlier attempt's applied batches — the cleaner stages in
  completion order (step-3 note), so batches are never renumbered and replayed: applied receipts
  stand as done work and the step recomputes the rest over the run's overlay, idempotently
  (loader: fetched rows are ok; cleaner: up-to-date claims are skipped; pruner: decisions from the
  overlay). Discovery is never started again: a persisted merge is applied exactly as persisted,
  an applied one skipped. A frozen run records only its missing gates and promotes.
- **Standalone mutations** (`store_broker.run_batch`, `step_session`; i.e. `rotation.py advance|
  next`, `blocklist.add`, `migrate_backend_state.py --apply`, find_github's pass recording, the
  registry adapters, and a standalone `build_corpus.py` / `prune_corpus.py --apply` /
  `clean_corpus.py`): outside a broker and under PostgreSQL authority each is ONE staged run of
  kind `standalone` (`staged_runs.standalone`): refused while a run is unfinished; the same
  identity as a round; its batches staged in the run; frozen with the round's gates `artifacts`,
  `check`, `contracts`, `lint`, `tests`, which run as subprocesses of the checkout's own scripts
  (and its test suite, without a read pin) against the frozen state; promoted; completed (a completion failure is a warning — the promotion stands). `run_batch`
  computes under the writer from the committed generation first and opens a run only when
  something was recorded; a step session with nothing to stage aborts its run as a no-op. Under a
  broker (a round's step, a maintenance window's agent) nothing changes: the batch is the broker's.
- **The maintainer** (`maintainer.open_store`, `Window`): the snapshot window serves no broker
  (nothing may mutate while evidence is taken), recovers every unfinished run with the shared
  routine (finishing the current generation), checks that corpus/ is a complete materialization
  of the current generation, pins the triage generation (`generation_retention`, holder
  `maintainer-<id>`, `until` +8 h so a dead maintainer cannot hold the fold forever; released on
  every way out of the pass — no action, a failed or invalid triage, the action's end — with
  `store_staging.release_pin`, which needs no writer because dropping a pin only lets the fold
  proceed) and reads the generation-range review evidence up to it; growth-block reasons add unfinished runs and open
  integrity findings (a failed read blocks). Triage then runs without locks. The action window
  reacquires both locks, recovers again, opens ONE maintenance run (identity as above; a dirty
  checkout means no run and every store mutation is refused in that window), exports its broker and
  read pin to the agent's processes, and reports fresh code AND generation evidence
  (`changed_since_triage` covers HEAD and the generation). After the agent: `conclude(ok)` drains,
  then — only if the action succeeded (exit 0 and no tool of it left running: `run_command`
  reports the survivors it had to stop, whose work is incomplete), something was staged and the
  checkout still has the run's commit and configuration (an agent that commits code or policy in
  the window gets its data mutations aborted: they ran under other code than the run records) —
  freezes, runs the gates, promotes (a compensating generation) and completes it; otherwise, or
  when a gate fails, it aborts. A conclusion that fails otherwise is recovered by durable status
  at once (so the growth block judged next never shows the window's own run as unfinished); a
  window left without a conclusion (an exception, SIGTERM) aborts through `staged_round`. Then the agent's review verdict (below) is recorded, the pin released and the
  growth block updated, still under both locks. `conclude` hides the exported broker/pin variables
  from the maintainer's own reads (found by the end-to-end test: the completion's materialization
  read the just-promoted run's pin through the exported environment). Under PostgreSQL authority
  both prompts get `maintainer_prompts/staged.md`.
- **Generation-range review** (`scripts/generation_review.py`). `evidence(through)` covers the next
  unreviewed range (reviewed_through, min(through, +200)]: per generation its run, kind, producer
  commit, config digest, extractor, ruleset, frozen state, operation counts (promotion record),
  manifest rows its run staged by status (bounded by the range) and its gate receipts; the
  configuration documents that changed between consecutive generations and ruleset changes; the
  runs aborted while producing the range (parent in [lo-1, hi-1]) with their reasons (total + 50);
  the code commits between consecutive producer commits (git, bounded); backup health (the WAL
  archiver's last success/failure from `pg_stat_archiver`, the newest base backup's age; an
  unreadable location is reported, never hidden). The database part has a digest.
  `record(through, verdict, evidence_digest, …)` recomputes the evidence under the writer and
  refuses a verdict whose digest differs — the reviewer can only endorse exactly what it saw. The
  database (v7) keeps verdicts contiguous (`lo = reviewed_through + 1`, `seq = verdicts + 1`, the
  shared `review_state` row locked, so concurrent verdicts conflict under any isolation level),
  bounded by the current generation, immutable except the one-time `resolved_by` of a finding set
  by a later verdict's trigger; `resolves` must name open findings (canonical sorted list) and
  a verdict resolves a finding only when its range covers a generation promoted AFTER the
  finding's range — the compensating repair — so a verdict written before the repair exists (or
  over no generation) cannot resolve it; an empty range (hi = lo - 1) only records a finding about
  generations already reviewed. Verdict semantics: `ok` reviews the range and, when no
  finding is open, endorses through it; `finding` reviews it but endorsement (and so publication:
  the `publication` watermark is capped) stops until a later verdict resolves the finding;
  `integrity` also refuses growth rounds (`growth_block`, checked by `run_round` before a round
  and in the maintainer's block reasons) while standalone and maintenance runs — the repairs —
  still promote. Every verdict acknowledges the range's outbox rows for the `review` consumer and
  advances its watermark (the consumer is the verdicts' outbox image). CLI `status | evidence |
  record`; in the action window the agent writes its verdict to `$NEKAISE_REVIEW_VERDICT_FILE`
  (`verdict_from_file` validates it) and the maintainer records it as `codex-maintainer` only
  when it is about exactly the range and digest the pass showed (an agent with database access
  could otherwise compute and endorse evidence for generations neither triage nor Claude saw); a
  refused verdict is reported in the history and the range stays unreviewed.
- **Test isolation** (`tests/authority_site/sitecustomize.py`, tests only): real `run_round.py`
  processes and all their children see a throwaway checkout's authority record because the tests
  put that directory on `PYTHONPATH`; its `sitecustomize` points `store_authority.HOST_RECORD` at
  the test's private record and can turn a crash-injection hook into a process kill. Production
  code has no such override (the record is still located through the passwd database only). The
  static run-id check now also covers `--resume`, `recover_staged` and `staged_round`.
- **Tests** (PostgreSQL, `nekaise_test`, throwaway schemas and checkouts): `test_staged_rounds.py`
  (15, real `run_round.py`/`prune_corpus.py`/`clean_corpus.py`/`generation_review.py` processes, a
  fake finder, a local HTTP server): a round is one promoted generation with every gate receipt
  bound to the frozen state, the finder pinned at sequence 0, git untouched; a second round's
  incremental materialization; a failing gate aborts and queues the purge, the next round
  promotes; a dirty checkout or `--push` refused before anything stages; SIGKILL mid-fetch — a new
  round refused, `--recover latest` stops the orphaned fetch before aborting, sweeps a dead
  writer's temporary, the next round promotes; SIGTERM (timeout) recovers on the way out; a round
  killed during its materialization stands and recovery completes it; resume after changed code
  refused without touching the run, then resumed (finder not re-run, `a2-` batches after the first
  attempt's checkpoint, every document present); a frozen run resumed with only its missing gates
  (a different gate set refused before adoption); a damaged unverified version refuses resume;
  standalone `blocklist.add` as one gated promoted run (a no-op makes no run), standalone prune as
  a promoted run and a no-op clean aborted, standalone mutations refused while a run is unfinished
  and aborted when their gates fail; an integrity verdict blocks rounds, a standalone repair
  promotes, a stale digest is refused, the resolving verdict endorses and growth resumes; an
  integrity finding also refuses resuming a round (nothing adopted).
  `test_staged_maintainer.py` (15): snapshot window recovery + triage pin holding the fold; action
  window mutations as one gated promoted maintenance run (a child without the broker refused);
  failed, unconcluded and empty windows abort; a timed-out agent's batch drained before the
  window is judged; a round inside the window refused at once; the whole pass (a round promoted
  during triage, fresh generation evidence, the repair promoted, the pin released) with a verdict
  recorded, a stale-digest verdict and a verdict over a range the pass did not show refused, no
  verdict, and an agent that left a tool running (its repair aborted); `run_command` reports and
  stops survivors; a window whose code changed does not promote; a failing conclusion is
  recovered before the growth block is judged. `test_store_pg_recovery.py` (18):
  adoption rules (current writer, parent, commit/config/extractor, verified artifacts, final runs,
  direct SQL), the purge queue, recovery order (stopped before drained before the status query),
  an unknown outcome mutates nothing, outcomes per status, a lost promotion reply stands, SIGTERM
  while stopping still drains and recovers, bounded housekeeping converges, attempt namespaces,
  and the v6 → v7 migration (runs by the real v6 code: owners backfilled, purge queue filled,
  v6 clients refused, a v6 run resumed by new code; a v6 shadow keeps identical digests/export and
  keeps replicating). `test_generation_review.py` (9): evidence contents and digest binding,
  configuration decisions, findings withholding endorsement until resolved (+ the outbox image),
  resolution only by a verdict covering a later (compensating) generation,
  the publication cap, contiguity/immutability against direct SQL, integrity blocking growth,
  verdict files, and the backfill from legacy acknowledgements. Step-1/2 contract tests now drive
  the review consumer through verdicts.
- **Internal adversarial review before hand-off** (a separate reviewer agent over the diff): five
  findings, all fixed with regressions — an integrity finding resolvable although the repair was
  aborted (the resolution rule above); a partially applied repair promotable after the supervisor
  stopped the agent's leftover tools (survivors fail the action); a maintenance run recording the
  wrong commit after the agent committed code (identity re-checked at conclusion); `--resume`
  skipping the integrity block (and resuming non-round runs); verdicts not tied to the evidence
  the pass showed. Also adopted: triage pins released on every path, a failing conclusion
  recovered before the growth block is judged, the review range capped at 200 generations.
- **Decisions the plan left open** (for review): one predicate selects every new path; the owner is
  a new column moved only by a logged adoption (the opener stays `writer_epoch`); resume refusals
  are checked in Python before adoption and again by the database; a resumed run's steps re-run
  idempotently in their own batch namespace instead of replaying numbered batches; discovery is
  never restarted on resume; `run_round` refuses to start while any run is unfinished (recovery
  stays an explicit or maintainer act, as with snapshots) but catches up a promoted generation's
  completion itself; aborted runs keep their row and are purged after 24 h; housekeeping is
  time-budgeted and piggybacks on promotions and recoveries rather than a new timer; standalone
  runs require the same identity (clean checkout) and the same gates as rounds (including the test
  suite); a maintenance run is promoted only when the action exited 0 with nothing left running
  and code/configuration unchanged; the triage pin expires after 8 h and is released without the
  writer; the review range is capped at 200 generations, its failures at 50 shown; verdicts are recorded by the maintainer from a file
  (the agent cannot take the writer inside the window); the review consumer is written only by
  verdicts and publication is capped at endorsement in the database; README statistics are not
  checked under PostgreSQL authority.
- **Deferred to step 5 (verification, backups, the rehearsal).** Generation-bound counters and
  ledger / derived-key / eligibility / artifact verification sweeps; periodic re-verification of
  verified versions; the `nk_basis_text` benchmark with accumulated unfolded generations (step-3
  note b); reference-checked garbage collection of versions; `backup_corpus` for `artifacts/` and
  PostgreSQL-aware backups; named recovery points and restore drills; metadata RPO ≤ 15 min and
  RTO ≤ 60 min demonstrated; alerts (backup health is only reported to the review now); a full
  throwaway PostgreSQL-authoritative rehearsal including rollback; the 160M-row benchmark.
- **Known limits.** A second SIGTERM while a failing staged round recovers escapes the recovery
  (the run stays open: fail-closed, the next `--recover` or maintainer pass aborts it); a round
  whose promotion reply was lost logs `run_failed` although its generation stands (the database
  is right; the next recovery records `staged_run_kept`); nothing acknowledges the `publication`
  consumer yet (its cap at endorsement becomes effective with stage 6's release publisher).
- **Deferred to step 6 (cutover).** The baseline generation-0 tool; switching the live record and
  the fence; making the frozen legacy data read-only; the cron/maintainer environment
  (`NEKAISE_STORE=postgres` + DSN/schema) and the maintainer's publication switch to the
  generation-range review (the code path exists and is selected by the record); the publication
  consumer's Parquet release (stage 6 of the plan). Also still open: the lease with heartbeat and
  fencing epoch before any second host writes; ownership and the review guards are consistency
  rules for one trusted host, not an authorization boundary.
- **Gates**: full suite 1195 passed / 206 skipped (PostgreSQL skipped), with PostgreSQL
  (`nekaise_test`) 1429 passed / 2 skipped (the two opt-in benchmarks); `py_compile` of scripts
  and tests clean. Nothing ran against the live schema, the live checkout, cron or the maintainer.

### Step 4, Codex review fixes (2026-09-25; verdict MERGE AFTER FIXES, six P2)

Each fix has a regression test. v7 is deployed nowhere but throwaway test schemas, so `V7_DDL`
itself was revised (the publication guard); once any schema is v7, a change ships as migration 8.

- **P2 1 — an unconfirmed stop never leads to an abort.** `store_broker.staged_round` marks the
  stop confirmed only after `stop_owned` returned. If it raised (a survivor after SIGKILL, an
  error), the broker is still drained but the run is NOT recovered: it stays open, which blocks
  every later round and standalone mutation until a recovery confirms the stop (`recover_staged`
  also raises before its status query when its own stop fails). An interrupt during the stop
  propagates unconfirmed, and recovery stops again, aborting only once that stop returned.
  Tests: interrupt then confirmed stop — the run is still open at the second stop and only then
  aborted; a survivor — the run stays open, `refuse_unfinished` refuses, `recover_staged` refuses
  to abort until the stop succeeds; two interrupts — the run stays open.
- **P2 2 — every staged lifecycle's processes are recoverable.** Each coordinator holds an
  ownership mark while its run is open (`round_recovery.OwnershipMark`,
  `workspace/run-owners/nekaise-run-owner.<run>`): an inheritable descriptor that every process
  it forks inherits (fork-only workers keep their parent's ORIGINAL environment in
  `/proc/<pid>/environ`, so a later tag cannot reach them), and a record of the coordinator's pid
  and start time. Standalone commands and the maintainer's action window also tag themselves
  (`NEKAISE_RUN_OWNER=<run>`, set before anything is exec'd, restored after; a recovery-only
  variable, so no pipeline step changes behaviour), so every child they exec — spawned
  extraction workers, the agent and its tools — carries it from exec. `round_processes` matches
  `NEKAISE_RUN_ID`, `NEKAISE_RUN_OWNER` or the mark; it skips only exited and non-dumpable
  processes and raises on any other read error. Found while testing: a fork of a dead
  coordinator also inherits its DATABASE SESSION, so the writer lock stays held and no recovery
  could take ownership — `stop_orphans_of_dead_coordinators` therefore runs before the writer
  is requested (run_round's staged modes, the maintainer's window, standalone commands) and
  stops the processes of runs whose mark names a coordinator that is no longer alive (a live
  coordinator's processes are never touched; an unreadable mark raises); the run's status is
  then decided under the writer as before. Maintenance window run ids gained a random suffix.
  Tests: a standalone loader SIGKILLed with its extraction workers up, and the maintainer
  SIGKILLed with its action agent and a fork of itself running (the writer lock demonstrably
  held by the fork) — both recovered by `run_round.py --recover latest` from another process,
  orphans stopped, run aborted, marks swept; a unit test of the fork/exec markers and of the
  dead-coordinator rule.
- **P2 3 — a standalone fetch completes its pipeline.** `staged_runs.DOWNSTREAM`: before a
  standalone fetch freezes, `prune_corpus.py --apply` and `clean_corpus.py` run as the run's
  children through its broker (sharing the loader's deferral handoff through `NEKAISE_RUN_ID`,
  as in a round); a standalone prune is followed by a clean. The gates are unchanged. Test: a
  standalone `build_corpus.py` is promoted with fetch and clean batches and its documents
  materialized.
- **P2 4 — discovery uses the configuration of the commit that runs.** The run is opened first;
  backend selection, finder arguments and validation come from its own view at sequence 0
  (`select_backends`), whose pinned configuration is the checkout's. Test: a committed rename of
  a finder (script and configuration) runs the new script in the next round, and a committed
  disable stops the next round from invoking it.
- **P2 5 — open findings stop publication.** The publication guard now writes the
  `review_state` row (a no-op update, allowed at trigger depth 2) and refuses any advance while a
  finding or integrity finding is open — including one raised about generations endorsed before
  it (an empty-range finding) — as well as beyond the endorsed generation.
  `generation_review.state` reports `publishable_through`. Because both a verdict and a
  publication advance write that row, they serialize under any isolation level. Tests: endorse
  through 1 with publication lagging, a retrospective integrity finding, the advance refused;
  after the compensating repair's resolving verdict it succeeds; a finding recorded while an
  advance is uncommitted waits for it.
- **P2 6 — `--skip-tests` is refused under PostgreSQL authority**, for rounds and `--resume`,
  before any run is opened or adopted (exit 2); the legacy file-store round is unchanged.
  `staged_gates()` always includes `tests`.
- **Test database.** PostgreSQL tests now run on a separate throwaway cluster without WAL
  archiving (test WAL was filling the backup SSD):
  `NEKAISE_PG_TEST_DSN="host=/home/zengp/.local/share/nekaise-pg-test/run dbname=nekaise_test"`;
  the benchmarks' default DSN points there. The `nekaise_test` database on the live cluster is
  no longer used.
- **Gates**: full suite 1195 passed / 217 skipped (PostgreSQL skipped), with PostgreSQL (the
  test cluster) 1440 passed / 2 skipped (the two opt-in benchmarks); `py_compile` clean.

### Step 4, Codex second review (2026-09-25): the six fixed; three new findings fixed

All three were in the process-ownership mechanism added for P2 2; it moved into its own module,
`scripts/run_ownership.py`, used only by staged (PostgreSQL) recovery.

- **P2 — legacy recovery is byte-for-byte main's again.** `round_recovery.py` is main's file plus
  the appended staged section: `round_processes`, `stop_owned`, `stop_processes` and
  `descendants` are identical (NEKAISE_RUN_ID only, the same error handling). Staged recovery opts
  into `run_ownership` explicitly, and staged ownership is scoped to a coordinator ATTEMPT of a
  run of a root: the mark records the root key (sha256 of the resolved root), the run id and a
  random nonce; the tag is `NEKAISE_RUN_OWNER=<root key>:<run>:<nonce>` (a round's children get it
  through `StagedRound.owner_env()`, standalone commands and the maintainer's window set it in
  their own environment before they exec anything); a descriptor counts only when it is the
  attempt's mark file itself, compared by (st_dev, st_ino). Tests: the four legacy functions
  equal main's source; legacy matching ignores the staged tag and mark descriptors; another
  root's attempt of the same run id and an earlier attempt of the same run never match.
- **P2 — liveness reads identity and state together.** A coordinator is alive only when ONE read
  of `/proc/<pid>/stat` shows the recorded start time AND a state other than zombie/dead; there
  is no self-pid shortcut (the start time decides for this process too). The mark also records
  the boot id and the PID namespace: another boot means the coordinator is dead; another
  namespace raises (refused: an operator recovers it from there). Tests: a zombie coordinator
  whose fork holds the attempt is judged dead and its worker stopped; a mark naming this
  process's pid with another start time is dead; boot and namespace mismatches.
- **P2 — a sweep can no longer act on a newly installed attempt.** The lifecycle lock
  (`run_ownership.lifecycle`, an flock under `workspace/run-owners/`) is taken BEFORE the
  database writer by every staged coordinator start and every sweep (run_round's staged modes,
  the maintainer's window and its out-of-window recovery, standalone commands) and held across
  sweep → writer → mark installation (`staged_round(lifecycle=…)` releases it right after the
  mark is written; `--recover` and the maintainer's snapshot phase hold it throughout). Signals
  go through pidfds: each pid is opened, re-verified as the attempt's while its pidfd shows it
  alive, and only then signalled (SIGTERM, SIGKILL after the grace, survivors raise), so a
  reused pid is never hit; a sweep bound to attempt N never matches attempt N+1 (nonce, inode).
  A run left unfinished keeps its mark (and a failed recovery too) so a later recovery finds its
  survivors; a decided run's mark is removed. Tests: sweep A paused between verification and
  signal while B cannot take the lifecycle lock, then A stops only attempt N's process and B's new
  attempt is untouched (B's coordinator alive); a pid that exited between scan and signal and a
  process that does not belong to the attempt are never signalled.
- **Gates**: full suite 1198 passed / 222 skipped (PostgreSQL skipped), with PostgreSQL (the test
  cluster) 1448 passed / 2 skipped (the two opt-in benchmarks); `py_compile` clean.

### Step 4, Codex third review (2026-09-25): one P2 fixed

- **P2 — the lifecycle lock was released at once in `run_round`.** `staged_main` called
  `run_ownership.lifecycle(...).__enter__()` and dropped the generator context manager, which
  CPython finalized immediately (its `finally` released the lock) — so the sweep and the writer
  acquisition ran without it, reopening the sweep-vs-new-attempt race. The lock is now held by an
  ExitStack for the whole staged command (released early only by `staged_round` once the new
  mark is installed). Regression: through the real `run_round.main()` in round, `--resume` and
  `--recover` modes, a second descriptor's non-blocking flock proves the lock is held during the
  sweep and during the writer acquisition, and free after the command returns (all three fail
  on a6408cb4d9). No other dropped `.__enter__()` exists in scripts/; the one in a test
  (expected to raise) now uses `with`.
- **Gates**: full suite 1198 passed / 225 skipped (PostgreSQL skipped), with PostgreSQL (the test
  cluster) 1451 passed / 2 skipped; `py_compile` clean.

## Persistent-identifier membership (OpenAlex phase 1, 2026-09-25)

`ReadView.known_pids(pids)` answers which normalized persistent identifiers (`doi:10.x/y`,
`openalex:W…`; `state_codec.normalize_pid`) any registry or manifest row declares, as its
`persistent_id` or among its `origin_ids` aliases (string or list, `state_codec.row_pids`).
Discovery and the serial proposal merge use it (`dedup.Keys.identity_known`). It is purely
additive: no schema change and no migration.

- **FileStore:** kind `pid` in the SQLite acceleration index (`corpus_index.INDEX_SCHEMA` 3);
  write views with local changes scan their rows. A lookup that finds a rebuild in progress waits
  for it (`NEKAISE_INDEX_WAIT`, default 900 s) and then fails; it never falls back to parsing every
  shard. `run_round` rebuilds the index once under the round lock before finders fan out
  (`warm_index`).
- **PgStore:** an UNINDEXED expression lookup over the rows' JSON (`row -> 'persistent_id'`,
  `row -> 'origin_ids'`, decoded natively and verified exactly in Python). It reads through the
  same visibility source as every other lookup, so staged revisions and tombstones behave as in
  `known()`. Each call sequentially scans `entries` and `manifest`; callers batch by page.

**REQUIRED before PostgreSQL becomes authoritative:** indexed PID membership (for example a
normalized-identifier side table or expression index maintained with the rows) that keeps the
staged-revision and deleted-row visibility of this lookup, and passes the same contract tests
(`test_known_pids_*`, `test_store_pg_staging` snapshots). The index belongs in a NEW migration
after v7, never an edit of an existing one.

## Collect-all directive: classified views and backup scope (2026-09-25)

Operator directive 2026-09-25: collect all raw data regardless of licence; the licence classifies
the bytes into a use class and view (`registry.use_class`, `corpus_path_for`), it never decides
collection. Storage consequences:

- Artifact identities stay `(stage, sha256)` with stages exactly `raw`, `text`, `corpus`; a licence
  is never part of an identity. `corpus_*` fields describe a cleaned artifact, not admission to
  the default training view. Reclassification rewrites a row's `corpus_path` (same
  `corpus_sha256`) and moves the materialized file between views; raw/text paths and hashes
  never change and no bytes are erased.
- Views: `corpus/` (the open class, the training default) and `collection/<class>/corpus/` for
  every restricted-use class and policy hold; `collection/<class>/{raw,text}/` are rebuildable
  hard-link views of the canonical originals. Materialization stamps carry view, stage,
  classification-policy version, dataset and generation; `after_promotion` refreshes every
  cleaned view (classified first), `materialization_current` requires all of them.
- **Backup scope (operator, 2026-09-25):** the external SSD stores the cleaned corpus only — the
  default view, every classified cleaned view, and the provenance metadata — as the existing
  verified tar archives with 7-copy retention (format 2 archives carry `SCOPE.json`; an older
  corpus-only archive no longer counts as fresh). A capacity guard keeps
  `max(64 GiB, 3 %)` of the drive free for the PostgreSQL WAL and base backups, and the scheduler
  now goes through `locked_backup()` (the file-authority check under the round lock).
- **Open question, out of scope for the SSD:** `raw/` (746 GB) and `text/` (82 GB) have no backup
  today. Their durability belongs to stage 5 (content-addressed packs / private S3 with
  independent deletion protection, section 5); until then a lost disk loses the originals, and
  cleaned views can be rebuilt only from surviving text.

## Stage 4, step 5 record: verification, backups and the rehearsal (2026-09-25)

Plan decided by Codex: "Complete verification, backups, and the rehearsal. Replace README-current-
count and shard-layout production contracts with generation-bound counters, ledger validation,
derived-key checks, eligibility, and artifact consistency. Remove SQLite index gates under PG; pin
vendor validation too. Keep Git CI runnable with fixtures. Retain full integrity sweeps alongside
changed-artifact checks. Keep daily bases and weekly restore drills; retain 35 days of recoverable
WAL/base coverage. Upgrade drills from counts/shadow watermark to a named recovery point with
generation, revisions, configuration, events, outbox, and canonical digests. Wait for confirmed
WAL archival instead of the current fixed sleep. Set metadata RPO ≤15 minutes; metadata service
RTO ≤60 minutes, demonstrated by restoration. Report payload-backup freshness separately; stage 5
closes that durability gap. Alert on archive lag, backup/drill age, capacity, stalled runs,
promotion latency, materialization lag, and review backlog. Block growth when recoverability
exceeds the RPO budget. Gate: throwaway PG-authoritative discovery→fetch→prune→clean→all-gates→
promotion, failure/kill/recovery, maintainer review, restore, and PG→FileStore rollback. Require
bounded-memory scaling and the ADR's 160M-row/400-document metadata p95 <30 seconds benchmark.
Rollback: fix failures while production remains FileStore."

Production is unchanged: FileStore stays authoritative (the review fixes add schema v8, below), and the
only new behaviour outside PostgreSQL authority is additive monitoring (a crontab snippet below,
NOT installed), a stronger restore drill that the existing Sunday cron line runs once this is
merged, the installed `prune --keep 7` line refusing to delete anything while the WAL chain has
a gap, and vendor validation reading the view's configuration (the same file under FileStore).
Every new gate path is selected by `staged_runs.staged_authority(st)`, as in step 4.

### Verification: generation-bound, per run, plus full sweeps

- **Per-run checks** (`scripts/verify_generation.py`, `run_checks(view, restrictions, root=…)`),
  run by the `contracts` gate against the frozen staged view (`view.stage` = (run, frozen seq)),
  bounded by the run's revisions — the keys come from the unique `(run_id, tbl, key, batch_seq)`
  index a page at a time, never from a scan of the corpus:
  * revision integrity and derived keys: every revision's `row_sha256` is the sha256 of its text,
    the text is canonical, the key is the row's id (entries/manifest), the URL digest (blocklist)
    or `<sha256 of the row>:<n, 10 digits>` (ledger), and the derived columns (url/title
    norm+key, sha256, legacy shard/topic) equal `store_pg.revision_keys(row)`; tombstones carry
    no text; the ledger and the blocklist are append-only (a tombstone there is an error);
  * ledger: every appended row has id/url/reason/pruned_at; its document is gone from the frozen
    registry AND manifest; a row marked `blocklisted` has its (normalized) URL in the frozen
    blocklist;
  * eligibility: no changed manifest row a restriction matches claims corpus data (the full
    aggregate `restricted_with_corpus_data` runs when the policy may have changed, below);
  * artifact consistency: every payload claim a changed row makes that its parent row did not
    make identically names a version held locally and registered for the run (versioned runs);
  * **generation-bound counters**: exact integers — rows per table, manifest rows per status,
    training-eligible documents, excluded rows, text/corpus characters (only integral JSON
    numbers are summed; a non-integral one is counted under `fractional`, never summed
    inexactly), topics, licences (corpus_stats' definitions and predicates). The gate computes
    the frozen state's counters as the parent generation's recorded counters + (new − old)
    contribution of every changed row; it recounts in full (server-side aggregates) when there
    is no parent, the parent has no recorded counters, or the run's configuration set differs
    from the parent's (a policy change changes eligibility everywhere — then the full
    eligibility aggregate runs as well). Counters that go negative fail the gate.
  * **Binding**: the gate hands its report back through `NEKAISE_GATE_REPORT` (a per-gate path
    the coordinator sets — `run_round.run_verify_parallel(record=…)` and
    `staged_runs.run_gates`, i.e. rounds, standalone runs and maintenance repairs alike); the
    coordinator records it in the gate receipt's detail, which is bound to (frozen seq, frozen
    digest). **Generation G's counters are therefore its run's passed contracts receipt at G's
    frozen state** (`recorded_counters(view, G)`) — no schema change. The report names the run
    and frozen sequence it describes; a reporting gate (`contracts`) that exits 0 without a
    readable report (missing, not JSON, > 256 KiB) or with a report about another run or
    sequence is recorded as FAILED (fail closed: a generation is never promoted without its
    counters). The `tests` gate gets no report path (its own tests run gates of their own).
- **`check_contracts.py` under PostgreSQL authority**: README statistics, `oversized_control_files`,
  the ledger shard files and the file pipeline/`VERIFY` shape are file-store contracts and run
  only under file authority (`file_layout_contract_errors`); under PostgreSQL the gate checks the
  staged gate set instead (`artifacts/check/contracts/lint/tests`, standalone == round, and **no
  SQLite `index` gate** in `STAGED_VERIFY`) plus the per-run checks above. Configuration contracts
  (backends, rotation, runtime state, eligibility-restricted backends, patent countries,
  suspended hosts, finders vs configuration) are unchanged, and **vendor validation now reads the
  view's pinned `vendors.json`** (`vendor_contract_errors(view.config_get())`) instead of the
  working tree. A committed PG view (an operator's invocation) reports the generation's recorded
  counters and runs the full restricted-rows aggregate. `--root DIR` checks another data root;
  `--full` forces the full checks inside a gate.
- **`lint_registry.py` under PostgreSQL authority**: inside a run's gate with the policy unchanged
  and a parent generation, it lints the run's changes (`changed_lint`: every changed entry, and
  the manifest row of every id changed in either table against its entry — so an entry removed
  under a surviving manifest row is still an orphan — and every eligibility restriction a changed
  entry matched before the run must still match some entry); otherwise (first generation,
  configuration change, `--full`, committed views) the whole state as before. `--root` for CI.
- **CI stays runnable without tracked data**: `tests/ci_fixture.py DIR` builds a data root from
  the repository's configuration and control state plus a synthetic registry/manifest/blocklist/
  ledger (one entry per eligibility restriction, README rendered by `update_readme_stats`); CI
  runs `lint_registry.py --root` and `check_contracts.py --root` on it next to the existing
  steps (`.github/workflows/ci.yml`); `tests/test_ci_fixture.py` runs the same and shows a
  violation is still caught. At stage 6 the fixture needs a committed copy of the control state
  (`rotation.json`, `backend_state.json`, `github_passes.json`) once those leave git.
- **Full integrity sweeps** (`scripts/integrity_sweep.py`, resumable, a bounded slice per
  invocation, state in `workspace/integrity-sweep.json` written atomically after every page, one
  sweep at a time under a named lock; a busy lock skips the slice; every invocation advances at
  least one page): `metadata` walks the whole dataset in key order, each slice in ONE snapshot of
  the CURRENT committed generation — every entry and manifest row (the lint checks, restricted
  rows with corpus data, every payload claim of a successful row resolvable here unless its host
  is fetch-suspended), every blocklist and ledger row, the derived columns of every physical row
  (projection tables and the revisions of the overlaid promoted runs) — and ends, in one snapshot,
  by recounting the current generation's counters with server-side aggregates and comparing them
  with its recorded ones (the per-run delta chain): the Python delta arithmetic and the SQL
  recount check each other; any difference is a failure. **Nothing is pinned**: an earlier design
  pinned the pass's generation, which holds the fold back for the whole pass (every later
  generation stays an overlay); the basis benchmark below shows what that costs, and the internal
  review flagged it, so a pass reads whatever generation is current and a row changed behind its
  cursor is checked by the next pass (its run's gates checked it already). `artifacts` re-hashes
  every registered local version against its identity and size (read-only: periodic
  re-verification; the artifact gate verified each once when first referenced). Under file
  authority both are no-ops (lint and contracts check everything every round). ops_health alerts
  on failures (critical) and on passes older than 8 days.
- **Reference-checked GC, dry run only** (`scripts/artifact_gc.py`; no code path deletes): a
  version is a candidate only when no manifest row claims it — in the projection or in any
  revision of a run that is not aborted (promoted revisions are every generation's history, so
  admitted originals and text are referenced forever, as section 5 requires) — no non-aborted
  run references it, nothing else links it (`st_nlink == 1`: materialized corpus/ files and
  adopted legacy files keep it) and it is older than 30 days (the ADR's grace, which also makes
  a run staging right now harmless). Bounded memory: the referenced (stage, sha256) stream from
  PostgreSQL in C-collation order (server-side sort, named cursor, checked to be sorted) merged
  with the local versions listed directory by directory in the same order; only the
  content-addressed layout counts (`<stage>/<sha[:2]>/<sha[2:4]>/<sha>`, regular files) —
  anything else found there is reported as misplaced, never as collectable. Report:
  `workspace/artifact-gc-report.json`.

### Backups and recovery

- **Retention** (`pg_backup.py prune --retain-days 35 --daily-days 7`, pure `retention_plan`):
  every base of the last 7 days, the newest base of each ISO week back to 35 days, the anchor
  (the newest base at least 35 days old) and always the newest base; WAL older than the oldest
  kept base's start segment is removed; nothing is removed while the WAL chain from the oldest
  kept base has a gap (reported instead). `--dry-run` prints the plan. The legacy `--keep N`
  stays for the installed cron line (now also refusing a broken chain). Tested: coverage of
  every point of the last 35 days by a kept base, the anchor, weekly thinning, gaps.
- **The drill** (`pg_backup.py restore-test`, the Sunday cron line): under the store's writer
  advisory lock (so no store writer — a round's batch, a promotion, a shadow sync — commits in
  between) a REPEATABLE READ snapshot is taken and `pg_create_restore_point('drill-<UTC>')`
  plus `pg_switch_wal()` are issued (the segment holding the point is named by the server,
  `pg_walfile_name`, so the timeline and a record ending on a segment boundary are right); the
  lock is released at once and the live **recovery fingerprint** of the locked schema is
  computed in the snapshot: every base table's row count and an
  order-independent digest (four 64-bit column sums of sha256 over each row's text, computed in
  the server), plus the full content of the small control tables — i.e. the generation
  (dataset, generations), revisions, batches, runs, configuration sets and blobs, events, outbox
  and consumers, review state, and the projection tables. The drill then **waits for the WAL
  segment holding the restore point to be in the archive** (the file itself; timeout 15 min →
  the drill fails and names the archiver's last failure), verifies and extracts the newest base
  into a scratch directory, recovers it on a private socket with `recovery_target_name` (a
  restore_command that also reads `.zst` segments; the drill's settings are appended to
  `postgresql.auto.conf`, which is read last, and `archive_mode=off` is also given on the
  command line — a promoted restore must never push a new timeline into the live archive, even
  if the source had an ALTER SYSTEM archive_command), and compares the restored fingerprint.
  Every drill appends a record to `logs/pg-drills.jsonl` (base, target, LSN, archive wait,
  segments replayed, phase timings, RTO, mismatches, error); a mismatch, an RTO above 60 minutes
  or any failure fails the drill (exit 1). One drill at a time (an flock in the scratch
  directory); SIGTERM (a cron `timeout`) unwinds it — the scratch instance is stopped and the
  failure recorded — and a drill first stops and removes what a SIGKILLed one left behind.
- **Measured on the live cluster, 2026-09-25** (read-only for live: a restore point, a WAL switch
  and a snapshot; the restore ran in a scratch directory on a private socket). Final drill, with
  the reviewed code, on a quiet host: base `20260925T013001Z` (12.1 h old) to recovery point
  `drill-20260925T133716Z` (LSN 1E/96000090) — **fingerprint identical** (entries 1,623,555,
  manifest 1,623,545, blocklist 140,295, ledger 115,984, events 1,814, schema v7, shadow
  watermark eda5681047); the restore point's segment was in the archive **18.4 s after the WAL
  switch** (confirmed by the file, before the 19 s live fingerprint finished); `pg_verifybackup`
  71.8 s, extraction 95.7 s, replay of **3,975 segments (67 GB)** 638.3 s, restored fingerprint
  19.0 s: **RTO 824.9 s = 13.7 min** (budget 60 min). The first drill (the pre-review code, same
  protocol) measured 846.2 s over 3,950 segments; a second one, run while the 160M benchmark
  loaded 437 GB on the same NVMe, replayed at a third of the speed and was killed by its own
  `timeout` at 58 min (which is how the SIGTERM handling above was found). The replay is
  dominated by the 113 GB of test-database WAL archived on 09-24/25 before tests moved to their
  own cluster; a normal day archives ≈ 200 segments, ≈ 35 s of replay at the measured ≈ 6
  segments/s, so with daily bases the RTO is ≈ 4–5 min (verify + extract + replay +
  fingerprint) plus switching the service over.
- **Drill under representative growth load** (after the review fixes, with the new cleanup and
  retention lock): `drill-20260925T143446Z`, 14:34:46–14:48:43 UTC, while the live cron round
  `20260925T142601Z-3dbc0595` ran its prune (from 14:34:06), clean, README stats and all five
  file gates concurrently (check, index, lint, contracts, the test suite, from 14:40:35) on the
  same host: **fingerprint identical** (entries 1,623,816, manifest 1,623,806, blocklist 140,305,
  ledger 115,994, events 1,856), segment archived 18.0 s after the switch, verify 71.8 s,
  extraction 96.7 s, replay of 3,984 segments 649.1 s, fingerprint 19.0 s: **restore time
  836.5 s = 13.9 min**. These are RESTORE times: they exclude the service cutover (moving the
  restored directory into place or repointing `nekaise-postgres` and the store DSN, restarting,
  and the first round's checks — an operator procedure, not yet rehearsed), so they demonstrate
  the 60-minute RTO for restoring the metadata, not an end-to-end operational RTO. Step 6 should
  rehearse the cutover part once on a scratch instance before the claim is made.
- **RPO**: `pg_backup.py rpo-probe` writes a WAL record (a non-transactional logical message: no
  table changes) and times until its segment is in the archive: 69.5 s, 302.7 s, 298.1 s and
  302.6 s on the live cluster (11:05–11:21 UTC). These are OBSERVED archive-arrival latencies of
  a healthy archiver — about archive_timeout (300 s) + the copy — not a proven worst-case bound:
  under failures (the SSD unmounted, the archive_command failing, the archiver stalled) the loss
  window grows until someone acts. What bounds it operationally is the growth block and the
  alert, which fire when the exposure — (age of the oldest `.ready` segment) + archive_timeout
  (`pg_backup.exposure`) — exceeds the 15-minute budget, or the archive stops holding what the
  server reports archived; the block stops growth then, so no NEW metadata is produced at risk.
- **Backup-disk budget (operator note, 2026-09-25: the 1.8 TB SSD is for corpus backups — about 7
  tars of ~22 GB — and metadata backups must fit a sensible share, ≤ 200 GB)**. Measured live WAL
  after the test clusters moved away (10:45–15:52 local, drills and probes included): 41 segments
  in 5.1 h ≈ 3.2 GB/day
  (16 MiB per segment however empty: archive_timeout switches a segment every 5 min whenever
  anything was written, so continuous rounds put a floor of 288 × 16 MiB ≈ 4.8 GB/day). Bases
  are 3.1 GB compressed; the plan keeps ≈ 13 (7 daily, ≤ 5 weekly, the anchor) ≈ 40 GB. 35 days
  therefore need ≈ 112–169 GB of WAL + 40 GB of bases = **≈ 152–209 GB: within the 200 GB share at
  today's rate, marginally over at the continuous-activity floor.** ops_health's
  `backup_capacity` check compares the projection (last-24 h rate × 35 + bases) with the
  200 GB budget and, when over, reports the retention that fits ((200 − bases) / rate: ≈ 33
  days at the floor) instead of silently filling the disk. Proposals for the coordinator
  (decisions on live configuration, not taken here): (1) one-time, after the 2026-09-26 03:30
  base: `pg_backup.py prune --keep 1` to drop the 113 GB of test-database WAL (it covers only
  the shadow period, when git held the authoritative history), then install the 35-day line;
  (2) compress archived segments in `archive_wal.sh` (zstd; forced-switch segments are mostly
  zero-filled, and the drill's restore_command already reads `.zst`), which would cut the WAL
  share to a fraction; (3) keep archive_timeout at 300 s (the RPO bound depends on it). Until
  (1), the 24-hour rate includes the test WAL and the capacity check warns.
- **Recoverability growth block** (`ops_health.recoverability_block`): a staged round
  (`_complete_previous`) and `--resume` refuse, and the maintainer's staged block reasons
  include it, while archive_mode is off, archive_timeout is 0, the exposure exceeds 15 minutes,
  the segment the server last archived is not in the archive the restores read (an
  archive_command writing elsewhere "succeeds"), the server's timeline differs from the newest
  base's (a new base is needed after a timeline switch), no base exists, the WAL chain after the
  newest base has a gap, or any of these facts cannot be read (the archiver's ready list needs
  superuser or pg_monitor for the store's role); a failure to evaluate is itself a reason.
  Repairs (standalone and maintenance runs) are not refused. The test cluster archives nothing,
  so tests waive the block (conftest, and `NEKAISE_TEST_WAIVE_RECOVERABILITY` through
  `tests/authority_site`); `tests/test_ops_health.py` and the rehearsal check the block itself.
- **Alerts** (`scripts/ops_health.py check`, every 5 min in the snippet): archive (exposure vs
  RPO, failing archiver, archive mode), base age (26/50 h), WAL chain gaps, the 35-day
  retention target (once drills have run longer than the window), drill (missing, failed, RTO >
  60 min, age 8/15 days), backup capacity (free fraction, 35-day projection vs the 200 GB
  budget), data-disk capacity, **payload backups apart from the metadata RPO**
  (backup_schedule's status; stage 5 closes that durability gap), stalled runs (3/12 h),
  promotion latency (a frozen run waiting > 30 min, no promotion for 6 h, open→promotion p95),
  materialization lag (corpus/ not complete at the current generation 30 min after its
  promotion), **fold lag** (promoted generations not yet folded: 32/128, with the active
  retention pins — see the basis benchmark), review backlog (> 200 unreviewed generations or
  24 h; open findings; integrity findings are critical) and the integrity sweeps (not
  applicable under file authority). A check that cannot read its facts is critical. State `workspace/ops-health.json` (atomic); every severity change is appended to
  `logs/alerts.jsonl`; the maintainer's snapshot carries the non-ok checks (`ops_health`), the
  generation-range review's backup evidence now also has the recoverability exposure, the newest
  drill and the payload-backup freshness.

### Rollback: PostgreSQL → FileStore

`scripts/rollback_export.py` (the sharded FileStore materializer step 6's rollback needs):
`export --target DIR [--generation G]` streams ONE snapshot of G into a fresh tree — entries
routed to their registry shards (header + entries in id order), manifest shards in legacy
(shard, topic, id) order written start to end, blocklist, ledger shards, the journal events in
sequence order rolled per UTC day like FileStore's, rotation/backend_state/github_passes, and the
configuration documents as G pinned them (exact bytes from the sealed set) — then renames the
temporary tree to DIR and **verifies**: DIR opened as a FileStore passes `validate_layout` and its
canonical export (`store.export`) equals G's, file digest by file digest, or the export fails.
`payloads --target ROOT` links every raw/text claim of G whose version is held into its legacy
path (whatever file was there is first adopted as a version; atomic rename), requires corpus/ to
be G's complete materialization, sets `corpus/.ruleset` to G's ruleset, and fails if a claim is
held nowhere. Memory: one page plus one open file per registry shard (the FileStore side of the
verification loads its tables, the file store's own accepted cost — this is the fallback-window
tool for today's size, not a scale path).

### The rehearsal (`tests/test_rehearsal.py`, real processes, throwaway checkout and schema)

One PostgreSQL-authoritative checkout: (1) a round — discovery (fake finder), fetch (local HTTP
server), prune (a thin document dropped, its ledger row), clean, every gate (the contracts
receipt carries the full counters, equal to a recount), promotion → generation 0; (2) a round with
a failing test gate is aborted; (3) a round SIGKILLed mid-fetch leaves an orphaned fetch, `--recover
latest` stops it and aborts the run, the next round promotes generation 1 with **delta** counters
equal to a recount; (4) with the test waiver removed, a round is refused ("growth blocked:
recoverability: archive_mode is off") before anything is staged; (5) `generation_review.py
evidence` + `record ok` over [0, 1] → reviewed and endorsed through 1; (6) `integrity_sweep.py
metadata` (recorded counters equal) and `artifacts` pass, `artifact_gc.py` finds no candidate and
deletes nothing; (7) **restore**: the schema's fingerprint and canonical export are identical in a
temporary cluster — a physical base backup (`pg_basebackup -X stream`) when the cluster allows
replication (run once on a throwaway replication-capable cluster: identical, 2.3 s), else a
logical dump (the shared test cluster runs `wal_level=minimal`); (8) **rollback**: the latest
generation exported and verified, payloads linked, the database half and the host record switched
back to `file` (lifting the cutover fence), the tree committed, and a legacy file-store round
(discovery of one more document, fetch, prune, clean, README stats, the `check`, `index`, `lint`,
`contracts` and `tests` gates, a commit) succeeds over it; the old PostgreSQL-bound store can no
longer write. The live point-in-time path is the drill above.

### Benchmarks

`tests/test_scale_bench.py` (opt-in; `scale` and `basis` modes; refuses the live cluster's
socket and database name, parsed as libpq parses a DSN). **Hardware**: Intel Xeon w7-3445 (40
threads), 125 GB RAM, KIOXIA 4 TB NVMe (data), Linux 6.12, PostgreSQL 18.6 in a throwaway
cluster with the live cluster's settings (shared_buffers 8 GB, effective_cache_size 48 GB,
work_mem 64 MB, maintenance_work_mem 2 GB, max_wal_size 16 GB, wal_compression zstd, **fsync
and synchronous_commit on**, wal_level replica; archive_mode off), removed afterwards. The
throwaway test cluster (fsync off) was not used for the numbers.

**The ADR's acceptance benchmark — 160M synthetic documents, schema v8** (rerun after the review
with indexed persistent-id membership: 160,000,000 entries + 160,000,000 manifest rows = 320M
metadata rows, each carrying a DOI `persistent_id` and its `pids`; 562 GB database; ~650-byte
rows), generated in the server by 16 sessions (manifest 60 min, entries 24 min) and indexed with
the schema's own definitions (url/title keys, sha256, legacy order and the two `pids` GIN
indexes: 59 min; populate 8,615 s in all), sample rows checked canonical with exact derived
columns including `pids`; a baseline generation whose contracts receipt carries the full recount
(**one-pass recount of 160M documents: 349 s**); then 20 rounds of a typical 400-document
round's metadata work (discovery with dedup over URLs/titles/ids AND `known_pids()` over 400
persistent identifiers — 200 new, 200 already present, all 200 found every round — 16 loader
checkpoints, a 100-document prune with blocklist + ledger + 300 metric updates, a 400-row cleaner
patch, freeze, the contracts + lint checks of the frozen run in delta mode, gate receipts,
promotion and the fold):

| per round (n = 20) | p50 | p95 | max |
|---|---|---|---|
| discovery: `known_pids()` of 400 identifiers | 0.770 s | 1.526 s | 1.526 s |
| discovery: known() over 400 URLs + titles + ids, with the above | 1.097 s | 1.716 s | 1.716 s |
| discovery merge (persisted + applied) | 0.054 s | 0.081 s | 0.081 s |
| loader checkpoint of 25 rows (n = 320) | 0.022 s | 0.027 s | 0.054 s |
| prune: 100 dropped + blocklist + ledger + 300 updates | 0.245 s | 0.570 s | 0.570 s |
| cleaner patch of 400 rows | 0.069 s | 0.079 s | 0.079 s |
| freeze | 0.003 s | 0.004 s | 0.004 s |
| contracts + lint checks at the frozen state (delta counters) | 0.232 s | 0.637 s | 0.637 s |
| gate receipts / promotion | 0.005 / 0.004 s | 0.068 / 0.023 s | |
| fold of the promoted generation | 0.499 s | 1.044 s | 1.044 s |
| **whole round's metadata work** | **2.82 s** | **4.00 s** | **4.00 s** |

**p95 4.00 s against the 30 s budget** (before v8, without `known_pids()`: p95 2.44 s); the
benchmark process's peak RSS was **56 MB** (bounded: every step pages; nothing holds a table).
The rounds ran on the populated schema kept from the load (`--reuse-schema`): the first attempt
found `known_pids()` planned as a sequential scan of 160M rows (a `pids && <400 values>` filter is
estimated to match most of the table), which is why it now probes the GIN index once per
identifier and reads the visible rows by primary key (`LATERAL … OFFSET 0`, as the artifact
checks do; a test pins the plan).

**Scope — what this benchmark covers and what it does not.** It measures the store's metadata
work of a round: staging, the per-run checks, promotion and the fold. The runs were
`artifacts="unchecked"`: no versioned artifact metadata (no `run_artifacts` registration, no
claim checks at sealing, no artifact gate re-hashing), no payload bytes, network or extraction,
no subprocess start-up, no test-suite gate, and not the full `check` gate
(`clean_corpus.py --check`, which still walks every manifest row — step 6). The versioned
artifact path was measured separately at 1.62M documents (step 3: a versioned 400-row cleaner
patch ≈ 60 ms, a 25-row versioned checkpoint 33 ms, the artifact gate 1.4 s per 20,000
versions); it has not been measured at 160M.

**nk_basis_text and reads with accumulated unfolded generations** (`basis` mode; 1.62M
documents; K generations promoted and NOT folded, each revising the same 400 documents — the worst
case, a key's history grows by one revision per generation — or 400 fresh documents each —
the typical case; then a VERSIONED 400-row patch with unchanged claims, so sealing looks up
every basis row, and the overlay's reads):

| unfolded generations K | hot: versioned 400-row patch (seal) | hot: 25-id lookup · known() of 400 URLs · first scan page | disjoint: patch | disjoint: lookup · known · first page |
|---|---|---|---|---|
| 0 | 0.099 s | 0.002 · 0.016 · 0.029 s | 0.101 s | 0.002 · 0.022 · 0.031 s |
| 1 | 0.110 s | 0.003 · 0.020 · 0.026 s | 0.108 s | 0.004 · 0.020 · 0.030 s |
| 16 | 0.163 s | 0.007 · 0.079 · 0.072 s | 0.118 s | 0.003 · 0.024 · 0.056 s |
| 64 | 11.7 s | 0.066 · 0.75 · 14.3 s | 0.121 s | 0.004 · 0.022 · 0.144 s |
| 65 (visibility by subquery) | 10.2 s | 0.122 · 1.63 · 15.1 s | 0.121 s | 0.003 · 0.023 · 0.172 s |
| 128 | 5.6 s | 0.050 · 0.34 · 4.4 s | 0.253 s | 0.004 · 0.023 · 0.075 s |
| 256 | (not measured: building it took > 1 h) | | 0.450 s | 0.004 · 0.022 · 0.083 s |

(K = 256 in the hot case was measured once while the 160M load ran concurrently: patch 28.1 s,
lookup 0.89 s, known 11.6 s, first page 25.0 s.) The disjoint (typical) case stays flat for
reads and grows the seal by ~1.4 ms per unfolded generation; the hot case is superlinear — each
of a key's K unfolded revisions is checked against the others for supersession, each check
evaluating the K-run visibility — and plan-dependent (K = 64 was slower than K = 128 after the
statistics refresh at 128).

What it means: with the fold running after every promotion K stays at 0–1 and none of this
matters (the 160M rounds above fold every generation). What can hold the fold back is a
retention pin — the maintainer's triage pin (held for the pass, ≤ 40 min by its time budgets,
expiring after 8 h if the maintainer dies) — or a stuck fold. The keys every round revises are
small-table rows (the rotation cursors, ~30 keys), for which even K ≈ 100 costs milliseconds;
manifest rows are revised by one generation at a time. Consequences taken here: the metadata
sweep pins nothing, and ops_health alerts on fold lag (warning above 32 unfolded generations,
critical above 128). If long pins ever become normal, the overlay's supersession check needs a
per-key "latest visible revision" form (e.g. DISTINCT ON over (rank, batch)) — noted for stage 5,
not needed for the cutover.

### Persistent-id membership: indexed (schema v8)

Shipped after the review (below: "Indexed persistent-id membership: schema v8"); it is no longer
a pre-cutover requirement. The live shadow migrates 7 → 8 on its next `pg_shadow sync` after
the merge (12,808 entries and 12,808 manifest rows backfilled), like every earlier migration.

### Tests and gates

- New tests: `tests/test_pg_backup.py` (10: segment arithmetic, the retention plan's coverage /
  anchor / weekly thinning, chain gaps with `.zst` segments, prune refusing a broken chain and a
  dry run touching nothing, the exposure bound, fingerprint diffs, a drill without a base, the
  archival wait timing out, one drill at a time and leftovers swept), `tests/test_ops_health.py`
  (every threshold, the metadata budget and the retention that fits, payload backups apart from
  the RPO, sweep failures, alert transitions, the recoverability block failing closed in every
  way including a crash-free message, an archive the restores do not read, a timeline switch,
  gaps, an unreadable archiver, no base; the retention-target warning),
  `tests/test_verify_generation.py` (20 with PostgreSQL: the row arithmetic against
  corpus_stats on varied rows (floats, null/missing fields, pointer-only licences, restrictions,
  status-less and failed rows); counters chained generation to generation (delta equal to a
  recount, non-integral counts kept apart), a recount without recorded parent counters or after
  a configuration change; restricted rows with corpus data; the ledger rules; revision digests
  and derived keys corrupted behind the store's back; versioned claims held and registered; the
  one-pass recount against corpus_stats and the row arithmetic; injective labels; gate reports
  bound to the frozen state; incremental lint catching a restriction the run emptied; the
  metadata sweep resuming page by page and finding a damaged projection column; a pass spanning
  promotions and folds without holding them; artifact re-verification finding same-size
  damage; the GC report reference-checked, deleting nothing and skipping misplaced files; the
  rollback export verified, refusing an existing target and detecting a tampered tree; lifecycle
  alerts over a staged schema), `tests/test_rehearsal.py` (the rehearsal above),
  `tests/test_ci_fixture.py` (2), and `tests/test_scale_bench.py` (opt-in).
- conftest: every test gets private pg_backup paths (socket, bases, WAL archive, drill log,
  scratch) and private ops_health state files, so no test can reach the live cluster, the backup
  SSD or the checkout's workspace; `tests/staged_world.py` gives real child processes the same
  through the environment; the recoverability block is waived only where the throwaway cluster
  cannot archive (`@pytest.mark.recoverability` opts out); `NEKAISE_GATE_REPORT` is cleared.
- **Internal adversarial review before hand-off** (a separate reviewer agent over the diff; no P1):
  fixed with regressions — the block's message crashed on an unset ready age (now a reason; the
  maintainer's block never raises), an archive_command writing elsewhere passed the block (the
  last archived segment must be in the archive), WAL segment names assumed timeline 1 and
  mis-named boundary LSNs (the server names them now), a promoted drill instance could archive
  into the live archive through the base's `postgresql.auto.conf` (settings now go last and
  `archive_mode=off` on the command line), the sweep's pin held the fold back and could be
  orphaned (the sweep pins nothing now), the benchmark's live-database guard was a regex (now
  libpq-parsed, refusing the live socket), plus: children of staged tests could read the live
  backup paths, the gate-report variable leaked into the tests gate, reports were not bound to
  the frozen state, committed-view contracts skipped the restricted-rows aggregate, incremental
  lint dropped the "restriction matches nothing" check, delta/full labels could collide, the GC
  merge trusted the directory layout and its isolation level was ignored, a slow drill passed,
  the drill fingerprinted more schemas than it locked, the 35-day target was never alerted on,
  a resume refused on a transient archive outage advised aborting, completion work stopped
  during an outage, the rollback export was not synced before its rename, and tests used fixed
  run ids. Also found while measuring: the first drill run under the benchmark load outlived its
  `timeout` and left its scratch instance running (SIGTERM now unwinds the drill; leftovers are
  swept under the drill lock), and the recount made seven passes over the manifest (one now:
  360 s at 160M).
- **Gates**: full suite 1253 passed / 243 skipped (PostgreSQL skipped), with PostgreSQL (the
  test cluster) 1523 passed / 3 skipped (the three opt-in benchmarks); `py_compile` of scripts
  and tests clean; on the branch's real data (file authority) `lint_registry` OK (1,623,256
  entries in 115 shards, 1,623,246 manifest rows, 262 s) and `check_contracts` OK (1,615,187
  documents / 30 backends, 74 s). Against live only the read-only drills and RPO probes ran; no
  cron line was installed or changed.

### Deferred (with where it goes)

- Of step 4's list, closed here: generation-bound counters and the ledger / derived-key /
  eligibility / artifact checks, periodic re-verification, the `nk_basis_text` benchmark,
  reference-checked GC (as a report), named recovery points and drills, the RPO/RTO
  demonstration, alerts, the rehearsal including rollback, the 160M benchmark. `backup_corpus`
  for `artifacts/` and PostgreSQL-aware payload backups are NOT done here: another branch is
  expanding `backup_corpus.py`/`backup_schedule.py` now, and payload durability is stage 5's
  (ops_health reports payload-backup freshness apart from the metadata RPO meanwhile).
- Stage 5: pack/object locators and eviction; registering adopted legacy versions in PostgreSQL;
  an actual (reviewed) collector behind the GC report; a per-key latest-revision form of the
  overlay's supersession check if long retention pins become normal.
- Step 6 prerequisites (below): the baseline tool recording counters, the changed-rows-only
  `check` gate before large growth, a rehearsal of the service cutover part of the RTO.
- Unchanged: the lease with heartbeat and fencing epoch before any second host writes.

### Crontab snippet (NOT installed; `bash scripts/install_ops_cron.sh` installs it, `--print` shows it)

```
*/5 * * * * cd '/home/zengp/Code/nekaise-corpus' && /usr/bin/flock -n '/home/zengp/Code/nekaise-corpus/workspace/.ops-health-cron.lock' '/home/zengp/Code/nekaise-corpus/.venv/bin/python' scripts/ops_health.py check >> '/home/zengp/Code/nekaise-corpus/logs/ops-health.log' 2>&1  # nekaise-corpus ops health
23 * * * * cd '/home/zengp/Code/nekaise-corpus' && '/home/zengp/Code/nekaise-corpus/.venv/bin/python' scripts/integrity_sweep.py metadata --seconds 900 >> '/home/zengp/Code/nekaise-corpus/logs/integrity-sweep.log' 2>&1  # nekaise-corpus integrity sweep metadata
53 * * * * cd '/home/zengp/Code/nekaise-corpus' && '/home/zengp/Code/nekaise-corpus/.venv/bin/python' scripts/integrity_sweep.py artifacts --seconds 900 >> '/home/zengp/Code/nekaise-corpus/logs/integrity-sweep.log' 2>&1  # nekaise-corpus integrity sweep artifacts
10 6 * * 1 cd '/home/zengp/Code/nekaise-corpus' && '/home/zengp/Code/nekaise-corpus/.venv/bin/python' scripts/artifact_gc.py >> '/home/zengp/Code/nekaise-corpus/logs/artifact-gc.log' 2>&1  # nekaise-corpus artifact gc report
30 3 * * * cd '/home/zengp/Code/nekaise-corpus' && '/home/zengp/Code/nekaise-corpus/.venv/bin/python' scripts/pg_backup.py base >> '/home/zengp/Code/nekaise-corpus/logs/pg-backup.log' 2>&1 && '/home/zengp/Code/nekaise-corpus/.venv/bin/python' scripts/pg_backup.py prune --retain-days 35 --daily-days 7 >> '/home/zengp/Code/nekaise-corpus/logs/pg-backup.log' 2>&1  # nekaise-corpus pg base backup
```

The last line replaces the stage-2 `prune --keep 7` line (same tag); the Sunday 05:00 drill line
stays as it is.

**Operational decisions (Codex-aligned, 2026-09-25), all for the coordinator after the merge:**
(1) after the 2026-09-26 03:30 base, run `pg_backup.py prune --keep 1` ONCE to drop the 113 GB of
test-database WAL — only with this branch merged (the prune now refuses a missing prefix,
interior gap or tail); (2) enable zstd compression in `archive_wal.sh` only together with this
branch (prune now cleans `.zst` segments; restores read them); (3) install the monitoring lines
(ops_health, sweeps, GC report) only now that fixes 6–8 are in, i.e. after the merge; install
the 35-day retention line after (1).

### What step 6 (cutover) must do

Everything below is for the coordinator/operator; nothing of it ran against live.

1. **Hard pre-cutover requirements** (code, reviewed like any step):
   - the baseline generation-0 tool (step 1 decision (1), still TODO) must promote generation 0
     through a baseline run whose `contracts` receipt carries `verify_generation.full_counters`
     of the baseline (as `tests/test_scale_bench.py`'s `baseline()` does), so the first round
     chains its counters instead of recounting — and the baseline must be verified against the
     canonical export before promotion;
   - the `check` gate (`clean_corpus.py --check` in a staged view) still walks every manifest row
     per round; it is fine at today's 1.6M rows but must become changed-rows-only (with the whole
     check moved into the metadata sweep) before the corpus grows by an order of magnitude — left
     for the owner of `clean_corpus.py` (another branch is changing it now);
   - the store's database role keeps superuser or gets `pg_monitor` (the recoverability block and
     ops_health read `pg_ls_archive_statusdir`; without it growth is blocked, never allowed);
   - a rehearsal of the service-cutover part of the RTO (restore → repoint the service and DSN →
     first round's checks) on a scratch instance: the drills measure restoration only.
2. **Backups before the switch**: after the 2026-09-26 03:30 base, one `pg_backup.py prune
   --keep 1` drops the 113 GB of test-database WAL; decide on archive compression (zstd in
   `archive_wal.sh`, restore already reads `.zst`); install `scripts/install_ops_cron.sh` (the
   35-day retention line replaces the `prune --keep 7` line; ops_health every 5 min; sweeps; the
   weekly GC report); let one Sunday drill pass with the new code (it ran to completion three
   times by hand, once under a live round, see above) and ops_health show no critical check.
3. **The cutover itself** (plan item 6, unchanged): pause dig and maintainer crons, take the
   locks in the fixed order, drain/recover, pin commit C, final `pg_shadow sync` + `verify`,
   create and verify the baseline generation (1), disable the shadow timers, switch the database
   half (`PgStore.set_authority("postgres")`) and the host record (`store_authority.write_record`
   — it writes the fence), set `NEKAISE_STORE=postgres` + DSN/schema in every cron and maintainer
   environment, make the frozen legacy tracked data read-only (step 1 decision (2)), run ONE
   supervised round (`run_round.py --commit`), check its gate receipts (contracts `counter_mode`
   `delta`, counters equal to `integrity_sweep.py metadata` of that generation), the
   materialization and ops_health, then re-enable the crons. Stop data snapshots, per-round
   commits, journal files and README rewrites (the staged path already writes none).
4. **Rollback during the seven-day window** (rehearsed end to end in `tests/test_rehearsal.py`):
   fence writers (pause crons, take the locks), `rollback_export.py export --target DIR` of the
   latest promoted generation (it verifies itself: identical canonical export or it fails),
   `rollback_export.py payloads --target <checkout>` (verifies every raw/text claim by hash and
   size and corpus/ file by file BEFORE linking anything; any failure stops the rollback before
   the authority switch), switch the database half to `file` (`set_authority("file")`) and the host record
   (`write_record(..., "file", lift_fence=True)`), replace the tracked layout with the exported
   tree, render README stats, commit, and run one legacy round. Never restore a stale cutover
   copy.
5. **After seven healthy days** (plan item 7): retire the production file adapters; keep
   `rollback_export.py`, `pg_shadow.py` import/verify and `pg_backup.py`.


### Step 5, Codex review fixes (2026-09-25; verdict MERGE AFTER FIXES, two P1, eight P2)

Rebased onto main with the OpenAlex resolver (phase 1: `known_pids()`, rights checks in
`lint_registry.entry_errors`, conftest machine-state isolation — all kept). Each fix has a
regression test.

- **P1 1 — drill cleanup never signals a process it cannot identify.** `stop_instance` now reads
  the scratch directory's `postmaster.pid` (pid, data directory, start time) and `/proc/<pid>`
  (start time from `stat` + `btime`, `cmdline`, `cwd`): only a process whose argv[0] is
  `postgres`, whose working directory (or `-D`) is that data directory and whose start time is
  the recorded one is ever stopped (`pg_ctl stop -m fast`, then `-m immediate`); success means
  the process has actually EXITED (polled; pg_ctl's exit code is not trusted). A pid that now
  belongs to another process (reuse after a reboot) is never signalled; anything that cannot be
  read is "uncertain" and the scratch state is KEPT (reported in the drill record and the log)
  — in the leftover sweep and in the drill's own teardown. Tests: a stale pid naming a live
  unrelated process (not signalled), an unreadable pid file (kept), a stop that "succeeds" but
  leaves the process running (kept), a stop that works (removed).
- **P1 2 — WAL coverage has explicit endpoints.** `missing_segments(first, last)` checks the
  inclusive range (the start and the end included; a range crossing timelines or running
  backwards is unrecoverable, never "complete"); `base_range(base)` gives a base's Start-LSN and
  End-LSN segments from its manifest. Prune requires every segment from the oldest kept base's
  start through the newest kept base's END; the drill requires its base's start through the
  restore point's segment; recoverability requires the newest base's start through its end and
  on through the server's last archived segment. `chain_gaps` (open-ended at the newest archived
  file, so a start beyond it returned `[]`) is gone. Tests: missing prefix, interior gap, missing
  tail, the reviewed case (the newer base's whole range absent) and an empty archive — prune
  refuses and keeps the fallback base in every case.
- **P2 3 — prune respects a running drill.** `retention_lock` (an flock on
  `<bases>/.retention.lock`): the drill holds it shared from choosing its base until its scratch
  instance is gone; prune holds it exclusive, waiting at most `--lock-timeout` (3 h) and then
  refusing. Corpus rounds never take it. Test: a shared holder makes prune refuse, releasing it
  lets prune proceed.
- **P2 4 — compressed WAL is pruned.** `pg_archivecleanup -x .zst`: `.zst` segments are judged by
  their segment name like plain ones. Test: the real binary over a mixed archive removes older
  plain and compressed segments and keeps newer ones of both kinds.
- **P2 5 — the rollback verifies payloads before anything changes.** `verify_payloads` (run by
  `link_payloads` first; any failure raises with nothing changed, so the authority switch never
  happens on unverified payloads): every raw/text claim must be a held version whose bytes hash
  to the claim (raw: and have the row's size) or a readable REGULAR file at the legacy path with
  those bytes (a directory, symlink or device is never "legacy ok"; a claim without an identity
  fails); corpus/ must be a complete materialization of G by its stamp AND file by file — every
  member regular with the row's `corpus_sha256`, no other document file. Test (real round): a
  same-size damaged text version, a directory at a raw path, a legacy file with wrong bytes, a
  tampered and an extra corpus file — each refused with raw/text unchanged and authority still
  PostgreSQL; the right bytes are then accepted and linked.
- **P2 6 — sweep failures in progress are failures.** `sweep_check` counts the failures of the
  completed pass AND of the pass in progress (lists or bare counts); a completed pass's failures
  stay critical until a later completed pass without failures replaces them; a malformed state
  file is critical.
- **P2 7 — health is always published.** `payload_check` validates the whole status inside its
  protected boundary (a non-object, a non-string `latest_backup`/`result`, a missing or
  impossible date → critical); every check runs through `_guarded` (an exception becomes a
  critical result), and `main` records even when `evaluate()` itself fails. The review evidence
  guards the same helper. Tests: seven malformed statuses, a failing `status()` plus a malformed
  payload file (both critical, recorded), a failing `evaluate()` (recorded), review evidence with
  a malformed status.
- **P2 8 — one recoverability judgement.** `pg_backup.recoverability(arch)` is THE function:
  WAL archived at all and exposure within the RPO; the server's last archived segment present in
  the archive; a base exists on the server's timeline; the chain from the newest base's start
  through its end and the last archived segment complete. The growth block
  (`ops_health.recoverability_block`), the health report (`pg_backup.status()` →
  `metadata_checks`' `archive` check: critical exactly when it reports problems) and the review
  evidence all call it; the oldest-base coverage check (`coverage_gaps`) stays on top as the
  retention check. Test: every state of the block test asserts monitoring and blocking agree.
- **P2 9 — the drill fingerprint ignores lock-free pin bookkeeping.** `release_pin()` deletes
  from `generation_retention` without the writer lock, so it can commit between the drill's
  snapshot and its restore point: `VOLATILE_TABLES = {"generation_retention"}` is excluded from
  every fingerprint (both sides); a pin changes only how far the fold may go, never a
  generation's content. Test: a pin released between an open REPEATABLE READ snapshot and a new
  one — identical fingerprints (the table differed: one pin vs none).
- **P2 10 — recoverability is re-checked while a round grows.** A growth round checks before
  every mutating step (`before fetch/prune/clean`), right before its promotion, and every
  `RECOVERABILITY_RECHECK_SECONDS` (300; `NEKAISE_RECOVERABILITY_RECHECK_SECONDS` can only lower
  it, clamped to 1..300) in a watcher thread that records `growth_blocked` and SIGTERMs the
  round, which unwinds as on a cron timeout (processes stopped, broker drained, run aborted).
  Recovery, standalone repairs and maintenance runs are not blocked. Tests (real rounds): a
  block that appears during the gates stops the promotion, one that appears during fetch stops
  the round before prune, the watcher stops a round held in its fetch (exit 130, aborted), and
  a standalone repair still promotes while rounds are refused.
- **Indexed persistent-id membership: schema v8** (it now fits: the normalization is on main).
  Migration 8 adds `pids text[]` to entries, manifest and revisions — NULL means "declares none",
  exact for rows without `persistent_id`/`origin_ids`, so there is no table rewrite — with partial
  GIN indexes, and backfills every row and every entries/manifest put revision that has either
  field (live: 12,808 entries + 12,808 manifest rows; the revision guard is disabled only for the
  backfill's own statements). Every derived-column writer fills it (`put_rows`: the store, the
  shadow replay, the fold; staging's `_stage` and replacement tombstones). `known_pids()` probes
  the GIN index once per identifier (`LATERAL … pids @> ARRAY[p] OFFSET 0`) into the projection
  and into revisions for candidate ids, then reads the VISIBLE rows of those ids by primary key
  through the view's source, so the overlay decides exactly as in `known()`. A single
  `pids && <400 values>` filter was estimated to match most of the table and seq-scanned 160M
  rows in the benchmark (12+ min per call); the probe is 0.77 s p50 / 1.53 s p95 there. The
  derived-key checks cover it
  (`pg_shadow` verify, `verify_generation`, the integrity sweep). Old-code migration tests now
  drive the pre-migration schema with the OLD `pg_shadow` too (`tests/old_code.py`). Tests
  (`tests/test_store_pg_pids.py`): a schema written by the real v7 code (projection rows, a
  promoted unfolded generation with a staged alias and a tombstone, an open run) migrates, every
  pid backfilled, `known_pids` answers exactly what the v7 expression scan answered, the open
  run's overlay sees its own staged pid, v7 clients refused; every writer derives `pids` and the
  fold carries them; the lookup uses `manifest_pids`; stale `pids` are found by all three
  derived-key checks. The OpenAlex `test_known_pids_*` contract tests pass on both backends.
- **Incremental lint and the OpenAlex rights checks**: `changed_lint` calls the shared
  `entry_errors`, so the rights-evidence rules apply to a run's changes (test: an `openalex_sim`
  entry with `license: open` and no evidence fails incrementally; a complete one passes).
- **Gates after the fixes**: full suite 1488 passed / 254 skipped (PostgreSQL skipped); with
  PostgreSQL (the test cluster) 1771 passed / 3 skipped (the opt-in benchmarks); `py_compile`
  of scripts and tests clean. The 160M benchmark was rerun at schema v8 (above: p95 4.00 s per
  round, `known_pids()` of 400 identifiers p95 1.53 s) and its cluster removed afterwards.
