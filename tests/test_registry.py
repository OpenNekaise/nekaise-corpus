"""Round-trip tests for the sharded registry I/O: prefix routing, id uniquification (the
truncation-collision bug class), and validated in-place removal (the marker-wipe bug class,
plus the blank-line-inside-a-quoted-title parser regression)."""
import json

import pytest

import blocklist
import registry


def entry(sid, url="https://example.gov/x.pdf", title=None):
    return {"id": sid, "title": title or sid, "url": url, "source": "test",
            "license": "public-domain", "topic": "building_energy", "format": "pdf"}


@pytest.fixture
def tmp_registry(tmp_path, monkeypatch):
    reg = tmp_path / "registry"
    reg.mkdir()
    (reg / registry.CURATED).write_text(
        "# hand comment that must survive\nsources:\n" + registry.emit_entry(entry("hand-one")))
    monkeypatch.setattr(registry, "REG_DIR", reg)
    monkeypatch.setattr(registry, "MANIFEST", tmp_path / "manifest.jsonl")
    monkeypatch.setattr(blocklist, "PATH", tmp_path / "pruned_urls.txt")
    return reg


def test_append_routes_by_prefix(tmp_registry):
    counts = registry.append_entries([
        entry("oer-book-a", "https://e.org/a.pdf"),
        entry("arc-old-text", "https://e.org/b.pdf"),
        entry("hand-two", "https://e.org/c.pdf"),
    ])
    assert counts == {"archive.yaml": 1, "books.yaml": 1, "curated.yaml": 1}
    assert (tmp_registry / "books.yaml").exists()
    ids = {e["id"] for e in registry.load_entries()}
    assert ids == {"hand-one", "oer-book-a", "arc-old-text", "hand-two"}
    # hand comment untouched by the curated append
    assert "# hand comment that must survive" in (tmp_registry / registry.CURATED).read_text()


def test_uniquify_vs_registry_and_batch(tmp_registry):
    registry.append_entries([entry("oer-same-slug", "https://e.org/1.pdf")])
    _, _, ids = registry.existing_keys()
    batch = [entry("oer-same-slug", "https://e.org/2.pdf"),
             entry("oer-same-slug", "https://e.org/3.pdf")]
    registry.uniquify_ids(batch, ids)
    got = [b["id"] for b in batch]
    assert got[0] != "oer-same-slug" and got[1] != "oer-same-slug"  # registry collision suffixed
    assert len(set(got)) == 2                                        # batch collision suffixed


def test_remove_ids_validated(tmp_registry):
    registry.append_entries([entry("oer-keep"), entry("oer-drop", "https://e.org/d.pdf")])
    removed = registry.remove_ids({"oer-drop", "not-present"})
    assert removed == 1
    ids = {e["id"] for e in registry.load_entries()}
    assert "oer-drop" not in ids and "oer-keep" in ids and "hand-one" in ids


def test_remove_entry_with_blank_line_in_title(tmp_registry):
    # regression: a quoted multi-line yaml scalar may contain a BLANK line (real OSTI title);
    # the block parser must treat it as part of the entry, not as the entry's end
    shard = tmp_registry / "reports.yaml"
    shard.write_text(
        "sources:\n"
        "  - id: ost-weird-title\n"
        "    title: 'Managing Things:\n"
        "\n"
        "      A Second Line After A Blank One'\n"
        "    url: https://e.org/w.pdf\n"
        "    source: test\n"
        "    license: public-domain\n"
        "    topic: building_energy\n"
        "    format: pdf\n"
        + registry.emit_entry(entry("ost-neighbor")))
    assert registry.remove_ids({"ost-weird-title"}) == 1
    ids = {e["id"] for e in registry.load_entries()}
    assert "ost-neighbor" in ids and "ost-weird-title" not in ids


def test_existing_keys_sees_manifest_and_blocklist(tmp_registry, tmp_path):
    (tmp_path / "manifest.jsonl").write_text(
        json.dumps({"id": "m-1", "title": "Manifested Doc", "url": "https://e.org/m.pdf"}) + "\n")
    blocklist.add(["https://e.org/pruned.pdf"])
    urls, titles, ids = registry.existing_keys()
    assert "https://e.org/m.pdf" in urls and "m-1" in ids
    assert "https://e.org/pruned.pdf" in urls  # blocklist folded in
    urls_nb, _, _ = registry.existing_keys(include_blocklist=False)
    assert "https://e.org/pruned.pdf" not in urls_nb
