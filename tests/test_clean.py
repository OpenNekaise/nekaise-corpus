"""Golden tests for the cleaning stage (scripts/clean_corpus.py).

Each seed is a real line class the audit found in text/. The KEEP cases matter more than the
DROP cases: this stage runs over 104k docs, and a rule that eats real content is far more
expensive than one that leaves junk behind. If you widen a rule, these tell you what you broke.
"""
import clean_corpus as cc


def dropped(rule: str, lines: list[str]) -> set[str]:
    """Run one rule over `lines`, return the set of line texts it dropped."""
    return {lines[i] for i in cc.RULES[rule](lines)}


# ---------------------------------------------------------------------------- toc_leaders
def test_toc_leader_dropped():
    lines = [
        "1 Kurzfassung ................................ ...................... 9 ",
        "3.2.1 Planerische Integration, Umsetzung ............................ 13 ",
        " . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . . ix",
    ]
    assert dropped("toc_leaders", lines[:2]) == set(lines[:2])


def test_prose_ellipsis_survives_toc_rule():
    """A three-dot ellipsis in real prose must never read as a contents entry."""
    lines = [
        "The damper opens, the fan ramps up ... and the economizer cycle engages at 12 degrees.",
        "As the code puts it: 'the designer shall verify...' before the permit is issued.",
    ]
    assert dropped("toc_leaders", lines) == set()


# ---------------------------------------------------------------------------- page_markers
def test_page_markers_dropped():
    lines = ["7", "  iv  ", "Page 12", "— 186 —", "- 12 -"]
    assert dropped("page_markers", lines) == set(lines)


def test_real_short_line_survives_page_marker_rule():
    lines = ["Table 3 shows the load", "1845. ", "Notes", "500 kPa"]
    kept = set(lines) - dropped("page_markers", lines)
    assert "Table 3 shows the load" in kept
    assert "500 kPa" in kept


# ---------------------------------------------------------------------------- patent_id_soup
def test_patent_id_soup_dropped():
    lines = ["US20150157190A1", "US 20150157190 A1", "US14/564,744", "CN208382619U",
             "CN201820708800.2U", "CN 208382619 U"]
    assert dropped("patent_id_soup", lines) == set(lines)


def test_patent_claim_text_survives():
    """The claims are the valuable part of a patent — the rule must only eat bare identifiers."""
    lines = [
        "1. A heat exchanger assembly for a building ventilation system, comprising:",
        "As disclosed in US20150157190A1, the damper is actuated by a stepper motor.",
        "FIG. 3 is a section through the duct at 45 degrees.",
    ]
    assert dropped("patent_id_soup", lines) == set()


# ---------------------------------------------------------------------------- ocr_debris
def test_ocr_debris_dropped():
    lines = ["‘ ; . ", "} . ", ", ‘ ” "]
    assert dropped("ocr_debris", lines) == set(lines)


def test_bare_number_is_not_ocr_debris():
    """'1845' on its own line is a date in a 1920s handbook, not scanner noise."""
    lines = ["1845", "212.73", "50"]
    assert dropped("ocr_debris", lines) == set()


# ---------------------------------------------------------------------------- CJK safety
CJK_PROSE = [
    "測定は 2019 年 3 月 14（暖房期）と 2021 年 10 月 22（冷房期）に行った。",
    "本研究では，幅 3.0m× 高さ 2.4m の試験体を用いて熱負荷を測定した。",
    "室温は 22.5 ℃，相対湿度は 45 % に保持された。",
]


def test_cjk_prose_survives_every_rule():
    """The trap this stage was designed around: real Japanese prose interleaved with figures has
    a low letters-per-character ratio, so any statistical rule reads it as number soup. Every
    rule here is structural, so all of it must survive."""
    for rule in cc.RULES:
        assert dropped(rule, CJK_PROSE) == set(), f"rule {rule} ate CJK prose"


def test_cjk_page_marker_still_dropped():
    """— 186 — is a page number in a J-STAGE paper and should still go."""
    assert dropped("page_markers", ["— 186 —"]) == {"— 186 —"}


# ---------------------------------------------------------------------------- numeric tables
NUMERIC_TABLE = [
    "Asphalt workers 2.81 (1.11-7.13) ",
    "Bricklayers 2.14 (1.08-4.25) ",
    "Floor layers 4.72 (1.80-12.33) ",
    "Concrete C25/30 25 30 2400 31 ",
    "Thermal conductivity 0.040 W/mK at 10 degrees ",
]


