#!/usr/bin/env python3
"""update_readme_stats.py — regenerate the README 'At a glance' stats from the manifest.

Rewrites the region between the <!-- STATS:START --> and <!-- STATS:END --> sentinels in README.md
with the live doc/token/topic/license counts. Called by scripts/marathon.sh each round so the README
never goes stale. No-op (leaves README untouched) if the sentinels are missing.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from collections import Counter
from pathlib import Path

import corpus_stats
import ops
import registry
import store

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
README = HERE / "README.md"
START = "<!-- STATS:START -->"
END = "<!-- STATS:END -->"


def du(path: str) -> str:
    try:
        return subprocess.run(["du", "-sh", path], capture_output=True, text=True,
                              cwd=HERE, timeout=120).stdout.split()[0]
    except Exception:
        return "?"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--print-tokens",
        action="store_true",
        help="print the exact manifest-derived token estimate and do not update README",
    )
    ap.add_argument(
        "--print-corpus-tokens",
        action="store_true",
        help="print the CLEANED corpus/ token estimate (sum of corpus_chars; rows the cleaner "
             "has not visited yet fall back to text_chars) and do not update README",
    )
    ap.add_argument("--lock-timeout", type=float, default=60,
                    help="standalone runs wait this long for the round lock (inside a round the "
                         "read is inherited)")
    args = ap.parse_args(argv)

    restrictions = registry.load_eligibility()
    with store.open(root=HERE).read(timeout=args.lock_timeout) as view:
        stats = corpus_stats.compute(view, restrictions)
        # README numbers are manifest-derived and identical on every machine; local availability
        # of suspended-host payloads is reported here only, never in the committed statistics.
        unavailable = corpus_stats.local_unavailable(view, HERE, restrictions)
    if unavailable:
        print(f"local availability: {unavailable:,} eligible rows on a fetch-suspended host "
              "have no local payload (counted in README; not in local corpus/)", file=sys.stderr)
    ok_count, excluded_count = stats.documents, stats.excluded
    chars, tok = stats.text_chars, stats.tokens
    cchars, ctok = stats.corpus_chars, stats.corpus_tokens
    if args.print_tokens:
        print(tok)
        return
    if args.print_corpus_tokens:
        print(ctok)
        return

    topics = stats.topics
    lic = Counter(stats.licenses)

    by_topic = " · ".join(f"{t} {n:,}" for t, n in topics)
    lic_order = ["open", "public-domain", "cc-by-sa", "cc-by", "cc0", "proprietary-internal"]
    by_lic = " · ".join(f"{k} {lic[k]:,}" for k in lic_order if lic.get(k)) or \
        " · ".join(f"{k} {n:,}" for k, n in lic.most_common())

    def big(n: float) -> str:
        """1484M -> '1.484B'; below a billion stay in M."""
        return f"{n/1e9:.3f}B" if n >= 1e9 else f"{n/1e6:.0f}M"

    block = f"""{START}
| | |
|---|---|
| **Documents** | **{ok_count:,}** |
| **Policy-excluded provenance** | **{excluded_count:,}** rows (not fetched or training-ready) |
| **Raw originals** | **~{du('raw')}** (PDF / HTML / source code) |
| **Extracted text** | **~{du('text')}** (~{big(chars)} chars, **≈{big(tok)} tokens**) |
| **Cleaned corpus** | **~{du('corpus')}** (~{big(cchars)} chars, **≈{big(ctok)} tokens**, ruleset-cleaned) |
| **Topics** | {len(topics)} |

**By topic** (a source gets one at registration): {by_topic}.

**By license:** {by_lic}.
{END}"""

    text = README.read_text()
    i, j = text.find(START), text.find(END)
    if i == -1 or j == -1 or j < i:
        print("update_readme_stats: STATS sentinels not found — README left unchanged")
        return
    new = text[:i] + block + text[j + len(END):]
    if new != text:
        ops.atomic_write_text(README, new)
        print(f"update_readme_stats: {ok_count:,} docs / ~{tok/1e6:.0f}M tokens")
    else:
        print("update_readme_stats: no change")


if __name__ == "__main__":
    main()
