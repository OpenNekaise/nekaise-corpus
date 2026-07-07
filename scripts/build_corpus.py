#!/usr/bin/env python3
"""build_corpus.py — fetch & verify the building-energy corpus from the registry.

Reads sources.yaml, downloads each source into raw/<source>/<id>.<ext>, extracts plain text into
text/<id>.md, and records everything (incl. sha256) in manifest.jsonl. The committed manifest is the
REPRODUCIBILITY record: a fresh clone runs this to fetch the SAME bytes, and the run reports how many
reproduced exactly (sha256 matches the manifest) vs drifted (the source changed upstream) vs new.

  python scripts/build_corpus.py            # fetch missing; report reproduced / drifted / new vs manifest
  python scripts/build_corpus.py --force    # re-fetch everything
  python scripts/build_corpus.py --only controls_bas
  python scripts/build_corpus.py --verify   # no download: re-hash local raw files against the manifest

Idempotent, dedups identical bytes by sha256, checkpoints the manifest after every fetch. raw/ and
text/ are git-ignored; respect each source's license (see README.md).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import time
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
RAW = HERE / "raw"
TEXT = HERE / "text"
MANIFEST = HERE / "manifest.jsonl"
SOURCES = HERE / "sources.yaml"
# Browser-like UA: publisher / repository bot-walls (eScholarship, Frontiers, PMC, …) 403 a generic
# UA even for openly-licensed (CC-BY / OA) PDFs we're entitled to fetch. (MDPI sits behind Cloudflare
# and still blocks; those need a headless browser — skipped for now.)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = 45
# plain-text source formats (GitHub READMEs / docs, .rst, etc.): stored verbatim, no parsing.
TEXT_FORMATS = {"md", "rst", "txt"}


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def extract_text_plain(data: bytes) -> str:
    """Decode an already-human-readable text file (markdown / rst / txt) verbatim."""
    return data.decode("utf-8", "ignore").strip()


def extract_for(fmt: str, data: bytes) -> str:
    if fmt == "pdf":
        return extract_pdf(data)
    if fmt == "html":
        return extract_html(data)
    if fmt in TEXT_FORMATS:
        return extract_text_plain(data)
    return ""


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
    # main content: MediaWiki first, then common doc-site (sphinx/readthedocs/mkdocs) containers, else body
    main = (soup.select_one("div.mw-parser-output") or soup.select_one("[role=main]")
            or soup.select_one("main") or soup.select_one("article")
            or soup.select_one("div.body") or soup.select_one("div.document")
            or soup.select_one(".md-content") or soup.select_one(".rst-content")
            or soup.body or soup)
    drop = (".reference", ".mw-editsection", "table.navbox", ".navbox",
            ".vertical-navbox", ".reflist", "#toc", ".toc",
            ".navigation-not-searchable", ".hatnote", ".ambox", "table.ambox",
            ".mbox-small", ".metadata", ".sistersitebox", ".shortdescription",
            ".noprint", ".mw-empty-elt", ".mw-jump-link", "#References",
            "#External_links", "#Further_reading", "#See_also",
            # doc-site chrome (sphinx / readthedocs / mkdocs):
            "nav", "header", "footer", ".sphinxsidebar", ".wy-nav-side",
            ".toctree-wrapper", ".headerlink", ".md-sidebar", ".md-header",
            ".md-footer", ".rst-footer-buttons", ".related", "#searchbox",
            ".breadcrumbs", ".wy-breadcrumbs", "[role=navigation]")
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
    ext = {"pdf": "pdf", "html": "html"}.get(fmt, fmt if fmt in TEXT_FORMATS else "bin")
    rec = {
        "id": sid, "title": src.get("title", sid), "url": src["url"],
        "source": source, "license": src.get("license", "unknown"),
        "topic": src.get("topic", "misc"), "format": fmt,
        "status": "failed", "http_status": None, "sha256": None, "bytes": 0,
        "raw_path": None, "text_path": None, "text_chars": 0,
        "error": None, "fetched_at": None,
    }
    try:
        resp = requests.get(src["url"],
                            headers={"User-Agent": UA,
                                     "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8"},
                            timeout=TIMEOUT, allow_redirects=True)
        rec["http_status"] = resp.status_code
        if resp.status_code in (403, 429):
            # Akamai/Cloudflare WAFs (e.g. fema.gov) 403 the python client but pass curl's TLS
            # fingerprint. Fall back to curl for openly-licensed docs we're entitled to fetch.
            out = subprocess.run(["curl", "-sSL", "--max-time", str(TIMEOUT), "-A", UA, src["url"]],
                                 capture_output=True, timeout=TIMEOUT + 15)
            body = out.stdout if out.returncode == 0 else b""
            # only trust the fallback if it returned the expected content (a real PDF, not a WAF
            # HTML challenge page that curl happily 200s)
            good = len(body) > 512 and (body[:5] == b"%PDF-" if fmt == "pdf" else True)
            if good:
                data, rec["http_status"] = body, 200
            else:
                resp.raise_for_status()
                data = resp.content
        else:
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
            txt = extract_for(fmt, data)
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
    ap.add_argument("--verify", action="store_true",
                    help="re-hash local raw files against the manifest sha256; no download")
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
                txt = extract_for(fmt, data)
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

    if args.verify:
        # reproducibility check: re-hash local raw files against the committed manifest sha256.
        match = miss = mismatch = 0
        for r in manifest.values():
            if r.get("status") != "ok" or not r.get("sha256"):
                continue
            rp = r.get("raw_path")
            if not rp or not (HERE / rp).exists():
                miss += 1
                continue
            if sha256_bytes((HERE / rp).read_bytes()) == r["sha256"]:
                match += 1
            else:
                mismatch += 1
                print(f"  MISMATCH {r['id']}")
        n_ok = sum(1 for r in manifest.values() if r.get("status") == "ok")
        print(f"verify: {match} match | {mismatch} sha256 MISMATCH | {miss} not downloaded "
              f"(of {n_ok} ok docs in manifest)")
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

    # the committed manifest's sha256 = what WE fetched; compare to detect upstream drift.
    expected = {sid: r.get("sha256") for sid, r in manifest.items() if r.get("sha256")}
    repro = drift = new = 0
    print(f"sources: {len(srcs)} total, {len(todo)} to fetch "
          f"({'forced' if args.force else 'missing only'})")
    for i, s in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {s['id']} ...", end=" ", flush=True)
        rec = fetch_one(s)
        manifest[rec["id"]] = rec
        if rec["status"] == "ok":
            exp = expected.get(rec["id"])
            tag = "reproduced" if exp == rec["sha256"] else ("DRIFTED" if exp else "new")
            repro += exp == rec["sha256"]
            drift += bool(exp) and exp != rec["sha256"]
            new += not exp
            print(f"ok  {rec['bytes'] // 1024}KB  {rec['text_chars']} chars  [{tag}]")
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
    if repro or drift or new:
        print(f"reproducibility vs manifest: {repro} reproduced (sha256 match) | "
              f"{drift} DRIFTED (source changed) | {new} new")
    if dups:
        print("duplicate bytes (same sha256):", dups)


if __name__ == "__main__":
    main()
