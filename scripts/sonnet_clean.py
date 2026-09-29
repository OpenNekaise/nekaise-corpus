#!/usr/bin/env python3
"""sonnet_clean.py — the model layer of corpus_v1/: rewrite broken text into normal text.

The deterministic rules (scripts/v1_rules.py) clean every document but cannot repair what only
a reader can: bad OCR ('Tung sten', 'determllled', 'lS dJssolved'). This stage sends the most
damaged documents — ranked by the OCR damage score corpus_v1.py records for every file — to a
language model, one file at a time, part by part, and stores each checked repair in
corpus_v1/.revisions/<id>.md, which corpus_v1.py then uses instead of the rule output for as
long as the text/ source is unchanged.

The model input is the RULE-CLEANED body (furniture already gone, paragraphs already re-flowed),
so the model only repairs. Every revised part is checked here, and a part that fails is retried
once with the reason, then halved and retried, and otherwise keeps its rule-cleaned text:
* numbers: the output's digit runs must align, in order, with the source's digits (OCR
  look-alikes l/I/O/S/B allowed; re-spacing allowed); a decimal point inserted between digits
  that were adjacent in the source is an invention ('443' -> '44.3');
* shrinkage: a part may not lose more than half its letters unless it was mostly garbage;
* drops: <<DROP>> is accepted only for a part the damage score calls garbage;
* growth: the output may not grow far beyond the source.
Parts the model refuses (safety or output filters) keep their rule-cleaned text and are
recorded, so another backend can redo them later.

Backends (the queue is the same for any model, so a local model can later run it endlessly):
    --backend claude   headless Claude Code, default model claude-sonnet-5-5
    --backend openai   an OpenAI-compatible server (llama.cpp, vLLM, Ollama) at --endpoint

    python scripts/sonnet_clean.py --max-seconds 3600          # repair the queue for an hour
    python scripts/sonnet_clean.py --ids a.md b.md             # repair exactly these
    python scripts/sonnet_clean.py --queue 20                  # show the next 20 in the queue
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import corpus_v1
import v1_rules

HERE = Path(__file__).resolve().parents[1]
OUT = corpus_v1.OUT
REVISIONS = corpus_v1.REVISIONS
LOGS = OUT / ".log"
LOCK = HERE / "workspace" / ".sonnet-clean.lock"
# Run the CLI outside the repo so no CLAUDE.md / project settings leak into the context.
NEUTRAL_CWD = Path(tempfile.gettempdir()) / "nekaise-sonnet-clean"

PROMPT_VERSION = "p2"
CHUNK_CHARS = 16_000          # body characters per model call
MAX_FILE_CHARS = 3_000_000    # larger files wait for a bigger budget (not truncated)
CALL_TIMEOUT = 900
MIN_SPLIT_CHARS = 2_000       # a rejected part larger than this is halved and retried
MIN_DAMAGE = 0.08             # queue threshold on corpus_v1's OCR damage score
DROP = "<<DROP>>"

SYSTEM_PROMPT = """You repair ONE part of a document for the training corpus of a built-environment
language model (buildings, HVAC, energy, construction, standards). The text was extracted from a PDF
or an old scan and already mechanically cleaned; what remains broken is for you to fix. Output ONLY
the repaired text: no preamble, no notes, no code fences.

Repair:
- OCR damage: split words ('Tung sten' -> 'Tungsten'), misread letters ('determllled' ->
  'determined', 'lS' -> 'is', 'aCld' -> 'acid'), stray symbols inside words ('chrom~um' -> 'chromium'),
  digits read as letters ('3/l6' -> '3/16', '5S.68' -> '55.68') where the digit is certain.
- Paragraphs still broken across lines; words hyphenated across line breaks.
- Leftover furniture: running headers/footers and page numbers inside the text (delete them and
  join the sentence around them - never turn them into headings), garbage lines that are not words.
- Structure: markdown '#'/'##' only for headings that are present in the text, '- ' lists, a
  markdown table when rows and columns are clear, LaTeX ($...$) for formulas whose parts are present.

