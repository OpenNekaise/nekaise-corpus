---
name: go
description: Materialize the existing recipe through the complete verified training-ready pipeline, then configure ongoing growth when authorized. Use on a fresh clone or when the user says go, start, or get the data.
---

# Skill: go

Read AGENTS.md's operator contract. Complete the requested work through verified `corpus/` in one
invocation; a loader's opening missing-file count is not proof of success or a reason to stop.

1. Use the project's working Python environment. On a fresh environment install `requirements.txt`.
2. Run the locked, transactional pipeline without discovering extra sources:
   ```
   python scripts/run_round.py --skip-discovery --commit --lock-timeout 30
   ```
   This fetches the existing eligible recipe, prunes, cleans with the selected ruleset, refreshes
   stats, runs all required checks, and commits only a healthy result locally. It never pushes.
   If another round owns the lock, allow it to finish; do not launch an overlapping manual loader.
3. Inspect the final result and residual failures. Investigate failures by cause/host and fix confirmed
   dead entries under the round lock. Do not loop over transient failures indefinitely or claim
   everything materialized just because the loader exited. Report any deferred retries explicitly.
4. If ongoing automatic growth is already authorized, preserve the existing schedule or install the
   requested mode with `scripts/install_cron.sh` (daily by default; `DIG_CONTINUOUS=1` for continuous).
   Verify the installed entry without replacing unrelated cron jobs. If the request was only to load
   once, finish that work and explain the optional schedule; do not make ongoing operation a
   prerequisite for completing the one-time load.
5. Report the verified outcome, failures, current schedule, and where to inspect `logs/dig-*.log`.
   Remove the growth schedule with `bash scripts/install_cron.sh --remove`.

Keep `raw/`, `text/`, and `corpus/` out of Git. Respect eligibility and licenses. Routine loading
never changes cleaning policy. See [dig](../dig/SKILL.md) for growth and
[clean-corpus](../clean-corpus/SKILL.md) for targeted cleaning investigations.
