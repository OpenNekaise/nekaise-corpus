#!/usr/bin/env python3
"""clean_corpus.py — stage 3: text/ (verbatim extraction) -> corpus/ (cleaned, training-ready).

The loader (build_corpus.py) writes VERBATIM extraction into text/. That text carries PDF
artefacts that are meaningless to a next-token objective: running headers repeated on every
page, table-of-contents dot-leaders, bare page numbers, OCR punctuation debris, patent
identifier blocks, Modelica graphical annotations. A 920-doc stratified audit put non-prose at
~12.8% of all characters. This stage strips it, leaving text/ untouched so an improved ruleset
can be re-applied in minutes without re-extracting 104k PDFs.

    python scripts/clean_corpus.py --report            # measure each rule; write nothing
    python scripts/clean_corpus.py                     # refresh corpus/ with the ACTIVE ruleset
    python scripts/clean_corpus.py --rules all         # apply every rule below
    python scripts/clean_corpus.py --rules toc_leaders,page_markers
    python scripts/clean_corpus.py --rules none        # faithful pass-through
    python scripts/clean_corpus.py --list-rules

WHICH RULES RUN IS A POLICY DECISION, recorded in corpus/.ruleset. The default --rules=stamp
reuses that recorded policy, so the round loop (run_round.py's argument-less clean step)
preserves it; changing policy requires an explicit --rules. Before any ruleset was ever chosen
the stamp reads 'none' and the default is a faithful pass-through. Rule changes go through the
audit loop first: sample each rule's dropped lines with context, adversarially review, fix or
defer (see tests/test_clean.py for the pinned verdicts).

Every rule here is STRUCTURAL — repetition- or shape-based, never a letters-per-character
threshold. That is deliberate: an alpha-fraction rule reads real Japanese prose interleaved
with figures ('測定は 2019 年 3 月 14（暖房期）に') as number soup, the same trap quality.py
already hit once (MIN_ALPHA_CJK). Structural rules are script-agnostic by construction.

corpus/ is built FROM THE MANIFEST, never from a directory listing, so it can only ever contain
docs that have a provenance row. corpus/ is git-ignored — like raw/ and text/, it never ships.

The manifest is read and patched through the store (ADR 0001 stage 3, step 6): corpus/ files are
written first, outside any transaction; then only the corpus fields that changed are patched (and
restricted rows' corpus fields unset) in bounded batches, each one short transaction; the ruleset
stamp is published only after the last batch committed.
"""
from __future__ import annotations

import argparse
import hashlib
import locale
import os
import random
import re
import shutil
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import artifact_store
import corpus_stats
import ops
import registry
import store
import store_broker

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
TEXT = HERE / "text"
CORPUS = HERE / "corpus"
STAMP = CORPUS / ".ruleset"  # which rules produced the current corpus/; a change forces rebuild
POLICY_QUARANTINE = HERE / "workspace" / "policy-excluded-corpus"

HEADER_SEP = "\n---\n\n"  # build_corpus's provenance header terminator (quality.body splits here)
# CPU-bound (regex over 13GB), so scale with cores but leave headroom: this stage runs inside the
# daily dig, which may be fetching at the same time.
DEFAULT_WORKERS = max(1, min(16, (os.cpu_count() or 4) - 2))
# Rows per metadata transaction. A full re-clean (a ruleset change) patches every row: bounded
# batches keep each transaction's journal and memory small, in manifest shard order so each
# shard is rewritten about once per batch that touches it.
METADATA_BATCH_ROWS = 20_000

# ---------------------------------------------------------------------------------------------
# Rules. Each takes the body's lines and returns the SET OF INDICES to drop. Doc-level (not
# per-line) so rules like repeated_boilerplate can count across the whole document.
# ---------------------------------------------------------------------------------------------

# 'Kurzfassung ....... 9' / '3.2 Problemstellung ....... 13' — contents entries. Requires 4+ dots
# so an ellipsis in prose ('and so on ... the next section') is never touched.
TOC_LEADER = re.compile(r"\.{4,}\s*[ivxlcdm\d]{1,7}\s*$", re.I)
# a whole line that is only a page number: '7', 'iv', 'Page 12', '— 186 —', '- 12 -'
PAGE_MARKER = re.compile(r"^[\s\-—–—]*(?:page\s+)?[ivxlcdm\d]{1,6}[\s\-—–—.]*$", re.I)
# Google Patents identifier soup: the same number restated in every format variant.
PATENT_ID = re.compile(r"^[A-Z]{2}\s?[\d/,.]{6,18}\s?[A-Z]?\d?$")
# Shapes that fit PATENT_ID but are real content (the 2026-08 audit's false positives):
# 'LS 700,000' is a lump-sum cost cell, 'HB2.16.1' an excavation locus / clause label.
MONEY_BLOB = re.compile(r"^[A-Z]{2}\s?\d{1,3}(?:,\d{3})+$")
CATALOG_BLOB = re.compile(r"^[A-Z]{2}\d*(?:\.\d+){2,}$")
# OCR debris: not one letter or CJK character on the line, but several punctuation marks —
# "‘ ; . " / "} . " / "1a} ". Digits alone do NOT qualify (a bare '1845' is a real date).
LETTERLIKE = re.compile(r"[^\W\d_]", re.UNICODE)  # any letter incl. CJK/kana/Cyrillic
PUNCT_RUN = re.compile(r"[^\w\s]")
# Modelica graphical annotations + bare markup: 'extent={{-50,48},{50,-42}},' / '<td>0.33</td>'.
# The annotation shape is required EXACTLY ('extent={', 'Text(' — no space): with a space
# allowed, English prose lines like 'points = 7, max. displacement ...' and nomenclature
# entries like 'Text = external temperature' were deleted mid-sentence (2026-08 audit).
MODELICA_ANNOT = re.compile(r"^\s*(?:(?:extent|fillColor|lineColor|origin|points)=\{"
                            r"|(?:pattern|fillPattern)=(?:Line|Fill)Pattern"
                            r"|rotation=-?\d|textString="
                            r"|(?:Rectangle|Polygon|Ellipse|Text|Line)\()")
