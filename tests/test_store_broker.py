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


def _slow_blocklist(monkeypatch, seconds):
    """Make the broker's blocklist mutation slow; returns (started, finished) events."""
    import threading
    import time
    started, finished = threading.Event(), threading.Event()
    real = store.WriteView.blocklist_add

    def slow(self, urls):
        started.set()
        time.sleep(seconds)
        out = real(self, urls)
        finished.set()
        return out
    monkeypatch.setattr(store.WriteView, "blocklist_add", slow)
    return started, finished


def test_an_interrupt_during_drain_waits_for_the_running_transaction(tmp_path, monkeypatch):
    """Codex review P1: a SIGTERM-style KeyboardInterrupt while serving() drains must not unwind
    the owner (and release its writer) before the executing transaction finished; the socket is
    cleaned up and the interrupt is re-raised afterwards."""
    import signal
    import threading
    st = file_store(tmp_path / "repo")
    write(st, "seed", seed)
    started, finished = _slow_blocklist(monkeypatch, 1.0)

    def interrupt(signum, frame):
        raise KeyboardInterrupt("terminated")
    previous = signal.signal(signal.SIGALRM, interrupt)
    state = {}
    try:
        with st.writer(round_id="rnd-int") as w:
            broker = store_broker.Broker(st, w, "rnd-int")
            client = store_broker.Client(broker.path, broker.cap, "rnd-int")
            def submit():
                try:
                    state["result"] = client.submit("prune", "slow", [
                        {"call": "blocklist_add", "args": [["https://s.org/1"]], "kwargs": {}}],
                        st.version())
                except store_broker.BrokerError as exc:  # drain cuts connections: the reply
                    state["result"] = str(exc)           # is lost, the transaction is not
            worker = threading.Thread(target=submit)
            with pytest.raises(KeyboardInterrupt):
                with broker.serving():
                    worker.start()
                    assert started.wait(5)
                    signal.setitimer(signal.ITIMER_REAL, 0.2)  # fires while drain waits
            # still inside the writer: the transaction must be over before ownership ends
            state["finished_before_release"] = finished.is_set()
            state["socket_gone"] = not broker._dir.exists()
        worker.join(5)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert state["finished_before_release"] and state["socket_gone"]
    assert state.get("result") in ([1], "store broker closed the connection")
    assert st.blocklist_path.read_text().endswith("https://s.org/1\n")
    assert st.pending_transactions() == []


DRAIN_STEPS = ("_stop_server", "_cut_connections", "_wait_idle", "_cleanup")


@pytest.mark.parametrize("step", DRAIN_STEPS)
@pytest.mark.parametrize("when", ["before", "after"])
def test_a_signal_at_every_drain_boundary_is_deferred_until_drained(tmp_path, monkeypatch,
                                                                    step, when):
    """Codex third review P1: a signal delivered before or after any drain step (i.e. at every
    boundary, including between steps) must not unwind the owner early. When the interrupt
    surfaces, the running transaction has finished, the socket is gone and cleanup ran, all
    before the writer is released; the interrupt still propagates, exactly once."""
    import signal
    import threading
    st = file_store(tmp_path / "repo")
    write(st, "seed", seed)
    started, finished = _slow_blocklist(monkeypatch, 0.5)
    raised = []

    def interrupt(signum, frame):
        raised.append(signum)
        raise KeyboardInterrupt("terminated")
    previous = signal.signal(signal.SIGALRM, interrupt)
    real = getattr(store_broker.Broker, step)

    def wrapped(self):
        if when == "before":
            os.kill(os.getpid(), signal.SIGALRM)
        real(self)
        if when == "after":
            os.kill(os.getpid(), signal.SIGALRM)
    monkeypatch.setattr(store_broker.Broker, step, wrapped)
    state = {}
    try:
        with st.writer(round_id="rnd-sig") as w:
            broker = store_broker.Broker(st, w, "rnd-sig")
            client = store_broker.Client(broker.path, broker.cap, "rnd-sig")

            def submit():
                try:
                    client.submit("prune", "slow", [{"call": "blocklist_add",
                                                     "args": [["https://s.org/2"]], "kwargs": {}}],
                                  st.version())
                except store_broker.BrokerError:
                    pass  # the drain cut the connection; the transaction's outcome stands
            worker = threading.Thread(target=submit)
            with pytest.raises(KeyboardInterrupt):
                with broker.serving():
                    worker.start()
                    assert started.wait(5)
            state.update(finished=finished.is_set(), socket_gone=not broker._dir.exists(),
                         drained=broker._drained, writer_live=w.nonce in st._live_tokens)
        worker.join(5)
    finally:
        signal.signal(signal.SIGALRM, previous)
    assert state == {"finished": True, "socket_gone": True, "drained": True, "writer_live": True}
    assert raised == [signal.SIGALRM]
    assert signal.getsignal(signal.SIGTERM) is not None
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler  # handlers restored
    assert st.blocklist_path.read_text().endswith("https://s.org/2\n")


def test_drain_off_the_main_thread_needs_no_signal_handlers(tmp_path):
    import threading
    st = file_store(tmp_path / "repo")
    write(st, "seed", seed)
    with st.writer(round_id="rnd-thr") as w:
        broker = store_broker.Broker(st, w, "rnd-thr")
        broker._thread.start()
        errors = []
        t = threading.Thread(target=lambda: errors.append(None) if broker.drain() is None else None)
        t.start()
        t.join(10)
    assert errors == [None] and broker._drained and not broker._dir.exists()


def test_drain_is_idempotent_and_refuses_later_batches(tmp_path):
    st = file_store(tmp_path / "repo")
    write(st, "seed", seed)
    with st.writer(round_id="rnd-dr") as w:
        broker = store_broker.Broker(st, w, "rnd-dr")
        with broker.serving():
            broker.drain()
            with pytest.raises(store_broker.BrokerError, match="unavailable"):
                store_broker.Client(broker.path, broker.cap, "rnd-dr").submit(
                    "prune", "late", [], st.version())
        broker.drain()
