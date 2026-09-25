#!/usr/bin/env python3
"""state_codec.py — the pure codecs of the tracked state layout (ADR 0001 stage 3, step 7).

Shared by the store (scripts/store.py, store_pg.py, pg_shadow.py) and the deprecated registry
adapters (scripts/registry.py), so neither imports the other for them and no module needs a
lazy-import workaround. Nothing here performs I/O: routing (id -> registry YAML shard, manifest
JSONL shard, prune-ledger bucket), normalization (URLs, titles, slugs), the YAML shard text
(emit, parse, in-place removal), the canonical manifest shard text and the eligibility-policy
schema. Byte formats are exactly what the legacy writers produced; changing any of them changes
tracked bytes and needs an ADR decision.
"""
from __future__ import annotations

import json
import re
import zlib

import yaml

CURATED = "curated.yaml"
# id prefix -> shard file. Anything not matching is hand-curated (curated.yaml).
SHARDS = {
    "oer-": "books.yaml",      # find_books (OAPEN CC-BY books)
    "arc-": "archive.yaml",    # find_archive (pre-1929 public-domain texts)
    "gh-": "github.yaml",      # find_github (repo docs + source code)
    "crawl-": "crawl.yaml",    # crawl_docs (doc-site pages)
    "ost-": "reports.yaml",    # find_osti + find_sources OSTI backend
    "eud-": "deliverables.yaml",  # find_openaire (EU Horizon/H2020 project deliverables)
    "nst-": "nist.yaml",       # find_nist (NIST/NBS technical series via Crossref DOI prefix)
    "pat-": "patents.yaml",    # find_patents (Google Patents sitemap, building/HVAC CPC classes);
                               # base name only — _shard_stem adds country + hash buckets
    "wik-": "wiki.yaml",       # find_wiki (multilingual Wikipedia articles via langlinks/categories)
    "doa-": "doaj.yaml",       # find_doaj (DOAJ open-access articles, all languages)
    "sdz-": "austria.yaml",    # find_sdz (Austrian Stadt/Haus der Zukunft building-research reports, German)
    "kit-": "kitopen.yaml",    # find_kitopen (KIT OAI repository, ddc:690 Bauwesen, German+English)
    "adm-": "ademe.yaml",      # find_ademe (French energy-agency reports, librairie.ademe.fr)
    "jpn-": "japan.yaml",      # find_japan (BRI kenken.go.jp + NILIM research reports, Japanese)
    "ibp-": "ibpsa.yaml",      # find_ibpsa (Building Simulation proceedings)
    "mod-": "modelica.yaml",   # find_modelica_conf (Modelica Conference proceedings, LiU E-Press 10.3384)
    "pur-": "purdue.yaml",     # find_purdue (Purdue e-Pubs Herrick conferences: icec/iracc/ihpbc)
    "zen-": "zenodo.yaml",     # find_zenodo (open CC-licensed records)
    "wbd-": "worldbank.yaml",  # find_worldbank (World Bank Documents & Reports API, open)
    "jrc-": "jrc.yaml",        # find_jrc (EU JRC science-for-policy reports via OpenAIRE, cc-by)
    "guk-": "govuk.yaml",      # find_govuk (UK gov.uk publications via Search/Content APIs, OGL v3)
    "jst-": "jstage.yaml",     # find_jstage (AIJ journals via the J-STAGE search API, Japanese)
    "iag-": "iea.yaml",        # find_iea (IEA agency analysis reports, CC BY 4.0, Azure-blob PDFs).
                               # NOT "iea-": that prefix shadows hand-curated iea-ebc-* ids in
                               # curated.yaml and the pruner ate 15 of them (2026-07-24, repaired).
    "bov-": "nordic.yaml",     # find_boverket (Swedish building authority; shard shared by Nordic sources)
    "sci-": "scielo.yaml",     # find_scielo (SciELO Brazil AEC journals, Portuguese, cc-by)
    "vnd-": "vendor.yaml",     # find_vendor (manufacturer product literature via sitemaps/listings;
                               # config registry/vendors.json, license=open by operator decision 2026-08-28)
    "ojs-": "ojs.yaml",        # find_ojs (CC-BY journals/proceedings on Open Journal Systems, OAI-PMH)
    "esc-": "escholarship.yaml",  # find_escholarship (LBNL + UC Berkeley CBE via eScholarship GraphQL, CC BY/BY-SA/CC0)
    "nlr-": "nlr.yaml",        # find_nlr (National Laboratory of the Rockies, ex-NREL, reports via Pure OAI)
    "ope-": "papers.yaml",     # find_sources OpenAlex backend
    "oa-": "papers.yaml",
    "arx-": "papers.yaml",     # find_sources arXiv backend
}
DISCOVERED_PREFIXES = tuple(SHARDS)  # the pruner's gate: machine-discovered ids
REQUIRED_FIELDS = ("id", "title", "url", "source", "license", "topic", "format")
# Optional metadata is appended gradually by new/updated finders. Existing 100k+ entries stay valid
# and are not mass-rewritten merely because the schema learned a new field.
OPTIONAL_FIELDS = (
    "language", "published_at", "jurisdiction", "document_type", "persistent_id",
    "license_url", "license_evidence", "rights_verified_at",
)
FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS
# Licenses in this set are registry pointers only: their metadata is useful for authorized users,
# but the loader must never fetch their bytes and the manifest must never describe a local payload.
POINTER_ONLY_LICENSES = frozenset({"proprietary-internal"})
CORPUS_FIELDS = ("corpus_path", "corpus_chars", "corpus_sha256", "cleaner_version",
                 "corpus_source_sha256")  # the last: stage 4 step 3 (versioned cleaning only)
