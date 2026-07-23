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
    r"coastal|levee|"
    # multilingual built-environment vocabulary (corpus went all-language 2026-07-09):
    # German / French / Spanish-Portuguese / Italian / Dutch / Nordic (Latin script)
    r"gebäude|bauwesen|baustoff|beton|heizung|lüftung|dämmung|tragwerk|brandschutz|mauerwerk|"
    r"stahlbau|holzbau|energieeffizien|wärme|bauteil|"
    r"bâtiment|chauffage|climatisation|génie civil|charpente|maçonnerie|béton|isolation|"
    r"edificio|edifício|hormigón|concreto|calefacción|construcción|construção|estructura|"
    r"estrutura|albañiler|cimentación|edilizia|calcestruzzo|riscaldamento|impianti|muratura|"
    r"gebouw|verwarming|bouwkunde|byggnad|uppvärmning|bygning|rakennus|"
    # Nordic (sv/no/dk) — widened 2026-07-23: the 2-stem list wrongly off-topic'd Boverket docs
    # (a solar-park land-use planning report scored domain=9 < 10). "\bbygg" covers
    # bygga/byggande/byggeri/byggregler across sv/no/dk.
    r"\bbygg|bostad|bostäder|planering|detaljplan|översiktsplan|fastighet|isolering|"
    r"fjärrvärme|solcell|inomhusmiljö|stomme|brandskydd|\bfukt|renover|stadsplan|"
    r"infrastruktur|energieffektiv|klimatdeklaration|bolig|oppvarming|opvarmning|"
    r"\bbrann|byggeforskrift|"
    # Russian / Ukrainian (Cyrillic)
    r"здани|бетон|отоплен|вентиляц|строительств|фундамент|теплоснабжен|конструкци|"
    # Chinese (simplified + traditional), Japanese, Korean — matched as substrings
    r"建筑|建築|结构|構造|混凝土|钢筋|鋼筋|暖通|空调|空調|供热|供暖|采暖|通风|換気|通風|"
    r"节能|節能|省エネ|保温|保溫|断熱|隔热|锅炉|鍋爐|制冷|製冷|冷凍|桥梁|橋梁|隧道|"
    r"钢结构|鋼結構|鉄骨|抗震|耐震|岩土|地基|施工|工程|造价|给排水|排水|市政|城市规划|"
    r"都市計画|コンクリート|建材|건축|구조|공조|난방|단열|콘크리트", re.I)
EN = re.compile(r"\b(the|and|of|to|in|is|for|that|with|are|this|be|as|by|on|from)\b", re.I)
CJK = re.compile(r"[一-鿿㐀-䶿぀-ヿ가-힯]")

# Patent-title off-domain kill. The polysemous stems that route patent discovery ("insulat",
# "heating", "tunnel", "construction") also fire on semiconductor, smoking-device and
# quantum-electronics patents — and those slip the full-text DOMAIN gate too, because chip prose
# is saturated with "structure/thermal/insulator/stress". These classes are unambiguous from the
# TITLE alone, so they are killed there: find_patents.py skips them at discovery and
# prune_corpus.py drops already-ingested ones (reason "off-topic-title"). Calibrated 2026-07-19
# on the 29k-patent corpus: ~800 kills, sampled for collateral.
PATENT_KILL_HARD = re.compile(  # never rescued — "ventilated smoking article" is still a vape
    r"electronic cigarette|e-cigarette|smoking article|nicotine", re.I)
PATENT_KILL = re.compile(
    r"transistor|semiconductor|field[- ]effect|insulated[- ]gate bipolar|\bwafer\b|"
    r"photolithograph|integrated circuit|memory cell|\bdram\b|\bcmos\b|\bmosfet\b|\bigbt\b|"
    r"silicon[- ]on[- ]insulator|epitaxial|atomizer|"
    r"magnetic tunnel|tunnel junction|tunnel(?:ing)? (?:junction|magnetoresist|barrier|diode)|"
    r"quantum tunnel|bridge (?:circuit|rectifier)|wheatstone|dental bridge|"
    r"pile fabric|pile yarn|carpet pile|"
    r"(?:cosmetic|makeup|make-up).{0,30}foundation|foundation.{0,30}(?:cosmetic|makeup|concealer)|"
    r"construction of (?:a )?(?:point cloud|data|image|gene|genome|dna|plasmid|genetic)", re.I)
