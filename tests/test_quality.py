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

# German academic prose — the non-English class (few English stopwords).
GERMAN = (
    "Die Untersuchung beschreibt das Verfahren zur Bestimmung der Kennwerte und erläutert die "
    "wesentlichen Ergebnisse der Messreihe. Anschließend werden die Abweichungen zwischen den "
    "berechneten und gemessenen Werten diskutiert sowie mögliche Ursachen benannt. Der zweite "
    "Abschnitt behandelt die Auswertung der Stichprobe und die statistische Unsicherheit. "
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


def test_german_is_non_english():
    assert v(rep(GERMAN, 10_000), book=False) == "non-english"


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
