#!/usr/bin/env python3
"""compliance_common.py — shared rules of the compliance/ESG programme finders
(find_boverket --mode bfs, find_regdocs, find_eurlex, find_esef).

* Licence registration (Codex decision 2026-09-25): every row carries license_url,
  license_evidence and rights_verified_at. Rows whose honest tag is outside the CURRENT registry
  vocabulary (proprietary, unverified, cc-by-nd, publisher-oa, …: the collect-all licence classes
  that are not yet on main) are NEVER appended — they are reported as held until the collect-all
  split lands, and the source definitions that produce them stay disabled.
* The host policy the discovery client enforces is the one pinned in a store view.
"""
from __future__ import annotations

import re
import sys

import lint_registry
import store

CURRENT_LICENSES = frozenset(lint_registry.LICENSES)
RIGHTS_FIELDS = ("license_url", "license_evidence", "rights_verified_at")
# Tags the collect-all branch introduces (state_codec.LICENSE_CLASSES there): accepted in
# configuration now, appended only after the split lands.
FUTURE_LICENSES = frozenset({"cc-by-nc", "cc-by-nc-sa", "cc-by-nd", "cc-by-nc-nd",
                             "arxiv-nonexclusive", "publisher-oa", "unverified", "proprietary"})
KNOWN_LICENSES = CURRENT_LICENSES | FUTURE_LICENSES
# The programme's id families (registry routing: state_codec.SHARDS). The loader applies the
# programme's robots/byte/pacing rules to exactly these rows.
ID_PREFIXES = ("bov-bfs-", "reg-", "eur-", "esf-")

# The programme's REVIEWED delivery and discovery hosts (robots/ToU checked 2026-09-25): every
# discovery request (polite_http) and every loader hop of a programme row (build_corpus) must be
# on one of these host groups — anything else is refused before it is requested (e.g. the TCFD
# website's assets.bbhub.io CDN, a SharePoint link on an EFRAG page, an unexpected CDN redirect).
# {group key: (seconds between requests, loader cap per run)}; a longer robots.txt Crawl-delay
# wins at run time. Aliases share their group's clock and cap (PACE_ALIAS / pace_key).
PROGRAMME_HOSTS: dict[str, tuple[float, int]] = {
    "www.boverket.se": (10.0, 12), "rinfo.boverket.se": (2.0, 24),
    "publications.europa.eu": (2.0, 24), "op.europa.eu": (2.0, 6), "filings.xbrl.org": (2.0, 4),
    "www.dibk.no": (2.0, 12), "www.mcf.se": (2.0, 12), "www.retsinformation.dk": (2.0, 12),
    "www.bygningsreglementet.dk": (2.0, 12), "finlex.fi": (2.0, 12),
    "opendata.finlex.fi": (2.0, 12), "ym.fi": (5.0, 6), "www.efrag.org": (2.0, 8),
    "www.fsb.org": (2.0, 8), "data.riksdagen.se": (1.0, 12), "www.aibob.io": (2.0, 6),
    "cleartraced.com": (2.0, 6), "ukbimframework.org": (2.0, 6), "tech.eu": (2.0, 6),
    "www.eu-startups.com": (2.0, 6), "arcticstartup.com": (2.0, 6), "www.vestbee.com": (2.0, 6),
    "opper.ai": (2.0, 6), "www.nationella-riktlinjer.se": (2.0, 6),
}
PACE_ALIAS: dict[str, str] = {
    "boverket.se": "www.boverket.se", "dibk.no": "www.dibk.no", "mcf.se": "www.mcf.se",
    "retsinformation.dk": "www.retsinformation.dk",
    "bygningsreglementet.dk": "www.bygningsreglementet.dk", "www.finlex.fi": "finlex.fi",
    "www.ym.fi": "ym.fi", "efrag.org": "www.efrag.org", "fsb.org": "www.fsb.org",
    "aibob.io": "www.aibob.io", "www.cleartraced.com": "cleartraced.com",
    "www.ukbimframework.org": "ukbimframework.org", "www.tech.eu": "tech.eu",
    "eu-startups.com": "www.eu-startups.com", "vestbee.com": "www.vestbee.com",
    "www.opper.ai": "opper.ai",
}
PROGRAMME_RUN_CAP = 88
ESEF_RUN_CAP = 4
RIGHTS_REVIEW_DAYS = 30  # a source whose access terms were reviewed longer ago is not run


def pace_key(host: str) -> str:
    """The pacing/cap/politeness group of a canonical hostname (aliases share one budget)."""
    return PACE_ALIAS.get(host, host)


def reviewed_host(url_or_host: str) -> bool:
    import host_policy
    return pace_key(host_policy.canonical_host(url_or_host)) in PROGRAMME_HOSTS


def review_due(reviewed_at: str | None, today=None) -> bool:
    """Whether an access-terms review is older than RIGHTS_REVIEW_DAYS (or missing)."""
    from datetime import date
    try:
        when = date.fromisoformat(str(reviewed_at))
    except ValueError:
        return True
    return ((today or date.today()) - when).days > RIGHTS_REVIEW_DAYS


