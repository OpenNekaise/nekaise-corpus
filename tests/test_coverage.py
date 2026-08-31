import json
import sys

import pytest

import coverage as coverage_report
import coverage_matrix


def fixture_rows():
    return [
        {
            "id": "pat-us1", "title": "US structural patent", "status": "ok",
            "source": "google_patents", "license": "public-domain", "text_chars": 100,
        },
        {
            "id": "pat-cn1", "title": "China structural patent", "status": "ok",
            "source": "google_patents", "license": "open", "text_chars": 800,
        },
        {
            "id": "jst-1", "title": "日本 建築", "status": "ok",
            "source": "jstage_aij", "license": "open", "text_chars": 400,
        },
        {
            "id": "pat-us2", "title": "Failed patent", "status": "failed",
            "source": "google_patents", "license": "public-domain", "text_chars": 40,
        },
    ]


def fixture_restrictions():
    return {
        "cn": {"match": {"id_prefix": "pat-cn"}},
        "jstage": {"match": {"source": "jstage_aij"}},
    }


def patch_manifest(monkeypatch, module):
    monkeypatch.setattr(module.registry, "load_manifest_rows", fixture_rows)
    monkeypatch.setattr(module.registry, "load_eligibility", fixture_restrictions)


def test_genre_coverage_omits_training_excluded_rows(monkeypatch, capsys):
    patch_manifest(monkeypatch, coverage_report)
    monkeypatch.setattr(sys, "argv", ["coverage.py"])

    coverage_report.main()

    output = capsys.readouterr().out
    assert "1 training-eligible docs" in output
    assert "2 training-excluded provenance rows omitted" in output
    patent_line = next(line for line in output.splitlines() if line.strip().startswith("patents "))
    assert patent_line.split()[:2] == ["patents", "1"]
    assert "jstage_aij" not in output


def test_vendor_sources_are_manufacturer_literature(monkeypatch, capsys):
    row = {
        "id": "vnd-sika-1", "title": "Construction product manual", "status": "ok",
        "source": "vendor_sika", "license": "open", "text_chars": 100,
    }
    monkeypatch.setattr(coverage_report.registry, "load_manifest_rows", lambda: [row])
    monkeypatch.setattr(coverage_report.registry, "load_eligibility", lambda: {})
    monkeypatch.setattr(sys, "argv", ["coverage.py", "--sources"])

    coverage_report.main()

    output = capsys.readouterr().out
    manufacturer_line = next(
        line for line in output.splitlines()
        if line.strip().startswith("equipment_mfr_docs ")
    )
    assert manufacturer_line.split()[:2] == ["equipment_mfr_docs", "1"]
    assert "uncategorized sources" not in output
    source_line = next(line for line in output.splitlines() if "vendor_sika" in line)
    assert "-> equipment_mfr_docs" in source_line
    assert coverage_report.genre_of("vendor_example") == "equipment_mfr_docs"


@pytest.mark.parametrize(
    ("genre", "sources"),
    [
        ("research_papers", ("jstage_aij", "modelica_conf", "scielo_scl")),
        (
            "international_bodies",
            (
                "jrc", "worldbank", "worldbank_wds", "boverket", "nrcan_oee",
                "canada_publications", "nz_mbie", "seai", "aivc",
            ),
        ),
        (
            "software_sim_docs",
            (
                "buildingspy", "energyplus-api", "energyplus-docs", "eppy",
                "openstudio-docs", "soep", "openmodelica-docs", "modelica-spec",
            ),
        ),
        ("codes_standards", ("doe_energycodes", "wbdg_ufc", "access_board")),
        ("us_gov_lab_reports", ("cec",)),
    ],
)
def test_known_source_genres(genre, sources):
    assert {source: coverage_report.genre_of(source) for source in sources} == {
        source: genre for source in sources
    }


@pytest.mark.parametrize(
    ("source", "region"),
    [
        ("openaire_deliverable", "EU"), ("sdz_at", "EU"), ("hdz_at", "EU"),
        ("bri_jp", "JP"), ("nilim_jp", "JP"), ("worldbank_wds", "Global"),
        ("modelica_conf", "Global"), ("scielo_scl", "LatAm"),
        ("boverket", "Nordic"), ("nrcan_oee", "Canada"),
        ("canada_publications", "Canada"), ("nz_mbie", "NZ"),
    ],
)
def test_live_source_regions(source, region):
    row = {"id": "doc-1", "source": source}
    assert coverage_matrix.region_of(row, "") == [region]


def test_stale_region_source_names_are_removed():
    stale = {"nrel", "openaire", "sdz_hdz", "bri_japan", "nilim_japan"}
    assert stale.isdisjoint(coverage_matrix.REGION_SOURCE)


def test_language_of_prefers_declared_language_for_bilingual_document():
    text = (
        "Influência do ligante na retração por secagem em fibrocimento\n"
        "The study evaluates the material and the results of the tests for the building "
        "with the methods that are described in this English abstract. " * 8
    )
    assert coverage_matrix.detect_lang(text) == "en"
    assert coverage_matrix.language_of({"language": "pt"}, text) == "pt"


def test_language_of_keeps_heuristic_fallback_for_english_document():
    text = (
        "Building ventilation study\n"
        "The study evaluates the system and the results for the building with the methods "
        "that are described in this technical report."
    )
    assert coverage_matrix.language_of({}, text) == "en"


def test_coverage_matrix_omits_restricted_regions_and_languages(
    tmp_path, monkeypatch, capsys
):
    patch_manifest(monkeypatch, coverage_matrix)
    monkeypatch.setattr(coverage_matrix, "head_of", lambda _row: "")
    report = tmp_path / "coverage.json"
    monkeypatch.setattr(sys, "argv", ["coverage_matrix.py", "--json", str(report)])

    coverage_matrix.main()

    output = capsys.readouterr().out
    data = json.loads(report.read_text())
    assert "1 training-eligible docs" in output
    assert "2 training-excluded provenance rows omitted" in output
    assert data["docs"] == 1
    assert data["training_excluded_docs"] == 2
    assert data["region"] == {"US": 1}
    assert data["language"] == {"en": 1}
