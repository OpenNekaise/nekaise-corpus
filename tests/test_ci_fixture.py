"""CI stays runnable without tracked data (ADR 0001 stage 4 step 5): the fixture data root
passes lint_registry.py --root and check_contracts.py --root exactly as CI runs them."""
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

import ci_fixture
import store

REPO = Path(__file__).resolve().parents[1]


def _assert_copied_configuration(root):
    # Resolve the store's configuration inventory without opening the live data store.
    # Digests pin the exact bytes, not only parsed JSON, including nested paths.
    expected = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                for name in store.CONFIG_FILES
                if (path := store.config_path(name, REPO)).exists()}
    with store.open(root=root).read() as view:
        copied = view.config_get()
    assert set(copied.documents) == set(expected)
    assert copied.digests == expected


def test_the_ci_fixture_passes_lint_and_contracts(tmp_path):
    root = ci_fixture.build(tmp_path / "ci-dataset")
    _assert_copied_configuration(root)
    for script in ("lint_registry.py", "check_contracts.py"):
        got = subprocess.run([sys.executable, str(REPO / "scripts" / script), "--root",
                              str(root)], capture_output=True, text=True, timeout=300)
        assert got.returncode == 0, got.stdout[-3000:] + got.stderr[-3000:]
        assert got.stdout.strip().splitlines()[-1].startswith("OK")


def test_contracts_on_the_fixture_still_catch_a_violation(tmp_path):
    root = ci_fixture.build(tmp_path / "ci-dataset")
    readme = root / "README.md"
    readme.write_text(readme.read_text().replace("**Documents** | **", "**Documents** | **1"))
    got = subprocess.run([sys.executable, str(REPO / "scripts" / "check_contracts.py"),
                          "--root", str(root)], capture_output=True, text=True, timeout=300)
    assert got.returncode == 1 and "README documents=" in got.stdout


@pytest.mark.parametrize("name", [name for name in store.CONFIG_FILES if "/" in name])
def test_fixture_configuration_guard_catches_missing_nested_file(tmp_path, name):
    root = ci_fixture.build(tmp_path / "ci-dataset")
    (root / "registry" / name).unlink()
    with pytest.raises(AssertionError, match=name):
        _assert_copied_configuration(root)
