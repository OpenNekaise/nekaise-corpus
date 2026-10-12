"""find_diva --family aec (backend find_diva_aec), the AEC gate (scripts/diva_aec.py) and the
watch cursor both DiVA families end in. Titles are live DiVA records sampled 2026-10-12."""
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

import bes_relevance
import diva_aec
import find_diva
import lint_registry
import registry
from test_find_diva import _error, _mods, _page, _setup, _with


@pytest.fixture(autouse=True)
def _no_pace_file(monkeypatch):
    monkeypatch.setattr(find_diva, "PACE_FILE", None)


def _hsv(code, name):
    return (f'<subject lang="eng" authority="hsv" xlink:href="{code}"><topic>Engineering and '
            f'Technology</topic><topic>{name}</topic></subject>'
            f'<subject lang="swe" authority="hsv" xlink:href="{code}"><topic>Teknik</topic>'
            "</subject>")


# --- the gate -------------------------------------------------------------------------------

@pytest.mark.parametrize("title,topic", [
    ("Klimatdeklaration för byggnader.", "construction"),
    ("Teoretisk betraktelse av karbonatisering av betongrör", "materials"),
    ("Fatigue assessment of steel bridges: Using the λ coefficient method", "structures_civil"),
    ("Lågtempererad fjärrvärme i Umeå", "building_energy"),
    ("Bergvärme och solenergi i flerbostadshus: En förstudie över Kv. Uppfarten",
     "equipment_systems"),
    ("Dimensionering av erosionsskydd för mindre dagvattendammar", "infrastructure"),
    ("En stad i världsklass, men inte för alla: En studie om stadsplaneringsprojekt i Kibera",
     "urban"),
    ("Interaction effects during internal explosions in reinforced concrete frame structures",
     "structures_civil"),
    ("Återbruk inom byggindustrin - en dold resurs?", "construction"),
    ("A Statistical Approach to Real Estate Scenario Analysis", "construction"),
    ("Predicting Space Heating Demand in Single-Family Houses with Machine Learning",
     "building_energy"),
    ("Wastewater Treatment by Partial Nitritation / Anammox", "infrastructure"),
    ("Ammoniumåterkoppling på Himmerfjärdsverket – utvärdering genom försök vid reningsverk",
     "infrastructure"),
    ("Energisimulering av ett flerbostadshus i IDA ICE", "simulation_modeling"),
    ("Brannsikkerhet i høye trehus med bygningsdeler av massivtre", "architecture"),
])
def test_strong_title_passes_alone_with_a_vocabulary_topic(title, topic):
    assert diva_aec.relevant([title], [], [])
    assert diva_aec.topic([title], [], []) == topic in lint_registry.TOPICS


@pytest.mark.parametrize("title,keywords,codes", [
    # every subject of DiVA: nursing, pedagogy, law, software, mechanical design ...
    ("Sjuksköterskors upplevelser av att vårda patienter med mekanisk ventilation", [], []),
    ("Building trust in a microservice architecture", ["software architecture"], [("10201", "")]),
    ("Konstruktion av startkatapult för UAV", ["Maskinkonstruktion"], [("203", "")]),
    ("Samhällets syn på modersmål i förskolan", ["förskolan"], [("50301", "Pedagogy")]),
    ("Reoxidation of Liquid Steel during Casting", ["continuous casting"], [("20506", "")]),
    ("Knock Sensor-based Estimation for Valve Events", ["Internal combustion engine"], []),
    # the Civil Engineering group code alone also tags finance, logistics and hydrology
    ("Bitcoin ETF: Framtidens investering eller regulatorisk utmaning?", ["Crypto"],
     [("201", "Civil Engineering")]),
    ("Review of general demand responsive transport in Sörmland county", ["kollektivtrafik"],
     [("20105", "Transport Systems and Logistics")]),
    ("Grundvattensituationen på sydöstra Runmarö", ["Groundwater"], [("20107", "Water Engineering")]),
    ("Image Database for Pose Hypotheses Generation", ["SLAM"],
     [("20199", "Other Civil Engineering")]),
    # "uppbyggnad" is a build-up of anything, not a building
    ("Uppbyggnad av en databas", [], []),
    ("Muskeluppbyggnad genom styrketräning", [], []),
    ("Den svenska välfärdsstatens uppbyggnad", [], []),
    ("Återuppbyggnad av förtroende efter en kris", [], []),
    # one author keyword without the group code is not enough
    ("Prospective Life Cycle Assessment of Wind Power Production in Sweden",
     ["Wind power", "low-carbon concrete"], [("20702", "Energy Systems")]),
])
def test_other_subjects_and_weak_signals_are_dropped(title, keywords, codes):
    assert not diva_aec.relevant([title], keywords, codes)


