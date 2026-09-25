"""A throwaway PostgreSQL-authoritative repository for end-to-end staged runs (ADR 0001 stage 4
step 4): this branch's scripts copied into a git checkout, a schema of the test database seeded
and switched to PostgreSQL authority for that root in a private host record, a local HTTP server
for payloads, and a fake finder. Real `run_round.py` processes (and every child they start) see
the private record through tests/authority_site (sitecustomize)."""
from __future__ import annotations

import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import store
import store_authority
from pipeline_repo import write_repo

REPO = Path(__file__).resolve().parents[1]
SITE = REPO / "tests" / "authority_site"
DSN = os.environ.get("NEKAISE_PG_TEST_DSN", "")

# The fake finder: proposes the entries of workspace/fake-finder.json that the store (the round's
# pinned view) does not know yet, records what it saw, and may sleep first.
FINDER = '''#!/usr/bin/env python3
"""Test finder (tests/staged_world.py): proposes workspace/fake-finder.json's entries."""
import json
import os
import sys
import time
from pathlib import Path

import dedup
import registry
import store

ROOT = Path(__file__).resolve().parents[1]
spec_path = ROOT / "workspace" / "fake-finder.json"
spec = json.loads(spec_path.read_text()) if spec_path.exists() else {}
with open(ROOT / "workspace" / "fake-finder.runs", "a") as f:
    with store.open(root=ROOT).read() as view:
        f.write(json.dumps({"run": os.environ.get("NEKAISE_RUN_ID"),
                            "version": view.version().token}) + "\\n")
time.sleep(spec.get("sleep", 0))
keys = dedup.open_keys(root=ROOT)
new = [e for e in spec.get("entries", []) if e["url"].rstrip("/") not in keys.urls]
registry.append_entries(new)
print(f"# fake finder: {len(new)} new")
'''

TRIVIAL_TEST = "def test_ok():\n    assert True\n"
GITIGNORE = "workspace/\nlogs/\nraw/\ntext/\ncorpus/\nartifacts/\n__pycache__/\n.pytest_cache/\n"
TEXT = ("Building energy simulation of HVAC systems, thermal comfort, ventilation, insulation "
        "and heat pump performance in residential and commercial buildings. ") * 40


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


class Payloads:
    """A local HTTP server: GET /<id>.md returns the document's body; ids in `hold` wait until
    released (a fetch that is still downloading)."""

    def __init__(self):
        self.bodies: dict[str, bytes] = {}
        self.hold: dict[str, threading.Event] = {}
        self.waiting: set[str] = set()
        world = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                sid = self.path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                if sid in world.hold:
                    world.waiting.add(sid)
                    world.hold[sid].wait(120)
                body = world.bodies.get(sid)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, sid: str) -> str:
        return f"http://127.0.0.1:{self.port}/{sid}.md"

    def release(self) -> None:
        for event in self.hold.values():
            event.set()

    def close(self) -> None:
        self.release()
        self.server.shutdown()
        self.server.server_close()


def entry(payloads: Payloads, sid: str, **kw) -> dict:
    payloads.bodies.setdefault(sid, f"{sid}: {TEXT}\n".encode())
    return {"id": sid, "title": f"Doc {sid}", "url": payloads.url(sid), "source": "osti",
            "license": "public-domain", "topic": "building_energy", "format": "md", **kw}


def copy_scripts(root: Path) -> None:
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for p in (REPO / "scripts").iterdir():
        if p.name == "__pycache__":
            continue
        if p.name.startswith("find_") and p.name not in ("find_vendor.py", "find_patents.py"):
            continue   # finders the test configuration does not name would fail contracts
        if p.is_dir():
            shutil.copytree(p, scripts / p.name)
        else:
            shutil.copy2(p, scripts / p.name)
    (scripts / "find_fake.py").write_text(FINDER)


BACKENDS = {
    "_readme": "test configuration",
    "find_fake": {"script": "find_fake.py", "args": [], "enabled": True, "rotation": False},
    "find_vendor": {"script": "find_vendor.py", "args": [], "enabled": False,
                    "reason": "test: not used", "rotation": False},
    "find_patents": {"script": "find_patents.py", "args": [], "enabled": False,
                     "reason": "test: not used", "rotation": False},
}


