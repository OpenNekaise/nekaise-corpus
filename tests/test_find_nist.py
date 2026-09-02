import sys
from datetime import datetime, timezone

import pytest

import find_nist
import lint_registry


@pytest.mark.parametrize(
    ("title", "topic"),
    [
        ("Annex 47 Report 1: Commissioning Overview", "commissioning_fdd"),
        ("A simulation study of fault detection in HVAC systems", "commissioning_fdd"),
        ("Programmers guide to the BACnet communications DLL", "standards_protocols"),
        ("Summer attic and whole-house ventilation", "building_energy"),
        ("Sensitivity of heat pump performance", "equipment_systems"),
        ("Seismic provisions for structural building codes", "standards_protocols"),
        ("Building automation sensor controls", "controls_bas"),
    ],
)
def test_title_gate_accepts_aec_titles_and_assigns_valid_topics(title, topic):
    assert find_nist.title_in_scope(title)
    assert find_nist.classify_topic(title) == topic
    assert topic in lint_registry.TOPICS


@pytest.mark.parametrize(
    "title",
    [
        "On strongly continuous stochastic processes",
        "Message handling systems interoperability tests",
        "Guidelines for smart grid cybersecurity",
        "Code extension techniques for the 7-bit coded character set",
        "Material Handling Workstation implementation",
        "Nuclear Regulatory Commission annual report",
    ],
)
def test_title_gate_rejects_observed_crossref_false_positives(title):
    assert not find_nist.title_in_scope(title)
    assert find_nist.classify_topic(title) is None


def test_month_index_is_stable_and_uses_real_calendar_boundaries():
    assert find_nist.FIRST_MONTH_INDEX == 24143
    assert find_nist.month_bounds(24143) == ("2011-12-01", "2011-12-31")
    assert find_nist.month_bounds(2012 * 12 + 1) == ("2012-02-01", "2012-02-29")
    with pytest.raises(ValueError, match="predates"):
        find_nist.month_bounds(24142)


def test_current_utc_month_index_uses_rotation_encoding():
    now = datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)
    assert find_nist.current_utc_month_index(now) == 24320


def _item(title, url="https://nvlpubs.nist.gov/report.pdf"):
    return {"title": [title], "resource": {"primary": {"URL": url}}}


class _Response:
    def __init__(self, message):
        self.message = message

    def raise_for_status(self):
        pass

    def json(self):
        return {"message": self.message}


def test_crossref_cursor_enumerates_month_and_applies_title_gate(monkeypatch):
    calls = []
    responses = iter(
        [
            _Response(
                {
                    "total-results": 3,
                    "next-cursor": "second",
                    "items": [
                        _item("HVAC functional inspection and testing guide"),
                        _item("Message handling systems interoperability tests"),
                    ],
                }
            ),
            _Response(
                {
                    "total-results": 3,
                    "next-cursor": "unused",
                    "items": [_item("Heat pump field performance", "https://example.org/x.pdf")],
                }
            ),
        ]
    )

    def get(_url, **kwargs):
        calls.append(kwargs["params"])
        return next(responses)

    monkeypatch.setattr(find_nist.requests, "get", get)
    monkeypatch.setattr(find_nist.time, "sleep", lambda _seconds: None)

    assert find_nist.from_crossref(24143, 2) == [
        (
            "HVAC functional inspection and testing guide",
            "https://nvlpubs.nist.gov/report.pdf",
            "equipment_systems",
        )
    ]
    assert [call["cursor"] for call in calls] == ["*", "second"]
    assert all(call["mailto"] == find_nist.MAILTO for call in calls)
    assert calls[0]["filter"] == (
        "prefix:10.6028,from-created-date:2011-12-01,until-created-date:2011-12-31"
    )