@pytest.mark.parametrize("title", [
    "Ombyggnad av kontor till bostäder", "Tillbyggnad av en skola", "Byggnadsvård i Visby",
    "Energianvändning i kontorsbyggnader", "Påbyggnad av flerbostadshus i trä",
    "Uppbyggnad av en databas för byggnaders energianvändning",
])
def test_physical_building_compounds_still_pass(title):
    assert diva_aec.relevant([title], [], [])


def test_classification_and_keywords_corroborate():
    # a built-environment research subject passes a neutral title
    assert diva_aec.relevant(["Återbruk av fönster – en klimatpositiv lösning?"], [],
                             [("20103", "Building Technologies")])
    assert diva_aec.relevant(["Aesthetic Principles for a City Wide Public Space Strategy"], [],
                             [("60405", "Architecture")])
    # one strong keyword + the Civil Engineering group code, or two strong keywords
    assert diva_aec.relevant(["En undersökning av prefabricerade utfackningsväggar"],
                             ["Byggteknik"], [("201", "Civil Engineering")])
    assert not diva_aec.relevant(["En undersökning av prefabricerade utfackningsväggar"],
                                 ["Byggteknik"], [("2", "Engineering and Technology")])
    assert diva_aec.relevant(["Fuktegenskaper för materialet lerputs"],
                             ["Moisture Buffering", "Indoor Air Quality", "byggmaterial"], [])


def test_off_domain_title_needs_a_second_signal():
    figurative = ("Från konkret till abstrakt i geometriundervisningen: En studie om konkret "
                  "material som bro mellan konkret och abstrakt")
    assert not diva_aec.relevant([figurative], ["didaktik"], [("50302", "Didactics")])
    car = "Spiral ljuddämpare för kompressorer i HVAC-system i bilar"
    assert not diva_aec.relevant([car], ["ljuddämpare"], [("2", "")])
    site = "Feasible Charging for Heavy-Duty Electric Vehicles in Stockholm County's Construction Industry"
    assert not diva_aec.relevant([site], [], [("20105", "Transport Systems and Logistics")])
    assert diva_aec.relevant([site], ["construction site logistics"], [])
    # a classification never overrides an off-domain title
    assert not diva_aec.relevant(["En app för patienter"], [], [("20103", "Building Technologies")])
    assert not diva_aec.relevant(["Tumour growth near concrete structures"], [], [])


def test_swedish_genom_is_not_the_genome_veto():
    title = "Energieffektivisering av byggnader genom tilläggsisolering"
    assert not bes_relevance.HARD_KILL.search(title)
    assert bes_relevance.HARD_KILL.search("Genome sequencing of indoor mould")
    assert bes_relevance.HARD_KILL.search("Genomic analysis")
    assert diva_aec.relevant([title], [], [])
    assert not diva_aec.relevant(["Genomik och byggnader"], [], [])


# --- the finder -----------------------------------------------------------------------------

BRIDGE = _mods("Fatigue assessment of steel bridges", lang="eng", rid="diva2:11",
               url="https://kth.diva-portal.org/smash/get/diva2:11/FULLTEXT01.pdf")
PLAN = _with(_mods("Medborgardialog vid utbyggnad", rid="diva2:12", genre="studentThesis",
                   url="https://uu.diva-portal.org/smash/get/diva2:12/FULLTEXT01.pdf"),
             _hsv("20101", "Architectural Engineering"))
NURSING = _mods("Sjuksköterskors upplevelser av mekanisk ventilation", rid="diva2:13",
                url="https://www.diva-portal.org/smash/get/diva2:13/FULLTEXT01.pdf")
FINANCE = _with(_mods("Hantering av ränterisk med derivatinstrument", rid="diva2:14",
                      url="https://www.diva-portal.org/smash/get/diva2:14/FULLTEXT01.pdf"),
                _hsv("201", "Civil Engineering"))
LOCKED = _mods("Betongbroar i Norrland", note="restricted", rid="diva2:15",
               url="https://www.diva-portal.org/smash/get/diva2:15/FULLTEXT01.pdf")
SIM_ONLY_NAME = _with(_mods("Studie av en idrottshall", rid="diva2:16",
                            url="https://www.diva-portal.org/smash/get/diva2:16/FULLTEXT01.pdf"),
                      _hsv("20103", "Building Technologies"))


