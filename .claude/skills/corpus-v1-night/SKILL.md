---
name: corpus-v1-night
description: The nightly corpus_v1 improvement session. Claude (Opus) and Codex (GPT) wake up every night, inspect corpus_v1/, improve its cleaning (rules, model-repair prompt and checks, queue policy), prove it with tests and an audit, get Codex review, and commit on a branch that the night job merges. Use when running the night session, or when asked to improve corpus_v1 by hand.
---

# Skill: corpus-v1-night

## The goal (operator, 2026-09-29)

`corpus_v1/` is the training view for **continued pretraining / mid-training of a small
built-environment LLM**. It holds **every** document of `corpus/`, in cleaned form. When text is
broken, it is repaired into normal text. Each night the session makes `corpus_v1/` **measurably
better for learning domain knowledge**. This is a continuous job: every night picks up the
backlog in `NOTES.md` and moves it forward. Nothing here is a one-off.

How much gets done in a night is up to Claude and Codex. Anything beyond the goal is a
suggestion. Record what you decide and why in `NOTES.md`.

## The machinery (read the module docstrings first)

| Layer | File | What it does |
|---|---|---|
| rules | `scripts/v1_rules.py` | deterministic cleaning of every file (patent template parser, furniture, re-flow, pre-pass); `RULESET_VERSION` |
| builder | `scripts/corpus_v1.py` | incremental build of `corpus_v1/`, the state DB, `--report`, `--show`, `--audit` |
| model repair | `scripts/sonnet_clean.py` | Sonnet 5.5 rewrites the most OCR-damaged docs into normal text, part by part, with number/drop/shrink checks; `PROMPT_VERSION` |
| tests | `tests/test_v1_rules.py`, `tests/test_sonnet_clean.py`, `tests/test_corpus_v1_safety.py` | golden KEEP/DROP cases; the 2026-09-29 audit's failures and the maintainer's 2026-09-30 publication blockers are pinned here |
| night job | `scripts/corpus_v1_night.py` | runs everything; you are its improvement phase |

The night job already did the incremental build (new dig documents) and is running Sonnet repair
in the background while you work. After you finish, it runs its **own** Codex gate on your
branch, merges it if Codex says `MERGE AS IS`, and rebuilds with your new ruleset.

## Your session, step by step

You are in a git worktree on branch `v1-night/<date>`. `CORPUS_V1_DATA` points at the live data.
`NIGHT_DIR` is tonight's working directory. `DEADLINE` is a unix time: stop before it.

1. **Orient (≤10 min).** Read `$NIGHT_DIR/context.json` (build report, last Sonnet run stats,
   last night's gate verdict) and `workspace/corpus-v1-night/NOTES.md` in the live repo
   (`$CORPUS_V1_DATA/workspace/corpus-v1-night/NOTES.md`): the backlog, what was tried,
   what was rejected and why.
2. **Inspect.** Look at real output. Don't guess.
   - `python scripts/corpus_v1.py --show --sample 15 --seed <tonight>`: random cleaned files.
   - The worst kept ratios, and the kinds and sources in the state DB
     (`$CORPUS_V1_DATA/corpus_v1/.state.sqlite`).
   - Sonnet logs in `$CORPUS_V1_DATA/corpus_v1/.log/*.jsonl`: fallback reasons,
     `dropped_samples`, refusals. Compare revisions in `corpus_v1/.revisions/` with their sources.
3. **Choose 1–3 improvements** with the best training value per unit of risk. Useful directions:
   - leftover furniture;
   - broken re-flow;
   - tables mangled into one value per line;
   - patent sections the parser misses;
   - whole classes of documents such as web crawls, EPDs, datasheets or standards;
   - Sonnet repairs that fall back too often (prompt or check);
   - a better damage score or queue order;
   - near-duplicate boilerplate across documents.

   Ask Codex for a second opinion when choosing. For example:
   `codex exec --sandbox read-only -C . "…evidence… which of these would you do first and why?"`
4. **Implement** in the files above. Add tests for every rule change, KEEP cases first. Bump
   `RULESET_VERSION` when rule output changes, and `PROMPT_VERSION` only when old repairs should
   be redone.
5. **Prove it.**
   - `python -m pytest -q tests/test_v1_rules.py tests/test_sonnet_clean.py tests/test_corpus_v1_safety.py`
   - `python scripts/corpus_v1.py --audit 400 --out $NIGHT_DIR/audit`. Read `changes.md`. Every
     removed line must be junk. If any real content goes, fix the rule or drop it.
5b. **Ask Codex to review** before committing:
   `git diff main | codex exec --sandbox read-only -C . "Review this corpus_v1 change adversarially: content loss, CJK/table safety, test gaps. Evidence: $NIGHT_DIR/audit/summary.json. Reply MERGE AS IS or list required changes."`
   Fix its findings and repeat, up to 3 rounds.
6. **Commit** on the branch with a message that begins `corpus_v1:` and says what changed plus the
   audit numbers. Never commit on main, never push, never merge yourself.
7. **Write the notes.** Append tonight's entry to the live `NOTES.md`: what changed, the evidence,
   Codex's verdict, the next backlog items, and ideas you rejected with the reason. Keep the
   backlog ranked, and keep the file under about 300 lines by summarising old nights.

## Hard rules

- **Keep everything.** Remove only what carries no readable content. Never delete whole
  documents: every `corpus/` doc stays in `corpus_v1/`. Topic and quality filtering are out of
  scope. If you think a class of documents should be excluded, write it in `NOTES.md` as a
  proposal for the operator.
- **Rules are structural.** Base them on shape, repetition or template, never on an
  alpha-fraction threshold. CJK prose interleaved with figures looks like garbage to those.
  Vertical CJK columns are content (the `orphan_chars` lesson in `clean_corpus.py`).
- **Never write** to `text/`, `corpus/` or `corpus_v1/`; the builder owns them. Never touch
  `registry/`, `manifest/`, the dig or maintainer machinery, or cron.
- **The model repairs; it never invents.** Keep the number, drop and shrink checks at least as
  strict as they are.
- **Mind the budget.** Sonnet runs on the operator's subscription alongside you. Don't start extra
  large Sonnet jobs from your session. Small experiments (≤10 files with `--ids`) are fine.
- **Codex budget: at most 3 `codex exec` calls per session** (choosing and review rounds
  combined), with short, focused prompts. The gate after your session needs Codex quota. On
  2026-09-30 in-session consultations used it up and the night's work could not merge. If Codex
  reports a usage limit, stop calling it, say so in `NOTES.md`, and commit anyway. An unmerged
  branch carries over and is gated with the next night's work.
- Work that missed its gate carries over: tonight's branch may already contain earlier nights'
  commits (see `base` in `summary.json`). Keep building on them. Don't redo them.
- **Stop before `DEADLINE`.** If the work isn't finished, commit what is proven, and leave the
  rest in `NOTES.md` for tomorrow.
