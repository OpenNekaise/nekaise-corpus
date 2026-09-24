import json

import yaml

import find_github


def test_curated_repos_have_distinct_source_buckets():
    # Completion is keyed by repo name alone; two owners sharing a name would silently
    # suppress one another's discovery once either repository has been ingested.
    owners = {}
    for spec in find_github.REPOS:
        bucket = "gh_" + find_github.registry.slug(spec["repo"].split("/")[-1])
        assert bucket not in owners, (bucket, owners.get(bucket), spec["repo"])
        owners[bucket] = spec["repo"]


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


def test_no_curated_repo_uses_the_moved_nrel_org():
    # The NREL org moved to NatLabRockies; the API answers NREL/<repo> with 301 Moved
    # Permanently, which a walk must not depend on.
    assert not [spec["repo"] for spec in find_github.REPOS if spec["repo"].startswith("NREL/")]


def test_doc_markup_is_opt_in_bounded_by_include_and_exclude():
    docs = tuple(find_github.doc_formats(["tex", "man", "text"]))
    include, exclude = ["Manuals/", "doc/"], ["Manuals/Bibliography/", "/FIGURES/"]
    keep = [
        "Manuals/FDS_User_Guide/FDS_User_Guide.tex",
        "doc/man/man1/rpict.1",
        "doc/ray.1",
        "doc/notes/materials",
        "doc/notes/BSDFdirections.txt",
    ]
    drop = [
        "Manuals/Bibliography/BIBLIO_FDS_refs.tex",
        "Manuals/FDS_User_Guide/FIGURES/Coriolis_vector.tex",
        "Source/notes.tex",          # outside every include prefix
        "doc/man/.gitignore",
    ]
    for path in keep:
        assert find_github.wanted(path, include, (), docs, exclude), path
    for path in drop:
        assert not find_github.wanted(path, include, (), docs, exclude), path
    # without the opt-in, markup is ignored exactly as before
    assert not find_github.wanted("Manuals/FDS_User_Guide/FDS_User_Guide.tex", include)


def test_from_repo_honours_branch_override_and_maps_doc_formats(monkeypatch):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    tree = [{"type": "blob", "path": p} for p in (
        "README.md", "doc/man/man1/rpict.1", "doc/notes/materials", "doc/filefmts.md",
        "doc/ps/ray.ps", "src/rt/rpict.c",
    )]
    seen = []

    def get(url, **_kwargs):
        seen.append(url)
        if "/git/trees/" in url:
            return Response({"tree": tree})
        return Response({"default_branch": "cvsimport"})

    monkeypatch.setattr(find_github.requests, "get", get)
    entries = find_github.from_repo({
        "repo": "LBNL-ETA/Radiance", "license": "open", "topic": "building_energy",
        "branch": "master", "include": ["doc/", "README"], "docs": ["man", "text"],
    })

    assert seen[-1].endswith("/git/trees/master")
    by_path = {e["title"].split(": ", 1)[1]: e for e in entries}
    assert set(by_path) == {"README.md", "doc/man/man1/rpict.1", "doc/notes/materials",
                            "doc/filefmts.md"}
    assert by_path["doc/man/man1/rpict.1"]["format"] == "troff"
    assert by_path["doc/notes/materials"]["format"] == "txt"
    assert by_path["doc/notes/materials"]["id"] == "gh-radiance-doc-notes-materials"
    assert by_path["README.md"]["format"] == "md"
    assert all("/master/" in e["url"] for e in entries)


def test_repo_walked_for_markdown_only_returns_for_its_doc_markup_pass():
    repos = [
        {"repo": "LBNL-ETA/Radiance", "docs": ["man"]},
        {"repo": "firemodels/fds", "docs": ["tex"]},
        {"repo": "example/plain"},
    ]
    formats = {"gh_radiance": {"md"}, "gh_fds": {"md", "tex"}, "gh_plain": {"md"}}

    pending = find_github.pending_repos(repos, set(formats), set(), formats)

    assert [spec["repo"] for spec in pending] == ["LBNL-ETA/Radiance"]


