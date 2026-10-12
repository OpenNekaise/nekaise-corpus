#!/usr/bin/env python3
"""find_repo.py — theses, dissertations and reports on the built environment from university and
institute repositories, harvested over OAI-PMH (one finder, one reviewed config file).

Institutional repositories are the long tail of AEC knowledge: doctoral and master theses,
institute reports and working papers in the language of the institution. Each repository in
`registry/repositories.json` was vetted by hand (robots.txt, terms of use, sample downloads) and
carries that evidence in its `rights` record, like the vendors in registry/vendors.json. OAI-PMH is
the protocol repositories publish for harvesters. The review is not trusted forever: before
every request and every redirect hop (redirects are followed by hand) the finder checks the
pinned host policy and the host's CURRENT robots.txt (scripts/robots_policy.py), stays on the
repository's reviewed hosts, and waits max(configured crawl delay, robots Crawl-delay, MIN_DELAY)
on a per-host clock, with an honest User-Agent. A new Disallow, an unreadable robots.txt or an
unexpected redirect defers that repository. The loader fetches the proposed files as polite hosts
(serial, paced, honest UA, per-run cap; `repo_config.loader_hosts`, kept for disabled
repositories too), defers `rep-` rows when that configuration cannot be loaded, and
robots-checks the download URL and every redirect hop (build_corpus.ROBOTS_CHECKED_PREFIXES:
Disallow, AI-training opt-out or unavailable robots.txt defers the row; no fallback client).

Platforms (adapters; `PLATFORMS`): `dspace-xoai` — DSpace's `xoai` metadata format, which lists
every dc field and the ORIGINAL bundle's bitstream URLs. Other platforms (EPrints oai_dc,
xMetaDissPlus, Pure) are backlog: an adapter is a function (record element, config) -> Record.

A record is kept only when
  * it is not deleted and has a PDF bitstream in the ORIGINAL bundle whose URL matches the
    repository's `fulltext_pattern` (same host);
  * it is not embargoed or restricted: a repository with an access field (`open_access`) must
    state the open value, an embargo field with a future or unreadable date drops the record,
    and an access term other than open in a rights / access field drops it (openAccess /
    "restricted access" spellings, info:eu-repo terms, COAR access-right URIs); fail closed.
    DSpace does not mark bitstream-level restrictions in xoai, so a repository without an
    access field can still propose a restricted file: the loader then gets no PDF and the row
    fails (the hosts are polite hosts: a 403 pauses that host for the rest of the run);
  * its document type matches the repository's `types` (theses, dissertations, reports, working
    papers, books; default TYPES) — datasets, images, presentations and journal articles are not
    harvested here (OpenAlex covers the articles);
  * it is on-topic (precision first, the quality gate is the second safeguard): the repository
    or set is dedicated to the built environment (`relevance: all`, `all_sets`), or a reviewed
    metadata field names a built-environment department or degree (`relevant_fields`), or a
    Dewey class of building / civil engineering / planning / architecture (624-628, 69x, 71x,
    72x) is stated, or the title names an unambiguous AEC concept in English
    (scripts/bes_relevance.py plus civil / structural / construction terms) or, for a record in
    one of the repository's other languages, in that language; a title with an off-domain
    veto never passes (in a dedicated set or department only the hard veto applies).

Licence (collect regardless of licence, classify exactly): from the record's rights values. Every
value that is a URL or names a licence must be a canonical open Creative Commons URL for an open
tag (scripts/licenses.py); a single canonical NC / ND licence URL keeps its exact tag; anything
else — rightsstatements.org "In Copyright", free text, no statement — is `unverified` with the
statement as evidence.

Rotation (dynamic `--cursor`, a JSON object): {"r": <round-robin position>, "s": {<key>: state}}.
A repository's state is {"i": set index, "t": resumption token, "f": OAI from-date of this pass,
"d": date this pass started, "w": date the last pass ended}. The first pass walks every set's
history; after it the repository is revisited WATCH_DAYS later with `from=` the previous pass's
start date, so new and changed records keep arriving. The backend never reports EXHAUSTED. An
unexpected answer or a network failure keeps that repository's state and proposes nothing for
it; the round-robin position still advances, so the other repositories are read and the failing
one is retried on its next turn. A rejected token restarts the set once.

    python scripts/find_repo.py --max 20 --pages 2
    python scripts/find_repo.py --only chalmers-odr --pages 1
"""
from __future__ import annotations

import argparse
import contextlib
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

import requests
import yaml

import bes_relevance
import dedup
import host_policy
import licenses
import registry
import robots_policy
import store
from finder_protocol import MAX_CURSOR, Report
from repo_config import (DATE, LANGUAGES, MIN_DELAY, RUN_SECONDS, load_config,  # noqa: F401
                         loader_hosts, validate)

UA = {"User-Agent": "nekaise-corpus/find_repo (research corpus; robots.txt honoured)"}
ROBOTS_MAX_DELAY = 30.0   # a larger robots Crawl-delay defers the repository
MAX_REDIRECTS = 5
REDIRECTS = frozenset({301, 302, 303, 307, 308})
WATCH_DAYS = 7         # a walked repository is read again (from=) this many days later
PREFIX = "rep-"
OAI_NS = "{http://www.openarchives.org/OAI/2.0/}"
XOAI_NS = "{http://www.lyncode.com/xoai}"

# Document types harvested by default (dc.type values in any of the repositories' languages).
TYPES = re.compile(
    r"thes[ie]s|dissertat|doctoral|master|bachelor|licentiat|habilitat|report|working ?paper|"
    r"monogra|\bbook\b|student ?(?:paper|work)|examensarbete|opinnäyte|diplomity|väitöskirj|"
    r"pro gradu|tesi|tese\b|tesis|trabajo fin|treball fi|trabalho de conclus|\btcc|"
    r"dissertaç|mémoire|thèse|"
    r"abschlussarbeit|masterarbeit|diplomarbeit|hochschulschrift|bericht|rapport|raportti|"
    r"info:eu-repo/semantics/(?:doctoralThesis|masterThesis|bachelorThesis|report|book|"
    r"workingPaper)", re.I)
