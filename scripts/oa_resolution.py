#!/usr/bin/env python3
"""oa_resolution.py — shared rights, host and version resolution for scholarly-metadata finders.

A scholarly-metadata record (OpenAlex work, Unpaywall record, Crossref record) describes a WORK;
the corpus fetches one COPY of it. This module decides which copy, if any, may be fetched, and
records why, so that nothing reaches the registry with a licence it merely "looks like" it has:

* every location is inspected and rights are required for the SELECTED copy — the licence of a
  different version (publisher PDF vs. accepted manuscript vs. preprint) never transfers, and a
  rejected licence on one version does not invalidate an independently licensed other version;
* only CC BY, CC BY-SA, CC0 and verified public domain are accepted (NC/ND are excluded as
  everywhere else); an unknown, missing or contradictory licence leaves the work UNRESOLVED —
  never ``license: open`` merely because a PDF downloads;
* canonical Creative Commons URLs go through licenses.cc_license(); structured provider licence
  ids ("cc-by", "CC-BY-4.0", "https://openalex.org/licenses/cc-by") are mapped by tested adapters
  that never invent a licence version a provider did not state (a bare "cc-by" records no
  license_url); a bare "public-domain"/"pd" label is not verification;
* hosts match exactly or as a subdomain (never by substring), fetch-suspended hosts (the pinned
  registry/host_policy.json, passed in by the caller) and the operational/NO-GO exclusions below
  are never selected, and DOI resolvers are never a copy (their final host is unknown).

Pure functions over already-fetched JSON: the network lives in the finders.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

import host_policy
import licenses
import state_codec

# The repository's configured polite-pool contact (OpenAlex mailto, Crossref mailto, Unpaywall
# email). It is the project address, never an operator's personal address.
MAILTO = "corpus@opennekaise.org"

ACCEPTED_TAGS = ("cc-by", "cc-by-sa", "cc0", "public-domain")

# Hosts that serve direct scholarly PDFs to an honest client, matched EXACTLY or as a subdomain
# ("gov" admits www.nrel.gov but not evilgov.com; "zenodo.org" admits zenodo.org only).
# Publisher landing platforms that 403 bots (sciencedirect, springerlink, wiley, ieee, tandf) are
# deliberately absent. A host here is fetch-eligible, never licence evidence.
ALLOWED_PDF_HOSTS = frozenset({
    # preprint servers and general repositories
    "arxiv.org", "zenodo.org", "figshare.com", "osf.io", "europepmc.org", "ncbi.nlm.nih.gov",
    "repository.tudelft.nl", "diva-portal.org", "orbit.dtu.dk", "backend.orbit.dtu.dk",
    "portal.findresearcher.sdu.dk", "vbn.aau.dk", "research-collection.ethz.ch",
    "infoscience.epfl.ch", "publikationen.bibliothek.kit.edu", "mediatum.ub.tum.de",
    "publications.rwth-aachen.de", "repositum.tuwien.at", "re.public.polimi.it",
    "iris.polito.it", "lirias.kuleuven.be", "biblio.ugent.be", "research.chalmers.se",
    "research.tue.nl", "pure.tue.nl", "ntnuopen.ntnu.no", "portal.research.lu.se",
    "publications.ibpsa.org", "ecp.ep.liu.se", "escholarship.org",
    # open-access publishers with direct PDF links (springeropen.com dropped 2026-09-25: its
    # /track/pdf/ links answer HTML to the loader)
    "plos.org", "frontiersin.org", "biomedcentral.com", "copernicus.org",
    "peerj.com", "elifesciences.org", "hindawi.com",
    # academic and government domains (suffix match on a whole label)
    "gov", "edu", "ac.uk", "ac.jp", "ac.kr", "edu.au", "edu.cn",
})
# Operational exclusions: license-compatible hosts that are unsuitable for reproducible bulk
# fetches from this operator's network. Kept here (not in host_policy.json) because they are
# network conditions, not access-policy decisions. OSTI's API and PDF host connect-timeout
# (re-probed 2026-09-07); PMC's apparent PDF URLs return a JavaScript interstitial as HTTP 200
# (re-probed 2026-09-06). MDPI's host-wide 403 moved to registry/host_policy.json (2026-09-25).
PAUSED_PDF_HOSTS = {
    "osti.gov": "API and PDF host connect-timeout; re-probe before re-enabling",
    "pmc.ncbi.nlm.nih.gov": "PDF paths return an HTTP 200 JavaScript download interstitial",
}
# NO-GO: never selected, whatever a host allowlist or host policy file says. ssrn.com is also
# fetch-suspended in host_policy.json; J-STAGE, SOEP and the OpenStudio Coalition site are
# eligibility restrictions that must not be revived through another finder.
NEVER_FETCH_HOSTS = frozenset({
    "ssrn.com", "jstage.jst.go.jp", "lbl-srg.github.io", "openstudiocoalition.org",
})
# A work with ANY location on these hosts is excluded outright (the restriction covers the work,
# whichever copy would be fetched).
WORK_EXCLUDED_HOSTS = frozenset({"jstage.jst.go.jp"})
RESOLVER_HOSTS = frozenset({"doi.org", "dx.doi.org", "hdl.handle.net", "n2t.net"})

VERSION_RANK = {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2}
# Crossref licence content-versions per OpenAlex/Unpaywall version. TDM licences are text-mining
# terms for subscribers, never a reuse licence, and are ignored.
CROSSREF_VERSION = {"publishedVersion": ("vor",), "acceptedVersion": ("am",)}
# Crossref relation types that state version identity outright; other relation types need
# title + author evidence (same_work) before a related DOI is treated as the same work.
EXPLICIT_RELATIONS = frozenset({
    "is-preprint-of", "has-preprint", "is-version-of", "has-version", "is-identical-to",
    "is-same-as", "is-manifestation-of", "has-manifestation",
})


def host_of(url: str | None) -> str:
    return host_policy.canonical_host(url)


def host_matches(host: str, domains) -> str | None:
    """The domain in `domains` that `host` equals or is a subdomain of (whole labels only)."""
    host = (host or "").lower().rstrip(".")
    for domain in domains:
        if host == domain or host.endswith("." + domain):
            return domain
    return None


# COPY IDENTITY across redirects. Licence evidence belongs to one copy, so a redirect may only
# be followed when it provably stays on that copy: an explicit, bounded URL transformation — the
# SAME repository identifier (Zenodo record id, UUID bitstream/item id, handle, arXiv id, PMC id,
# long numeric record id, or the same PDF file name) on the SAME host or a configured host pair.
# Registrable domains are deliberately NOT used: a shared domain holds many different copies
# (two Zenodo records; two universities under one multi-label public suffix).
COPY_HOST_PAIRS = frozenset({
    # DSpace 7 front end -> REST API delivering the same bitstream
    ("www.repository.cam.ac.uk", "api.repository.cam.ac.uk"),
    ("repository.cam.ac.uk", "api.repository.cam.ac.uk"),
    # repository -> delivery network (same identifier still required)
    ("europepmc.org", "www.ebi.ac.uk"),
    ("europepmc.org", "europepmc.org"),
    ("arxiv.org", "export.arxiv.org"),
})
_COPY_IDS = (
    ("zenodo", re.compile(r"/(?:api/)?records?/(\d+)(?:/|$)")),
    ("uuid", re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
                        re.I)),
    ("handle", re.compile(r"/(?:handle|bitstream(?:/handle)?)/(\d+/\d+)(?:/|$)")),
    ("arxiv", re.compile(r"/(?:pdf|abs)/(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?(?:/|$)")),
    ("pmc", re.compile(r"\b(PMC\d+)\b", re.I)),
    ("number", re.compile(r"/(\d{6,})(?:/|$)")),
    ("file", re.compile(r"/([^/?#]{5,}\.pdf)$", re.I)),
)


GENERIC_FILE_NAMES = frozenset({"fulltext.pdf", "download.pdf", "content.pdf", "file.pdf",
                                "paper.pdf", "main.pdf", "article.pdf", "document.pdf",
                                "manuscript.pdf", "preprint.pdf", "pdf.pdf"})


def copy_ids(url: str | None) -> set[tuple[str, str]]:
    """The repository identifiers a URL names ((kind, value) pairs, see _COPY_IDS)."""
    path = urlparse(url or "").path
    out = set()
    for kind, pattern in _COPY_IDS:
        for m in pattern.finditer(path):
            value = m.group(1).lower()
            if kind == "file" and value in GENERIC_FILE_NAMES:
                continue  # names every copy on the host: identifies none
            out.add((kind, value))
    return out


def same_copy(origin: str | None, url: str | None) -> bool:
    """Whether a redirect from the registered copy URL `origin` to `url` provably stays on that
    copy (see COPY_HOST_PAIRS / _COPY_IDS). Anything else is a different copy whose rights
    were never checked: the caller refuses it (fail closed)."""
    a, b = host_of(origin), host_of(url)
    if not a or not b:
        return False
    if (a, b) != (a, a) and (a, b) not in COPY_HOST_PAIRS:
        return False
    if (origin or "").strip() == (url or "").strip():
        return True
    return bool(copy_ids(origin) & copy_ids(url))


# compatibility name (the loader's hop check)
same_copy_host = same_copy


def copy_refusal(url: str | None, policy: dict) -> str | None:
    """Why this URL may not be selected as a copy to fetch, or None. `policy` is the pinned
    host policy (store.pinned_policy(view)[1]); passing it is mandatory."""
    if policy is None:
        raise ValueError("copy_refusal needs the pinned host policy")
    parsed = urlparse(url or "")
    host = host_of(url)
    if parsed.scheme not in ("http", "https") or not host:
        return "not an http(s) URL"
    if never := host_matches(host, NEVER_FETCH_HOSTS):
        return f"host_never_fetch:{never}"
    if host_policy.suspended(url, policy):
        return f"host_suspended:{host}"
    if paused := host_matches(host, PAUSED_PDF_HOSTS):
        return f"host_paused:{paused}"
    if host_matches(host, RESOLVER_HOSTS):
        return "resolver_url"
    # eScholarship /uc/item/<id> pages are HTML landing pages (also host-suspended today).
    if host_matches(host, {"escholarship.org"}) and re.fullmatch(r"/uc/item/[^/]+/?",
                                                                 parsed.path):
        return "landing_page"
    if not host_matches(host, ALLOWED_PDF_HOSTS):
        return f"host_not_allowed:{host}"
    return None


# --- identifiers ----------------------------------------------------------------------------------

def normalize_doi(value: str | None) -> str | None:
    """Lower-case bare DOI ("10.x/y") from a DOI, doi: form or doi.org URL; None if not a DOI."""
    return state_codec.normalize_doi(value)


def normalize_openalex(value: str | None) -> str | None:
    """"W123…" from an OpenAlex work URL or id; None otherwise."""
    return state_codec.normalize_openalex(value)


def doi_url(doi: str) -> str:
    return f"https://doi.org/{doi}"


# --- licence adapters -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Evidence:
    """One provider's statement about one copy: accepted (tag), rejected, or unknown."""
    provider: str
    value: str
    status: str            # "accepted" | "rejected" | "unknown"
    tag: str | None = None
    url: str | None = None  # canonical licence URL, only when the provider stated one


