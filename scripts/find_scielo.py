#!/usr/bin/env python3
"""find_scielo.py — AEC journals of the SciELO national collections (ArticleMeta API).

SciELO is the open-access backbone of Latin-American, Iberian and South African science: ~1,700
journals in national collections, nearly all CC-licensed, nearly all with a direct PDF. It is the
biggest body of PORTUGUESE and SPANISH built-environment literature on the open web — Ambiente
Construído, Revista IBRACON de Estruturas e Materiais, Soils and Rocks, urbe, Matéria (Brazil),
Revista ALCONPAT, Ingeniería sísmica, Tecnología y ciencias del agua (Mexico), Bitácora Urbano
Territorial, DYNA (Colombia), the Journal of the South African Institution of Civil Engineering —
and the quality gate's DOMAIN vocabulary already covers pt and es.

EACH NATIONAL HOST HAS ITS OWN robots.txt AND THEY DISAGREE — check before adding a collection.
Reviewed 2026-10-12 (honest UA; a sample PDF of every listed host returned %PDF):
  scl  www.scielo.br       `User-agent: * / Disallow: /admin / Allow: /` (three PDFs fetched
                           with the honest tool UA, 2026-10-12)
  mex  www.scielo.org.mx   no robots.txt (HTTP 404): nothing disallowed
  arg  www.scielo.org.ar   no robots.txt (HTTP 404)
  prt  scielo.pt           `User-Agent: * / Allow: /`
  cri  www.scielo.sa.cr    no robots.txt (HTTP 404)
  sza  www.scielo.org.za   `User-agent: * / Allow: / / Disallow: /storage/do_xml/id/` (not used)
  col  www.scielo.org.co   no robots.txt (HTTP 404); the host serves plain HTTP only (port 443
                           refuses), so its URLs are http:// — the site's own protocol
NO-GO, never mined: Chile (www.scielo.cl) and Peru (www.scielo.org.pe) serve
`User-agent: * Disallow: /` (Peru also names anthropic-ai and Claude-Web). Cuba (scielo.sld.cu),
Ecuador, Uruguay and Venezuela do not answer from this network. Spain, Bolivia and Paraguay have
no dedicated AEC journal.

Two ArticleMeta calls per article (articlemeta.scielo.org):
  article/identifiers/  -> a journal's article PIDs, paged by `offset`; `from=<date>` lists only
                           the records processed since that day (the watch sweep)
  article/              -> one article's metadata; `fulltexts.pdf` carries the PDF URL outright
When `fulltexts.pdf` is absent (most pre-2015 records) the PDF path is DERIVED from the SciELO
markup path in field v702 — legacy Windows paths ("C:\\SciELO\\Serial\\ric\\v22n1\\markup\\art06.html")
and modern relative ones ("ac/v26/1678-8621-AC-26-e149370.xml") both reduce to
<host>/pdf/<acron>/<issue>/<file>.pdf.

Ids are `sci-<pid>` rather than a title slug: the PID is stable and collision-free, so a round can
skip an already-held article WITHOUT spending a metadata request on it.

Licence (collect regardless of licence; the class picks the view), exact and per article:
  1. the article record's own `license` ("by/4.0", "by-nc-nd/3.0"): that Creative Commons tag
     with its canonical URL (open grants through the fail-closed parser scripts/licenses.py);
  2. else `unverified`. The journal's CURRENT licence (ArticleMeta journal record, field v541:
     "BY", "BY-NC/4.0", read once per run) is kept in the evidence text as context only: it does
     not establish the rights of every issue (Revista ALCONPAT 2015 articles are by-nc-nd/3.0
     while the journal record now says BY/4.0), so it never yields a licence tag.

Broad multidisciplinary engineering journals carry a title gate (`gate=True`): only titles with a
built-environment term in pt / es / en are proposed. Dedicated AEC journals are taken whole.

Rotation (dynamic, `--cursor`):
  "walk j=<journal index> o=<offset>"   the first walk of JOURNALS (append-only), journal-major
  "watch:<YYYY-MM-DD>"                  every journal listed through that day; WATCH_DAYS later a
                                        sweep starts
  "sweep:<since>:<started> j=<j> o=<o>" a sweep in progress: each journal's records processed
                                        since <since> minus SWEEP_MARGIN_DAYS; it ends as
                                        "watch:<started>"
A page of identifiers is all-or-nothing: an API failure keeps the cursor at that page. The backend
never reports EXHAUSTED.

    python scripts/find_scielo.py --cursor "walk j=11 o=0" --pages 1 --max 20
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
import yaml

import dedup
import licenses
import registry
from finder_protocol import Report

API = "https://articlemeta.scielo.org/api/v1"
UA = {"User-Agent": "nekaise-corpus/find_scielo (research corpus; robots.txt honoured)"}
DELAY = 0.3              # seconds between ArticleMeta requests
PER = 50                 # identifiers per page
WATCH_DAYS = 7
SWEEP_MARGIN_DAYS = 3    # a sweep re-lists this many days before its `since`
# The 11 journals of the first vein were walked to their end in 2026-08; the first sweep after
# the walk of the journals added later starts from this day for every journal.
FIRST_SWEEP_SINCE = "2026-07-15"

# Canonical base per collection, and every host spelling ArticleMeta uses for it. A collection
# missing here makes pdf_url return None, so no collection can be mined by accident before
# someone has read its robots.txt (see the module docstring).
HOSTS = {
    "scl": "https://www.scielo.br",
    "mex": "https://www.scielo.org.mx",
    "arg": "https://www.scielo.org.ar",
    "prt": "https://scielo.pt",
    "cri": "https://www.scielo.sa.cr",
    "sza": "https://www.scielo.org.za",
    "col": "http://www.scielo.org.co",    # plain HTTP only: port 443 refuses the connection
}
HOST_ALIASES = {
    "scl": {"www.scielo.br", "scielo.br"},
    "mex": {"www.scielo.org.mx", "scielo.org.mx"},
    "arg": {"www.scielo.org.ar", "scielo.org.ar"},
    "prt": {"scielo.pt", "www.scielo.pt", "www.scielo.mec.pt", "scielo.mec.pt"},
    "cri": {"www.scielo.sa.cr", "scielo.sa.cr"},
    "sza": {"www.scielo.org.za", "scielo.org.za"},
    "col": {"www.scielo.org.co", "scielo.org.co"},
}

# (collection, issn, topic, gate) — APPEND-ONLY: the cursor indexes this list. Chosen by WoS
# subject category from a scan of all collections (2026-07), hand-checked; counts and the
# journal-level licence (ArticleMeta v541) as probed on 2026-10-12.
JOURNALS = [
    # --- first vein (2026-07-28), walked to the end in 2026-08 ---
    ("scl", "1413-4152", "infrastructure", False),    # Engenharia Sanitaria e Ambiental (BY)
    ("scl", "1679-7825", "structures_civil", False),  # Latin American J. of Solids and Structures
    ("scl", "1678-8621", "construction", False),      # Ambiente Construído (BY, 1333)
    ("scl", "1983-4195", "structures_civil", False),  # Revista IBRACON de Estruturas e Materiais
    ("scl", "2175-3369", "urban", False),             # urbe. Rev. Brasileira de Gestão Urbana
    ("scl", "2448-167X", "materials", False),         # REM - International Engineering Journal
    ("scl", "2236-9996", "urban", False),             # Cadernos Metrópole
    ("scl", "2318-0331", "infrastructure", False),    # RBRH (water resources)
    ("scl", "2675-5475", "structures_civil", False),  # Soils and Rocks (geotechnics)
    ("scl", "2238-1031", "infrastructure", False),    # Journal of Transport Literature
    ("scl", "1516-1439", "materials", False),         # Materials Research (4368)
    # --- added 2026-10-12: dedicated AEC journals first, gated broad journals after them ---
    ("scl", "2317-1529", "urban", False),             # Rev. Bras. de Estudos Urbanos e Regionais (BY, 381)
    ("mex", "2007-6835", "materials", False),         # Revista ALCONPAT (BY/4.0, 253)
    ("mex", "2007-3011", "materials", False),         # Concreto y cemento (BY-NC/4.0, 60)
    ("mex", "0185-092X", "structures_civil", False),  # Ingeniería sísmica (BY-NC/4.0, 158)
    ("mex", "2007-2422", "infrastructure", False),    # Tecnología y ciencias del agua (BY-NC-SA/4.0, 708)
    ("mex", "2594-083X", "architecture", False),      # Academia XXII (BY-NC-ND/4.0, 106)
    ("arg", "2362-6097", "architecture", False),      # A y P continuidad (BY-NC-SA, 156)
    ("arg", "2250-4206", "architecture", False),      # Arquisur revista (BY-NC-ND, 53)
    ("arg", "1853-3655", "urban", False),             # Cuaderno urbano (BY-NC, 321)
    ("arg", "2362-2024", "architecture", False),      # Anales del IAA (architecture history, 426)
    ("arg", "2422-5703", "structures_civil", False),  # Rev. de geología aplicada a la ingeniería (21)
    ("prt", "2182-3030", "urban", False),             # CIDADES, Comunidades e Territórios (BY-NC, 442)
    ("prt", "0870-1164", "materials", False),         # Corrosão e Protecção de Materiais (BY-NC, 93)
    ("cri", "2215-3705", "infrastructure", False),    # Infraestructura Vial (BY-NC/3.0, 76)
    ("sza", "1021-2019", "structures_civil", False),  # J. of the SA Institution of Civil Engineering (BY-NC-ND/4.0, 388)
    ("sza", "2415-0487", "construction", False),      # Acta Structilia (BY/4.0, 116)
    ("sza", "2959-9652", "construction", False),      # J. of Construction Project Management and Innovation (31)
    ("sza", "2415-0495", "urban", False),             # Town and Regional Planning (BY/4.0, 166)
    ("col", "0124-7913", "urban", False),             # Bitácora Urbano Territorial (BY, 539)
    ("col", "1657-0308", "architecture", False),      # Revista de Arquitectura (Bogotá) (BY-NC)
    ("col", "1657-9763", "architecture", False),      # Apuntes: estudios sobre patrimonio cultural (BY)
    ("scl", "1517-7076", "materials", True),          # Matéria (Rio de Janeiro) (BY, 3067)
    ("scl", "0366-6913", "materials", True),          # Cerâmica (BY, 1733)
    ("scl", "0370-4467", "materials", True),          # Rem: Revista Escola de Minas (BY, 1024)
    ("scl", "1980-993X", "infrastructure", True),     # Revista Ambiente & Água (BY, 978)
    ("scl", "1982-2170", "infrastructure", True),     # Boletim de Ciências Geodésicas (524)
    ("mex", "1405-7743", "structures_civil", True),   # Ingeniería, investigación y tecnología (BY-NC-ND/4.0, 763)
    ("mex", "2594-1925", "materials", True),          # Revista de ciencias tecnológicas (BY/4.0, 85)
    ("mex", "0186-7210", "urban", True),              # Estudios demográficos y urbanos (BY-NC-ND/4.0, 564)
    ("mex", "1405-8421", "urban", True),              # Economía, sociedad y territorio (576)
    ("arg", "2250-4060", "urban", True),              # Quid 16 (urban studies) (BY-NC, 140)
    ("prt", "0870-8312", "materials", True),          # Ciência & Tecnologia dos Materiais (not declared, 112)
    ("prt", "1646-8872", "infrastructure", True),     # Revista de Gestão Costeira Integrada (BY-NC, 198)
    ("cri", "2215-2652", "structures_civil", True),   # Ingeniería (UCR) (BY-NC-SA/4.0, 43)
    ("cri", "0379-3982", "construction", True),       # Revista Tecnología en Marcha (BY-NC-ND/3.0, 603)
    ("sza", "1816-7950", "infrastructure", True),     # Water SA (BY/4.0, 1298)
    ("sza", "1021-447X", "building_energy", True),    # Journal of Energy in Southern Africa (BY-SA/4.0, 443)
    ("sza", "2225-6253", "structures_civil", True),   # J. of the Southern African Inst. of Mining and Metallurgy (BY/4.0)
    ("sza", "2310-8789", "infrastructure", True),     # J. of Transport and Supply Chain Management (BY-NC-ND/4.0)
    ("col", "0012-7353", "structures_civil", True),   # DYNA (BY-NC-ND)
    ("col", "0120-5609", "structures_civil", True),   # Ingeniería e Investigación (BY)
    ("col", "0122-3461", "structures_civil", True),   # Ingeniería y Desarrollo (BY)
    ("col", "0123-2126", "structures_civil", True),   # Ingeniería y Universidad (BY)
    ("col", "0120-6230", "structures_civil", True),   # Rev. Facultad de Ingeniería Univ. de Antioquia (BY)
    ("col", "0121-1129", "structures_civil", True),   # Revista Facultad de Ingeniería (UPTC) (BY)
    ("col", "0124-8170", "structures_civil", True),   # Ciencia e Ingeniería Neogranadina (BY-NC-ND)
    ("col", "1794-1237", "structures_civil", True),   # Revista EIA (BY-NC-ND)
    ("col", "0121-750X", "structures_civil", True),   # Ingeniería (Bogotá) (BY-NC-ND)
    ("col", "0123-3033", "structures_civil", True),   # Ingeniería y competitividad (BY-NC-SA)
    ("col", "1692-3324", "structures_civil", True),   # Revista Ingenierías Universidad de Medellín (BY-NC)
    ("col", "0123-921X", "structures_civil", True),   # Tecnura (BY-SA)
    ("col", "0121-4993", "structures_civil", True),   # Revista de Ingeniería (Uniandes) (BY)
]
FIRST_NEW_JOURNAL = 11

# Built-environment title terms in Portuguese, Spanish and English for the gated journals.
AEC_TITLE = re.compile(
    r"constru|edif[ií]c|\bbuildings?\b|viviendas?|habita[cç]|\bhousing\b|arquitet|arquitect|"
    r"architect|\burban|\bcidades?\b|\bciudad|\bcit(?:y|ies)\b|concret|hormig|ciment|cement|"
    r"argamassa|\bmorteros?\b|\bmortars?\b|alvenaria|mamposter|masonry|"
    # "structural" alone is also materials science ("structural characterization of starch"):
    # it counts only in an engineering phrase; other structural titles pass through their
    # concrete / steel / beam / bridge / building term
    r"engenharia estrutural|ingenier[ií]a estructural|(?:an[aá]lise|projeto|refor[cç]o|"
    r"seguran[cç]a|desempenho|comportamento) estrutural|(?:an[aá]lisis|dise[nñ]o|refuerzo|"
    r"seguridad|comportamiento) estructural|structural (?:engineering|safety|reliability|"
    r"health monitoring|strengthening|retrofit|design|analysis|behaviou?r|performance|"
    r"elements?|members?)\b|estruturas? (?:de|em|met[aá]lic)|estructuras? (?:de|met[aá]lic)|"
    r"p[oó]rticos?\b|treli[cç]as?\b|\bcerchas?\b|\btruss(?:es)?\b|\bvigas?\b|\bbeams?\b|\blajes?\b|\blosas?\b|\bslabs?\b|"
    r"\bpilar(?:es)?\b|\bcolumnas?\b|\bcolumns?\b|\bpontes?\b|\bpuentes?\b|\bbridges?\b|"
    r"pavim|asfalt|asphalt|geot[eé]c|geotech|\bsolos?\b|\bsuelos?\b|\bsoils?\b|"
    r"funda[cç][aã]o|funda[cç][oõ]es|cimentaci|\bfoundations?\b|\btalud|\bslopes?\b|"
    r"\bt[uú]ne(?:l|is|les)\b|\btunnel|maci[cç]o rochoso|\brock (?:mass|slope|support|bolt)|"
    r"s[ií]smic|seismic|earthquake|terremoto|saneamento|saneamiento|esgoto|aguas residuales|"
    r"águas residuais|wastewater|sewage|abastecim|water supply|drenagem|drenaje|drainage|"
    r"hidr[aá]ulic|hydraulic|barrage[mn]|\bpresas?\b|\bdams?\b|rodovi|carretera|highway|"
    r"\broads?\b|ferrovi|railway|tr[aá]fego|tr[aá]fico|\btraffic\b|"
    r"(?:conforto|desempenho|isolamento|condutividade|comportamento) t[eé]rmic|"
    r"(?:confort|desempeño|aislamiento|conductividad|comportamiento) t[eé]rmic|"
    r"thermal (?:comfort|performance|insulation|conductivity|behaviou?r of (?:build|wall|roof))|"
    r"climatiza|\bventila|\bhvac\b|ar condicionado|aire acondicionado|air[- ]condition|"
    r"ilumina[cç][aã]o natural|iluminaci[oó]n natural|daylight|efici[eê]ncia energ[eé]tica|"
    r"eficiencia energ[eé]tica|energy efficien|insulation|inc[eê]ndio|\bfire\b|madeira|"
    r"\bmadera|\btimber\b|\bwood\b|cer[aâ]mica vermelha|revestimento cer[aâ]mic|"
    r"bloco cer[aâ]mic|ceramic (?:tiles?|bricks?|blocks?)|tijolo|ladrillo|\bbricks?\b|\btelhas?\b|"
    r"agregado|aggregate|durabil|patologia|pathological|reabilita|rehabilit|retrofit|"
    r"\bbim\b|\bobras?\b|canteiro|infraestrutura|infraestructura|infrastructure|"
    r"fotovolt|photovolt|calefacci|\bgesso\b|\byeso\b|gypsum|\bcal\b|\blime\b|telhad|"
    r"\btechos?\b|\broofs?\b|fachad|fa[cç]ade|\bparedes?\b|\bmuros?\b|\bwalls?\b|\bpisos?\b|"
    r"\bfloors?\b|territori|res[ií]duos? de (?:constru|demoli)|geopol[ií]mer|geopolymer|"
    r"pozolan|pozzolan|cinza volante|fly ash|esc[oó]ria|\bslag\b|\bfibrocimento",
    re.I,
)
CC = re.compile(r"^(by|by-sa|by-nc|by-nd|by-nc-sa|by-nc-nd)(?:/(\d\.\d)(?:/([a-z]{2,3}))?)?$",
                re.I)
WALK = re.compile(r"^walk j=(\d+) o=(\d+)$")
WATCH = re.compile(r"^watch:(\d{4}-\d{2}-\d{2})$")
SWEEP = re.compile(r"^sweep:(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2}) j=(\d+) o=(\d+)$")


class Unexpected(RuntimeError):
    """An ArticleMeta answer that is not the documented JSON shape."""


def _get(path: str, **params):
    response = requests.get(f"{API}/{path}", params=params, headers=UA, timeout=60)
    if response.status_code != 200:
        raise Unexpected(f"ArticleMeta {path} answered HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as exc:
        raise Unexpected(f"ArticleMeta {path} answer is not JSON: {exc}") from exc


def _vals(a: dict, key: str) -> list:
    return a.get(key) or []


def pick_title(a: dict):
    """Article title, preferring the original language (v40) then English then whatever exists."""
    cands = [t for t in _vals(a, "v12") if t.get("_")]
    if not cands:
        return None
    orig = (_vals(a, "v40") or [{}])[0].get("_")
    for want in (orig, "en"):
        for t in cands:
            if want and t.get("l") == want:
                return t["_"]
    return cands[0]["_"]


def all_titles(a: dict) -> list[str]:
    return [t["_"] for t in _vals(a, "v12") if t.get("_")]


def derive_pdf(a: dict, collection: str):
    """<host>/pdf/<acron>/<issue>/<file>.pdf from the v702 markup path (legacy or modern)."""
    raw = (_vals(a, "v702") or [{}])[0].get("_") or ""
    base = HOSTS.get(collection)
    if not raw or not base:
        return None
    p = raw.replace("\\", "/")
    m = re.search(r"(?i)/serial/(.+)$", p)
    if m:
        p = m.group(1)
    p = re.sub(r"(?i)/markup/", "/", p)
    p = re.sub(r"\.[A-Za-z0-9]+$", ".pdf", p)
    p = p.lstrip("/")
    if not p.endswith(".pdf") or p.count("/") < 2:
        return None
    # The acronym segment is served lowercase even when v702 spells it "MR"/"AC"; the FILENAME is
    # case-sensitive the other way (1678-8621-AC-26-e149370.pdf), so lowercase only that one part.
    acron, rest = p.split("/", 1)
    return f"{base}/pdf/{acron.lower()}/{rest}"


def pdf_url(rec: dict, collection: str):
    """Prefer the PDF link the API states outright; fall back to deriving it from v702.

    Whichever path produced it, the result must sit on the reviewed host of ITS collection and is
    rebuilt on that host's canonical base (scheme included). `fulltexts.pdf` is filled in by
    SciELO and happily points at a national host we have NOT robots-cleared (it is how the
    reverted Venezuela entries got a www.scielo.org.ve URL), so it never bypasses that gate.
    """
    base = HOSTS.get(collection)
    if not base:
        return None
    pdfs = ((rec.get("fulltexts") or {}).get("pdf")) or {}
    a = rec.get("article") or {}
    url = None
    if pdfs:
        orig = (_vals(a, "v40") or [{}])[0].get("_")
        for want in (orig, "en", "pt", "es"):
            if want and pdfs.get(want):
                url = pdfs[want]
                break
        url = url or list(pdfs.values())[0]
    else:
        url = derive_pdf(a, collection)
    if not url or not isinstance(url, str):
        return None
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in HOST_ALIASES[collection] or \
            not parsed.path.startswith("/pdf/"):
        return None
    return f"{base}{parsed.path}"


def cc_licence(value: str | None) -> tuple[str, str | None] | None:
    """(tag, canonical licence URL or None) for a SciELO licence code ("by/4.0", "BY-NC",
    "by/3.0/br"), or None when the value is not a Creative Commons code. A jurisdiction port
    is a different instrument from the unported licence, so the URL keeps it."""
    match = CC.match((value or "").strip())
    if not match:
        return None
    kind, version = match.group(1).lower(), match.group(2)
    port = f"{match.group(3).lower()}/" if match.group(3) else ""
    url = f"https://creativecommons.org/licenses/{kind}/{version}/{port}" if version else None
    if kind in ("by", "by-sa"):
        if url is None:
            return f"cc-{kind}", None
        tag, canonical = licenses.cc_license([url])   # the shared fail-closed parser
        return (tag, canonical) if tag else None
    return f"cc-{kind}", url


def licence(rec: dict, journal_code: str | None, collection: str, pid: str,
            issn: str) -> tuple[str, str | None, str]:
    """(tag, licence URL, evidence): the article record's own licence, else unverified. The
    journal record's current licence is context in the evidence text, never a tag."""
    article_url = f"{API}/article/?collection={collection}&code={pid}&format=json"
    stated = rec.get("license")
    if isinstance(stated, str) and stated.strip():
        if found := cc_licence(stated):
            return found[0], found[1], (f"SciELO ArticleMeta article record license "
                                        f"'{stated.strip()}' ({article_url})")
        return "unverified", None, (f"SciELO ArticleMeta article record license "
                                    f"'{stated.strip()[:80]}' is not a Creative Commons code "
                                    f"({article_url})")
    journal_url = f"{API}/journal/?collection={collection}&issn={issn}"
    return "unverified", None, (f"SciELO ArticleMeta: the article record states no licence "
                                f"({article_url}); context only, not evidence for this "
                                f"article: the journal record's current licence (v541) is "
                                f"'{journal_code or ''}' ({journal_url})")