def is_programme_id(sid: str) -> bool:
    return str(sid or "").startswith(ID_PREFIXES)


def rights_errors(entry: dict) -> list[str]:
    errors = [f"{entry.get('id')}: missing {f}" for f in RIGHTS_FIELDS if not entry.get(f)]
    if entry.get("license") not in KNOWN_LICENSES:
        errors.append(f"{entry.get('id')}: unknown licence tag {entry.get('license')!r}")
    if entry.get("license") == "proprietary-internal":
        errors.append(f"{entry.get('id')}: pointer licence on a payload candidate")
    return errors


def split_appendable(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """(appendable now, held until the collect-all split). Raises on missing rights evidence."""
    problems = [p for e in entries for p in rights_errors(e)]
    if problems:
        raise ValueError("rights evidence incomplete: " + "; ".join(problems[:5]))
    ok = [e for e in entries if e["license"] in CURRENT_LICENSES]
    held = [e for e in entries if e["license"] not in CURRENT_LICENSES]
    if held:
        tags = sorted({e["license"] for e in held})
        print(f"# {len(held)} candidate(s) HELD, licence tag(s) {tags} await the collect-all "
              f"licence classes (never appended before the split)", file=sys.stderr)
    return ok, held


def pin_host_policy(root=None) -> dict:
    """Load the pinned host policy from a store view into the discovery client."""
    return pinned(root)[0]


def pinned(root=None) -> tuple[dict, dict]:
    """(host policy, configuration documents) pinned in ONE store view — the finder's policy and
    its registry/*.json configuration come from the same view the round validates (never from a
    working-tree file that may differ). The host policy is installed in the discovery client."""
    import polite_http

    st = store.open(root=root) if root is not None else store.open()
    with st.read(timeout=60) as view:
        _, policy = store.pinned_policy(view)
        documents = dict(view.config_get().documents)
    polite_http.set_policy(policy)
    return policy, documents


def pinned_config(name: str, validate, root=None) -> dict:
    """A validated programme configuration document from the pinned view (fails closed)."""
    _, documents = pinned(root)
    data = documents.get(name)
    if data is None:
        raise ValueError(f"registry/{name} is not pinned in the store view")
    errors = validate(data)
    if errors:
        raise ValueError(f"invalid registry/{name}: " + "; ".join(errors))
    return data


# --- quality profile (quality.verdict_for) --------------------------------------------------------
_CELEX_ID = re.compile(r"^celex:([0-9CE][0-9A-Z()/.\-]{4,40})$")
_RINFO = re.compile(r"^https://rinfo\.boverket\.se/(?P<grund>[A-Z]{2,5}\d{4}-\d+)/"
                    r"(?:pdf/(?P<doc>[A-Z]{2,5}\d{4}-\d+)\.pdf|"
                    r"dok/(?P<kdoc>[A-Z]{2,5}\d{4}-\d+)_Konsolidering\.pdf)$")


_CELLAR_ITEM = re.compile(r"^https://publications\.europa\.eu/resource/cellar/"
                          r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                          r"\.\d{4}\.\d{2}/(DOC_\d+)$")
_RESOLUTION = re.compile(r"^cellar seed=(?P<seed>\S+) rel=(?P<rel>[a-z]+) item=(?P<item>DOC_\d+)$")


def quality_profile(row: dict, documents: dict | None) -> str | None:
    """"normative" for a VERIFIED normative instrument of the programme, else None (generic gate).

    `documents` are the view-pinned configuration documents (registry/*.json). The authority is
    that pinned configuration plus the row's validated identity, never a source label alone:
      * EU act: `celex:<C>` whose id is exactly `eur-<slug(C)>-<lang>[-d<n>]`, a Cellar ITEM URL
        (`/resource/cellar/<uuid>.<expr>.<manif>/DOC_<n>`), and resolution evidence
        `cellar seed=<S> rel=<r> item=DOC_<n>` where S is a pinned registry/eurlex.json seed, r a
        pinned expansion relation (or `seed` with C == S) and DOC_<n> the URL's item;
      * Boverket BFS: a rinfo.boverket.se BFS or consolidation PDF whose path matches its id;
      * regdocs: a pinned registry/regdocs.json source marked `quality_profile: normative` (same
        source tag, id prefix) served from one of that source's declared `hosts`.
    The verdict then still requires the extracted text to carry the instrument's identifiers
    (metric `anchor`, instrument_anchor), so a login/error/navigation page never passes."""
    import host_policy
    import state_codec

    documents = documents or {}
    sid, url, source = str(row.get("id", "")), str(row.get("url", "")), row.get("source")
    host = host_policy.canonical_host(url)
    if sid.startswith("eur-"):
        m = _CELEX_ID.match(str(row.get("persistent_id") or ""))
        item = _CELLAR_ITEM.match(url)
        res = _RESOLUTION.match(str(row.get("resolution") or ""))
        eurlex = documents.get("eurlex.json") or {}
        seeds = {s.get("celex") for s in eurlex.get("seeds") or []}
        if not (m and item and res and source == "eurlex"):
            return None
        celex = m.group(1)
        if not re.fullmatch(rf"eur-{re.escape(state_codec.slug(celex))}-[a-z]{{2}}(?:-d\d+)?",
                            sid):
            return None
        if res.group("seed") not in seeds or res.group("item") != item.group(1):
            return None
        rel = res.group("rel")
        if not ((rel == "seed" and celex == res.group("seed"))
                or rel in (eurlex.get("expand") or [])):
            return None
        return "normative"
    if sid.startswith("bov-bfs-"):
        m = _RINFO.match(url)
        if not m or source != "boverket_bfs":
            return None
        grund = state_codec.slug(m.group("grund"))
        if m.group("doc"):
            doc = state_codec.slug(m.group("doc"))
            want = f"bov-bfs-{grund}" + ("" if doc == grund else f"-{doc}")
        else:
            want = f"bov-bfs-{grund}-{state_codec.slug(m.group('kdoc'))}-kons"
        return "normative" if sid == want else None
    if sid.startswith("reg-"):
        regdocs = documents.get("regdocs.json") or {}
        for key, cfg in (regdocs.get("sources") or {}).items():
            if (cfg.get("quality_profile") == "normative" and cfg.get("source") == source
                    and sid.startswith(f"reg-{key}-") and host in (cfg.get("hosts") or [])):
                return "normative"
    return None


# --- instrument anchor (quality metric "anchor", computed at extraction) ---------------------------
ANCHOR_WINDOW = 40_000
_CELEX_PARTS = re.compile(r"^(?P<sector>[0-9CE])(?P<year>\d{4})(?P<type>[A-Z]{1,4})(?P<num>\d{1,5})")


def instrument_patterns(row: dict) -> list[re.Pattern] | None:
    """The identifiers the extracted text of a normative row must contain (ALL of them), derived
    from the row's own identity; None when the row is not a normative instrument family."""
    sid, url = str(row.get("id", "")), str(row.get("url", ""))
    if sid.startswith("eur-"):
        m = _CELEX_ID.match(str(row.get("persistent_id") or ""))
        p = _CELEX_PARTS.match(m.group(1)) if m else None
        if not p:
            return [re.compile(r"(?!x)x")]  # unparseable identity: never anchored
        year, num = p.group("year"), str(int(p.group("num")))
        if p.group("sector") == "5":
            return [re.compile(rf"COM\s*[(/]\s*{year}\s*\)?\s*/?\s*0*{num}\b")]
        return [re.compile(rf"(?<!\d){year}\s*/\s*0*{num}(?!\d)|(?<!\d){num}\s*/\s*{year}(?!\d)")]
    if sid.startswith("bov-bfs-"):
        m = _RINFO.match(url)
        if not m:
            return [re.compile(r"(?!x)x")]
        code = m.group("doc") or m.group("grund")
        c = re.fullmatch(r"[A-Z]+(\d{4})-(\d+)", code)
        return [re.compile(rf"(?<!\d){c.group(1)}\s*:\s*{c.group(2)}(?!\d)")]
    if sid.startswith("reg-"):
        if m := re.search(r"data\.riksdagen\.se/dokument/sfs-(\d{4})-(\d+)\.text(?:#(.+))?$", url):
            pats = [re.compile(rf"\({m.group(1)}:{m.group(2)}\)|SFS nr:\s*{m.group(1)}:{m.group(2)}")]
            if m.group(3):  # a dated snapshot: the text must be exactly that consolidation
                tom = re.fullmatch(r"tom-sfs-(\d{4})-(\d+)", m.group(3))
                pats.append(re.compile(rf"t\.o\.m\.\s*SFS\s*{tom.group(1)}:{tom.group(2)}(?!\d)")
                            if tom else re.compile(r"Ändrad:[ \t]*\r?\n"))
            return pats
        if m := re.search(r"retsinformation\.dk/eli/lta/(\d{4})/(\d+)/pdf$", url):
            return [re.compile(rf"(?<!\d){m.group(2)}(?!\d)")]
        if m := re.search(r"/akn/fi/act/statute/(\d{4})/(\d+)/", url):
            return [re.compile(rf"(?<!\d){m.group(2)}\s*/\s*{m.group(1)}(?!\d)")]
    return None


def instrument_anchor(row: dict, text: str) -> bool | None:
    """Whether the extracted text carries the row's own instrument identifiers (a login page,
    error page, navigation shell or a different act never does); None = not applicable."""
    pats = instrument_patterns(row)
    if pats is None:
        return None
    head = text[:ANCHOR_WINDOW]
    return all(p.search(head) for p in pats)
