import sys
from types import SimpleNamespace

import pytest

import find_ibpsa

LISTING = b"""<html><body><div class="entry-content single-content">
<h4>List of topics</h4><ul><li>x</li></ul>
<h4>Smart building systems and controls</h4>
<table border='0'>
<tr><td><b><a class='paper_title' href='https://publications.ibpsa.org/conference/paper/?id=bs2025_1'>Model predictive control of a heat pump</a></b></td>
<td><a href='https://publications.ibpsa.org/proceedings/bs/2025/papers/bs2025_1.pdf'>pdf</a></td></tr>
</table>
<h4>Performance-driven design</h4>
<table border='0'>
<tr><td><b><a class='paper_title' href='https://publications.ibpsa.org/conference/paper/?id=bs2025_2'>Daylight simulation of atria</a></b></td>
<td><a href='/proceedings/bs/2025/papers/bs2025_2.pdf'>pdf</a></td></tr>
<tr><td><b><a class='paper_title' href='https://publications.ibpsa.org/conference/paper/?id=bs2025_3'>Urban heat island modelling</a></b></td>
<td><a href='https://publications.ibpsa.org/proceedings/bs/2025/papers/bs2025_3.pdf'>pdf</a></td></tr>
<tr><td><b><a class='paper_title' href='https://publications.ibpsa.org/conference/paper/?id=bs2025_4'>No pdf here</a></b></td><td></td></tr>
</table></div></body></html>"""


def _response(status=200, content=LISTING):
    return SimpleNamespace(
        status_code=status,
        content=content,
        text=content.decode(),
        raise_for_status=lambda: None,
    )


def _setup(monkeypatch, tmp_path, response, known=None):
    known = known or (set(), set(), set())
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs["headers"]))
        return response

    monkeypatch.setattr(find_ibpsa.requests, "get", get)
    monkeypatch.setattr(find_ibpsa.registry, "existing_keys", lambda: known)
    appended = []
    monkeypatch.setattr(find_ibpsa.registry, "append_entries", appended.extend)
    hold = tmp_path / "hold"
    exhausted = tmp_path / "exhausted"
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(hold))
    monkeypatch.setenv("NEKAISE_BACKEND_EXHAUSTED_FILE", str(exhausted))
    return calls, appended, hold, exhausted


def test_universe_is_the_verified_append_only_listing_set():
    assert len(find_ibpsa.UNIVERSE) == 76
    assert len(set(find_ibpsa.UNIVERSE)) == 76
    assert find_ibpsa.UNIVERSE[:3] == (("esim", 2026), ("simbuild", 2026), ("bs", 2025))
    assert find_ibpsa.UNIVERSE[-1] == ("bs", 1985)
    assert {conf for conf, _ in find_ibpsa.UNIVERSE} == {
        "bs", "bsa", "esim", "simbuild", "bso", "asim", "bausim", "usim"
    }


def test_listing_parser_tracks_headings_and_requires_direct_pdf(monkeypatch, tmp_path):
    calls, _, _, _ = _setup(monkeypatch, tmp_path, _response())

    papers = find_ibpsa.fetch_papers("bs", 2025)

    assert [p["title"] for p in papers] == [
        "Model predictive control of a heat pump", "Daylight simulation of atria",
        "Urban heat island modelling",
    ]
    assert [p["topic"] for p in papers] == ["controls_bas", "building_energy", "building_energy"]
    assert calls[0][0] == "https://publications.ibpsa.org/conference/?id=bs2025"
    assert not calls[0][1]["User-Agent"].startswith(("python-requests", "Mozilla"))


def test_slot_run_proposes_deduped_entries_and_advances(monkeypatch, tmp_path, capsys):
    known = ({"https://publications.ibpsa.org/proceedings/bs/2025/papers/bs2025_3.pdf"}, set(),
             set())
    _, appended, hold, _ = _setup(monkeypatch, tmp_path, _response(), known)
    monkeypatch.setattr(sys, "argv", ["find_ibpsa.py", "--slot", "2", "--append"])

    find_ibpsa.main()

    assert [e["url"] for e in appended] == [
        "https://publications.ibpsa.org/proceedings/bs/2025/papers/bs2025_1.pdf",
        "https://publications.ibpsa.org/proceedings/bs/2025/papers/bs2025_2.pdf",
    ]
    assert all(e["id"].startswith("ibp-") and e["license"] == "open" for e in appended)
    assert not hold.exists()  # listing drained: rotation may advance
    assert "2 NEW papers from bs2025" in capsys.readouterr().out


