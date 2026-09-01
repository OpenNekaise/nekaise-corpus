"""Golden tests for the quality gate. Each seed is a synthetic stand-in for a document class the
gate has actually had to judge. If you tune a threshold in scripts/quality.py, these tell you
exactly which class you just killed or saved."""
import quality

# Dense built-environment prose — the average OSTI report / heating handbook paragraph.
ON_TOPIC = (
    "The heating and ventilation system of the building was designed around a low-pressure steam "
    "boiler feeding cast-iron radiators in every occupied zone. The thermal load calculation "
    "accounts for envelope insulation, window infiltration, and the heat loss through the roof "
    "and floor slabs. A damper in each duct balances the airflow, and the economizer cycle "
    "reduces cooling energy when outdoor conditions permit. Commissioning the controls requires "
    "checking each sensor, actuator, and setpoint against the sequence of operations, and the "
    "retrofit added a heat pump with improved efficiency and lower carbon emissions. "
)

# Humanities prose with no built-environment vocabulary — the Pite Saami grammar class.
OFF_TOPIC = (
    "The committee devoted its morning session to the poem's meter and the history of its many "
    "translations. Scholars have long debated whether the author intended irony when the "
    "narrator recalls her childhood by the lake, and the seminar considered how vowel harmony "
    "shapes the dialect's sound system. A chapter on verb morphology follows, tracing how the "
    "spoken idiom diverged from the written standard over three generations of usage. The "
    "closing essay reviews archival letters, diaries, and the recollections of former students. "
)

# One domain word per ~220 off-topic words (~4.5 hits/1000): enough hits to pass the short
# absolute gate over 20k chars, but below book density — the "off-topic book that sneaks past".
SPICED = OFF_TOPIC * 2 + "The lecture hall building was mentioned once in passing. "

# Number-table soup — the trade-catalog / Barlow's-tables class (OCR of tabular matter).
CATALOG_LINE = "No. 482  3/4 in.  12 1/2  9.75  0.375  1 1/4  18 20 22  $4.60  7/8 x 5 3/16\n"

# German GENERIC academic prose — off-topic in any language (no built-environment vocabulary).
GERMAN = (
    "Die Untersuchung beschreibt das Verfahren zur Bestimmung der Kennwerte und erläutert die "
    "wesentlichen Ergebnisse der Messreihe. Anschließend werden die Abweichungen zwischen den "
    "berechneten und gemessenen Werten diskutiert sowie mögliche Ursachen benannt. Der zweite "
    "Abschnitt behandelt die Auswertung der Stichprobe und die statistische Unsicherheit. "
)

# German BUILDING prose — the corpus is all-language; on-topic German must pass.
GERMAN_BUILDING = (
    "Die energetische Sanierung der Gebäude umfasst die Dämmung der Fassade, den Austausch der "
    "Heizung gegen eine Wärmepumpe und eine kontrollierte Lüftung mit Wärmerückgewinnung. Für das "
    "Tragwerk aus Beton und Mauerwerk wurden die Anforderungen des Brandschutzes geprüft, und die "
    "Bauteile der Gebäudehülle erreichen nach der Dämmung deutlich bessere Kennwerte. "
)

# Chinese building-energy prose — CJK has no spaces; the cjk-aware word count must carry it.
CHINESE_BUILDING = (
    "本报告分析了公共建筑的节能改造方案，包括围护结构保温、供暖系统改造与通风空调系统的优化运行。"
    "针对钢筋混凝土结构的既有建筑，评估了外墙保温材料的热工性能与防火要求，并对锅炉房进行了改造设计。"
    "施工过程中对地基与基础工程进行了监测，桥梁与隧道等市政设施的维护也纳入了城市规划的整体考虑。"
)

# Chinese GENERIC prose — off-topic in any language.
CHINESE_OFFTOPIC = (
    "本文考察了宋代诗歌的音韵演变及其在民间的传播方式，讨论了不同抄本之间的文字差异。"
    "作者比较了多位学者对词牌起源的看法，并分析了唐宋文学批评传统中的审美观念如何影响后世的选本编纂。"
    "文章最后回顾了近代以来的研究史，指出版本考证与文体研究相结合的必要性。"
)

