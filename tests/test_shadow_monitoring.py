"""Shadow configuration coverage and durable daily verdicts, without a database."""
import hashlib
import json

import pytest

import ops_health
import pg_shadow
import store


@pytest.mark.parametrize("name", store.CONFIG_FILES)
def test_every_declared_configuration_is_replicated(name):
    assert pg_shadow.kind_of(f"registry/{name}") == "config"


def test_nested_configuration_digest_uses_its_full_relative_name(monkeypatch):
    name = "domains/modelica.json"
    monkeypatch.setattr(pg_shadow, "tracked_paths", lambda *a: [f"registry/{name}"])
    monkeypatch.setattr(pg_shadow, "git_bytes", lambda *a: b'{"version": 1}')
    result = pg_shadow.git_digests("commit")
    assert result["config"] == store._digest({name: hashlib.sha256(b'{"version": 1}').hexdigest()})
    assert pg_shadow.kind_of("registry/unrecognized/backends.json") is None
    assert pg_shadow.kind_of("registry/domains/unrecognized.json") is None


@pytest.mark.parametrize("matches", [True, False])
def test_daily_verdict_reaches_health(tmp_path, monkeypatch, matches):
    watermark = "a" * 40
    monkeypatch.setattr(pg_shadow, "pg_digests", lambda _: (watermark, {"config": "a"}))
    monkeypatch.setattr(pg_shadow, "git_digests", lambda *a: {"config": "a" if matches else "b"})
    assert pg_shadow.verify_and_record(None, tmp_path, log=lambda *_: None) is matches
    path = tmp_path / "workspace/pg-shadow-verify.json"
    receipt = json.loads(path.read_text())
    assert receipt["watermark"] == watermark
    assert receipt["mismatches"] == ([] if matches else ["config"])
    check = ops_health.shadow_check(receipt["checked_at"], path)
    assert check["severity"] == ("ok" if matches else "critical")


def test_failed_evaluation_replaces_a_previous_pass(tmp_path, monkeypatch):
    path = tmp_path / "workspace/pg-shadow-verify.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"ok": True}))

    def fail(_):
        raise RuntimeError("connection unavailable")

    monkeypatch.setattr(pg_shadow, "pg_digests", fail)
    with pytest.raises(RuntimeError, match="connection unavailable"):
        pg_shadow.verify_and_record(None, tmp_path)
    receipt = json.loads(path.read_text())
    assert not receipt["ok"]
    assert ops_health.shadow_check(receipt["checked_at"], path)["severity"] == "critical"


def test_missing_stale_and_invalid_shadow_receipts(tmp_path):
    path = tmp_path / "receipt.json"
    assert ops_health.shadow_check(100, path)["severity"] == "warning"
    good = {"ok": True, "checked_at": 100, "watermark": "a" * 40,
            "mismatches": [], "error": None}
    path.write_text(json.dumps(good))
    assert ops_health.shadow_check(100 + 27 * 3600, path)["severity"] == "warning"
    assert ops_health.shadow_check(100 + 51 * 3600, path)["severity"] == "critical"
    for bad in ([1], {**good, "checked_at": float("nan")},
                {**good, "checked_at": 500}, {**good, "mismatches": ["config"]},
                {**good, "watermark": None}, {**good, "ok": "true"}):
        path.write_text(json.dumps(bad))
        assert ops_health.shadow_check(100, path)["severity"] == "critical"


def test_health_includes_shadow_verdict_only_for_a_configured_file_shadow(tmp_path, monkeypatch):
    marker = tmp_path / "shadow-marker"
    marker.touch()
    monkeypatch.setattr(ops_health, "SHADOW_MARKER", marker)
    monkeypatch.setattr(ops_health, "shadow_check", lambda now: ops_health._check(
        "shadow_verification", "critical", "diverged"))
    checks = ops_health.evaluate(lifecycle_conn=object())
    assert any(c["check"] == "shadow_verification" and c["severity"] == "critical" for c in checks)
    marker.unlink()
    assert not any(c["check"] == "shadow_verification" for c in
                   ops_health.evaluate(lifecycle_conn=object()))
