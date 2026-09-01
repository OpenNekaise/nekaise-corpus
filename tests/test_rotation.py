import json

import pytest

import rotation


@pytest.mark.parametrize(
    ("bucket", "expected"),
    [
        ("2023-W29", "2023-W28"),
        ("2024-W01", "2023-W52"),
        ("2021-W01", "2020-W53"),
        ("2016-W01", "2015-W53"),
    ],
)
def test_prev_week_follows_iso_calendar(bucket, expected):
    assert rotation._prev_week(bucket) == expected


def test_prev_week_rejects_invalid_iso_week():
    with pytest.raises(ValueError, match="not a valid ISO weekly bucket"):
        rotation._prev_week("2021-W53")


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("2021-W01", "2020-W53"),
        ("2020-W53", "2017-W48"),
    ],
)
def test_advance_preserves_virgin_week_before_skipping_mined_range(
        current, expected, tmp_path, monkeypatch):
    path = tmp_path / "rotation.json"
    path.write_text(json.dumps({
        "find_patents": {
            "flag": "--bucket",
            "next": current,
            "skip": [["2020-W52", "2017-W49"]],
        }
    }))
    monkeypatch.setattr(rotation, "PATH", path)
    monkeypatch.setattr(rotation.ops, "WORKSPACE", tmp_path)

    assert rotation.advance("find_patents") == f"--bucket {expected}"
    assert rotation.load()["find_patents"]["next"] == expected


def test_skip_range_can_cross_a_53_week_year(tmp_path, monkeypatch):
    path = tmp_path / "rotation.json"
    path.write_text(json.dumps({
        "find_patents": {
            "flag": "--bucket",
            "next": "2021-W01",
            "skip": [["2020-W53", "2019-W52"]],
        }
    }))
    monkeypatch.setattr(rotation, "PATH", path)
    monkeypatch.setattr(rotation.ops, "WORKSPACE", tmp_path)

    assert rotation.advance("find_patents") == "--bucket 2019-W51"


def test_skip_range_can_jump_a_completed_multi_decade_patent_span(tmp_path, monkeypatch):
    path = tmp_path / "rotation.json"
    path.write_text(json.dumps({
        "find_patents": {
            "flag": "--bucket",
            "next": "2026-W20",
            "skip": [["2026-W19", "1998-W19"]],
        }
    }))
    monkeypatch.setattr(rotation, "PATH", path)
    monkeypatch.setattr(rotation.ops, "WORKSPACE", tmp_path)

    assert rotation.advance("find_patents") == "--bucket 1998-W18"
    assert rotation.load()["find_patents"]["next"] == "1998-W18"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (["2020-W52", "2017-W49"], "skip\\[0\\] must be"),
        ([["2017-W49", "2020-W52"]], "newest bucket must not precede"),
        ([["2020-W53", "2020-W54"]], "not a valid ISO weekly bucket"),
    ],
)
def test_malformed_skip_range_is_rejected(value, message):
    with pytest.raises(ValueError, match=message):
        rotation._skip_ranges(value)


def test_dynamic_pointer_is_replaced_atomically(tmp_path, monkeypatch):
    path = tmp_path / "rotation.json"
    path.write_text(json.dumps({
        "find_kitopen": {
            "flag": "--token",
            "next": "START",
            "dynamic": True,
        }
    }))
    monkeypatch.setattr(rotation, "PATH", path)
    monkeypatch.setattr(rotation.ops, "WORKSPACE", tmp_path)

    assert rotation.set_next("find_kitopen", "opaque-token") == "--token opaque-token"
    assert rotation.load()["find_kitopen"]["next"] == "opaque-token"
    with pytest.raises(ValueError, match="must be replaced"):
        rotation.advance("find_kitopen")


def test_dynamic_rotation_contract_requires_a_string_and_forbids_weekly_skips():
    assert rotation.validate_entry(
        "find_kitopen", {"next": 1, "dynamic": True}
    ) == ["find_kitopen: dynamic rotation requires a non-empty string pointer"]
    assert rotation.validate_entry(
        "find_kitopen", {"next": "START", "dynamic": True, "skip": []}
    ) == ["find_kitopen: dynamic rotation cannot use weekly skip ranges"]


@pytest.mark.parametrize("value", ["", "two\nlines"])
def test_dynamic_pointer_rejects_invalid_control_values(value, tmp_path, monkeypatch):
    path = tmp_path / "rotation.json"
    path.write_text(json.dumps({
        "find_kitopen": {"flag": "--token", "next": "START", "dynamic": True}
    }))
    monkeypatch.setattr(rotation, "PATH", path)
    monkeypatch.setattr(rotation.ops, "WORKSPACE", tmp_path)

    with pytest.raises(ValueError, match="one non-empty line"):
        rotation.set_next("find_kitopen", value)
