"""v1_rules.py — the deterministic ruleset that turns verbatim text/ into corpus_v1/.

corpus_v1/ is the training view for continued pretraining / mid-training of a small
built-environment LLM: EVERY document of corpus/, in a cleaned, readable form. This module is
the script half of that cleaning; scripts/sonnet_clean.py is the model half (broken text that no
rule can repair is rewritten into normal text by a model and overlaid by corpus_v1.py).

Contract for every rule here:
* input and output are the document BODY (the metadata header is handled by the builder);
* removal is structural (shape, repetition, a known page template) — never an alpha-fraction
  threshold, which reads CJK prose interleaved with figures as garbage (see clean_corpus.py);
* a rule never rewrites words, never reorders, never invents; the only text changes are
  entity decoding, glyph/control-character removal, hyphen repair and line re-flow;
* each rule is pinned by tests/test_v1_rules.py — KEEP cases matter more than DROP cases.

RULESET_VERSION is part of every corpus_v1 file's state: bump it whenever output can change,
and the builder rebuilds every file with the new rules.
"""
from __future__ import annotations

import html
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

RULESET_VERSION = "r2"  # r2: fail-closed: page furniture only at real page breaks; equations joined, not deleted

# ------------------------------------------------------------------------------------ pre-pass

_ENTITY = re.compile(r"&(?:#\d{1,7}|#x[0-9a-fA-F]{1,6}|[a-zA-Z]{2,8});")
_PUA = re.compile("[\ue000-\uf8ff]")
_CTRL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffd\u200b\ufeff]")


def prepass(line: str) -> str:
    """Mechanical fixes needing no judgement: entities, PDF bullet glyphs, control chars."""
    if "&" in line and _ENTITY.search(line):
        line = _ENTITY.sub(lambda m: html.unescape(m.group(0)), line)
    if _PUA.search(line):
        stripped = line.lstrip()
        if stripped and _PUA.match(stripped):  # a private-use bullet glyph at line start
            line = line[: len(line) - len(stripped)] + "- " + _PUA.sub("", stripped[1:]).lstrip()
        line = _PUA.sub("", line)
    return _CTRL.sub("", line).rstrip()


# ------------------------------------------------------------------------------------ patents
# Every patent in corpus/ is a Google Patents page with one template (500-doc sample: 100%
# carry Info/Links/Description, 98% Abstract, 95% Claims). The prose is Abstract, Description
# and Claims; everything else is page furniture. CN pages give the Chinese original and an
# English machine translation paragraph by paragraph — both are kept.

_SECTION = re.compile(r"^(Abstract|Description|Claims(?: \(\d*\)?)?)$")  # "Claims (" + "1" + ")"
_PATENT_END = re.compile(
    r"^(?:Info|Links|Images|Classifications|Landscapes|Cited By \(\d+\)|Patent Citations \(\d+\)"
    r"|Non-Patent Citations \(\d+\)|Similar Documents|Legal Events|Family Cites Families \(\d+\)"
    r"|Priority Applications \(\d+\)|Applications Claiming Priority \(\d+\)|Publications \(\d+\)"
    r"|Info: Patent citations \(\d+\).*|Cited By|Patent Citations|Family To Family Citations"
    r"|Worldwide applications|Application Number|Priority Date|Publication Date)$")
_PATENT_NO = re.compile(r"^(?:[A-Z]{2}\d{3,}[A-Z0-9.]*|[A-Z]{2}\d{2}/[\d,]+)$")  # CN111964330B, US06/234,977
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PAREN_FRAGMENT = re.compile(r"^(?:\(|\)|[a-z]{2}|\d{1,3}|AREA|Translated from|Chinese|Japanese"
                             r"|Korean|German|French|Other languages|Show more|Hide Dependent)$")


def is_patent(header: str) -> bool:
    return "patents.google.com/patent/" in header


def parse_patent(body: list[str]) -> list[str] | None:
    """Title, abstract, description and claims of a Google Patents page, or None if the page
    does not have the template (the caller then falls back to the generic rules)."""
    title = next((x.split(" - ", 1)[1].strip() for x in body[:10]
                  if " - " in x and _PATENT_NO.match(x.split(" - ", 1)[0].strip())), None)
    sections: dict[str, list[str]] = {}
    i, n = 0, len(body)
    while i < n:
        m = _SECTION.match(body[i].strip())
        name = m and m.group(1).split(" ")[0]
        if not name or name in sections:  # a later duplicate (e.g. a 2nd Description) is ignored
            i += 1
            continue
        j, text = i + 1, []
        while j < n:
            s = body[j].strip()
            if _SECTION.match(s) or _PATENT_END.match(s) or _PATENT_NO.match(s) or _ISO_DATE.match(s):
                break
            if not _PAREN_FRAGMENT.match(s):
                text.append(s)
            j += 1
        sections[name] = text
        i = j
    if "Description" not in sections and "Claims" not in sections:
        return None
    out: list[str] = []
    if title:
        out += [f"# {title}", ""]
    for name in ("Abstract", "Description", "Claims"):
        paras = [x for x in sections.get(name, []) if x]
        # The description often opens by repeating the title: drop that one line.
        if name == "Description" and paras and title and paras[0] == title:
            paras = paras[1:]
        if paras:
            out += [f"## {name}", ""]
            for p in paras:
                out += [p, ""]
    return out


