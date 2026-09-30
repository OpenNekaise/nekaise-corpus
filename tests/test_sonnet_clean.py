"""Tests for the model layer of corpus_v1 (scripts/sonnet_clean.py), with fake backends.

Whatever model runs the queue, these must hold: a repair can never introduce or guess a number,
never silently drop readable content, and a rejected or refused part degrades to its
rule-cleaned text — never to nothing. The number cases come from the 2026-09-29 pilot audit.
"""
from collections import Counter

import sonnet_clean as sc

PROSE = ("The sample is dissolved in aqua regia and the solution is evaporated to fumes of "
         "sulfuric acid, then diluted to volume with diluted sulfuric acid. ") * 6


def test_numbers_ocr_repairs_and_respacing_pass():
    assert sc.number_problem("Vol. 59, No.6, l957 page 4l5", "Vol. 59, No. 6, 1957 page 415") is None
    assert sc.number_problem("tested in 19 57 at 1 000 kPa", "tested in 1957 at 1000 kPa") is None
    assert sc.number_problem("area 12 m²", "area 12 $m^2$") is None  # NFKC: superscript is a 2
    # separating a glued OCR run is re-spacing, not invention
    assert sc.number_problem("446425646.2%", "4464 25646.2%") is None
    assert sc.number_problem("range 0 2 percent", "range 0.2 percent") is None  # '0 2' had a gap


def test_numbers_guesses_rejected():
    assert sc.number_problem("tested in l957", "tested in 1958")
    assert sc.number_problem("cost sn.87i per ton, 12 tons", "cost $2.87 per ton, 12 tons")
    assert "decimal" in sc.number_problem("output was 443 units", "output was 44.3 units")
    assert sc.number_problem("CE~20fp", "CE-204")


def test_check_rejects_dropping_or_gutting_readable_content():
    table = "\n".join(f"Kansas | County {i} | Mixed-Humid | zone {i % 7}" for i in range(80))
    assert "dropped" in sc.check(table, sc.DROP)
    assert "half" in sc.check(PROSE, PROSE[:200])
    assert sc.check("■■ rrrR.nafti««fc ¦¦ ~~", sc.DROP) is None  # real garbage may go
    assert sc.check(PROSE, PROSE.replace("aqua regia", "aqua  regia")) is None


class FakeBackend:
    def __init__(self, reply):
        self.reply = reply

    def __call__(self, prompt):
        src = prompt.split("<text>\n", 1)[1].split("\n</text>", 1)[0]
        return self.reply(src), Counter({"in": 1, "out": 1})


def test_revise_part_accepts_clean_repair():
    r = sc.revise_part(FakeBackend(lambda s: s.replace("Tung sten", "Tungsten")),
                       "t", 0, 1, "Tung sten 12 percent " + PROSE)
    assert r["text"].startswith("Tungsten 12 percent") and r["fallback"] is None


def test_revise_part_falls_back_only_where_rejected():
    good = "Clean paragraph with 12 units of sulfuric acid in the steel sample solution."
    bad = "Paragraph with 34 units of phosphoric acid in the titanium alloy solution."
    src = "\n".join([good] * 40 + [""] + [bad] * 40)
    r = sc.revise_part(FakeBackend(lambda s: s.replace("34", "43")), "t", 0, 1, src)
    assert good in r["text"] and bad in r["text"]
    assert r["fallback"] and 0 < r["fallback_chars"] < len(src)


def test_unfence():
    assert sc.unfence("```markdown\nhello\n```") == "hello"
    assert sc.unfence("plain") == "plain"


def test_quota_stops_the_file_without_marking_it_failed(tmp_path, monkeypatch):
    """A usage limit is not a property of the document: the file must stay queued."""
    monkeypatch.setattr(sc.corpus_v1, "TEXT", tmp_path)
    (tmp_path / "d.md").write_text("# T\n\n---\n" + PROSE)

    def backend(prompt):
        raise sc.QuotaHit("You've hit your session limit · resets 6:10am")

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as pool:
        rec = sc.revise("d.md", backend, pool)
    assert rec["status"] == "quota"
