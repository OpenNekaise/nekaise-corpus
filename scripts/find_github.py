#!/usr/bin/env python3
"""find_github.py — discover building-energy text from GitHub repos (corpus growth).

The building-simulation world lives on GitHub (Modelica Buildings, EnergyPlus, OpenStudio, ResStock,
…) and much of it is openly licensed with rich prose docs. This backend walks a CURATED list of
permissive repos, enumerates their human-readable text files (README* / docs/*.md / *.rst) via one
Git-Trees API call per repo, and PROPOSES ready-to-paste registry entries pointing at
raw.githubusercontent.com (which build_corpus.py now fetches as plain text).

Curated on purpose: GitHub's license auto-detection is unreliable (many building repos use custom
BSD-style licenses the API reports as null), so we hardcode the license per repo and only list ones
that are clearly redistributable. Depth is docs + READMEs only — high signal, low noise.

    python scripts/find_github.py                 # propose entries for every curated repo
    python scripts/find_github.py --repo lbl-srg/modelica-buildings   # just one repo
    python scripts/find_github.py --append        # append into the registry, then load + prune

No key needed (unauthenticated GitHub API = 60 req/hr, ~2 calls/repo). Set GITHUB_TOKEN / GH_TOKEN to
raise the limit.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import requests
import yaml

import dedup
import ops
import registry
from store import Prefix, Table

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
API = "https://api.github.com"
# Completed opt-in doc-markup passes, per source bucket and doc kind: {"gh_radiance": {"man":
# "2026-09-24"}}. Recorded only for a successful --append walk, INCLUDING passes that found no file
# of that kind, so an empty pass is never re-walked. It is the store's control document
# github_passes.json: in a round the finder stages it in its proposal and run_round writes it
# through the store with the merge; a failed round's snapshot rolls it back with the proposals.
PASSES_DOC = "github_passes.json"
PASSES = registry.REG_DIR / PASSES_DOC

# Curated, clearly-permissive (BSD / MIT / Apache) building-energy repos. Extend freely.
#   repo     owner/name on github
#   license  registry license tag (permissive BSD/MIT/Apache -> "open")
#   topic    one of our five corpus topics
#   include  (optional) only keep paths under these prefixes — bounds huge repos like EnergyPlus
REPOS = [
    # caps raised 2026-08-05 (EnergyPlus/Modelica vein): the .mo libraries carry the richest
    # embedded HTML documentation in building simulation — collect them in full, bounded only
    # against runaway repo growth.
    {"repo": "lbl-srg/modelica-buildings", "license": "open", "topic": "building_energy",
     "include": ["Buildings/"], "code": ["mo"], "cap": 5000},
    {"repo": "ibpsa/modelica-ibpsa", "license": "open", "topic": "building_energy",
     "include": ["IBPSA/"], "code": ["mo"], "cap": 2200},
    {"repo": "lbl-srg/BuildingsPy", "license": "open", "topic": "building_energy"},
    # NREL's GitHub org moved to NatLabRockies (2026): the API answers NREL/<repo> with
    # 301 Moved Permanently, so every entry names the new owner. Source buckets are keyed by repo
    # NAME (gh_<name>), so the move does not re-walk repos already ingested under NREL/.
    {"repo": "NatLabRockies/EnergyPlus", "license": "open", "topic": "building_energy",
     "include": ["doc/", "design/", "README"]},
    {"repo": "NatLabRockies/OpenStudio", "license": "open", "topic": "standards_protocols"},
    {"repo": "NatLabRockies/openstudio-standards", "license": "open",
     "topic": "standards_protocols"},
    {"repo": "NatLabRockies/resstock", "license": "open", "topic": "building_energy"},
    {"repo": "NatLabRockies/ComStock", "license": "open", "topic": "building_energy"},
    {"repo": "NatLabRockies/OpenStudio-HPXML", "license": "open", "topic": "building_energy"},
    # Radiance Software License v2.0 (BSD-3 style): master/License.txt. The DEFAULT branch
    # `cvsimport` holds only CI scripts; the sources and docs live on master. Docs are troff man
    # pages (doc/man, ~150), the -ms reference manual doc/ray.1 and plain-text notes.
    # doc/filefmts.ms / *.html duplicate filefmts.md / ray.1, doc/ps + doc/pdf are binaries.
    {"repo": "LBNL-ETA/Radiance", "license": "open", "topic": "building_energy",
     "branch": "master", "include": ["doc/", "README"], "docs": ["man", "text"],
     "exclude": ["doc/ps/", "doc/pdf/", "doc/filefmts.ms"], "cap": 220},
    {"repo": "CoolProp/CoolProp", "license": "open", "topic": "equipment_systems"},
    # --- round 7: solar / PV / thermal-systems (Python domain code + rich docs) ---
    {"repo": "pvlib/pvlib-python", "license": "open", "topic": "equipment_systems"},
    {"repo": "NatLabRockies/pysam", "license": "open", "topic": "equipment_systems"},
    {"repo": "NatLabRockies/bifacial_radiance", "license": "open", "topic": "equipment_systems"},
    {"repo": "NatLabRockies/floris", "license": "open", "topic": "equipment_systems"},
    {"repo": "oemof/tespy", "license": "open", "topic": "equipment_systems"},
    {"repo": "NatLabRockies/ssc", "license": "open", "topic": "equipment_systems",
     "include": ["ssc/", "shared/", "README"]},
    # --- round 7: Modelica building/HVAC/thermal libraries (pull .mo domain code, bounded) ---
    {"repo": "open-ideas/IDEAS", "license": "open", "topic": "building_energy",
     "include": ["IDEAS/", "docs/", "README"], "code": ["mo"], "cap": 3000},
    {"repo": "RWTH-EBC/AixLib", "license": "open", "topic": "equipment_systems",
     "include": ["AixLib/", "docs/", "README"], "code": ["mo"], "cap": 4000},
    {"repo": "modelica/ModelicaStandardLibrary", "license": "open", "topic": "equipment_systems",
     "include": ["Modelica/", "README"], "code": ["mo"], "cap": 2000},
    {"repo": "UdK-VPT/BuildingSystems", "license": "open", "topic": "building_energy",
     "include": ["BuildingSystems/", "README"], "code": ["mo"], "cap": 2200},
    # --- 2026-08-05 EnergyPlus/Modelica/OpenModelica vein ---
    # OpenModelica User's Guide comes in via the rendered-site crawl (crawl_docs), not the repo's
    # .rst sources, so the corpus doesn't hold the same text twice.
    {"repo": "OpenModelica/OMPython", "license": "open", "topic": "controls_bas"},
    {"repo": "modelica/fmi-standard", "license": "cc-by-sa", "topic": "standards_protocols",
     "include": ["docs/", "README"], "code": ["adoc"], "cap": 80},
    {"repo": "queraltab/Greenhouses-Library", "license": "open", "topic": "equipment_systems",
     "code": ["mo"], "cap": 120},
    # --- round 7: whole-building / urban building energy modeling ---
    {"repo": "RWTH-EBC/TEASER", "license": "open", "topic": "building_energy"},
    {"repo": "RWTH-EBC/ebcpy", "license": "open", "topic": "building_energy"},
    {"repo": "NatLabRockies/OCHRE", "license": "open", "topic": "building_energy"},
    {"repo": "architecture-building-systems/CityEnergyAnalyst", "license": "open",
     "topic": "building_energy", "include": ["cea/", "docs/", "README"]},
    # --- round 7: energy-system / power-system / techno-economic modeling ---
    {"repo": "oemof/oemof-solph", "license": "open", "topic": "building_energy"},
    {"repo": "calliope-project/calliope", "license": "open", "topic": "building_energy"},
    {"repo": "PyPSA/PyPSA", "license": "open", "topic": "building_energy"},
    {"repo": "OSeMOSYS/OSeMOSYS", "license": "open", "topic": "building_energy"},
    {"repo": "NatLabRockies/REopt.jl", "license": "open", "topic": "building_energy"},
    {"repo": "e2nIEE/pandapower", "license": "open", "topic": "equipment_systems",
     "include": ["pandapower/", "doc/", "README"]},
    {"repo": "gridlab-d/gridlab-d", "license": "open", "topic": "equipment_systems",
     "include": ["README", "documents/"]},
    # --- round 7: controls / BAS / co-simulation / test frameworks ---
    {"repo": "ibpsa/project1-boptest", "license": "open", "topic": "controls_bas",
     "include": ["README", "docs/", "testcases/"]},
    {"repo": "VOLTTRON/volttron", "license": "open", "topic": "controls_bas"},
    {"repo": "GMLC-TDC/HELICS", "license": "open", "topic": "controls_bas"},
    {"repo": "bsl546/energym", "license": "open", "topic": "controls_bas"},
    # --- round 7: standards / metadata schemas ---
    {"repo": "BrickSchema/Brick", "license": "open", "topic": "standards_protocols"},
    # BSD-style terms: retain notices; no endorsement. Clause 4 restricts BuildingSync trademark
    # use in derivative distributions: https://github.com/BuildingSync/schema/blob/develop-v2/LICENSE.md
    {"repo": "BuildingSync/schema", "license": "open", "topic": "standards_protocols",
     "include": ["docs/", "README"], "cap": 20},
    # --- round 7 built-environment: structural analysis / FEA (some pull pedagogical .py code) ---
    {"repo": "JWock82/Pynite", "license": "open", "topic": "structures_civil",
     "include": ["Pynite/", "docs/", "README"], "code": ["py"], "cap": 60},
    {"repo": "calfem/calfem-python", "license": "open", "topic": "structures_civil",
     "include": ["src/", "docs/", "README"], "code": ["py"], "cap": 60},
    {"repo": "jjcremmers/PyFEM", "license": "open", "topic": "structures_civil",
     "include": ["pyfem/", "doc/", "README"], "code": ["py"], "cap": 80},
    {"repo": "AppliedMechanics-EAFIT/SolidsPy", "license": "open", "topic": "structures_civil",
     "include": ["solidspy/", "docs/", "README"], "code": ["py"], "cap": 45},
    {"repo": "buddyd16/Structural-Engineering", "license": "open", "topic": "structures_civil",
     "include": ["Analysis/", "Steel/", "Concrete/", "Wood/", "Code/", "README"],
     "code": ["py"], "cap": 70},
    {"repo": "robbievanleeuwen/section-properties", "license": "open", "topic": "structures_civil"},
    {"repo": "robbievanleeuwen/concrete-properties", "license": "open", "topic": "structures_civil"},
    {"repo": "JesseBonanno/IndeterminateBeam", "license": "open", "topic": "structures_civil"},
    {"repo": "connorferster/handcalcs", "license": "open", "topic": "structures_civil"},
    {"repo": "sfepy/sfepy", "license": "open", "topic": "structures_civil"},
    {"repo": "kinnala/scikit-fem", "license": "open", "topic": "structures_civil"},
    {"repo": "nschloe/meshio", "license": "open", "topic": "structures_civil"},
    {"repo": "FEniCS/dolfinx", "license": "open", "topic": "structures_civil"},
    {"repo": "compas-dev/compas", "license": "open", "topic": "structures_civil",
     "include": ["docs/", "README"]},
    # --- round 7 built-environment: BIM / IFC / CAD geometry ---
    {"repo": "IfcOpenShell/IfcOpenShell", "license": "open", "topic": "construction",
     "include": ["docs/", "README"]},
    {"repo": "tpaviot/pythonocc-core", "license": "open", "topic": "construction"},
    # --- round 7 built-environment: GIS / geospatial / terrain ---
    {"repo": "shapely/shapely", "license": "open", "topic": "infrastructure"},
    {"repo": "geopandas/geopandas", "license": "open", "topic": "infrastructure"},
    {"repo": "gboeing/osmnx", "license": "open", "topic": "infrastructure"},
    {"repo": "pyproj4/pyproj", "license": "open", "topic": "infrastructure",
     "include": ["docs/", "README"]},
    {"repo": "rasterio/rasterio", "license": "open", "topic": "infrastructure"},
    {"repo": "Toblerity/Fiona", "license": "open", "topic": "infrastructure"},
    {"repo": "pysal/pysal", "license": "open", "topic": "infrastructure"},
    {"repo": "pysal/momepy", "license": "open", "topic": "infrastructure"},
    {"repo": "landlab/landlab", "license": "open", "topic": "infrastructure",
     "include": ["docs/", "README"]},
    # --- round 7 built-environment: hydrology / hydraulics / stormwater / groundwater ---
    {"repo": "pyswmm/pyswmm", "license": "open", "topic": "infrastructure"},
    {"repo": "OpenWaterAnalytics/EPANET", "license": "open", "topic": "infrastructure"},
    {"repo": "USEPA/Stormwater-Management-Model", "license": "open", "topic": "infrastructure"},
    {"repo": "modflowpy/flopy", "license": "open", "topic": "infrastructure",
     "include": ["docs/", "README"]},
    {"repo": "USEPA/WNTR", "license": "open", "topic": "infrastructure"},
    {"repo": "pastas/pastas", "license": "open", "topic": "infrastructure"},
    # --- 2026-09-24 building-energy-simulation documentation gaps (non-Markdown docs) ---
    # NIST-developed software notice (LICENSE.md): NIST work is not subject to US copyright;
    # "You may use, copy and distribute copies ... in any medium". The FDS / CFAST user guides,
    # technical references and validation guides are LaTeX chapters under Manuals/.
    # Bibliography/ is BibTeX-in-.tex and FIGURES/ holds TikZ drawings: excluded.
    {"repo": "firemodels/fds", "license": "public-domain", "topic": "architecture",
     "include": ["Manuals/", "README"], "docs": ["tex"],
     "exclude": ["Manuals/Bibliography/", "/FIGURES/", "/SCRIPT_FIGURES/", "Appendix_Graphs",
                 "Appendix_Only"], "cap": 80},
    {"repo": "firemodels/cfast", "license": "public-domain", "topic": "architecture",
     "include": ["Manuals/", "README"], "docs": ["tex"],
     "exclude": ["Manuals/Bibliography/", "/FIGURES/", "/SCRIPT_FIGURES/", "Appendix_Graphs",
                 "Appendix_Only"], "cap": 60},
    # MIT: RL building-control environments on EnergyPlus (docs/source/pages/*.rst, ~132 files).
    {"repo": "ugr-sail/sinergym", "license": "open", "topic": "controls_bas", "cap": 150},
    # MIT (moved from intelligent-environments-lab/CityLearn): demand-response RL environment.
    {"repo": "citylearn-project/CityLearn", "license": "open", "topic": "controls_bas"},
    # MIT: EnergyPlus geometry (geomeppy), UMI/archetype templates. NOT santoshphilip/eppy: its
    # rendered docs are already held via crawl_docs (source eppy, 22 pages) — no double copy.
    {"repo": "jamiebull1/geomeppy", "license": "open", "topic": "building_energy"},
    {"repo": "samuelduchesne/archetypal", "license": "open", "topic": "building_energy"},
    # AGPL-3.0 — COPYLEFT, not permissive. Decision 2026-09-24 (project decision maker): GO for
    # local training use, keeping the exact licence id + evidence on every entry, because the
    # registry tag `open` alone must not read as "permissive" (license_url / license_evidence).
    {"repo": "ladybug-tools/honeybee-energy", "license": "open", "topic": "building_energy",
     "license_url": "https://www.gnu.org/licenses/agpl-3.0.html",
     "license_evidence": ("SPDX AGPL-3.0 (GNU Affero GPL v3, copyleft; not permissive): "
                          "https://github.com/ladybug-tools/honeybee-energy/blob/master/LICENSE"),
     "rights_verified_at": "2026-09-24"},
    {"repo": "ladybug-tools/honeybee-radiance", "license": "open", "topic": "building_energy",
     "license_url": "https://www.gnu.org/licenses/agpl-3.0.html",
     "license_evidence": ("SPDX AGPL-3.0 (GNU Affero GPL v3, copyleft; not permissive): "
                          "https://github.com/ladybug-tools/honeybee-radiance/blob/master/LICENSE"),
     "rights_verified_at": "2026-09-24"},
    # NOT listed (checked 2026-09-24): lbl-srg/obc has no LICENSE file; mosaik lives on GitLab
    # (LGPL-2.1, crawl mosaik.readthedocs.io instead); BESOS lives on GitLab; urbanopt.github.io
    # keeps its Jekyll pages outside doc/ dirs (a walk yields 3 files) -> crawl docs.urbanopt.net.
]

# Extra documentation kinds a repo may opt into with `docs: [...]` (beyond md/rst prose). They
# are only collected under an `include` prefix, because e.g. a stray .tex or man page elsewhere
# in a source tree is rarely documentation. kind -> (file extensions, registry format).
# "" is an extension-less file (Radiance's doc/notes/*).
DOC_KINDS = {
    "tex": (("tex",), "tex"),
    "man": (("1", "2", "3", "4", "5", "6", "7", "8", "9", "man", "ms"), "troff"),
    "text": (("txt", ""), "txt"),
    "html": (("html", "htm"), "html"),
}

MAX_PER_REPO = 100  # cap files kept per repo; excess is logged, never silently dropped
SKIP_BASENAMES = {"license", "license.md", "license.txt", "license.rst", "copying",
                  "code_of_conduct.md", "contributing.md", "contributing.rst",
                  "changelog.md", "changelog.rst", "security.md", "authors.md"}
SKIP_SEGMENTS = ("/.github/", "/node_modules/", "/test/", "/tests/", "/vendor/",
                 "/third_party/", "/examples/", "/example/")


def headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "nekaise-corpus"}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _ext(path: str) -> str:
    base = path.lower().rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1] if "." in base.lstrip(".") else ""


def doc_formats(kinds) -> dict[str, str]:
    """Extension -> registry format for a repo's opted-in extra documentation kinds."""
    out: dict[str, str] = {}
    for kind in kinds or ():
        exts, fmt = DOC_KINDS[kind]
        for ext in exts:
            out[ext] = fmt
    return out