def test_aec_candidates_topics_and_the_untouched_sim_family():
    mods, _ = find_diva.parse_page(_page([BRIDGE, PLAN, NURSING, FINANCE, LOCKED]))
    bridge, plan, nursing, finance, locked = (find_diva.candidate(m, "2026-10-12", "aec")
                                              for m in mods)
    assert nursing is None and finance is None and locked is None
    assert bridge["topic"] == "structures_civil" and plan["topic"] == "architecture"
    assert bridge["url"] == "https://www.diva-portal.org/smash/get/diva2:11/FULLTEXT01.pdf"
    for entry in (bridge, plan):
        assert entry["source"] == "diva" and entry["license"] == "unverified"
        assert registry.shard_filename(entry["id"]) == "diva.yaml"
        assert not lint_registry.entry_errors(entry, "diva.yaml")
    # the simulation family still wants a building AND a simulation concept
    assert [find_diva.candidate(m, "2026-10-12") for m in mods] == [None] * 5


def test_classification_names_are_not_author_keywords():
    mods, _ = find_diva.parse_page(_page([PLAN, SIM_ONLY_NAME]))
    keywords, codes = find_diva.subjects_of(mods[0])
    assert keywords == [] and codes == [("20101", "Architectural Engineering"), ("20101", "")]
    assert find_diva.candidate(mods[1], "2026-10-12", "aec")["persistent_id"] == "diva2:16"


def test_aec_history_walks_student_theses_of_every_subject(monkeypatch, tmp_path):
    pages = [_page([BRIDGE, NURSING], "tok:2"), _page([PLAN])]
    calls, appended, files = _setup(monkeypatch, tmp_path, pages)
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--family", "aec", "--cursor",
                                      "0:2024:START", "--pages", "2", "--append"])
    find_diva.main()
    made = [c for c in calls if c != "sleep"]
    assert made[0]["set"] == "studentThesis" and made[0]["from"] == "2024-01-01T00:00:00Z"
    assert [e["persistent_id"] for e in appended] == ["diva2:11", "diva2:12"]
    assert files["next"].read_text() == "0:2023:START\n"


# --- the watch cursor -----------------------------------------------------------------------

TODAY = datetime.now(timezone.utc).date()


def test_initial_history_cursor_records_the_day_the_walk_began(monkeypatch, tmp_path):
    calls, _, files = _setup(monkeypatch, tmp_path, [_page([BRIDGE], "tok:2")])
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--family", "aec", "--cursor",
                                      f"0:{TODAY.year}:START", "--pages", "1"])
    find_diva.main()
    assert files["next"].read_text() == f"0@{TODAY}:{TODAY.year}:tok:2\n"


def test_walk_start_date_is_carried_through_years_and_sets(monkeypatch, tmp_path):
    pages = [_page([BRIDGE], "tok:2"), _page([PLAN]), _error("noRecordsMatch")]
    _, _, files = _setup(monkeypatch, tmp_path, pages)
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--family", "aec", "--cursor",
                                      f"0@2026-01-05:{find_diva.FLOOR_YEAR + 1}:START",
                                      "--pages", "3"])
    find_diva.main()
    assert files["next"].read_text() == f"1@2026-01-05:{TODAY.year}:START\n"
    assert find_diva.parse_cursor("2@2026-01-05:2010:tok:9") == (2, 2010, "tok:9")


def test_first_watch_starts_at_the_walk_start_however_long_the_walk_took(monkeypatch, tmp_path):
    """A walk that began 200 days ago (downtime, HOLDs): records stamped at any time since then
    are in the first watch window, not only the last weeks."""
    started = TODAY - timedelta(days=200)
    last = len(find_diva.AEC_SETS) - 1
    _, _, files = _setup(monkeypatch, tmp_path, [_error("noRecordsMatch")])
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--family", "aec", "--cursor",
                                      f"{last}@{started}:{find_diva.FLOOR_YEAR}:START",
                                      "--pages", "1"])
    find_diva.main()
    assert files["next"].read_text() == f"watch:{started}\n" and not files["exhausted"].exists()
    # the next run harvests from one day before the walk began
    calls, _, files = _setup(monkeypatch, tmp_path, [_error("noRecordsMatch")])
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--family", "aec", "--cursor",
                                      f"watch:{started}", "--pages", "1"])
    find_diva.main()
    assert calls[0]["from"] == f"{started - timedelta(days=1)}T00:00:00Z"
    assert calls[0]["until"] == f"{TODAY}T23:59:59Z"