ENTRY_RE = re.compile(r"^  - id:\s*['\"]?(.+?)['\"]?\s*$")
_FIELD_RE = re.compile(r"^    \s*\S")  # continuation lines of one entry
# Quality decisions grow independently of the live registry because pruned sources can be
# rediscovered after transient failures. Keep that append-only provenance in stable hash buckets,
# just like the heavy patent veins, so no single publication file approaches GitHub's 100 MiB
# hard limit. The original registry/pruned.jsonl is migration input only.
PRUNE_LEDGER_BUCKETS = 16

# Growing shard families use stable hash buckets. patents-cn.jsonl crossed GitHub's 100MB limit
# at 152MB (2026-08-05), registry/patents.yaml later hit 89MB (2026-08-17), and the first 16-way
# CN split plus the vendor monolith were both on course to cross the 80 MiB safety gate in
# September 2026. crc32 keeps routing stable across runs and platforms and identical between one
# id's registry YAML shard and manifest JSONL shard. Power-of-two refinements split every old
# bucket cleanly, which makes future migrations deterministic.
HASH_BUCKETS = {
    "patents-cn": 64,
    "patents-us": 8,
    "vendor": 16,
}


# --- routing --------------------------------------------------------------------------------------

def discovered(sid: str) -> bool:
    return sid.startswith(DISCOVERED_PREFIXES)


def _bucketed_stem(stem: str, sid: str) -> str:
    n = HASH_BUCKETS.get(stem)
    return f"{stem}-{zlib.crc32(sid.encode()) % n}" if n else stem


def shard_stem(sid: str) -> str | None:
    """Shard stem for a machine-discovered id (None = hand-curated): the SHARDS route, except
    patents split further by publication country (pat-us…/pat-cn…), then any growing family in
    HASH_BUCKETS is split again so no file approaches GitHub's 100MB limit."""
    for prefix, fname in SHARDS.items():
        if sid.startswith(prefix):
            stem = fname.rsplit(".", 1)[0]
            if stem == "patents":
                m = re.match(r"pat-([a-z]{2})", sid)
                if m:
                    stem = f"patents-{m.group(1)}"
            return _bucketed_stem(stem, sid)
    return None


def shard_filename(sid: str) -> str:
    """The registry YAML shard an id routes to."""
    stem = shard_stem(sid)
    return f"{stem}.yaml" if stem else CURATED


def manifest_shard(sid: str) -> str:
    """Manifest shard stem for an id — identical to the id's registry shard stem (curated.yaml
    rows land in curated.jsonl), so one doc's YAML and JSONL shards always pair up."""
    return shard_stem(sid) or "curated"


def prune_ledger_name(sid: str) -> str:
    """Stable decision-ledger shard filename for a pruned source id."""
    if not sid:
        raise ValueError("prune ledger row requires a non-empty id")
    return f"pruned-{zlib.crc32(sid.encode()) % PRUNE_LEDGER_BUCKETS}.jsonl"


# --- normalization --------------------------------------------------------------------------------

def slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")


def norm(s: str) -> str:
    """Title normal form used by every dedup key."""
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def normalize_url(u: str) -> str:
    """URL normal form used by every dedup key and the blocklist."""
    return (u or "").strip().rstrip("/")


def uniquify_ids(entries: list[dict], reserved: set) -> None:
    """Suffix -2/-3/… onto any id already in `reserved` (registry + manifest ids) or repeated
    within the batch. Mutates entries and grows `reserved`. Truncated title slugs WILL collide
    across runs — this is the guard that keeps a collision from silently overwriting a doc."""
    for h in entries:
        base, i = h["id"], 2
        while h["id"] in reserved:
            h["id"] = f"{base[:50]}-{i}"
            i += 1
        reserved.add(h["id"])


# --- registry YAML shard text ---------------------------------------------------------------------

# PyYAML's pure-Python SafeLoader parses the 251MB registry in ~210 s; libyaml's CSafeLoader does
# the same in ~42 s (measured 2026-08-28 over all 49 shards, resulting objects identical). Every
# registry-shard parse goes through here so the whole loop shares that one decision; the fallback
# keeps hosts without the C extension correct, only slower.
YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def parse_yaml(text: str):
    """yaml.safe_load semantics, C-accelerated when libyaml is available."""
    return yaml.load(text, Loader=YAML_LOADER)


def emit_entry(e: dict) -> str:
    d = yaml.safe_dump(
        [{k: e[k] for k in FIELDS if k in e and e[k] not in (None, "")}],
        sort_keys=False,
        allow_unicode=True,
    )
    return "".join(("  " + ln + "\n") if ln else "\n" for ln in d.splitlines())


