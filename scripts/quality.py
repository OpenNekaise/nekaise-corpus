#!/usr/bin/env python3
"""quality.py — the corpus quality gate: text metrics + keep/drop verdict.

Shared by build_corpus.py (computes metrics once at extraction time and stores them in the
manifest row) and prune_corpus.py (turns stored metrics into a verdict without re-reading 2 GB of
text). Golden regression tests in tests/test_quality.py pin the verdicts — run them whenever you
tune a threshold, and they will tell you exactly which class of document you just killed or saved.
"""
from __future__ import annotations

import re

DOMAIN = re.compile(
    r"build|hvac|energy|thermal|heat|cool|ventil|chiller|boiler|refriger|damper|ahu|vav|setpoint|"
    r"occupan|comfort|retrofit|envelope|commission|controller|sensor|actuator|bacnet|ashrae|kwh|"
    r"carbon|emission|psychrometr|economizer|fault|diagnos|efficien|insulat|"
    # AEC / built-environment: architecture, engineering, construction, infrastructure, materials
    r"struct|concret|cement|reinforc|rebar|masonr|timber|lumber|steel|weld|beam|column|"
    r"truss|girder|slab|foundation|footing|bearing|geotech|soil|slope|retain|excavat|"
    r"settlement|tunnel|bridge|deck|abutment|pavement|asphalt|aggregat|seismic|earthquake|"
    r"deflection|modulus|stress|strain|shear|bending|axial|construct|contractor|scaffold|"
    r"formwork|demolition|renovat|estimat|architect|facade|roof|floor|durab|corros|"
    r"fatigue|fracture|composite|civil|infrastructur|survey|geomat|\bbim\b|\bifc\b|hydraul|"
    r"drainag|culvert|fire|egress|sprinkler|smoke|osha|material|coating|polymer|elastic|"
    r"plastic|urban|zoning|transport|highway|traffic|wastewater|geolog|hazard|flood|"
    r"coastal|levee", re.I)
EN = re.compile(r"\b(the|and|of|to|in|is|for|that|with|are|this|be|as|by|on|from)\b", re.I)

# Long book-like docs are judged over a 100k-char window: their first 20k chars are front matter
# (title pages, TOC dot-leaders, OCR noise) that fails alpha-ratio checks and under-represents the
# content. Short docs (and source code, whatever its length) use the 20k window + absolute gate.
BOOK_MIN_CHARS = 120_000
# Off-topic gate for books: DOMAIN hits per 1000 words, calibrated 2026-07-07 on a 300-book OAPEN
# sample — clearly-unrelated books score < 8; real AEC books score 30-150.
BOOK_MIN_DENSITY = 8.0
SHORT_MIN_HITS = 10
MIN_ALPHA = 0.55
MIN_EN_RATIO = 0.04


def body(text: str) -> str:
    """Strip the `# title\\n\\nsource:...\\n---\\n\\n` header build_corpus puts on text/*.md."""
    return text.split("\n---\n\n", 1)[-1] if "\n---\n\n" in text else text


def _window(t: str) -> dict:
    words = re.findall(r"[A-Za-z]{2,}", t)
    return {
        "chars": len(t.strip()),
        "alpha": round(sum(c.isalpha() for c in t) / len(t), 6) if t else 0.0,
        "words": len(words),
        "en": len(EN.findall(t)),
        "domain": len(DOMAIN.findall(t)),
    }


def metrics(bod: str) -> dict:
    """Window stats for both gates + total length. `bod` is extracted text WITHOUT the header.
    Stored per-doc in manifest.jsonl (key `quality`) so verdicts never re-read the text."""
    return {"total": len(bod), "w20": _window(bod[:20_000]), "w100": _window(bod[:100_000])}


def is_booklike(sid: str, fmt: str) -> bool:
    """PDFs and arc- OCR texts are book-like; source code / repo docs (md/rst/txt) are not —
    code identifiers have book-unlike word statistics and the density rule falsely kills them."""
    return fmt == "pdf" or sid.startswith("arc-")


def verdict(m: dict, book: bool) -> str:
    is_book = book and m["total"] > BOOK_MIN_CHARS
    w = m["w100"] if is_book else m["w20"]
    if w["chars"] < 2000:
        return "thin"
    if w["alpha"] < MIN_ALPHA:
        return "garbage"
    if w["words"] < 200:
        return "thin"
    if w["en"] / w["words"] < MIN_EN_RATIO:
        return "non-english"
    density = 1000 * w["domain"] / w["words"]
    if (density < BOOK_MIN_DENSITY) if is_book else (w["domain"] < SHORT_MIN_HITS):
        return "off-topic"
    return "ok"


def assess(text: str, sid: str = "", fmt: str = "pdf") -> str:
    """One-shot verdict straight from raw text (header ok) — for spot checks and tests."""
    return verdict(metrics(body(text)), is_booklike(sid, fmt))
