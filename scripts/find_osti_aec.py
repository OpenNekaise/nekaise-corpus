#!/usr/bin/env python3
"""find_osti_aec.py — the general built-environment (AEC) query family over OSTI.

OSTI (osti.gov) is the US DOE's report, conference-paper, thesis and accepted-manuscript
repository. scripts/find_osti_sim.py walks its simulation queries; this backend walks the rest
of the built environment with the same machinery (`find_osti_sim.run(Family)`: records API at
one request per second, query-major history walk, then a daily watch pass over newly entered
records, so it never reports EXHAUSTED). It supersedes the old page-rotating find_osti.py,
which labelled every record public-domain and had no title gate.

Queries (append-only, a registry topic each) cover building energy use and efficiency,
envelopes, HVAC and equipment, lighting, controls, commissioning and FDD, codes and standards,
weatherization, district energy, and the civil side: concrete and cement, structures and seismic
design, geotechnics, bridges, pavements, construction, water infrastructure, fire, urban systems.

Precision. OSTI searches full text and most of its holdings are nuclear, physics, chemistry and
bio science, so a record is kept only when its TITLE passes a gate (never the query alone):
  * the building-science gate scripts/bes_relevance.py (strict), or
  * the civil gate below: an unambiguous civil / structural / construction-materials concept
    (CIVIL), or an ambiguous one (CIVIL_WEAK: "concrete", "seismic", "bridge", "foundation" ...)
    together with an engineering context word (CIVIL_CONTEXT),
and never when the title names an off-domain subject (bes_relevance veto, plus OFF_DOMAIN:
reactor physics, radioactive waste forms, weapons, particle accelerators, geophysics exploration
...). Ambiguous subjects (OFF_DOMAIN_SOFT: detectors, oil, lasers, turbines, vehicles ...) veto
only a title without an unambiguous building anchor.
Structural and seismic engineering of energy facilities (containment concrete, plant seismic
design) is civil engineering and passes; reactor physics does not.
Calibrated 2026-10-12 on live pages of every query (numbers in the backend's `reason`).

Licence classification and identity are find_osti_sim's, unchanged: a canonical CC /
public-domain URL in the record's `rights` field, DOE PAGES accepted manuscripts `publisher-oa`,
everything else `unverified` (OSTI public access is not public domain); DOI persistent_id.

    python scripts/find_osti_aec.py --cursor "q=0 p=1" --pages 2 --max 50
"""
from __future__ import annotations

import re

import bes_relevance
import find_osti_sim

UA = {"User-Agent": "nekaise-corpus/find_osti_aec (research corpus; robots.txt honoured)"}
SOURCE = "osti_aec"
PREFIX = "osa-"

