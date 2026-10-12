#!/usr/bin/env python3
"""ojs_gate.py — the built-environment gate of find_ojs's broad (`gate`) venues.

A broad engineering, planning or heritage journal mixes building and infrastructure papers with
electronics, chemistry, food science, computing and medicine. relevant(text) keeps a record only
when its title or subjects carry built-environment evidence:

  * an UNAMBIGUOUS term (concrete, masonry, pavement, seismic, HVAC, housing, urban ..., with
    es / pt / id / pl / ru / uk / it / de / fr equivalents), or
  * an AMBIGUOUS stem in a built-environment phrase. A bare "structure", "thermal", "steel",
    "soil", "foundation", "wall", "beam", "column", "energy", "transport", "construction" or
    "architecture" proves nothing ("Protein structure prediction", "Thermal degradation of
    edible oils", "Software architecture", "Construction of a fuzzy controller"); "structural
    analysis", "thermal comfort", "steel beam", "soil stabilization", "pile foundation",
    "shear wall", "construction industry" do.

Measured 2026-10-12 on 2,425 live records (first OAI page of 27 gated venues): the previous
bare-stem gate kept 902; this gate keeps 585. A reading of 100 random kept records found 6 that
are clearly not built-environment (a place name with "city", a subject keyword) and about as
many borderline transport-service papers; a reading of 100 random dropped records found 3 that
are (their patterns were then added). The loader's quality gate (scripts/quality.py) stays the
second filter.
"""
from __future__ import annotations

import re

