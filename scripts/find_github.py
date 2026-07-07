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
from pathlib import Path

import requests
import yaml

import registry

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
API = "https://api.github.com"

# Curated, clearly-permissive (BSD / MIT / Apache) building-energy repos. Extend freely.
#   repo     owner/name on github
#   license  registry license tag (permissive BSD/MIT/Apache -> "open")
#   topic    one of our five corpus topics
#   include  (optional) only keep paths under these prefixes — bounds huge repos like EnergyPlus
REPOS = [
    {"repo": "lbl-srg/modelica-buildings", "license": "open", "topic": "building_energy",
     "include": ["Buildings/"], "code": ["mo"], "cap": 140},
    {"repo": "ibpsa/modelica-ibpsa", "license": "open", "topic": "building_energy",
     "include": ["IBPSA/"], "code": ["mo"], "cap": 140},
    {"repo": "lbl-srg/BuildingsPy", "license": "open", "topic": "building_energy"},
    {"repo": "NREL/EnergyPlus", "license": "open", "topic": "building_energy",
     "include": ["doc/", "design/", "README"]},
    {"repo": "NREL/OpenStudio", "license": "open", "topic": "standards_protocols"},
    {"repo": "NREL/openstudio-standards", "license": "open", "topic": "standards_protocols"},
    {"repo": "NREL/resstock", "license": "open", "topic": "building_energy"},
    {"repo": "NREL/comstock", "license": "open", "topic": "building_energy"},
    {"repo": "NREL/OpenStudio-HPXML", "license": "open", "topic": "building_energy"},
    {"repo": "LBNL-ETA/Radiance", "license": "open", "topic": "building_energy"},
    {"repo": "CoolProp/CoolProp", "license": "open", "topic": "equipment_systems"},
    # --- round 7: solar / PV / thermal-systems (Python domain code + rich docs) ---
    {"repo": "pvlib/pvlib-python", "license": "open", "topic": "equipment_systems"},
    {"repo": "NREL/PySAM", "license": "open", "topic": "equipment_systems"},
    {"repo": "NREL/bifacial_radiance", "license": "open", "topic": "equipment_systems"},
    {"repo": "NREL/floris", "license": "open", "topic": "equipment_systems"},
    {"repo": "oemof/tespy", "license": "open", "topic": "equipment_systems"},
    {"repo": "NREL/ssc", "license": "open", "topic": "equipment_systems",
     "include": ["ssc/", "shared/", "README"]},
    # --- round 7: Modelica building/HVAC/thermal libraries (pull .mo domain code, bounded) ---
    {"repo": "open-ideas/IDEAS", "license": "open", "topic": "building_energy",
     "include": ["IDEAS/", "docs/", "README"], "code": ["mo"], "cap": 140},
    {"repo": "RWTH-EBC/AixLib", "license": "open", "topic": "equipment_systems",
     "include": ["AixLib/", "docs/", "README"], "code": ["mo"], "cap": 140},
    {"repo": "modelica/ModelicaStandardLibrary", "license": "open", "topic": "equipment_systems",
     "include": ["Modelica/Thermal/", "Modelica/Fluid/", "Modelica/Media/", "README"],
     "code": ["mo"], "cap": 140},
    {"repo": "queraltab/Greenhouses-Library", "license": "open", "topic": "equipment_systems",
     "code": ["mo"], "cap": 120},
    # --- round 7: whole-building / urban building energy modeling ---
    {"repo": "RWTH-EBC/TEASER", "license": "open", "topic": "building_energy"},
    {"repo": "RWTH-EBC/ebcpy", "license": "open", "topic": "building_energy"},
    {"repo": "NREL/OCHRE", "license": "open", "topic": "building_energy"},
    {"repo": "architecture-building-systems/CityEnergyAnalyst", "license": "open",
     "topic": "building_energy", "include": ["cea/", "docs/", "README"]},
    # --- round 7: energy-system / power-system / techno-economic modeling ---
    {"repo": "oemof/oemof-solph", "license": "open", "topic": "building_energy"},
    {"repo": "calliope-project/calliope", "license": "open", "topic": "building_energy"},
    {"repo": "PyPSA/PyPSA", "license": "open", "topic": "building_energy"},
    {"repo": "OSeMOSYS/OSeMOSYS", "license": "open", "topic": "building_energy"},
    {"repo": "NREL/REopt.jl", "license": "open", "topic": "building_energy"},
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
]

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


