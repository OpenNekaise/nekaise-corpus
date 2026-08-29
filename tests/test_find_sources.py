import json
from types import SimpleNamespace
import sys

import find_sources


def stub_registry(monkeypatch):
    monkeypatch.setattr(find_sources.registry, "existing_keys", lambda: (set(), set(), set()))
    monkeypatch.setattr(find_sources.registry, "uniquify_ids", lambda *_args: None)


def test_all_upstreams_throttled_fails_and_persists_retry_after(tmp_path, monkeypatch):
    calls = []

    def throttled(*_args):
        calls.append(1)
        error = RuntimeError("rate limited")
        error.response = SimpleNamespace(status_code=429, headers={"Retry-After": "60"})
        raise error

    monkeypatch.setattr(find_sources, "QUERIES", [
        ("one", "construction"),
        ("two", "construction"),
        ("three", "construction"),
        ("four", "construction"),
        ("five", "construction"),
    ])
    monkeypatch.setattr(find_sources, "BACKENDS", {"openalex": throttled})
    monkeypatch.setattr(find_sources, "COOLDOWN_FILE", tmp_path / "cooldowns.json")
    monkeypatch.setattr(find_sources.time, "time", lambda: 1_000)
    stub_registry(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_sources.py", "--backends", "openalex", "--circuit-threshold", "3"],
    )

    result = find_sources.main()

    assert len(calls) == 3
    assert result == 1
    assert json.loads(find_sources.COOLDOWN_FILE.read_text()) == {"openalex": 1_060}


def test_persisted_cooldown_with_other_backend_dry_success_is_ok(tmp_path, monkeypatch):
    cooldown_file = tmp_path / "cooldowns.json"
    cooldown_file.write_text('{"openalex": 1060}\n')
    openalex_calls = []
    arxiv_calls = []

    def openalex(*_args):
        openalex_calls.append(1)
        return []

    def arxiv(*_args):
        arxiv_calls.append(1)
        return []

    monkeypatch.setattr(find_sources, "QUERIES", [
        ("one", "construction"),
        ("two", "construction"),
    ])
    monkeypatch.setattr(find_sources, "BACKENDS", {
        "openalex": openalex,
        "arxiv": arxiv,
    })
    monkeypatch.setattr(find_sources, "COOLDOWN_FILE", cooldown_file)
    monkeypatch.setattr(find_sources.time, "time", lambda: 1_000)
    stub_registry(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_sources.py", "--backends", "openalex,arxiv"],
    )

    result = find_sources.main()

    assert result == 0
    assert openalex_calls == []
    assert len(arxiv_calls) == 2


def test_openalex_and_arxiv_receive_requested_page(monkeypatch):
    requests = []

    class Response:
        text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return {"results": []}

    def get(url, *, params, timeout):
        requests.append((url, params, timeout))
        return Response()

    monkeypatch.setattr(find_sources.requests, "get", get)
    monkeypatch.setattr(find_sources.time, "sleep", lambda _seconds: None)

    find_sources.from_openalex("concrete", "materials", 20, page=5)
    find_sources.from_arxiv("concrete", "materials", 20, page=5)

    assert requests[0][1]["page"] == 5
    assert requests[1][1]["start"] == 80


