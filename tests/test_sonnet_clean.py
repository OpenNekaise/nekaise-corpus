"""Tests for the model layer of corpus_v1 (scripts/sonnet_clean.py), with fake backends.

Whatever model runs the queue, these must hold: a repair can never introduce or guess a number,
never silently drop readable content, and a rejected or refused part degrades to its
rule-cleaned text — never to nothing. The number cases come from the 2026-09-29 pilot audit.
"""
from collections import Counter

import sonnet_clean as sc

PROSE = ("The sample is dissolved in aqua regia and the solution is evaporated to fumes of "
         "sulfuric acid, then diluted to volume with diluted sulfuric acid. ") * 6


def test_numbers_allowed_transformations_pass():
    assert sc.number_problem("Vol. 59, No.6, l957 page 4l5", "Vol. 59, No. 6, 1957 page 415") is None
    assert sc.number_problem("grade S355 and B20", "grade S355 and B20") is None
    assert sc.number_problem("the 20 aud 25 values", "the 20 and 25 values") is None  # OCR word
    assert sc.number_problem("range 20–25 °C", "range 20-25 °C") is None
    table = "| grade | fck | density |\n|C25/30|25|2400|\n|C30/37|30|2400|"
    assert sc.check(table, table) is None and sc.check(PROSE + table, PROSE + table) is None


def test_numbers_changes_rejected():
    bad = [("tested in l957", "tested in 1958"),
           ("design pressure is 1200 kPa", "design pressure is 200 kPa"),
           ("between 20 and 25 C", "between 20 and C"),
           ("cost sn.87i per ton, 12 tons", "cost $2.87 per ton, 12 tons"),
           ("output was 443 units", "output was 44.3 units"),
           ("CE~20fp", "CE-204"),
           ("at -20 C", "at +20 C"), ("at -20 C", "at 20 C"), ("at - 20 °C", "at + 20 °C"),
           ("mass 12.00 kg", "mass 1200 kg"),
           ("dose 1e-6 Sv", "dose 1e6 Sv"),
           ("range 20–25 C", "range 2025 C"),
           ("pressure 1200 kPa", "pressure 1200 Pa"), ("pressure 1200 kPa", "pressure 1200 atm"),
           ("pressure 1200 kPa", "pressure 1200"),
           ("1200 kPa and 1200 kPa", "1200 kPa"), ("1200 kPa", "1200 kPa and 1200 kPa"),
           ("tested in 19 57", "tested in 1957"), ("at 1 000 kPa", "at 1000 kPa"),
           ("cells 1 100 200", "cells 1100200"),
           ("range 20-25 C", "range 20+25 C"), ("1200  kPa", "1200  Pa"),
           ("12 kg/m² density", "12 lb/ft² density"),
           ("-1 000 kPa", "+1000 Pa"), ("-1234567 kPa", "+1234 567 kPa"),
           ("a gap of .5 mm", "a gap of 5 mm"), ("a gap of ,5 mm", "a gap of 5 mm"),
           ("446425646.2%", "4464 25646.2%"),                     # splits are not allowed
           ("C25/30 25 2400\nC30/37 30 2400\nC35/45 35 2400", "")]
    for src, out in bad:
        assert sc.number_problem(src, out), (src, out)
    page = "\n".join([PROSE[:200]] * 10)
    headers = "\n".join(f"Design pressure {v} kPa\n{page}" for v in (1200, 1300, 1400))
    assert sc.number_problem(headers, "\n".join([page] * 3))  # repeated measurements stay


