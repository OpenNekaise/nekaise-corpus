#!/usr/bin/env python3
"""diva_aec.py — precision-first AEC / built-environment gate for DiVA (find_diva --family aec).

DiVA holds student theses, doctoral and licentiate theses and reports of every subject (nursing,
pedagogy, law, computer science ...). This gate decides from a record's titles (with subtitles),
author keywords and its research-subject classification (the Swedish standard "HSV" codes that
DiVA records carry: 201xx is Civil Engineering) whether the document is about the built
environment: buildings and building energy / HVAC / indoor climate, architecture, construction
management, structural engineering, geotechnics, building materials, roads / railways /
pavements, water and wastewater infrastructure, fire safety, urban planning, real estate and
facility management. Swedish and English first, with the Norwegian and Danish forms that differ.

A record passes when no title names a subject without a building reading
(bes_relevance.HARD_KILL) and
  * a title names an unambiguous concept (STRONG_SV / STRONG_EN); when that title also names an
    off-domain context (OFF_CONTEXT: patients, software, vehicles, teaching ...) it needs a
    second signal (a strong author keyword, a Civil Engineering code); or
  * the title is not off-domain and the record is classified under a built-environment research
    subject (Architectural Engineering, Construction Management, Building Technologies,
    Infrastructure Engineering, Geotechnical Engineering, Architecture, Landscape Architecture);
    or
  * the title is not off-domain and two author keywords are strong, or one is and the record
    carries a Civil Engineering group code (201xx, except 20105 Transport Systems and Logistics).
The group code alone is not enough: at some universities it also tags real-estate finance,
logistics, hydrology and geodesy theses. The downstream quality gate (scripts/quality.py DOMAIN)
stays the second safeguard.

Calibrated 2026-10-12 on 3,361 live free-full-text records (sets studentThesis, Technology,
doctoralThesis, report; datestamps of 2012, 2015 and 2024): 322 kept (student theses of every
subject 4-5%, Technology theses 15-24%, reports 3%, doctoral theses under 1%).
tests/test_find_diva_aec.py pins examples of both directions.
"""
from __future__ import annotations

import re

import bes_relevance

