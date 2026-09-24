#!/usr/bin/env python3
"""store_broker.py — how a round's subprocesses write to the store (ADR 0001, stage 3).

A round has exactly one writer: run_round holds store.writer(round_id=...) for the whole round.
Its pipeline steps are subprocesses, and a writer token never leaves the process that owns it
(a PostgreSQL token is bound to its session). So run_round starts a Broker: a unix-socket server
that accepts bounded mutation batches from its children and executes each as ONE store
transaction with the round's writer token.

    # in a mutating step (fetch, prune, clean):
    client = store_broker.client()                  # None outside a round
    with store.open().read() as v:                  # an inherited, verified read view
        ...compute...
        version = v.version()
    with client.batch("prune", "decisions", expected_version=version) as tx:
        tx.delete_manifest(ids, reason="junk")      # recorded here, applied by the broker
        tx.blocklist_add(urls)

* Only the steps run_round chooses receive NEKAISE_STORE_BROKER / NEKAISE_STORE_CAP; verification
  gates and discovery workers get no write capability.
* Every batch runs as transaction "<round>.<step>.<batch>", so an identical retry after a lost
  response is a no-op and a different batch under the same id is refused (store replay rules).
* expected_version protects read-compute-write: a batch computed from an older version is refused.
* Only store mutation methods can be requested; each request is JSON, so arguments are plain data.
* The capability is 32 random bytes per round; the socket lives in a 0700 directory and dies with
  the round.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import socketserver
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator

import store

BROKER_ENV = "NEKAISE_STORE_BROKER"
CAP_ENV = "NEKAISE_STORE_CAP"
ROUND_ENV = "NEKAISE_STORE_ROUND"
MUTATIONS = frozenset({
    "insert_entries", "upsert_entries", "delete_entries", "upsert_manifest", "replace_manifest",
    "update_manifest_fields", "delete_manifest", "blocklist_add", "ledger_append", "rotation_set",
    "backend_state_set", "control_set",
})
MAX_REQUEST_BYTES = 256 * 1024 * 1024
REQUEST_DEADLINE = 120.0   # seconds a client may take to send its request
MAX_CONNECTIONS = 8
_BATCH_ID = store._RUN_ID  # same shape as run ids: plain, bounded names


class BrokerError(store.StoreError):
    """The broker refused or failed a batch."""


# Signals whose Python handlers could unwind a broker's owner mid-drain (SIGTERM is the
# maintainer's cancellation, SIGINT is KeyboardInterrupt, SIGALRM/SIGHUP for completeness).
DEFERRED_SIGNALS = tuple(getattr(signal, n) for n in ("SIGTERM", "SIGINT", "SIGALRM", "SIGHUP")
                         if hasattr(signal, n))


class _SignalDeferral:
    """Record DEFERRED_SIGNALS instead of handling them (main thread only), then restore every
    original handler and replay the recorded signals to them.

    Restoration must complete whatever happens: once a handler is restored, a signal delivered
    to ANY thread (a thread mask protects only its own thread) makes Python run that handler on
    the main thread, raising anywhere in this code. So finish() is a resumable state machine —
    each restore is retried until recorded as done, each replay is consumed before it is raised
    (so a raising handler cannot make it loop), every exception is caught and only the first is
    kept — and it is idempotent, so a caller retrying after an escape resumes where it stopped.
    """

    def __init__(self):
        self.previous: dict[int, object] = {}  # signal -> its original handler
        self.pending: list[int] = []
        self._restored: set[int] = set()
        self._replay: list[int] | None = None
        self.done = False

    def _record(self, signum, frame) -> None:
        self.pending.append(signum)

    def start(self) -> None:
        """Swap every handler for the recorder (resumable: the original is saved first)."""
        for sig in DEFERRED_SIGNALS:
            if sig not in self.previous:
                self.previous[sig] = signal.getsignal(sig)
            signal.signal(sig, self._record)

    def finish(self) -> BaseException | None:
        """Restore all handlers, then replay the recorded signals. Returns the first exception a
        restored handler raised meanwhile (to be re-raised by the caller), else None."""
        first: BaseException | None = None
        while not self.done:
            try:
                for sig, handler in self.previous.items():
                    if sig not in self._restored:
                        signal.signal(sig, handler if handler is not None else signal.SIG_DFL)
                        self._restored.add(sig)
                if self._replay is None:  # every recorder is gone: nothing more is recorded
                    self._replay = list(dict.fromkeys(self.pending))
                while self._replay:
                    signal.raise_signal(self._replay.pop(0))  # its handler runs here
                self.done = True
            except BaseException as exc:  # noqa: B036 - a restored handler's interrupt
                if first is None:
                    first = exc
        return first


def _bind(call: str, args: list, kwargs: dict) -> dict:
    """Bind a request to the store method's signature (so positional and keyword forms decode the
    same way) and rebuild the dataclass arguments JSON flattened."""
    import inspect
    bound = inspect.signature(getattr(store.WriteView, call)).bind(None, *args, **kwargs)
    arguments = dict(bound.arguments)
    arguments.pop("self")
    if call == "backend_state_set" and isinstance(arguments.get("value"), dict):
        arguments["value"] = store.BackendState(**arguments["value"])
    return arguments


class Broker:
    """Serve one round's mutation batches with the round's writer token."""

    def __init__(self, st, writer: store.WriterToken, round_id: str):
        self.st, self.writer, self.round_id = st, writer, store._check_run_id(round_id)
        self.cap = secrets.token_hex(32)
        # a short private directory: unix socket paths are limited to ~108 bytes
        self._dir = Path(tempfile.mkdtemp(prefix="nekaise-broker-"))
        os.chmod(self._dir, 0o700)
        self.path = self._dir / "sock"
        self._lock = threading.Lock()  # one transaction at a time: transactions do not nest
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._conns: set[socket.socket] = set()
        self._conns_lock = threading.Lock()
        self._closing = False
        self._drained = False
        self._deferral: _SignalDeferral | None = None
        broker = self

        class Handler(socketserver.StreamRequestHandler):
            def setup(self):
                self.request.settimeout(REQUEST_DEADLINE)  # a stalled client cannot hold us
                super().setup()

            def handle(self):
                if not broker._slots.acquire(blocking=False):
                    self.wfile.write(b'{"ok": false, "error": "BrokerError: too many connections"}\n')
                    return
                with broker._conns_lock:
                    broker._conns.add(self.request)
                try:
                    try:
                        line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
                        if broker._closing:
                            raise BrokerError("broker is shutting down")
                        if len(line) > MAX_REQUEST_BYTES:
                            raise BrokerError("batch too large")
                        reply = broker._execute(json.loads(line))
                    except Exception as exc:  # every failure is reported, never swallowed
                        reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    try:
                        self.wfile.write((json.dumps(reply) + "\n").encode())
                    except OSError:
                        pass  # the client went away; its batch outcome stands
                finally:
                    with broker._conns_lock:
                        broker._conns.discard(self.request)
                    broker._slots.release()

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True          # never join a stuck handler on shutdown
            block_on_close = False

        self._server = Server(str(self.path), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def env(self) -> dict[str, str]:
        """Environment giving one child write access through this broker."""
        return {BROKER_ENV: str(self.path), CAP_ENV: self.cap, ROUND_ENV: self.round_id}

    def _execute(self, msg: dict) -> dict:
        if not secrets.compare_digest(str(msg.get("cap", "")), self.cap):
            raise BrokerError("bad capability")
        step, batch = str(msg.get("step", "")), str(msg.get("batch", ""))
        if not _BATCH_ID.fullmatch(step) or not _BATCH_ID.fullmatch(batch):
            raise BrokerError("step and batch must be plain names")
        requests = msg.get("requests")
        if not isinstance(requests, list):
            raise BrokerError("requests must be a list")
        for r in requests:
            if not isinstance(r, dict) or r.get("call") not in MUTATIONS:
                raise BrokerError(f"not a store mutation: {r.get('call') if isinstance(r, dict) else r!r}")
        expected = store.Version(str(msg.get("expected_version", "")))
        run_id = store._check_run_id(f"{self.round_id}.{step}.{batch}")
        with self._lock:
            if self._closing:  # re-checked under the lock: shutdown may have begun meanwhile
                raise BrokerError("broker is shutting down")
            results = []
            with self.st.transaction(run_id, expected_version=expected, writer=self.writer) as tx:
                for r in requests:
                    arguments = _bind(r["call"], list(r.get("args") or []),
                                      dict(r.get("kwargs") or {}))
                    results.append(getattr(tx, r["call"])(**arguments))
            return {"ok": True, "results": results, "version": self.st.version().token}

    @contextmanager
    def local_batch(self, step: str, batch: str) -> Iterator["store.WriteView"]:
        """A batch from the round's own process (e.g. run_round's discovery merge), run as
        transaction "<round>.<step>.<batch>" and serialized with the children's batches."""
        if not _BATCH_ID.fullmatch(step) or not _BATCH_ID.fullmatch(batch):
            raise BrokerError("step and batch must be plain names")
        run_id = store._check_run_id(f"{self.round_id}.{step}.{batch}")
        with self._lock:
            if self._closing:
                raise BrokerError("broker is shutting down")
            with self.st.transaction(run_id, expected_version=self.st.version(),
                                     writer=self.writer) as tx:
                yield tx

    @contextmanager
    def serving(self) -> Iterator["Broker"]:
        self._thread.start()
        try:
            yield self
        finally:
            self.drain()

    def drain(self) -> None:
        """Stop accepting, cut every open connection (a half-sent request must not keep the owner
        alive), wait until no transaction is executing, and remove the socket. Idempotent.

        Cancellation-safe: the caller releases its writer and locks right after this returns, so
        no Python-level interrupt may surface anywhere inside it. On the main thread the handlers
        of DEFERRED_SIGNALS are swapped for one that only records the signal (a C-level handler
        on any thread just sets a flag; the Python handler then runs on the main thread, and it
        is ours), so nothing is raised inside or between the steps; afterwards every original
        handler is restored and each recorded signal replayed to it (_SignalDeferral.finish:
        complete even when a restored handler raises part-way), and the first interrupt is
        re-raised once the broker is drained. One raised in the instant before the swap is
        caught by the retry loop and re-raised at the end.

        Off the main thread no handler can be swapped (signal.signal is main-thread only) and
        none is needed: Python runs signal handlers on the main thread only, so they cannot
        unwind a stack owned by another thread. Serve and drain a broker on the thread that owns
        its writer (run_round and the maintainer both do so on the main thread)."""
        interrupted: BaseException | None = None
        try:
            while True:
                try:
                    self._drain_steps()
                    break
                except (KeyboardInterrupt, SystemExit) as exc:  # only before the swap
                    interrupted = interrupted or exc
        finally:
            # Restoration completes even if a step failed; finish() catches what restored
            # handlers raise, and the loop resumes it if something escaped in between.
            while self._deferral is not None and not self._deferral.done:
                try:
                    first = self._deferral.finish()
                    interrupted = interrupted or first
                except (KeyboardInterrupt, SystemExit) as exc:
                    interrupted = interrupted or exc
        if interrupted is not None:
            raise interrupted

    def _drain_steps(self) -> None:
        if self._drained:
            return
        if threading.current_thread() is threading.main_thread():
            if self._deferral is None:
                self._deferral = _SignalDeferral()
            self._deferral.start()
        self._closing = True
        for step in (self._stop_server, self._cut_connections, self._wait_idle, self._cleanup):
            step()
        self._drained = True

    def _stop_server(self) -> None:
        if self._thread.is_alive():
            self._server.shutdown()

    def _cut_connections(self) -> None:
        with self._conns_lock:
            for conn in list(self._conns):
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _wait_idle(self) -> None:
        with self._lock:  # held by an executing transaction; _closing refuses any later one
            pass

    def _cleanup(self) -> None:
        self._server.server_close()
        self.path.unlink(missing_ok=True)
        if self._dir.exists():
            os.rmdir(self._dir)