_STRONG = (
    # --- English
    r"\bbuildings\b|(?<!capacity )(?<!capacity-)(?<!competency )(?<!team )(?<!peace )"
    r"(?<!model )(?<!nation )\bbuilding\b(?! (?:a|an|on|the|up|of|approach|blocks?)\b)",
    r"\bbuilt environment|\bcivil engineering|\bhigh-?rise|\bmulti-?stor(?:e)?y|\bstor(?:e)?ys?\b",
    r"\bconcretes?\b(?! example)|\bcements?\b|\bcementitious|\bcement-|\bmortars?\b|\bmasonry|\bbricks?\b|\btimber|\bplywood|"
    r"\bglulam|\brebars?\b|\bprestress|\bpost-?tension|\bgeopolymer|\bfly ash|"
    r"\bpozzolan|\bbitum|\basphalt|\bpavements?\b|\bgeotech|\bgeotextile|\bgeosynthetic|"
    r"\bgeogrid|\bembankment|\bsubgrade|\blandslide|\bslope stabil|\bretaining\b|\bfooting|"
    r"\baseismic|\bantiseismic|\bhighway|\broadway|\broads?\b|\brailways?\b|\brailroads?\b|\brail tracks?\b|"
    r"\bgirder|\btruss(?:es)?\b|\bslabs?\b|\bformwork|\bculvert|\bspillway|\bweirs?\b|"
    r"\bdams?\b|\bfloods\b|\bflood (?:risk|damage|control|protection|hazard|mitigation|map|"
    r"model|forecast|event|defen|management|inundation|prone|vulnerab|resilien|wave)|"
    r"\b(?:urban|flash|river) flood|\bdrainage|\bsewers?\b|\bsewerage|\bstormwater|\blandfill|"
    r"\b(?:wastewater|sewage) treatment plants?\b|\b(?:municipal|domestic|urban) "
    r"(?:wastewater|sewage|solid waste)|\bsewage (?:system|network)|"
    r"\bwater (?:supply|distribution|treatment plant|network)|\birrigation canal|"
    r"\bhvac\b|\bair[- ]condition|\bheat pumps?\b|\bventilat|\bdaylight|\bheat loss(?:es)?\b|"
    r"\bindoor (?:air|environment|climate|thermal|temperature|comfort|lighting|acoustic|space)|"
    r"\bfa[cç]ades?\b|\bglazing|\broof(?:s|ing|top)?\b|\bdwelling|\bresidential|"
    r"(?<!motor )(?<!bearing )(?<!pump )(?<!gear )\bhousing\b|\bhouses\b|\bindividual house|"
    r"\bcommunal services|\burban|\bcities\b|\b(?:city|town) (?:plan|cent(?:er|re)|development|"
    r"council|growth|form|design|street|district|image|identity|expansion)|\b(?:smart|creative|"
    r"historic|sustainable|compact|garden|new|old|inner|capital) city\b|\btowns\b|"
    r"\bmunicipal (?:infrastructure|water|building|services|planning|engineering)|"
    r"\bneighbou?rhood|\bland use|"
    r"\breal estate|\bproperty (?:management|market|valuation|development)|\bfacilit(?:y|ies) "
    r"management|\b(?:road|transport|urban|civil|water|green|municipal|public|railway|"
    r"critical|bridge|drainage|sanitation|building|physical|blue|grey) infrastructures?\b|"
    r"\binfrastructures? (?:project|asset|development|investment|planning|management|resilien|"
    r"maintenance|deteriorat|engineering|financ|deliver|procure)|\bbim\b|\bbuilding information|"
    r"\bquantity survey|\bcontractors?\b|\btunnel(?:s|ling|ing)?\b(?! (?:diode|junction|effect|"
    r"fet|current))|\bbridges?\b(?! (?:circuit|converter|rectifier|inverter|amplifier))|"
    r"\bairport|\brunway|\bharbou?r|\bparking|\bpedestrian|\bintersections?\b|\broundabout|"
    r"\bpublic transport|\btransit\b|\blanes?\b|\bmedian opening|\bmixed traffic|"
    r"\bbus (?:stop|route|arrival|rapid|lane|service|network)|\bfire (?:safety|resistance|protection|spread|dynamics|"
    r"load|engineering)|\bfire-?resist|\bevacuation|\bchurch|\bcathedral|\bmonuments?\b|"
    r"\bcastle|\bpalace|\bmosque|\bhistoric(?:al)? (?:building|cent(?:er|re)|town|"
    r"district|masonry)|\bbuilt heritage|\barchitectural heritage|\bplaster|\bstucco|"
    r"\bzero[- ]energy|\bnzeb\b|\benergy retrofit|\bgreen roof|\bsmart cit",
    # --- Spanish / Portuguese
    r"\bedific|\bedif[ií]c|\bviviendas?\b|\bhabita[cç][aãoõ]|\bhabitacional|\bhabitacionais|"
    r"\bmoradia|\bhormig|\bcemento|\bcimento|\bargamassa|\bmorteros?\b|\bladrillo|\btijolo|"
    r"\balvenaria|\bmamposter|\bs[ií]smic|\bsismo|\bterremoto|\bgeot[eé]c|\btalud|"
    r"\bcimentaci|\bfunda[cç][oõ]es|\bpavimento|\basf[aá]lt|\bpuentes?\b|\bpontes?\b|"
    r"\bcarretera|\brodovi|\bferrovi|\bferrocarril|\bt[uú]nel|\bt[uú]neis|\bbarrage|\bpresas?\b|"
    r"\balcantarill|\besgoto|\bsaneam|\bdrenaje|\bdrenagem|\binundaci|\benchente|"
    r"\bagua potable|\babastecim|\burban[oa]s?\b|\burbanis|\burbaniza|\bciudad|\bcidades?\b|"
    r"\bpatrim[oô]nio (?:arquitect|arquitet|edificad|constru|cultural|hist)|"
    r"\bclimatiza|\bcalefacci|\bventila[cç]|\bconforto? t[eé]rmico|\b(?:aislamiento|isolamento) "
    r"t[eé]rmico|\bfachada|\bcubiertas?\b|\btechos?\b|\btelhado|\blosas?\b|\blajes?\b|"
    r"\bvigas?\b|\bmuros?\b|\bp[oó]rticos?\b|\bobras? civil|\bingenier[ií]a civil|"
    r"\bengenharia civil|\bconstru[cç][aã]o civil|\bcanteiro|\biglesia|\bigreja|\bcatedral|"
    r"\bmonumento|\bcastillo|\bcastelo|\bconvento|\bmosteiro|\bmonasterio|\bazulejo|\bestuque|"
    r"\bconcreto (?:armado|reforzado|asf[aá]ltico|simple|pretensado|protendido|permeable|"
    r"de alt|autocompact|autoadens|reciclado|hidr[aá]ulico|con\b|com\b|ligero|leve)|"
    r"\b(?:de|del|do|em|en) concreto (?:armado|reforzado|asf|pre|pro|con\b|com\b)|"
    r"\b(?:vigas?|losas?|lajes?|pilares?|columnas?|mezclas?|misturas?|estruturas?|estructuras?|"
    r"pavimentos?|blocos?|bloques?|muros?) (?:de|del|do|em|en) concreto",
    # --- Indonesian / Malay
    r"\bbangunan|\bgedung|\bkonstruksi|\bbeton|\bjalan\b|\bjembatan|\bperkerasan|\baspal|"
    r"\bpondasi|\bfondasi|\bgempa|\bbalok\b|\bpelat\b|\btulangan|\bbertulang|"
    r"\bkolom (?:beton|baja|struktur|komposit|pendek|langsing)|\barsitektur|\bperumahan|"
    r"\brumah\b|\bperkotaan|\bkota\b|\bdrainase|\bbanjir|\birigasi|\bbendung|\blalu lintas|"
    r"\btransportasi|\bsimpang\b|\bbandara|\bpelabuhan|\bsemen\b|\bbata\b|\bbaja\b|"
    r"\bdaya dukung|\btanah (?:lempung|lunak|dasar|ekspansif)|\bstabilisasi tanah",
    # --- Polish
    r"budownictw|budowlan|\bbudynk|[żz]elbet|nawierzchni|\bdrogow|wiadukt|\bmostow|\bgrunt|"
    r"\bfundament|\burbanist|\bmiast|\bmiejsk|mieszka[nl]|\bceg[łl]|\bzabytk|rewitaliz|"
    r"termomodernizac|ociepleni|wentylac|ogrzewani|\bko[śs]ci[oó][łl]|\bzamek\b|\bpa[łl]ac",
    # --- Russian / Ukrainian
    r"строител|здани|сооружен|бетон|арматур|кирпич|фундамент|грунт|градостро|город|жилищ|"
    r"жил(?:ой|ые|ых|ого)\b|мостов|дорог|дорож|асфальт|тоннел|сейсм|теплоизол|вентиляц|"
    r"отоплен|водоснабж|водоотвед|канализац|кровл|фасад|недвижим|коммунальн|"
    r"будів|споруд|залізобетон|цегл|ґрунт|містобуд|\bміст[оа]?\b|міськ|житл|тунел|теплоізол|"
    r"опален|водопостач|водовідвед|каналізац|покрівл|нерухом|комунальн|пожежн\w* безпек|"
    r"вогнестійк|евакуац",
    # --- Italian / German / French
    r"\bedilizi|calcestruzzo|\bmuratur|\bcitt[àa]\b|\barchitett|\brestauro|\bchies[ae]\b|"
    r"\bpalazz|\bgeb[äa]ude|\bbauwerk|\bbauwesen|mauerwerk|stahlbau|holzbau|tragwerk|"
    r"\bstadt|\bst[äa]dt|\bwohn|\bdenkmal|\bb[âa]timent|\bb[ée]ton|ma[çc]onnerie|"
    r"g[ée]nie civil|chauss[ée]e|\blogement|\burbanisme|\bvilles?\b|patrimoine b[âa]ti",
)
# Ambiguous stems count only inside a built-environment phrase.
_CONTEXT = (
    r"\bstructural (?:engineering|analysis|design|health|integrity|elements?|members?|steel|"
    r"behaviou?r|response|damage|reliability|performance|systems?|safety|strengthen|retrofit|"
    r"glass|wall|loads?|assessment|optimi[sz]ation of (?:truss|frame)|dynamics|vibration)|"
    r"\b(?:steel|framed?|shell|offshore|hydraulic|civil|marine|historic(?:al)?|space|tall|"
    r"composite|lattice|tensile|cable|truss|sandwich) structures?\b|\bsoil-?structure|"
    r"\bstructures? (?:under|subjected to) (?:wind|blast|impact|fire|load)|"
    r"\bthermal (?:comfort|insulation|bridg|mass|transmittance|envelope|barrier)|"
    r"\bseismic (?:performance|design|retrofit|isolat|response|behaviou?r|analysis of|vulnerab|"
    r"risk|hazard|assessment|load|resistan|strengthen|fragility|demand|capacity|code|zon|upgrad|"
    r"rehabilit|protection|evaluation of|excitation)|\bearthquake (?:engineering|resist|damage|"
    r"load|excitation|response|ground motion|hazard|risk)|\bearthquake-(?:induced|resistant)|"
    r"\breinforced (?:concrete|masonry|soil|earth|beam|slab|column|wall|embankment|brick|mortar|"
    r"cement|pavement|slope)|\bfib(?:er|re)[- ]reinforced (?:concrete|polymer|mortar|cement)|"
    r"\b[cg]?frp[- ](?:bars?|sheets?|strips?|confin|strengthen|reinforc|wrap)|"
    r"\b(?:outer|inner|exterior|external) walls?\b|"
    r"\bsteel (?:beam|column|frame|plate girder|bars?\b|fib|structure|connection|tub|section|"
    r"member|joint|deck|brac|slag|shear)|\bcold-?formed|\bcfst\b|"
    r"\bsoils? (?:stabili|mechanic|improv|bearing|liquefaction|compaction|settlement|slope|"
    r"reinforc|nailing|consolidation|shear|strength|erosion)|\b(?:expansive|collapsible|"
    r"gypseous|soft|sandy|clayey|cohesive|granular|lateritic|treated|stabili[sz]ed|saline|"
    r"saturated|unsaturated|compacted|fine-grained|residual|organic) soils?\b|\bcollapsib|"
    r"\bdin[aá]mica de suelos|\brespuesta (?:din[aá]mica )?de sitio|\baterro sanit|"
    r"\brelleno sanit|"
    r"\bclay(?:ey)?\b|\b(?:pile|piled|raft|shallow|deep|mat|strip|spread|machine) "
    r"foundations?\b|\bfoundations? (?:on|in|under) (?:soil|clay|sand|rock|slope)|"
    r"\bpiles? (?:group|cap|load|driv|capacity|subjected|in (?:sand|clay|dense))|\blong piles?\b|"
    r"\bsteel construction|\b(?:bored|driven|sheet|micro) ?piles?\b|"
    r"\b(?:shear|masonry|infill|bearing|curtain|diaphragm|partition|external|exterior|cavity|"
    r"trombe|green|sandwich|stone|rammed|earth|panel) walls?\b|"
    r"\b(?:rc|deep|cantilever|continuous|t-|i-|box|composite|laminated|castellated|simply "
    r"supported) ?beams?\b|\bbeam-?column|\bbeams? (?:under|subjected|strengthen|with web)|"
    r"\b(?:rc|cfst|tubular|short|slender|stone|composite|circular|square) columns?\b|"
    r"\bcolumns? (?:under|subjected|strengthen|confin)|"
    r"\bfloor (?:slab|system|plan|heating|vibration|area)|\bconstruction (?:industr|projects?|"
    r"sites?|materials?|management|sector|works?|costs?|waste|safety|compan|firms?|contract|"
    r"labou?r|delay|technolog|equipment|joints?|stage|phase|process|activit|productivity|"
    r"workers?|claims?|disputes?|risk)|\b(?:road|housing|sustainable|modular|lean|green|"
    r"off-?site|prefabricated) construction|\bunder construction|"
    r"\benergy (?:efficien\w*|sav\w*|consum\w*|performance|demand|use) (?:in|of|for) "
    r"(?:hous|school|office|residen|hospital|hotel|campus|home)|"
    r"\btraffic (?:flow|safety|accident|crash|signal|congestion|volume|noise|management|"
    r"simulation|calming|delay|control at|engineering)|\broad traffic|\btransport(?:ation)? "
    r"(?:infrastructure|planning|network|engineering|corridor|polic|mode|demand)|"
    r"\bfreight|\bport (?:terminal|infrastructure|facilit)|\bhydraulic (?:structure|"
    r"engineering|jump|model of)|\bopen channel|\bscour\b|\baggregates?\b|"
    # es / pt
    r"\b(?:an[aá]lisis|an[aá]lise|dise[ñn]o|projeto|comportamiento|comportamento|ingenier[ií]a|"
    r"engenharia|elementos?|refuerzo|reforzamiento|refor[cç]o|vulnerabilidad|vulnerabilidade|"
    r"seguridad|seguran[cç]a|integridad|respuesta|da[ñn]o|rehabilitaci[oó]n|reabilita[cç][aã]o|"
    r"desempenho|desempe[ñn]o) estr?u[c]?tura(?:l|is|les)\b|\bestr?u[c]?turas? (?:de acero|"
    r"de a[cç]o|met[aá]lic|de madera|de madeira|sismorresist|aporticad|reticulad)|"
    r"\bacero estructural|\ba[cç]o estrutural|\b(?:mec[aá]nica|mec[aâ]nica) d[eo]s? "
    r"(?:suelos?|solos?)|\b(?:estabilizaci[oó]n|estabiliza[cç][aã]o|mejoramiento|compactaci[oó]n|"
    r"compacta[cç][aã]o) d[eo]s? (?:suelos?|solos?)|\b(?:suelos?|solos?) (?:arcillos|argilos|"
    r"expansiv|bland|mole|residua|compactad|later[ií]tic|ciment|grampead|refor[cç]ad|granular)|"
    r"\b(?:industria|sector|setor|materiales|materiais|proyectos?|projetos?|gesti[oó]n|"
    r"gest[aã]o|obras?|residuos|res[ií]duos|costos?|custos?|seguridad|seguran[cç]a) (?:de|de la|"
    r"da|na|en la) constru[cç]|\bconstru[cç]\w+ (?:sostenible|sustent|de (?:edific|viviend|"
    r"carreter|puentes?|pontes?|obras?|estradas?))|\beficiencia energ[eé]tica (?:en|de|em) "
    r"(?:edific|viviend|habita)|\bhidr[aá]ulic[ao] (?:de canales|fluvial|urbana)|"
    r"\b(?:ingenier[ií]a|engenharia) (?:estructural|estrutural|s[ií]smica|vial|de transporte)|"
    # id
    r"\bstruktur (?:bangunan|beton|baja|gedung|jembatan|atas|bawah|rangka|portal)|"
    # pl / ru
    r"konstrukcj\w* (?:stalow|[żz]elbet|drewnian|murow|budowl|no[śs]n)|"
    r"конструкци\w* (?:здани|сооружен|стальн|железобетон|деревянн|несущ)"
)
# "architecture" proves a building subject unless the text is about computing.
_ARCH = re.compile(r"\barchitect|\barquitect|\barquitet|\barchitekt|архитект|архітект", re.I)
_COMPUTING = re.compile(
    r"software|\bnetworks?\b|comput|processor|fpga|\bneural|\bcloud|\biot\b|microservic|"
    r"\bcpu\b|\bgpu\b|deep learning|transformer|protocol|database|\bcnn\b|\bvlsi\b|\bsoc\b|"
    r"\bchip|embedded|inform[aá]tic|empresarial|iptv|\bvideo|servicios?\b|telecom|wireless|\bredes?\b|\bdatos\b|\bdados\b|\bsistemas? (?:de )?"
    r"(?:informaci|informa[cç])|blockchain|\bapi\b|\bweb\b|enterprise", re.I)
AEC = re.compile("|".join(_STRONG) + "|" + _CONTEXT, re.I)


def relevant(text: str) -> bool:
    """Whether a title (with subjects) carries built-environment evidence."""
    if AEC.search(text):
        return True
    return bool(_ARCH.search(text)) and not _COMPUTING.search(text)