# --- unambiguous built-environment concepts ------------------------------------------------
STRONG_SV = re.compile(
    # not "uppbyggnad" (the build-up of a database, of muscle, of the welfare state)
    r"(?<!upp)byggnad|husbygg|\bbostad|\bbostäder|"
    r"bostads(?:bygg|hus|områd|bestånd|marknad|brist|produktion)|"
    r"flerbostadshus|småhus|\bvill(?:a|an|or|orna)\b|kontorsbyggnad|skolbyggnad|"
    r"(?:enfamiljs|flerfamiljs|rad|par|flervånings|hög|passiv|kontors|parkerings|punkt|lamell|"
    r"fritids)hus|sommarstug|\blägenhet|panncentral|"
    r"bygg(?:bransch|projekt|produktion|process|arbetsplats|sektor|industri|herr|entrepren|lov|"
    r"teknik|konstruktion|material|företag|logistik|platschef|ledning|regler|del|system|fukt|"
    r"skede|kostnad|arbetsmiljö|avfall|element|nad[ds])|"
    r"entreprenadform|totalentreprenad|utförandeentreprenad|nybyggnation|ombyggnad|tillbyggnad|"
    r"påbyggnad|\bbetong|armering|stålkonstruktion|träkonstruktion|träbygg|trähus|trästomm|"
    r"\blimträ|kl-trä|korslimma|murverk|\btegel|fasad|takkonstruktion|bjälklag|stomsystem|"
    r"byggnadsstomm|grundläggning|\bpålning|\bpålar\b|geotekni|bergteknik|bergtunn|vägtunn|"
    r"järnvägstunn|tunnelbygg|\bbro(?:ar|n|ars|arna|konstruktion\w*|bygg\w*|underhåll\w*)?\b|"
    r"järnvägsbro|vägbro|gångbro|\w*träbro\w*|vägbygg|vägkonstruktion|vägunderhåll|asfalt|"
    r"vägbelägg|järnvägs(?:bro|bygg|anlägg|underhåll|infrastruktur|spår|bank|tunn)|"
    r"banunderhåll|spårväg|dagvatten|avloppsvatten|avloppsren|avloppsled|"
    r"reningsverk|va-(?:system|nät|ledning|bransch|verksamhet)|vattenledning|"
    r"dricksvattenförsörjning|"
    r"fjärrvärme|fjärrkyla|värmepump|bergvärme|inomhusklimat|inneklimat|inomhusmiljö|"
    r"inomhusluft|"
    r"klimatskal|köldbrygg|fuktsäker|fuktskad|passivhus|lågenergihus|miljonprogram|"
    r"nära-?nollenergi|"
    r"energideklaration|brandskydd|brandsäkerhet|brandteknisk|stadsplanering|stadsbygg|"
    r"samhällsbygg|samhällsplanering|detaljplan|översiktsplan|stadsutveckling|förtätning|"
    r"\bfastighet|hyresrätt|bostadsrätt|\barkitektur\b|landskapsarkitekt|kulturmiljö|"
    r"byggnadsvård|dagsljus|solavskärmning|radonhalt|\bbim\b|ventilationssystem|fönsterbyte|"
    r"tilläggsisolering|värmeåtervinning i (?:fler|bostad|byggnad)|golvvärme|"
    # Norwegian / Danish forms that differ from Swedish
    r"\bbygning|\bboliger\b|\bbolig(?:bygg|blokk|område)|byggeri|byggebransj|byggenæring|"
    r"fjernvarme|varmepumpe|inneklima|indeklima|byplan|\bbeton(?:g|konstruk)|"
    r"brannsikkerhet|brandsikring|overvann|spildevand",
    re.I,
)
STRONG_EN = re.compile(
    r"\bbuildings\b|"
    r"\b(?:office|residential|school|apartment|timber|wooden|concrete|high-rise|tall|"
    r"multi-?stor\w+|historic\w*|heritage|existing|public|commercial|industrial|green|"
    r"sustainable|energy[- ]efficient|low[- ]energy|zero[- ]energy|passive|smart|modular|"
    r"prefabricated|university|hospital|multi-?family|single-?family) building\b|"
    r"\bbuilding (?:energy|envelopes?|stock|sector|industry|materials?|permits?|codes?|"
    r"regulations?|design|performance|physics|services|information model\w*|construction|"
    r"renovation|retrofit\w*|projects?|sites?|process|technolog\w+|structures?|elements?|"
    r"components?|fa[cç]ades?|automation|management systems?|owners?|life cycle|simulation|"
    r"refurbishment|conservation|certification|ventilation|insulation|acoustics|heating|"
    r"cooling|integrated)\b|"
    r"\bconstruction (?:industry|sector|sites?|projects?|management|compan\w+|works?|materials?|"
    r"waste|process\w*|methods?|safety|contracts?|logistics|supply chain|firms?|workers?|"
    r"equipment|machin\w+|engineering|phase|stage)\b|"
    r"\bcivil engineering|\bstructural (?:engineering|design|analysis|health monitoring|timber|"
    r"steel|elements?|members?|behaviou?r|response|performance|reliability|safety|glass|"
    r"optimi[sz]ation of (?:a |the )?(?:bridge|building|frame|truss|slab))|"
    r"reinforced concrete|\bconcrete (?:structures?|slabs?|beams?|columns?|bridges?|walls?|"
    r"mix\w*|strength|crack\w*|durability|elements?|frames?|pavements?|dams?|floors?|"
    r"production|industry|buildings?|construction|specimens?|tunnels?|linings?)\b|"
    r"\b(?:fib(?:er|re)[- ]reinforced|self-compacting|precast|prestressed|post-tensioned|"
    r"sprayed|high[- ](?:performance|strength)|recycled(?: aggregate)?|low-carbon|"
    r"green|geopolymer) concrete|shotcrete|"
    r"\btimber (?:buildings?|structures?|frames?|construction|elements?|joints?|floors?|"
    r"bridges?|connections?|beams?|engineering)\b|cross[- ]laminated timber|\bglulam\b|"
    r"\bmasonry\b|\bsteel (?:structures?|frames?|bridges?|beams?|columns?|connections?|"
    r"girders?|trusses|construction)\b|"
    r"\b(?:road|railway|rail|highway|concrete|steel|timber|suspension|cable[- ]stayed|arch|"
    r"girder|pedestrian|foot|truss|composite|integral|soil[- ]steel) ?bridges?\b|"
    r"\bbridge (?:decks?|design|girders?|structures?|maintenance|engineering|piers?|"
    r"management|inspection|construction|bearings?|dynamics)\b|"
    r"geotechni|foundation engineering|\bpile (?:foundations?|groups?|driving|design)|"
    r"\b(?:driven|bored|end[- ]bearing|friction|timber|steel|concrete) piles\b|"
    r"\bretaining walls?|\bsheet piles?|slope stability|\bembankments?\b|soil stabili[sz]ation|"
    r"\b(?:road|rail(?:way)?|rock|metro|subway|hard[- ]rock|immersed) tunnels?\b|"
    r"\btunnel(?:ling)? (?:construction|excavation|linings?|boring|fires?|ventilation|projects?|"
    r"in (?:rock|soil))|rock (?:mechanics|engineering|support|bolts?|mass)\b|"
    r"\bpavements?\b|\basphalt|\bbitumen|\broad (?:construction|maintenance|design|"
    r"infrastructure|surfaces?|network maintenance|safety barriers?|drainage|structures?)\b|"
    r"\brailway (?:tracks?|infrastructure|bridges?|maintenance|embankments?|switches|ballast|"
    r"turnouts?|stations?|tunnels?)\b|track ballast|"
    r"storm ?water|waste ?water|\bsewage|\bsewers?\b|sewerage|"
    r"drinking water (?:treatment|distribution|networks?|supply|production|pipes?|systems?)|"
    r"water (?:supply|distribution) (?:systems?|networks?)|water utilit|"
    r"district[- ](?:heating|cooling|energy)|\bheat pumps?\b|\bhvac\b|"
    r"\bindoor (?:climate|air quality|environment\w*|thermal|air)\b|thermal comfort|"
    r"building envelope|thermal bridges?|passive houses?|\bpassivhaus|energy performance "
    r"certificat|energy (?:renovation|retrofit\w*)|net[- ]zero energy|"
    r"\b(?:natural|mechanical|demand[- ]controlled|displacement|exhaust|hybrid) ventilation "
    r"(?:in|of|for|systems? (?:in|of|for)) (?:\w+ )?(?:buildings?|schools?|offices?|dwellings?|"
    r"houses?|apartments?|homes?|classrooms?|residential)|ventilation systems?\b|"
    r"\bdwellings?\b|apartment (?:buildings?|blocks?)|residential (?:buildings?|houses?|"
    r"areas?|construction|development)|multi-?(?:family|dwelling|stor\w+) (?:houses?|housing|"
    r"residential)|single-?family houses?|detached houses?|\bhousing (?:construction|stock|"
    r"shortage|development|compan\w+|market|policy|estates?|areas?|projects?|cooperativ\w+|"
    r"associations?|renovation|prices?|sector|production|supply)\b|"
    r"fire (?:safety|protection|resistance|dynamics|engineering|spread in|safety design)|"
    r"\bevacuation (?:of|from|in) (?:\w+ )?(?:buildings?|tunnels?|high-rise|stations?)|"
    r"urban (?:planning|design|development|densification|regeneration|renewal|form|"
    r"morpholog\w+|heat island|drainage|infrastructure|districts?|green|space|sprawl|"
    r"transformation|land use)|city (?:planning|districts?|development)|spatial planning|"
    r"land[- ]use planning|town planning|comprehensive plan|detailed development plan|"
    r"\breal estate|property (?:management|development|valuation|developers?|owners?|market|"
    r"compan\w+)|facilit(?:y|ies) management|commercial propert|"
    r"landscape architect|architectural (?:engineering|heritage|design|competition|practice|"
    r"conservation|history|education|quality|space)|built (?:environment|heritage)|"
    r"building information model|\bbim\b|\bdaylight|green roofs?|\bfa[cç]ades?\b|"
    r"\bprefabricat\w+ (?:house|housing|building|concrete|timber|element|construction)|"
    r"modular construction|off-?site construction|industriali[sz]ed (?:house|housing|"
    r"construction|building)|radon (?:in|levels?|mitigation)|"
    r"\bcement(?:itious)?\b(?! (?:of|between))|"
    r"energy[- ]efficien\w+ (?:in |of |for )?(?:\w+ )?(?:buildings?|hous\w+|dwellings?|renovation)|"
    r"ground[- ]source|borehole (?:heat|thermal)|solar (?:shading|gains?)|"
    r"\b(?:photovoltaic|pv|solar)\b[^;]{0,40}"
    r"\b(?:buildings?|roofs?|fa[cç]ades?|houses?|residential)",
    re.I,
)

