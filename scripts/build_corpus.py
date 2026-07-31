#!/usr/bin/env python3
"""build_corpus.py — fetch & verify the corpus from the registry.

Reads the registry (registry/*.yaml), downloads each source into raw/<source>/<id>.<ext>, extracts
plain text into text/<id>.md, and records everything (incl. sha256 and quality metrics) in the
sharded manifest (manifest/<shard>.jsonl, all I/O via registry.py). The committed manifest is the
REPRODUCIBILITY record: a fresh clone runs this to fetch the SAME bytes, and the run reports how many
reproduced exactly (sha256 matches the manifest) vs drifted (the source changed upstream) vs new.

  python scripts/build_corpus.py            # fetch missing; report reproduced / drifted / new vs manifest
  python scripts/build_corpus.py --force    # re-fetch everything
  python scripts/build_corpus.py --only controls_bas
  python scripts/build_corpus.py --workers 16 --extract-workers 8
  python scripts/build_corpus.py --verify   # no download: re-hash local raw files against the manifest

Idempotent, dedups identical bytes by sha256, checkpoints the manifest every 25 fetches. Downloads
are fairly interleaved across hosts with conservative host-specific request caps; extraction runs
in a separate process pool so CPU work never holds a network slot. raw/ and text/ are git-ignored;
respect each source's license (see README.md).

text/ is the VERBATIM extraction and stays that way — the cleaned, training-ready copy is built
from it by the next stage, scripts/clean_corpus.py, into corpus/. Keeping the two separate is what
lets an improved cleaning ruleset be re-applied in minutes instead of re-extracting 104k PDFs.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import (
    FIRST_COMPLETED,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from pathlib import Path
from urllib.parse import urlparse

import requests

import quality
import registry

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
RAW = HERE / "raw"
TEXT = HERE / "text"
# Browser-like UA: publisher / repository bot-walls (eScholarship, Frontiers, PMC, …) 403 a generic
# UA even for openly-licensed (CC-BY / OA) PDFs we're entitled to fetch. (MDPI sits behind Cloudflare
# and still blocks; those need a headless browser — skipped for now.)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = 45
# plain-text source formats (GitHub READMEs / docs, .rst, etc.): stored verbatim, no parsing.
TEXT_FORMATS = {"md", "rst", "txt"}
# Politeness: never more than this many in-flight requests against one host, however many workers.
# Hosts listed below have handled four concurrent public-document transfers reliably.  Parsing is
# deliberately outside these limits: a 100MB PDF must not hold a network slot while pypdf works.
PER_HOST = 2
HOST_CONCURRENCY: dict[str, int] = {
    "www.osti.gov": 4,
    "library.oapen.org": 4,
    "patents.google.com": 4,
    "documents.worldbank.org": 4,
    "documents1.worldbank.org": 4,
    "www.scielo.br": 4,
}
# politeness overrides for hosts that need them (currently none). HOST_DELAY: minimum seconds
# between request STARTS against a host, enforced under its semaphore — for hosts that tarpit at
# volume. HOST_UA: per-host User-Agent override, applied to both the requests call and the curl
# fallback — for hosts that block the spoofed-browser UA but pass an honest bot UA. Before adding
# a host here to work around its wall, check its ToS/robots.txt — a wall is sometimes the host
# enforcing terms we must respect (nrc-publications.canada.ca, 07-12: "systematic downloading is
# not permitted" — that vein was reverted, NO-GO).
#
# Same verdict, 07-28 — NO-GO, do not rebuild these:
#   erdc-library.erdc.dren.mil  US Army Corps ERDC Library. Content is public-domain and a
#     find_erdc.py backend worked (400 entries appended, then REVERTED). But the 403 it serves a
#     python UA is a wall, and robots.txt names the AI/dataset crawlers — ClaudeBot, Claude-Web,
#     GPTBot, PerplexityBot, img2dataset, Google-Extended — with `Disallow: /`. An LLM-training
#     corpus is precisely what that opts out of; public-domain content does not override the
#     operator's stated access policy.
#   www.scielo.cl · www.scielo.org.pe  SciELO Chile / Peru: robots.txt `User-agent: * Disallow: /`
#     (Peru additionally names anthropic-ai and Claude-Web). www.scielo.br is `Allow: /` and IS
#     mined — check each SciELO national host separately, they do not share a policy.
HOST_DELAY: dict[str, float] = {
    "www.jstage.jst.go.jp": 2.0,  # J-STAGE throttles bulk fetches; nightly ~00:00 JST 503 window
    "www.boverket.se": 10.0,      # robots.txt Crawl-delay: 10 — respect it
}
HOST_UA: dict[str, str] = {}


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "missing"


def _binary_version(name: str) -> str:
    if not shutil.which(name):
        return "missing"
    try:
        out = subprocess.run(
            [name, "-v"], capture_output=True, text=True, timeout=5,
        )
        line = (out.stdout or out.stderr).splitlines()[0]
        return line.strip().replace(";", ",")
    except Exception:
        return "unknown"


# Stored on newly extracted rows. Old rows remain valid and gain it only when re-extracted.
EXTRACTOR_VERSION = (
    f"build_corpus/2;pypdf={_version('pypdf')};beautifulsoup4={_version('beautifulsoup4')};"
    f"pdftotext={_binary_version('pdftotext')}"
)

_host_sems: dict[str, threading.BoundedSemaphore] = {}
_host_sems_lock = threading.Lock()
_host_next: dict[str, float] = {}
_host_next_lock = threading.Lock()


def _host_sem(url: str) -> threading.BoundedSemaphore:
    host = urlparse(url).netloc.lower()
    with _host_sems_lock:
        limit = HOST_CONCURRENCY.get(host, PER_HOST)
        return _host_sems.setdefault(host, threading.BoundedSemaphore(limit))


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def clean_text(s: str) -> str:
    """Drop lone surrogates etc. that pypdf sometimes emits — they crash write_text()."""
    return s.encode("utf-8", "replace").decode("utf-8")


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

    try:
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for i, page in enumerate(reader.pages):
            try:
                parts.append(page.extract_text() or "")
            except Exception as e:  # keep going on a bad page
                parts.append(f"[page {i} extract error: {e}]")
        txt = "\n\n".join(parts).strip()
    except Exception:  # broken xref/trailer — pypdf can't even open it; poppler usually can
        txt = ""
    # pdftotext (poppler) rescues three pypdf failure classes: (a) legacy scans whose OCR text
    # layer has no space glyphs ("ThermalAnalysisofEffect...", 259 NBS docs wrongly pruned 07-09),
    # (b) CID-keyed CJK fonts where pypdf extracts NOTHING (413 Japanese NILIM PDFs, 07-10), and
    # (c) subset CJK fonts where pypdf extracts PLENTY of text but maps glyphs to WRONG codepoints
    # (2019 AIJ kouzou PDFs, 07-23: "⪏ⅆ⿕そCFT..." — never trips the length/glue checks). For (c)
    # the tell is a CJK-ish doc with a depressed alpha ratio; the arbiter is which extractor
    # yields more true-CJK chars.
    if shutil.which("pdftotext"):
        if len(txt) < 500 or _word_glued(txt):
            alt = _pdftotext(data)
            if len(alt) > max(len(txt), 400) and not _word_glued(alt):
                return alt
        else:
            head = txt[:20_000]
            cjk = len(quality.CJK.findall(head))
            alpha = sum(c.isalpha() for c in head) / max(len(head), 1)
            if cjk > 50 and alpha < 0.60:
                alt = _pdftotext(data)
                if len(quality.CJK.findall(alt[:20_000])) > cjk * 1.3:
                    return alt
    return txt


def _word_glued(t: str) -> bool:
    head = t[:20_000]
    if not head:
        return False
    if len(quality.CJK.findall(head)) / len(head) > 0.10:
        return False  # CJK scripts don't space-separate — that's not gluing
    return head.count(" ") / len(head) < 0.05  # spaced prose runs ~15-18%


def _pdftotext(data: bytes) -> str:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        out = subprocess.run(["pdftotext", f.name, "-"], capture_output=True, timeout=300)
    return out.stdout.decode("utf-8", "ignore").strip() if out.returncode == 0 else ""


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
    return {r["id"]: r for r in registry.load_manifest_rows()}


def write_manifest(rows: dict) -> None:
    registry.write_manifest_rows(rows.values())


def _fetch_ec_deliverable(url: str) -> requests.Response:
    """EC 'Documents download module' (Horizon project deliverables, eud- ids): the stable public
    URL returns a JS interstitial whose window.location points at a session-bound tokenized URL —
    follow it with the same cookie jar to get the actual PDF."""
    with requests.Session() as s:
        s.headers.update({"User-Agent": UA})
        first = s.get(url, timeout=TIMEOUT, allow_redirects=True)
        if not first.headers.get("Content-Type", "").startswith("text/html"):
            return first
        m = re.search(r"window\.location='(https://ec\.europa\.eu[^']+)'", first.text)
        if not m:
            return first
        return s.get(m.group(1), timeout=TIMEOUT, allow_redirects=True)


def _fetch_publications_gc_ca(url: str) -> requests.Response:
    """Fetch archived Government of Canada PDFs.

    publications.gc.ca redirects older documents to a bilingual archive notice.  Continuing to
    the publication requires the notice's session cookie and Referer; a stateless retry receives
    the notice forever and fails the PDF magic-byte check.
    """
    with requests.Session() as s:
        s.headers.update({
            "User-Agent": UA,
            "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
        })
        first = s.get(url, timeout=TIMEOUT, allow_redirects=True)
        if first.content.startswith(b"%PDF-"):
            return first
        if "/site/archivee-archived.html" not in first.url:
            return first
        return s.get(url, headers={"Referer": first.url},
                     timeout=TIMEOUT, allow_redirects=True)


def _new_record(src: dict) -> tuple[dict, str]:
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
        # corpus_path / corpus_chars are written by the next stage (clean_corpus.py), not here.
        "corpus_path": None, "corpus_chars": 0,
        "error": None, "fetched_at": None,
    }
    for key in registry.OPTIONAL_FIELDS:
        if src.get(key) not in (None, ""):
            rec[key] = src[key]
    return rec, ext


def _wait_for_host(host: str) -> None:
    delay = HOST_DELAY.get(host)
    if not delay:
        return
    with _host_next_lock:
        start = max(time.monotonic(), _host_next.get(host, 0.0))
        _host_next[host] = start + delay
    if (wait := start - time.monotonic()) > 0:
        time.sleep(wait)


def download_one(src: dict) -> dict:
    """Download and persist original bytes, holding a host slot for network I/O only."""
    rec, ext = _new_record(src)
    sid = rec["id"]
    fmt = rec["format"]
    source = rec["source"]
    host = urlparse(src["url"]).netloc.lower()
    ua = HOST_UA.get(urlparse(src["url"]).netloc.lower(), UA)
    try:
        with _host_sem(src["url"]):
            _wait_for_host(host)
            if "ec.europa.eu/research/participants/documents/downloadPublic" in src["url"]:
                resp = _fetch_ec_deliverable(src["url"])
            elif (host.removeprefix("www.") == "publications.gc.ca"
                  and "/collections/" in urlparse(src["url"]).path):
                resp = _fetch_publications_gc_ca(src["url"])
            else:
                resp = requests.get(
                    src["url"],
                    headers={
                        "User-Agent": ua,
                        "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
                    },
                    timeout=TIMEOUT,
                    allow_redirects=True,
                )
            rec["http_status"] = resp.status_code
            if resp.status_code in (403, 410, 429, 503):
                # WAFs (Akamai/Cloudflare/Google) block the python client's TLS fingerprint but
                # pass curl's. Only accept a fallback with the expected content.
                # Do not capture curl stdout through a pipe.  Extraction workers are spawned
                # while downloads are active, and a fork can inherit the pipe's write end; if
                # that happens communicate() never observes EOF after curl exits.  A temporary
                # file also avoids buffering large patent HTML responses in a pipe.
                with tempfile.TemporaryFile() as curl_body:
                    out = subprocess.run(
                        ["curl", "-sSL", "--max-time", str(TIMEOUT), "-A", ua, src["url"]],
                        stdout=curl_body,
                        stderr=subprocess.DEVNULL,
                        timeout=TIMEOUT + 15,
                    )
                    curl_body.seek(0)
                    body = curl_body.read() if out.returncode == 0 else b""
                good = len(body) > 512 and (
                    body[:5] == b"%PDF-" if fmt == "pdf"
                    else (
                        b"automated queries" not in body[:4000]
                        and b"unusual traffic" not in body[:4000]
                        and b"Too many requests" not in body[:4000]
                        and b"too many requests" not in body[:4000]
                    )
                )
                if good:
                    data, rec["http_status"] = body, 200
                else:
                    resp.raise_for_status()
                    data = resp.content
            else:
                resp.raise_for_status()
                data = resp.content
        if fmt == "pdf" and not data.startswith(b"%PDF-"):
            # a 200 that isn't a PDF is a WAF interstitial / captcha / error page — without this
            # check it lands in the corpus as an ok row with 0 text chars (IBPSA sgcaptcha, 07-09)
            rec["error"] = f"not-a-pdf (got {data[:12]!r})"
            return rec
        rec["sha256"] = sha256_bytes(data)
        rec["bytes"] = len(data)

        raw_dir = RAW / source
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"{sid}.{ext}"
        raw_path.write_bytes(data)
        rec["raw_path"] = str(raw_path.relative_to(HERE))
        # Absolute process-local paths are removed by extract_downloaded before the row can reach
        # the manifest. Carrying them makes the extraction stage safe under both fork and spawn.
        rec["_raw_file"] = str(raw_path)
        rec["_root"] = str(HERE)
        rec["_text_dir"] = str(TEXT)
    except Exception as e:
        rec["error"] = str(e)
    return rec


def extract_downloaded(rec: dict) -> dict:
    """Extract one already-downloaded record. Safe to run in a separate process."""
    sid = rec["id"]
    try:
        root = Path(rec.pop("_root", HERE))
        text_dir = Path(rec.pop("_text_dir", TEXT))
        raw_path = Path(rec.pop("_raw_file", root / rec["raw_path"]))
        data = raw_path.read_bytes()
        try:
            txt = clean_text(extract_for(rec["format"], data))
        except Exception as e:
            txt, rec["error"] = "", f"text-extract: {e}"
        if txt:
            text_dir.mkdir(parents=True, exist_ok=True)
            header = (f"# {rec['title']}\n\n"
                      f"source: {rec['url']}\nlicense: {rec['license']}\n"
                      f"topic: {rec['topic']}\n\n---\n\n")
            tp = text_dir / f"{sid}.md"
            rendered = header + txt
            tp.write_text(rendered)
            rec["text_path"] = str(tp.relative_to(root))
            rec["text_chars"] = len(txt)
            rec["text_sha256"] = sha256_bytes(rendered.encode())
            rec["extractor_version"] = EXTRACTOR_VERSION
            rec["quality"] = quality.metrics(txt)  # prune verdicts read this, not the file

        rec["status"] = "ok"
        rec["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    except Exception as e:
        rec["error"] = str(e)
    return rec


def reuse_extraction(rec: dict, template: dict) -> dict:
    """Reuse text for identical bytes while rendering this record's own provenance header."""
    root = Path(rec.pop("_root", HERE))
    text_dir = Path(rec.pop("_text_dir", TEXT))
    rec.pop("_raw_file", None)
    template_path = template.get("text_path")
    if template_path and (root / template_path).exists():
        txt = quality.body((root / template_path).read_text())
        if txt:
            text_dir.mkdir(parents=True, exist_ok=True)
            header = (
                f"# {rec['title']}\n\n"
                f"source: {rec['url']}\nlicense: {rec['license']}\n"
                f"topic: {rec['topic']}\n\n---\n\n"
            )
            rendered = header + txt
            target = text_dir / f"{rec['id']}.md"
            target.write_text(rendered)
            rec["text_path"] = str(target.relative_to(root))
            rec["text_chars"] = len(txt)
            rec["text_sha256"] = sha256_bytes(rendered.encode())
            rec["quality"] = template.get("quality") or quality.metrics(txt)
            if template.get("extractor_version"):
                rec["extractor_version"] = template["extractor_version"]
    rec["status"] = "ok"
    rec["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return rec


def fetch_one(src: dict) -> dict:
    """Compatibility path for callers/tests that fetch and extract one document synchronously."""
    rec = download_one(src)
    return extract_downloaded(rec) if rec.get("raw_path") else rec


def fair_sources(srcs: list[dict]) -> list[dict]:
    """Round-robin sources by host so semaphore waiters cannot starve unrelated hosts."""
    queues: dict[str, deque] = defaultdict(deque)
    for src in srcs:
        queues[urlparse(src["url"]).netloc.lower()].append(src)
    ordered = []
    hosts = deque(queues)
    while hosts:
        host = hosts.popleft()
        ordered.append(queues[host].popleft())
        if queues[host]:
            hosts.append(host)
    return ordered


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-fetch everything")
    ap.add_argument("--workers", type=int, default=16,
                    help="parallel downloads (host-specific caps still apply; default 16)")
    ap.add_argument(
        "--extract-workers",
        type=int,
        default=min(8, max(1, os.cpu_count() or 1)),
        help="separate PDF/text extraction processes (default min(8, CPU count))",
    )
    ap.add_argument("--only", default="", help="comma-separated topics to limit to")
    ap.add_argument("--reextract", action="store_true",
                    help="re-extract text from existing raw files; no download")
    ap.add_argument("--verify", action="store_true",
                    help="re-hash local raw files against the manifest sha256; no download")
    args = ap.parse_args()
    only = {t.strip() for t in args.only.split(",") if t.strip()}

    srcs = registry.load_entries()
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
                txt = clean_text(extract_for(fmt, data))
            except Exception as e:
                txt, r["error"] = "", f"reextract: {e}"
            if txt:
                header = (f"# {r['title']}\n\nsource: {r['url']}\n"
                          f"license: {r['license']}\ntopic: {r['topic']}\n\n---\n\n")
                (TEXT / f"{r['id']}.md").write_text(header + txt)
                r["text_path"] = f"text/{r['id']}.md"
                r["text_chars"] = len(txt)
                r["text_sha256"] = sha256_bytes((header + txt).encode())
                r["extractor_version"] = EXTRACTOR_VERSION
                r["quality"] = quality.metrics(txt)
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
    extraction_templates = {
        row["sha256"]: row
        for row in manifest.values()
        if (
            row.get("sha256")
            and row.get("status") == "ok"
            and row.get("text_path")
            and (HERE / row["text_path"]).exists()
        )
    }
    repro = drift = new = done = 0
    print(
        f"sources: {len(srcs)} total, {len(todo)} to fetch "
        f"({'forced' if args.force else 'missing only'}, {args.workers} download workers, "
        f"{args.extract_workers} extract workers)"
    )

    def record_result(rec: dict) -> None:
        nonlocal done, repro, drift, new
        done += 1
        manifest[rec["id"]] = rec
        if rec["status"] == "ok":
            exp = expected.get(rec["id"])
            tag = "reproduced" if exp == rec["sha256"] else ("DRIFTED" if exp else "new")
            repro += exp == rec["sha256"]
            drift += bool(exp) and exp != rec["sha256"]
            new += not exp
            print(
                f"[{done}/{len(todo)}] {rec['id']}  ok  {rec['bytes'] // 1024}KB  "
                f"{rec['text_chars']} chars  [{tag}]",
                flush=True,
            )
        else:
            print(
                f"[{done}/{len(todo)}] {rec['id']}  FAIL http={rec['http_status']} "
                f"{rec.get('error')}",
                flush=True,
            )
        if done % 25 == 0:
            write_manifest(manifest)  # checkpoint so an interrupted run loses <25 extractions

    ordered = fair_sources(todo)
    with (
        ThreadPoolExecutor(max_workers=max(1, args.workers)) as downloads,
        ProcessPoolExecutor(max_workers=max(1, args.extract_workers)) as extractors,
    ):
        download_futures = {downloads.submit(download_one, src) for src in ordered}
        extract_futures = set()
        extract_sha = {}
        waiting_by_sha: dict[str, list[dict]] = defaultdict(list)
        # Drain both stages continuously: extraction overlaps downloads, progress remains visible,
        # and the manifest checkpoint still bounds hard-kill rework to fewer than 25 completions.
        while download_futures or extract_futures:
            completed, _ = wait(
                download_futures | extract_futures,
                return_when=FIRST_COMPLETED,
            )
            for future in completed:
                if future in download_futures:
                    download_futures.remove(future)
                    rec = future.result()
                    if rec.get("raw_path"):
                        digest = rec["sha256"]
                        if digest in extraction_templates:
                            record_result(reuse_extraction(rec, extraction_templates[digest]))
                        elif digest in extract_sha.values():
                            waiting_by_sha[digest].append(rec)
                        else:
                            extract_future = extractors.submit(extract_downloaded, rec)
                            extract_futures.add(extract_future)
                            extract_sha[extract_future] = digest
                    else:
                        record_result(rec)
                else:
                    extract_futures.remove(future)
                    digest = extract_sha.pop(future)
                    extracted = future.result()
                    extraction_templates[digest] = extracted
                    record_result(extracted)
                    for duplicate in waiting_by_sha.pop(digest, []):
                        record_result(reuse_extraction(duplicate, extracted))
    if todo:
        write_manifest(manifest)

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