# Append-only: the cursor indexes this tuple. (OSTI query, registry topic).
QUERIES: tuple[tuple[str, str], ...] = (
    ('"building energy efficiency"', "building_energy"),
    ('"commercial buildings"', "building_energy"),
    ('"residential buildings"', "building_energy"),
    ('"building envelope"', "building_energy"),
    ('"zero energy" AND (building OR home)', "building_energy"),
    ('"weatherization assistance" OR "home weatherization" OR "weatherization program" OR '
     '"low-income weatherization"', "building_energy"),
    ('"energy retrofit" OR "building retrofit"', "building_energy"),
    ('"indoor air quality"', "building_energy"),
    ('"thermal comfort"', "building_energy"),
    ('daylighting', "architecture"),
    ('"solid-state lighting" OR "lighting controls" OR "lighting retrofit" OR luminaire OR '
     '"commercial lighting" OR "residential lighting"', "building_energy"),
    ('(glazing OR fenestration OR electrochromic OR "low-e" OR "window retrofit") AND '
     '(building OR residential)', "building_energy"),
    ('"cool roof" OR "roof insulation" OR "attic insulation"', "building_energy"),
    ('"thermal insulation" AND (wall OR envelope OR attic OR "building")', "materials"),
    ('moisture AND ("building envelope" OR "wall assembly" OR crawlspace OR basement)',
     "building_energy"),
    ('ventilation AND (residential OR building OR indoor)', "equipment_systems"),
    ('"heat pump"', "equipment_systems"),
    ('"air conditioning" OR "air conditioner"', "equipment_systems"),
    ('HVAC', "equipment_systems"),
    ('"water heater" OR "water heating"', "equipment_systems"),
    ('(chiller OR boiler OR furnace) AND ("commercial building" OR residential OR HVAC)',
     "equipment_systems"),
    ('(refrigerant OR refrigeration) AND (building OR supermarket OR "air conditioning")',
     "equipment_systems"),
    ('"thermal energy storage" AND (building OR cooling OR heating)', "equipment_systems"),
    ('"ground source heat pump" OR "geothermal heat pump" OR "ground-coupled"',
     "equipment_systems"),
    ('"district heating" OR "district cooling" OR "district energy"', "infrastructure"),
    ('"combined heat and power" AND (building OR campus OR district)', "equipment_systems"),
    ('"building controls" OR "building automation" OR "energy management system"',
     "controls_bas"),
    ('"demand response" AND (building OR HVAC OR thermostat)', "controls_bas"),
    ('"grid-interactive efficient buildings" OR "building-to-grid"', "controls_bas"),
    ('"fault detection" AND (building OR HVAC OR chiller OR "air handling")',
     "commissioning_fdd"),
    ('"building commissioning" OR retrocommissioning OR "retro-commissioning" OR '
     '"continuous commissioning" OR "monitoring-based commissioning"', "commissioning_fdd"),
    ('"data center" AND (cooling OR energy)', "equipment_systems"),
    ('"building energy codes" OR "energy code" OR "building code"', "standards_protocols"),
    ('ASHRAE', "standards_protocols"),
    ('"measurement and verification" OR "energy savings performance"', "standards_protocols"),
    ('"appliance standards" OR "equipment standards" OR "energy conservation standards"',
     "standards_protocols"),
    ('"energy audit" AND building', "building_energy"),
    ('"building-integrated photovoltaics" OR "solar water heating" OR "passive solar"',
     "building_energy"),
    ('"manufactured housing" OR multifamily OR "affordable housing"', "building_energy"),
    ('"building stock" OR "energy use intensity" OR "building energy consumption"',
     "building_energy"),
    ('radon AND (house OR home OR building OR indoor)', "building_energy"),
    ('concrete AND (durability OR strength OR reinforced OR cracking)', "materials"),
    ('cement AND (clinker OR hydration OR "low-carbon" OR "supplementary cementitious")',
     "materials"),
    ('"mass timber" OR "cross-laminated timber" OR "wood frame" OR masonry', "materials"),
    ('("embodied carbon" OR "life cycle assessment") AND (building OR concrete OR construction)',
     "materials"),
    ('"structural engineering" OR "structural design" OR "structural analysis"',
     "structures_civil"),
    ('"seismic design" OR "earthquake engineering" OR "seismic retrofit"', "structures_civil"),
    ('"steel structure" OR "steel frame" OR "structural steel"', "structures_civil"),
    ('"soil-structure interaction" OR "geotechnical engineering" OR "foundation design"',
     "structures_civil"),
    ('("wind load" OR "wind engineering") AND (building OR structure)', "structures_civil"),
    ('bridge AND (deck OR girder OR inspection OR highway)', "infrastructure"),
    ('pavement OR asphalt', "infrastructure"),
    ('"tunnel construction" OR "tunnel lining" OR tunneling OR "tunnel boring"',
     "infrastructure"),
    ('dam AND (safety OR embankment OR spillway OR concrete)', "infrastructure"),
    ('"water treatment plant" OR "wastewater treatment" OR "water distribution" OR stormwater',
     "infrastructure"),
    ('"infrastructure resilience" OR "transportation infrastructure" OR "civil infrastructure"',
     "infrastructure"),
    ('"construction industry" OR "construction management" OR "modular construction" OR '
     '"construction cost"', "construction"),
    ('"additive manufacturing" AND (construction OR concrete OR building)', "construction"),
    ('("fire safety" OR "fire protection" OR "fire resistance") AND (building OR structure)',
     "architecture"),
    ('"urban heat island" OR "urban planning" OR "smart city" OR "urban form"', "urban"),
)