Never:
- translate, summarize, shorten, reorder, add sentences, titles or comments;
- guess a number: if a digit is illegible, leave that OCR text as it is. Never insert a decimal point
  or digit that is not in the input. Numbers, units, names, dates and citations stay exactly;
- drop content: tables, lists, data rows, reference lists, code and equations are content.
  When unsure whether something is content, keep it.

Only if the whole part is unreadable garbage with no words to recover, output exactly: <<DROP>>
The part may start or end mid-sentence because the document continues in neighbouring parts."""


# ---------------------------------------------------------------- checks

_DIGITS = re.compile(r"\d+")
_OCR_DIGIT = str.maketrans({"l": "1", "I": "1", "|": "1", "O": "0", "o": "0", "S": "5", "B": "8"})
_DIGITISH = re.compile(r"[0-9lI|OoSB]*\d[0-9lI|OoSB]*")
_DECIMAL = re.compile(r"(\d+)[.,](\d+)")
_LETTER = re.compile(r"[^\W\d_]")


def source_digits(src: str) -> tuple[str, set[int]]:
    """The source's digits as one string (OCR look-alikes inside numeric tokens read as digits)
    and the set of positions where a digit run starts (a boundary between two runs)."""
    src = unicodedata.normalize("NFKC", src)
    digits, starts = [], set()
    for tok in _DIGITISH.findall(src):
        for run in _DIGITS.findall(tok.translate(_OCR_DIGIT)):
            starts.add(sum(len(d) for d in digits))
            digits.append(run)
    return "".join(digits), starts


def number_problem(src: str, out: str) -> str | None:
    """Digit runs of `out` must appear in order inside the source's digit string. A run may
    span neighbouring source runs ('19 57' -> '1957') or split one ('446425646' -> '4464 25646'),
    but not appear from nowhere, and a decimal point may not split a source run."""
    s, starts = source_digits(src)
    out = unicodedata.normalize("NFKC", out)
    pos = 0
    ends: dict[int, int] = {}  # output run start offset -> source end position
    for m in _DIGITS.finditer(out):
        run = m.group(0)
        at = s.find(run, pos)
        if at < 0:
            # Out of order: a long number may move (a re-built table); a 1-2 digit one that is
            # not next in line is how a guessed digit ('sn.87i' -> '2.87') looks.
            at = s.find(run) if len(run) >= 3 else -1
            if at < 0:
                return f"number {run!r} is not in the source at this point"
        else:
            pos = at + len(run)
        ends[m.start()] = at
    for m in _DECIMAL.finditer(out):
        a, b = m.start(1), m.start(2)
        if a in ends and b in ends:
            joint = ends[a] + len(m.group(1))
            if ends[b] == joint and joint not in starts and f"{m.group(1)}.{m.group(2)}" not in src \
                    and f"{m.group(1)},{m.group(2)}" not in src:
                return f"decimal point inserted into {m.group(1)}{m.group(2)!s}"
    return None


def check(src: str, out: str) -> str | None:
    """Why a repaired part is rejected, or None if it passes."""
    garbage = (v1_rules.damage_score(src) or 0) >= 0.35 or len(_LETTER.findall(src)) < 200
    if out.strip() == DROP:
        return None if garbage else "dropped a part that holds readable content; repair it instead"
    if why := number_problem(src, out):
        return why
    if len(out) > 1.25 * len(src) + 200:
        return "output much longer than input"
    if not garbage and len(_LETTER.findall(out)) < 0.5 * len(_LETTER.findall(src)):
        return "output lost more than half of the text; keep all content"
    return None


# ---------------------------------------------------------------- backends
# A backend takes the user text and returns (text, usage). It raises Refused when the model
# declines the content — deterministic, so never retried with the same model.


class Refused(RuntimeError):
    pass


_REFUSAL = re.compile(r"Try rephrasing the request|change your model|unable to respond to this"
                      r"|blocked by content filtering", re.I)


class ClaudeBackend:
    def __init__(self, model: str, effort: str):
        self.model, self.effort = model, effort
        NEUTRAL_CWD.mkdir(parents=True, exist_ok=True)

    def __call__(self, prompt: str) -> tuple[str, dict]:
        cmd = ["claude", "-p", "--model", self.model, "--effort", self.effort,
               "--system-prompt", SYSTEM_PROMPT, "--tools", "", "--setting-sources", "",
               "--no-session-persistence", "--output-format", "json"]
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=CALL_TIMEOUT, cwd=NEUTRAL_CWD)
        if p.returncode != 0:
            both = p.stdout + p.stderr
            raise (Refused if _REFUSAL.search(both) else RuntimeError)(
                f"claude exit {p.returncode}: {both[-400:]}")
        d = json.loads(p.stdout)
        if d.get("is_error") or not isinstance(d.get("result"), str):
            raise RuntimeError(f"model error: {str(d.get('result'))[:400]}")
        if d.get("stop_reason") == "max_tokens":
            raise RuntimeError("output truncated (max_tokens)")
        u = d.get("usage", {})
        return d["result"], {"in": u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                             + u.get("cache_creation_input_tokens", 0),
                             "out": u.get("output_tokens", 0), "usd": d.get("total_cost_usd", 0.0)}


class OpenAIBackend:
    """Any OpenAI-compatible chat endpoint (a local llama.cpp / vLLM / Ollama server)."""

    def __init__(self, model: str, endpoint: str):
        self.model, self.endpoint = model, endpoint.rstrip("/") + "/chat/completions"

    def __call__(self, prompt: str) -> tuple[str, dict]:
        body = {"model": self.model, "temperature": 0,
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": prompt}]}
        req = urllib.request.Request(self.endpoint, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=CALL_TIMEOUT) as r:
            d = json.load(r)
        c = d["choices"][0]
        if c.get("finish_reason") == "length":
            raise RuntimeError("output truncated (length)")
        u = d.get("usage", {})
        return c["message"]["content"], {"in": u.get("prompt_tokens", 0),
                                         "out": u.get("completion_tokens", 0), "usd": 0.0}


# ---------------------------------------------------------------- one file

def chunks(text: str) -> list[str]:
    """The body in parts of about CHUNK_CHARS, split at paragraph breaks where possible."""
    parts, cur, size = [], [], 0
    for x in text.split("\n"):
        cur.append(x)
        size += len(x) + 1
        if size >= CHUNK_CHARS and (not x.strip() or size >= 1.3 * CHUNK_CHARS):
            parts.append("\n".join(cur))
            cur, size = [], 0
    if cur or not parts:
        parts.append("\n".join(cur))
    return parts


def unfence(out: str) -> str:
    out = out.strip()
    if out.startswith("```") and out.endswith("```"):
        out = out.split("\n", 1)[1] if "\n" in out else ""
        out = out.rsplit("```", 1)[0]
    return out.strip()


def revise_part(backend, title: str, k: int, n: int, src: str, before: str = "") -> dict:
    """One part through the model, checked; a corrective retry; then halves; else the source."""
    context = f"\nThe previous part ended with (context only, do not repeat):\n<<<{before}>>>\n" \
        if before else ""
    prompt = f"Document: {title}\nPart {k + 1} of {n}.{context}\n<text>\n{src}\n</text>"
    usage: Counter = Counter()
    why = None
    for attempt in range(2):
        ask = prompt + (f"\n\nYour previous repair was rejected: {why}. Repair again; keep every "
                        "number exactly as in the input and keep all content." if why else "")
        out, u = backend(ask)
        usage.update(u)
        out = unfence(out)
        why = check(src, out)
        if why is None:
            dropped = out == DROP
            return {"text": "" if dropped else out, "dropped": dropped, "dropped_text":
                    src[:2000] if dropped else "", "fallback": None, "fallback_chars": 0,
                    "usage": usage, "attempts": attempt + 1}
    lines = src.split("\n")
    if len(src) > MIN_SPLIT_CHARS and len(lines) > 1:
        cut = len(lines) // 2
        blanks = [i for i in range(len(lines) // 4, 3 * len(lines) // 4) if not lines[i].strip()]
        if blanks:
            cut = min(blanks, key=lambda i: abs(i - len(lines) // 2))
        halves = [revise_part(backend, title, k, n, "\n".join(h), before)
                  for h in (lines[:cut], lines[cut:]) if "\n".join(h).strip()]
        for h in halves:
            usage.update(h["usage"])
        return {"text": "\n\n".join(h["text"] for h in halves if h["text"]),
                "dropped": all(h["dropped"] for h in halves),
                "dropped_text": "".join(h["dropped_text"] for h in halves)[:2000],
                "fallback": next((h["fallback"] for h in halves if h["fallback"]), None),
                "fallback_chars": sum(h["fallback_chars"] for h in halves),
                "usage": usage, "attempts": 2 + sum(h["attempts"] for h in halves)}
    return {"text": src, "dropped": False, "dropped_text": "", "fallback": why,
            "fallback_chars": len(src), "usage": usage, "attempts": 2}


def revise(doc_id: str, backend, pool: ThreadPoolExecutor) -> dict:
    t0 = time.time()
    key = corpus_v1.source_key(doc_id)
    raw = (corpus_v1.TEXT / doc_id).read_bytes()
    header, body, in_chars, damage, kind = corpus_v1.rule_clean(doc_id)
    rec = {"id": doc_id, "src_size": key[0], "src_mtime": key[1],
           "src_sha256": hashlib.sha256(raw).hexdigest(), "prompt": PROMPT_VERSION,
           "in_chars": len(body), "damage": damage}
    if len(body) > MAX_FILE_CHARS:
        return rec | {"status": "skipped_large"}
    parts = chunks(body)
    title = header.split("\n", 1)[0].lstrip("# ")[:200]

    def call(k, src):
        if not src.strip():
            return {"text": "", "dropped": False, "dropped_text": "", "fallback": None,
                    "fallback_chars": 0, "usage": Counter(), "attempts": 0}
        before = parts[k - 1][-600:] if k else ""
        for attempt in range(3):  # transport errors (rate limits, timeouts): back off, retry
            try:
                return revise_part(backend, title, k, len(parts), src, before)
            except Refused as e:
                return {"text": src, "dropped": False, "dropped_text": "",
                        "fallback": f"refused: {str(e)[-120:]}", "fallback_chars": len(src),
                        "refused": True, "usage": Counter(), "attempts": 1}
            except Exception:  # noqa: BLE001
                if attempt == 2:
                    raise
                time.sleep(20 * (attempt + 1))

    futs = [pool.submit(call, k, src) for k, src in enumerate(parts)]
    try:
        results = [f.result() for f in futs]
    except Exception as e:  # noqa: BLE001
        for f in futs:
            f.cancel()
        return rec | {"status": "failed", "error": str(e)[-500:], "secs": round(time.time() - t0, 1)}

    usage: Counter = Counter()
    for r in results:
        usage.update(r["usage"])
    text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(r["text"] for r in results if r["text"])).strip()
    fallbacks = [r["fallback"] for r in results if r["fallback"]]
    rec |= {"status": "ok" if text else "dropped", "out_chars": len(text), "parts": len(parts),
            "parts_dropped": sum(r["dropped"] for r in results),
            "dropped_samples": [r["dropped_text"][:500] for r in results if r["dropped_text"]][:5],
            "parts_fallback": len(fallbacks), "fallback_reasons": fallbacks[:5],
            "fallback_chars": sum(r["fallback_chars"] for r in results),
            "parts_refused": sum(bool(r.get("refused")) for r in results),
            "retries": sum(max(0, r["attempts"] - 1) for r in results),
            "secs": round(time.time() - t0, 1), "tokens_in": usage["in"],
            "tokens_out": usage["out"], "usd": round(usage["usd"], 4)}
    if text:
        corpus_v1.write_atomic(REVISIONS / doc_id, text + "\n")
    return rec


# ---------------------------------------------------------------- queue and run

def queue(con, limit: int | None = None) -> list[str]:
    """Most damaged documents first, skipping ones already repaired under this prompt from the
    same source (a refused one is retried only by a different model)."""
    rows = con.execute(f"""
        SELECT b.id FROM build b LEFT JOIN revision r ON r.id = b.id
        WHERE b.kind = 'doc' AND b.damage >= ?
          AND (r.id IS NULL OR r.prompt != ? OR r.src_size != b.src_size
               OR r.src_mtime != b.src_mtime OR r.status = 'failed')
        ORDER BY b.damage DESC {'LIMIT ' + str(int(limit)) if limit else ''}""",
                       (MIN_DAMAGE, PROMPT_VERSION)).fetchall()
    return [r[0] for r in rows]


def record_revision(con, rec: dict, model: str, run_id: str) -> None:
    if rec["status"] in ("failed", "skipped_large"):
        return
    con.execute("INSERT OR REPLACE INTO revision VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec["id"], rec["src_size"], rec["src_mtime"], rec["src_sha256"], model,
                 rec["prompt"], rec["status"], rec.get("parts"), rec.get("parts_fallback"),
                 rec.get("parts_refused"), rec.get("in_chars"), rec.get("out_chars"), run_id))
    con.commit()


def run(args) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    LOCK.parent.mkdir(exist_ok=True)
    lock = LOCK.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another sonnet_clean run holds the lock; exiting")
        return 0
    backend = (OpenAIBackend(args.model, args.endpoint) if args.backend == "openai"
               else ClaudeBackend(args.model, args.effort))
    con = corpus_v1.connect()
    todo = args.ids or queue(con)
    print(f"queue: {len(todo)} documents", flush=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = LOGS / f"{run_id}.jsonl"
    deadline = time.time() + args.max_seconds
    t0 = time.time()
    totals: Counter = Counter()
    lock_db = threading.Lock()
    calls = ThreadPoolExecutor(max_workers=args.workers)
    files = ThreadPoolExecutor(max_workers=max(2, args.workers // 2))
    it = iter(todo)
    inflight: set = set()

    def refill():
        while len(inflight) < max(2, args.workers // 2) and time.time() < deadline:
            doc = next(it, None)
            if doc is None:
                return
            inflight.add(files.submit(revise, doc, backend, calls))

    refill()
    while inflight:
        done = next(as_completed(inflight))
        inflight.discard(done)
        try:
            rec = done.result()
        except Exception as e:  # noqa: BLE001 — one bad file never stops the run
            print(f"error: {e}", file=sys.stderr)
            refill()
            continue
        with lock_db:
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            record_revision(con, rec, args.model, run_id)
            if rec["status"] in ("ok", "dropped"):
                corpus_v1.rebuild_one(con, rec["id"])  # overlay into corpus_v1/ now
            totals["files"] += 1
            totals[rec["status"]] += 1
            for k in ("in_chars", "out_chars", "tokens_in", "tokens_out", "parts",
                      "parts_dropped", "parts_fallback", "parts_refused", "fallback_chars",
                      "retries"):
                totals[k] += rec.get(k) or 0
            totals["usd_milli"] += int(1000 * (rec.get("usd") or 0))
            el = time.time() - t0
            print(f"[{el / 60:5.1f} min] {totals['files']} files ({totals['files'] / el * 3600:.0f}/h) "
                  f"{rec['id'][:50]} {rec['status']} fb={rec.get('parts_fallback')}", flush=True)
        refill()
    files.shutdown()
    calls.shutdown()
    el = time.time() - t0
    summary = {"run": run_id, "model": args.model, "prompt": PROMPT_VERSION,
               "workers": args.workers, "elapsed_min": round(el / 60, 1),
               "files_per_hour": round(totals["files"] / max(el, 1) * 3600),
               "usd": totals.pop("usd_milli", 0) / 1000, **totals}
    (LOGS / f"{run_id}.summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--backend", choices=["claude", "openai"], default="claude")
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--effort", default="low")
    ap.add_argument("--endpoint", default="http://127.0.0.1:8000/v1")
    ap.add_argument("--workers", type=int, default=16, help="parallel model calls")
    ap.add_argument("--max-seconds", type=int, default=3600, help="start no new file after this")
    ap.add_argument("--ids", nargs="*", help="repair exactly these doc ids")
    ap.add_argument("--queue", type=int, help="print the next N queued ids and exit")
    args = ap.parse_args()
    if args.queue:
        for doc_id in queue(corpus_v1.connect(), args.queue):
            print(doc_id)
        return 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