def test_overflow_holds_the_slot_until_drained(monkeypatch, tmp_path):
    _, appended, hold, _ = _setup(monkeypatch, tmp_path, _response())
    monkeypatch.setattr(sys, "argv", ["find_ibpsa.py", "--slot", "2", "--max", "1", "--append"])

    find_ibpsa.main()

    assert len(appended) == 1
    assert "bs2025 has 3 new papers" in hold.read_text()


@pytest.mark.parametrize("status", [202, 403, 429])
def test_captcha_answer_holds_rotation_cleanly(monkeypatch, tmp_path, status, capsys):
    captcha = _response(status, b"<html><script src='/.well-known/sgcaptcha/x.js'></script>")
    _, appended, hold, _ = _setup(monkeypatch, tmp_path, captcha)
    monkeypatch.setattr(sys, "argv", ["find_ibpsa.py", "--slot", "0", "--append"])

    find_ibpsa.main()  # exits 0: the round is not failed

    assert appended == []
    assert f"HTTP {status}" in hold.read_text()
    assert "site challenge" in capsys.readouterr().out


def test_captcha_page_served_with_200_is_still_a_challenge(monkeypatch, tmp_path):
    captcha = _response(200, b"<html><meta http-equiv='refresh' content='0;/.well-known/sgcaptcha/'>")
    _, _, hold, _ = _setup(monkeypatch, tmp_path, captcha)
    monkeypatch.setattr(sys, "argv", ["find_ibpsa.py", "--slot", "0"])

    find_ibpsa.main()

    assert hold.exists()


def test_network_failure_exits_nonzero_so_pointer_is_retained(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, None)
    monkeypatch.setattr(
        find_ibpsa.requests, "get",
        lambda *_a, **_k: (_ for _ in ()).throw(TimeoutError("offline")),
    )
    monkeypatch.setattr(sys, "argv", ["find_ibpsa.py", "--slot", "0", "--append"])

    with pytest.raises(SystemExit) as exc:
        find_ibpsa.main()

    assert exc.value.code == 1


def test_slot_past_universe_holds_without_request_or_exhaustion(monkeypatch, tmp_path):
    calls, _, hold, exhausted = _setup(monkeypatch, tmp_path, _response())
    monkeypatch.setattr(
        sys, "argv", ["find_ibpsa.py", "--slot", str(len(find_ibpsa.UNIVERSE))]
    )

    find_ibpsa.main()

    assert calls == []
    assert "past the 76-listing universe" in hold.read_text()
    assert not exhausted.exists()  # the pointer must stay on the first unvisited index


def test_draining_the_last_slot_reports_exhausted_so_pointer_lands_on_first_unvisited(
    monkeypatch, tmp_path
):
    _, _, hold, exhausted = _setup(monkeypatch, tmp_path, _response())
    last = len(find_ibpsa.UNIVERSE) - 1
    monkeypatch.setattr(sys, "argv", ["find_ibpsa.py", "--slot", str(last)])

    find_ibpsa.main()

    assert "76 IBPSA" in exhausted.read_text()
    assert not hold.exists()
    # run_round advances a non-dynamic pointer by `step` before disabling the backend:
    assert last + 1 == len(find_ibpsa.UNIVERSE)  # = index of the next appended edition


def test_last_slot_overflow_holds_instead_of_exhausting(monkeypatch, tmp_path):
    _, _, hold, exhausted = _setup(monkeypatch, tmp_path, _response())
    last = len(find_ibpsa.UNIVERSE) - 1
    monkeypatch.setattr(sys, "argv", ["find_ibpsa.py", "--slot", str(last), "--max", "1"])

    find_ibpsa.main()

    assert hold.exists() and not exhausted.exists()