_SPDX = re.compile(r"^cc[- ]?(by(?:[- ]sa)?|0)(?:[- ](\d\.\d))?$", re.I)
_RESTRICTIVE_LABEL = re.compile(r"\bnc\b|\bnd\b|non-?commercial|no-?deriv", re.I)


def structured_licence(provider: str, value: str | None) -> Evidence | None:
    """Map one structured provider licence id/label/URL to evidence (None when absent).

    Canonical creativecommons.org URLs go through licenses.cc_license(). SPDX-style ids with a
    version ("CC-BY-4.0", "CC0-1.0") are expanded to their canonical URL; bare labels ("cc-by")
    are accepted as the provider's structured claim WITHOUT a licence URL — no version is
    invented. NC/ND in any form is rejected; public-domain labels are not verification."""
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    v = raw.lower()
    v = re.sub(r"^https?://openalex\.org/licenses/", "", v)
    if v.startswith(("http://", "https://")):
        tag, url = licenses.cc_license([raw])
        if tag:
            return Evidence(provider, raw, "accepted", tag, url)
        if "creativecommons.org" in v and ("-nc" in v or "-nd" in v):
            return Evidence(provider, raw, "rejected")
        return Evidence(provider, raw, "unknown")  # TDM / publisher / other licence URLs
    label = v.replace("_", "-")
    if _RESTRICTIVE_LABEL.search(label.replace("-", " ")):
        return Evidence(provider, raw, "rejected")
    if m := _SPDX.match(label):
        kind = m.group(1).replace(" ", "-")
        version = m.group(2)
        if kind == "0":
            url = "https://creativecommons.org/publicdomain/zero/1.0/" if version == "1.0" else None
            return Evidence(provider, raw, "accepted", "cc0", url)
        tag = "cc-by-sa" if kind == "by-sa" else "cc-by"
        url = (f"https://creativecommons.org/licenses/{kind}/{version}/" if version else None)
        if url:
            tag, url = licenses.cc_license([url])
            if not tag:
                return Evidence(provider, raw, "unknown")
        return Evidence(provider, raw, "accepted", tag, url)
    # "public-domain" / "pd" labels, "other-oa", "implied-oa", "publisher-specific-oa", software
    # licences, …: no accepted reuse grant is verified.
    return Evidence(provider, raw, "unknown")