COORD_BLOB = re.compile(r"^\s*[\[{(]?\s*[-\d]+\s*,[\s\-\d,.{}\[\]()]*[}\])]?\s*,?\s*$")
# Tag-only lines are dropped ONLY for known HTML table/layout tags. An unrestricted <...> match
# ate '<https://...>' bibliography URLs, RDF '@prefix <iri>' lines in the Brick/223P docs,
# EBNF placeholders like '<true-block-of-statements>', and doctest '<class ...>' outputs.
# MathML tags (<mn>, <mi>, <mo>...) are deliberately NOT listed: dropping only some equation
# tokens corrupts the equations that remain — keep formula markup whole instead.
HTML_TAG = (r"(?:br|p|li|ul|ol|dl|dt|dd|td|tr|th|thead|tbody|table|div|span|pre|code|em|i|b|u|"
            r"strong|small|hr|img|sub|sup|figure|figcaption|section|article|h[1-6])")
TAG_ONLY = re.compile(rf"^\s*</?{HTML_TAG}(?:\s[^<>]*)?/?>\s*(?:[\d.,%\s]*)\s*"
                      rf"(?:</{HTML_TAG}\s*>)?\s*$", re.I)


def _rule_repeated_boilerplate(lines: list[str]) -> set[int]:
    """Running headers/footers: the same substantive line on 4+ pages. Largest single class
    (3.4% of corpus chars). Threshold 4 (not 2) so a genuinely repeated sentence survives; the
    length floor keeps short real repeats ('Table 1', 'Notes') out of scope."""
    counts = Counter(s for ln in lines if len(s := ln.strip()) > 12)
    repeated = {s for s, c in counts.items() if c >= 4}
    return {i for i, ln in enumerate(lines) if ln.strip() in repeated and len(ln.strip()) > 12}


def _rule_toc_leaders(lines: list[str]) -> set[int]:
    return {i for i, ln in enumerate(lines) if TOC_LEADER.search(ln.strip())}


def _rule_page_markers(lines: list[str]) -> set[int]:
    out = set()
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s and PAGE_MARKER.match(s) and any(c.isdigit() or c.isalpha() for c in s):
            out.add(i)
    return out


def _rule_orphan_chars(lines: list[str]) -> set[int]:
    """1-2 character lines — column-break debris. Kept separate from page_markers so a corpus
    of CJK docs (where a 1-char line can be meaningful) can enable one without the other."""
    return {i for i, ln in enumerate(lines) if 0 < len(ln.strip()) <= 2}


def _rule_patent_id_soup(lines: list[str]) -> set[int]:
    """'US20150157190A1' / 'US 20150157190 A1' / 'US14/564,744' restated 8x per patent. Only
    fires when the line is *nothing but* an identifier, so an in-sentence citation survives.
    Real soup always comes in runs, so a lone match (a DOE award number 'EE0008757', a World
    Bank indicator id) survives unless its nearest non-blank neighbor is also an identifier."""
    def is_id(ln: str) -> bool:
        s = ln.strip()
        return bool(PATENT_ID.match(s)) and not MONEY_BLOB.match(s) and not CATALOG_BLOB.match(s)

    ids = [is_id(ln) for ln in lines]
    out = set()
    for i, hit in enumerate(ids):
        if not hit:
            continue
        prev = next((j for j in range(i - 1, -1, -1) if lines[j].strip()), None)
        nxt = next((j for j in range(i + 1, len(lines)) if lines[j].strip()), None)
        if (prev is not None and ids[prev]) or (nxt is not None and ids[nxt]):
            out.add(i)
    return out


def _rule_ocr_debris(lines: list[str]) -> set[int]:
    """No letters, 2+ punctuation marks, AND digits under 10% of the line: the digit guard is
    load-bearing. Without it this rule deleted every decimal-bearing numeric table row (NBS gas
    properties, EnergyPlus PV coefficients, European comma-decimal tables), reference tails
    ('2019;48(21):5310-49.') and equation numbers — the 2026-08 audit measured 78% of its hits
    as real content, and re-measured ~0% with this guard."""
    out = set()
    for i, ln in enumerate(lines):
        s = ln.strip()
        if len(s) < 2 or LETTERLIKE.search(s):
            continue
        dense = s.replace(" ", "")
        if dense and sum(c.isdigit() for c in dense) / len(dense) >= 0.10:
            continue
        if len(PUNCT_RUN.findall(s)) >= 2:
            out.add(i)
    return out


# Google Patents page furniture. Every patent page (114k docs, 55% of the corpus) carries the
# same scaffolding: navigation labels, three verbatim legal disclaimers, WIPO/CNIPA legal-event
# codes, 12-digit substance identifiers, bare ISO dates from the events table. All exact-shape
# matches; single common words ('granted', 'filed') are deliberately NOT listed — a wrapped
# prose line could consist of exactly that word, and deleting it would corrupt a sentence.
PATENT_FURNITURE = frozenset({
    "Download PDF", "Global Dossier", "Espacenet", "Similar Documents", "Legal Events",
    "Prior art keywords", "Prior art date", "Original Assignee", "legal-status", "not_active",
    "Anticipated expiration", "Adjusted expiration", "Discuss", "Landscapes", "Classifications",
    "Other languages", "Other versions", "Also Published As", "USPTO", "USPTO PatentCenter",
    "USPTO Assignment", "Year of fee payment", "Fee payment procedure", "Maintenance fee payment",
    "- Google Patents", "* Cited by examiner, † Cited by third party",
    "Chemical & Material Sciences", "Engineering & Computer Science", "Physics & Mathematics",
    "General Engineering & Computer Science", "Life Sciences & Earth Sciences",
    "Information on status: patent grant", "PATENTED CASE",
    "Entry into force of request for substantive examination",
    "Entry into substantive examination",
    "ASSIGNMENT OF ASSIGNORS INTEREST (SEE DOCUMENT FOR DETAILS).",
    "Legal status (The legal status is an assumption and is not a legal conclusion. Google has "
    "not performed a legal analysis and makes no representation as to the accuracy of the "
    "status listed.)",
    "Current Assignee (The listed assignees may be inaccurate. Google has not performed a legal "
    "analysis and makes no representation or warranty as to the accuracy of the list.)",
    "Priority date (The priority date is an assumption and is not a legal conclusion. Google "
    "has not performed a legal analysis and makes no representation as to the accuracy of the "
    "date listed.)",
    "GR01", "PB01", "SE01", "STCF", "MAFP", "FEPP", "TA01", "CB02", "C06", "C10", "C14",
})
SUBSTANCE_CODE = re.compile(r"^\d{12}$")   # '238000000034' — Google Patents compound ids
BARE_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # events-table date cell alone on a line

