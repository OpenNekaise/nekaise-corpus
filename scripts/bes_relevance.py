#!/usr/bin/env python3
"""bes_relevance.py — precision-first building-science gate shared by lab-repository finders.

National-lab and university repositories (LBNL eScholarship, NLR/NREL Pure) mix building energy
research with genomics, particle physics, batteries, fuels, grid-scale power systems and more. The
finders that mine them need a *precision-first* decision before spending a download slot: keep a
record only when its title (or author keywords / Fields-of-Research subjects) names an unambiguous
built-environment concept, and drop anything whose title names an obviously off-domain one. The
downstream quality gate (scripts/quality.py DOMAIN) remains the second safeguard.

Calibrated 2026-09-24 on CC-BY PDF items of eScholarship units lbnl_et_btus (loose: 139/166
kept; the drops were power-sector, fuels, vehicle, bio and physics papers), cedr_cbe (loose:
44/45) and a recent lbnl_rw sample (strict: 22/226, all building-science except one airport
paper), plus 300 NLR 2024 records (14 kept).
"""
from __future__ import annotations

import re

# Unambiguous building / built-environment concepts: enough on their own.
BUILT = re.compile(
    r"\bbuildings?\b|\bhvac\b|heat[ -]?pumps?\b|air[ -]?condition|\bchillers?\b|\bboilers?\b|"
    r"\bindoor|\bdaylight|\blighting\b|luminaire|glazing|fenestration|"
    r"\bfa[cç]ades?\b|building envelopes?|\bdwellings?\b|\bresidential\b|"
    r"\bcommercial (?:sector|office|space|build)|thermal comfort|thermal sensation|occupant|"
    r"plug[ -]?loads?|miscellaneous electric loads?|water heat|space (?:heat|cool)|"
    r"(?:heating|cooling) loads?|energy ?plus|\bmodelica\b|\bretrofit|"
    r"zero[ -]?(?:net[ -]?)?energy|\bzne\b|\bnzeb?s?\b|grid[ -]?interactive|\bgebs?\b|"
    r"cool (?:roofs?|walls?|pavements?|surfaces?)|\burban heat|urban (?:energy|building)|"
    r"district (?:heat|cool|energy)|\bthermostats?\b|refrigerants?|energy codes?|"
    r"building codes?|\bashrae\b|energy (?:audit|benchmark)|"
    r"electrochromic|solar gains?|radiant (?:cool|heat|slab|ceil|floor|panel)|underfloor air|"
    r"airtight|range hoods?|cooktops?|\bresstock\b|\bcomstock\b|\burbanopt\b|\bbeopt\b|"
    r"\bopenstudio\b|\bweatheriz|\bsolar decathlon\b|light redirection|"
    r"built environment|\bventilation\b|natural(?:ly)? ventilat|mechanical ventilat",
    re.I,
)

# Words with a building sense that are ambiguous in a general-science repository ("time
# windows", "construction of an estimator", "concrete example"). In a dedicated building unit
# they pass alone; in strict mode they need an energy/thermal/comfort context in the same text.
BUILT_WEAK = re.compile(
    r"\bwindows?\b|\benvelopes?\b|insulation|\broofs?\b|\bhouses?\b|\bhousing\b|\bhomes?\b|"
    r"\bhouseholds?\b|\boffices?\b|occupan|\bappliances?\b|demand (?:response|flexibility)|"
    r"\bshading\b|\bducts?\b|infiltration|\bconstruction\b|\bconcrete\b|\bcement|"
    r"\btimber\b|\bmasonry\b|\barchitectur|\bdata ?cent(?:er|re)s?\b",
    re.I,
)
ENERGY_CONTEXT = re.compile(
    r"energy|thermal|\bheat|cool|efficien|comfort|electric|\bair\b|decarboni|emission|"
    r"retrofit|\bload|carbon|climate|weather|seismic|structur",
    re.I,
)

