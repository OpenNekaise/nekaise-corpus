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
    import pipeline_repo
    return {
        "cn": pipeline_repo.restriction({"id_prefix": "pat-cn"}),
        "jstage": pipeline_repo.restriction({"source": "jstage_aij"}),
    }


def store_with(monkeypatch, tmp_path, module, rows, restrictions=None):
    """The coverage tools read the manifest through the store: build one holding `rows`."""
    import registry
    shards = {}
    for row in rows:
        shards.setdefault(registry.manifest_shard(row["id"]), []).append(dict(row, topic="t"))
    (tmp_path / "manifest").mkdir(parents=True, exist_ok=True)
    for stem, group in shards.items():
        (tmp_path / "manifest" / f"{stem}.jsonl").write_text(registry.manifest_shard_text(group))
    monkeypatch.setattr(module.registry, "ROOT", tmp_path)
    # the tools read eligibility from their view's pinned configuration
    import pipeline_repo
    (tmp_path / "registry").mkdir(exist_ok=True)
    (tmp_path / "registry" / "eligibility.json").write_text(
        json.dumps({"version": 1, "restrictions": restrictions or {}}))
    pipeline_repo.write_policy(tmp_path, None)


def patch_manifest(monkeypatch, module, tmp_path):
    store_with(monkeypatch, tmp_path, module, fixture_rows(), fixture_restrictions())


def test_genre_coverage_omits_training_excluded_rows(monkeypatch, capsys, tmp_path):
    patch_manifest(monkeypatch, coverage_report, tmp_path)
    monkeypatch.setattr(sys, "argv", ["coverage.py"])

    coverage_report.main()

    output = capsys.readouterr().out
    assert "1 training-eligible docs" in output
    assert "2 training-excluded provenance rows omitted" in output
    patent_line = next(line for line in output.splitlines() if line.strip().startswith("patents "))
    assert patent_line.split()[:2] == ["patents", "1"]
    assert "jstage_aij" not in output


def test_vendor_sources_are_manufacturer_literature(monkeypatch, capsys, tmp_path):
    row = {
        "id": "vnd-sika-1", "title": "Construction product manual", "status": "ok",
        "source": "vendor_sika", "license": "open", "text_chars": 100,
    }
    store_with(monkeypatch, tmp_path, coverage_report, [row])
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


def test_cross_published_globalabc_rows_use_originating_genre(monkeypatch, capsys, tmp_path):
    rows = [
        {
            "id": "iag-globalabc-regional-roadmap-for-buildings",
            "title": "GlobalABC Regional Roadmap",
            "status": "ok",
            "source": "iea",
            "license": "cc-by",
            "text_chars": 100,
        },
        {
            "id": "iag-global-status-report-for-buildings-and-constructio",
            "title": "Global Status Report for Buildings and Construction 2019",
            "status": "ok",
            "source": "iea",
            "license": "cc-by",
            "text_chars": 100,
        },
        {
            "id": "iea-energy-efficiency-2025",
            "title": "Energy Efficiency 2025",
            "status": "ok",
            "source": "iea",
            "license": "cc-by",
            "text_chars": 100,
        },
    ]
    store_with(monkeypatch, tmp_path, coverage_report, rows)
    monkeypatch.setattr(sys, "argv", ["coverage.py", "--sources"])

    coverage_report.main()

    output = capsys.readouterr().out
    ngo_line = next(
        line for line in output.splitlines()
        if line.strip().startswith("industry_ngo_utility ")
    )
    international_line = next(
        line for line in output.splitlines()
        if line.strip().startswith("international_bodies ")
    )
    assert ngo_line.split()[:2] == ["industry_ngo_utility", "2"]
    assert international_line.split()[:2] == ["international_bodies", "1"]
    source_line = next(line for line in output.splitlines() if line.strip().startswith("iea "))
    assert "international_bodies:1" in source_line
    assert "industry_ngo_utility:2" in source_line


@pytest.mark.parametrize(
    "row",
    [
        {
            "id": "doc-ufc-3-410-01-hvac",
            "title": "UFC 3-410-01 Heating, Ventilating, and Air Conditioning Systems",
            "source": "wbdg",
        },
        {
            "id": "guk-fire-safety-approved-document-b",
            "title": "Fire safety: Approved Document B — Approved Document B, volume 1",
            "source": "gov_uk",
        },
        {
            "id": "bov-boverkets-byggregler-bbr",
            "title": "Boverkets byggregler, BBR",
            "source": "boverket",
        },
        {
            "id": "bov-allmanna-rad-om-andring",
            "title": "Allmänna råd om ändring av byggnad, BÄR",
            "source": "boverket",
        },
    ],
)
def test_reviewed_regulatory_documents_are_codes_and_standards(row):
    assert coverage_report.genre_of_row(row) == "codes_standards"


@pytest.mark.parametrize(
    "row",
    [
        {
            "id": "guk-future-buildings-standard-consultation",
            "title": "The Future Buildings Standard consultation document",
            "source": "gov_uk",
        },
        {
            "id": "bov-budgetunderlag-2026-2028",
            "title": "Boverkets budgetunderlag 2026–2028",
            "source": "boverket",
        },
        {
            "id": "other-approved-document",
            "title": "Approved Document for an unrelated collection",
            "source": "iea",
        },
    ],
)
def test_broad_government_material_is_not_misclassified_as_codes(row):
    assert coverage_report.genre_of_row(row) == "international_bodies"


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
    patch_manifest(monkeypatch, coverage_matrix, tmp_path / "repo")
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
