import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import rotation
import store


def _rotation_file(tmp_path):
    """registry/rotation.json of a throwaway repository (the store's root is its parent)."""
    (tmp_path / "registry").mkdir(exist_ok=True)
    return tmp_path / "registry" / "rotation.json"


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
    path = _rotation_file(tmp_path)
    path.write_text(json.dumps({
        "find_patents": {
            "flag": "--bucket",
            "next": current,
            "skip": [["2020-W52", "2017-W49"]],
        }
    }))
    monkeypatch.setattr(rotation, "PATH", path)

    assert rotation.advance("find_patents") == f"--bucket {expected}"
    assert rotation.load()["find_patents"]["next"] == expected


def test_skip_range_can_cross_a_53_week_year(tmp_path, monkeypatch):
    path = _rotation_file(tmp_path)
    path.write_text(json.dumps({
        "find_patents": {
            "flag": "--bucket",
            "next": "2021-W01",
            "skip": [["2020-W53", "2019-W52"]],
        }
    }))
    monkeypatch.setattr(rotation, "PATH", path)

    assert rotation.advance("find_patents") == "--bucket 2019-W51"


def test_skip_range_can_jump_a_completed_multi_decade_patent_span(tmp_path, monkeypatch):
    path = _rotation_file(tmp_path)
    path.write_text(json.dumps({
        "find_patents": {
            "flag": "--bucket",
            "next": "2026-W20",
            "skip": [["2026-W19", "1998-W19"]],
        }
    }))
    monkeypatch.setattr(rotation, "PATH", path)

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
    path = _rotation_file(tmp_path)
    path.write_text(json.dumps({
        "find_kitopen": {
            "flag": "--token",
            "next": "START",
            "dynamic": True,
        }
    }))
    monkeypatch.setattr(rotation, "PATH", path)

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
    path = _rotation_file(tmp_path)
    path.write_text(json.dumps({
        "find_kitopen": {"flag": "--token", "next": "START", "dynamic": True}
    }))
    monkeypatch.setattr(rotation, "PATH", path)

    with pytest.raises(ValueError, match="one non-empty line"):
        rotation.set_next("find_kitopen", value)


# --- standalone writes go through the store (ADR 0001 stage 3, step 5) ----------------------------

STATE = {
    "find_osti": {"flag": "--page", "next": 7, "step": 2},
    "find_kitopen": {"flag": "--token", "next": "START", "dynamic": True},
}


def _legacy_bytes(state):
    """What the retired rotation.save() wrote."""
    return json.dumps(state, indent=2, ensure_ascii=False) + "\n"


def test_standalone_advance_is_one_journaled_store_transaction(tmp_path, monkeypatch):
    path = _rotation_file(tmp_path)
    path.write_text(_legacy_bytes(STATE))
    monkeypatch.setattr(rotation, "PATH", path)

    assert rotation.advance("find_osti") == "--page 9"
    assert rotation.set_next("find_kitopen", " tok-2 ") == "--token tok-2"
    want = {**STATE, "find_osti": {**STATE["find_osti"], "next": 9},
            "find_kitopen": {**STATE["find_kitopen"], "next": "tok-2"}}
    assert path.read_text() == _legacy_bytes(want)  # byte-identical to the legacy writer
    with store.FileStore(tmp_path).read() as v:
        events = v.scan(store.Table.EVENTS).rows
    assert [(e["op"], e["counts"]) for e in events] == [  # one receipt per transaction
        ("commit", {"rotation": {"upsert": 1}}), ("commit", {"rotation": {"upsert": 1}})]
    assert all(e["run_id"].startswith("rotation-") for e in events)


def test_standalone_advance_failure_writes_nothing(tmp_path, monkeypatch):
    path = _rotation_file(tmp_path)
    path.write_text(_legacy_bytes(STATE))
    monkeypatch.setattr(rotation, "PATH", path)
    with pytest.raises(KeyError):
        rotation.advance("find_unknown")
    with pytest.raises(ValueError, match="must be replaced"):
        rotation.advance("find_kitopen")
    assert path.read_text() == _legacy_bytes(STATE)
    assert not (tmp_path / "registry" / "journal").exists()


def test_standalone_advance_waits_for_a_running_round_then_fails(tmp_path, monkeypatch):
    path = _rotation_file(tmp_path)
    path.write_text(_legacy_bytes(STATE))
    monkeypatch.setattr(rotation, "PATH", path)
    monkeypatch.setattr(rotation, "LOCK_TIMEOUT", 0.2)
    with store.FileStore(tmp_path).writer(round_id="live-round"):
        with pytest.raises(RuntimeError, match="corpus-round"):
            rotation.advance("find_osti")
    assert path.read_text() == _legacy_bytes(STATE)


def test_cli_output_is_unchanged(tmp_path):
    """next/show/advance print what they always printed; the CLI runs against a copy of the
    scripts so its store root is the throwaway repository."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    scripts = Path(rotation.__file__).parent
    for name in ("rotation.py", "store.py", "store_broker.py", "registry.py", "blocklist.py",
                 "ops.py"):
        (repo / "scripts" / name).write_bytes((scripts / name).read_bytes())
    path = _rotation_file(repo)
    path.write_text(_legacy_bytes(STATE))
    env = {**os.environ, "PYTHONPATH": str(repo / "scripts")}

    def cli(*args):
        return subprocess.run([sys.executable, str(repo / "scripts" / "rotation.py"), *args],
                              capture_output=True, text=True, env=env, cwd=repo)

    assert cli("next", "find_osti").stdout == "--page 7\n"
    assert cli("advance", "find_osti").stdout == "--page 9\n"
    shown = cli("show")
    assert shown.stdout == json.dumps(json.loads(path.read_text()), indent=2) + "\n"
    unknown = cli("advance", "find_nope")
    assert unknown.returncode == 1 and "unknown finder 'find_nope'" in unknown.stderr
    assert cli("bogus").returncode == 2