def test_openalex_relevance_and_license_gate(monkeypatch):
    def location(url, license_name):
        return {"pdf_url": url, "license": license_name}

    def work(title, subfield, license_name="cc-by", *, locations=None):
        best = location(f"https://arxiv.org/pdf/{len(title)}", license_name)
        return {
            "display_name": title,
            "primary_topic": {
                "subfield": {"id": f"https://openalex.org/subfields/{subfield}"}
            },
            "topics": [],
            "best_oa_location": best,
            "locations": locations or [],
        }

    results = [
        work("Computational intelligence techniques for HVAC systems", "2215"),
        # Narrow title kills beat an erroneous AEC subfield assignment.  Their full text is rich
        # in generic construction/material vocabulary, so the downstream density gate keeps them.
        work("Design and construction of the DEAP-3600 dark matter detector", "2215"),
        work("UAV remote sensing for field-based crop phenotyping", "2305"),
        # OpenAlex sometimes classifies building metadata work as computer vision; the title rescue
        # protects explicit AEC material without admitting generic computer-science results.
        work("Brick metadata schema for portable smart building applications", "1707", "other-oa"),
        work("Gebäudeenergie und Lüftung im Bestand", "1707"),
        work("A Review of Antibiotic Resistance in Wastewater Treatment Plants", "2404"),
        work("The Polarimetric and Helioseismic Imager on Solar Orbiter", "3103"),
        work("NINE-YEAR WMAP OBSERVATIONS", "3103"),
        work("Standardized preprocessing for large-scale EEG analysis", "2805"),
        work("The internet of things for smart manufacturing", "2209"),
        work("Deep learning in medical imaging", "2707"),
        work("Livestock thermal comfort during road transportation", "1103"),
        work("Building energy optimization", "2215", "cc-by-nc-nd"),
        work(
            "Ventilation control in office buildings",
            "2215",
            "cc-by-nc",
            locations=[location("https://escholarship.org/permissive.pdf", "cc-by")],
        ),
    ]

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": results}

    monkeypatch.setattr(find_sources.requests, "get", lambda *_args, **_kwargs: Response())

    got = find_sources.from_openalex("building controls", "controls_bas", 100)

    assert [row["title"] for row in got] == [
        "Computational intelligence techniques for HVAC systems",
        "Brick metadata schema for portable smart building applications",
        "Gebäudeenergie und Lüftung im Bestand",
        "A Review of Antibiotic Resistance in Wastewater Treatment Plants",
        "Ventilation control in office buildings",
    ]
    assert got[1]["license"] == "open"
    assert got[1]["license_evidence"] == "OpenAlex OA location license: other-oa"
    assert got[-1]["url"] == "https://escholarship.org/permissive.pdf"
    assert got[-1]["license"] == "cc-by"


def test_openalex_skips_paused_mdpi_host_for_fetchable_alternative():
    work = {
        "best_oa_location": {
            "pdf_url": "https://www.mdpi.com/2075-5309/13/6/1388/pdf",
            "license": "cc-by",
        },
        "locations": [{
            "pdf_url": "https://escholarship.org/uc/item/abc123.pdf",
            "license": "cc-by",
        }],
    }

    assert not find_sources.downloadable(work["best_oa_location"]["pdf_url"])
    assert find_sources._openalex_location(work) == (work["locations"][0], "cc-by")


def test_query_cursor_walks_queries_then_advances_page():
    queries = [
        ("one", "construction"),
        ("two", "materials"),
        ("three", "architecture"),
    ]
    assert find_sources.query_window(queries, page=1, cursor=2, count=3) == [
        ("three", "architecture", 1),
        ("one", "construction", 2),
        ("two", "materials", 2),
    ]


def test_openalex_query_cursor_width_is_pinned_for_reproducibility():
    assert len(find_sources.QUERIES) == find_sources.QUERY_CURSOR_WIDTH == 105


def test_query_count_bounds_requests_in_main(tmp_path, monkeypatch):
    calls = []

    def available(term, topic, per, page):
        calls.append((term, topic, per, page))
        return []

    monkeypatch.setattr(find_sources, "QUERIES", [
        ("one", "construction"),
        ("two", "materials"),
        ("three", "architecture"),
    ])
    monkeypatch.setattr(find_sources, "QUERY_CURSOR_WIDTH", 3)
    monkeypatch.setattr(find_sources, "BACKENDS", {"openalex": available})
    monkeypatch.setattr(find_sources, "COOLDOWN_FILE", tmp_path / "cooldowns.json")
    stub_registry(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "find_sources.py", "--backends", "openalex", "--per", "100",
            "--query-cursor", "4", "--query-count", "1",
        ],
    )

    assert find_sources.main() == 0
    assert calls == [("two", "materials", 100, 2)]


def test_partial_upstream_run_requests_rotation_hold(tmp_path, monkeypatch, capsys):
    calls = []

    def partly_available(*_args):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("temporary outage")
        return []

    monkeypatch.setattr(find_sources, "QUERIES", [
        ("one", "construction"),
        ("two", "construction"),
    ])
    monkeypatch.setattr(find_sources, "BACKENDS", {"arxiv": partly_available})
    monkeypatch.setattr(find_sources, "COOLDOWN_FILE", tmp_path / "cooldowns.json")
    hold = tmp_path / "rotation-hold"
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(hold))
    stub_registry(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_sources.py", "--backends", "arxiv", "--page", "7"],
    )

    result = find_sources.main()

    assert result == 0
    assert hold.read_text() == "incomplete upstream request(s): arxiv\n"
    assert "rotation hold requested" in capsys.readouterr().err