def wanted(path: str, include, code_exts=(), doc_exts=(), exclude=()) -> bool:
    low = "/" + path.lower()
    base = low.rsplit("/", 1)[-1]
    ext = _ext(path)
    is_prose = ext in ("md", "rst")
    is_code = bool(code_exts) and ext in code_exts  # opt-in: pull domain source (e.g. Modelica .mo)
    # opt-in doc markup (LaTeX / troff / plain text / html), never outside an include prefix
    is_doc = (not is_prose and not is_code and ext in doc_exts and bool(include)
              and any(path.startswith(p) for p in include))
    if not (is_prose or is_code or is_doc):
        return False
    if any(seg in low for seg in SKIP_SEGMENTS):
        return False
    if base in SKIP_BASENAMES or base.startswith("."):
        return False
    if any(x in "/" + path for x in exclude):
        return False
    if include and not any(path.startswith(p) for p in include) and not base.startswith("readme"):
        return False
    if is_code or is_doc:
        # code must be bounded by an `include` prefix (huge libs); prune later drops symbol-soup files.
        return True
    # prose: any README, anything under a doc/ or docs/ dir, or a top-level doc
    return (base.startswith("readme") or "/doc/" in low or "/docs/" in low
            or path.count("/") == 0)


def _url_format(filename: str) -> str:
    """Registry format implied by a raw file name (blocklisted URLs carry no format field)."""
    ext = _ext(filename)
    if ext in ("md", "rst"):
        return ext
    for exts, fmt in DOC_KINDS.values():
        if ext in exts and fmt != "txt":
            return fmt
    return "txt"