# Cross-doc site chrome from crawled Q&A/doc pages (Unmet Hours navigation and footer). These
# repeat once per PAGE, so repeated_boilerplate (4+ per doc) can never see them.
SITE_CHROME = frozenset({
    "Site design and logo: Copyright © 2015 Big Ladder Software LLC. All rights reserved.",
    "names and logos are trademarks of Big Ladder Software LLC.",
    "Question-and-Answer Resource for the Building Energy Modeling Community",
    "Hi there! Please sign in", "First time here? Check out the",
    "Get started with the Help page", "Ask Your Question", "Sort by »", "UNANSWERED",
    "User contributions licensed under the", "Powered by",
    "Creative Commons Attribution Share Alike 3.0 License",
})


def _rule_patent_furniture(lines: list[str]) -> set[int]:
    """Google Patents scaffolding: exact labels/disclaimers, legal-event codes, 12-digit
    substance ids, bare ISO-date lines. Shape-exact so patent CLAIMS and descriptions — the
    content we keep patents for — are never touched."""
    out = set()
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s in PATENT_FURNITURE or SUBSTANCE_CODE.match(s) or BARE_ISO_DATE.match(s):
            out.add(i)
    return out


def _rule_site_chrome(lines: list[str]) -> set[int]:
    return {i for i, ln in enumerate(lines) if ln.strip() in SITE_CHROME}


def _rule_code_annotations(lines: list[str]) -> set[int]:
    """Modelica diagram geometry and bare HTML tags from repo docs (the github shard measures
    39.8% non-prose, nearly all of this). The physics equations in a .mo file are NOT touched.

    Coordinate blobs are claimed ONLY as continuations chained under an annotation line: a
    standalone 4,000-doc sample found 3,883 free-floating COORD_BLOB matches and essentially
    all were citation tails ('75, 1543-1545, 2004.'), index page runs, or money columns."""
    out = set()
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s and (MODELICA_ANNOT.match(ln) or TAG_ONLY.match(s)):
            out.add(i)
    for i, ln in enumerate(lines):
        s = ln.strip()
        if i - 1 in out and i not in out and len(s) > 6 and COORD_BLOB.match(s) \
                and s.count(",") >= 2:
            out.add(i)
    return out


RULES = {
    "repeated_boilerplate": _rule_repeated_boilerplate,
    "toc_leaders": _rule_toc_leaders,
    "page_markers": _rule_page_markers,
    "orphan_chars": _rule_orphan_chars,
    "patent_id_soup": _rule_patent_id_soup,
    "patent_furniture": _rule_patent_furniture,
    "site_chrome": _rule_site_chrome,
    "ocr_debris": _rule_ocr_debris,
    "code_annotations": _rule_code_annotations,
}

RULE_DOC = {
    "repeated_boilerplate": "running headers/footers repeated on 4+ pages",
    "toc_leaders":          "table-of-contents dot-leader lines",
    "page_markers":         "lines that are only a page number",
    "orphan_chars":         "1-2 character column-break debris",
    "patent_id_soup":       "patent numbers restated in every format variant",
    "patent_furniture":     "Google Patents page scaffolding (labels, disclaimers, codes)",
    "site_chrome":          "crawled-site navigation/footer chrome (Unmet Hours)",
    "ocr_debris":           "punctuation-only lines from scanned pages",
    "code_annotations":     "Modelica diagram geometry, bare HTML tags",
}


def split_header(text: str) -> tuple[str, str]:
    """Return (header_including_separator, body). The provenance header is always preserved."""
    if HEADER_SEP in text:
        head, body = text.split(HEADER_SEP, 1)
        return head + HEADER_SEP, body
    return "", text


def collapse_blanks(lines: list[str]) -> list[str]:
    """Removing lines leaves runs of blanks behind; 3+ consecutive collapse to one. Always on
    when any rule is active — it repairs this stage's own damage, it isn't a cleaning policy."""
    out: list[str] = []
    blanks = 0
    for ln in lines:
        if ln.strip():
            blanks = 0
            out.append(ln)
        else:
            blanks += 1
            if blanks <= 1:
                out.append(ln)
    return out


def clean_body(body: str, rules: list[str]) -> tuple[str, Counter]:
    """Apply `rules` to a body. Returns (cleaned_body, per-rule dropped-char attribution).
    A line dropped by two rules is attributed to the first that claimed it, so the per-rule
    numbers sum to the true total instead of double-counting."""
    lines = body.split("\n")
    attribution: Counter = Counter()
    claimed: set[int] = set()
    for name in rules:
        hits = RULES[name](lines) - claimed
        attribution[name] = sum(len(lines[i]) + 1 for i in hits)
        claimed |= hits
    if not claimed:
        return body, attribution
    kept = [ln for i, ln in enumerate(lines) if i not in claimed]
    return "\n".join(collapse_blanks(kept)), attribution