def wanted(path: str, include, code_exts=()) -> bool:
    low = "/" + path.lower()
    base = low.rsplit("/", 1)[-1]
    ext = base.rsplit(".", 1)[-1] if "." in base else ""
    is_prose = ext in ("md", "rst")
    is_code = bool(code_exts) and ext in code_exts  # opt-in: pull domain source (e.g. Modelica .mo)
    if not (is_prose or is_code):
        return False
    if any(seg in low for seg in SKIP_SEGMENTS):
        return False
    if base in SKIP_BASENAMES:
        return False
    if include and not any(path.startswith(p) for p in include) and not base.startswith("readme"):
        return False
    if is_code:
        # code must be bounded by an `include` prefix (huge libs); prune later drops symbol-soup files.
        return True
    # prose: any README, anything under a doc/ or docs/ dir, or a top-level doc
    return (base.startswith("readme") or "/doc/" in low or "/docs/" in low
            or path.count("/") == 0)


def done_sources():
    """gh_ source buckets already ingested (manifest or registry). Returns (all_done, code_done)
    where code_done = buckets that already have a txt (source-code) entry. Lets repeated runs skip
    finished repos — and skip code repos whose code is already pulled — spending the 60/hr API budget
    only on repos not yet walked."""
    all_done, code_done = set(), set()

    def note(s, fmt):
        if s and s.startswith("gh_"):
            all_done.add(s)
            if fmt == "txt":
                code_done.add(s)

    for r in registry.load_manifest_rows():
        note(r.get("source", ""), r.get("format"))
    for e in registry.load_entries():
        note(e.get("source", ""), e.get("format"))
    return all_done, code_done


def from_repo(spec: dict) -> list:
    repo = spec["repo"]
    name = repo.split("/")[-1]
    meta = requests.get(f"{API}/repos/{repo}", headers=headers(), timeout=30)
    meta.raise_for_status()
    branch = meta.json().get("default_branch", "main")
    tree = requests.get(f"{API}/repos/{repo}/git/trees/{branch}",
                        params={"recursive": "1"}, headers=headers(), timeout=45)
    tree.raise_for_status()
    tj = tree.json()
    if tj.get("truncated"):
        print(f"# WARN {repo}: tree truncated by GitHub — some deep files not listed", file=sys.stderr)
    code_exts = tuple(e.lower().lstrip(".") for e in spec.get("code", []))
    cap = spec.get("cap", MAX_PER_REPO)
    paths = sorted(n["path"] for n in tj.get("tree", [])
                   if n.get("type") == "blob" and wanted(n["path"], spec.get("include"), code_exts))
    if len(paths) > cap:
        print(f"# NOTE {repo}: {len(paths)} files, capping at {cap} "
              f"(dropped {len(paths) - cap})", file=sys.stderr)
        paths = paths[:cap]
    out = []
    for p in paths:
        low = p.lower()
        # code (e.g. Modelica .mo) is stored & extracted verbatim as plain text (format: txt)
        fmt = "rst" if low.endswith(".rst") else "md" if low.endswith(".md") else "txt"
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/{p}"
        title = f"{name}: {p}"[:150]
        sid = f"gh-{registry.slug(name)}-{registry.slug(p.rsplit('.', 1)[0])}"[:63]
        out.append({"id": sid, "title": title, "url": url, "source": f"gh_{registry.slug(name)}",
                    "license": spec["license"], "topic": spec["topic"], "format": fmt})
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
        done, code_done = done_sources()
        def pending(r):
            b = f"gh_{registry.slug(r['repo'].split('/')[-1])}"
            if b not in done:
                return True                       # never walked
            return bool(r.get("code")) and b not in code_done  # walked docs, code still to pull
        keep = [r for r in repos if pending(r)]
        if len(keep) < len(repos):
            print(f"# skipping {len(repos) - len(keep)} already-ingested repos; "
                  f"walking {len(keep)} (60/hr API budget)", file=sys.stderr)
        repos = keep

    urls, titles, reg_ids = registry.existing_keys()
    out, seen = [], set()
    for spec in repos:
        try:
            hits = from_repo(spec)
        except Exception as e:
            print(f"# {spec['repo']} failed: {e}", file=sys.stderr)
            continue
        kept = 0
        for h in hits:
            u, t = h["url"].rstrip("/"), registry.norm(h["title"])
            if u in urls or t in titles or u in seen:
                continue
            seen.add(u)
            out.append(h)
            kept += 1
        print(f"# {spec['repo']}: {kept} new", file=sys.stderr)

    registry.uniquify_ids(out, reg_ids)

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


if __name__ == "__main__":
    main()