# Unambiguous civil / structural / construction-materials concepts: enough on their own.
CIVIL = re.compile(
    r"reinforced concrete|prestressed|precast|\bconcretes?\b(?= (?:structures?|beams?|columns?|"
    r"slabs?|walls?|pavements?|bridges?|dams?|durability|mix|mixtures?|construction|"
    r"buildings?|floors?|foundations?|members?|shear|frames?|decks?))|"
    r"portland cement|cementitious materials?|supplementary cementitious|fly ash concrete|"
    r"\brebars?\b|reinforcing (?:bars?|steel)|\bmasonry\b|mass timber|cross[- ]laminated timber|"
    r"\bclt\b|wood[- ]frame|timber (?:frame|structures?|construction|buildings?)|"
    r"structural (?:engineering|design|steel|health monitoring|retrofit|response|members?|"
    r"walls?|systems?|performance|integrity of (?:buildings?|bridges?))|"
    r"steel (?:frames?|structures?|moment|braced|girders?|bridges?|buildings?|connections?)|"
    r"shear walls?|moment[- ](?:resisting )?frames?|base isolation|seismic (?:design|retrofit|"
    r"isolation|performance|response|fragility|qualification|rehabilitation|evaluation of)|"
    r"earthquake (?:engineering|resistan|response of)|soil[- ]structure interaction|"
    r"geotechnical|\bfoundations? (?:design|engineering|piles?|settlement)|\bpiles?\b(?= (?:"
    r"foundation|driving|capacity|load))|retaining walls?|embankments?|slope stability|"
    r"\bbridges? (?:decks?|girders?|inspection|design|piers?|construction|engineering|"
    r"rehabilitation|structures?)|highway bridges?|\bpavements?\b|\basphalt|"
    r"\btunnel(?:s|ing|ling)? (?:construction|lining|design|boring|excavation)|"
    r"\bdams? (?:safety|design|construction|failure|rehabilitation)|\bspillways?\b|"
    r"wind (?:loads?|loading|engineering)|"
    r"construction (?:industry|management|cost|costs|methods?|practices?|materials?|"
    r"productivity|safety|site|sector|technology|techniques?|workers?|projects?)|"
    r"modular construction|prefabricat|building construction|civil (?:engineering|"
    r"infrastructure|structures?)|"
    r"fire (?:safety|protection|resistance|suppression|sprinklers?|codes?)|"
    r"wastewater treatment|water treatment plants?|drinking water|water distribution|"
    r"\bstormwater|\bsewers?\b|sewage|water (?:supply|utilit|infrastructure)|"
    r"wastewater (?:industry|utilit\w+|facilit\w+|plants?|infrastructure|systems?)|"
    r"\bcofferdams?\b|wall assembl(?:y|ies)|"
    r"transportation infrastructure|\bhighways?\b|\brailways?\b|\btransit\b|"
    r"urban (?:planning|form|design|development|infrastructure|areas?|systems?)|"
    r"smart cit(?:y|ies)|land use|embodied (?:carbon|energy)",
    re.I,
)
# Words with a civil reading that OSTI also files under physics, chemistry and nuclear science
# ("concrete example", "bridge functions", "foundations of quantum ..."): they need an
# engineering context word in the same title.
CIVIL_WEAK = re.compile(
    r"\bconcretes?\b|\bcements?\b|\bmortars?\b|\bgrouts?\b|\bseismic\b|"
    r"\bearthquakes?\b|\bbridges?\b|\bfoundations?\b|\btunnels?\b|\bdams?\b|"
    r"\bbeams?\b|\bcolumns?\b|\bslabs?\b|\bgirders?\b|\btimber\b|\bbricks?\b|"
    r"\bgypsum\b|\bwastewater\b|\bcofferdams?\b",
    re.I,
)
CIVIL_CONTEXT = re.compile(
    r"\bbuildings?\b|structural|\bcivil\b|\bdesign(?:s|ed|ing)?\b|durab|strength|crack|"
    r"corros|reinforc|load[- ]bearing|\bloads?\b|retrofit|rehabilitat|\bconstruct|"
    r"\bengineer|infrastructure|\bhighways?\b|\broads?\b|pavement|\bcodes?\b|standards?|"
    r"specification|hazard|resilien|safety|inspection|maintenance|repair|service life|"
    r"compressive|\bshear\b|\bwalls?\b|\bfloors?\b|\broofs?\b|\bhousing\b|residential|"
    r"commercial|energy[- ]efficien|life[- ]cycle|embodied|sustainab|municipal|planning|"
    r"\bindustry\b|\butilit(?:y|ies)\b|hydropower",
    re.I,
)
# Generic words ("structural phase transition", "construction of an estimator", "computing
# infrastructure", "fire" in a combustion title, "urban" air chemistry): a context word is not
# enough ("design and construction of the linac"). They pass only beside a second civil or
# building word: a CIVIL_WEAK word or one of COMPANION.
CIVIL_GENERIC = re.compile(
    r"\bstructural\b|\bstructures?\b|\bconstruction\b|\binfrastructure\b|\bfire\b|"
    r"\burban\b|\bcit(?:y|ies)\b|\baggregates?\b",
    re.I,
)
COMPANION = re.compile(
    r"\bbuildings?\b|\bcivil\b|\bsteel\b|\bhighways?\b|\broads?\b|\bhousing\b|"
    r"\bhouses?\b|\bhomes?\b|residential|multifamily|\bdwellings?\b|\bmunicipal|"
    r"\bwalls?\b|\bfloors?\b|\broofs?\b|\bsoils?\b|\btransportation\b|"
    r"\bresilien|\bretrofit|\bdemolition\b|\bcontractors?\b|"
    r"\bplanning\b|\bneighbou?rhoods?\b|passive solar|energy[- ]efficien|\bcodes?\b",
    re.I,
)
# "Building a bright future", "Building trust in ...": the gerund is not a building.
GERUND = re.compile(
    r"\bbuilding (?:a|an|the|on|upon|up|out|new|better|trust|capacity|blocks?|bridges|"
    r"toward|towards|momentum|resilien\w+|communit\w+|partnerships?|consensus|confidence|"
    r"knowledge|tomorrow|security into|ai)\b",
    re.I,
)
# Subjects that turn a civil word into something else in a DOE repository. A CIVIL hit does not
# rescue these: "concrete durability in tank waste grout" stays out. Every stem is anchored so
# that it cannot match inside an ordinary word ("fusion" in "diffusion").
OFF_DOMAIN = re.compile(
    r"\breactor (?:core|physics|coolant|vessel internals|fuel|kinetics)|\bfuel (?:rods?|pins?|"
    r"assembl|cycle|cladding|pellets?)|\bneutron|\bneutrino|\bfission\b|\bfusion\b|"
    r"\bgaseous diffusion\b|\buf\W*(?:sub )?6\b|"
    r"\btokamak|\bplasma|"
    r"\bradionuclide|\bradioactiv|\bwaste forms?\b|\btank waste|\bsaltstone|\bvitrif|"
    r"\bplutonium|\buranium|\btritium|\bactinide|\bisotop|\bspent (?:nuclear )?fuel|"
    r"\bnuclear (?:weapons?|explosi|test|material|fuel|waste)|\bweapons?\b|\bwarheads?\b|"
    r"\bmunitions?\b|"
    r"\bexplosi(?:ve|on)s? (?:test|yield|monitoring)|\bunderground (?:nuclear|test)|"
    r"\b(?:particle|linear|ion|electron|proton|heavy[- ]ion) accelerators?\b|"
    r"\baccelerator (?:facilit|physics|laborator|beam|complex|driven)|\bcolliders?\b|"
    r"\blinac\b|\bsynchrotron|\bbeamlines?\b|"
    r"\b(?:particle|radiation|neutron|gamma|silicon|pixel|germanium|scintillat\w+) detectors?\b|"
    r"\bseismic (?:survey|imaging|monitoring of (?:nuclear|explosion)|reflection|tomograph|"
    r"inversion|waveform|source|discrimination|attenuation|anisotropy|velocity|data "
    r"processing|exploration)|\breservoirs? (?:characteri|simulat|rock)|\bhydraulic fractur|"
    r"\bwellbores?\b|\bwell cements?\b|\bshale\b|\bpetroleum\b|"
    r"\boil (?:exploration|recovery|reservoirs?|fields?|wells?|sands?|shale|production|"
    r"refin\w+|spills?)|\bcrude oil\b|\bnatural gas (?:production|reservoir)|"
    r"\bgeologic (?:repository|storage|sequestration|disposal)|"
    r"\byucca mountain|\bwipp\b|\bco2 (?:storage|sequestration|injection)|"
    r"\benhanced geothermal|\bgeothermal (?:reservoir|well|field|exploration|resource)|"
    r"\bcoal(?:[- ]fired| gasif| combust| mining| liquefaction| utilization| conversion| "
    r"plants?| preparation| mine)|\bgasif|\bcombustors?\b|\bgas turbines?\b|"
    r"\bfluid(?:ized)?[- ]bed|\butility boilers?\b|"
    r"\bsafety analysis report|\bsodium[- ]concrete|\bmolten\b|\blight water reactor|"
    r"\b[bp]wr\b|\bhtgr\b|\blmfbr\b|\bfermilab\b|\bcern\b|\bslac\b|"
    r"\blaboratory directed research|\bldrd\b|\baccident sequences?\b|\bipeee\b|"
    r"\bprobabilistic (?:risk|safety) assessment|\breactor vessels?\b|\bliquid[- ]metal|"
    r"\bfluid[- ]structure interaction|\bautomotive\b|\bbumpers?\b|"
    r"\bcorrective action|\bdecontaminat|\bdecommission|\bdeactivation\b|\bclosure report|"
    r"\benvironmental (?:impact statement|surveillance|restoration|monitoring report)|"
    r"\bsite environmental report|"
    r"\bquantum|\bmolecul|\batom(?:s|ic)?\b|\bnano|\bcrystal|\balloys?\b(?! (?:steel|"
    r"structur))|\bsuperconduct|\bphotovoltaic (?:cell|material)|\bsolar cells?\b|"
    r"\bbiofuel|\bethanol\b|\b(?:micro)?algae\b|\bspecies\b|\bfish(?:es|ery|eries)?\b|"
    r"\bsalmon\b|\bwildlife\b|"
    r"\bforests?\b|\bcrops?\b|\bagricultur|\bgenes?\b|\bcancer|\bclinical\b|"
    r"\bdose (?:reconstruction|assessment|rate)|\bdosimetr|"
    r"\bsoftware (?:quality|architecture)|\bmachine learning (?:for|of) (?:material|"
    r"molecul)|\bsupercomput|\bexascale\b|\bdata structures?\b|\bcomputing infrastructure|"
    r"\bhydrogen infrastructure|\baircraft|\bspacecraft|\blunar\b|\bmars\b",
    re.I,
)
# Subjects that are usually off-domain here but have ordinary building readings ("smoke
# detectors in residential buildings", "oil-fired residential furnaces", "laser scanning of
# buildings", "cybersecurity of building automation", "EV charging infrastructure in
# multifamily housing"): vetoed only when the title carries no unambiguous building anchor
# (bes_relevance.BUILT) — the rule bes_relevance uses for its own soft vetoes.
OFF_DOMAIN_SOFT = re.compile(
    r"\bdetectors?\b|\blasers?\b|\boil\b|\baccelerators?\b|\bdrilling\b|\bwells\b|"
    r"\bleach(?:ing|ed|ate|ability)?\b|\brepositor(?:y|ies)\b|"
    r"\bcarbon (?:capture|sequestration|storage)|\bturbines?\b|\belectric utilit|"
    r"\bbiomass\b|\bpatients?\b|\bcyber|\bcharging infrastructure|\bvehicles?\b|"
    r"\bsatellites?\b",
    re.I,
)