# HSV / SCB research-subject codes (xlink:href of <subject authority="hsv">).
# 201 Civil Engineering: 20101 Architectural Engineering, 20102 Construction Management,
# 20103 Building Technologies, 20104 Infrastructure Engineering, 20105 Transport Systems and
# Logistics (NOT built environment on its own), 20106 Geotechnical Engineering, 20107 Water
# Engineering, 20108 Environmental Analysis and Construction Information Technology, 20199 Other.
HSV_STRONG = {"20101", "20102", "20103", "20104", "20106"}
HSV_STRONG_NAMES = {"architecture", "landscape architecture"}
# The group code 201 itself and 20105 / 20107 / 20108 / 20199 also tag real-estate finance,
# logistics, hydrology and geodesy theses (measured 2026-10-12: 62 of 64 such records without a
# STRONG concept were off-domain), so they never pass a record on their own.

# Off-domain context for words with a second meaning (mechanical ventilation of patients,
# software architecture, "building" as a verb ...): vetoes unless a STRONG concept is present.
OFF_CONTEXT = re.compile(
    r"\bpatient|\bnurs|sjuksköt|omvårdnad|intensivvård|\bclinical|\bklinisk|\bsurg|kirurg|"
    r"\bdisease|sjukdom|\bcancer|\bcovid|\bpediatric|\bneonat|\bventilator[- ]associated|"
    r"\bsoftware|mjukvaru|programvaru|\bmicroservice|\bcloud (?:comput|service|platform)|"
    r"\bneural network|"
    r"\bfpga\b|\bprocessor|\bcompiler|\bblockchain|\bapp\b|\bweb\b|\bgames?\b|\bspel\b|"
    r"\bvehicle|\bfordon|\bbil(?:ar|en)?\b|\btruck|lastbil|\baircraft|flygplan|\bengine\b|"
    r"\bmotor\b|\bbatter|\brobot|\bsatellit|\bantenn|\bwireless|\b5g\b|\bradar\b|"
    r"\bprotein|\bgenom(?!\b)|\benzym|\bcell(?:s|er)?\b|\bfood\b|livsmedel|\bfisk|\bskog(?:s|en)?\b|"
    r"\bforestry|\bpulp\b|massa(?:bruk|industri)|pappers(?:bruk|industri)|\bgruv|\bmining\b|"
    r"\bfilm\b|\bnovels\b|\broman(?:en)?\b|\bdikt|\bpoet|\bmusik|\bmusic\b|\bteater|"
    r"förskol(?:a|an|lärar)|\bpreschool|\belev(?:er|ers)?\b|\bpupils?\b|undervisning|"
    r"\bteaching\b|\bdidakti",
    re.I,
)