NOT_TYPES = re.compile(r"dataset|research data|image|video|sound|software|presentation|poster|"
                       r"patent|journal article|review article|newspaper", re.I)

# Dewey classes of the built environment in a classification value ("DDC::624 - Civil ...",
# "ddc:690", "720 Architecture").
DDC_BUILT = re.compile(r"(?<![\d.])(?:62[4-8]|69\d|71\d|72\d)(?![\d])")
DDC_FIELD = re.compile(r"ddc|dewey", re.I)
GERUND = re.compile(r"\bbuilding (?=(?:an?|the|on|up|upon|better|trust|resilien\w+|capacity|"
                    r"bridges|blocks?|towards?|and (?:testing|evaluating|deploying)|"
                    r"[a-z]+ (?:models?|systems?|tools?|pipelines?|frameworks?|communit\w+))\b)",
                    re.I)
# Unambiguous English AEC concepts beyond bes_relevance.BUILT (which is building-energy centred).
AEC_EN = re.compile(
    r"\bcivil engineering\b|\bstructural (?:engineering|design|analysis|health monitoring|"
    r"behaviou?r|performance|safety|reliability|elements?|members?|systems?|steel|glass)\b|"
    r"\b(?:reinforced|prestressed|precast|high[- ]strength|fib(?:er|re)[- ]reinforced|"
    r"self[- ]compacting|recycled aggregate) concrete\b|\bconcrete (?:structures?|beams?|slabs?|"
    r"columns?|walls?|bridges?|pavements?|members?|frames?|durability|mix(?:es|tures?)?)\b|"
    r"\bcement(?:itious)?\b|\bmasonry\b|\bmortars?\b|\btimber (?:structures?|construction|"
    r"buildings?|frames?|beams?|joints?|connections?)\b|\bcross[- ]laminated timber\b|\bglulam\b|"
    r"\bsteel (?:structures?|frames?|bridges?|beams?|columns?|connections?|construction)\b|"
    r"\b(?:road|railway|highway|suspension|cable[- ]stayed|arch|girder|footbridges?|pedestrian|"
    r"composite|steel|concrete|masonry|timber) bridges?\b|\bbridge (?:decks?|design|piers?|"
    r"girders?|engineering|inspection|maintenance|structures?)\b|\btunnell?ing\b|"
    r"\b(?:road|railway|metro|highway) tunnels?\b|\bpavements?\b|\basphalt\b|\bbitum|"
    r"\bgeotechn|\bsoil mechanics\b|\bfoundation engineering\b|\bpile foundations?\b|"
    r"\bretaining walls?\b|\bslope stability\b|\bearthquake engineering\b|"
    r"\bseismic (?:design|retrofit|performance|response|assessment|vulnerability|isolation|"
    r"behaviou?r|analysis)\b|\bconstruction (?:management|industry|sector|projects?|sites?|"
    r"materials?|process(?:es)?|engineering|technology|safety|waste|logistics|contracts?|"
    r"productivity|costs?|works?)\b|\bbuilding information model|\bbim\b|\bprefabricat|"
    r"\burban (?:planning|design|development|regeneration|renewal|form|morpholog|density|"
    r"drainage|heat island|districts?|infrastructures?)\b|\bspatial planning\b|"
    r"\bland[- ]use planning\b|\btown planning\b|\bcity planning\b|\barchitectur(?:e|al) "
    r"(?:design|theory|history|heritage|practice|education|competitions?)\b|\barchitects?\b|"
    r"\bhousing\b|\bwastewater\b|\bsewer|\bsewage\b|\bstormwater\b|\bdrinking water\b|"
    r"\bwater (?:supply|distribution) (?:systems?|networks?)\b|\bfire (?:safety|resistance|"
    r"protection|engineering)\b|\bfacilit(?:y|ies) management\b|\breal estate\b|"
    r"\b(?:road|railway|transport|port|harbou?r) (?:infrastructures?|engineering)\b|"
    r"\brailway tracks?\b|\bhydraulic (?:structures?|engineering)\b|\bdams?\b(?= (?:safety|"
    r"design|construction|break|engineering))|\bcoastal (?:engineering|structures?|protection)\b|"
    r"\bbuilding (?:physics|materials?|services|technology|design|envelopes?|stock|"
    r"regulations?|performance|acoustics|renovation|construction)\b|\bheritage buildings?\b|"
    r"\bhistoric(?:al)? (?:buildings?|structures?|masonry)\b|\bdemolition\b",
    re.I)
