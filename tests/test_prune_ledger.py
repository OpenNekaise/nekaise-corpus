import json

import ops
import prune_corpus
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