def test_blocklisted_markup_urls_record_their_doc_format(tmp_path, monkeypatch):
    registry_dir = tmp_path / "registry"
    manifest_dir = tmp_path / "manifest"
    registry_dir.mkdir()
    manifest_dir.mkdir()
    monkeypatch.setattr(find_github.registry, "REG_DIR", registry_dir)
    monkeypatch.setattr(find_github.registry, "MAN_DIR", manifest_dir)
    raw = "https://raw." + "githubusercontent.com"
    monkeypatch.setattr(find_github.blocklist, "load", lambda: {
        f"{raw}/firemodels/fds/master/Manuals/A/B.tex",
        f"{raw}/LBNL-ETA/Radiance/master/doc/man/man1/rpict.1",
    })

    formats = find_github.source_formats()

    assert formats == {"gh_fds": {"tex"}, "gh_radiance": {"troff"}}


def test_doc_completion_is_per_kind_not_any_format():
    # Radiance requests man pages AND plain-text notes; a recorded notes file (txt) must not
    # complete the man-page (troff) pass.
    spec = {"repo": "LBNL-ETA/Radiance", "docs": ["man", "text"]}
    formats = {"gh_radiance": {"md", "txt"}}

    assert find_github.missing_doc_kinds(spec, formats, {}) == ["man"]
    assert find_github.pending_repos([spec], set(formats), set(), formats, {}) == [spec]
    formats["gh_radiance"].add("troff")
    assert find_github.pending_repos([spec], set(formats), set(), formats, {}) == []


def test_successfully_empty_pass_is_recorded_and_not_rewalked(tmp_path):
    passes_path = tmp_path / "github_passes.json"
    spec = {"repo": "example/manuals", "docs": ["tex", "man"]}
    formats = {"gh_manuals": {"md", "tex"}}  # no man page exists in the repo at all

    assert find_github.pending_repos([spec], set(formats), set(), formats, {}) == [spec]
    find_github.record_passes([spec], "2026-09-24", passes_path)
    passes = find_github.load_passes(passes_path)

    assert passes == {"gh_manuals": {"man": "2026-09-24", "tex": "2026-09-24"}}
    assert find_github.pending_repos([spec], set(formats), set(), formats, passes) == []


def test_main_records_passes_only_for_walked_repos_with_append(tmp_path, monkeypatch):
    specs = [{"repo": "ok/walked", "docs": ["tex"], "license": "open", "topic": "urban"},
             {"repo": "bad/failed", "docs": ["tex"], "license": "open", "topic": "urban"}]
    monkeypatch.setattr(find_github, "REPOS", specs)
    monkeypatch.setattr(find_github, "PASSES", tmp_path / "passes.json")
    monkeypatch.setattr(find_github, "source_formats", dict)
    monkeypatch.setattr(find_github.registry, "existing_keys", lambda: (set(), set(), set()))
    monkeypatch.setattr(find_github.registry, "append_entries", lambda _e: {})

    def from_repo(spec):
        if spec["repo"] == "bad/failed":
            raise RuntimeError("API down")
        return []

    monkeypatch.setattr(find_github, "from_repo", from_repo)
    monkeypatch.setattr(find_github.sys, "argv", ["find_github.py"])
    find_github.main()
    assert not (tmp_path / "passes.json").exists()  # dry run records nothing

    monkeypatch.setattr(find_github.sys, "argv", ["find_github.py", "--append"])
    find_github.main()
    assert set(find_github.load_passes(tmp_path / "passes.json")) == {"gh_walked"}


def test_copyleft_repos_carry_exact_license_evidence(monkeypatch):
    spec = next(s for s in find_github.REPOS if s["repo"] == "ladybug-tools/honeybee-energy")

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def get(url, **_kwargs):
        if "/git/trees/" in url:
            return Response({"tree": [{"type": "blob", "path": "docs/index.rst"}]})
        return Response({"default_branch": "master"})

    monkeypatch.setattr(find_github.requests, "get", get)
    (entry,) = find_github.from_repo(spec)

    assert entry["license"] == "open"
    assert entry["license_evidence"].startswith("SPDX AGPL-3.0")
    assert "not permissive" in entry["license_evidence"]
    assert entry["license_url"] == "https://www.gnu.org/licenses/agpl-3.0.html"
    assert entry["rights_verified_at"] == "2026-09-24"