# Unambiguous AEC stems per language (Latin script): matched on title + subjects.
AEC_LANG = {
    "de": r"gebäude|bauwesen|bauingenieur|bauwerk|baustoff|bauteil|baukonstruktion|bauphysik|"
          r"baubetrieb|bauwirtschaft|bauprojekt|bautechnik|hochbau|tiefbau|tragwerk|stahlbau|"
          r"holzbau|massivbau|mauerwerk|\bbeton|stahlbeton|spannbeton|brückenbau|\bbrücken\b|"
          r"tunnelbau|geotechnik|grundbau|bodenmechanik|straßenbau|städtebau|stadtplanung|"
          r"raumplanung|architektur|wohnungsbau|wohngebäude|sanierung|wärmedämmung|"
          r"wärmepumpe|heizung|lüftung|raumklima|fernwärme|brandschutz|erdbeben|"
          r"siedlungswasser|abwasser|denkmalpflege|fassade",
    "fr": r"bâtiment|génie civil|\bbéton|maçonnerie|charpente|ouvrages? d'art|\bponts?\b|"
          r"géotechnique|mécanique des sols|chaussée|construction (?:métallique|bois|durable)|"
          r"urbanisme|architecture|logement|rénovation|isolation thermique|chauffage|"
          r"climatisation|pompe à chaleur|parasismique|assainissement|eaux usées|"
          r"sécurité incendie",
    "es": r"edificio|edificación|ingeniería civil|hormigón|concreto (?:armado|reforzado)|"
          r"mampostería|albañilería|estructuras? (?:de|metálicas?)|puentes?\b|túnel|geotecni|"
          r"cimentaci|pavimento|urbanismo|arquitectura|vivienda|rehabilitación|"
          r"aislamiento térmico|calefacción|climatización|bomba de calor|sísmic|"
          r"aguas residuales|saneamiento|construcción",
    "ca": r"edifici|edificació|enginyeria civil|formigó|estructures?\b|ponts?\b|túnel|geotècni|"
          r"fonamentaci|paviment|urbanisme|arquitectura|habitatge|rehabilitació|"
          r"aïllament tèrmic|calefacció|climatització|sísmic|aigües residuals|construcció",
    "pt": r"edifício|edificaç|engenharia civil|\bbetão|concreto (?:armado|protendido)|"
          r"alvenaria|estruturas? (?:de|metálicas?)|pontes?\b|túnel|geotecni|fundações|"
          r"pavimento|urbanismo|arquitetura|arquitectura|habitação|reabilitação|"
          r"isolamento térmico|climatização|sísmic|águas residuais|saneamento|construção",
    "it": r"edifici|edilizia|ingegneria civile|calcestruzzo|cemento armato|muratura|"
          r"strutture? (?:in|di)|ponti\b|galleri[ae]|geotecnic|fondazioni|pavimentazion|"
          r"urbanistica|architettura|abitazion|riqualificazione|isolamento termico|"
          r"riscaldamento|climatizzazione|pompa di calore|sismic|acque reflue|costruzion",
    "nl": r"gebouw|bouwkunde|bouwfysica|civiele techniek|\bbeton|metselwerk|staalconstructie|"
          r"houtbouw|\bbruggen\b|tunnelbouw|geotechniek|fundering|wegenbouw|stedenbouw|"
          r"architectuur|woningbouw|renovatie|warmtepomp|verwarming|ventilatie|brandveiligheid|"
          r"afvalwater",
    "sv": r"byggnad|\bbygg|husbyggnad|byggteknik|betong|murverk|träbyggnad|stålbyggnad|"
          r"konstruktionsteknik|\bbroar?\b|\bbro(?:byggnad|konstruktion)|tunnel|geoteknik|"
          r"grundläggning|vägbyggnad|stadsplanering|samhällsbyggnad|arkitektur|bostad|bostäder|"
          r"renovering|värmepump|uppvärmning|ventilation|inomhusklimat|fjärrvärme|brandskydd|"
          r"avloppsvatten|fastighet",
    "no": r"bygning|\bbygg|betong|murverk|trekonstruksjon|stålkonstruksjon|\bbruer?\b|tunnel|"
          r"geoteknikk|fundamentering|vegbygging|byplanlegging|arkitektur|bolig|rehabilitering|"
          r"varmepumpe|oppvarming|ventilasjon|inneklima|fjernvarme|brannsikkerhet|avløp",
    "da": r"bygning|\bbygg|byggeri|beton|murværk|trækonstruktion|stålkonstruktion|\bbroer\b|"
          r"tunnel|geoteknik|fundering|vejbygning|byplanlægning|arkitektur|bolig|renovering|"
          r"varmepumpe|opvarmning|ventilation|indeklima|fjernvarme|brandsikkerhed|spildevand",
    "fi": r"rakennu|rakenta|betoni|muurattu|puurakent|teräsrakent|\bsil(?:ta|lan|lat|tojen)|"
          r"tunneli|geotekni|perustus|tierakennu|kaupunkisuunnittelu|yhdyskuntasuunnittelu|"
          r"arkkitehtuuri|asuin|asunto|korjausrakentaminen|lämpöpumppu|lämmity|ilmanvaihto|"
          r"sisäilma|kaukolämp|paloturvallisuus|jätevesi|kiinteistö|talotekni|infrarakent",
}
AEC_LANG_RE = {lang: re.compile(pattern, re.I) for lang, pattern in AEC_LANG.items()}
assert set(AEC_LANG) | {"en"} == set(LANGUAGES), "repo_config.LANGUAGES is out of step"
# (pattern -> topic) over title + subjects (all languages); first match wins.
TOPIC_RULES = (
    (re.compile(r"heat ?pump|wärmepumpe|värmepump|varmepumpe|lämpöpumppu|\bhvac\b|ventilat|"
                r"lüftung|ilmanvaihto|chauffage|calefacc|heizung|uppvärmning|lämmity|"
                r"climatiz|climatis|air[- ]condition|boiler|chiller", re.I), "equipment_systems"),
    (re.compile(r"energy|energi|énergie|energía|thermal comfort|indoor|inomhus|sisäilma|"
                r"insulat|dämmung|isolation thermique|daylight|retrofit|sanierung", re.I),
     "building_energy"),
    (re.compile(r"concrete|cement|beton|betong|betoni|béton|hormigón|betão|formigó|"
                r"calcestruzzo|masonry|mauerwerk|timber|holzbau|baustoff|mortar|asphalt|bitum|"
                r"building materials?", re.I), "materials"),
    (re.compile(r"struct|tragwerk|konstruktion|bridge|brücke|\bbro|pont|puente|seismic|"
                r"earthquake|erdbeben|sísmic|geotech|geotekn|foundation|fundament|tunnel|steel",
                re.I), "structures_civil"),
    (re.compile(r"construction (?:manag|industr|sector|project|site|process|safety|logist|"
                r"cost|product)|baubetrieb|bauwirtschaft|bauprojekt|byggprod|byggproj|\bbim\b|"
                r"building information|prefab|rakennuttam|työmaa", re.I), "construction"),
    (re.compile(r"wastewater|abwasser|sewer|sewage|stormwater|drinking water|water supply|"
                r"avlopp|jätevesi|pavement|road|railway|straßenbau|vägbygg|tierakenn|"
                r"infrastruct|hydraulic|coastal|\bdam\b", re.I), "infrastructure"),
    (re.compile(r"urban|städtebau|stadtplan|raumplan|stadsplan|samhällsbygg|urbanis|"
                r"kaupunkisuunn|yhdyskunta|spatial planning|housing|wohnungsbau|bostad|bolig|"
                r"logement|vivienda|habita", re.I), "urban"),
    (re.compile(r"architect|architekt|arkitekt|arkkiteht|arquitect|arquitet|fassade|"
                r"fa[cç]ade|heritage|denkmal", re.I), "architecture"),
)
LANG3 = {"eng": "en", "ger": "de", "deu": "de", "fre": "fr", "fra": "fr", "spa": "es",
         "cat": "ca", "por": "pt", "ita": "it", "dut": "nl", "nld": "nl", "swe": "sv",
         "nor": "no", "nob": "no", "nno": "no", "dan": "da", "fin": "fi"}