def journal_licence(collection: str, issn: str) -> str | None:
    """The journal's licence code (ArticleMeta journal record, field v541), or None."""
    data = _get("journal/", collection=collection, issn=issn)
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise Unexpected("ArticleMeta journal record is not an object")
    values = [v.get("_") for v in (data.get("v541") or []) if isinstance(v, dict) and v.get("_")]
    return values[0] if values else None


def candidate(rec: dict, collection: str, issn: str, topic: str, gate: bool,
              journal_code: str | None, pid: str, today: str) -> dict | None:
    article = rec.get("article") or {}
    title = pick_title(article)
    url = pdf_url(rec, collection)
    if not title or not url:
        return None
    if gate and not any(AEC_TITLE.search(t) for t in all_titles(article)):
        return None
    tag, licence_url, evidence = licence(rec, journal_code, collection, pid, issn)
    entry = {"id": f"sci-{pid.lower()}", "title": title.strip()[:150], "url": url,
             "source": f"scielo_{collection}", "license": tag, "topic": topic, "format": "pdf",
             "document_type": "journal-article", "persistent_id": pid,
             "license_evidence": evidence, "rights_verified_at": today}
    language = ((_vals(article, "v40") or [{}])[0].get("_") or "").lower()
    if re.fullmatch(r"[a-z]{2}", language):
        entry["language"] = language
    if licence_url:
        entry["license_url"] = licence_url
    if (doi := dedup.normalize_pid(rec.get("doi") or "")) and doi.startswith("doi:"):
        entry["origin_ids"] = doi
    return entry


