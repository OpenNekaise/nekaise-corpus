import sys

import pytest
import requests

import find_patents


def _empty_registry(monkeypatch):
    monkeypatch.setattr(
        find_patents.registry,
        "existing_keys",
        lambda: (set(), set(), set()),
    )


def test_fetch_retries_transient_5xx_with_bounded_backoff(monkeypatch):
    calls = 0
    sleeps = []

    class Response:
        text = "recovered"
        status_code = 200

        def raise_for_status(self):
            nonlocal calls
            calls += 1
            if calls < 3:
                failed = requests.Response()
                failed.status_code = 503
                raise requests.HTTPError("service unavailable", response=failed)

    monkeypatch.setattr(find_patents.requests, "get", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(find_patents.time, "sleep", sleeps.append)

    assert find_patents.fetch("https://example.test/sitemap.html") == "recovered"
    assert calls == 3
    assert sleeps == [2.0, 4.0]


def test_fetch_exhausts_transient_timeouts(monkeypatch):
    calls = 0
    sleeps = []

    def timeout(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise requests.Timeout("timed out")

    monkeypatch.setattr(find_patents.requests, "get", timeout)
    monkeypatch.setattr(find_patents.time, "sleep", sleeps.append)

    with pytest.raises(requests.Timeout, match="timed out"):
        find_patents.fetch("https://example.test/sitemap.html")

    assert calls == 4
    assert sleeps == [2.0, 4.0, 8.0]


def test_fetch_does_not_retry_permanent_http_error(monkeypatch):
    calls = 0

    def not_found(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        failed = requests.Response()
        failed.status_code = 404
        raise requests.HTTPError("not found", response=failed)

    monkeypatch.setattr(find_patents.requests, "get", not_found)
    monkeypatch.setattr(
        find_patents.time,
        "sleep",
        lambda _delay: pytest.fail("permanent errors must not be retried"),
    )

    with pytest.raises(requests.HTTPError, match="not found"):
        find_patents.fetch("https://example.test/sitemap.html")

    assert calls == 1


def test_subpage_failure_exits_nonzero_before_partial_append(monkeypatch, capsys):
    _empty_registry(monkeypatch)

    def fetch(url):
        if url.endswith("2020-W01.html"):
            return "href='2020-W01-p1.html'\nhref='2020-W01-p2.html'"
        if url.endswith("2020-W01-p1.html"):
            return "<li>US123B1 - Concrete foundation :"
        raise TimeoutError("offline")

    monkeypatch.setattr(find_patents, "fetch", fetch)
    monkeypatch.setattr(find_patents.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        find_patents.registry,
        "append_entries",
        lambda _entries: pytest.fail("partial results must not be appended"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_patents.py", "--bucket", "2020-W01", "--append"],
    )

    with pytest.raises(SystemExit) as exc:
        find_patents.main()

    assert exc.value.code == 1
    assert "refusing a partial append so rotation does not advance" in capsys.readouterr().err


def test_successful_empty_bucket_is_not_a_fetch_failure(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(find_patents, "fetch", lambda _url: "")
    monkeypatch.setattr(
        find_patents.registry,
        "append_entries",
        lambda _entries: pytest.fail("an empty result must not append"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_patents.py", "--bucket", "2020-W01", "--append"],
    )

    find_patents.main()

    assert "0 NEW built-environment US patents" in capsys.readouterr().out


def test_us_design_patents_are_skipped_before_append(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(
        find_patents,
        "fetch",
        lambda _url: "\n".join([
            "<li>USD1074974S1 - Roof fan :",
            "<li>US12345678B2 - Concrete roof connection :",
        ]),
    )
    appended = []
    monkeypatch.setattr(
        find_patents.registry,
        "append_entries",
        lambda entries: appended.extend(entries),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_patents.py", "--bucket", "2025-W17", "--append"],
    )

    find_patents.main()

    assert [entry["id"] for entry in appended] == ["pat-us12345678b2"]
    assert appended[0]["license_url"] == find_patents.USPTO_TERMS
    assert "37 CFR exceptions" in appended[0]["license_evidence"]
    assert "1 US design publications skipped" in capsys.readouterr().out


def test_polysemous_off_domain_titles_are_skipped_before_append(monkeypatch):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(
        find_patents,
        "fetch",
        lambda _url: "\n".join([
            "<li>US100A - Packet tunneling and decapsulation with split-horizon attributes :",
            "<li>US101A - Hair conditioner compositions with a preservative system :",
            "<li>US102A - Block placing tool for building a user-defined algorithm :",
            "<li>US104A - Aerosol-generating system with ventilation airflow :",
            "<li>US105A - End-to-end map building from a video sequence :",
            "<li>US106A - Metal-insulator-metal capacitor and integrated chip :",
            "<li>US107A - Roof top automobile ventilation system :",
            "<li>US108A - Heating control method for electric steamer :",
            "<li>US109A - Methods for building regression trees in a distributed environment :",
            "<li>US103A - Concrete tunnel ventilation system :",
        ]),
    )
    appended = []
    monkeypatch.setattr(
        find_patents.registry,
        "append_entries",
        lambda entries: appended.extend(entries),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_patents.py", "--bucket", "2025-W17", "--append"],
    )

    find_patents.main()

    assert [entry["id"] for entry in appended] == ["pat-us103a"]


def test_unreviewed_jurisdictions_are_policy_blocked_before_fetch(monkeypatch, capsys):
    """US and CN are approved; every other jurisdiction stays gated until rights-reviewed."""
    monkeypatch.setattr(
        find_patents,
        "fetch",
        lambda _url: pytest.fail("a policy-blocked source must not be fetched"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["find_patents.py", "--countries", "EP", "--bucket", "2022-W48"],
    )

    with pytest.raises(SystemExit) as exc:
        find_patents.main()

    assert exc.value.code == 2
    assert "policy-blocked patent source: EP" in capsys.readouterr().err


def test_cn_rows_are_open_licensed_not_uspto_public_domain(tmp_path, monkeypatch, capsys):
    """CN publications carry no USPTO statement — they must not claim public-domain/USPTO terms."""
    _empty_registry(monkeypatch)
    monkeypatch.setattr(
        find_patents,
        "fetch",
        lambda _url: "<li>CN100333646C - Concrete floor heating structure :",
    )
    appended = []
    monkeypatch.setattr(
        find_patents.registry, "append_entries",
        lambda entries, **kw: (appended.extend(entries), {"patents-cn-0.yaml": len(entries)})[1],
    )
    monkeypatch.setattr(
        sys, "argv",
        ["find_patents.py", "--countries", "CN", "--bucket", "2022-W48", "--append"],
    )

    find_patents.main()

    assert [e["id"] for e in appended] == ["pat-cn100333646c"]
    assert appended[0]["license"] == "open"
    assert "license_url" not in appended[0]
    assert "license_evidence" not in appended[0]


def test_candidate_cap_requests_rotation_hold(tmp_path, monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(
        find_patents,
        "fetch",
        lambda _url: "\n".join([
            "<li>US100A - Concrete foundation :",
            "<li>US101A - Bridge foundation :",
        ]),
    )
    appended = []
    monkeypatch.setattr(
        find_patents.registry,
        "append_entries",
        lambda entries: appended.extend(entries),
    )
    hold = tmp_path / "rotation-hold"
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(hold))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "find_patents.py", "--countries", "US", "--bucket", "2022-W48",
            "--max", "1", "--append",
        ],
    )

    find_patents.main()

    assert [entry["id"] for entry in appended] == ["pat-us100a"]
    assert hold.read_text() == "candidate cap reached\n"
    assert "rotation hold requested" in capsys.readouterr().out