RESTRICTED_CC = re.compile(r"https?://(?:www\.)?creativecommons\.org/licenses/"
                           r"(by-nc-nd|by-nc-sa|by-nc|by-nd)/\d\.\d(?:/[a-z]{2,5})?"
                           r"(?:/(?:legalcode(?:\.[a-z]{2})?|deed\.[a-z]{2}))?/?", re.I)
# An access-rights term, with or without the info:eu-repo prefix ("openAccess",
# "embargoedAccess (2 Years)", "info:eu-repo/semantics/restrictedAccess").
ACCESS_TERM = re.compile(r"(?:info:eu-repo/semantics/)?\b(open|closed|embargoed|restricted)"
                         r"[ _-]?access\b", re.I)
# Fields that carry access or rights statements (access terms are only read there, so an
# abstract about "restricted access" is not a restriction), licence statements, and subjects.
# COAR access-right vocabulary URIs: c_abf2 open access, c_f1cf embargoed, c_16ec restricted,
# c_14cb metadata only.
COAR_ACCESS = re.compile(r"https?://purl\.org/coar/access_right/(c_[0-9a-f]{4})/?$", re.I)
RIGHTS_PATH = re.compile(r"rights|access|availab|license|licence")
LICENCE_PATH = re.compile(r"(?:dc|dcterms)\.(?:rights|license)(?:\.|$)")
SUBJECT_PATH = re.compile(r"(?:dc|dcterms)\.subject(?:\.|$)|dcterms\.LCSH$")
LANG_NAMES = {"english": "en", "german": "de", "deutsch": "de", "french": "fr", "spanish": "es",
              "portuguese": "pt", "italian": "it", "swedish": "sv", "finnish": "fi",
              "chinese": "zh", "japanese": "ja", "korean": "ko", "dutch": "nl"}


class Unexpected(RuntimeError):
    """Not an OAI-PMH ListRecords answer (challenge, maintenance page, error)."""


# ---------------------------------------------------------------------------------------------
# OAI-PMH + DSpace xoai
POLICY: dict[str, dict] = {}      # pinned host fetch policy (install_policy)
_last: dict[str, float] = {}      # host -> start of its last request (monotonic)


def load_policy() -> dict:
    """The host fetch policy pinned with the data the round validates (never a working-tree
    file); raises when it cannot be established."""
    with store.open().read(timeout=60) as view:
        return store.pinned_policy(view)[1]


def repository_hosts(cfg: dict) -> frozenset[str]:
    """The hosts a repository's discovery may ask: its OAI host and its full-text hosts."""
    return frozenset({host_policy.canonical_host(cfg["oai"]), *cfg["hosts"]})


def _pace(url: str, delay: float) -> None:
    """One clock per host: the next request starts `delay` seconds after the previous START."""
    host = host_policy.canonical_host(url)
    now = time.monotonic()
    start = now if host not in _last else max(now, _last[host] + delay)
    _last[host] = start
    if start > now:
        time.sleep(start - now)


@contextlib.contextmanager
def _robots_slot(url: str):
    """robots.txt requests share the host clock of the OAI requests."""
    _pace(url, MIN_DELAY)
    yield


def install_policy(policy: dict, repos: dict[str, dict]) -> None:
    """Install the host policy for OAI requests and for robots.txt requests; a robots.txt
    redirect may only go to a host of a configured repository."""
    hosts = frozenset().union(*(repository_hosts(cfg) for cfg in repos.values())) if repos \
        else frozenset()
    POLICY.clear()
    POLICY.update(policy or {})
    robots_policy.set_policy(policy)
    robots_policy.set_pacer(_robots_slot)
    robots_policy.set_hop_filter(lambda url: host_policy.canonical_host(url) in hosts)


def polite_get(url: str, params: dict, cfg: dict) -> bytes:
    """GET with manual redirects. Before EVERY hop, on the exact URL that will be sent: the
    hop is https on one of the repository's reviewed hosts, the host is not fetch-suspended
    (registry/host_policy.json), robots.txt (scripts/robots_policy.py, read at run time, cached
    24 h) allows the path and carries no AI-training opt-out covering it
    (robots_policy.ai_training_refused), and the host clock waits max(configured crawl delay, robots
    Crawl-delay). A refusal, an unavailable robots.txt or a Crawl-delay above ROBOTS_MAX_DELAY
    raises Unexpected: the repository keeps its state and is asked again on its next turn."""
    hop = requests.Request("GET", url, params=params).prepare().url
    hosts = repository_hosts(cfg)
    for _ in range(MAX_REDIRECTS + 1):
        host = host_policy.canonical_host(hop)
        if urlsplit(hop).scheme != "https" or host not in hosts:
            raise Unexpected(f"request or redirect to an unreviewed destination: {hop[:120]}")
        if host_policy.suspended(hop, POLICY):
            raise Unexpected(f"{host} is fetch-suspended (registry/host_policy.json)")
        try:
            allowed, robots_delay = robots_policy.decision(hop)
        except robots_policy.RobotsUnavailable as exc:
            raise Unexpected(f"robots.txt unavailable: {exc}") from exc
        if not allowed:
            raise Unexpected(f"robots.txt disallows {hop[:120]}")
        try:  # an AI / dataset crawler opt-out added after the review skips the repository
            opt_out = robots_policy.ai_training_refused(hop)
        except robots_policy.RobotsUnavailable as exc:
            raise Unexpected(f"robots.txt unavailable: {exc}") from exc
        if opt_out:
            raise Unexpected(f"robots.txt of {host} opts out of AI training ({opt_out})")
        delay = max(float(cfg["crawl_delay"]), MIN_DELAY, float(robots_delay or 0.0))
        if delay > ROBOTS_MAX_DELAY:
            raise Unexpected(f"robots.txt Crawl-delay {delay:g} s for {host} exceeds "
                             f"{ROBOTS_MAX_DELAY:g} s")
        _pace(hop, delay)
        response = requests.get(hop, headers=UA, timeout=(10, 180), allow_redirects=False)
        location = response.headers.get("location") if response.headers else None
        if response.status_code in REDIRECTS and location:
            hop = requests.Request("GET", urljoin(hop, location)).prepare().url
            continue
        if response.status_code != 200:
            raise Unexpected(f"OAI answered HTTP {response.status_code}")
        return response.content
    raise Unexpected(f"more than {MAX_REDIRECTS} redirects from {url}")