def parse_cursor(cursor: str) -> dict:
    cursor = cursor.strip()
    state = None
    if m := WALK.match(cursor):
        state = {"mode": "walk", "j": int(m.group(1)), "o": int(m.group(2))}
    elif m := WATCH.match(cursor):
        return {"mode": "watch", "since": date.fromisoformat(m.group(1))}
    elif m := SWEEP.match(cursor):
        state = {"mode": "sweep", "since": date.fromisoformat(m.group(1)),
                 "started": date.fromisoformat(m.group(2)), "j": int(m.group(3)),
                 "o": int(m.group(4))}
    if state is not None:
        # an index past the table is a corrupt pointer, never "all journals walked"
        if state["j"] >= len(JOURNALS):
            raise ValueError(f"cursor names journal {state['j']} of {len(JOURNALS)}: {cursor!r}")
        return state
    raise ValueError("cursor must be 'walk j=<i> o=<n>', 'watch:<date>' or "
                     f"'sweep:<since>:<started> j=<i> o=<n>': {cursor!r}")


def format_cursor(state: dict) -> str:
    if state["mode"] == "walk":
        return f"walk j={state['j']} o={state['o']}"
    if state["mode"] == "watch":
        return f"watch:{state['since'].isoformat()}"
    return (f"sweep:{state['since'].isoformat()}:{state['started'].isoformat()} "
            f"j={state['j']} o={state['o']}")