RAW_PREFIX = "https://raw.githubusercontent.com/"


def source_formats(view) -> dict[str, set[str]]:
    """Map each GitHub source bucket (gh_<repo>) to the registry formats already ingested or
    pruned for it, from filtered store scans over one read view.

    Only gh- rows and raw-GitHub blocklist URLs are asked for, never the entire corpus (the file
    store reads just the GitHub shards for an id-prefix scan). A pruned raw GitHub URL is durable
    evidence that the repo was walked, and its extension proves which pass (prose, opted-in code,
    opted-in doc markup) was attempted.
    """
    seen: dict[str, set[str]] = {}

    def note(s, fmt):
        if s and s.startswith("gh_"):
            seen.setdefault(s, set()).add(fmt or "")

    for table in (Table.MANIFEST, Table.ENTRIES):
        for row in dedup.scan_all(view, table, where=Prefix("id", "gh-"),
                                  fields=("source", "format")):
            note(row.get("source", ""), row.get("format"))

    for row in dedup.scan_all(view, Table.BLOCKLIST, where=Prefix("url", RAW_PREFIX),
                              fields=("url",)):
        parts = row["url"][len(RAW_PREFIX):].split("/")
        if len(parts) < 4:
            continue
        note(f"gh_{registry.slug(parts[1])}", _url_format(parts[-1]))
    return seen