def shard_header(stem: str) -> str:
    """Opening lines of a newly created machine shard."""
    return (f"# {stem} — machine-appended shard (see AGENTS.md); "
            f"prune_corpus edits it in place\nsources:\n")


def entry_span(lines: list[str], i: int) -> int:
    """lines[i] is an entry's `  - id:` line; return one past the entry's last line. Blank lines
    are part of the entry when more field lines follow — yaml allows blank lines INSIDE a quoted
    multi-line scalar (a real OSTI title bit us: the naive parser cut the entry in half)."""
    j = i + 1
    while j < len(lines):
        if _FIELD_RE.match(lines[j]):
            j += 1
        elif lines[j].strip() == "" and j + 1 < len(lines) and _FIELD_RE.match(lines[j + 1]):
            j += 1
        else:
            break
    return j


def remove_ids_from_text(old_text: str, drop: set, label: str = "shard") -> tuple[str, int]:
    """Delete entries by id from one shard's text IN PLACE, preserving everything else byte for
    byte (comments, hand formatting): (validated new text, removed count). Position-agnostic;
    validates (parses, exactly the dropped ids gone, count arithmetic holds) and raises on any
    mismatch. Pure — no I/O."""
    lines = old_text.splitlines(keepends=True)
    out: list[str] = []
    removed = 0
    i = 0
    while i < len(lines):
        m = ENTRY_RE.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        j = entry_span(lines, i)
        if m.group(1) in drop:
            removed += 1
        else:
            out.extend(lines[i:j])
        i = j
    if not removed:
        return old_text, 0
    new_text = "".join(out)
    entries = parse_yaml(new_text).get("sources") or []
    old_count = len(parse_yaml(old_text).get("sources") or [])
    leftover = {e["id"] for e in entries} & drop
    if leftover:
        raise RuntimeError(f"{label}: failed to remove {len(leftover)} ids, "
                           f"e.g. {sorted(leftover)[:3]}")
    if len(entries) != old_count - removed:
        raise RuntimeError(f"{label}: count mismatch {old_count} - {removed} != {len(entries)}")
    return new_text, removed


# --- manifest shard text --------------------------------------------------------------------------

def manifest_shard_text(group) -> str:
    """Canonical text of one manifest shard: rows sorted by (topic, id) for stable diffs."""
    return "".join(json.dumps(r, ensure_ascii=False) + "\n"
                   for r in sorted(group, key=lambda x: (x.get("topic", ""), x["id"])))


# --- eligibility policy schema --------------------------------------------------------------------

def validate_eligibility(data: object) -> list[str]:
    """Return schema errors for registry/eligibility.json."""
    if not isinstance(data, dict):
        return ["top level must be an object"]
    errors: list[str] = []
    if data.get("version") != 1:
        errors.append("version must be 1")
    restrictions = data.get("restrictions")
    if not isinstance(restrictions, dict):
        errors.append("restrictions must be an object")
        return errors
    for name, rule in restrictions.items():
        label = f"restrictions.{name}"
        if not name:
            errors.append("restriction names must be non-empty")
        if not isinstance(rule, dict):
            errors.append(f"{label} must be an object")
            continue
        if rule.get("status") != "restricted":
            errors.append(f"{label}.status must be 'restricted'")
        match = rule.get("match")
        if not isinstance(match, dict) or not match:
            errors.append(f"{label}.match must be a non-empty object")
        else:
            unknown = sorted(set(match) - {"id_prefix", "source"})
            if unknown:
                errors.append(f"{label}.match has unknown selector(s): {', '.join(unknown)}")
            for key, value in match.items():
                if not isinstance(value, str) or not value:
                    errors.append(f"{label}.match.{key} must be a non-empty string")
        backends = rule.get("backends")
        if (not isinstance(backends, list) or not backends
                or any(not isinstance(v, str) or not v for v in backends)):
            errors.append(f"{label}.backends must be a non-empty string list")
        for key in ("reason", "decided_at"):
            if not isinstance(rule.get(key), str) or not rule[key]:
                errors.append(f"{label}.{key} must be a non-empty string")
        urls = rule.get("evidence_urls")
        if (not isinstance(urls, list) or not urls
                or any(not isinstance(url, str) or not url.startswith("https://") for url in urls)):
            errors.append(f"{label}.evidence_urls must be a non-empty HTTPS URL list")
    return errors


def restriction_for(entry: dict, restrictions: dict[str, dict]) -> tuple[str, dict] | None:
    """Return the first committed restriction matching a registry or manifest record."""
    for name, rule in restrictions.items():
        match = rule["match"]
        if "id_prefix" in match and not str(entry.get("id", "")).startswith(match["id_prefix"]):
            continue
        if "source" in match and entry.get("source") != match["source"]:
            continue
        return name, rule
    return None


def is_training_eligible(entry: dict, restrictions: dict[str, dict]) -> bool:
    """Whether an entry may produce fetched and training-ready payload bytes."""
    return (entry.get("license") not in POINTER_ONLY_LICENSES
            and restriction_for(entry, restrictions) is None)
