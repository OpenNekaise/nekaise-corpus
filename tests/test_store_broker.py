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
            env = dict(os.environ, **broker.env(),
                       **{store.INHERITED_LOCK_ENV: f"{os.getpid()}:rnd1"})
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
