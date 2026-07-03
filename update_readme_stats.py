#!/usr/bin/env python3
"""update_readme_stats.py — regenerate the README 'At a glance' stats from the manifest.

Rewrites the region between the <!-- STATS:START --> and <!-- STATS:END --> sentinels in README.md
with the live doc/token/topic/license counts. Called by scripts/marathon.sh each round so the README
never goes stale. No-op (leaves README untouched) if the sentinels are missing.
"""
from __future__ import annotations

import json
import subprocess
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
README = HERE / "README.md"
START = "<!-- STATS:START -->"
END = "<!-- STATS:END -->"


def du(path: str) -> str:
    try:
        return subprocess.run(["du", "-sh", path], capture_output=True, text=True,
                              cwd=HERE, timeout=120).stdout.split()[0]
    except Exception:
        return "?"


def main() -> None:
    rows = [json.loads(l) for l in (HERE / "manifest.jsonl").read_text().splitlines() if l.strip()]
    ok = [r for r in rows if r.get("status") == "ok"]
    chars = sum(r.get("text_chars", 0) for r in ok)
    tok = chars // 4
    topics = Counter(r["topic"] for r in ok)
    lic = Counter(r["license"] for r in ok)
    date = time.strftime("%Y-%m-%d", time.gmtime())

    by_topic = " · ".join(f"{t} {n:,}" for t, n in topics.most_common())
    lic_order = ["open", "public-domain", "cc-by-sa", "cc-by", "cc0", "proprietary-internal"]
    by_lic = " · ".join(f"{k} {lic[k]:,}" for k in lic_order if lic.get(k)) or \
        " · ".join(f"{k} {n:,}" for k, n in lic.most_common())

    block = f"""{START}
| | |
|---|---|
| **Documents** | **{len(ok):,}** |
| **Raw originals** | **~{du('raw')}** (PDF / HTML / source code) |
| **Extracted text** | **~{du('text')}** (~{chars/1e6:.0f}M chars, **≈{tok/1e6:.0f}M tokens**) |
| **Topics** | {len(topics)} |

**By topic** (a source gets one at registration): {by_topic}.

**By license:** {by_lic}.

_Snapshot of the live registry ({date}) — auto-generated from `manifest.jsonl`. The bytes are not
shipped; run the loader to fetch your own copy. The corpus grows as sources are added to `sources.yaml`._
{END}"""

    text = README.read_text()
    i, j = text.find(START), text.find(END)
    if i == -1 or j == -1 or j < i:
        print("update_readme_stats: STATS sentinels not found — README left unchanged")
        return
    new = text[:i] + block + text[j + len(END):]
    if new != text:
        README.write_text(new)
        print(f"update_readme_stats: {len(ok):,} docs / ~{tok/1e6:.0f}M tokens")
    else:
        print("update_readme_stats: no change")


if __name__ == "__main__":
    main()
