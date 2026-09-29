"""Golden tests for the corpus_v1 ruleset (scripts/v1_rules.py).

KEEP cases matter more than DROP cases: these rules run over every document of the training
view, and a rule that eats real content costs more than one that leaves junk behind. When a
night session widens a rule, these say what it broke.
"""
import v1_rules as R

HEADER = "# Refrigerator\n\nsource: https://patents.google.com/patent/CN111964330B/en\n\n---"

PATENT = """
CN111964330B - Refrigerator capable of being opened easily

Info

Publication number
CN111964330B
Critical
Abstract

Translated from
Chinese

本发明公开了一种易开防漏气的冰箱。

The invention discloses an easy-to-open refrigerator.

Description

Refrigerator capable of being opened easily

Technical Field

The invention relates to refrigerators.

Claims (
2
)

1. A refrigerator comprising a box body (5).

2. The refrigerator of claim 1, wherein the door (12) is hinged.

CN202010842933.0A
2020-08-20
Similar Documents
Some other patent title
Legal Events
Description
Refrigerator capable of being opened easily
""".split("\n")


def test_patent_keeps_prose_drops_furniture():
    out = "\n".join(R.clean_body(PATENT, HEADER))
    for keep in ("# Refrigerator capable of being opened easily", "## Abstract",
                 "本发明公开了一种易开防漏气的冰箱。", "easy-to-open refrigerator",
                 "## Description", "Technical Field", "The invention relates to refrigerators.",
                 "## Claims", "1. A refrigerator comprising a box body (5).", "wherein the door (12)"):
        assert keep in out, keep
    for drop in ("Publication number", "Critical", "Translated from", "CN202010842933.0A",
                 "2020-08-20", "Similar Documents", "Some other patent title", "Legal Events"):
        assert drop not in out, drop
    assert out.count("## Description") == 1  # the page's second Description copy is ignored


def test_non_template_patent_falls_back_to_generic():
    assert R.parse_patent(["just some text", "no sections"]) is None


def test_prepass():
    assert R.prepass("FISCHER &amp; KRECKE driver&#39;s") == "FISCHER & KRECKE driver's"
    assert R.prepass(" first item") == "- first item"
    assert R.prepass("张哲�") == "张哲"
    assert R.prepass("R&D and a&b") == "R&D and a&b"


def test_rule_lines_and_toc_leaders():
    lines = ["________________", "Introduction . . . . . . . . . 12", "Real text... continues.",
             "a = b - c"]
    assert R.drop_toc_leaders(R.drop_rule_lines(lines)) == ["Real text... continues.", "a = b - c"]


def test_glyph_columns_dropped_but_cjk_columns_kept():
    formula = ["Heat balance:", "=", "∑", "W", "T", "ρ", "β", "x", "Next sentence."]
    assert R.drop_glyph_columns(formula) == ["Heat balance:", "", "Next sentence."]
    vertical_cjk = ["縦", "書", "き", "の", "日", "本", "語"]
    assert R.drop_glyph_columns(vertical_cjk) == vertical_cjk


def test_running_header_dropped_table_values_kept():
    page = [f"Line {i} of real prose about building energy use in cold climates." for i in range(60)]
    doc = []
    for p in range(8):
        doc += ["Volume 7.1 Guide to Determining Climate Regions by County", *page, str(p + 1)]
    table = ["Kansas", "Allen", "Mixed-Humid"] * 5  # repeated values close together
    doc[100:100] = table
    out = R.drop_running_lines(doc)
    assert "Volume 7.1 Guide to Determining Climate Regions by County" not in out
    assert "7" not in out  # page numbers
    assert out.count("Mixed-Humid") == 5


def test_reflow_joins_wrapped_prose_only():
    lines = ["The refrigerator has become one of the indispensable house-",
             "hold appliances, and after it was closed for a long",
             "time the door opens with difficulty.",
             "Table 1  Values   A   B",
             "- a list item that is long enough to be a wrapped line",
             "- second item"]
    out = R.reflow(lines)
    assert out[0] == ("The refrigerator has become one of the indispensable household appliances, "
                      "and after it was closed for a long time the door opens with difficulty.")
    assert out[1:] == lines[3:]


def test_reflow_keeps_compound_hyphen_and_joins_cjk():
    assert R.reflow(["a building envelope that is designed to be energy-",
                     "efficient in all seasons."])[0].endswith("energy-efficient in all seasons.")
    cjk = ["国総研では、平成30年度から3カ年の計画で、事項立て課題を進めているところであり、",
           "その成果を報告する。"]
    assert R.reflow(cjk) == [cjk[0] + cjk[1]]


def test_damage_score_ranks_ocr_garbage():
    clean = "the solution is evaporated to fumes of sulfuric acid and diluted " * 30
    broken = "the solu t ion is e vaporated to fume s of s ulfuric aCld and dilut ed " * 30
    assert R.damage_score(clean) < 0.05 < R.damage_score(broken)
    assert R.damage_score("冰箱" * 500) is None
    estonian = "käesoleva direktiiviga edendatakse liidus hoonete energiatõhususe parandamist " * 40
    assert R.damage_score(estonian) is None  # not English: not judged by an English dictionary
