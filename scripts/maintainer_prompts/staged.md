
PostgreSQL authority (the snapshot has a `store` section; ADR 0001 stage 4). The dataset lives
in PostgreSQL generations, not in git: a round is ONE staged run that its gates validate and one
promotion turns into the next generation. There are no round snapshots, per-round data commits or
README statistics; `pending_round_snapshots` is always empty. Git still owns code and policy.

- Recovery is automatic and shared: before triage, and again when the action window opens, the
  maintainer stops a dead round's processes, aborts every unpromoted run by its durable status
  (a promoted run stands) and completes the current generation (corpus/ materialization, bounded
  fold/purge). `store.unfinished_runs` should therefore be empty; if not, report why.
- `triage_generation` is the generation triage judged; it is pinned (kept reconstructible) until
  the action ends. The action snapshot's `store.generation` is fresh: growth may have promoted
  more generations meanwhile (`changed_since_triage`).
- In the action window every store mutation you run (`prune_corpus.py --apply`, `rotation.py
  advance`, `blocklist.add`, `migrate_backend_state.py --apply`, a standalone clean) stages into
  ONE maintenance run. Only when your action exits successfully does the maintainer freeze it,
  run the gates (artifacts, claim check, contracts, lint) against the frozen state and promote it
  as a compensating generation; otherwise it is aborted and nothing becomes visible. Never run
  `run_round.py` here (refused nested) and never try to resume a run: `--resume` belongs to an
  operator outside the window.
- Commit code and policy changes to git as before. Data is never committed to git.
