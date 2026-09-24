"""The round broker: a child process writes through the coordinator's single writer (stage 3)."""
import json
import os
import subprocess
import sys

import pytest

import store
import store_broker
from store import Table
from test_store_contract import entry, file_store, seed, write

CHILD = r"""
import json, os, sys
sys.path.insert(0, "scripts")
import store, store_broker
st = store.FileStore(sys.argv[1])
client = store_broker.client()
with st.read() as v:                         # inherited, verified read access
    version = v.version()
    before = len(v.scan(store.Table.ENTRIES, limit=100).rows)
with client.batch("fetch", sys.argv[2], expected_version=version) as tx:
    tx.insert_entries([{"id": "oer-child", "title": "From the child", "url": "https://c.org/x.pdf",
                        "source": "t", "license": "public-domain", "topic": "t", "format": "pdf"}])
    tx.blocklist_add(u for u in ["https://c.org/blocked"])
print(json.dumps({"before": before, "version": client.last_version.token}))
"""


@pytest.fixture
def round_(tmp_path):
    st = file_store(tmp_path / "repo")
    write(st, "seed", seed)
    with st.writer(round_id="rnd1") as w:
        snap = st.round_snapshots / "rnd1"   # run_round captures its snapshot after the writer
        snap.mkdir(parents=True)
        (snap / "snapshot.json").write_text("{}")
        broker = store_broker.Broker(st, w, "rnd1")
        with broker.serving():
            env = store.ops.with_holder(dict(os.environ, **broker.env()), os.getpid(),
                                        st.workspace / ".corpus-round.lock", "rnd1")
            yield st, broker, env, w
        snap.joinpath("snapshot.json").unlink()
        snap.rmdir()


def run_child(st, env, batch="b1"):
    return subprocess.run([sys.executable, "-c", CHILD, str(st.root), batch], env=env,
                          capture_output=True, text=True, cwd=store.ROOT)


def test_a_child_writes_through_the_broker(round_):
    st, broker, env, w = round_
    out = run_child(st, env)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["before"] == 4
    assert st.version().token == got["version"]
    with st.read(writer=w) as v:  # the round's own writer may read its in-flight state
        assert v.known(ids=["oer-child"]).ids == {"oer-child"}
        assert v.known(urls=["https://c.org/blocked"]).urls == {"https://c.org/blocked"}
        commits = [e for e in v.scan(Table.EVENTS, limit=1000).rows if e["op"] == "commit"]
    assert commits[-1]["run_id"] == "rnd1.fetch.b1"


def test_identical_retry_is_a_noop_and_a_stale_version_is_refused(round_):
    st, broker, env, w = round_
    assert run_child(st, env).returncode == 0
    version = st.version()
    retry = run_child(st, env)  # same step/batch: replay of a committed run
    assert retry.returncode == 0, retry.stderr
    assert st.version() == version
    client = store_broker.Client(broker.path, broker.cap, "rnd1")
    with pytest.raises(store_broker.BrokerError, match="VersionConflict"):
        client.submit("fetch", "b9", [{"call": "blocklist_add", "args": [["https://x"]],
                                       "kwargs": {}}], store.Version("stale"))


def test_capability_and_call_whitelist(round_):
    st, broker, env, w = round_
    bad = store_broker.Client(broker.path, "0" * 64, "rnd1")
    with pytest.raises(store_broker.BrokerError, match="bad capability"):
        bad.submit("fetch", "b1", [], st.version())
    good = store_broker.Client(broker.path, broker.cap, "rnd1")
    with pytest.raises(store_broker.BrokerError, match="not a store mutation"):
        good.submit("fetch", "b1", [{"call": "_commit"}], st.version())
    with pytest.raises(store_broker.BrokerError, match="plain names"):
        good.submit("../x", "b1", [], st.version())
    with pytest.raises(AttributeError):
        with good.batch("fetch", "b2", expected_version=st.version()) as tx:
            tx.export("/tmp")


def test_an_unrelated_round_snapshot_is_still_refused(round_):
    st, broker, env, w = round_
    other = st.round_snapshots / "someone-else"
    other.mkdir()
    (other / "snapshot.json").write_text("{}")
    out = run_child(st, env)
    assert out.returncode != 0 and "someone-else" in out.stderr


def test_no_broker_outside_a_round(monkeypatch):
    for k in (store_broker.BROKER_ENV, store_broker.CAP_ENV, store_broker.ROUND_ENV):
        monkeypatch.delenv(k, raising=False)
    assert store_broker.client() is None


def test_broker_is_gone_after_the_round(tmp_path):
    st = file_store(tmp_path / "repo")
    write(st, "seed", seed)
    with st.writer(round_id="rnd2") as w:
        broker = store_broker.Broker(st, w, "rnd2")
        with broker.serving():
            path, cap = broker.path, broker.cap
    with pytest.raises(store_broker.BrokerError, match="unavailable"):
        store_broker.Client(str(path), cap, "rnd2").submit("fetch", "b1", [], st.version())


def test_positional_and_keyword_dataclass_arguments_cross_the_broker(round_):
    st, broker, env, w = round_
    client = store_broker.Client(broker.path, broker.cap, "rnd1")
    with client.batch("fetch", "pos", expected_version=st.version()) as tx:
        tx.backend_state_set("find_books", store.BackendState(False, "exhausted: p"))
    with client.batch("fetch", "kw", expected_version=client.last_version) as tx:
        tx.backend_state_set(name="find_paused", value=store.BackendState(True, None))
    with st.read(writer=w) as v:
        assert v.backend_state_get("find_books") == store.BackendState(False, "exhausted: p")


def test_a_stalled_client_cannot_block_shutdown(tmp_path):
    import socket as _socket
    import time
    st = file_store(tmp_path / "repo")
    write(st, "seed", seed)
    with st.writer(round_id="rnd3") as w:
        broker = store_broker.Broker(st, w, "rnd3")
        with broker.serving():
            s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            s.connect(str(broker.path))
            s.sendall(b'{"cap": "partial')  # never finishes its line
            time.sleep(0.2)
            started = time.monotonic()
        assert time.monotonic() - started < 5
        s.close()


def test_gate_environment_for_pytest_is_clean(tmp_path, monkeypatch):
    # the round passes the plain environment to the pytest gate; the store tests themselves open
    # read views that must not inherit the round's lock
    monkeypatch.delenv(store.INHERITED_LOCK_ENV, raising=False)
    st = file_store(tmp_path / "repo")
    write(st, "seed", seed)
    with st.read() as v:
        assert v.version()


def test_no_transaction_starts_once_shutdown_has_begun(round_):
    st, broker, env, w = round_
    broker._closing = True  # a handler that passed the early check, then resumed late
    with pytest.raises(store_broker.BrokerError, match="shutting down"):
        broker._execute({"cap": broker.cap, "step": "fetch", "batch": "late", "requests": [],
                         "expected_version": st.version().token})
    broker._closing = False
