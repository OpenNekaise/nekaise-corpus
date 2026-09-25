#!/usr/bin/env python3
"""stream_guard.py — read a streamed HTTP body under a HARD total deadline and a byte cap.

requests/urllib3 reads can block far longer than the idle read timeout: a source that trickles a
few bytes before each timeout expires keeps one `read(n)` waiting until n bytes arrived. Checking
the clock between chunks therefore does not bound a download. read_body() arms a watchdog timer
at the deadline that shuts the response's socket down (and closes the response), which makes the
blocked read fail at once; the failure is then reported as DeadlineExceeded. Used by the
compliance programme's loader rows (build_corpus), discovery client (polite_http) and robots.txt
fetches (robots_policy).
"""
from __future__ import annotations

import socket
import threading
import time


class BodyTooLarge(Exception):
    """The decoded body exceeded the byte cap."""


class DeadlineExceeded(Exception):
    """The total deadline passed before the body was complete."""


def _kill(resp) -> None:
    """Abort a response from another thread: shut its socket down, then close it."""
    try:
        conn = getattr(getattr(resp, "raw", None), "_connection", None)
        sock = getattr(conn, "sock", None)
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
    except Exception:  # noqa: BLE001 — best effort: the close below still ends the read
        pass
    try:
        resp.close()
    except Exception:  # noqa: BLE001
        pass


def read_body(resp, *, max_bytes: int, deadline: float, chunk: int = 8192,
              prefix: int | None = None, on_chunk=None) -> bytes:
    """The decoded body of a streamed response, read before `deadline` (time.monotonic()).
    `prefix`: stop after that many bytes (and close). `on_chunk(n)` is called per chunk (budget
    accounting; its exceptions propagate after the response is closed)."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _kill(resp)
        raise DeadlineExceeded("deadline already passed before reading")
    fired = threading.Event()

    def fire():
        fired.set()
        _kill(resp)

    timer = threading.Timer(remaining, fire)
    timer.daemon = True
    timer.start()
    body = bytearray()
    try:
        for part in resp.iter_content(chunk if prefix is None else min(chunk, prefix)):
            if fired.is_set():
                raise DeadlineExceeded("total deadline reached while reading")
            body.extend(part)
            if on_chunk is not None:
                on_chunk(len(part))
            if prefix is not None and len(body) >= prefix:
                del body[prefix:]
                break
            if len(body) > max_bytes:
                raise BodyTooLarge(f"body exceeds the {max_bytes}-byte cap")
            if time.monotonic() > deadline:
                raise DeadlineExceeded("total deadline reached while reading")
    except (BodyTooLarge, DeadlineExceeded):
        _kill(resp)
        raise
    except Exception as exc:  # noqa: BLE001 — a read aborted by the watchdog
        if fired.is_set():
            raise DeadlineExceeded("total deadline reached while reading") from exc
        _kill(resp)
        raise
    finally:
        timer.cancel()
    if fired.is_set():
        raise DeadlineExceeded("total deadline reached while reading")
    if prefix is not None:
        _kill(resp)
    resp._content = bytes(body)  # noqa: SLF001 — requests' own cache of a consumed body
    resp._content_consumed = True  # noqa: SLF001
    return resp._content  # noqa: SLF001