class _Batch:
    """Records mutation calls (store.WriteView names and signatures) for one broker batch."""

    def __init__(self):
        self.requests: list[dict] = []

    def __getattr__(self, name):
        if name not in MUTATIONS:
            raise AttributeError(f"{name} is not a store mutation")

        def record(*args, **kwargs):
            args = [store._materialize(a) for a in args]
            kwargs = {k: store._materialize(v) for k, v in kwargs.items()}
            self.requests.append({"call": name, "args": store._plain(args),
                                  "kwargs": store._plain(kwargs)})
        return record


class Client:
    def __init__(self, path, cap: str, round_id: str):
        self.path, self.cap, self.round_id = str(path), cap, round_id
        self.last_version: store.Version | None = None

    def submit(self, step: str, batch: str, requests: list[dict],
               expected_version: store.Version) -> list:
        msg = {"cap": self.cap, "step": step, "batch": batch, "requests": requests,
               "expected_version": expected_version.token}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            try:
                sock.connect(self.path)
            except OSError as exc:
                raise BrokerError(f"store broker unavailable at {self.path}: {exc}") from exc
            sock.sendall((json.dumps(msg) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(1 << 20)
                if not chunk:
                    raise BrokerError("store broker closed the connection")
                data += chunk
        reply = json.loads(data)
        if not reply.get("ok"):
            raise BrokerError(reply.get("error", "unknown broker error"))
        self.last_version = store.Version(reply["version"])
        return reply["results"]

    @contextmanager
    def batch(self, step: str, batch: str, *, expected_version: store.Version) -> Iterator[_Batch]:
        """Collect mutations; submit them as one transaction when the block exits cleanly."""
        b = _Batch()
        yield b
        if b.requests:
            self.submit(step, batch, b.requests, expected_version)


def client() -> Client | None:
    """The broker client of the round or maintenance window this process runs in, or None
    (standalone commands then take their own writer)."""
    path, cap, rnd = (os.environ.get(k) for k in (BROKER_ENV, CAP_ENV, ROUND_ENV))
    if not (path and cap and rnd):
        return None
    return Client(path, cap, rnd)


def run_batch(st, step: str, body, *, writer: "store.WriterToken | None" = None,
              timeout: float = 30.0):
    """Run a standalone command's read-compute-write as ONE store transaction, wherever it runs.

    body(view, batch) reads only from `view` and records its mutations on `batch` (store
    mutation names and signatures). Under a broker (a round's mutating step, or a child of the
    maintainer's window, whose parent holds the round lock) the view is the inherited read view
    and the batch is submitted with that view's version, so a concurrent change refuses it.
    Otherwise the command takes its own writer — or uses `writer`, when it already holds one —
    for one transaction; `timeout` bounds the wait for a running round. Nothing recorded means no
    transaction. Returns (body's result, [each mutation's result])."""
    batch = _Batch()
    if (c := client()) is not None:
        with st.read() as view:
            out = body(view, batch)
            version = view.version()
        results = (c.submit(step, f"{step}-{secrets.token_hex(6)}", batch.requests, version)
                   if batch.requests else [])
        return out, results
    run_id = store._check_run_id(
        f"{step}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(4)}")
    with ExitStack() as stack:
        if writer is None:
            writer = stack.enter_context(st.writer(timeout=timeout))
        with st.transaction(run_id, expected_version=st.version(), writer=writer) as tx:
            out = body(tx, batch)
            results = [getattr(tx, r["call"])(**_bind(r["call"], r["args"], r["kwargs"]))
                       for r in batch.requests]
    return out, results