@pytest.mark.parametrize("family,cursor", [
    ("aec", "watch:2026-09-01:2026-10-01:99:START"),
    ("aec", f"watch:2026-09-01:2026-10-01:{len(find_diva.AEC_SETS)}:START"),
    ("sim", f"{len(find_diva.SETS)}:2024:START"),
    ("aec", "9@2026-10-12:2024:START"),
    ("aec", "0@2026-13-40:2024:START"),
    ("aec", "0@:2024:START"),
    ("aec", "0@20261012:2024:START"),
])
def test_out_of_range_set_or_bad_start_date_is_rejected_without_a_request(
        monkeypatch, tmp_path, family, cursor):
    calls, appended, files = _setup(monkeypatch, tmp_path, [])
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--family", family, "--cursor", cursor,
                                      "--append"])
    with pytest.raises(SystemExit) as exc:
        find_diva.main()
    assert exc.value.code == 2 and calls == [] and appended == []
    assert not files["next"].exists() and not files["exhausted"].exists()


def test_idle_watch_makes_no_request_until_due(monkeypatch, tmp_path):
    cursor = f"watch:{TODAY - timedelta(days=find_diva.WATCH_DAYS - 1)}"
    calls, appended, files = _setup(monkeypatch, tmp_path, [])
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--family", "aec", "--cursor", cursor,
                                      "--append"])
    find_diva.main()
    assert calls == [] and appended == []
    assert files["next"].read_text() == cursor + "\n" and not files["exhausted"].exists()


def test_due_watch_harvests_every_set_by_datestamp_and_goes_idle(monkeypatch, tmp_path):
    since = TODAY - timedelta(days=find_diva.WATCH_DAYS)
    pages = [_page([BRIDGE]), _error("noRecordsMatch"), _page([PLAN]), _error("noRecordsMatch")]
    calls, appended, files = _setup(monkeypatch, tmp_path, pages)
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--family", "aec", "--cursor",
                                      f"watch:{since}", "--pages", "9", "--append"])
    find_diva.main()
    made = [c for c in calls if c != "sleep"]
    assert [c["set"] for c in made] == list(find_diva.AEC_SETS)
    for call in made:  # one day of overlap, a fixed upper bound
        assert call["from"] == f"{since - timedelta(days=1)}T00:00:00Z"
        assert call["until"] == f"{TODAY}T23:59:59Z"
    assert [e["persistent_id"] for e in appended] == ["diva2:11", "diva2:12"]
    assert files["next"].read_text() == f"watch:{TODAY}\n" and not files["exhausted"].exists()


def test_watch_in_progress_resumes_its_fixed_window(monkeypatch, tmp_path):
    since, until = date(2026, 9, 1), date(2026, 9, 9)   # an old window continues, even if due
    pages = [_page([BRIDGE], "tok:3")]
    calls, _, files = _setup(monkeypatch, tmp_path, pages)
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--cursor",
                                      f"watch:{since}:{until}:2:tok:2", "--pages", "1"])
    find_diva.main()
    assert calls == [{"verb": "ListRecords", "resumptionToken": "tok:2"}]
    assert files["next"].read_text() == f"watch:{since}:{until}:2:tok:3\n"


def test_watch_budget_stops_between_sets(monkeypatch, tmp_path):
    since = TODAY - timedelta(days=30)
    calls, _, files = _setup(monkeypatch, tmp_path, [_error("noRecordsMatch")])
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--cursor", f"watch:{since}",
                                      "--pages", "1"])
    find_diva.main()
    assert calls[0]["set"] == "Technology"
    assert files["next"].read_text() == f"watch:{since}:{TODAY}:1:START\n"


def _window_token(offset, since, until, setname="studentThesis", size=2):
    return (f"1791327753133/diva/swepub_mods/{offset}/{size}/{setname}/"
            f"{since}T00:00:00Z/{until}T23:59:59Z/")


def test_full_final_watch_page_closes_only_for_the_requested_window():
    from test_find_diva import _counted_page
    scope = ("studentThesis", "2026-10-04T00:00:00Z", "2026-10-12T23:59:59Z")
    body = _counted_page([BRIDGE, PLAN], _window_token(2, "2026-10-04", "2026-10-12"), 2, 2)
    assert find_diva.parse_page(body, scope=scope)[1] is None
    # same counts, but the token names another window or another set: completion is unproven
    for token in (_window_token(2, "2026-10-03", "2026-10-12"),
                  _window_token(2, "2026-10-04", "2026-10-12", setname="report")):
        body = _counted_page([BRIDGE, PLAN], token, 2, 2)
        assert find_diva.parse_page(body, scope=scope)[1] == token
    # a window that ends before it starts is not a DiVA token
    assert find_diva._position(_window_token(2, "2026-10-12", "2026-10-04")) is None


