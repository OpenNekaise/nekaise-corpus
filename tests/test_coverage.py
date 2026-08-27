import json
import sys

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
