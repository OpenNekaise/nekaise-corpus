"""Architecture contract (ADR 0001 stage 3, step 7): production code reaches tracked state only
through the store.

Tracked state = registry/*.yaml shards, manifest/*.jsonl, the prune ledger, pruned_urls.txt, the
runtime state documents (rotation.json, backend_state.json, control documents) and the journal.
No scripts/*.py outside ALLOWLIST may spell those paths in code (path joins, path/file/glob calls,
path constants), glob the tracked directories, use a store's private layout attributes, or call
registry.py's deprecated list/set adapters. Git-owned configuration (backends/eligibility/
vendors/host_policy JSON) is policy, not state: its loaders locate it with store.config_path.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

# Kept deliberately small; every entry must still be needed (test_allowlist_is_minimal).
ALLOWLIST: dict[str, set[str] | str] = {
    # The FileStore owns the tracked layout: the only module that opens, globs and writes it.
    "store.py": "*",
    # Git-shadow tooling: imports a commit and replays first-parent commits from git objects of
    # the tracked paths into PostgreSQL, and verifies both exports (ADR 0001 stage 2).
    "pg_shadow.py": "*",
    # The FileStore's rebuildable SQLite membership index, built from the tracked files it
    # accelerates (store.ReadView._known_indexed is its caller); removed with the file store.
    "corpus_index.py": "*",
    # Physical validators of the published files themselves (per-file size gate before git
    # publication; the prune ledger's on-disk shard layout), which no logical store read sees.
    "check_contracts.py": {"oversized_control_files", "prune_ledger_contract_errors"},
}

# Names that are tracked state paths wherever they appear in code positions.
STATE_NAMES = {"registry", "manifest", "journal", "pruned_urls.txt", "pruned.jsonl",
               "rotation.json", "backend_state.json"}
STATE_PREFIXES = ("registry/", "manifest/")
STATE_GLOBS = ("*.yaml", "*.jsonl", "pruned-*.jsonl")
PATH_CALLS = {"Path", "PurePath", "open", "glob", "rglob", "iglob", "joinpath", "join",
              "exists", "is_file", "is_dir", "copytree", "copy", "copy2", "rmtree", "unlink"}
# registry.py's deprecated list/set adapters and retired path helpers; blocklist/rotation paths.
LEGACY_ATTRS = {
    ("registry", name) for name in (
        "load_entries", "load_manifest_rows", "write_manifest_rows", "existing_keys",
        "remove_ids", "load_prune_ledger_rows", "write_prune_ledger_rows", "REG_DIR", "MAN_DIR",
        "shard_path", "shard_files", "manifest_files", "prune_ledger_path", "prune_ledger_files")
} | {("blocklist", "PATH"), ("rotation", "PATH"), ("find_github", "PASSES")}
# FileStore's private layout attributes (st.reg, st.man, …): the store's business only.
STORE_LAYOUT_ATTRS = {"reg", "man", "blocklist_path", "rotation_path", "backend_state_path",
                      "journal_dir", "txn_dir"}


def _is_state_string(value: str) -> bool:
    return value in STATE_NAMES or value.startswith(STATE_PREFIXES)


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def violations(path: Path) -> list[tuple[str, int, str]]:
    """(enclosing top-level function or "<module>", line, what) for every forbidden access."""
    tree = ast.parse(path.read_text())
    found: list[tuple[str, int, str]] = []
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                    ast.ClassDef))
                  and n.body and isinstance(n.body[0], ast.Expr)
                  and isinstance(n.body[0].value, ast.Constant)}

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if scope == "<module>" and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                          ast.ClassDef)):
                inner = child.name
            check(child, inner)
            visit(child, inner)

    def const_strings(node: ast.AST):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            yield node
        elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            for elt in node.elts:
                yield from const_strings(elt)

    def check(node: ast.AST, scope: str) -> None:
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            for side in (node.left, node.right):
                for c in const_strings(side):
                    if _is_state_string(c.value):
                        found.append((scope, line, f"path join with {c.value!r}"))
        elif isinstance(node, ast.Call) and _call_name(node.func) in PATH_CALLS:
            for arg in node.args:
                for c in const_strings(arg):
                    if _is_state_string(c.value) or c.value in STATE_GLOBS:
                        found.append((scope, line,
                                      f"{_call_name(node.func)}() with {c.value!r}"))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            for c in const_strings(node.value):
                if _is_state_string(c.value):
                    found.append((scope, line, f"path constant {c.value!r}"))
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and (node.value.id, node.attr) in LEGACY_ATTRS:
                found.append((scope, line, f"{node.value.id}.{node.attr}"))
            elif node.attr in STORE_LAYOUT_ATTRS:
                found.append((scope, line, f"store layout attribute .{node.attr}"))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + (
                [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            if "legacy_registry" in names:
                found.append((scope, line, "imports the test-only legacy_registry"))

    visit(tree, "<module>")
    return found


def _allowed(module: str, scope: str) -> bool:
    rule = ALLOWLIST.get(module)
    return rule == "*" or (isinstance(rule, set) and scope in rule)


PRODUCTION = sorted(p.name for p in SCRIPTS.glob("*.py"))


@pytest.mark.parametrize("module", PRODUCTION)
def test_only_the_store_touches_tracked_state(module):
    bad = [f"{module}:{line} in {scope}: {what}"
           for scope, line, what in violations(SCRIPTS / module)
           if not _allowed(module, scope)]
    assert not bad, ("direct access to tracked state outside the store (use scripts/store.py; "
                     "see tests/test_architecture.py):\n  " + "\n  ".join(bad))


def test_allowlist_is_minimal():
    """Every allowlisted module (and function) still needs its exemption."""
    for module, rule in ALLOWLIST.items():
        scopes = {scope for scope, _, _ in violations(SCRIPTS / module)}
        if rule == "*":
            assert scopes, f"{module} no longer needs its allowlist entry"
        else:
            assert rule <= scopes, f"{module}: {sorted(rule - scopes)} no longer need exemption"


def test_the_detector_sees_every_kind_of_access(tmp_path):
    """The detector itself (so a refactor cannot blind it)."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        'import json, registry, blocklist\n'
        'from pathlib import Path\n'
        'ROOT = Path(".")\n'
        'A = ROOT / "registry" / "x.yaml"\n'
        'B = ("README.md", "manifest")\n'
        'def f(st):\n'
        '    """registry/ in a docstring is fine"""\n'
        '    open("manifest/a.jsonl")\n'
        '    ROOT.glob("*.yaml")\n'
        '    registry.load_manifest_rows()\n'
        '    blocklist.PATH\n'
        '    st.reg\n'
        '    print(f"registry/{A}: message")\n'
        '    return json.loads(Path("pruned_urls.txt").read_text())\n')
    kinds = sorted(what for _, _, what in violations(probe))
    assert kinds == sorted([
        "path join with 'registry'", "path constant 'manifest'", "open() with 'manifest/a.jsonl'",
        "glob() with '*.yaml'", "registry.load_manifest_rows", "blocklist.PATH",
        "store layout attribute .reg", "Path() with 'pruned_urls.txt'"])