def _clean_one(task: tuple) -> tuple[str, int, dict, str, str | None]:
    """Clean one doc. Module-level and returning plain data so it can run in a process pool.

    Returns (id, corpus_chars, per-rule attribution, status, sha256-if-written)."""
    sid, text_path, rules, rebuild = task
    src = HERE / text_path
    dst = CORPUS / f"{sid}.md"
    if not src.exists():
        return sid, 0, {}, "missing-text", None
    # Canonical name corpus/<id>.md regardless of what text_path is called — this normalizes rows
    # whose text_path drifted from their id (the iea- -> iag- rename left 33 of them).
    if not rebuild and dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return sid, len(split_header(dst.read_text(errors="replace"))[1]), {}, "up-to-date", None
    header, body = split_header(src.read_text(errors="replace"))
    if rules:
        cleaned, attr = clean_body(body, rules)
    else:
        cleaned, attr = body, Counter()
    # Unlink before writing: the pass-through path may have left a hard link, and an in-place write
    # would then follow it back into text/ and corrupt the verbatim stage.
    if dst.exists():
        dst.unlink()
    if rules:
        dst.write_text(header + cleaned)
    else:
        shutil.copyfile(src, dst)  # byte-identical pass-through
    digest = hashlib.sha256(dst.read_bytes()).hexdigest()
    return sid, len(cleaned), dict(attr), "written", digest


def stamped_ruleset() -> str:
    """The ruleset that produced the current corpus/ ('none' if never built). An IN-PROGRESS
    stamp names the intended set — return it so a crashed run resumes the same policy."""
    if not STAMP.exists():
        return "none"
    spec = STAMP.read_text().strip()
    if spec.startswith("IN-PROGRESS"):
        spec = spec.split(None, 1)[1].strip() if " " in spec else ""
    return spec or "none"


def parse_rules(spec: str) -> list[str]:
    spec = spec.strip()
    if not spec or spec == "none":
        return []
    if spec == "all":
        return list(RULES)
    names = [s.strip() for s in spec.split(",") if s.strip()]
    if bad := [n for n in names if n not in RULES]:
        raise SystemExit(f"unknown rule(s): {', '.join(bad)}\n"
                         f"available: {', '.join(RULES)} (or 'all' / 'none')")
    return [n for n in RULES if n in names]  # canonical order -> stable attribution