# ------------------------------------------------------------------------------------ generic

_RULE_LINE = re.compile(r"^\s*([_=\-~*.·•─━═]\s?)\1{7,}\s*$")        # ________ ======= - - - -
_TOC_LEADER = re.compile(r"(?:\.\s?){5,}\s*[\divxlcIVXLC]+\s*$")      # Introduction ...... 12
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uf900-\ufaff]")
_TABLEISH = re.compile(r"\|.*\||\S\s{3,}\S.*\S\s{3,}\S|\t")
_LIST_START = re.compile(r"^\s*(?:[-*•▪◦·]\s|\(?\d{1,3}[.)]\s|\(?[a-zA-Z][.)]\s|[ivx]{1,4}[.)]\s)")
_END_PUNCT = re.compile(r"[.!?:;。！？：；…)\]\"'”’»」』]$")


def drop_rule_lines(lines: list[str]) -> list[str]:
    return [x for x in lines if not _RULE_LINE.match(x)]


def drop_toc_leaders(lines: list[str]) -> list[str]:
    return [x for x in lines if not _TOC_LEADER.search(x)]


def join_glyph_columns(lines: list[str], run: int = 6) -> list[str]:
    """A formula or figure broken into one glyph per line ('F', '=', 'm', 'a', '+', 'b'): runs of
    >= `run` consecutive non-blank lines of <= 2 characters are JOINED into one line
    ('F = m a + b'), never deleted — an equation is content. Lines holding a digit or CJK never
    count (a column of measurements, vertical CJK text)."""
    def glyph(s: str) -> bool:
        return 0 < len(s) <= 2 and not _CJK.search(s) and not any(c.isdigit() for c in s)

    out, i, n = [], 0, len(lines)
    while i < n:
        j = i
        while j < n and (glyph(lines[j].strip()) or (not lines[j].strip() and j > i)):
            j += 1
        cells = [x.strip() for x in lines[i:j] if x.strip()]
        if len(cells) >= run:
            out.append(" ".join(cells))
            i = j
        else:
            out.append(lines[i])
            i += 1
    return out


def page_furniture(body: list[str], min_pages: int = 3) -> set[int]:
    """Indexes of running headers, footers and page numbers — ONLY where the extraction kept real
    page breaks (form feeds). A line counts when it is the first or last non-blank line of its
    page and the same line (digits aside) stands at that page edge on at least half the pages;
    a bare number counts when page edges carry a bare number on at least half the pages.
    Without form feeds nothing is removed: a stray header is noise, a deleted measurement or
    repeated table row is loss, and without page breaks the two cannot be told apart."""
    starts = [0] + [i for i, x in enumerate(body) if "\f" in x]
    starts = sorted(set(starts))
    if len(starts) < min_pages:
        return set()
    edges: list[list[int]] = []  # per page: indexes of its first and last non-blank lines
    for k, a in enumerate(starts):
        b = starts[k + 1] if k + 1 < len(starts) else len(body)
        idx = [i for i in range(a, b) if body[i].replace("\f", "").strip()]
        edges.append(sorted({idx[0], idx[-1]}) if idx else [])
    pages = len(edges)
    text = lambda i: body[i].replace("\f", "").strip()
    key = lambda i: re.sub(r"\d+", "#", text(i))
    seen: dict[str, list[tuple[int, ...]]] = {}  # key -> the numbers it carried, page by page
    for e in edges:
        for i in e:
            seen.setdefault(key(i), []).append(tuple(int(d) for d in re.findall(r"\d+", text(i))))
    furniture = {k for k, nums in seen.items()
                 if len(nums) >= max(min_pages, pages / 2) and _page_like(nums)}
    return {i for e in edges for i in e if key(i) in furniture}


def _page_like(nums: list[tuple[int, ...]]) -> bool:
    """A running line's numbers stay constant ('Volume 7.1 ... 2010') or count pages: from one
    occurrence to the next at most one number changes, and it goes up by 1-2. Values that jump
    ('Design pressure 1200 / 1300 / 1400 kPa') are measurements, not furniture."""
    for a, b in zip(nums, nums[1:]):
        if len(a) != len(b):
            return False
        changed = [(x, y) for x, y in zip(a, b) if x != y]
        if len(changed) > 1 or any(not 1 <= y - x <= 2 for x, y in changed):
            return False
    return True