def fetch_page(cfg: dict, setspec: str, token: str, from_date: str) -> bytes:
    if token:
        params = {"verb": "ListRecords", "resumptionToken": token}
    else:
        params = {"verb": "ListRecords", "metadataPrefix": "xoai"}
        if setspec:
            params["set"] = setspec
        if from_date:
            params["from"] = from_date
    return polite_get(cfg["oai"], params, cfg)


def parse_page(body: bytes, *, has_token: bool) -> tuple[list[ET.Element], str]:
    """(metadata elements of non-deleted records, next token or ''). An initial noRecordsMatch is
    an empty finished window; badResumptionToken raises LookupError; anything that is not an
    OAI-PMH ListRecords answer raises Unexpected."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise Unexpected(f"OAI answer is not XML: {exc}") from exc
    if root.tag != f"{OAI_NS}OAI-PMH":
        raise Unexpected(f"OAI answer has root element {root.tag!r}")
    error = root.find(f"{OAI_NS}error")
    if error is not None:
        code = error.get("code")
        if code == "noRecordsMatch" and not has_token:
            return [], ""
        if code == "badResumptionToken":
            raise LookupError(f"badResumptionToken: {error.text}")
        raise Unexpected(f"OAI error {code}: {error.text}")
    listing = root.find(f"{OAI_NS}ListRecords")
    if listing is None:
        raise Unexpected("OAI-PMH answer has neither ListRecords nor error")
    records = listing.findall(f"{OAI_NS}record")
    if not records:
        raise Unexpected("ListRecords answer with no records; completion is unproven")
    out = []
    for record in records:
        header = record.find(f"{OAI_NS}header")
        if header is None:
            raise Unexpected("an OAI record carries no header")
        if header.get("status") == "deleted":
            continue
        metadata = record.find(f"{OAI_NS}metadata")
        if metadata is None or not len(metadata):
            raise Unexpected("a non-deleted OAI record carries no metadata")
        if len(metadata) != 1 or metadata[0].tag != f"{XOAI_NS}metadata":
            # another format (oai_dc after a repository change) has no bundles and no fields
            # this adapter reads: it would look like a page without full texts
            raise Unexpected(f"OAI record metadata is {metadata[0].tag!r}, not xoai")
        metadata[0].set("oai-identifier", header.findtext(f"{OAI_NS}identifier") or "")
        out.append(metadata[0])
    token_el = listing.find(f"{OAI_NS}resumptionToken")
    token = (token_el.text or "").strip() if token_el is not None else ""
    return out, token


def flatten(xoai: ET.Element) -> dict[str, list[str]]:
    """xoai metadata -> {"dc.title": [...], "dc.rights.uri": [...]}. The innermost element
    (the value's language, "none", "en", "de_CH" ...) is not part of the path; `bundles`,
    `others` and `repository` are not metadata."""
    fields: dict[str, list[str]] = {}

    def walk(element: ET.Element, path: tuple[str, ...]) -> None:
        values = [" ".join((f.text or "").split()) for f in element.findall(f"{XOAI_NS}field")
                  if f.get("name") == "value" and (f.text or "").strip()]
        if values:
            # the element holding values is the language level: its name is not a path part
            fields.setdefault(".".join(path[:-1]), []).extend(values)
        for child in element.findall(f"{XOAI_NS}element"):
            walk(child, (*path, child.get("name") or ""))

    for top in xoai.findall(f"{XOAI_NS}element"):
        name = top.get("name") or ""
        if name not in ("bundles", "others", "repository"):
            walk(top, (name,))
    return fields


def pdf_bitstreams(xoai: ET.Element) -> list[str]:
    """URLs of the PDF bitstreams in the ORIGINAL bundle, in order."""
    urls = []
    for bundles in xoai.findall(f"{XOAI_NS}element"):
        if bundles.get("name") != "bundles":
            continue
        for bundle in bundles.findall(f"{XOAI_NS}element"):
            name = next((f.text for f in bundle.findall(f"{XOAI_NS}field")
                         if f.get("name") == "name"), "")
            if (name or "").strip() != "ORIGINAL":
                continue
            for bitstream in bundle.iter(f"{XOAI_NS}element"):
                if bitstream.get("name") != "bitstream":
                    continue
                info = {f.get("name"): (f.text or "").strip()
                        for f in bitstream.findall(f"{XOAI_NS}field")}
                if info.get("format") == "application/pdf" and info.get("url"):
                    urls.append(info["url"])
    return urls


def _embargo_over(value: str, today: str) -> bool:
    """True when an embargo value is empty/'none' or names a date that has passed. A bare year
    counts as its last day; anything unreadable is treated as still embargoed."""
    text = value.strip().lower()
    if text in ("", "none", "no", "false", "0", "not_embargoed", "not embargoed"):
        return True
    if match := DATE.search(text):
        return match.group(0) <= today
    if re.fullmatch(r"\d{4}", text):
        return f"{text}-12-31" <= today
    return False


def restricted(fields: dict[str, list[str]], cfg: dict, today: str) -> str | None:
    """Why the record's full text is not open, or None. Fail closed: an embargo field with a
    future or unreadable date, an access term other than open in a rights / access field, or a
    missing / different value in the repository's open-access field."""
    for path, values in fields.items():
        lowered = path.lower()
        if path in (cfg.get("ignore_fields") or ()):
            continue    # reviewed per repository: a field that is not an access statement
        for value in values:
            if "embargo" in lowered and not _embargo_over(value, today):
                return f"embargo ({path}: {value[:40]})"
            if not RIGHTS_PATH.search(lowered):
                continue
            if (match := ACCESS_TERM.search(value)) and match.group(1).lower() != "open":
                return f"access {match.group(0)}"
            if (match := COAR_ACCESS.match(value.strip())) and match.group(1).lower() != "c_abf2":
                return f"access {match.group(0)}"
    access = cfg.get("open_access")
    if access:
        values = fields.get(access["field"]) or []
        if not values or not all(re.search(access["pattern"], v, re.I) for v in values):
            return f"{access['field']} is not open ({'; '.join(values)[:60] or 'missing'})"
    return None


_JURISDICTION = (r"(?: (?:international|unported|generic|universal|germany|deutschland|de|"
                 r"united states|us|usa|portugal|pt|spain|es|peru|pe|chile|cl|canada|ca|"
                 r"sweden|se|brazil|brasil|br|france|fr|italy|it|netherlands|nl|"
                 r"switzerland|ch|austria|at|england (?:and|&) wales|uk|scotland|"
                 r"australia|au|south africa|za))?")
_CC_SHORT = re.compile(r"(?:creative commons )?cc[ -]?(by(?:-(?:nc|nd|sa)){0,2}|0|zero)"
                       r"(?: \d\.\d" + _JURISDICTION + r")?(?: licen[cs]e)?")
_CC_LONG = re.compile(
    r"(?:creative commons )?(?:attribution|namensnennung)"
    r"((?:[ -]+(?:non[ -]?commercial|no[ -]?deriv(?:ative)?s?(?: works)?|share[ -]?alike))*)"
    r"(?: \d\.\d" + _JURISDICTION + r")?(?: licen[cs]e)?")
_CC_ZERO = re.compile(r"(?:creative commons )?(?:cc0|cc zero|public domain dedication)"
                      r"(?: 1\.0(?: universal)?)?")
_PD_MARK = re.compile(r"(?:creative commons )?public domain(?: mark)?(?: 1\.0)?")


def licence_name_kind(text: str) -> str | None:
    """The Creative Commons kind a rights text NAMES ("by", "by-nc-nd", "zero",
    "public-domain"), only when the WHOLE text is one recognised licence name ("CC BY 4.0
    International", "Attribution-NonCommercial-NoDerivs 3.0 United States"). Any other prose —
    a negation, "All rights reserved", a sentence that mentions a licence — is None."""
    text = " ".join(text.replace("\u2013", "-").replace("\u2010", "-").lower().split())
    text = text.rstrip(".")
    if match := _CC_SHORT.fullmatch(text):
        kind = match.group(1)
        return "zero" if kind in ("0", "zero") else kind
    if match := _CC_LONG.fullmatch(text):
        parts = match.group(1)
        kind = "by"
        if re.search(r"non[ -]?commercial", parts):
            kind += "-nc"
        if re.search(r"no[ -]?deriv", parts):
            kind += "-nd"
        if re.search(r"share[ -]?alike", parts):
            kind += "-sa"
        return kind if "-nd-sa" not in kind else None
    if _CC_ZERO.fullmatch(text):
        return "zero"
    if _PD_MARK.fullmatch(text):
        return "public-domain"
    return None


def licence_of(fields: dict[str, list[str]]) -> tuple[str, str | None, list[str]]:
    """(tag, licence URL, rights statements). A licence tag needs a canonical Creative Commons
    URL, every licence URL of the record naming the SAME licence, and every free-text rights
    statement being exactly a recognised name of that same licence (licence_name_kind). Open
    licences are read by scripts/licenses.py; one canonical NC / ND licence keeps its exact
    tag. Anything else — no URL, two different licences, negated, contradictory or unrecognised
    prose ("... not licensed under Creative Commons", "Attribution. All rights reserved"),
    rightsstatements.org, a publisher copyright line — is unverified. Access terms (openAccess
    ...) are not licence statements."""
    statements = []
    for path, values in fields.items():
        if LICENCE_PATH.match(path):
            statements.extend(v for v in values if not ACCESS_TERM.fullmatch(v.strip().rstrip("."))
                              and not COAR_ACCESS.match(v.strip())
                              and v.strip().upper() not in ("N/A", "NA", "-", "--"))
    statements = list(dict.fromkeys(statements))
    urls = [s for s in statements if re.match(r"https?://", s)]
    texts = [s for s in statements if s not in urls]
    if not urls:
        return "unverified", None, statements
    found: set[tuple[str, str]] = set()      # (tag, kind) per URL
    for url in urls:
        tag, _ = licenses.cc_license([url])
        if tag:
            kind = {"cc0": "zero", "public-domain": "public-domain"}.get(
                tag, tag.removeprefix("cc-"))
        elif match := RESTRICTED_CC.fullmatch(url):
            kind = match.group(1).lower()
            tag = f"cc-{kind}"
        else:
            return "unverified", None, statements
        found.add((tag, kind))
    if len(found) != 1:
        return "unverified", None, statements
    (tag, kind), = found
    if any(licence_name_kind(t) != kind for t in texts):
        return "unverified", None, statements
    return tag, urls[0], statements


def relevant(title: str, subjects: list[str], classes: list[str], cfg: dict,
             fields: dict[str, list[str]] | None = None, setspec: str = "",
             language: str = "") -> bool:
    """A record of a dedicated built-environment set, repository or department passes unless
    its title names a subject with no building reading (HARD_KILL; soils, microbes and vehicles
    are core topics of a civil engineering department, so the soft veto does not apply there).
    Any other record needs a built-environment Dewey class or an unambiguous AEC term and must
    pass the full off-domain veto."""
    if not title.strip() or bes_relevance.HARD_KILL.search(title):
        return False
    if cfg.get("relevance") == "all" or setspec in (cfg.get("all_sets") or ()):
        return True
    for path, pattern in (cfg.get("relevant_fields") or {}).items():
        if any(re.search(pattern, v, re.I) for v in (fields or {}).get(path, ())):
            return True
    if bes_relevance.vetoed(title):
        return False
    # "Building a compiler", "Building trust in ...": the verb is not a building
    title = GERUND.sub(" ", title)
    if any(DDC_BUILT.search(c) for c in classes):
        return True
    if bes_relevance.relevant(title, strict=True) or AEC_EN.search(title):
        return True
    text = " ; ".join([title, *subjects])
    if AEC_EN.search(text) and (bes_relevance.BUILT.search(text)
                                or bes_relevance.BUILT_WEAK.search(title)):
        return True  # an AEC subject term backed by a building word in the title or subjects
    for lang in cfg.get("languages") or ():
        # a record that states its language is read with that language's terms only
        # ("multi-core architectures" is not French architecture)
        if language and language != lang:
            continue
        pattern = AEC_LANG_RE.get(lang)
        if pattern is not None and pattern.search(title):
            return True
    return False


def topic_for(text: str, default: str) -> str:
    for pattern, topic in TOPIC_RULES:
        if pattern.search(text):
            return topic
    return default


def _first(fields: dict[str, list[str]], *paths: str) -> str:
    for path in paths:
        if fields.get(path):
            return fields[path][0]
    return ""


def candidate(xoai: ET.Element, key: str, cfg: dict, today: str,
              setspec: str = "") -> dict | None:
    fields = flatten(xoai)
    title = " ".join(html.unescape(_first(fields, "dc.title", "dcterms.title")).split())
    if not title:
        return None
    types = [v for path, values in fields.items()
             if re.match(r"(?:dc|dcterms)\.type(?:\.|$)|thesis\.degree\.(?:name|level)$", path)
             for v in values]
    skipped = re.compile(cfg["skip_types"], re.I) if cfg.get("skip_types") else NOT_TYPES
    if any(skipped.search(t) for t in types):
        return None
    if cfg.get("types") != "*":
        wanted = re.compile(cfg["types"], re.I) if cfg.get("types") else TYPES
        if not any(wanted.search(t) for t in types):
            return None
    pattern = re.compile(cfg["fulltext_pattern"])
    url = next((u for u in pdf_bitstreams(xoai) if pattern.fullmatch(u)
                and urlsplit(u).netloc in cfg["hosts"]), None)
    if url is None or restricted(fields, cfg, today):
        return None
    subjects = [v for path, values in fields.items() if SUBJECT_PATH.match(path)
                and not DDC_FIELD.search(path) for v in values]
    classes = [v for path, values in fields.items() if DDC_FIELD.search(path) for v in values]
    language = _first(fields, "dc.language.iso", "dc.language", "dcterms.language")
    language = language.strip().lower().replace("_", "-").split("-")[0]
    language = LANG3.get(language) or LANG_NAMES.get(language) or language
    if not re.fullmatch(r"[a-z]{2}", language):
        language = ""
    if not relevant(title, subjects, classes, cfg, fields, setspec, language):
        return None
    tag, licence_url, statements = licence_of(fields)
    entry = {
        "id": f"{PREFIX}{key[:14]}-{registry.slug(title)[:40]}", "title": title[:150],
        "url": url, "source": cfg["source"], "license": tag,
        "topic": topic_for(" ; ".join([title, *subjects]), cfg["topic"]), "format": "pdf",
        "license_evidence": (
            f"{cfg['name']} OAI record rights: {'; '.join(statements)[:220]}" if statements else
            f"{cfg['name']} OAI record states no rights; the full text is openly downloadable, "
            "no reuse licence is given"),
        "rights_verified_at": today,
    }
    if licence_url:
        entry["license_url"] = licence_url
    kind = " ".join(types).lower()
    entry["document_type"] = (
        "journal-article" if re.search(r"article", kind) else
        "thesis" if re.search(
            r"thes|dissert|doctoral|master|bachelor|licentiat|habilit|tesi|tese|tesis|mémoire|"
            r"thèse|arbeit|examensarbete|opinnäyte|diplomity|väitös|gradu|trabajo|treball|"
            r"trabalho|tcc|student|doctor|m\.?phil|ph\.?d", kind) or cfg.get("types") == "*" else
        "report" if re.search(r"report|bericht|rapport|raportti|working", kind) else "book")
    if language:
        entry["language"] = language
    issued = _first(fields, "dc.date.issued", "dcterms.issued", "dc.date")
    if re.match(r"\d{4}", issued):
        entry["published_at"] = issued[:10] if DATE.match(issued) else issued[:4]
    handle = next((v for v in fields.get("dc.identifier.uri") or []
                   if re.match(r"https?://hdl\.handle\.net/\S+$", v)), "")
    if handle:
        entry["persistent_id"] = "hdl:" + handle.split("hdl.handle.net/", 1)[1]
    for doi in [*(fields.get("dc.identifier.doi") or []), *(fields.get("dc.relation.doi") or [])]:
        if (pid := dedup.normalize_pid(doi)) and pid.startswith("doi:"):
            entry["persistent_id"] = pid
            break
    return entry


# ---------------------------------------------------------------------------------------------
# rotation
def parse_cursor(cursor: str) -> tuple[int, dict[str, dict]]:
    if cursor.strip() in ("", "START"):
        return 0, {}
    try:
        data = json.loads(cursor)
        position, states = data["r"], data["s"]
        if (not isinstance(position, int) or isinstance(position, bool) or position < 0
                or not isinstance(states, dict)):
            raise ValueError("r must be a non-negative integer and s an object")
        for key, state in states.items():
            if (not isinstance(state, dict) or not isinstance(state.get("i"), int)
                    or state["i"] < 0
                    or not all(isinstance(state.get(k, ""), str) for k in "tfdwp")
                    or not all(not state.get(k) or DATE.fullmatch(state[k]) for k in "fdwp")):
                raise ValueError(f"bad state for {key}")
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"cursor must be START or a find_repo JSON cursor: {exc}") from exc
    return position, states


