import sys
from types import SimpleNamespace

import pytest

import find_zenodo_sim as fz
import lint_registry
import registry


def _rec(rid, title, lic="cc-by-4.0", access="open", files=("paper.pdf",), doi=None,
         rtype="publication"):
    meta = {"title": title, "access_right": access, "publication_date": "2023-05-01",
            "resource_type": {"type": rtype, "subtype": "conferencepaper"}}
    if lic is not None:
        meta["license"] = {"id": lic}
    return {"id": rid, "doi": doi, "metadata": meta,
            "files": [{"key": k, "links": {"self": f"https://zenodo.org/api/records/{rid}/files/"
                                                  f"{k}/content"}} for k in files]}


EPLUS = _rec(1, "Calibrating an EnergyPlus model of an office building", doi="10.5281/zenodo.1")
NC = _rec(2, "Daylight simulation of school classrooms", lic="cc-by-nc-nd-4.0")
OTHER = _rec(3, "TRNSYS model of a solar heating system", lic="zenodo-freetoread-1.0")
NOLIC = _rec(4, "Indoor air temperature simulation of a dwelling", lic=None)
CLOSED = _rec(5, "Building energy simulation of a hospital", access="restricted")
NOPDF = _rec(6, "Building energy simulation of a hotel", files=("data.csv",))
OFF = _rec(7, "Simulation of protein folding")
DATASET = _rec(8, "Building energy simulation results", rtype="dataset")


def test_candidate_gate_and_licence_classes():
    got = {r["id"]: fz.candidate(r, "2026-10-02") for r in
           (EPLUS, NC, OTHER, NOLIC, CLOSED, NOPDF, OFF, DATASET)}
    assert {k for k, v in got.items() if v} == {1, 2, 3, 4}
    assert got[1]["license"] == "cc-by" and got[1]["persistent_id"] == "doi:10.5281/zenodo.1"
    assert got[2]["license"] == "cc-by-nc-nd"
    assert got[3]["license"] == "unverified" and "freetoread" in got[3]["license_evidence"]
    assert got[4]["license"] == "unverified"
    for e in (got[1], got[2], got[3], got[4]):
        assert e["topic"] == "simulation_modeling" in lint_registry.TOPICS
        assert e["license"] in lint_registry.LICENSES
        assert registry.shard_filename(e["id"]) == "zenodosim.yaml"
        assert not lint_registry.entry_errors(e, "zenodosim.yaml")


@pytest.mark.parametrize("lid,tag", [
    ("cc-by-4.0", "cc-by"), ("cc-by-sa-4.0", "cc-by-sa"), ("cc-zero", "cc0"),
    ("cc-by-nc-4.0", "cc-by-nc"), ("cc-by-nc-sa-4.0", "cc-by-nc-sa"), ("cc-by-nd-4.0", "cc-by-nd"),
    ("cc-nc", "unverified"), ("other-open", "unverified"), ("cc-byx", "unverified"),
    ("cc-by-invalid", "unverified"), ("cc-by-sa-not-a-license", "unverified"),
    ("cc-by", "cc-by"), ("cc-by-3.0-de", "cc-by"), ("cc0-1.0", "cc0"), ("cc-by-4.0x", "unverified"),
])
def test_licence_ids(lid, tag):
    assert fz.licence({"license": {"id": lid}})[0] == tag


def _setup(monkeypatch, tmp_path, pages, known=None):
    known = known or (set(), set(), set())
    calls, queue = [], iter(pages)

    def get(url, **kwargs):
        calls.append(kwargs["params"])
        item = next(queue)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, SimpleNamespace):
            return item
        return SimpleNamespace(status_code=200, json=lambda item=item: {"hits": {"hits": item}})

    monkeypatch.setattr(fz.requests, "get", get)
    monkeypatch.setattr(fz.time, "sleep", lambda _s: calls.append("sleep"))
    monkeypatch.setattr(fz.dedup, "open_keys", lambda: fz.dedup.from_sets(*known))
    appended = []
    monkeypatch.setattr(fz.registry, "append_entries", appended.extend)
    files = {n: tmp_path / n for n in ("next", "exhausted", "hold")}
    for env, name in (("NEKAISE_ROTATION_NEXT_FILE", "next"),
                      ("NEKAISE_BACKEND_EXHAUSTED_FILE", "exhausted"),
                      ("NEKAISE_ROTATION_HOLD_FILE", "hold")):
        monkeypatch.setenv(env, str(files[name]))
    return calls, appended, files


def test_full_page_advances_page_short_page_advances_query(monkeypatch, tmp_path):
    monkeypatch.setattr(fz, "ROWS", 2)
    known = (set(), set(), set(), {"doi:10.5281/zenodo.1"})
    calls, appended, files = _setup(monkeypatch, tmp_path, [[EPLUS, NC], [OTHER]], known)
    monkeypatch.setattr(sys, "argv", ["find_zenodo_sim.py", "--pages", "2", "--append"])
    fz.main()
    made = [c for c in calls if c != "sleep"]
    assert [(c["q"], c["page"]) for c in made] == [(fz.QUERIES[0], 1), (fz.QUERIES[0], 2)]
    assert all(c["type"] == "publication" and c["sort"] == "oldest" for c in made)
    assert calls.count("sleep") == 1
    assert [e["license"] for e in appended] == ["cc-by-nc-nd", "unverified"]  # EPLUS DOI known
    assert files["next"].read_text() == "q=1 p=1\n"


def test_last_query_finished_reports_exhausted(monkeypatch, tmp_path):
    _, _, files = _setup(monkeypatch, tmp_path, [[]])
    monkeypatch.setattr(sys, "argv", ["find_zenodo_sim.py", "--cursor",
                                      f"q={len(fz.QUERIES) - 1} p=2"])
    fz.main()
    assert files["next"].read_text() == "END\n" and files["exhausted"].read_text().strip()


@pytest.mark.parametrize("answer", [
    SimpleNamespace(status_code=429, json=lambda: {}),
    SimpleNamespace(status_code=200, json=lambda: {"message": "error"}),
    SimpleNamespace(status_code=200, json=lambda: (_ for _ in ()).throw(ValueError("html"))),
])
def test_rate_limit_or_unexpected_answer_holds(monkeypatch, tmp_path, answer):
    monkeypatch.setattr(fz, "ROWS", 1)
    _, appended, files = _setup(monkeypatch, tmp_path, [[NC], answer])
    monkeypatch.setattr(sys, "argv", ["find_zenodo_sim.py", "--pages", "3", "--append"])
    fz.main()
    assert appended == [] and files["hold"].read_text().strip() and not files["next"].exists()


def test_network_failure_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr(fz, "ROWS", 1)
    _, appended, files = _setup(monkeypatch, tmp_path, [[NC], TimeoutError("x")])
    monkeypatch.setattr(sys, "argv", ["find_zenodo_sim.py", "--pages", "3", "--append"])
    with pytest.raises(SystemExit) as exc:
        fz.main()
    assert exc.value.code == 1 and appended == [] and not files["next"].exists()


@pytest.mark.parametrize("cursor", ["", "q=1", "q=a p=1", "q=1 p=0"])
def test_malformed_cursor_is_rejected(cursor):
    with pytest.raises(ValueError):
        fz.parse_cursor(cursor)