def test_watch_rejected_token_restarts_the_set_window_once(monkeypatch, tmp_path):
    since, until = date(2026, 9, 1), date(2026, 9, 9)
    pages = [_error("badResumptionToken"), _page([BRIDGE], "fresh")]
    calls, _, files = _setup(monkeypatch, tmp_path, pages)
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--family", "aec", "--cursor",
                                      f"watch:{since}:{until}:1:stale", "--pages", "2"])
    find_diva.main()
    made = [c for c in calls if c != "sleep"]
    assert made[1]["set"] == "doctoralThesis" and made[1]["from"] == "2026-08-31T00:00:00Z"
    assert files["next"].read_text() == f"watch:{since}:{until}:1:fresh\n"


def test_watch_unexpected_answer_holds(monkeypatch, tmp_path):
    _, appended, files = _setup(monkeypatch, tmp_path, [b"<html>maintenance</html>"])
    monkeypatch.setattr(sys, "argv", ["find_diva.py", "--cursor", "watch:2026-01-01",
                                      "--append"])
    find_diva.main()
    assert appended == [] and files["hold"].read_text().strip() and not files["next"].exists()


@pytest.mark.parametrize("cursor", ["watch", "watch:", "watch:2026-13-01", "watch:20261001",
                                    "watch:2026-10-01:2026-09-01:0:START",
                                    "watch:2026-10-01:2026-10-05:x:START",
                                    "watch:2026-10-01:2026-10-05:0:", "watch:2026-10-01:2026-10-05"])
def test_malformed_watch_cursor_is_rejected(cursor):
    with pytest.raises(ValueError):
        find_diva.parse_watch(cursor)


def test_families_share_one_request_pace(monkeypatch, tmp_path):
    """Two backends, one robots Crawl-delay: a request waits for the last answer of any process."""
    pace = tmp_path / "ws" / ".diva-oai.pace"
    monkeypatch.setattr(find_diva, "PACE_FILE", pace)
    clock = {"now": 1000.0}
    slept = []

    def sleep(seconds):
        slept.append(round(seconds, 3))
        clock["now"] += seconds

    monkeypatch.setattr(find_diva.time, "time", lambda: clock["now"])
    monkeypatch.setattr(find_diva.time, "sleep", sleep)
    monkeypatch.setattr(find_diva.requests, "get", lambda *a, **k: "answer")
    assert find_diva.paced_get({}) == "answer" and slept == []   # no earlier request
    clock["now"] += 4.0
    find_diva.paced_get({})
    assert slept == [find_diva.CRAWL_DELAY - 4.0]
    clock["now"] += 60.0
    find_diva.paced_get({})
    assert len(slept) == 1 and float(pace.read_text()) == clock["now"]


def _pace_world(monkeypatch, tmp_path, now):
    pace = tmp_path / ".diva-oai.pace"
    monkeypatch.setattr(find_diva, "PACE_FILE", pace)
    clock = {"now": now}
    slept, requests_at = [], []

    def sleep(seconds):
        slept.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(find_diva.time, "time", lambda: clock["now"])
    monkeypatch.setattr(find_diva.time, "sleep", sleep)
    monkeypatch.setattr(find_diva.requests, "get",
                        lambda *a, **k: requests_at.append(clock["now"]))
    return pace, clock, slept, requests_at


def test_pace_timestamp_rounded_ahead_of_the_clock_still_waits(monkeypatch, tmp_path):
    """The stored time is rounded to ms and can be ahead of the waiter's clock: the computed
    wait is then just over CRAWL_DELAY and must be clamped, never skipped."""
    pace, clock, slept, requests_at = _pace_world(monkeypatch, tmp_path, 1000.0008)
    pace.write_text("1000.001")
    find_diva.paced_get({})
    assert slept == [find_diva.CRAWL_DELAY]
    assert requests_at == [1000.0008 + find_diva.CRAWL_DELAY]


def test_two_waiting_callers_are_each_a_full_delay_apart(monkeypatch, tmp_path):
    pace, clock, slept, requests_at = _pace_world(monkeypatch, tmp_path, 2000.0004)
    find_diva.paced_get({})
    for _ in range(2):               # two callers that were waiting on the lock
        clock["now"] += 0.0001
        find_diva.paced_get({})
    assert len(slept) == 2 and len(requests_at) == 3
    gaps = [b - a for a, b in zip(requests_at, requests_at[1:])]
    assert all(gap >= find_diva.CRAWL_DELAY - 1e-5 for gap in gaps)


def test_pace_clock_set_back_waits_one_delay_at_most(monkeypatch, tmp_path):
    pace, clock, slept, _ = _pace_world(monkeypatch, tmp_path, 1000.0)
    pace.write_text("999999.000")    # a stored time far in the future
    find_diva.paced_get({})
    assert slept == [find_diva.CRAWL_DELAY]