def off_domain(title: str) -> bool:
    """An unconditional veto, or a contextual one without a building anchor in the title."""
    return bool(OFF_DOMAIN.search(title)) or (
        bool(OFF_DOMAIN_SOFT.search(title)) and not bes_relevance.BUILT.search(title))


# A laboratory's name is not a subject ("... Idaho National Engineering Laboratory").
LAB_NAME = re.compile(r"national engineering (?:and environmental )?laborator(?:y|ies)", re.I)


def civil_relevant(title: str) -> bool:
    if CIVIL.search(title):
        return True
    weak = CIVIL_WEAK.search(title)
    if weak and CIVIL_CONTEXT.search(title):
        return True
    return bool(CIVIL_GENERIC.search(title)) and bool(weak or COMPANION.search(title))


def relevant(title: str, _q: int = 0) -> bool:
    """Title gate of the family: building science (strict) or civil engineering, no veto."""
    title = LAB_NAME.sub(" ", GERUND.sub(" ", title or ""))
    if not title.strip() or off_domain(title):
        return False
    if bes_relevance.vetoed(_without_weak_soil(title)):
        return False
    return bes_relevance.relevant(title, strict=True) or civil_relevant(title)


def _without_weak_soil(title: str) -> str:
    """bes_relevance vetoes a bare "soil" title (a lab-repository soil is biology or
    contamination); in a title with a CIVIL concept it is geotechnics, so the word is hidden
    from that veto only then."""
    return re.sub(r"\bsoils?\b", " ", title, flags=re.I) if CIVIL.search(title) else title


def topic(title: str, q: int) -> str:
    return QUERIES[q][1]


def family() -> find_osti_sim.Family:
    return find_osti_sim.Family("AEC", tuple(query for query, _topic in QUERIES), SOURCE, PREFIX,
                                relevant, topic, UA)


def main() -> None:
    find_osti_sim.run(family())


if __name__ == "__main__":
    main()
