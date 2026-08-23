import json

import yaml

import find_github


def test_blocklisted_repo_counts_as_durably_done(tmp_path, monkeypatch):
    registry_dir = tmp_path / "registry"
    manifest_dir = tmp_path / "manifest"
    registry_dir.mkdir()
    manifest_dir.mkdir()
    monkeypatch.setattr(find_github.registry, "REG_DIR", registry_dir)
    monkeypatch.setattr(find_github.registry, "MAN_DIR", manifest_dir)
    monkeypatch.setattr(find_github.blocklist, "load", lambda: {
        "https://raw.githubusercontent.com/example/pruned/main/docs/guide.md",
        "https://raw.githubusercontent.com/example/pruned/main/src/model.py",
    })

    done, code_done = find_github.done_sources()

    assert "gh_pruned" in done
    assert "gh_pruned" in code_done


def test_done_sources_reads_only_github_shards(tmp_path, monkeypatch):
    registry_dir = tmp_path / "registry"
    manifest_dir = tmp_path / "manifest"
    registry_dir.mkdir()
    manifest_dir.mkdir()
    (manifest_dir / "github.jsonl").write_text(json.dumps({
        "source": "gh_manifested", "format": "md",
    }) + "\n")
    (registry_dir / "github.yaml").write_text(yaml.safe_dump({"sources": [{
        "source": "gh_registered", "format": "txt",
    }]}))
    (manifest_dir / "patents-us.jsonl").write_text("not json and must not be read\n")
    monkeypatch.setattr(find_github.registry, "REG_DIR", registry_dir)
    monkeypatch.setattr(find_github.registry, "MAN_DIR", manifest_dir)
    monkeypatch.setattr(find_github.blocklist, "load", set)

    done, code_done = find_github.done_sources()

    assert done == {"gh_manifested", "gh_registered"}
    assert code_done == {"gh_registered"}


def test_code_files_receive_reserved_cap_capacity(monkeypatch):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    tree = [
        {"type": "blob", "path": f"doc/{index:03}.md"}
        for index in range(100)
    ] + [
        {"type": "blob", "path": f"pyfem/{index:03}.py"}
        for index in range(100)
    ]

    def get(url, **_kwargs):
        if "/git/trees/" in url:
            return Response({"tree": tree})
        return Response({"default_branch": "main"})

    monkeypatch.setattr(find_github.requests, "get", get)
    entries = find_github.from_repo({
        "repo": "jjcremmers/PyFEM",
        "license": "open",
        "topic": "structures_civil",
        "include": ["pyfem/", "doc/", "README"],
        "code": ["py"],
        "cap": 80,
    })

    assert len(entries) == 80
    assert sum(entry["format"] == "md" for entry in entries) == 40
    assert sum(entry["format"] == "txt" for entry in entries) == 40


def test_completed_code_repo_drops_out_of_routine_walk():
    repos = [
        {"repo": "example/docs", "topic": "construction"},
        {"repo": "example/code", "topic": "structures_civil", "code": ["py"]},
        {"repo": "example/pending", "topic": "urban"},
    ]

    pending = find_github.pending_repos(
        repos,
        done={"gh_docs", "gh_code"},
        code_done={"gh_code"},
    )

    assert [spec["repo"] for spec in pending] == ["example/pending"]
