import json

import corpus_index


def write_sources(path, rows):
    import yaml
    path.write_text(yaml.safe_dump({"sources": rows}, sort_keys=False))


def test_index_rebuilds_when_registry_changes(tmp_path, monkeypatch):
    reg, man = tmp_path / "registry", tmp_path / "manifest"
    reg.mkdir()
    man.mkdir()
    blocked = tmp_path / "pruned_urls.txt"
    db = tmp_path / "workspace" / "index.sqlite3"
    first = {
        "id": "ost-one", "title": "First Building", "url": "https://e.org/one.pdf",
        "source": "x", "license": "open", "topic": "construction", "format": "pdf",
    }
    write_sources(reg / "reports.yaml", [first])
    (man / "reports.jsonl").write_text(json.dumps({**first, "status": "ok"}) + "\n")
    monkeypatch.setattr(corpus_index.ops, "WORKSPACE", tmp_path / "locks")

    urls, titles, ids = corpus_index.existing_keys(reg, man, blocked, db_path=db)
    assert "ost-one" in ids
    assert "https://e.org/one.pdf" in urls

    second = {**first, "id": "ost-two", "title": "Second Building",
              "url": "https://e.org/two.pdf"}
    write_sources(reg / "reports.yaml", [first, second])
    _, _, ids = corpus_index.existing_keys(reg, man, blocked, db_path=db)
    assert ids == {"ost-one", "ost-two"}


def test_index_includes_blocklisted_urls(tmp_path, monkeypatch):
    reg, man = tmp_path / "registry", tmp_path / "manifest"
    reg.mkdir()
    man.mkdir()
    write_sources(reg / "curated.yaml", [])
    blocked = tmp_path / "pruned_urls.txt"
    blocked.write_text("https://e.org/nope.pdf\n")
    monkeypatch.setattr(corpus_index.ops, "WORKSPACE", tmp_path / "locks")
    urls, _, _ = corpus_index.existing_keys(
        reg, man, blocked, db_path=tmp_path / "index.sqlite3",
    )
    assert "https://e.org/nope.pdf" in urls


def test_append_can_update_fresh_index_without_rebuild(tmp_path, monkeypatch):
    reg, man = tmp_path / "registry", tmp_path / "manifest"
    reg.mkdir()
    man.mkdir()
    write_sources(reg / "reports.yaml", [])
    blocked = tmp_path / "pruned_urls.txt"
    db = tmp_path / "index.sqlite3"
    monkeypatch.setattr(corpus_index.ops, "WORKSPACE", tmp_path / "locks")
    corpus_index.rebuild(reg, man, blocked, db)
    prior = corpus_index.source_signature(reg, man, blocked)
    entry = {
        "id": "ost-new", "title": "New Building", "url": "https://e.org/new.pdf",
        "source": "x", "license": "open", "topic": "construction", "format": "pdf",
    }
    write_sources(reg / "reports.yaml", [entry])
    assert corpus_index.record_appended_entries(reg, man, blocked, [entry], prior, db)
    _, _, ids = corpus_index.existing_keys(reg, man, blocked, db_path=db)
    assert "ost-new" in ids
