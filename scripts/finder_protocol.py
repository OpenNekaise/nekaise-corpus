#!/usr/bin/env python3
"""finder_protocol.py — a finder's rotation report to run_round (scripts/run_round.py).

run_round passes three files to every finder: NEKAISE_ROTATION_HOLD_FILE (keep the committed
pointer), NEKAISE_ROTATION_NEXT_FILE (a dynamic cursor's successor) and
NEKAISE_BACKEND_EXHAUSTED_FILE (the finite universe is proven complete). A finder reports exactly
ONE outcome: HOLD alone, or NEXT (optionally with EXHAUSTED, whose NEXT is then `END`). Standalone
runs (no environment) just print the outcome.
"""
from __future__ import annotations

import os
from pathlib import Path

END = "END"
MAX_CURSOR = 4096  # rotation.MAX_POINTER: the runner refuses longer cursors


class Report:
    def __init__(self):
        self.outcome: str | None = None

    def _claim(self, kind: str) -> None:
        if self.outcome is not None:
            raise RuntimeError(f"finder already reported {self.outcome}; cannot also report {kind}")
        self.outcome = kind

    def hold(self, reason: str) -> None:
        self._claim("hold")
        print(f"# rotation HOLD: {reason}")
        if name := os.environ.get("NEKAISE_ROTATION_HOLD_FILE"):
            Path(name).write_text(reason.strip() + "\n")

    def next(self, cursor: str) -> None:
        cursor = str(cursor)
        if not cursor or len(cursor) > MAX_CURSOR or "\n" in cursor:
            raise ValueError(f"invalid cursor {cursor[:80]!r}")
        self._claim("next")
        print(f"# rotation NEXT: {cursor}")
        if name := os.environ.get("NEKAISE_ROTATION_NEXT_FILE"):
            Path(name).write_text(cursor + "\n")

    def exhausted(self, reason: str) -> None:
        """Terminal completion of a finite universe: NEXT=END plus EXHAUSTED."""
        self._claim("exhausted")
        print(f"# rotation NEXT: {END}; backend EXHAUSTED: {reason}")
        if name := os.environ.get("NEKAISE_ROTATION_NEXT_FILE"):
            Path(name).write_text(END + "\n")
        if name := os.environ.get("NEKAISE_BACKEND_EXHAUSTED_FILE"):
            Path(name).write_text(reason.strip() + "\n")