# Topic radar label: first rule that matches titles + keywords, else the classification.
SIM = re.compile(
    r"simul|ida[ -]?ice|energy ?plus|trnsys|modelica|vip[- ]?energy|designbuilder|\bcfd\b|"
    r"computational fluid|digital twin|energiberäkning|energimodell|energy model", re.I)
TOPIC_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("infrastructure", re.compile(
        r"\bväg|\broads?\b|pavement|asfalt|asphalt|bitumen|järnväg|railway|spårväg|\btrack\b|"
        r"dagvatten|storm ?water|avlopp|waste ?water|sewage|sewer|reningsverk|vattenledning|"
        r"drinking water|dricksvatten|water (?:supply|distribution|utilit)|\bva-|tunnel|tunnl|"
        r"\bhamn|\bharbou?r|\bdam(?:m|ms|mar)?\b|trafik|traffic|infrastru[ck]tur", re.I)),
    ("structures_civil", re.compile(
        r"\bbro(?:ar|n)?\b|bridge|geotekn|geotechn|grundläggning|foundation|\bpål|\bpiles?\b|"
        r"structural|bärande|bärförmåga|stomm|stomsystem|\bframe|dimensionering|eurokod|"
        r"eurocode|seismic|jordbävning|earthquake|stålkonstruktion|träkonstruktion|"
        r"betongkonstruktion|bjälklag|\bbalk|\bbeams?\b|\bcolumns?\b|pelare|slope|släntstabil|"
        r"bergmekanik|rock mechanics|finite element|knäckning|buckling|utmattning|fatigue", re.I)),
    ("materials", re.compile(
        r"betong|concrete|cement|armering|byggmaterial|building materials?|construction "
        r"materials?|tegel|murverk|masonry|limträ|glulam|kl-trä|cross[- ]laminated|"
        r"\bträ\b|timber|\bwood|korrosion|corrosion|bindemedel|\bballast|aggregate",
        re.I)),
    ("equipment_systems", re.compile(
        r"värmepump|heat pumps?|bergvärme|ventilationssystem|ventilation system|\bhvac\b|"
        r"värmeväxl|heat exchanger|kylmaskin|chiller|\bpann|boiler|golvvärme|radiator|"
        r"solfångare|solar collector|ackumulatortank|borrhål|borehole", re.I)),
    ("building_energy", re.compile(
        r"energi|energy|fjärrvärme|fjärrkyla|district (?:heating|cooling)|värme|\bheat|kyla|"
        r"cooling|inomhus|inneklimat|indoor|thermal|termisk|klimatskal|envelope|köldbrygg|"
        r"passivhus|passive house|fukt|moisture|ventilation|isolering|insulation|dagsljus|daylight|solcell|"
        r"photovoltaic|belysning|lighting|radon|akusti|acoust|klimatpåverkan|livscykel|"
        r"life cycle|\blca\b", re.I)),
    ("urban", re.compile(
        r"stadsplan|stadsbygg|stadsutveckl|samhällsplan|detaljplan|översiktsplan|förtätning|"
        r"\burban|\bcity\b|\bcities\b|spatial planning|land[- ]use|town planning|stadsdel|"
        r"\bstad(?:en|s\w*)?\b|\bstäder|grönområd|green space|markanvändning|exploatering",
        re.I)),
    ("architecture", re.compile(
        r"arkitekt|architect|brandskydd|brandsäker|brandteknisk|brannsik|brandsikring|\bfire\b|utrymning|evacuation|"
        r"kulturmiljö|byggnadsvård|kulturarv|heritage|gestaltning|fasad|fa[cç]ade", re.I)),
)
HSV_TOPIC = {"20106": "structures_civil", "20107": "infrastructure", "20104": "infrastructure",
             "20105": "infrastructure", "20101": "architecture"}


