#!/usr/bin/env python3
"""stream_guard.py — HARD total deadlines for HTTP requests (headers AND body) and byte caps.

requests/urllib3 timeouts bound each socket read, not a request: a source that trickles a few
bytes before every idle timeout expires keeps the header read, or one body `read(n)`, waiting
indefinitely. `Deadline` is a context manager: while it is active on a thread, every urllib3
connection that thread connects or sends a request on registers its socket with the guard, and a
watchdog timer shuts those sockets down (and closes watched responses) at the deadline, so the
blocked read fails at once. The socket object itself is kept, so a `Connection: close` response
whose connection already dropped its `sock` reference is still interrupted. A failure after the
watchdog fired is reported as DeadlineExceeded.

    with stream_guard.Deadline(time.monotonic() + 180) as guard:
        resp = requests.get(url, stream=True, timeout=(10, 45))
        body = stream_guard.read_body(resp, max_bytes=..., guard=guard)

Used by the compliance programme's loader rows (build_corpus), discovery client (polite_http)
and robots.txt fetches (robots_policy). The urllib3 hooks are no-ops on threads without a guard.
"""
from __future__ import annotations

import socket
import threading
import time

import urllib3.connection


class BodyTooLarge(Exception):
    """The decoded body exceeded the byte cap."""


class DeadlineExceeded(Exception):
    """The total deadline passed before the request (headers and body) completed."""


_local = threading.local()


def _shutdown(sock) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except Exception:  # noqa: BLE001 — already closed / not connected
        pass


def _response_socket(resp):
    """The socket a response reads from, via its connection or its buffered reader."""
    raw = getattr(resp, "raw", None)
    conn = getattr(raw, "_connection", None)
    if getattr(conn, "sock", None) is not None:
        return conn.sock
    fp = getattr(getattr(raw, "_fp", None), "fp", None)   # http.client.HTTPResponse.fp
    sock_io = getattr(fp, "raw", None)                    # socket.SocketIO
    return getattr(sock_io, "_sock", None)


def _kill(resp) -> None:
    """Abort a response from another thread: shut its socket down, then close it."""
    sock = _response_socket(resp)
    if sock is not None:
        _shutdown(sock)
    try:
        resp.close()
    except Exception:  # noqa: BLE001
        pass


class Deadline:
    """A total deadline (time.monotonic() value) for the requests made on this thread."""

    def __init__(self, at: float, label: str = "request"):
        self.at, self.label = at, label
        self.fired = threading.Event()
        self._lock = threading.Lock()
        self._socks: list = []
        self._resps: list = []
        self._timer: threading.Timer | None = None
        self._prev = None

    def __enter__(self) -> "Deadline":
        remaining = self.at - time.monotonic()
        if remaining <= 0:
            self.fired.set()
            raise DeadlineExceeded(f"{self.label}: the deadline passed before the request")
        self._prev = getattr(_local, "guard", None)
        _local.guard = self
        self._timer = threading.Timer(remaining, self._fire)
        self._timer.daemon = True
        self._timer.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._timer is not None:
            self._timer.cancel()
        _local.guard = self._prev
        if exc_type is not None and self.fired.is_set() and not issubclass(
                exc_type, (DeadlineExceeded, BodyTooLarge)):
            raise DeadlineExceeded(f"{self.label}: total deadline reached") from exc
        return False

    def _fire(self) -> None:
        self.fired.set()
        with self._lock:
            socks, resps = list(self._socks), list(self._resps)
        for sock in socks:
            _shutdown(sock)
        for resp in resps:
            _kill(resp)

    def register_socket(self, sock) -> None:
        if sock is None:
            return
        with self._lock:
            self._socks.append(sock)
        if self.fired.is_set():
            _shutdown(sock)

    def watch(self, resp) -> None:
        with self._lock:
            self._resps.append(resp)
        sock = _response_socket(resp)
        if sock is not None:
            self.register_socket(sock)
        if self.fired.is_set():
            _kill(resp)


def _install_hooks() -> None:
    """Register every socket a guarded thread connects or sends on (idempotent)."""
    def wrap(cls, name, after: bool):
        original = cls.__dict__.get(name)
        if original is None or getattr(original, "_stream_guard", False):
            return

        def hooked(self, *args, **kwargs):
            guard = getattr(_local, "guard", None)
            if guard is not None and not after:
                guard.register_socket(getattr(self, "sock", None))
            result = original(self, *args, **kwargs)
            if guard is not None and after:
                guard.register_socket(getattr(self, "sock", None))
            return result
        hooked._stream_guard = True
        setattr(cls, name, hooked)
    for cls in (urllib3.connection.HTTPConnection, urllib3.connection.HTTPSConnection):
        wrap(cls, "connect", after=True)
    wrap(urllib3.connection.HTTPConnection, "request", after=False)


_install_hooks()


def read_body(resp, *, max_bytes: int, deadline: float | None = None, chunk: int = 8192,
              prefix: int | None = None, on_chunk=None, guard: Deadline | None = None) -> bytes:
    """The decoded body of a streamed response, read before the deadline — the active `guard`'s
    or, without one, `deadline` (time.monotonic()) with a guard of its own. `prefix`: stop after
    that many bytes (and close). `on_chunk(n)` is called per chunk (budget accounting; its
    exceptions propagate after the response is closed)."""
    if guard is None:
        with Deadline(deadline if deadline is not None else time.monotonic() + 180,
                      "body") as own:
            return read_body(resp, max_bytes=max_bytes, chunk=chunk, prefix=prefix,
                             on_chunk=on_chunk, guard=own)
    guard.watch(resp)
    body = bytearray()
    try:
        for part in resp.iter_content(chunk if prefix is None else min(chunk, prefix)):
            if guard.fired.is_set():
                raise DeadlineExceeded("total deadline reached while reading")
            body.extend(part)
            if on_chunk is not None:
                on_chunk(len(part))
            if prefix is not None and len(body) >= prefix:
                del body[prefix:]
                break
            if len(body) > max_bytes:
                raise BodyTooLarge(f"body exceeds the {max_bytes}-byte cap")
            if time.monotonic() > guard.at:
                raise DeadlineExceeded("total deadline reached while reading")
    except (BodyTooLarge, DeadlineExceeded):
        _kill(resp)
        raise
    except Exception as exc:  # noqa: BLE001 — a read aborted by the watchdog
        if guard.fired.is_set():
            raise DeadlineExceeded("total deadline reached while reading") from exc
        _kill(resp)
        raise
    if guard.fired.is_set():
        raise DeadlineExceeded("total deadline reached while reading")
    if prefix is not None:
        _kill(resp)
    resp._content = bytes(body)  # noqa: SLF001 — requests' own cache of a consumed body
    resp._content_consumed = True  # noqa: SLF001
    return resp._content  # noqa: SLF001
