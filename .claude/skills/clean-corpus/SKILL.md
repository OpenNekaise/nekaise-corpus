---
name: clean-corpus
description: Build the cleaned, training-ready corpus/ from the verbatim text/ via clean_corpus.py, verify it agrees with the manifest, and measure/propose cleaning-rule changes. Use when asked to clean the corpus, rebuild corpus/, investigate junk or "meaningless" text in the dataset, or after any load/prune so corpus/ mirrors the manifest.
---

# Skill: clean-corpus

Turn the **verbatim** extraction in `text/` into the **cleaned, training-ready** text in `corpus/`.
This is the curation half of the loop: the pruner decides *which documents* survive, this stage
decides *which lines within them* do. Mechanics live in `scripts/clean_corpus.py`; your job is to run
it, verify it, and measure candidate improvements within the authorized task.

`text/` is never edited in place. `corpus/` is derived and rebuildable from text/; runtime depends on current corpus size and hardware.

## The one rule that matters

**Keep the selected ruleset unchanged during routine loading or digging.** The maintained corpus currently
uses `toc_leaders,patent_id_soup,patent_furniture,site_chrome,ocr_debris,code_annotations`, recorded
in `corpus/.ruleset`; an argument-less refresh reuses that stamp. A fresh checkout with no selected
ruleset defaults to faithful pass-through. If you think the maintained policy should change, run
`--report` and measure representative drop/retain examples. Implement and test candidate rules
within an authorized improvement task; promotion to the maintained ruleset must be explicit and
within that authorization. Do not repeat permission questions already resolved by the user.
Headless maintainers stage out-of-scope policy proposals without waiting for an interactive reply.

## To rebuild `corpus/` (the normal case)

```
python scripts/clean_corpus.py
python scripts/clean_corpus.py --check
```

Reads each eligible manifest row's `text/` file, applies the enabled rules, writes `corpus/<id>.md`,
and records `corpus_path` / `corpus_chars` in the manifest. `registry/eligibility.json` exclusions
retain registry/manifest plus raw/text provenance but are moved out of the training directory.
Incremental — only new/changed docs are rewritten, unless the ruleset changed.

Run this **after every load and after every prune**, so `corpus/` mirrors the manifest. `--check` must
pass before you commit; it exits non-zero and names the drift.

Historical timings on a 103,931-document / 13GB benchmark were 6s/46s/14s for pass-through/full
rules/check. They are not current deadlines. Use recent run timings and process progress to
distinguish expected work from a stall; do not declare a hung run healthy without evidence.

## To investigate junk ("the corpus still has meaningless text")

```
python scripts/clean_corpus.py --list-rules           # what exists
python scripts/clean_corpus.py --report --sample 40   # per-rule impact, writes nothing
python scripts/clean_corpus.py --report               # full corpus, slower
```

`--report` attributes removal per rule (first rule to claim a line owns it, so the numbers sum to the
real total). Historical 103,931-document measurement: **966.9M of 13,089.4M chars = 7.39%**, of which
`repeated_boilerplate` alone is 529.4M. Report *that* kind of number, not an impression.

**Before proposing any new rule, read real examples of what it would remove.** Sample lines from
several shards — `crawl` (~45% non-prose) and `github` (~40%) look nothing like `books` (~8%).

## Two traps that have already caught us

1. **Numeric ≠ meaningless.** `Asphalt workers 2.81 (1.11-7.13)` is an odds ratio by trade;
   `Concrete C25/30 25 30 2400 31` is a material property table. These are real AEC knowledge and the
   user's "meaningless numbers" complaint does **not** license removing them.
2. **Never write a letters-per-character rule.** It reads real Japanese prose interleaved with figures
   (`測定は 2019 年 3 月 14（暖房期）と 2021 年 10 月 22（冷房期）に`) as number soup — CJK plus
   digits drops the alpha fraction below any threshold you'd pick. `quality.py` already hit this once
   and needed `MIN_ALPHA_CJK` to recover. **Every rule here is structural** (repetition- or
   shape-based), which is script-agnostic by construction. Keep it that way.

Any rule you add must come with golden tests in `tests/test_clean.py` pinning **both** directions:
what it drops, *and* the CJK prose / data tables / Modelica equations it must not touch.

## If something goes wrong

- **`--check` reports drift** → `python scripts/clean_corpus.py --force` rebuilds from scratch.
  Drift means `corpus/` and the manifest disagree (missing files, unprovenanced files, `corpus_chars`
  mismatches) — never leave it, a training run would read text the provenance record doesn't describe.
- **You need to stop a run** → identify that run's owned process group/descendants and terminate
  them together (TERM, then KILL only if necessary). Verify workers have exited before releasing
  its lock or starting another cleaner. Never kill every process matching a shared script name;
  killing only the parent can leave workers writing after the lock is released.
- **A run was interrupted** → the stamp reads `IN-PROGRESS <ruleset>` and the next run rebuilds
  everything automatically. Never hand-edit `corpus/.ruleset`.

## Notes

- `corpus/` is built **from eligible manifest rows**, never from a directory listing, so it can only
  contain docs with provenance and current policy approval. Files in `text/` that no row references
  are excluded by design, and ids whose `text_path` drifted get canonical `corpus/<id>.md` names.
- **License discipline:** `corpus/` is git-ignored like `raw/` and `text/` and must NEVER be committed.
  Commit only the manifest changes (`corpus_path` / `corpus_chars`), the code, and the docs.
- The pruner consumes **uncleaned** `text/`; the cleaner writes the later `corpus/` stage. A cleaning
  change does not move the pruner's input. Recalibrate thresholds only as an explicit, measured
  quality-gate change, never as an incidental consequence of enabling a cleaning rule.
