import json

import ops
import prune_corpus
import pytest
import registry


def test_prune_ledger_records_reason_and_blocklist_decision(tmp_path, monkeypatch):
    reg = tmp_path / "registry"
    reg.mkdir()
    monkeypatch.setattr(registry, "REG_DIR", reg)
    monkeypatch.setattr(ops, "WORKSPACE", tmp_path / "workspace")
    rows = [{
        "id": "ost-bad",
        "url": "https://example.org/bad.pdf",
        "title": "Bad extraction",
        "source": "osti",
        "topic": "construction",
        "license": "public-domain",
        "status": "ok",
        "quality": {"chars": 3},
    }]

    assert prune_corpus.write_prune_ledger(
        rows, {"ost-bad": "thin"}, {"https://example.org/bad.pdf"},
    ) == 1

    path = registry.prune_ledger_path("ost-bad")
    assert path.parent == reg
    got = json.loads(path.read_text())
    assert got["id"] == "ost-bad"
    assert got["reason"] == "thin"
    assert got["blocklisted"] is True


def test_legacy_prune_ledger_migrates_without_losing_or_duplicating_rows(
    tmp_path, monkeypatch,
):
    reg = tmp_path / "registry"
    reg.mkdir()
    monkeypatch.setattr(registry, "REG_DIR", reg)
    rows = [
        {"id": f"vnd-test-{i}", "url": f"https://example.org/{i}.pdf", "reason": "thin"}
        for i in range(64)
    ]
    legacy = reg / "pruned.jsonl"
    legacy.write_text("".join(json.dumps(row) + "\n" for row in rows))

    counts = registry.write_prune_ledger_rows(registry.load_prune_ledger_rows())

    assert not legacy.exists()
    assert len(counts) > 1
    assert sum(counts.values()) == len(rows)
    assert sorted(registry.load_prune_ledger_rows(), key=lambda row: row["id"]) == sorted(
        rows, key=lambda row: row["id"]
    )
    for path in registry.prune_ledger_files():
        assert all(
            registry.prune_ledger_path(row["id"]) == path
            for row in map(json.loads, path.read_text().splitlines())
        )


def test_reviewed_title_drops_fail_closed_on_stale_or_curated_ids(tmp_path):
    rows = [
        {"id": "guk-off-topic"},
        {"id": "hand-curated"},
    ]
    reviewed = tmp_path / "reviewed.txt"
    reviewed.write_text("guk-off-topic\n")

    assert prune_corpus.reviewed_title_drops(str(reviewed), rows) == {
        "guk-off-topic": "off-topic-title",
    }

    reviewed.write_text("missing-id\n")
    with pytest.raises(ValueError, match="unknown ids"):
        prune_corpus.reviewed_title_drops(str(reviewed), rows)

    reviewed.write_text("hand-curated\n")
    with pytest.raises(ValueError, match="hand-curated ids"):
        prune_corpus.reviewed_title_drops(str(reviewed), rows)


@pytest.mark.parametrize(
    ("row", "reason", "expected"),
    [
        ({"url": "https://www.mdpi.com/article.pdf", "http_status": 403}, "failed", False),
        ({"url": "https://files.mdpi.com/article.pdf", "http_status": 403}, "failed", False),
        ({"url": "https://example.org/forbidden.pdf", "http_status": 403}, "failed", True),
        ({"url": "https://example.org/busy.pdf", "http_status": 429}, "failed", False),
        ({"url": "https://example.org/slow.pdf", "error": "connection timed out"}, "failed", False),
        ({"url": "https://example.org/old.pdf", "error": "SSL certificate mismatch"}, "failed", True),
        ({"url": "https://www.mdpi.com/thin.pdf", "http_status": 403}, "thin", True),
    ],
)
def test_blocklist_policy_distinguishes_mdpi_wall_from_durable_failures(
    row, reason, expected,
):
    assert prune_corpus._blocklistable(row, reason) is expected