def done_sources(view):
    """Return (buckets walked at all, buckets whose opted-in code pass completed).

    This keeps fully-pruned repos from being walked forever while still allowing a docs-only
    code repo to return once for its source files.
    """
    seen = source_formats(view)
    return set(seen), {s for s, fmts in seen.items() if "txt" in fmts}


def _bucket(spec: dict) -> str:
    return f"gh_{registry.slug(spec['repo'].split('/')[-1])}"


def load_passes(path: Path | None = None, *, view=None) -> dict[str, dict[str, str]]:
    """Completed passes: the store's control document when a view is given, else the file."""
    if view is not None:
        return view.control_get(PASSES_DOC) or {}
    path = path or PASSES
    return json.loads(path.read_text()) if path.exists() else {}


def pass_records(specs: list[dict], today: str) -> dict[str, dict[str, str]]:
    """{bucket: {doc kind: today}} for every requested doc kind of these walked repos."""
    records: dict[str, dict[str, str]] = {}
    for spec in specs:
        for kind in spec.get("docs") or ():
            records.setdefault(_bucket(spec), {})[kind] = today
    return records


def record_passes(specs: list[dict], today: str, path: Path | None = None) -> None:
    """Mark every requested doc kind of successfully walked repos complete (empty or not).

    Inside a round's discovery phase (proposal mode) nothing shared is written: the records are
    staged in this finder's proposal and run_round applies them to the store only if the finder
    succeeds. A standalone --append writes the passes file, like registry.append_entries."""
    records = pass_records(specs, today)
    if not records or registry.stage_github_passes(records):
        return
    path = path or PASSES
    current = load_passes(path)
    passes = registry.merge_github_passes(current, records)
    if passes != current:
        ops.atomic_write_text(path, json.dumps(passes, indent=2, sort_keys=True) + "\n")