def test_check_rejects_dropping_or_gutting_readable_content():
    garbage = "the xqzt vbnm rtyu of wkpl and qxvz " * 40
    assert sc.check(garbage, sc.DROP)  # the model never drops
    words_table = "Material | Use\nConcrete | Foundation\nSteel | Reinforcement"
    assert sc.check(garbage + "\n" + words_table, "the " * 30)
    assert sc.check(PROSE, PROSE[:200])
    german = "Die Wärmedämmung der Außenwand verringert den Heizwärmebedarf deutlich."
    assert sc.check(PROSE + "\n" + german, PROSE)          # a Latin-language sentence deleted
    assert sc.check(PROSE + " Tung sten was added.", PROSE + " Tungsten was added.") is None
    garbled = "■■ rrrR ¦¦ ~~ chrom~um"
    assert sc.check(PROSE + "\n" + garbled, PROSE) is None  # debris with an OCR signature may go
    assert sc.check(PROSE + "\nxqzt vbnm", PROSE)  # no signature: protected, kept
    assert sc.check(PROSE + "\n■■ nafti", PROSE)  # word-shaped: kept (fail-closed)
    assert not sc.model_eligible("本发明公开了一种冰箱。" + PROSE)
    assert not sc.model_eligible("Теплоизоляция стены " + PROSE) and sc.model_eligible(PROSE)
    assert not sc.model_eligible(PROSE + " भवन ऊर्जा दक्षता") and not sc.model_eligible(PROSE + " Շենք")
    assert sc.model_eligible(PROSE + " with density ρ and Δt")  # Greek formula symbols are fine
    assert sc.check(PROSE + " The beam is not safe.", PROSE + " The beam is safe.")
    assert sc.check(PROSE + " The beam is safe.", PROSE + " The beam is not safe.")
    listing = PROSE + "\n- Concrete foundation\n- Steel reinforcement\n- Timber frame"
    assert sc.check(listing, listing.replace("\n- Steel reinforcement", ""))
    assert sc.check(PROSE + " aCld dJssolved", PROSE + " acid dissolved") is None  # OCR repair
    assert sc.check(PROSE + " The beam is not permitted.", PROSE + " The beam is permitted!")
    assert sc.check(PROSE + " The beam is permitted.", PROSE + " The beam is not permitted.")
    assert sc.check(PROSE + " Force = m a + b", PROSE + " Force = m a - b")
    assert sc.check(PROSE + " -  20 C", PROSE + " +  20 C")
    assert sc.check(PROSE + " ≤1200 kPa", PROSE + " ≥1200 kPa")
    assert sc.check(PROSE + " 12\nMPa", PROSE + " 12\nkPa")
    assert sc.check(PROSE + " 12 MPa", PROSE + " 12")
    assert sc.check(PROSE + " It can't burn.", PROSE + " It can burn.")
    assert sc.check(PROSE + " a non-combustible wall", PROSE + " a combustible wall")
    assert sc.check(PROSE + " F=m*a+b holds", PROSE + " F=m*a-b holds")
    assert sc.check(PROSE + " Force = m - a", PROSE + " Force = ma")
    assert sc.check(PROSE + " rrrR ■■", PROSE + " F=m*a")          # no inserting formulas
    assert sc.check(PROSE + " ■■ here", PROSE + " ■■ xqzt here")      # nor inserting debris
    assert sc.token_kind("EnergyPlus") == "protected" and sc.token_kind("IfcWall") == "protected"
    assert sc.check(PROSE + " tau = | V | / A", PROSE + " tau = V / A")
    assert sc.check(PROSE + " if a || b then", PROSE + " if a b then")
    assert sc.check(PROSE + " the IfcWall entity", PROSE + " the entity")      # never deleted
    assert sc.check(PROSE + " the IfcWall entity", PROSE + " the wall entity")  # nor replaced
    assert sc.check(PROSE + " Force = m a \u2212 b", PROSE + " Force = m a b")
    assert sc.check(PROSE + " where k_eff is the gain", PROSE + " where is the gain")
    assert sc.check(PROSE + " Force = m -\na", PROSE + " Force = ma")
    assert sc.check(PROSE + " the deter-\nmination", PROSE + " the determination") is None
    assert sc.check(PROSE + " if load == limit then stop", PROSE + " if load limit then stop")
    assert sc.check(PROSE + " compute a ** b", PROSE + " compute a b")
    assert sc.check(PROSE + " Die Kunststofffassade des Gebäudes ist brennbar.",
                    PROSE + " Die des Gebäudes ist brennbar.")


def test_repairs_that_must_pass():
    ok = [(PROSE + " The house-\nhold uses gas.", PROSE + " The household uses gas."),
          (PROSE + " Tung sten and descr ibed", PROSE + " Tungsten and described"),
          (PROSE + " chrom~um steel ■■", PROSE + " chromium steel"),
          (PROSE + " dJssolved in l957", PROSE + " dissolved in 1957"),
          (PROSE + "\nResults\nThe line\nwraps here.", PROSE + "\n## Results\nThe line wraps here."),
          (PROSE + "\nGrade Strength Density\nC25 25 2400\nC30 30 2400",
           PROSE + "\n| Grade | Strength | Density |\n|---|---|---|\n| C25 | 25 | 2400 |\n| C30 | 30 | 2400 |")]
    for src, out in ok:
        assert sc.check(src, out) is None, (src[-60:], out[-60:])


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


def test_revise_part_drop_keeps_source():
    src = "rrrR.nafti fc xqzt " + PROSE
    r = sc.revise_part(FakeBackend(lambda s: sc.DROP), "t", 0, 1, src)
    assert not r["dropped"] and r["text"] == src and r["fallback"]


def test_unfence():
    assert sc.unfence("```markdown\nhello\n```") == "hello"
    assert sc.unfence("plain") == "plain"


def test_quota_stops_the_file_without_marking_it_failed(tmp_path, monkeypatch):
    """A usage limit is not a property of the document: the file must stay queued."""
    monkeypatch.setattr(sc.corpus_v1, "TEXT", tmp_path)
    monkeypatch.setattr(sc.corpus_v1, "CORPUS", tmp_path)  # d.md is in the training view
    (tmp_path / "d.md").write_text("# T\n\n---\n" + PROSE)

    def backend(prompt):
        raise sc.QuotaHit("You've hit your session limit · resets 6:10am")

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(2) as pool:
        rec = sc.revise("d.md", backend, pool)
    assert rec["status"] == "quota"