def partition_training_rows(
    rows: list[dict], restrictions: dict[str, dict], policy: dict[str, dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Split cleaner inputs from policy-restricted rows whose provenance must remain.

    Successful rows on a fetch-suspended host whose text is not on this machine (e.g. a fresh
    clone) are "locally unavailable, suspended": neither cleaned nor expected in corpus/ (see
    registry.suspended_unavailable); ``locally_unavailable`` reports them explicitly.
    """
    restricted = [r for r in rows if registry.restriction_for(r, restrictions) is not None]
    eligible, _ = registry.partition_manifest_ok_rows(rows, restrictions)
    missing = {r["id"] for r in locally_unavailable(eligible, restrictions, policy)}
    todo = [r for r in eligible if r.get("text_path") and r["id"] not in missing]
    return todo, restricted


def locally_unavailable(rows: list[dict], restrictions: dict[str, dict],
                        policy: dict[str, dict] | None = None) -> list[dict]:
    eligible, _ = registry.partition_manifest_ok_rows(rows, restrictions)
    return registry.locally_unavailable_rows(eligible, policy, root=HERE)


def clear_corpus_metadata(rows: list[dict]) -> int:
    """Remove training-artifact claims while preserving fetch/extraction provenance."""
    changed = 0
    for row in rows:
        if any(field in row for field in registry.CORPUS_FIELDS):
            changed += 1
        for field in registry.CORPUS_FIELDS:
            row.pop(field, None)
    return changed


def quarantine_policy_files(rows: list[dict]) -> int:
    """Move existing restricted corpus copies aside without deleting derived data."""
    names = {f"{r['id']}.md" for r in rows}
    files = [p for p in CORPUS.glob("*.md") if p.name in names]
    if not files:
        return 0
    POLICY_QUARANTINE.mkdir(parents=True, exist_ok=True)
    collisions = [p.name for p in files if (POLICY_QUARANTINE / p.name).exists()]
    if collisions:
        raise RuntimeError(
            "policy quarantine already contains target(s): " + ", ".join(collisions[:5])
        )
    for path in files:
        path.replace(POLICY_QUARANTINE / path.name)
    return len(files)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", default="stamp",
                    help="comma-separated rule names, 'all', 'none', or 'stamp' (default) = "
                         "reuse the ruleset that produced the current corpus/ — so the round "
                         "loop (run_round.py calls this with no args) preserves the active "
                         "cleaning policy instead of silently resetting to pass-through")
    ap.add_argument("--report", action="store_true",
                    help="measure what each rule would remove; write nothing")
    ap.add_argument("--sample", type=int, default=0,
                    help="with --report: only read the first N docs per shard (fast estimate)")
    ap.add_argument("--list-rules", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="verify corpus/ agrees with the manifest (paths, char counts, no extra "
                         "or missing files); exit 1 on drift. Writes nothing.")
    ap.add_argument("--force", action="store_true", help="rewrite every doc, ignore the stamp")
    ap.add_argument("--lock-timeout", type=float, default=60,
                    help="standalone runs wait this long for the round lock")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help=f"parallel worker processes (default {DEFAULT_WORKERS} on this machine)")
    args = ap.parse_args()

    if args.list_rules:
        print("available rules (all opt-in; default is pass-through):")
        for name in RULES:
            print(f"  {name:22s} {RULE_DOC[name]}")
        return

    st = store.open(root=HERE)
    if not (args.report or args.check):
        # build mode: the round's inherited view and broker, or this command's own writer (the
        # round lock) for the whole read-clean-patch sequence. The ruleset stamp and the policy
        # are read under it: eligibility and host policy from the view's pinned configuration.
        with store_broker.step_session(st, "clean", timeout=args.lock_timeout) as session:
            access = artifact_store.for_view(session.view, HERE)
            if access is not None:   # a versioned staged run (ADR 0001 stage 4 step 3)
                build_versioned(session, access, args)
                return
            rules = parse_rules(stamped_ruleset() if args.rules == "stamp" else args.rules)
            restrictions, policy = store.pinned_policy(session.view)
            rows = list(corpus_stats.iter_manifest(session.view))
            todo, restricted = partition_training_rows(rows, restrictions, policy)
            build(session, todo, restricted, rules, args)
        return
    # read-only modes: one consistent store view, rows in the legacy manifest order (the seeded
    # per-shard sample and first-N diagnostics depend on it).
    with st.read(timeout=args.lock_timeout) as view:
        access = artifact_store.for_view(view, HERE)
        if access is not None:   # a versioned staged run: its claims, not corpus/, are checked
            if args.report:
                raise SystemExit("--report measures the legacy corpus; run it outside a staged run")
            check_versioned(view, access)
            return
        rules = parse_rules(stamped_ruleset() if args.rules == "stamp" else args.rules)
        restrictions, policy = store.pinned_policy(view)
        rows = list(corpus_stats.iter_manifest(view))
    todo, restricted = partition_training_rows(rows, restrictions, policy)

    # --------------------------------------------------------------------- report mode
    if args.report:
        if args.sample:
            # RANDOM per shard, not the first N: manifest order is discovery order, so the head of
            # a shard is one vein/crawl and misses whole document classes (taking the first 40
            # github rows found zero Modelica .mo files, reporting code_annotations as 0.00%).
            # Fixed seed so a repeated --report gives the same number.
            by_shard: dict[str, list] = {}
            for r in todo:
                by_shard.setdefault(registry.manifest_shard(r["id"]), []).append(r)
            rng = random.Random(0)
            todo = []
            for shard_rows in by_shard.values():
                rng.shuffle(shard_rows)
                todo += shard_rows[:args.sample]
        measure = list(RULES) if not rules else rules
        total_chars = 0
        attribution: Counter = Counter()
        read = 0
        for r in todo:
            f = HERE / r["text_path"]
            if not f.exists():
                continue
            body = split_header(f.read_text(errors="replace"))[1]
            total_chars += len(body)
            read += 1
            _, attr = clean_body(body, measure)
            attribution.update(attr)
        if not total_chars:
            print("nothing to measure")
            return
        print(f"read {read} docs / {total_chars/1e6:.1f}M body chars"
              f"{f' (sample {args.sample}/shard)' if args.sample else ' (full corpus)'}\n")
        print("per-rule removal (first rule to claim a line owns it):")
        for name in measure:
            c = attribution[name]
            print(f"  {100*c/total_chars:6.2f}%  {c/1e6:8.2f}M  {name:22s} {RULE_DOC[name]}")
        tot = sum(attribution.values())
        print(f"\n  COMBINED: {100*tot/total_chars:.2f}%  ({tot/1e6:.2f}M chars) would be removed")
        print("\nwrite nothing yet — choose a ruleset, then re-run without --report")
        return

    # --------------------------------------------------------------------- check mode
    if args.check:
        stamp = STAMP.read_text().strip() if STAMP.exists() else "(none written)"
        problems: list[str] = []
        if stamp.startswith("IN-PROGRESS"):
            problems.append(f"stamp says a run never finished: {stamp!r} — re-run to rebuild")
        restricted_with_metadata = [
            r for r in restricted if any(field in r for field in registry.CORPUS_FIELDS)
        ]
        for r in restricted_with_metadata[:10]:
            problems.append(f"policy-restricted row has corpus metadata: {r['id']}")
        if len(restricted_with_metadata) > 10:
            problems.append(
                f"... and {len(restricted_with_metadata) - 10} more restricted metadata rows"
            )
        expect = {f"{r['id']}.md" for r in todo}
        if unavailable := locally_unavailable(rows, restrictions, policy):
            print(f"locally unavailable, suspended host or unrestored programme row "
                  f"(provenance kept, not expected in corpus/): {len(unavailable):,} rows, "
                  f"e.g. {unavailable[0]['id']}")
        on_disk = {p.name for p in CORPUS.glob("*.md")}
        for name in sorted(expect - on_disk)[:10]:
            problems.append(f"missing from corpus/: {name}")
        for name in sorted(on_disk - expect)[:10]:
            problems.append(f"unprovenanced file in corpus/: {name}")
        if len(expect - on_disk) > 10:
            problems.append(f"... and {len(expect - on_disk) - 10} more missing")
        if len(on_disk - expect) > 10:
            problems.append(f"... and {len(on_disk - expect) - 10} more unprovenanced")
        # char-count drift: the manifest's corpus_chars must match the file actually on disk.
        # This is what catches a partially-applied ruleset (disk cleaned, manifest still says
        # pass-through), which is otherwise completely invisible.
        drift = 0
        hash_drift = 0
        checked = 0
        for r in todo:
            p = CORPUS / f"{r['id']}.md"
            if not p.exists():
                continue
            checked += 1
            actual = len(split_header(p.read_text(errors="replace"))[1])
            if actual != r.get("corpus_chars"):
                drift += 1
                if drift <= 5:
                    problems.append(f"corpus_chars drift {r['id']}: manifest="
                                    f"{r.get('corpus_chars')} disk={actual}")
            expected_hash = r.get("corpus_sha256")
            if expected_hash and hashlib.sha256(p.read_bytes()).hexdigest() != expected_hash:
                hash_drift += 1
                if hash_drift <= 5:
                    problems.append(f"corpus_sha256 drift {r['id']}")
        print(f"checked {checked} docs | ruleset stamp: {stamp}")
        if drift > 5:
            problems.append(f"... and {drift - 5} more rows with corpus_chars drift")
        if hash_drift > 5:
            problems.append(f"... and {hash_drift - 5} more rows with corpus_sha256 drift")
        if problems:
            print(f"\nDRIFT — {len(expect - on_disk)} missing, {len(on_disk - expect)} "
                  f"unprovenanced, {drift} char-count mismatches, "
                  f"{hash_drift} hash mismatches:")
            for p_ in problems:
                print(f"  {p_}")
            print("\nfix: python scripts/clean_corpus.py --force --rules <the ruleset you want>")
            raise SystemExit(1)
        print("OK — corpus/ matches the manifest exactly")
        return


def _same_value(a, b) -> bool:
    """Whether two field values are the same stored JSON (1 vs 1.0 vs true differ)."""
    return store.canonical_row({"v": a}) == store.canonical_row({"v": b})


def corpus_patches(todo: list[dict], before: dict[str, dict]) -> dict[str, dict]:
    """The corpus fields that changed per row (in CORPUS_FIELDS order, as the legacy writer
    appended new keys), against `before` (id -> the row's corpus fields when read)."""
    patches = {}
    for r in todo:
        old = before[r["id"]]
        patch = {f: r[f] for f in registry.CORPUS_FIELDS
                 if f in r and (f not in old or not _same_value(old[f], r[f]))}
        if patch:
            patches[r["id"]] = patch
    return patches


def commit_metadata(session, patches: dict[str, dict], cleared: list[str]) -> int:
    """Patch the manifest in bounded batches, each one short store transaction: first unset the
    corpus fields of policy-restricted rows, then set the changed corpus fields. Batches follow
    manifest shard order, so a batch touches few shards. Returns the number of transactions."""
    n = 0
    for chunk in store_broker.shard_batches(cleared, METADATA_BATCH_ROWS):
        n += 1
        with session.batch(f"restricted-{n:04d}") as b:
            b.update_manifest_fields({sid: {} for sid in chunk}, unset=registry.CORPUS_FIELDS)
    m = 0
    for chunk in store_broker.shard_batches(list(patches), METADATA_BATCH_ROWS):
        m += 1
        with session.batch(f"meta-{m:04d}") as b:
            b.update_manifest_fields({sid: patches[sid] for sid in chunk})
    return n + m


def build(session, todo: list[dict], restricted: list[dict], rules: list[str], args) -> None:
    """Refresh corpus/ and record it. Artifacts (corpus/*.md) are written first, outside any
    transaction; then the changed corpus fields are patched in short transactions; the ruleset
    stamp is published only after the last one committed. Until then the stamp reads
    IN-PROGRESS, so a crash anywhere in between makes the next run rebuild every document and
    re-patch whatever did not commit (a rebuild with an unchanged ruleset reproduces the same
    bytes, hence no spurious patches)."""
    CORPUS.mkdir(parents=True, exist_ok=True)
    stamp_now = ",".join(rules) if rules else "none"
    stamp_was = STAMP.read_text().strip() if STAMP.exists() else None
    rebuild = args.force or stamp_was != stamp_now
    if args.force:
        print(f"--force: rebuilding every doc (ruleset {stamp_now})")
    elif rebuild and stamp_was is not None:
        if stamp_was.startswith("IN-PROGRESS"):
            print(f"previous run did not finish ({stamp_was}) — rebuilding every doc")
        else:
            print(f"ruleset changed ({stamp_was} -> {stamp_now}) — rebuilding every doc")

    # Mark the stamp in-progress BEFORE writing anything. A killed run leaves cleaned files whose
    # mtime is newer than their text/ source, which the incremental check would happily accept as
    # up-to-date — so an interrupted run must never leave a stamp that any later run can match.
    ops.atomic_write_text(STAMP, f"IN-PROGRESS {stamp_now}\n")

    attribution: Counter = Counter()
    stats: Counter = Counter()
    before = {r["id"]: {f: r[f] for f in registry.CORPUS_FIELDS if f in r} for r in todo}
    cleared = [r["id"] for r in restricted if any(f in r for f in registry.CORPUS_FIELDS)]
    by_id = {r["id"]: r for r in todo}
    tasks = [(r["id"], r["text_path"], rules, rebuild) for r in todo]

    # Processes, not threads: the rules are regex-bound, so a thread pool stays pinned at ~1 core
    # (measured 108% CPU on 12 threads). Chunked to amortize pickling over 104k tiny tasks.
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for sid, chars, attr, status, digest in pool.map(_clean_one, tasks, chunksize=64):
            stats[status] += 1
            if status == "missing-text":
                continue
            row = by_id[sid]
            row["corpus_path"] = f"corpus/{sid}.md"
            row["corpus_chars"] = chars
            if digest:
                row["corpus_sha256"] = digest
                row["cleaner_version"] = f"clean_corpus/2;rules={stamp_now}"
            attribution.update(attr)

    # Policy exclusions are broad and reversible: move their derived corpus copy out of the
    # training directory instead of deleting it. raw/ and verbatim text/ are never touched.
    restricted_names = {f"{r['id']}.md" for r in restricted}
    quarantined = quarantine_policy_files(restricted)

    # Drop other corpus files with no manifest row (pruned docs, renamed ids) — corpus/ mirrors
    # the provenance record exactly, so a training run over corpus/* can't read unprovenanced text.
    # Derived bytes only: text/ still holds every retained document's verbatim source.
    live = {f"{r['id']}.md" for r in todo}
    orphans = [
        p for p in CORPUS.glob("*.md")
        if p.name not in live and p.name not in restricted_names
    ]
    for p in orphans:
        p.unlink()

    patches = corpus_patches(todo, before)
    batches = commit_metadata(session, patches, cleared)
    # last: only a fully-finished run (every metadata batch committed) may claim its ruleset
    ops.atomic_write_text(STAMP, stamp_now + "\n")

    kept = sum(r.get("corpus_chars", 0) for r in todo)
    print(f"corpus/: {stats['written']} written | {stats['up-to-date']} up-to-date | "
          f"{stats['missing-text']} missing text | {len(orphans)} orphans removed")
    print(f"policy restricted: {len(restricted)} rows | {len(cleared)} manifest rows cleared | "
          f"{quarantined} corpus files quarantined")
    print(f"manifest: {len(patches)} rows patched in {batches} transaction(s)")
    print(f"ruleset: {stamp_now}")
    print(f"corpus chars: {kept/1e6:.1f}M ({kept//4/1e6:.0f}M tokens)")
    if attribution:
        tot = sum(attribution.values())
        print(f"removed {tot/1e6:.2f}M chars this run:")
        for name, c in attribution.most_common():
            if c:
                print(f"  {c/1e6:8.2f}M  {name}")


# --- versioned cleaning (ADR 0001 stage 4 step 3: a PostgreSQL staged run) -------------------------
#
# The run, not corpus/.ruleset, is the cleaning policy authority: its ruleset was recorded when it
# was opened (runs.cleaning_ruleset, copied into the generation it promotes). Cleaned bytes become
# immutable versions (artifact_store); corpus/ is never written here — it is a materialization of
# a promoted generation (scripts/materialize.py). A row is up to date when its cleaner version is
# this ruleset's, its source identity is the text it now claims, and its cleaned version is held
# locally; everything else is cleaned (again), with the same rules and byte for byte the output
# of the legacy cleaner. Policy-restricted rows lose their corpus fields exactly as before; no
# file is moved or deleted.

# cleaned versions made durable together (two syncfs per group instead of fsyncs per file)
GROUP_COMMIT = 5000
# what Path.write_text uses (the legacy cleaner wrote corpus/ with it)
_WRITE_ENCODING = "utf-8" if sys.flags.utf8_mode else locale.getencoding()


def _clean_one_versioned(task: tuple) -> tuple:
    """Clean one document's text into a pending corpus version (committed by the parent in
    groups, artifact_store.LocalArtifacts.commit). Returns (id, corpus_chars, per-rule
    attribution, status, the pending version, source text sha256)."""
    sid, src, rules, root, owner = task
    src = Path(src)
    data = src.read_bytes()
    source = hashlib.sha256(data).hexdigest()
    header, body = split_header(src.read_text(errors="replace"))  # exactly the legacy reading
    if rules:
        cleaned, attr = clean_body(body, rules)
        out = (header + cleaned).encode(_WRITE_ENCODING)
    else:
        cleaned, attr, out = body, Counter(), data   # byte-identical pass-through
    pending = artifact_store.write_pending(Path(root), "corpus", out, owner)
    return sid, len(cleaned), dict(attr), "written", pending, source


def cleaner_tag(stamp: str) -> str:
    return f"clean_corpus/2;rules={stamp}"


def versioned_fresh(row: dict, access, tag: str) -> bool:
    source = row.get("corpus_source_sha256")
    text_id = row.get("text_sha256")
    return (row.get("cleaner_version") == tag
            and row.get("corpus_path") == f"corpus/{row['id']}.md"
            and isinstance(row.get("corpus_sha256"), str)
            and isinstance(source, str) and (text_id is None or source == text_id)
            and access.local.has("corpus", row["corpus_sha256"]))


def _clean_many_versioned(tasks: list[tuple]) -> list[tuple]:
    """A chunk of _clean_one_versioned tasks (one pool submission)."""
    return [_clean_one_versioned(t) for t in tasks]


# tasks per pool submission, and submissions in flight per worker (bounds the parent's memory)
CLEAN_CHUNK = 64
IN_FLIGHT_PER_WORKER = 4


class _VersionedBuild:
    """build_versioned's state, bounded whatever the corpus size: one manifest page, at most
    workers x IN_FLIGHT_PER_WORKER x CLEAN_CHUNK documents being cleaned, one group of at most
    GROUP_COMMIT cleaned versions awaiting durability and at most METADATA_BATCH_ROWS patches
    (or restricted ids) awaiting their batch. A group's versions are made durable BEFORE its
    rows are patched; the patches are staged as soon as a batch is full."""

    def __init__(self, session, access, rules, tag):
        self.session, self.access, self.rules, self.tag = session, access, rules, tag
        self.stats: Counter = Counter()
        self.attribution: Counter = Counter()
        self.mismatched: list[str] = []
        self.group: list[tuple] = []          # (row's before-image fields, result)
        self.patches: dict[str, dict] = {}
        self.cleared: list[str] = []
        self.n_meta = self.n_restricted = 0
        self.patched = self.cleared_total = 0

    def result(self, before: dict, res: tuple) -> None:
        sid, chars, attr, status, pending, source = res
        if before["text_sha256"] not in (None, source):
            # the text payload does not hold the identity its row claims: surface it
            self.stats["text-mismatch"] += 1
            if len(self.mismatched) < 5:
                self.mismatched.append(sid)
            return
        self.stats[status] += 1
        self.group.append((before, res))
        if len(self.group) >= GROUP_COMMIT:
            self.commit_group()

    def commit_group(self) -> None:
        """Make the group's cleaned versions durable (two syncs), then — only then — let their
        rows claim them."""
        if not self.group:
            return
        self.access.local.commit([res[4] for _, res in self.group])
        for before, (sid, chars, attr, _status, pending, source) in self.group:
            new = {"corpus_path": f"corpus/{sid}.md", "corpus_chars": chars,
                   "corpus_sha256": pending.sha256, "cleaner_version": self.tag,
                   "corpus_source_sha256": source}
            patch = {f: new[f] for f in registry.CORPUS_FIELDS
                     if f not in before["fields"] or not _same_value(before["fields"][f], new[f])}
            if patch:
                self.patches[sid] = patch
            self.attribution.update(attr)
        self.group.clear()
        if len(self.patches) >= METADATA_BATCH_ROWS:
            self.flush_patches()

    def flush_patches(self) -> None:
        ids = list(self.patches)
        for start in range(0, len(ids), METADATA_BATCH_ROWS):
            part = ids[start:start + METADATA_BATCH_ROWS]
            self.n_meta += 1
            with self.session.batch(f"meta-{self.n_meta:04d}") as b:
                b.update_manifest_fields({sid: self.patches[sid] for sid in part})
            self.patched += len(part)
        self.patches = {}

    def clear(self, sid: str) -> None:
        self.cleared.append(sid)
        if len(self.cleared) >= METADATA_BATCH_ROWS:
            self.flush_cleared()

    def flush_cleared(self) -> None:
        if self.cleared:
            self.n_restricted += 1
            with self.session.batch(f"restricted-{self.n_restricted:04d}") as b:
                b.update_manifest_fields({sid: {} for sid in self.cleared},
                                         unset=registry.CORPUS_FIELDS)
            self.cleared_total += len(self.cleared)
            self.cleared = []


def build_versioned(session, access, args) -> None:
    import host_policy
    from concurrent.futures import FIRST_COMPLETED, wait
    prov = artifact_store.run_policy(session.view)
    pinned = parse_rules(prov["cleaning_ruleset"])
    if args.rules != "stamp" and parse_rules(args.rules) != pinned:
        raise SystemExit(f"this run's cleaning ruleset is {prov['cleaning_ruleset']!r} (pinned when "
                         f"it opened); --rules {args.rules} is refused")
    rules = pinned
    stamp_now = ",".join(rules) if rules else "none"
    tag = cleaner_tag(stamp_now)
    restrictions, policy = store.pinned_policy(session.view)
    access.local.sweep_incoming()
    job = _VersionedBuild(session, access, rules, tag)
    workers = max(1, args.workers)
    limit = workers * IN_FLIGHT_PER_WORKER
    chunk: list[tuple] = []
    chunk_before: list[dict] = []
    in_flight: dict = {}   # future -> the before-images of its documents

    def drain(block_until: int) -> None:
        while len(in_flight) > block_until:
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for fut in done:
                befores = in_flight.pop(fut)
                for before, res in zip(befores, fut.result()):
                    job.result(before, res)

    with ProcessPoolExecutor(max_workers=workers) as pool:
        def submit() -> None:
            nonlocal chunk, chunk_before
            if chunk:
                in_flight[pool.submit(_clean_many_versioned, chunk)] = chunk_before
                chunk, chunk_before = [], []
                drain(limit)

        cursor = None
        while True:   # one manifest page at a time, in key order
            page = session.view.scan(store.Table.MANIFEST, cursor=cursor, limit=store.MAX_PAGE)
            for r in page.rows:
                # the full training predicate (license: pointer-only; eligibility.json), as the
                # legacy partition_training_rows applies it; an ineligible row keeps its
                # raw/text provenance and loses any corpus claim
                if not registry.is_training_eligible(r, restrictions):
                    job.stats["restricted"] += 1
                    if any(f in r for f in registry.CORPUS_FIELDS):
                        job.clear(r["id"])
                    continue
                if r.get("status") != "ok" or not r.get("text_path"):
                    continue
                if policy and host_policy.suspended(r.get("url") or "", policy) \
                        and not access.exists(r, "text"):
                    continue   # locally unavailable, suspended host
                if not args.force and versioned_fresh(r, access, tag):
                    job.stats["up-to-date"] += 1
                    continue
                src = access.path(r, "text")
                if src is None:
                    job.stats["missing-text"] += 1
                    continue
                chunk.append((r["id"], str(src), rules, str(access.root), os.getpid()))
                chunk_before.append({"text_sha256": r.get("text_sha256"), "fields": {
                    f: r[f] for f in registry.CORPUS_FIELDS if f in r}})
                if len(chunk) >= CLEAN_CHUNK:
                    submit()
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        submit()
        drain(0)
    job.commit_group()
    job.flush_patches()
    job.flush_cleared()
    stats = job.stats
    print(f"versioned clean (run {prov['run']}, ruleset {stamp_now}): {stats['written']} "
          f"written | {stats['up-to-date']} up-to-date | {stats['missing-text']} missing text | "
          f"{stats['text-mismatch']} text payloads not matching their identity"
          + (f" (e.g. {job.mismatched})" if job.mismatched else ""))
    print(f"policy restricted: {stats['restricted']} rows | {job.cleared_total} manifest rows "
          "cleared")
    print(f"manifest: {job.patched} rows patched in {job.n_meta + job.n_restricted} batch(es); "
          "corpus/ is refreshed from the promoted generation (scripts/materialize.py)")
    attribution = job.attribution
    if attribution:
        tot = sum(attribution.values())
        print(f"removed {tot/1e6:.2f}M chars this run:")
        for name, c in attribution.most_common():
            if c:
                print(f"  {c/1e6:8.2f}M  {name}")


def check_versioned(view, access) -> None:
    """--check inside a versioned staged run (e.g. a gate at the frozen sequence): every eligible
    row with text is cleaned under the run's ruleset from the text it claims, its cleaned version
    is held locally, and no restricted row claims corpus data. corpus/ itself is a
    materialization of a promoted generation and is not what a staged run is checked against.
    Reads only (the artifact gate re-hashes the versions a run introduced)."""
    prov = artifact_store.run_policy(view)
    rules = parse_rules(prov["cleaning_ruleset"])
    tag = cleaner_tag(",".join(rules) if rules else "none")
    restrictions, policy = store.pinned_policy(view)
    problems: list[str] = []
    checked = stale = 0
    import host_policy
    for r in corpus_stats.iter_manifest(view):
        if not registry.is_training_eligible(r, restrictions):
            if any(f in r for f in registry.CORPUS_FIELDS) and len(problems) < 20:
                problems.append(f"training-ineligible row (license or policy) has corpus "
                                f"metadata: {r['id']}")
            continue
        if r.get("status") != "ok" or not r.get("text_path"):
            continue
        if policy and host_policy.suspended(r.get("url") or "", policy) \
                and not access.exists(r, "text"):
            continue
        checked += 1
        if not versioned_fresh(r, access, tag):
            stale += 1
            if len(problems) < 20:
                problems.append(f"not cleaned under {tag} from its claimed text, or its cleaned "
                                f"version is missing: {r['id']}")
    print(f"checked {checked} docs of run {prov['run']} | ruleset: {prov['cleaning_ruleset']}")
    if problems:
        print(f"\nDRIFT — {stale} rows not cleaned as the run pins:")
        for p_ in problems:
            print(f"  {p_}")
        raise SystemExit(1)
    print("OK — every eligible row claims a cleaned version held locally")


if __name__ == "__main__":
    main()