# Modelica-style source code — passes via the short/absolute gate; the density rule must NOT
# apply to code (book-unlike word statistics).
MODELICA = (
    'model ThermalConductor "Lumped thermal element transporting heat without storing it"\n'
    "  parameter Modelica.Units.SI.ThermalConductance G;\n"
    "  Interfaces.HeatPort_a port_a;\n"
    "  Interfaces.HeatPort_b port_b;\n"
    "equation\n"
    "  Q_flow = G*dT;\n"
    "  dT = port_a.T - port_b.T;\n"
    '  annotation (Documentation(info="The heat flow rate is proportional to the temperature\n'
    '    difference between the two ports of this element, as in a lumped resistance."));\n'
    "end ThermalConductor;\n"
)


def rep(seed: str, min_chars: int) -> str:
    return seed * (min_chars // len(seed) + 1)


def v(text: str, book: bool) -> str:
    return quality.verdict(quality.metrics(text), book)


def test_on_topic_short_ok():
    assert v(rep(ON_TOPIC, 5_000), book=False) == "ok"


def test_on_topic_book_ok():
    assert v(rep(ON_TOPIC, 150_000), book=True) == "ok"


def test_off_topic_short():
    assert v(rep(OFF_TOPIC, 5_000), book=False) == "off-topic"


def test_off_topic_book():
    assert v(rep(OFF_TOPIC, 150_000), book=True) == "off-topic"


def test_spiced_passes_short_gate_but_book_density_kills_it():
    text = rep(SPICED, 150_000)
    assert v(text, book=False) == "ok"          # absolute gate: >=10 hits in 20k chars
    assert v(text, book=True) == "off-topic"    # density gate: << 8 hits / 1000 words


def test_catalog_is_garbage():
    assert v(rep(CATALOG_LINE, 30_000), book=False) == "garbage"
    assert v(rep(CATALOG_LINE, 150_000), book=True) == "garbage"


def test_german_generic_is_off_topic():
    assert v(rep(GERMAN, 10_000), book=False) == "off-topic"


def test_german_building_ok():
    assert v(rep(GERMAN_BUILDING, 10_000), book=False) == "ok"
    assert v(rep(GERMAN_BUILDING, 150_000), book=True) == "ok"


def test_chinese_building_ok():
    assert v(rep(CHINESE_BUILDING, 10_000), book=False) == "ok"
    assert v(rep(CHINESE_BUILDING, 150_000), book=True) == "ok"


def test_chinese_generic_is_off_topic():
    assert v(rep(CHINESE_OFFTOPIC, 10_000), book=False) == "off-topic"


def test_thin():
    assert v("too short to judge", book=False) == "thin"


def test_modelica_code_ok_via_short_gate():
    # code is never judged as a book (is_booklike gates that), but even a long code file must
    # pass through the 20k absolute gate untouched by the density rule
    assert v(rep(MODELICA, 150_000), book=False) == "ok"


def test_is_booklike_routing():
    assert quality.is_booklike("oer-some-book", "pdf")
    assert quality.is_booklike("arc-steam-heating", "txt")       # IA OCR texts are books
    assert not quality.is_booklike("gh-modelica-fluid", "txt")   # source code is not
    assert not quality.is_booklike("gh-energyplus-doc", "md")


def test_assess_strips_header():
    header = "# Title\n\nsource: http://x\nlicense: open\ntopic: t\n\n---\n\n"
    assert quality.assess(header + rep(ON_TOPIC, 5_000), "ost-x", "pdf") == "ok"


# --- patent-title off-domain kill (added 2026-07-19: semiconductor/vape/quantum patents whose
# full text passes the DOMAIN gate — "structure/thermal/insulator" saturate chip prose) ---

def test_patent_title_kill_semiconductor():
    assert quality.off_domain_title("Silicon-on-insulator channels")
    assert quality.off_domain_title("Method and Structure for Vertical Tunneling Field Effect Transistor")
    assert quality.off_domain_title("Magnetic tunnel junction with electronically reflective insulative spacer")
    assert quality.off_domain_title("Insulated gate bipolar transistor and its manufacturing method")


def test_patent_title_kill_hard_beats_guard():
    # "ventilated" would rescue via PATENT_GUARD, but a smoking article is never AEC
    assert quality.off_domain_title("Smoking article with a ventilated mouthpiece")
    assert quality.off_domain_title("Electronic cigarette with prolonged heating protection")
    assert quality.off_domain_title("Hair conditioner compositions with a preservative system")


def test_patent_title_kill_non_aec_polysemes():
    assert quality.off_domain_title("Packet tunneling and decapsulation with split-horizon attributes")
    assert quality.off_domain_title(
        "Block placing tool for building a user-defined algorithm for electronic trading"
    )


def test_patent_title_guard_rescues_real_aec():
    assert not quality.off_domain_title("A kind of UHPC wafer board composite beam bridge shear connector")
    assert not quality.off_domain_title("Semiconductor full heat recovery device, fresh air ventilator")
    assert not quality.off_domain_title("A semiconductor refrigerator")


def test_patent_title_on_topic_untouched():
    assert not quality.off_domain_title("Method and system for ensuring leak-free roof installation")
    assert not quality.off_domain_title("Heat pump with variable speed compressor")
    assert not quality.off_domain_title("Silicone roof edge accessory for foam roof")
    assert not quality.off_domain_title("Packet radio monitoring in a concrete tunnel")


def test_patent_discovery_title_kills_reviewed_false_positive_classes():
    assert quality.off_domain_discovery_title("Aerosol-generating system with ventilation airflow")
    assert quality.off_domain_discovery_title(
        "Aerosol medicament delivery adapter for dispensing medicament into a ventilation circuit"
    )
    assert quality.off_domain_discovery_title(
        "Methods to reduce microbridge defects in EUV patterning for microelectronic workpieces"
    )
    assert quality.off_domain_discovery_title(
        "Systems and methods for end-to-end map building from a video sequence"
    )
    assert quality.off_domain_discovery_title(
        "Three-dimensional printed bone cement composite scaffolds for bone regeneration"
    )
    assert quality.off_domain_discovery_title("Metal-insulator-metal capacitor and integrated chip")
    assert quality.off_domain_discovery_title("Roof top automobile ventilation system")
    assert quality.off_domain_discovery_title(
        "Method for controlling an air-conditioning system of a vehicle"
    )
    assert quality.off_domain_discovery_title("Heating control method for electric steamer")
    assert quality.off_domain_discovery_title(
        "Methods for building regression trees in a distributed computing environment"
    )
    assert quality.off_domain_discovery_title(
        "Refrigerator and control method for aging of meat"
    )
    assert quality.off_domain_discovery_title(
        "Air conditioner and battery heat dissipation system of a mining dump truck"
    )
    assert quality.off_domain_discovery_title("Roof structure for a toy vehicle")
    assert quality.off_domain_discovery_title(
        "Drainage device for pediatric neurosurgery"
    )
    assert quality.off_domain_discovery_title(
        "Big data-based medical ventilator air pressure compensation system"
    )
    assert quality.off_domain_discovery_title(
        "Medical service entry network construction method based on cloud computing"
    )
    assert quality.off_domain_discovery_title(
        "Lithium battery module fireproof insulation product for thermal runaway"
    )
    assert quality.off_domain_discovery_title(
        "Substrate heating apparatus for forming thin films"
    )
    assert quality.off_domain_discovery_title(
        "Heating substrate for a thin film thermal printing head"
    )


def test_patent_discovery_extensions_are_not_implicit_retroactive_prunes():
    # Existing bytes/URLs need an explicit reviewed-id cleanup; prune_corpus intentionally uses
    # the narrower historical predicate because applying a new regex corpus-wide is destructive.
    assert not quality.off_domain_title("Aerosol-generating system with ventilation airflow")
    assert not quality.off_domain_title("End-to-end map building from a video sequence")
    assert not quality.off_domain_title("Biocompatible bone scaffold for cell regeneration")
    assert not quality.off_domain_title("Metal-insulator-metal capacitor and integrated chip")


def test_patent_discovery_title_preserves_aec_rescues():
    assert not quality.off_domain_discovery_title("Generation of a building information model")
    assert not quality.off_domain_discovery_title("Tunnel ventilation system for smoke extraction")
    assert not quality.off_domain_discovery_title("Energy recovery ventilator for a building roof")
    assert not quality.off_domain_discovery_title("Monitoring fatigue of a steel bridge deck")
    assert not quality.off_domain_discovery_title(
        "A kind of UHPC wafer board composite beam bridge shear connector"
    )
    assert not quality.off_domain_discovery_title("A semiconductor refrigerator")
    assert not quality.off_domain_discovery_title(
        "Bridge vibration suppression under earthquake, wind, and vehicle loading"
    )
    assert not quality.off_domain_discovery_title(
        "Underground ventilation system for an automobile factory building"
    )
    assert not quality.off_domain_discovery_title(
        "Temperature control for an automobile plant energy storage station"
    )
    assert not quality.off_domain_discovery_title(
        "Insulated concrete pouring truck for external-wall construction"
    )
    assert not quality.off_domain_discovery_title(
        "Hospital water supply and drainage sewage treatment equipment"
    )
    assert not quality.off_domain_discovery_title(
        "Construction method of centralized medical liquid oxygen supply system"
    )
    assert not quality.off_domain_discovery_title(
        "BIM model for rapid construction of clustered medical buildings"
    )
    assert not quality.off_domain_discovery_title(
        "Expansion valves, thermostats and battery packs for building HVAC"
    )
    assert not quality.off_domain_discovery_title(
        "Thin film curing on a concrete substrate"
    )
    assert not quality.off_domain_discovery_title(
        "Concrete 3D printing material and printing method"
    )


SWEDISH_PLANNING = (  # Boverket-style planning/energy prose — regression for the 07-23 Nordic
    "Boverket redovisar ett ramverk för nationell planering. Kommunernas översiktsplan och "
    "detaljplan styr var bostäder och byggnader får uppföras. Rapporten beskriver "
    "energieffektivisering av befintlig bebyggelse, krav på klimatdeklaration vid nybyggnad, "
    "samt planering av solcellsparker och fjärrvärme. God inomhusmiljö kräver ventilation och "
    "isolering av klimatskalet, och fastighetsägare ansvarar för fukt och underhåll. "
)


def test_swedish_planning_ok():
    # the pre-07-23 DOMAIN (2 Swedish stems) killed this class as off-topic (domain=9 < 10)
    assert v(rep(SWEDISH_PLANNING, 10_000), book=False) == "ok"
    assert v(rep(SWEDISH_PLANNING, 150_000), book=True) == "ok"


JA_STRUCTURAL = (  # AIJ kouzou-style prose: dense numerics/DOI headers drag alpha to ~0.54 —
    # regression for the 07-23 CJK alpha-floor carve-out (clean papers were killed as garbage)
    "日本建築学会構造系論文集 第84巻 第764号，1355-1365，2019年10月 "
    "DOI http://doi.org/10.3130/aijs.84.1355 無耐火被覆CFT柱の耐火性能に関する解析的考察 "
    "実験は載荷比 0.3, 0.45, 0.6 の 3 水準、断面 400×400×12 mm、鋼材 SN490B とし、"
    "コンクリート強度 Fc=36 N/mm2 の試験体 12 体を用いた。耐震性能と耐火性能の関係を検討し、"
    "鉄骨造建築物の構造設計における許容応力度 235 N/mm2 に対する安全率を 1.5 とした。 "
)


def test_japanese_structural_numerics_ok():
    assert v(rep(JA_STRUCTURAL, 25_000), book=False) == "ok"


def test_symbol_soup_still_garbage_despite_some_cjk():
    # a mostly-symbol doc with a sprinkle of CJK must NOT ride the lower floor
    soup = ("±×÷≦≧∑∫√∂ 0.123 4.56e-7 |—|=|+|*| 建 " * 400)
    assert v(soup[:20_000] + rep(ON_TOPIC, 2_000), book=False) == "garbage"
