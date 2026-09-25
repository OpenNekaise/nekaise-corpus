
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

Publication review is a GENERATION-RANGE review here (it replaces reviewing outgoing data
commits; outgoing code commits are still reviewed and pushed as before). `generation_review` in
the snapshot summarizes the unreviewed generations (reviewed_through, triage_generation]: each
generation's run, producer commit, operation counts, manifest rows by status, gate receipts,
configuration decisions, the failed runs of the range, code revisions and backup health; the full
evidence is the file it names. Choose action_kind `publish` when that range needs a verdict.

In the action, after inspecting the evidence, write your verdict as JSON to the file named by
the environment variable NEKAISE_REVIEW_VERDICT_FILE:
`{"through": <the range's upper generation>, "verdict": "ok" | "finding" | "integrity",
"evidence_digest": "<generation_review.digest>", "summary": "...", "findings": ["..."],
"resolves": [<open finding verdict numbers>]}`. The maintainer recomputes the evidence and
records the verdict only if its digest is exactly the one you reviewed; verdicts are contiguous
and permanent. `ok` endorses the range (publication may reach it) when no finding is open;
`finding` withholds endorsement until a later verdict resolves it; `integrity` (corrupted,
unprovenanced or policy-violating data) also blocks growth rounds until resolved. A repair is a
compensating generation (your maintenance run, promoted after the gates when your action
succeeds and code and configuration are unchanged in the window): list the finding under
`resolves` only in a LATER pass whose range covers that promoted repair — the database refuses
a resolution by a verdict that covers no later generation. The verdict must be about exactly
the range and digest shown in this pass. Never record a verdict for evidence you did not
inspect, and never use `ok` to paper over a finding. If you commit code or configuration in a
window, its store mutations are not promoted (they ran under other code): make data repairs in a
pass without code changes.