def test_numeric_tables_survive_every_rule():
    """Load tables, material properties and epidemiological results are real AEC knowledge. The
    user-reported complaint was 'meaningless numbers' — these are the meaningful ones."""
    for rule in cc.RULES:
        assert dropped(rule, NUMERIC_TABLE) == set(), f"rule {rule} ate a data table"


# ---------------------------------------------------------------------------- repeated_boilerplate
def test_running_header_dropped_at_four_occurrences():
    header = "Energy Efficiency in Buildings — Final Report 2019"
    lines = ["some real prose about the boiler and its controls"] + [header] * 4
    assert dropped("repeated_boilerplate", lines) == {header}


def test_repeated_three_times_survives():
    """Threshold is 4: a sentence that genuinely recurs three times is not a running header."""
    header = "Energy Efficiency in Buildings — Final Report 2019"
    assert dropped("repeated_boilerplate", [header] * 3) == set()


def test_short_repeat_survives():
    """'Notes' / 'Table 1' repeat legitimately and sit under the 12-char floor."""
    assert dropped("repeated_boilerplate", ["Notes"] * 8) == set()


# ---------------------------------------------------------------------------- code_annotations
def test_modelica_annotation_dropped():
    lines = ["          extent={{-50,48},{50,-42}},",
             "          fillColor={255,255,255},",
             "            100,160}})),",
             "<td>0.33</td>"]
    assert dropped("code_annotations", lines) == set(lines)


def test_modelica_equation_survives():
    """The physics is why we pull .mo source at all — only the diagram geometry is noise."""
    lines = [
        "  Q_flow = m_flow * cp * (T_in - T_out);",
        "  parameter Modelica.Units.SI.Volume V = 10 \"Room volume\";",
        "equation",
        "  der(T) = (Q_flow - UA * (T - T_amb)) / (V * rho * cp);",
    ]
    assert dropped("code_annotations", lines) == set()


# ---------------------------------------------------------------------------- plumbing
def test_header_is_always_preserved():
    doc = ("# 2010 ADA Standards\n\nsource: https://example.gov/a.pdf\nlicense: public-domain\n"
           "topic: architecture\n\n---\n\nreal body text here\n7\n")
    header, body = cc.split_header(doc)
    assert "license: public-domain" in header
    cleaned, _ = cc.clean_body(body, ["page_markers"])
    assert "real body text here" in cleaned
    assert "\n7" not in cleaned


def test_pass_through_is_byte_identical():
    body = "line one\n7\n\n\n\nline two\n"
    cleaned, attr = cc.clean_body(body, [])
    assert cleaned == body
    assert sum(attr.values()) == 0


def test_attribution_never_double_counts():
    """A line both rules claim is charged once, so per-rule numbers sum to the real total."""
    lines = "\n".join(["7"] * 5 + ["  "] * 2)
    cleaned, attr = cc.clean_body(lines, ["page_markers", "orphan_chars"])
    assert attr["page_markers"] > 0
    assert attr["orphan_chars"] == 0  # page_markers claimed them first
    assert not cleaned.strip()


def test_blank_runs_collapse_after_removal():
    body = "a\n\n\n\n\nb"
    assert cc.collapse_blanks(body.split("\n")) == ["a", "", "b"]


def test_parse_rules():
    assert cc.parse_rules("none") == []
    assert cc.parse_rules("") == []
    assert cc.parse_rules("all") == list(cc.RULES)
    assert cc.parse_rules("page_markers,toc_leaders") == ["toc_leaders", "page_markers"]


def test_parse_rules_rejects_unknown():
    import pytest
    with pytest.raises(SystemExit):
        cc.parse_rules("no_such_rule")


def test_every_rule_is_documented():
    assert set(cc.RULE_DOC) == set(cc.RULES)


def test_in_progress_stamp_can_never_match_a_ruleset():
    """Interrupt safety: a killed run leaves cleaned files whose mtime beats their text/ source, so
    the incremental check would call them up-to-date. The in-progress stamp must therefore be
    unmatchable by any real ruleset, forcing the next run to rebuild."""
    candidates = ["none", "all", ",".join(cc.RULES)] + list(cc.RULES)
    for spec in candidates:
        rules = cc.parse_rules(spec)
        stamp = ",".join(rules) if rules else "none"
        assert stamp != f"IN-PROGRESS {stamp}"
        assert not stamp.startswith("IN-PROGRESS")


def test_clean_one_is_picklable_for_the_process_pool():
    """The worker must be importable module-level state, not a closure — a closure silently breaks
    only under ProcessPoolExecutor, i.e. only on the full 104k-doc run."""
    import pickle
    assert pickle.loads(pickle.dumps(cc._clean_one)) is cc._clean_one
