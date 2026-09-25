"""CI stays runnable without tracked data (ADR 0001 stage 4 step 5): the fixture data root
passes lint_registry.py --root and check_contracts.py --root exactly as CI runs them."""
import subprocess
import sys
from pathlib import Path

import ci_fixture

REPO = Path(__file__).resolve().parents[1]


def test_the_ci_fixture_passes_lint_and_contracts(tmp_path):
    root = ci_fixture.build(tmp_path / "ci-dataset")
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
