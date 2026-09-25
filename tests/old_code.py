"""Modules exactly as an earlier commit shipped them, for migration tests: a schema written by the
old code must migrate. Tools that act on a schema before its migration (pg_shadow's import,
sync, digests) must be the OLD version too — the current one writes and reads the current
derived columns (schema v8 added `pids`)."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def load(commit: str, name: str, **bind):
    """scripts/<name>.py at `commit`, as a fresh module; `bind` replaces module globals after
    it ran (e.g. store_pg=<the old store_pg>, so an old pg_shadow drives the old store)."""
    got = subprocess.run(["git", "-C", str(REPO), "show", f"{commit}:scripts/{name}.py"],
                         capture_output=True)
    if got.returncode:
        pytest.skip(f"{commit} not in this clone")
    spec = importlib.util.spec_from_loader(f"{name}_{commit}", loader=None)
    mod = importlib.util.module_from_spec(spec)
    # it believes it lives where the current file does (ROOT = parents[1] etc.)
    mod.__file__ = str(REPO / "scripts" / f"{name}.py")
    sys.modules[spec.name] = mod   # its dataclasses resolve their module there
    try:
        exec(compile(got.stdout, f"{name}_{commit}.py", "exec"), mod.__dict__)  # noqa: S102
    finally:
        del sys.modules[spec.name]
    for key, value in bind.items():
        setattr(mod, key, value)
    return mod