def openalex_location_evidence(location: dict) -> list[Evidence]:
    """Evidence from one OpenAlex location's `license` and `license_id`: BOTH statements are
    kept, so a disagreement between them stays visible (and fails closed in combine())."""
    out = []
    for key in ("license", "license_id"):
        ev = structured_licence(f"openalex.{key}", location.get(key))
        if ev:
            out.append(ev)
    return out


def unpaywall_location_evidence(location: dict) -> list[Evidence]:
    ev = structured_licence("unpaywall.license", location.get("license"))
    return [ev] if ev else []


_MALFORMED = object()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _crossref_start(lic: dict):
    """A Crossref licence's effective start as an aware UTC datetime; _MALFORMED when it is
    missing, partial ([year] or [year, month] could be any later day) or unparsable."""
    start = lic.get("start")
    if not isinstance(start, dict):
        return _MALFORMED
    if isinstance(start.get("date-time"), str):
        try:
            return _aware(datetime.fromisoformat(start["date-time"].replace("Z", "+00:00")))
        except ValueError:
            return _MALFORMED
    parts = start.get("date-parts")
    parts = parts[0] if isinstance(parts, list) and parts and isinstance(parts[0], list) else None
    if not parts or len(parts) != 3 or not all(isinstance(x, int) and not isinstance(x, bool)
                                               for x in parts):
        return _MALFORMED
    try:
        return datetime(parts[0], parts[1], parts[2], tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return _MALFORMED


def crossref_evidence(message: dict, version: str | None,
                      now: datetime | None = None) -> tuple[list[Evidence], list[Evidence]]:
    """Crossref licence statements about the DOI's own copy in `version`, as (direct,
    corroborating). Direct: the grant for exactly that content-version (vor = publishedVersion,
    am = acceptedVersion) with a valid, already effective start date (a missing, partial or
    malformed date is an `unknown` statement: it fails closed, it never authorizes). Corroborating: `unspecified` grants, which never grant on their own
    (the caller adds them only next to other evidence, where a disagreement fails closed). A
    grant that starts in the future (an embargo) is not a grant yet: it becomes an `unknown`
    statement, which fails closed next to any accepted one. TDM licences are ignored."""
    now = _aware(now) if now is not None else datetime.now(timezone.utc)
    wanted = CROSSREF_VERSION.get(version or "", ())
    direct: list[Evidence] = []
    corroborating: list[Evidence] = []
    for lic in message.get("license") or []:
        if not isinstance(lic, dict):
            continue
        kind = lic.get("content-version")
        if kind not in wanted and kind != "unspecified":
            continue
        provider = f"crossref.license[{kind}]"
        start = _crossref_start(lic)
        if start is _MALFORMED:
            # no valid effective date: the grant can neither authorize nor be ignored
            ev = Evidence(provider, f"{lic.get('URL')} (no valid start date)", "unknown")
        elif start > now:
            ev = Evidence(provider, f"{lic.get('URL')} (effective {start.date()})", "unknown")
        else:
            ev = structured_licence(provider, lic.get("URL"))
        if ev:
            (corroborating if kind == "unspecified" else direct).append(ev)
    return direct, corroborating


@dataclass(frozen=True)
class Rights:
    status: str                  # "accepted" | "rejected" | "unknown" | "conflict"
    tag: str | None = None
    url: str | None = None
    evidence: str = ""


def combine(evidence: list[Evidence]) -> Rights:
    """Fail-closed combination of every statement about ONE copy."""
    text = "; ".join(f"{e.provider}={e.value}" for e in evidence)
    if not evidence:
        return Rights("unknown", evidence="no licence statement for this copy")
    if any(e.status == "rejected" for e in evidence):
        return Rights("rejected", evidence=text)
    accepted = [e for e in evidence if e.status == "accepted"]
    if not accepted:
        return Rights("unknown", evidence=text)
    tags = {e.tag for e in accepted}
    if len(tags) > 1 or len(accepted) != len(evidence):
        # two different grants, or a grant next to a non-grant statement about the same copy
        return Rights("conflict", evidence=text)
    tag = tags.pop()
    if tag == "public-domain" and not any(e.url for e in accepted):
        return Rights("unknown", evidence=text + " (public domain not verified)")
    urls = {e.url for e in accepted if e.url}
    if len(urls) > 1:
        # e.g. CC BY 3.0 vs 4.0 from two providers: the tag agrees, the URL does not
        return Rights("conflict", evidence=text)
    return Rights("accepted", tag, urls.pop() if urls else None, text)


# --- copy selection -------------------------------------------------------------------------------

def copy_acceptable(rights: Rights) -> bool:
    """THE acceptance predicate: whether a copy with these combined rights may be selected and
    registered. Today: accepted evidence only (CC BY / BY-SA / CC0 / verified PD). Every
    selection decision goes through this one function, so a later collection-vs-licence-class
    policy changes it here (and nowhere else)."""
    return rights.status == "accepted"


@dataclass
class Copy:
    url: str
    version: str | None
    evidence: list[Evidence] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    doi_copy: bool = False  # the DOI's own (publisher) location


@dataclass
class Resolution:
    status: str                          # "resolved" | "unresolved" | "excluded"
    reasons: list[str] = field(default_factory=list)
    copy: Copy | None = None
    rights: Rights | None = None

    def reason_kinds(self) -> list[str]:
        return sorted({r.split(":", 1)[0] for r in self.reasons})


def _norm_url(u: str | None) -> str:
    return (u or "").strip().rstrip("/")


def work_locations(work: dict) -> list[dict]:
    """EVERY location statement (best_oa_location, primary_location, locations), never deduped:
    the same location listed twice with different licences is a contradiction to keep."""
    return [loc for loc in [work.get("best_oa_location"), work.get("primary_location"),
                            *(work.get("locations") or [])] if isinstance(loc, dict)]


def work_excluded(work: dict) -> str | None:
    """A work-level exclusion (never proposed, never retried), or None."""
    if work.get("is_retracted"):
        return "retracted"
    if work.get("is_paratext"):
        return "paratext"
    for loc in work_locations(work):
        for key in ("pdf_url", "landing_page_url"):
            if hit := host_matches(host_of(loc.get(key)), WORK_EXCLUDED_HOSTS):
                return f"restricted_host:{hit}"
    return None


def candidate_copies(work: dict, *, unpaywall: dict | None = None,
                     crossref: dict | None = None, now: datetime | None = None) -> list[Copy]:
    """Every distinct PDF copy the metadata names, with EVERY licence statement about it (only
    identical statements — same provider, value, verdict, tag and licence URL — collapse)."""
    doi = normalize_doi(work.get("doi"))
    copies: dict[str, Copy] = {}

    def add(url, version, evidence, source, doi_copy=False):
        key = _norm_url(url)
        if not key:
            return
        c = copies.setdefault(key, Copy(url=url.strip(), version=version))
        if c.version != version and version:
            if c.version is None:
                c.version = version
            else:
                # two providers disagree on WHICH version this file is: its rights are unknowable
                c.evidence.append(Evidence(source, f"version {version} vs {c.version}",
                                           "unknown"))
        for ev in evidence:
            if ev not in c.evidence:
                c.evidence.append(ev)
        c.sources.append(source)
        c.doi_copy = c.doi_copy or doi_copy

    for loc in work_locations(work):
        if not loc.get("pdf_url"):
            continue
        loc_doi = normalize_doi(loc.get("id")) or normalize_doi(loc.get("landing_page_url"))
        add(loc["pdf_url"], loc.get("version"), openalex_location_evidence(loc), "openalex",
            doi_copy=bool(doi and loc_doi == doi))
    for loc in (unpaywall or {}).get("oa_locations") or []:
        if not isinstance(loc, dict) or not loc.get("url_for_pdf"):
            continue
        add(loc["url_for_pdf"], loc.get("version"), unpaywall_location_evidence(loc),
            "unpaywall", doi_copy=loc.get("host_type") == "publisher")
    if crossref:
        for c in copies.values():
            if c.doi_copy:
                direct, corroborating = crossref_evidence(crossref, c.version, now)
                c.evidence.extend(direct)
                if c.evidence:  # `unspecified` never grants alone
                    c.evidence.extend(corroborating)
    return list(copies.values())


def select_copy(work: dict, policy: dict, *, unpaywall: dict | None = None,
                crossref: dict | None = None, now: datetime | None = None) -> Resolution:
    """Choose the best fetchable copy WITH accepted rights for that very copy."""
    if why := work_excluded(work):
        return Resolution("excluded", [why])
    reasons: list[str] = []
    eligible: list[tuple[tuple, Copy, Rights]] = []
    copies = candidate_copies(work, unpaywall=unpaywall, crossref=crossref, now=now)
    if not copies:
        reasons.append("no_pdf_copy")
    for c in copies:
        if refusal := copy_refusal(c.url, policy):
            reasons.append(refusal)
            continue
        rights = combine(c.evidence)
        if not copy_acceptable(rights):
            reasons.append(f"rights_{rights.status}:{host_of(c.url)}")
            continue
        rank = (VERSION_RANK.get(c.version or "", 3), 0 if rights.url else 1,
                -len(c.evidence), c.url)
        eligible.append((rank, c, rights))
    if not eligible:
        return Resolution("unresolved", reasons)
    _, chosen, rights = min(eligible, key=lambda item: item[0])
    return Resolution("resolved", reasons, chosen, rights)


# --- version identity -----------------------------------------------------------------------------

def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch)).lower()