def missing_doc_kinds(spec: dict, formats: dict[str, set[str]],
                      passes: dict[str, dict[str, str]]) -> list[str]:
    """Requested doc kinds with neither a recorded pass nor a file of that kind on record.

    Completion is PER KIND: Radiance asks for `man` and `text`; a notes file (txt) on record
    does not complete the man-page (troff) pass."""
    bucket = _bucket(spec)
    seen = formats.get(bucket, set())
    done = passes.get(bucket, {})
    return [kind for kind in spec.get("docs") or ()
            if kind not in done and DOC_KINDS[kind][1] not in seen]


def pending_repos(repos: list[dict], done: set[str], code_done: set[str],
                  formats: dict[str, set[str]] | None = None,
                  passes: dict[str, dict[str, str]] | None = None) -> list[dict]:
    """Keep unwalked repos, code repos whose source-file pass has not completed, and repos with
    an opted-in doc-markup kind (`docs: [...]`) whose pass has not completed — a repo walked
    earlier for Markdown only (e.g. Radiance, whose README was pruned) returns for its man pages."""
    formats = formats or {}
    passes = passes or {}
    out = []
    for spec in repos:
        bucket = _bucket(spec)
        kinds = spec.get("docs") or ()
        # A recorded pass for EVERY requested doc kind proves a successful walk even when the
        # repo yielded zero files (no registry/manifest/blocklist row can exist to say so).
        walked = bucket in done or (bool(kinds) and all(k in passes.get(bucket, {})
                                                        for k in kinds))
        if (not walked or (spec.get("code") and bucket not in code_done)
                or missing_doc_kinds(spec, formats, passes)):
            out.append(spec)
    return out