def dump_cursor(position: int, states: dict[str, dict]) -> str:
    compact = {key: {k: v for k, v in state.items() if v or k == "i"}
               for key, state in sorted(states.items())}
    return json.dumps({"r": position, "s": compact}, separators=(",", ":"), sort_keys=True)


def due(state: dict, today: date) -> bool:
    """A repository mid-pass is always due; a finished one after WATCH_DAYS."""
    if not state.get("w") or state.get("d"):
        return True
    return today >= date.fromisoformat(state["w"]) + timedelta(days=WATCH_DAYS)


def walk(key: str, cfg: dict, state: dict, pages: int, limit: int, keys,
         today: str) -> tuple[dict, list[dict], dict]:
    """Read up to `pages` OAI pages of one repository. Returns (new state, entries, stats);
    raises Unexpected / LookupError / a network error with nothing changed: `state` is copied
    and `keys` is only READ here. The entries' URLs, titles and identities are reserved in sets
    local to this walk; main() merges them into `keys` when it accepts the repository, so a
    repository that fails on a later page never hides a record from the next repository."""
    state = dict(state)
    if not state.get("d"):       # a new pass starts
        previous = state.get("p", "")
        state.update(i=0, t="", d=today, f=previous)
    sets = cfg["sets"]
    out: list[dict] = []
    stats = {"pages": 0, "scanned": 0}
    restarted = False
    urls: set[str] = set()
    titles: set[str] = set()
    pids: set[str] = set()
    while state["i"] < len(sets) and stats["pages"] < pages and len(out) < limit:
        try:
            records, token = parse_page(
                fetch_page(cfg, sets[state["i"]], state.get("t", ""), state.get("f", "")),
                has_token=bool(state.get("t")))
        except LookupError:
            if restarted or not state.get("t"):
                raise
            restarted, state["t"] = True, ""
            stats["pages"] += 1
            continue
        stats["pages"] += 1
        stats["scanned"] += len(records)
        setspec = sets[state["i"]]
        entries = [e for e in (candidate(r, key, cfg, today, setspec) for r in records) if e]
        keys.prefetch(**dedup.page_keys(entries))
        for entry in entries:
            u, t = entry["url"].rstrip("/"), registry.norm(entry["title"])
            own = set(dedup.entry_pids(entry))
            if (u in urls or t in titles or own & pids
                    or u in keys.urls or t in keys.titles or keys.identity_known(entry)):
                continue
            urls.add(u)
            titles.add(t)
            pids |= own
            out.append(entry)
        if token:
            state["t"] = token
        else:
            state["i"], state["t"] = state["i"] + 1, ""
    if state["i"] >= len(sets):  # pass complete: watch from this pass's start date next time
        start = date.fromisoformat(state["d"]) - timedelta(days=1)
        state = {"i": 0, "t": "", "f": "", "d": "", "w": today, "p": start.isoformat()}
    return state, out, stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", default="START", help="rotation cursor (JSON) or START")
    ap.add_argument("--repos", type=int, default=2, help="repositories read per run")
    ap.add_argument("--pages", type=int, default=6, help="max OAI pages per repository and run")
    ap.add_argument("--max", type=int, default=60,
                    help="page-granular target for new entries per repository")
    ap.add_argument("--only", help="read only this repository key (manual probe)")
    ap.add_argument("--append", action="store_true", help="append into the registry")
    args = ap.parse_args()
    if min(args.repos, args.pages, args.max) < 1:
        ap.error("--repos, --pages and --max must be positive")
    try:
        position, states = parse_cursor(args.cursor)
    except ValueError as exc:
        ap.error(str(exc))
    repos = {k: c for k, c in load_config().items() if c.get("enabled")}
    if args.only:
        if args.only not in repos:
            ap.error(f"{args.only} is not an enabled repository")
    report = Report()
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    order = sorted(repos)
    if not order:
        print("# 0 NEW repository records (no enabled repository)")
        report.next(dump_cursor(position, states))
        return

    try:
        install_policy(load_policy(), repos)
    except Exception as exc:   # no policy, no request
        report.hold(f"host policy could not be established: {exc}"[:300])
        print("# 0 NEW repository records (host policy unavailable)")
        return
    keys = dedup.open_keys()
    out: list[dict] = []
    held: list[str] = []
    read = 0
    for offset in range(len(order)):
        if read >= args.repos:
            break
        key = args.only or order[(position + offset) % len(order)]
        state = states.get(key) or {"i": 0, "t": "", "f": "", "d": "", "w": ""}
        if not due(state, now.date()):
            if args.only:
                break
            continue
        read += 1
        try:
            new_state, entries, stats = walk(key, repos[key], state, args.pages, args.max, keys,
                                             today)
        except (Unexpected, LookupError) as exc:
            held.append(f"{key}: {exc}")
            print(f"# {key}: {exc}; state kept, nothing proposed for it", file=sys.stderr)
        except requests.RequestException as exc:
            held.append(f"{key}: {type(exc).__name__}")
            print(f"# {key}: request failed ({exc}); state kept, nothing proposed for it",
                  file=sys.stderr)
        else:
            states[key] = new_state
            for entry in entries:     # accepted: only now do its keys count for later walks
                keys.urls.add(entry["url"].rstrip("/"))
                keys.titles.add(registry.norm(entry["title"]))
                keys.add_identity(entry)
            out.extend(entries)
            print(f"# {key}: {len(entries)} new / {stats['scanned']} records over "
                  f"{stats['pages']} OAI pages")
        if args.only:
            break
    position = (position + max(read, 1)) % len(order)
    states = {k: v for k, v in states.items() if k in repos}  # a removed repository's state goes

    keys.uniquify_ids(out)
    by_source: dict[str, int] = {}
    by_licence: dict[str, int] = {}
    for entry in out:
        by_source[entry["source"]] = by_source.get(entry["source"], 0) + 1
        by_licence[entry["license"]] = by_licence.get(entry["license"], 0) + 1
    print(f"# {len(out)} NEW repository records (deduped vs manifest + registry + blocklist)")
    print(f"# by source: {by_source}; by licence: {by_licence}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))
    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    cursor = dump_cursor(position, states)
    if len(cursor) > MAX_CURSOR:
        # cannot happen with tens of repositories; never write a cursor the runner would refuse
        report.hold(f"cursor would be {len(cursor)} characters")
        return
    if held:
        # a failing repository keeps its own state; the round-robin still moves on, so one
        # broken host can never starve the others
        print(f"# kept state for {len(held)} repositories: {'; '.join(held)[:300]}")
    report.next(cursor)


if __name__ == "__main__":
    main()