def title_tokens(title: str | None) -> set[str]:
    return {t for t in re.findall(r"\w+", _fold(title or "")) if len(t) > 2}


def surnames(authors) -> set[str]:
    out = set()
    for a in authors or []:
        name = a if isinstance(a, str) else ((a.get("author") or {}).get("display_name")
                                             or a.get("family") or a.get("name") or "")
        parts = re.findall(r"\w+", _fold(name))
        if parts:
            out.add(parts[-1])
    return out


def same_work(a: dict, b: dict) -> bool:
    """Convincing title + author evidence that two records are versions of one work:
    near-identical titles (token Jaccard >= 0.9, at least 4 tokens), a shared author surname, and
    publication years at most 3 apart. Anything weaker is ambiguous (False)."""
    ta, tb = title_tokens(a.get("title")), title_tokens(b.get("title"))
    if len(ta) < 4 or len(tb) < 4:
        return False
    if len(ta & tb) / len(ta | tb) < 0.9:
        return False
    if not surnames(a.get("authors")) & surnames(b.get("authors")):
        return False
    ya, yb = a.get("year"), b.get("year")
    return not (isinstance(ya, int) and isinstance(yb, int) and abs(ya - yb) > 3)


MAX_RELATIONS = 2  # related DOIs examined per work, explicit ones first (bounds lookups)


