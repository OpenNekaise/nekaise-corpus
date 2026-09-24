"""A throwaway repository for the pipeline steps (loader, pruner, cleaner): files written exactly as
the legacy writers leave them, and the helpers that point the step modules at it."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import registry


def entry_of(row: dict) -> dict:
    """The registry entry a manifest row came from."""
    return {k: row[k] for k in registry.FIELDS if row.get(k) not in (None, "")}


COMMITTED_HOST_POLICY = Path(__file__).resolve().parents[1] / "registry" / "host_policy.json"


def write_policy(root: Path, policy: dict | None) -> None:
    """registry/host_policy.json: `policy` ({host: rule}) or, for None, the committed one."""
    path = Path(root) / "registry" / "host_policy.json"
    if policy is None:
        data = COMMITTED_HOST_POLICY.read_bytes()
    else:
        hosts = {h: {"status": "suspended", "reason": "test", "decided_at": "2026-09-24", **rule}
                 for h, rule in policy.items()}
        data = (json.dumps({"version": 1, "hosts": hosts}, indent=2) + "\n").encode()
    if not path.exists() or path.read_bytes() != data:  # idempotent: same bytes, same version
        path.write_bytes(data)


def restriction(match: dict) -> dict:
    """A schema-valid eligibility restriction."""
    return {"status": "restricted", "match": match, "backends": ["find_x"], "reason": "test",
            "decided_at": "2026-09-01", "evidence_urls": ["https://e.org/why"]}


def write_repo(root: Path, *, entries=(), manifest=(), blocklist=(), ledger=(),
               restrictions: dict | None = None, policy: dict | None = None) -> Path:
    """A repository with configuration (eligibility `restrictions`, host `policy`; None = the
    committed host policy) and the tracked files as the legacy writers leave them."""
    root = Path(root)
    reg = root / "registry"
    reg.mkdir(parents=True, exist_ok=True)
    (reg / "backends.json").write_text(json.dumps({"_readme": "test"}, indent=2) + "\n")
    (reg / "eligibility.json").write_text(json.dumps(
        {"version": 1, "restrictions": restrictions or {}}, indent=2) + "\n")
    write_policy(root, policy)
    shards: dict[str, list] = {}
    for e in entries:
        shards.setdefault(registry.shard_filename(e["id"]), []).append(e)
    for name, group in shards.items():
        head = ("# hand-curated\nsources:\n" if name == registry.CURATED
                else registry.shard_header(Path(name).stem))
        (reg / name).write_text(head + "".join(registry.emit_entry(e) for e in group))
    man = root / "manifest"
    man.mkdir(exist_ok=True)
    groups: dict[str, list] = {}
    for r in manifest:
        groups.setdefault(registry.manifest_shard(r["id"]), []).append(r)
    for stem, group in groups.items():
        (man / f"{stem}.jsonl").write_text(registry.manifest_shard_text(group))
    (root / "pruned_urls.txt").write_text("".join(f"{u}\n" for u in blocklist))
    buckets: dict[str, list] = {}
    for row in ledger:
        buckets.setdefault(registry.prune_ledger_name(row["id"]), []).append(row)
    for name, rows in buckets.items():
        (reg / name).write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                                        for r in rows))
    return root


class InlineProcessPool(ThreadPoolExecutor):
    """ProcessPoolExecutor stand-in: same interface, runs in threads, so monkeypatched module
    state (paths, clocks) reaches the workers."""

    def __init__(self, max_workers=None, mp_context=None, **_kw):
        super().__init__(max_workers=1)


def point(monkeypatch, root: Path, *, policy: dict | None = None) -> None:
    """Point every pipeline module (and the registry/blocklist/host-policy paths) at `root`;
    `policy` ({host: rule}) rewrites root's registry/host_policy.json (the steps read the pinned
    copy through their store view). With monkeypatch None the attributes are set for good (a
    child process driving a step, see CHILD)."""
    import os

    import blocklist
    import build_corpus
    import clean_corpus
    import host_policy
    import ops
    import prune_corpus

    def put(obj, name, value):
        if monkeypatch is None:
            setattr(obj, name, value)
        else:
            monkeypatch.setattr(obj, name, value)

    root = Path(root)
    for mod in (build_corpus, prune_corpus, clean_corpus):
        put(mod, "HERE", root)
    put(build_corpus, "RAW", root / "raw")
    put(build_corpus, "TEXT", root / "text")
    put(clean_corpus, "TEXT", root / "text")
    put(clean_corpus, "CORPUS", root / "corpus")
    put(clean_corpus, "STAMP", root / "corpus" / ".ruleset")
    put(clean_corpus, "POLICY_QUARANTINE", root / "workspace" / "policy-excluded-corpus")
    put(registry, "ROOT", root)
    put(registry, "REG_DIR", root / "registry")
    put(registry, "MAN_DIR", root / "manifest")
    put(blocklist, "PATH", root / "pruned_urls.txt")
    put(ops, "WORKSPACE", root / "workspace")
    put(host_policy, "PATH", root / "registry" / "host_policy.json")
    if policy is not None:
        write_policy(root, policy)
    elif not host_policy.PATH.exists():
        write_policy(root, None)
    put(build_corpus, "ProcessPoolExecutor", InlineProcessPool)
    put(clean_corpus, "ProcessPoolExecutor", InlineProcessPool)
    if monkeypatch is None:
        os.environ["NEKAISE_DISABLE_INDEX"] = "1"
    else:
        monkeypatch.setenv("NEKAISE_DISABLE_INDEX", "1")


# A child process running one pipeline step against a throwaway repository (argv: root, policy
# JSON or "", then the step's module and its arguments), as run_round runs it: its environment
# carries the round's inherited lock entry and broker.
CHILD = """
import json, sys
sys.path[:0] = ["scripts", "tests"]
import pipeline_repo
root, policy, module, *args = sys.argv[1:]
pipeline_repo.point(None, root, policy=json.loads(policy) if policy else None)
mod = __import__(module)
sys.argv = [module + ".py", *args]
mod.main()
"""


def tracked(root: Path, *, journal: bool = False) -> dict[str, bytes]:
    """The tracked metadata files (registry, manifest, blocklist), optionally with the journal."""
    root = Path(root)
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if not p.is_file() or rel.parts[0] not in ("registry", "manifest", "pruned_urls.txt"):
            continue
        if not journal and rel.parts[:2] == ("registry", "journal"):
            continue
        out[str(rel)] = p.read_bytes()
    return out


def artifacts(root: Path) -> dict[str, bytes]:
    """raw/, text/, corpus/ bytes (the corpus stamp included)."""
    root = Path(root)
    return {str(p.relative_to(root)): p.read_bytes()
            for d in ("raw", "text", "corpus") if (root / d).exists()
            for p in sorted((root / d).rglob("*")) if p.is_file()}


def manifest_rows(root: Path) -> dict[str, dict]:
    rows = {}
    for p in sorted((Path(root) / "manifest").glob("*.jsonl")):
        for line in p.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                rows[row["id"]] = row
    return rows


def entry_ids(root: Path) -> set[str]:
    ids = set()
    for p in (Path(root) / "registry").glob("*.yaml"):
        ids |= {e["id"] for e in registry.parse_yaml(p.read_text()).get("sources") or []}
    return ids


def journal_runs(root: Path) -> list[str]:
    """Run ids of the committed store transactions, in commit order."""
    runs = []
    for p in sorted((Path(root) / "registry" / "journal").glob("*.jsonl")):
        for line in p.read_text().splitlines():
            event = json.loads(line)
            if event.get("op") == "commit":
                runs.append((event["seq"], event["run_id"]))
    return [r for _, r in sorted(runs)]
