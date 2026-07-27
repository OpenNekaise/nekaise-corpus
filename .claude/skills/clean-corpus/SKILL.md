---
name: clean-corpus
description: Build the cleaned, training-ready corpus/ from the verbatim text/ via clean_corpus.py, verify it agrees with the manifest, and measure/propose cleaning-rule changes. Use when asked to clean the corpus, rebuild corpus/, investigate junk or "meaningless" text in the dataset, or after any load/prune so corpus/ mirrors the manifest.
---

# Skill: clean-corpus

Turn the **verbatim** extraction in `text/` into the **cleaned, training-ready** text in `corpus/`.
This is the curation half of the loop: the pruner decides *which documents* survive, this stage
decides *which lines within them* do. Mechanics live in `scripts/clean_corpus.py`; your job is to run
it, verify it, and — when the numbers justify it — **propose** a ruleset change rather than make one.

`text/` is never edited in place. `corpus/` is derived, disposable, and rebuildable in seconds.

## The one rule that matters

**Do not enable or disable cleaning rules on your own initiative.** The ruleset is currently `none`
(faithful pass-through) *by the maintainer's explicit decision* — the agentic cleaning pipeline is a
pending design discussion. If you think a rule should change, run `--report`, bring the numbers, and
ask. Silently changing what enters the training set is the most damaging thing this stage can do.

## To rebuild `corpus/` (the normal case)

```
python scripts/clean_corpus.py
python scripts/clean_corpus.py --check
```

Reads each manifest row's `text/` file, applies the enabled rules, writes `corpus/<id>.md`, records
`corpus_path` / `corpus_chars` in the manifest. Incremental — only new/changed docs are rewritten,
unless the ruleset changed, in which case it rebuilds everything.

Run this **after every load and after every prune**, so `corpus/` mirrors the manifest. `--check` must
pass before you commit; it exits non-zero and names the drift.

Timings on a 40-core box: pass-through rebuild **6s**, full ruleset **46s**, `--check` **14s**. If it
seems to hang, it isn't — the rules are regex-bound over 13GB.

## To investigate junk ("the corpus still has meaningless text")

```
python scripts/clean_corpus.py --list-rules           # what exists
python scripts/clean_corpus.py --report --sample 40   # per-rule impact, writes nothing
python scripts/clean_corpus.py --report               # full corpus, slower
```

`--report` attributes removal per rule (first rule to claim a line owns it, so the numbers sum to the
real total). Current full-corpus measurement: **966.9M of 13,089.4M chars = 7.39%**, of which
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
- **You need to stop a run** → `pkill -9 -f clean_corpus.py`. Plain `kill` on the parent **orphans its
  worker processes**, which keep writing to `corpus/` after the parent is gone; that is exactly how a
  disk-vs-manifest mismatch gets created.
- **A run was interrupted** → the stamp reads `IN-PROGRESS <ruleset>` and the next run rebuilds
  everything automatically. Never hand-edit `corpus/.ruleset`.

## Notes

- `corpus/` is built **from the manifest**, never from a directory listing, so it can only contain docs
  with a provenance row. Files in `text/` that no row references are excluded by design, and ids whose
  `text_path` drifted get canonical `corpus/<id>.md` names.
- **License discipline:** `corpus/` is git-ignored like `raw/` and `text/` and must NEVER be committed.
  Commit only the manifest changes (`corpus_path` / `corpus_chars`), the code, and the docs.
- The pruner's quality thresholds were tuned on **uncleaned** `text/`. If a ruleset is ever enabled,
  those thresholds need re-deriving against `corpus/` — otherwise cleaning silently shifts every gate
  verdict. Raise this, don't quietly work around it.
