"""Golden tests for the next cleaning ruleset of corpus/ (scripts/v1_rules.py, dormant).

KEEP cases matter more than DROP cases: these rules will run over every document of the
training view, and a rule that eats real content costs more than one that leaves junk behind.
When a rule is widened, these say what it broke.
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


def test_glyph_columns_joined_not_deleted_and_cjk_columns_kept():
    formula = ["Heat balance:", "F", "=", "m", "a", "+", "b", "Next sentence."]
    assert R.join_glyph_columns(formula) == ["Heat balance:", "F = m a + b", "Next sentence."]
    vertical_cjk = ["縦", "書", "き", "の", "日", "本", "語"]
    assert R.join_glyph_columns(vertical_cjk) == vertical_cjk
    measurements = ["20", "21", "22", "23", "24", "25"]
    assert R.join_glyph_columns(measurements) == measurements


def test_page_furniture_needs_real_page_breaks():
    page = [f"Line {i} of real prose about building energy use in cold climates." for i in range(40)]
    header = "Guide to Determining Climate Regions by County"
    with_breaks, without = [], []
    for p in range(6):
        with_breaks += [("\f" if p else "") + header, *page, f"Page {p + 1}"]
        without += [header, *page, f"Page {p + 1}"]
    out = R.clean_body(with_breaks, "")
    assert header not in "\n".join(out)
    assert not any(x.strip().startswith("Page ") for x in out)
    out = R.clean_body(without, "")  # no page breaks: no page evidence, nothing removed
    assert "\n".join(out).count(header) == 6


def test_constant_numbers_at_page_edges_are_kept():
    page = [f"Line {i} of real prose about ventilation of office buildings." for i in range(30)]
    for edge in ("Volume 7.1 Guide to Climate Regions, August 2010", "Design airflow 1200 cfm"):
        doc = []
        for p in range(5):
            doc += [("\f" if p else "") + "Intro line of the page.", *page, edge]
        assert "\n".join(R.clean_body(doc, "")).count(edge) == 5, edge


def test_counting_measurements_with_units_at_page_edges_are_kept():
    page = [f"Line {i} of real prose about ventilation of office buildings." for i in range(30)]
    for unit in ("cfm", "lps", "kPa", "Btu"):
        doc = []
        for p in range(5):
            doc += [("\f" if p else "") + "Intro line of the page.", *page, f"Design airflow {20 + p} {unit}"]
        assert "\n".join(R.clean_body(doc, "")).count("Design airflow") == 5, unit


def test_measurement_at_a_page_edge_is_kept_when_edges_do_not_repeat():
    pages = []
    for p, v in enumerate((1200, 1300, 1400, 1500)):
        pages += [("\f" if p else "") + f"Section {p} discusses the design of pipe network {p}.",
                  "Real prose about the heating plant and its distribution network." * 2,
                  f"Design pressure {v} kPa"]
    out = "\n".join(R.clean_body(pages, ""))
    for v in ("1200", "1300", "1400", "1500"):
        assert v in out


def test_numbers_spread_by_blank_lines_are_not_page_numbers():
    lines = ["Measured temperatures:"]
    for v in (20, 21, 22, 23):
        lines += [str(v)] + [""] * 11
    lines += ["End."] + ["Filler prose line about building energy performance."] * 80
    out = R.clean_body(lines, "")
    for v in ("20", "21", "22", "23"):
        assert v in out


def test_labelled_values_a_few_sentences_apart_are_not_page_numbers():
    lines = ["Measured temperatures:"]
    for v in (20, 21, 22, 23):
        lines += [str(v)] + ["The chamber was held at this temperature for one hour."] * 11
    out = R.clean_body(lines, "")
    for v in ("20", "21", "22", "23"):
        assert v in out


def test_labelled_values_twenty_lines_apart_are_not_page_numbers():
    lines = ["Measured temperatures:"]
    for v in (20, 21, 22, 23):
        lines += [str(v)] + ["The chamber was held at this temperature for one hour."] * 20
    out = R.clean_body(lines, "")
    for v in ("20", "21", "22", "23"):
        assert v in out


def test_repeated_plain_text_rows_kept_without_page_evidence():
    row = "Concrete grade C25/30 strength 25 density 2400"
    doc = []
    for _ in range(9):
        doc += ["Real prose about the structural design of the building frame, checked in 2021."] * 30 + [row]
    assert "\n".join(R.clean_body(doc, "")).count(row) == 9


def test_measurements_at_page_edges_are_never_furniture():
    for values in ((20, 21, 22, 23), (1200, 1200, 1200, 1200)):
        pages = []
        for p, v in enumerate(values):
            pages += [("\f" if p else "") + f"Experiment {chr(65 + p)} on the heating plant.",
                      "Real prose about the heating plant and its distribution network.",
                      f"Design pressure {v} kPa"]
        out = "\n".join(R.clean_body(pages, ""))
        assert out.count("Design pressure") == 4, values
    edges = []
    for p in range(4):  # a constant bare value at every page end is data, not a page number
        edges += [("\f" if p else "") + f"Table {chr(65 + p)} of results.", "Prose line.", "1200"]
    assert "\n".join(R.clean_body(edges, "")).count("1200") == 4


def test_labelled_values_twenty_five_lines_apart_are_kept():
    lines = ["Measured temperatures:"]
    for v in range(20, 28):
        lines += [str(v)] + ["The chamber was held at this temperature for one hour."] * 25
    out = R.clean_body(lines, "")
    for v in range(20, 28):
        assert str(v) in out


def test_repeated_table_rows_and_short_legends_kept():
    doc = []
    for fig in range(9):
        doc += ["Real prose about the district and its public services, measured in 2021."] * 30 + \
            ["Public Health Centre", "| C25/30 concrete | strength 25 | density 2400 |"]
    out = "\n".join(R.clean_body(doc, ""))
    assert out.count("Public Health Centre") == 9
    assert out.count("| C25/30 concrete | strength 25 | density 2400 |") == 9


def test_repeated_figure_legends_kept():
    doc = []
    for fig in range(9):
        doc += [f"Figure {fig} shows the distribution of services in the district."] + \
            ["Real prose about adaptive reuse of heritage buildings in contested cities."] * 30 + \
            ["Nursery", "Parks", "Police Station", "Pharmacy", "Gas Station"]
    out = "\n".join(R.clean_body(doc, ""))
    assert out.count("Police Station") == 9 and out.count("Nursery") == 9


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


def test_patent_numeric_table_cells_kept():
    """Maintainer 2026-10-01: standalone numeric cells inside a patent are content."""
    body = ["CN1 - Concrete", "Description", "Material properties of the concrete:",
            "Grade", "C25/30", "Strength", "25", "30", "Density", "2400",
            "Claims (", "1", ")", "1. A concrete of grade C25/30.", "Legal Events"]
    out = "\n".join(R.clean_body(body, HEADER))
    for keep in ("C25/30", "\n25\n", "\n30\n", "2400", "1. A concrete of grade C25/30."):
        assert keep in out, keep
    claims = out[out.index("## Claims"):]
    assert claims.split("\n")[2] == "1. A concrete of grade C25/30."  # 'Claims (' '1' ')' dropped
    assert ")" not in claims.split("\n")[:3]


def test_numeric_measurement_column_survives_cleaning():
    """Maintainer 2026-09-30 blocker 1, kept as a regression test."""
    source = ["Measured air temperatures (C):", "20", "21", "22", "23", "24", "25",
              "End of measured temperatures."]
    output = R.clean_body(source, "")
    assert all(value in output for value in source[1:7]), output