def test_registry_adapters_are_deprecated_store_adapters(tmp_path, monkeypatch):
    """The retained list/set API goes through the store: write_manifest_rows keeps REPLACEMENT
    semantics (rows left out are deleted, with a tombstone carrying the reason), and every
    adapter is one journaled transaction."""
    import registry
    import store
    from pipeline_repo import write_repo

    root = write_repo(tmp_path / "r", manifest=[
        {"id": "ost-a", "title": "A", "url": "https://e.org/a", "topic": "t", "status": "ok"},
        {"id": "ost-b", "title": "B", "url": "https://e.org/b", "topic": "t", "status": "ok"}])
    monkeypatch.setattr(registry, "ROOT", root)
    with pytest.warns(DeprecationWarning):
        registry.write_manifest_rows(
            [{"id": "ost-a", "title": "A2", "url": "https://e.org/a", "topic": "t",
              "status": "ok"}], reason="test: replace")
    with pytest.warns(DeprecationWarning):
        rows = registry.load_manifest_rows()
    assert [(r["id"], r["title"]) for r in rows] == [("ost-a", "A2")]
    with store.FileStore(root).read() as view:
        events = view.scan(store.Table.EVENTS).rows
    tomb = [e for e in events if e["op"] == "delete"]
    assert [(e["id"], e["reason"]) for e in tomb] == [("ost-b", "test: replace")]
    assert [e["run_id"].split("-")[0] for e in events if e["op"] == "commit"] == ["manifest"]
