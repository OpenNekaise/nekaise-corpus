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