class World:
    """One PostgreSQL-authoritative checkout. `env` is what a real operator's shell would carry
    (the authority settings) plus the test hooks (tests/authority_site)."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.root = tmp_path / "repo"
        self.payloads = Payloads()
        self.record = store_authority.HOST_RECORD   # conftest's private record for this test
        self.schema = f"sr_{uuid.uuid4().hex[:12]}"
        self.pg = None
        self.procs: list[subprocess.Popen] = []

    # -- building ------------------------------------------------------------------------------

    def build(self, entries: list[dict], *, patches: dict | None = None) -> "World":
        import store_pg
        root = self.root
        write_repo(root, entries=entries, manifest=[], policy={})
        (root / "registry" / "backends.json").write_text(json.dumps(BACKENDS, indent=2) + "\n")
        (root / "registry" / "rotation.json").write_text("{}\n")
        shutil.copy2(REPO / "registry" / "vendors.json", root / "registry" / "vendors.json")
        copy_scripts(root)
        (root / "tests").mkdir()
        (root / "tests" / "test_ok.py").write_text(TRIVIAL_TEST)
        (root / "README.md").write_text("test checkout\n")
        (root / ".gitignore").write_text(GITIGNORE)
        (root / "workspace").mkdir(exist_ok=True)
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "t@example.test")
        git(root, "config", "user.name", "T")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "initial")
        self.pg = st = store_pg.PgStore(root, dsn=DSN, schema=self.schema)
        st.pin_config_from_files()
        with store.FileStore(root).read() as v:
            rows = v.scan(store.Table.ENTRIES, limit=store.MAX_PAGE).rows
        with st.writer() as w:
            with st.transaction("seed", expected_version=st.version(), writer=w) as tx:
                if rows:
                    tx.insert_entries(rows)
        epoch = st.set_authority("postgres", root=root, reason="test cutover")
        store_authority.write_record(root, "postgres", reason="test cutover", epoch=epoch,
                                     dataset_uuid=st.authority()["dataset_uuid"], dsn=DSN,
                                     schema=self.schema)
        self.patches = patches or {}
        return self

    def store(self):
        """The authoritative store, bound to this root's authority record exactly as store.open()
        builds it under the operator's environment (self.env)."""
        import store_pg
        return store_pg.PgStore(self.root, dsn=DSN, schema=self.schema, create=False,
                                authority=store_authority.record_for(self.root))

    @property
    def env(self) -> dict:
        base = {k: v for k, v in os.environ.items() if not k.startswith("NEKAISE_")}
        return {**base, "PYTHONPATH": str(SITE), "NEKAISE_TEST_AUTHORITY_RECORD": str(self.record),
                "NEKAISE_STORE": "postgres", "NEKAISE_PG_DSN": DSN,
                "NEKAISE_PG_SCHEMA": self.schema, "NEKAISE_DISABLE_INDEX": "1",
                "NEKAISE_TEST_PATCHES": json.dumps(self.patches), "PYTHONUNBUFFERED": "1"}

    def finder(self, entries: list[dict], **kw) -> None:
        (self.root / "workspace" / "fake-finder.json").write_text(
            json.dumps({"entries": entries, **kw}))

    def finder_runs(self) -> list[dict]:
        path = self.root / "workspace" / "fake-finder.runs"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    # -- running -------------------------------------------------------------------------------

    def run(self, *args: str, env: dict | None = None, timeout: float = 300,
            script: str = "run_round.py") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.root / "scripts" / script), *args],
                              cwd=self.root, env={**self.env, **(env or {})},
                              capture_output=True, text=True, timeout=timeout)

    def python(self, code: str, *, env: dict | None = None,
               timeout: float = 300) -> subprocess.CompletedProcess:
        """Python `code` in a child process of this checkout (its scripts first on the path)."""
        prelude = f"import sys\nsys.path.insert(0, {str(self.root / 'scripts')!r})\n"
        return subprocess.run([sys.executable, "-c", prelude + code], cwd=self.root,
                              env={**self.env, **(env or {})}, capture_output=True, text=True,
                              timeout=timeout)

    def start(self, *args: str, env: dict | None = None,
              script: str = "run_round.py") -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, str(self.root / "scripts" / script), *args],
                                cwd=self.root, env={**self.env, **(env or {})},
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs.append(proc)
        return proc

    def events(self, run_id: str | None = None) -> list[dict]:
        path = self.root / "logs" / "run_history.jsonl"
        if not path.exists():
            return []
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        return [r for r in rows if run_id is None or r.get("run_id") == run_id]

    def wait_for(self, predicate, timeout: float = 120, what: str = "condition") -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out waiting for {what}")
            time.sleep(0.1)

    def q(self, statement: str, params=()) -> list[tuple]:
        with self.pg._connect(autocommit=True) as conn:
            return conn.execute(statement, params).fetchall()

    def run_row(self, run_id: str) -> dict | None:
        rows = self.q("SELECT status, kind, promoted_generation, staged_seq, owner_epoch, "
                      "writer_epoch FROM runs WHERE run_id = %s", [run_id])
        return dict(zip(("status", "kind", "generation", "staged_seq", "owner_epoch",
                         "writer_epoch"), rows[0])) if rows else None

    def generation(self):
        return self.q("SELECT current_generation FROM dataset")[0][0]

    def commit(self, message: str, **files: str) -> str:
        for rel, text in files.items():
            path = self.root / rel.replace("__", "/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", message)
        return git(self.root, "rev-parse", "HEAD")

    def close(self) -> None:
        self.payloads.close()
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                pass
        if self.pg is not None:
            self.pg.drop()


def kill(proc: subprocess.Popen) -> None:
    """SIGKILL the process alone (its children live on as orphans, as after a crash)."""
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(10)