def _strong(text: str) -> bool:
    return bool(STRONG_SV.search(text) or STRONG_EN.search(text))


def hsv_strong(codes: list[tuple[str, str]]) -> bool:
    """A built-environment research subject on its own: Architectural Engineering, Construction
    Management, Building Technologies, Infrastructure Engineering, Geotechnical Engineering, or
    a subject whose English name is Architecture / Landscape Architecture."""
    return any(code in HSV_STRONG or name.strip().lower() in HSV_STRONG_NAMES
               for code, name in codes)


def relevant(titles: list[str], keywords: list[str], codes: list[tuple[str, str]]) -> bool:
    """Precision-first decision for one record. `codes` are (HSV code, English leaf name)."""
    if not titles or any(bes_relevance.HARD_KILL.search(t) for t in titles):
        return False
    title_text = " ; ".join(titles)
    hits = sum(1 for k in keywords if _strong(k))
    # 20105 Transport Systems and Logistics corroborates nothing about the built environment
    civil = any(code.startswith("201") and code != "20105" for code, _ in codes)
    off = bool(OFF_CONTEXT.search(title_text))
    if _strong(title_text):
        # "bro" as a figure of speech in a didactics thesis, an HVAC part of a car: a title
        # that also names an off-domain context needs a second, independent signal
        return not off or hits >= 1 or civil or hsv_strong(codes)
    if off:
        # keywords and classification are broader than a title: an off-domain title vetoes them
        return False
    if hsv_strong(codes):
        return True
    # Author keywords are broader than a title ("fire safety" of a forest machine, "concrete" in
    # a wind-power LCA): they need a second one, or the Civil Engineering group code.
    return hits >= 2 or (hits == 1 and civil)


def topic(titles: list[str], keywords: list[str], codes: list[tuple[str, str]]) -> str:
    text = " ; ".join([*titles, *keywords])
    if SIM.search(text) and (bes_relevance.BUILT.search(text) or STRONG_SV.search(text)):
        return "simulation_modeling"
    for name, pattern in TOPIC_RULES:
        if pattern.search(text):
            return name
    for code, _ in codes:
        if code in HSV_TOPIC:
            return HSV_TOPIC[code]
    return "construction"