# Genuine AEC/HVAC anchors rescue a soft kill: "UHPC wafer board … bridge", Peltier
# ("semiconductor refrigeration") HVAC gear, IGBT drives inside air conditioners.
PATENT_GUARD = re.compile(
    r"wafer ?board|ventilat|dehumidif|heat pump|air.?condition|\bhvac\b|chiller|refrigerat|"
    r"formwork|concrete|masonry|girder|abutment|asphalt|pavement|curtain wall", re.I)


def off_domain_title(title: str) -> bool:
    """True if a patent TITLE alone marks the doc off-domain (see PATENT_KILL above)."""
    t = title or ""
    if PATENT_KILL_HARD.search(t):
        return True
    return bool(PATENT_KILL.search(t)) and not PATENT_GUARD.search(t)

# Long book-like docs are judged over a 100k-char window: their first 20k chars are front matter
# (title pages, TOC dot-leaders, OCR noise) that fails alpha-ratio checks and under-represents the
# content. Short docs (and source code, whatever its length) use the 20k window + absolute gate.
BOOK_MIN_CHARS = 120_000
# Off-topic gate for books: DOMAIN hits per 1000 words, calibrated 2026-07-07 on a 300-book OAPEN
# sample — clearly-unrelated books score < 8; real AEC books score 30-150.
BOOK_MIN_DENSITY = 8.0
SHORT_MIN_HITS = 10
MIN_ALPHA = 0.55
# CJK engineering papers are legitimately digit/symbol-dense (equations, tables, DOI headers):
# clean 2019 AIJ structural papers score alpha ~0.54 and were wrongly killed as garbage
# (07-23). A doc that is verifiably CJK (hundreds of true-CJK chars in the window) gets a
# lower alpha floor; symbol-soup garbage rarely lands in the CJK blocks, so this stays safe.
MIN_ALPHA_CJK = 0.45
CJK_ALPHA_MIN_CHARS = 300


def body(text: str) -> str:
    """Strip the `# title\\n\\nsource:...\\n---\\n\\n` header build_corpus puts on text/*.md."""
    return text.split("\n---\n\n", 1)[-1] if "\n---\n\n" in text else text


def _window(t: str) -> dict:
    words = re.findall(r"[A-Za-z]{2,}", t)
    return {
        "chars": len(t.strip()),
        "alpha": round(sum(c.isalpha() for c in t) / len(t), 6) if t else 0.0,  # CJK isalpha ✓
        "words": len(words),
        "cjk": len(CJK.findall(t)),  # CJK scripts don't space-separate; ~2 chars ≈ 1 word
        "en": len(EN.findall(t)),
        "domain": len(DOMAIN.findall(t)),
    }


def metrics(bod: str) -> dict:
    """Window stats for both gates + total length. `bod` is extracted text WITHOUT the header.
    Stored per-doc in the manifest (key `quality`) so verdicts never re-read the text."""
    return {"total": len(bod), "w20": _window(bod[:20_000]), "w100": _window(bod[:100_000])}


def is_booklike(sid: str, fmt: str) -> bool:
    """PDFs and arc- OCR texts are book-like; source code / repo docs (md/rst/txt) are not —
    code identifiers have book-unlike word statistics and the density rule falsely kills them."""
    return fmt == "pdf" or sid.startswith("arc-")


def verdict(m: dict, book: bool) -> str:
    # The corpus is ALL-LANGUAGE (2026-07-09): there is deliberately no non-English kill — the
    # DOMAIN vocabulary covers the major languages, so any language passes if it's on-topic.
    # CJK text has no space-separated words; cjk//2 approximates its word count.
    is_book = book and m["total"] > BOOK_MIN_CHARS
    w = m["w100"] if is_book else m["w20"]
    eff_words = w["words"] + w.get("cjk", 0) // 2
    if w["chars"] < 2000:
        return "thin"
    floor = MIN_ALPHA_CJK if w.get("cjk", 0) >= CJK_ALPHA_MIN_CHARS else MIN_ALPHA
    if w["alpha"] < floor:
        return "garbage"
    if eff_words < 200:
        return "thin"
    density = 1000 * w["domain"] / eff_words
    if (density < BOOK_MIN_DENSITY) if is_book else (w["domain"] < SHORT_MIN_HITS):
        return "off-topic"
    return "ok"


def assess(text: str, sid: str = "", fmt: str = "pdf") -> str:
    """One-shot verdict straight from raw text (header ok) — for spot checks and tests."""
    return verdict(metrics(body(text)), is_booklike(sid, fmt))