def reflow(lines: list[str]) -> list[str]:
    """Re-join paragraphs hard-wrapped at every line; repair hyphenated breaks. A line joins
    the previous one only if the previous line does not end a sentence, both lines are prose
    (not table rows, list items or headings) and the previous line is long enough to be a
    wrapped line. CJK joins without a space."""
    out: list[str] = []
    for x in lines:
        s = x.strip()
        prev = out[-1] if out else ""
        joinable = (
            s and prev.strip()
            and len(prev) >= 35
            and not _END_PUNCT.search(prev)
            and not prev.lstrip().startswith(("#", "|"))
            and not _TABLEISH.search(prev) and not _TABLEISH.search(x)
            and not _LIST_START.match(s)
            and not s.startswith(("#", "|"))
        )
        if joinable:
            if prev.endswith("­"):
                out[-1] = prev[:-1] + s
            elif prev.endswith("-") and len(prev) > 1 and prev[-2].isalpha() and s[0].islower():
                out[-1] = prev[:-1] + s if dehyphenate(prev, s) else prev + s
            elif _CJK.match(prev[-1]) or _CJK.match(s[0]):
                out[-1] = prev + s
            elif s[0].islower() or s[0].isdigit() or s[0] in "([{\"'“‘,":
                out[-1] = prev + " " + s
            else:
                out.append(x)
        else:
            out.append(x)
    return out


def dehyphenate(prev: str, nxt: str) -> bool:
    """'determi-' + 'nation' -> 'determination' when the joined word is known or the pieces
    are not; keep the hyphen for real compounds ('energy-' + 'efficient')."""
    a = re.findall(r"[A-Za-z]+$", prev[:-1])
    b = re.findall(r"^[A-Za-z]+", nxt)
    if not a or not b:
        return True
    words = english_words()
    joined = (a[0] + b[0]).lower()
    if joined in words:
        return True
    return not (a[0].lower() in words and b[0].lower() in words)


def collapse_blank(lines: list[str]) -> list[str]:
    out: list[str] = []
    for x in lines:
        if x.strip() or (out and out[-1].strip()):
            out.append(x if x.strip() else "")
    while out and not out[-1].strip():
        out.pop()
    return out


def paragraphs(lines: list[str]) -> list[str]:
    """Separate re-flowed paragraphs by one blank line so the text reads as markdown."""
    out: list[str] = []
    for x in lines:
        if out and x.strip() and out[-1].strip() and len(out[-1]) > 200:
            out.append("")
        out.append(x)
    return out


def clean_body(body: list[str], header: str) -> list[str]:
    """The whole deterministic ruleset, in order."""
    if is_patent(header):
        parsed = parse_patent([prepass(x) for x in body])
        if parsed is not None:
            return collapse_blank(parsed)
    furniture = page_furniture(body)  # needs the form feeds that prepass removes
    lines = [prepass(x) for i, x in enumerate(body) if i not in furniture]
    lines = drop_rule_lines(lines)
    lines = drop_toc_leaders(lines)
    lines = join_glyph_columns(lines)
    lines = reflow(lines)
    lines = paragraphs(lines)
    return collapse_blank(lines)


# ------------------------------------------------------------------------------------ scoring

@lru_cache(maxsize=1)
def english_words() -> frozenset[str]:
    for p in (Path("/usr/share/dict/american-english"), Path("/usr/share/dict/words")):
        if p.exists():
            return frozenset(w.strip().lower() for w in p.read_text(errors="ignore").split()
                             if w.isalpha())
    return frozenset()


_WORD = re.compile(r"[A-Za-z]{2,}")
_ENGLISH_FUNCTION = frozenset("the of and to in is for that with as on are by be this from or at "
                              "an it which was were not can has have its".split())


def damage_score(text: str, sample_chars: int = 200_000) -> float | None:
    """Share of English word tokens NOT in the dictionary (0 = clean, 0.3+ = badly broken
    OCR). None when the text has too few Latin words to judge (CJK, tables, code). Used to
    rank documents for model repair; never to delete anything."""
    words = english_words()
    toks = _WORD.findall(unicodedata.normalize("NFKC", text[:sample_chars]))
    if len(toks) < 150 or not words:
        return None
    # Capitalised words are often names/acronyms: judge lowercase tokens only.
    low = [t for t in toks if t.islower()]
    if len(low) < 100:
        return None
    # English only: an English dictionary reads Estonian, Swedish or Portuguese as garbage. OCR
    # damage spares short function words, so broken English still passes this.
    if sum(1 for t in low if t in _ENGLISH_FUNCTION) < 0.12 * len(low):
        return None
    bad = sum(1 for t in low if t not in words and t.rstrip("s") not in words)
    return round(bad / len(low), 4)