# Titles naming a subject with no building reading never pass, whatever else they mention.
HARD_KILL = re.compile(
    r"\bgenom|\bprotein|tumou?r|cancer|neutrino|\bquarks?\b|hadron|\bbosons?\b|lepton|galax|"
    r"cosmolog|supernova|\bmice\b|\bmouse\b|enzym|photosynth|crystal structure|"
    r"x-ray diffraction|synchrotron|beamline|\bstomata|\bvaccin|antibod|\bhiv\b|\bhsv\b|"
    r"metabolic engineering|\bcations?\b|semiconductor|perovskite|\bwafers?\b",
    re.I,
)
# Subjects that are usually off-domain in a lab repository but have real building readings
# ("battery storage for grid-interactive buildings", "airborne virus transmission indoors",
# "soil thermal conductivity for ground-source heat pumps", "microbial growth on building
# materials", "EV charging in residential buildings"): vetoed only when the TITLE carries no
# unambiguous BUILT anchor.
SOFT_KILL = re.compile(
    r"\bcells?\b(?! phones?)|\bplasmas?\b|microb|bacteri|\bcatalys|electrolyte|"
    r"\bbatter(?:y|ies)\b|\bsoils?\b|nanopartic|\bviral\b|\bvirus|"
    r"transportation (?:secure|sector|network|electrification)|\bvehicles?\b|\bfleets?\b",
    re.I,
)


def vetoed(title: str) -> bool:
    """Off-domain title: a HARD_KILL term, or a SOFT_KILL term without a building anchor."""
    return bool(HARD_KILL.search(title)) or (
        bool(SOFT_KILL.search(title)) and not BUILT.search(title)
    )


# ANZSRC Fields-of-Research labels that eScholarship carries in `keywords`/`subjects`, e.g.
# "3302 Building (for-2020)":
# 1201/3301 Architecture and 1202/3302 Building. The bare division codes (12, 33) and urban /
# transport planning (1205, 3304) also tag vehicle, grid and airport papers, so they do not count.
FOR_BUILT = re.compile(r"^(?:1201|1202|3301|3302)\s", re.I)

TOPIC_RULES = (
    (re.compile(r"commissioning|fault[ -]?detect|\bfdd\b|diagnos", re.I), "commissioning_fdd"),
    (re.compile(r"\bcodes?\b|standards?\b|\bashrae\b|protocol|test methods?", re.I),
     "standards_protocols"),
    (re.compile(r"control|automation|demand (?:response|flexibility)|grid[ -]?interactive|"
                r"flexib|thermostat|sensors?\b|occupancy|\bgebs?\b", re.I), "controls_bas"),
    (re.compile(r"\bhvac\b|heat[ -]?pump|chiller|boiler|air[ -]?condition|water heat|"
                r"refrigerant|ventilat|\bducts?\b|radiant|underfloor|range hood|cooktop", re.I),
     "equipment_systems"),
    (re.compile(r"\bconcrete\b|\bcement|\btimber\b|\bmasonry\b|phase[ -]change material|"
                r"insulation material", re.I), "materials"),
    (re.compile(r"\bconstruction\b|modular|prefab", re.I), "construction"),
    (re.compile(r"\burban\b|\bcity\b|\bcities\b|district|neighbou?rhood|community", re.I), "urban"),
    (re.compile(r"architect|fa[cç]ade|daylight", re.I), "architecture"),
)


def relevant(title: str, keywords: list[str] | None = None,
             subjects: list[str] | None = None, strict: bool = True) -> bool:
    """Precision-first building-science decision for one repository record.

    A Fields-of-Research architecture/building subject or an unambiguous BUILT concept passes. A
    BUILT_WEAK word passes alone only when ``strict`` is false (dedicated building units, which
    also consult keywords); in strict mode it needs an energy/thermal context in the title. A
    vetoed title (see ``vetoed``) never passes.
    """
    title = title or ""
    if not title.strip() or vetoed(title):
        return False
    if any(FOR_BUILT.match(s or "") for s in [*(subjects or ()), *(keywords or ())]):
        return True
    # Repository keywords mix author terms with subject-scheme labels ("Built Environment and
    # Design", "Buildings") that tag grid, vehicle and airport papers, so strict mode reads the
    # title alone.
    text = title if strict else f"{title} ; {' ; '.join(keywords or ())}"
    if BUILT.search(text):
        return True
    if not BUILT_WEAK.search(text):
        return False
    return not strict or bool(ENERGY_CONTEXT.search(text))


def topic_for(title: str) -> str:
    """Coverage-radar topic for an accepted title (first rule wins, default building_energy)."""
    for pattern, topic in TOPIC_RULES:
        if pattern.search(title or ""):
            return topic
    return "building_energy"
