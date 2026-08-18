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

    got = json.loads((reg / "pruned.jsonl").read_text())
    assert got["id"] == "ost-bad"
    assert got["reason"] == "thin"
    assert got["blocklisted"] is True


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
