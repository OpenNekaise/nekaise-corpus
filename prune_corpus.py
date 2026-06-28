#!/usr/bin/env python3
"""prune_corpus.py — quality gate. Drop low-value docs after a load.

Next-token CPT memorizes text literally, so junk in = junk out. This removes, from the manifest +
registry + disk, docs that are: failed downloads, thin/empty, garbage extractions (symbol soup from
scanned / math-heavy PDFs), non-English, or off-topic (too few building/energy keywords). It prunes
only *discovered* sources (id prefix oa-/ope-/ost-/arx-); hand-curated originals are left alone.

    python prune_corpus.py            # dry run -- report what would be pruned
    python prune_corpus.py --apply    # prune (delete files, rewrite manifest + sources.yaml)
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent

DOMAIN = re.compile(
    r"build|hvac|energy|thermal|heat|cool|ventil|chiller|boiler|refriger|damper|ahu|vav|setpoint|"
    r"occupan|comfort|retrofit|envelope|commission|controller|sensor|actuator|bacnet|ashrae|kwh|"
    r"carbon|emission|psychrometr|economizer|fault|diagnos|efficien|envelope|insulat", re.I)
EN = re.compile(r"\b(the|and|of|to|in|is|for|that|with|are|this|be|as|by|on|from)\b", re.I)


def body(text: str) -> str:
    return text.split("\n---\n\n", 1)[-1] if "\n---\n\n" in text else text


def quality(text: str) -> str:
    t = body(text)[:20000]
    if len(t.strip()) < 2000:
        return "thin"
    if sum(c.isalpha() for c in t) / len(t) < 0.55:
        return "garbage"
    words = re.findall(r"[A-Za-z]{2,}", t)
    if len(words) < 200:
        return "thin"
    if len(EN.findall(t)) / len(words) < 0.04:
        return "non-english"
    if len(DOMAIN.findall(t)) < 10:
        return "off-topic"
    return "ok"


def discovered(i: str) -> bool:
    return i.startswith(("oa-", "ope-", "ost-", "arx-", "crawl-"))


def emit_entry(r: dict) -> str:
    e = {k: r[k] for k in ("id", "title", "url", "source", "license", "topic", "format")}
    d = yaml.safe_dump([e], sort_keys=False, allow_unicode=True)
    return "".join(("  " + ln + "\n") if ln else "\n" for ln in d.splitlines())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    manifest = [json.loads(l) for l in (HERE / "manifest.jsonl").read_text().splitlines() if l.strip()]
    drop: dict[str, str] = {}
    for r in manifest:
        if not discovered(r["id"]):
            continue
        if r["status"] != "ok":
            drop[r["id"]] = "failed"
            continue
        tp = r.get("text_path")
        if not tp or not (HERE / tp).exists():
            drop[r["id"]] = "no-text"
            continue
        q = quality((HERE / tp).read_text())
        if q != "ok":
            drop[r["id"]] = q

    disc_total = sum(1 for r in manifest if discovered(r["id"]))
    print(f"discovered docs: {disc_total} | would prune: {dict(Counter(drop.values()))} "
          f"(total {len(drop)})")
    if not args.apply:
        print("dry run -- pass --apply to prune")
        return

    for r in manifest:
        if r["id"] in drop:
            for p in (r.get("raw_path"), r.get("text_path")):
                if p and (HERE / p).exists():
                    (HERE / p).unlink()
    keep = [r for r in manifest if r["id"] not in drop]
    (HERE / "manifest.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep))

    good_disc = [r for r in keep if discovered(r["id"]) and r["status"] == "ok"]
    text = (HERE / "sources.yaml").read_text()
    head = text.split("# --- discovered")[0].rstrip()
    block = "".join(emit_entry(r) for r in good_disc)
    (HERE / "sources.yaml").write_text(head + "\n\n  # --- discovered (find_sources.py) ---\n" + block)
    print(f"pruned {len(drop)}; kept {len(good_disc)} good discovered docs")


if __name__ == "__main__":
    main()
