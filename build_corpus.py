#!/usr/bin/env python3
"""build_corpus.py — fetch & maintain the public HVAC reference corpus.

Reads sources.yaml (the curated seed registry), downloads each source into
raw/<source>/<id>.<ext>, extracts plain text into text/<id>.md, and records
everything in manifest.jsonl (one JSON object per line).

  python build_corpus.py            # fetch missing only
  python build_corpus.py --force    # re-fetch everything
  python build_corpus.py --only controls_bas,standards_protocols

Idempotent (skips sources already fetched ok), dedups identical bytes by sha256,
checkpoints the manifest after every fetch.

This corpus is for INTERNAL RESEARCH ONLY — some sources are copyrighted.
Do not redistribute. See README.md.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
TEXT = HERE / "text"
MANIFEST = HERE / "manifest.jsonl"
SOURCES = HERE / "sources.yaml"
UA = "nekaise-studio-hvac-corpus/0.1 (research)"
TIMEOUT = 45


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts = []
    for i, page in enumerate(reader.pages):
        try:
            parts.append(page.extract_text() or "")
        except Exception as e:  # keep going on a bad page
            parts.append(f"[page {i} extract error: {e}]")
    return "\n\n".join(parts).strip()


def extract_html(data: bytes) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(data, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    # MediaWiki/Wikipedia main content if present, else whole body
    main = soup.select_one("div.mw-parser-output") or soup.body or soup
    drop = (".reference", ".mw-editsection", "table.navbox", ".navbox",
            ".vertical-navbox", ".reflist", "#toc", ".toc",
            ".navigation-not-searchable", ".hatnote", ".ambox", "table.ambox",
            ".mbox-small", ".metadata", ".sistersitebox", ".shortdescription",
            ".noprint", ".mw-empty-elt", ".mw-jump-link", "#References",
            "#External_links", "#Further_reading", "#See_also")
    for sel in drop:
        for t in main.select(sel):
            t.decompose()
    text = main.get_text("\n")
    out, blanks = [], 0
    for ln in (l.strip() for l in text.splitlines()):
        if ln:
            out.append(ln)
            blanks = 0
        else:
            blanks += 1
            if blanks <= 1:
                out.append("")
    return "\n".join(out).strip()


def load_manifest() -> dict:
    rows: dict = {}
    if MANIFEST.exists():
        for line in MANIFEST.read_text().splitlines():
            line = line.strip()
            if line:
                r = json.loads(line)
                rows[r["id"]] = r
    return rows


def write_manifest(rows: dict) -> None:
    with MANIFEST.open("w") as f:
        for r in sorted(rows.values(), key=lambda x: (x.get("topic", ""), x["id"])):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def fetch_one(src: dict) -> dict:
    sid = src["id"]
    fmt = src.get("format", "pdf")
    source = src.get("source", "misc")
    ext = {"pdf": "pdf", "html": "html"}.get(fmt, "bin")
    rec = {
        "id": sid, "title": src.get("title", sid), "url": src["url"],
        "source": source, "license": src.get("license", "unknown"),
        "topic": src.get("topic", "misc"), "format": fmt,
        "status": "failed", "http_status": None, "sha256": None, "bytes": 0,
        "raw_path": None, "text_path": None, "text_chars": 0,
        "error": None, "fetched_at": None,
    }
    try:
        resp = requests.get(src["url"], headers={"User-Agent": UA},
                            timeout=TIMEOUT, allow_redirects=True)
        rec["http_status"] = resp.status_code
        resp.raise_for_status()
        data = resp.content
        rec["sha256"] = sha256_bytes(data)
        rec["bytes"] = len(data)

        raw_dir = RAW / source
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"{sid}.{ext}"
        raw_path.write_bytes(data)
        rec["raw_path"] = str(raw_path.relative_to(HERE))

        try:
            txt = extract_pdf(data) if fmt == "pdf" else (
                extract_html(data) if fmt == "html" else "")
        except Exception as e:
            txt, rec["error"] = "", f"text-extract: {e}"
        if txt:
            TEXT.mkdir(parents=True, exist_ok=True)
            header = (f"# {rec['title']}\n\n"
                      f"source: {rec['url']}\nlicense: {rec['license']}\n"
                      f"topic: {rec['topic']}\n\n---\n\n")
            tp = TEXT / f"{sid}.md"
            tp.write_text(header + txt)
            rec["text_path"] = str(tp.relative_to(HERE))
            rec["text_chars"] = len(txt)

        rec["status"] = "ok"
        rec["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    except Exception as e:
        rec["error"] = str(e)
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-fetch everything")
    ap.add_argument("--only", default="", help="comma-separated topics to limit to")
    ap.add_argument("--reextract", action="store_true",
                    help="re-extract text from existing raw files; no download")
    args = ap.parse_args()
    only = {t.strip() for t in args.only.split(",") if t.strip()}

    srcs = yaml.safe_load(SOURCES.read_text())["sources"]
    manifest = load_manifest()

    if args.reextract:
        TEXT.mkdir(parents=True, exist_ok=True)
        done = 0
        for r in sorted(manifest.values(), key=lambda x: x["id"]):
            rp = r.get("raw_path")
            if not rp or not (HERE / rp).exists():
                continue
            data = (HERE / rp).read_bytes()
            fmt = r.get("format", "pdf")
            try:
                txt = extract_pdf(data) if fmt == "pdf" else (
                    extract_html(data) if fmt == "html" else "")
            except Exception as e:
                txt, r["error"] = "", f"reextract: {e}"
            if txt:
                header = (f"# {r['title']}\n\nsource: {r['url']}\n"
                          f"license: {r['license']}\ntopic: {r['topic']}\n\n---\n\n")
                (TEXT / f"{r['id']}.md").write_text(header + txt)
                r["text_path"] = f"text/{r['id']}.md"
                r["text_chars"] = len(txt)
            done += 1
        write_manifest(manifest)
        tot = sum(r["text_chars"] for r in manifest.values() if r["status"] == "ok")
        print(f"re-extracted {done} docs | total text {tot / 1e6:.2f} M chars")
        return

    todo = []
    for s in srcs:
        if only and s.get("topic") not in only:
            continue
        cur = manifest.get(s["id"])
        if cur and cur.get("status") == "ok" and not args.force:
            if cur.get("raw_path") and (HERE / cur["raw_path"]).exists():
                continue
        todo.append(s)

    print(f"sources: {len(srcs)} total, {len(todo)} to fetch "
          f"({'forced' if args.force else 'missing only'})")
    for i, s in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {s['id']} ...", end=" ", flush=True)
        rec = fetch_one(s)
        manifest[rec["id"]] = rec
        if rec["status"] == "ok":
            print(f"ok  {rec['bytes'] // 1024}KB  {rec['text_chars']} chars")
        else:
            print(f"FAIL http={rec['http_status']} {rec.get('error')}")
        write_manifest(manifest)  # checkpoint after each

    seen: dict = {}
    for r in manifest.values():
        if r.get("sha256"):
            seen.setdefault(r["sha256"], []).append(r["id"])
    dups = {h: ids for h, ids in seen.items() if len(ids) > 1}

    ok = sum(1 for r in manifest.values() if r["status"] == "ok")
    by_topic: dict = {}
    for r in manifest.values():
        if r["status"] == "ok":
            by_topic[r["topic"]] = by_topic.get(r["topic"], 0) + 1
    print(f"\nmanifest: {len(manifest)} rows | {ok} ok | {len(manifest) - ok} failed")
    print("ok by topic:", by_topic)
    if dups:
        print("duplicate bytes (same sha256):", dups)


if __name__ == "__main__":
    main()