def test_cursor_expiry_fails_closed(monkeypatch):
    responses = iter(
        [
            _Response(
                {
                    "total-results": 2,
                    "next-cursor": "expired-token",
                    "items": [_item("HVAC field test")],
                }
            ),
            TimeoutError("cursor expired"),
        ]
    )

    def get(*_args, **_kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(find_nist.requests, "get", get)
    monkeypatch.setattr(find_nist.time, "sleep", lambda _seconds: None)

    with pytest.raises(TimeoutError, match="expired"):
        find_nist.from_crossref(24143, 1)


@pytest.mark.parametrize(
    "message, match",
    [
        ({"total-results": 2, "items": []}, "ended early"),
        ({"total-results": 2, "items": [_item("HVAC field test")]}, "omitted next-cursor"),
        (
            {
                "total-results": 2,
                "next-cursor": "*",
                "items": [_item("HVAC field test")],
            },
            "repeated a cursor",
        ),
    ],
)
def test_incomplete_cursor_shapes_fail_closed(monkeypatch, message, match):
    monkeypatch.setattr(find_nist.requests, "get", lambda *_args, **_kwargs: _Response(message))
    with pytest.raises(RuntimeError, match=match):
        find_nist.from_crossref(24143, 1)


def _empty_registry(monkeypatch):
    monkeypatch.setattr(find_nist.registry, "existing_keys", lambda: (set(), set(), set()))


def test_api_failure_exits_nonzero_before_partial_append(monkeypatch, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(
        find_nist,
        "from_crossref",
        lambda *_args: (_ for _ in ()).throw(TimeoutError("offline")),
    )
    monkeypatch.setattr(
        find_nist.registry,
        "append_entries",
        lambda _entries: pytest.fail("partial results must not be appended"),
    )
    monkeypatch.setattr(sys, "argv", ["find_nist.py", "--append"])

    with pytest.raises(SystemExit) as exc:
        find_nist.main()

    assert exc.value.code == 1
    assert "refusing a partial append so rotation does not advance" in capsys.readouterr().err


def test_candidate_cap_emits_batch_and_requests_rotation_hold(monkeypatch, tmp_path, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(
        find_nist,
        "from_crossref",
        lambda *_args: [
            (f"HVAC report {index}", f"https://nvlpubs.nist.gov/{index}.pdf", "equipment_systems")
            for index in range(3)
        ],
    )
    appended = []
    monkeypatch.setattr(find_nist.registry, "append_entries", lambda entries: appended.extend(entries))
    hold = tmp_path / "rotation-hold"
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(hold))
    monkeypatch.setattr(sys, "argv", ["find_nist.py", "--max", "2", "--append"])

    find_nist.main()

    assert len(appended) == 2
    assert hold.exists()
    assert "has 3 new candidates; emitted 2" in hold.read_text()
    assert "rotation hold requested" in capsys.readouterr().err


def test_past_empty_month_advances_without_hold(monkeypatch, tmp_path, capsys):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(find_nist, "current_utc_month_index", lambda: 24144)
    monkeypatch.setattr(find_nist, "from_crossref", lambda *_args: [])
    monkeypatch.setattr(
        find_nist.registry,
        "append_entries",
        lambda _entries: pytest.fail("an empty result must not append"),
    )
    hold = tmp_path / "rotation-hold"
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(hold))
    monkeypatch.setattr(sys, "argv", ["find_nist.py", "--append"])

    find_nist.main()

    assert not hold.exists()
    assert "0 NEW NIST/NBS technical series PDFs" in capsys.readouterr().out


def test_current_month_is_scanned_and_held_for_reprobe(monkeypatch, tmp_path):
    _empty_registry(monkeypatch)
    scanned = []
    monkeypatch.setattr(find_nist, "current_utc_month_index", lambda: 24320)
    monkeypatch.setattr(
        find_nist,
        "from_crossref",
        lambda month_index, _rows: scanned.append(month_index) or [],
    )
    hold = tmp_path / "rotation-hold"
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(hold))
    monkeypatch.setattr(sys, "argv", ["find_nist.py", "--month-index", "24320"])

    find_nist.main()

    assert scanned == [24320]
    assert "open UTC month; re-probe until it closes" in hold.read_text()


def test_future_month_is_not_queried_and_requests_hold(monkeypatch, tmp_path):
    _empty_registry(monkeypatch)
    monkeypatch.setattr(find_nist, "current_utc_month_index", lambda: 24320)
    monkeypatch.setattr(
        find_nist,
        "from_crossref",
        lambda *_args: pytest.fail("future months must not query Crossref"),
    )
    hold = tmp_path / "rotation-hold"
    monkeypatch.setenv("NEKAISE_ROTATION_HOLD_FILE", str(hold))
    monkeypatch.setattr(sys, "argv", ["find_nist.py", "--month-index", "24321"])

    find_nist.main()

    assert "beyond the current UTC month" in hold.read_text()