def read_page(state: dict, keys, journal_codes: dict, today: str) -> tuple[list[dict], bool]:
    """(new entries of one identifiers page, whether the journal is finished). Raises on any
    API failure or inconsistent answer (a listing whose length does not match meta.total, an
    identifier or article record of another shape), so a page is all-or-nothing."""
    collection, issn, topic, gate = JOURNALS[state["j"]]
    params = {"collection": collection, "issn": issn, "offset": state["o"], "limit": PER}
    if state["mode"] == "sweep":
        params["from"] = (state["since"] - timedelta(days=SWEEP_MARGIN_DAYS)).isoformat()
    listing = _get("article/identifiers/", **params)
    objects = listing.get("objects") if isinstance(listing, dict) else None
    total = (listing.get("meta") or {}).get("total") if isinstance(listing, dict) else None
    if not isinstance(objects, list) or not isinstance(total, int) or isinstance(total, bool) \
            or total < 0:
        raise Unexpected("ArticleMeta identifiers answer has no objects / meta.total")
    expected = max(0, min(PER, total - state["o"]))
    if len(objects) != expected:
        raise Unexpected(f"ArticleMeta listed {len(objects)} identifiers at offset "
                         f"{state['o']} where meta.total {total} means {expected}")
    pids = []
    for obj in objects:
        code = obj.get("code") if isinstance(obj, dict) else None
        if not isinstance(code, str) or not code.strip() or \
                (obj.get("collection") or collection) != collection:
            raise Unexpected(f"ArticleMeta identifier is not a {collection} article code: "
                             f"{str(obj)[:80]}")
        pids.append(code)
    keys.prefetch(ids=[f"sci-{pid.lower()}" for pid in pids])
    entries = []
    for pid in dict.fromkeys(pids):
        if f"sci-{pid.lower()}" in keys.ids:   # already held — skip WITHOUT a metadata request
            continue
        if (collection, issn) not in journal_codes:
            time.sleep(DELAY)
            journal_codes[(collection, issn)] = journal_licence(collection, issn)
        time.sleep(DELAY)
        rec = _get("article/", collection=collection, code=pid, format="json")
        if not isinstance(rec, dict) or rec.get("code") != pid or \
                not isinstance(rec.get("article"), dict) or not rec["article"]:
            raise Unexpected(f"ArticleMeta answer for {pid} is not that article's record: "
                             f"{str(rec)[:80]}")
        entry = candidate(rec, collection, issn, topic, gate,
                          journal_codes[(collection, issn)], pid, today)
        if entry:
            entries.append(entry)
    finished = state["o"] + PER >= total
    return entries, finished


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", default=f"walk j={FIRST_NEW_JOURNAL} o=0",
                    help="'walk j=<i> o=<n>', 'watch:<date>' or 'sweep:...' (rotation pointer)")
    ap.add_argument("--pages", type=int, default=2,
                    help=f"max identifier pages ({PER} articles each) this run")
    ap.add_argument("--max", type=int, default=100,
                    help="page-granular target for new entries (the last page may exceed it)")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/scielo.yaml)")
    args = ap.parse_args()
    if args.pages < 1 or args.max < 1:
        ap.error("--pages and --max must be positive")
    try:
        state = parse_cursor(args.cursor)
    except ValueError as exc:
        ap.error(str(exc))
    report = Report()
    now = datetime.now(timezone.utc).date()
    today = now.isoformat()
    if state["mode"] == "watch":
        due = state["since"] + timedelta(days=WATCH_DAYS)
        if now < due:
            print(f"# 0 NEW SciELO articles (journals listed through {state['since']}; next "
                  f"sweep {due})")
            report.next(format_cursor(state))
            return
        state = {"mode": "sweep", "since": state["since"], "started": now, "j": 0, "o": 0}

    keys = dedup.open_keys()
    out: list[dict] = []
    journal_codes: dict = {}
    pages = 0
    # Every journal that ends before the page budget is spent costs one request; a sweep over
    # 60 quiet journals therefore needs the larger budget below, never more article lookups.
    budget = args.pages if state["mode"] == "walk" else max(args.pages, len(JOURNALS))
    while state["j"] < len(JOURNALS) and pages < budget and len(out) < args.max:
        if pages:
            time.sleep(DELAY)
        try:
            entries, finished = read_page(state, keys, journal_codes, today)
        except Unexpected as exc:
            if not pages:
                report.hold(f"{exc} at {format_cursor(state)}; nothing proposed")
                print("# 0 NEW SciELO articles (unexpected ArticleMeta answer)")
                return
            print(f"# {exc}; stopping after {pages} complete pages", file=sys.stderr)
            break
        except requests.RequestException as exc:
            if not pages:
                print(f"# ERROR: ArticleMeta request failed at {format_cursor(state)}: {exc}; "
                      "refusing a partial append so rotation does not advance", file=sys.stderr)
                raise SystemExit(1)
            print(f"# {exc}; stopping after {pages} complete pages", file=sys.stderr)
            break
        pages += 1
        keys.prefetch(**dedup.page_keys(entries))
        for entry in entries:
            u, t = entry["url"].rstrip("/"), registry.norm(entry["title"])
            # a DOI makes the article known when OpenAlex already brought another copy of it
            has_doi = bool(entry.get("origin_ids"))
            if u in keys.urls or t in keys.titles or (has_doi and keys.identity_known(entry)):
                continue
            keys.urls.add(u)
            keys.titles.add(t)
            if has_doi:
                keys.add_identity(entry)
            out.append(entry)
        if finished:
            state["j"], state["o"] = state["j"] + 1, 0
        else:
            state["o"] += PER

    if state["j"] >= len(JOURNALS):
        state = {"mode": "watch", "since": state["started"] if state["mode"] == "sweep"
                 else date.fromisoformat(FIRST_SWEEP_SINCE)}

    by_source: dict[str, int] = {}
    by_licence: dict[str, int] = {}
    for entry in out:
        by_source[entry["source"]] = by_source.get(entry["source"], 0) + 1
        by_licence[entry["license"]] = by_licence.get(entry["license"], 0) + 1
    print(f"# {len(out)} NEW SciELO AEC articles over {pages} identifier pages (pt/es/en, "
          "deduped vs manifest + registry + blocklist)")
    print(f"# by collection: {by_source}; by licence: {by_licence}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))
    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    report.next(format_cursor(state))   # every state built above round-trips parse_cursor


if __name__ == "__main__":
    main()