def capped_paths(paths: list[str], code_exts: tuple[str, ...], cap: int) -> list[str]:
    """Cap deterministically while reserving up to half the slots for opted-in source code."""
    paths = sorted(paths)
    if len(paths) <= cap:
        return paths
    if cap <= 0 or not code_exts:
        return paths[:max(0, cap)]

    def is_code(path: str) -> bool:
        base = path.lower().rsplit("/", 1)[-1]
        return "." in base and base.rsplit(".", 1)[-1] in code_exts

    code = [path for path in paths if is_code(path)]
    prose = [path for path in paths if not is_code(path)]
    reserve = min(len(code), (cap + 1) // 2)
    selected = prose[:cap - reserve] + code[:reserve]
    remaining = cap - len(selected)
    if remaining:
        selected.extend(code[reserve:reserve + remaining])
        remaining = cap - len(selected)
    if remaining:
        selected.extend(prose[cap - reserve:cap - reserve + remaining])
    return sorted(selected)


def from_repo(spec: dict) -> list:
    repo = spec["repo"]
    name = repo.split("/")[-1]
    meta = requests.get(f"{API}/repos/{repo}", headers=headers(), timeout=30)
    meta.raise_for_status()
    # `branch` overrides a default branch that does not carry the docs (Radiance: cvsimport)
    branch = spec.get("branch") or meta.json().get("default_branch", "main")
    tree = requests.get(f"{API}/repos/{repo}/git/trees/{branch}",
                        params={"recursive": "1"}, headers=headers(), timeout=45)
    tree.raise_for_status()
    tj = tree.json()
    if tj.get("truncated"):
        print(f"# WARN {repo}: tree truncated by GitHub — some deep files not listed", file=sys.stderr)
    code_exts = tuple(e.lower().lstrip(".") for e in spec.get("code", []))
    doc_map = doc_formats(spec.get("docs"))
    cap = spec.get("cap", MAX_PER_REPO)
    paths = sorted(n["path"] for n in tj.get("tree", [])
                   if n.get("type") == "blob"
                   and wanted(n["path"], spec.get("include"), code_exts, tuple(doc_map),
                              tuple(spec.get("exclude", ()))))
    if len(paths) > cap:
        print(f"# NOTE {repo}: {len(paths)} files, capping at {cap} "
              f"(dropped {len(paths) - cap})", file=sys.stderr)
        paths = capped_paths(paths, code_exts, cap)
    out = []
    for p in paths:
        ext = _ext(p)
        # code (e.g. Modelica .mo) is stored & extracted verbatim as plain text (format: txt);
        # opted-in doc markup maps to its extractor format (tex / troff / html / txt)
        fmt = ext if ext in ("md", "rst") else "txt" if ext in code_exts else doc_map.get(ext, "txt")
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/{p}"
        title = f"{name}: {p}"[:150]
        stem = p.rsplit(".", 1)[0] if ext else p
        sid = f"gh-{registry.slug(name)}-{registry.slug(stem)}"[:63]
        entry = {"id": sid, "title": title, "url": url, "source": f"gh_{registry.slug(name)}",
                 "license": spec["license"], "topic": spec["topic"], "format": fmt}
        # exact-licence provenance (e.g. copyleft repos filed under the generic `open` tag)
        for key in ("license_url", "license_evidence", "rights_verified_at"):
            if spec.get(key):
                entry[key] = spec[key]
        out.append(entry)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="", help="limit to one owner/repo from the curated list")
    ap.add_argument("--append", action="store_true",
                    help="append candidates into the registry shards (then load + prune)")
    args = ap.parse_args()
    repos = [r for r in REPOS if not args.repo or r["repo"] == args.repo]
    if args.repo and not repos:
        print(f"# {args.repo} not in curated REPOS; add it to find_github.py first", file=sys.stderr)
        return

    if not args.repo:  # skip repos already ingested; re-walk a code repo only until its code lands
        with dedup.read_view() as view:
            formats = source_formats(view)
            passes = load_passes(view=view)
        done = set(formats)
        code_done = {s for s, fmts in formats.items() if "txt" in fmts}
        keep = pending_repos(repos, done, code_done, formats, passes)
        if len(keep) < len(repos):
            print(f"# skipping {len(repos) - len(keep)} already-ingested repos; "
                  f"walking {len(keep)} (60/hr API budget)", file=sys.stderr)
        repos = keep

    keys = dedup.open_keys()
    urls, titles = keys.urls, keys.titles
    out, seen, walked = [], set(), []
    for spec in repos:
        try:
            hits = from_repo(spec)
        except Exception as e:
            print(f"# {spec['repo']} failed: {e}", file=sys.stderr)
            continue
        walked.append(spec)
        kept = 0
        keys.prefetch(**dedup.page_keys(hits))
        for h in hits:
            u, t = h["url"].rstrip("/"), registry.norm(h["title"])
            if u in urls or t in titles or u in seen:
                continue
            seen.add(u)
            out.append(h)
            kept += 1
        print(f"# {spec['repo']}: {kept} new", file=sys.stderr)

    keys.uniquify_ids(out)

    by_src, by_fmt = {}, {}
    for h in out:
        by_src[h["source"]] = by_src.get(h["source"], 0) + 1
        by_fmt[h["format"]] = by_fmt.get(h["format"], 0) + 1
    print(f"# {len(out)} NEW GitHub text files (deduped vs manifest + registry)")
    print(f"# by repo:   {by_src}")
    print(f"# by format: {by_fmt}")
    print("# --- review, then --append, then run scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    if args.append:
        record_passes(walked, date.today().isoformat())


if __name__ == "__main__":
    main()
