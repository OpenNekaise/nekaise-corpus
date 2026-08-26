from pathlib import Path

import check_contracts


def test_oversized_control_files_enforces_headroom(tmp_path, monkeypatch):
    registry_dir = tmp_path / "registry"
    manifest_dir = tmp_path / "manifest"
    registry_dir.mkdir()
    manifest_dir.mkdir()
    limit = 10
    monkeypatch.setattr(check_contracts, "MAX_CONTROL_FILE_BYTES", limit)

    (registry_dir / "safe.yaml").write_bytes(b"x" * limit)
    oversized = manifest_dir / "too-large.jsonl"
    oversized.write_bytes(b"x" * (limit + 1))
    oversized_ledger = registry_dir / "pruned.jsonl"
    oversized_ledger.write_bytes(b"x" * (limit + 2))
    (tmp_path / "untracked.bin").write_bytes(b"x" * (limit + 1))

    assert check_contracts.oversized_control_files(tmp_path) == [
        (oversized, limit + 1),
        (oversized_ledger, limit + 2),
    ]


def test_real_control_files_have_publication_headroom():
    assert check_contracts.oversized_control_files(Path(check_contracts.ROOT)) == []


def test_policy_blocked_backends_and_manifest_rows_fail_closed():
    restrictions = {
        "translated": {
            "match": {"id_prefix": "pat-cn"},
            "backends": ["find_patents_cn"],
        },
    }
    backends = {
        "find_patents_cn": {
            "enabled": False,
            "reason": "policy-blocked pending rights review",
        },
        "find_jstage": {
            "enabled": False,
            "reason": "policy-blocked pending rights review",
        },
    }
    rows = [{"id": "pat-cn1", "corpus_path": "corpus/pat-cn1.md"}]

    errors = check_contracts.eligibility_contract_errors(rows, backends, restrictions)

    assert any("find_jstage" in error for error in errors)
    assert any("still claim corpus data" in error for error in errors)


def test_eligibility_restricted_backend_cannot_be_reenabled_silently():
    restrictions = {
        "translated": {
            "match": {"id_prefix": "pat-cn"},
            "backends": ["find_patents_cn"],
        },
    }
    backends = {"find_patents_cn": {"enabled": True, "reason": "healthy"}}

    errors = check_contracts.eligibility_contract_errors([], backends, restrictions)

    assert any("must be disabled" in error for error in errors)
    assert any("lacks policy-blocked reason" in error for error in errors)