def crossref_related_dois(message: dict, record: dict | None = None,
                          lookup=None, limit: int = MAX_RELATIONS) -> list[tuple[str, str]]:
    """At most `limit` (relation type, DOI) pairs naming another version of this work. The
    candidates are chosen BEFORE any network call — explicit version relations first, in
    document order, then weaker relation types — and only the chosen weak ones are looked up
    (`lookup(doi)`, confirmed by same_work() against `record`). One work therefore costs at
    most `limit` relation lookups however many relations Crossref lists."""
    explicit, weak = [], []
    for rel_type, items in sorted((message.get("relation") or {}).items()):
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or item.get("id-type") != "doi":
                continue
            doi = normalize_doi(item.get("id"))
            if not doi:
                continue
            (explicit if rel_type in EXPLICIT_RELATIONS else weak).append((rel_type, doi))
    chosen = list(dict.fromkeys(explicit))[:limit]
    out = list(chosen)
    if record is not None and lookup is not None:
        seen = {doi for _, doi in chosen}
        for rel_type, doi in weak[:max(0, limit - len(chosen))]:
            if doi in seen:
                continue
            seen.add(doi)
            other = lookup(doi)
            if other is not None and same_work(record, other):
                out.append((rel_type, doi))
    return out


def abstract_text(work: dict) -> str:
    inv = work.get("abstract_inverted_index") or {}
    if not isinstance(inv, dict):
        return ""
    words = sorted((p, w) for w, ps in inv.items() for p in (ps or []) if isinstance(p, int))
    return " ".join(w for _, w in words)


def work_record(work: dict) -> dict:
    """The compact metadata kept for version matching and the resolution record."""
    return {
        "title": (work.get("title") or work.get("display_name") or "").strip(),
        "authors": [((a.get("author") or {}).get("display_name") or "")
                    for a in (work.get("authorships") or [])[:8]],
        "year": work.get("publication_year"),
    }
