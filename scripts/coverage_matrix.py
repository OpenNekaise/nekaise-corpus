#!/usr/bin/env python3
"""coverage_matrix.py — multi-dimensional coverage radar: what does the corpus LACK?

The registry `topic` is a single routing label assigned at discovery — good for sharding,
too coarse to answer "which parts of the built environment are still uncovered?". This script
derives a MULTI-LABEL facet vector per document at analysis time (nothing is stored, no
migration): domain × lifecycle stage × data type × region × language × building type × license,
classified from the title + the first few KB of extracted text with multilingual keyword rules.
Every rule set is a curated list below — extend a dimension by adding one line, then re-run.

Marginals are printed per dimension plus the two decision-driving cross sections
(domain × lifecycle, domain × region), with under-covered cells flagged. The output is the
targeting radar for the growth loop: a flagged gap = the next vein/query to dig.

    python scripts/coverage_matrix.py                 # full report (~2 min: reads text heads)
    python scripts/coverage_matrix.py --sample 5000   # quick pass on a random subset
    python scripts/coverage_matrix.py --json out.json # also dump aggregate counts as JSON
"""
from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

import registry

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
HEAD_CHARS = 4000  # how much extracted text joins the title for classification

# ---- facet rules: dimension -> [(key, regex)] — multi-label, first-match does NOT win ----
def rx(p):  # compact compile
    return re.compile(p, re.I)

DOMAIN = [
    ("hvac_energy",     rx(r"hvac|heat pump|ventilat|air.?condition|chiller|boiler|refriger|"
                           r"energy efficien|thermal comfort|insulat|energy performance|"
                           r"节能|暖通|空调|供暖|采暖|保温|断熱|省エネ|heizung|dämmung|wärme|isolation therm")),
    ("architecture",    rx(r"architect|building design|floor plan|spatial|facade|interior|"
                           r"accessib|建筑设计|意匠|entwurf")),
    ("structural",      rx(r"structural|load.?bearing|seismic|beam|column|truss|girder|"
                           r"reinforced concrete|steel structure|结构|抗震|耐震|tragwerk|charpente")),
    ("water",           rx(r"water supply|wastewater|sewer|drainage|plumbing|storm.?water|"
                           r"potable|给排水|排水|下水道|assainissement")),
    ("transport_infra", rx(r"highway|roadway|railway|railroad|bridge|tunnel|pavement|asphalt|"
                           r"traffic|transit|桥梁|隧道|道路|铁路|公路|straße|pont")),
    ("urban",           rx(r"urban|city plan|zoning|land use|master.?plan|neighborhood|"
                           r"public space|城市规划|市政|都市計画|städtebau|urbanisme")),
    ("fire_safety",     rx(r"\bfire\b|sprinkler|smoke|egress|flammab|combusti|防火|消防|"
                           r"brandschutz|incendie")),
    ("geotech",         rx(r"geotechnic|foundation|\bsoil\b|\bpile\b|excavat|retaining|"
                           r"地基|岩土|基礎|geotechnik")),
    ("materials",       rx(r"cement|concrete|aggregate|timber|masonry|coating|admixture|"
                           r"durability|corrosion|建材|混凝土|水泥|baustoff|matériau")),
    ("lighting_elec",   rx(r"lighting|luminair|daylight|electrical system|wiring|照明|电气|"
                           r"beleuchtung|éclairage")),
    ("controls_bas",    rx(r"bacnet|building automation|\bbas\b|thermostat|setpoint|sensor|"
                           r"fault detection|smart building|楼宇自控|控制系统")),
    ("constr_mgmt",     rx(r"construction management|project delivery|cost estimat|scheduling|"
                           r"procurement|construction safety|施工管理|造价|工程管理|bauleitung")),
]
LIFECYCLE = [
    ("planning",      rx(r"\bplanning\b|feasibility|siting|pre.?design|programming phase|规划|企画")),
    ("design",        rx(r"\bdesign\b|specification|detailing|设计|設計|entwurf|conception")),
    ("construction",  rx(r"construction|erection|formwork|installation|prefabricat|施工|工法|bauausführung")),
    ("commissioning", rx(r"commissioning|functional test|startup|acceptance test|调试|试运行|inbetriebnahme")),
    ("operation",     rx(r"operation|maintenance|facility management|\bo&m\b|energy management|"
                         r"fault detection|运行|维护|运维|betrieb|exploitation")),
    ("retrofit",      rx(r"retrofit|renovation|refurbish|rehabilitat|modernis|upgrade|改造|翻新|sanierung")),
    ("demolition",    rx(r"demolition|deconstruction|end.of.life|recycl|circular econom|拆除|废弃|rückbau")),
]
DATATYPE = [
    ("code_standard", rx(r"(building|energy|fire|plumbing|electrical) code|\bstandard\b|regulation|"
                         r"ordinance|directive|approved document|规范|标准|条例|norm\b|richtlinie")),
    ("manual_guide",  rx(r"manual|guidebook|handbook|guideline|guidance|how.to|best practice|"
                         r"手册|指南|指引|leitfaden|guide\b")),
    ("dataset_ts",    rx(r"dataset|time.?series|measured data|monitoring data|smart meter|"
                         r"benchmark(ing)? data|数据集|实测数据")),
    ("case_study",    rx(r"case stud|demonstration|pilot project|lessons learned|案例|示范工程")),
    ("drawing_bim",   rx(r"\bbim\b|\bifc\b|\bcad\b|drawing|blueprint|图纸|図面")),
    ("research",      rx(r"study|analysis|assessment|evaluation|experiment|simulation|modeling|"
                         r"研究|分析|untersuchung|étude")),
]
BLDGTYPE = [
    ("residential", rx(r"residential|dwelling|housing|home[s ]|apartment|multifamily|住宅|居住|wohn")),
    ("office",      rx(r"\boffice\b|commercial building|办公|事務所|büro")),
    ("hospital",    rx(r"hospital|healthcare|clinic|医院|病院|krankenhaus")),
    ("school",      rx(r"school|education building|classroom|campus|学校|校园|schule")),
    ("industrial",  rx(r"industrial|factory|manufacturing plant|warehouse|工厂|仓库|industrie")),
    ("datacenter",  rx(r"data cent(er|re)|server room|数据中心|机房")),
    ("hotel_retail", rx(r"hotel|retail|shopping|supermarket|restaurant|酒店|商场|商店")),
]
# region: primarily by source; title cues can ADD (e.g. a World Bank report about China)
REGION_SOURCE = {
    "osti": "US", "nist_crossref": "US", "gov_cec": "US", "gov_nyserda": "US", "nrel": "US",
    "google_patents": None,  # split by pat- country prefix below
    "gov_uk": "UK", "jrc": "EU", "openaire": "EU", "kitopen": "EU", "sdz_hdz": "EU",
    "ademe": "EU", "bri_japan": "JP", "nilim_japan": "JP", "worldbank": "Global",
    "iea": "Global", "oapen": "Global", "zenodo": "Global", "internet_archive": "US/UK-hist",
}
REGION_TITLE = [
    ("CN", rx(r"\bchina\b|chinese|中国")), ("JP", rx(r"\bjapan\b|日本")),
    ("EU", rx(r"\beu\b|european|europe\b")), ("US", rx(r"united states|\bu\.s\.")),
    ("UK", rx(r"united kingdom|\buk\b|england|scotland|wales")),
    ("Nordic", rx(r"sweden|swedish|norway|denmark|danish|finland|finnish|nordic|iceland")),
]
CJK_KANA = rx(r"[぀-ヿ]")
HANGUL = rx(r"[가-힯]")
CYRILLIC = rx(r"[а-яА-Я]")
CJK_ANY = re.compile(r"[一-鿿]")
STOPWORDS = {  # latin-script language votes over the text head
    "de": rx(r"\b(der|die|das|und|für|nicht|werden|eine)\b"),
    "fr": rx(r"\b(le|la|les|des|une|dans|pour|avec|est)\b"),
    "es": rx(r"\b(el|los|las|una|para|con|por|del)\b"),
    "it": rx(r"\b(il|gli|delle|una|per|con|del|che)\b"),
    "pt": rx(r"\b(os|uma|para|com|não|dos|das)\b"),
    "nl": rx(r"\b(het|een|voor|niet|aan|bij|zijn)\b"),
    "sv": rx(r"\b(och|att|för|inte|som|med|den|det)\b"),
    "en": rx(r"\b(the|and|of|for|with|that|this)\b"),
}


def detect_lang(text: str) -> str:
    if HANGUL.search(text):
        return "ko"
    if CJK_KANA.search(text):
        return "ja"
    if len(CJK_ANY.findall(text[:2000])) > 20:
        return "zh"
    if len(CYRILLIC.findall(text[:2000])) > 20:
        return "ru"
    votes = {k: len(p.findall(text[:3000])) for k, p in STOPWORDS.items()}
    best = max(votes, key=votes.get)
    # en is the prior: another language must clearly outvote it
    return best if best != "en" and votes[best] > votes["en"] * 1.2 else "en"


def facet(rules, text: str) -> list[str]:
    return [k for k, p in rules if p.search(text)] or ["(none)"]


def region_of(r: dict, text: str) -> list[str]:
    out = set()
    src = REGION_SOURCE.get(r.get("source", ""))
    if src:
        out.add(src)
    if r["id"].startswith("pat-"):
        m = re.match(r"pat-([a-z]{2})", r["id"])
        if m:
            out.add({"us": "US", "cn": "CN", "ep": "EU", "de": "EU"}.get(m.group(1), "Global"))
    for key, p in REGION_TITLE:
        if p.search(text[:600]):
            out.add(key)
    return sorted(out) or ["(unmapped)"]


def head_of(r: dict) -> str:
    tp = r.get("text_path")
    if not tp:
        return ""
    try:
        t = (HERE / tp).open(encoding="utf-8", errors="ignore").read(HEAD_CHARS + 400)
    except OSError:
        return ""
    return t.split("\n---\n\n", 1)[-1][:HEAD_CHARS]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="classify a random subset only")
    ap.add_argument("--json", default="", help="dump aggregate counts to this file")
    args = ap.parse_args()

    rows = [r for r in registry.load_manifest_rows() if r.get("status") == "ok"]
    if args.sample:
        rows = random.Random(7).sample(rows, min(args.sample, len(rows)))

    dims = {d: Counter() for d in
            ("domain", "lifecycle", "datatype", "bldgtype", "region", "language", "license")}
    tok = {d: defaultdict(int) for d in dims}
    cross_dl = defaultdict(Counter)  # domain -> lifecycle
    cross_dr = defaultdict(Counter)  # domain -> region
    total_tok = 0

    for r in rows:
        text = (r.get("title") or "") + "\n" + head_of(r)
        t = r.get("text_chars", 0) // 4
        total_tok += t
        f = {
            "domain": facet(DOMAIN, text), "lifecycle": facet(LIFECYCLE, text),
            "datatype": facet(DATATYPE, text), "bldgtype": facet(BLDGTYPE, text),
            "region": region_of(r, text), "language": [detect_lang(text)],
            "license": [r.get("license", "unknown")],
        }
        for d, keys in f.items():
            for k in keys:
                dims[d][k] += 1
                tok[d][k] += t
        for dom in f["domain"]:
            for lc in f["lifecycle"]:
                cross_dl[dom][lc] += 1
            for rg in f["region"]:
                cross_dr[dom][rg] += 1

    n = len(rows)
    print(f"coverage matrix — {n:,} ok docs / {total_tok/1e6:,.0f}M tokens "
          f"(facets are multi-label; a doc counts in every matching cell)\n")
    GAP = 0.01  # <1% of docs in a dimension key = flagged
    for d, c in dims.items():
        print(f"── {d}")
        for k, v in c.most_common():
            flag = "  ▲ GAP" if v < n * GAP and k != "(none)" else ""
            print(f"  {k:16s} {v:7,}  {100*v/n:5.1f}%   {tok[d][k]/1e6:7,.0f}M{flag}")
        print()

    def matrix(name, cross, cols):
        print(f"── {name} (docs; '·' = empty, cells <0.3% flagged ▲)")
        col_keys = [k for k, _ in cols]
        print(" " * 17 + "".join(f"{k[:12]:>13s}" for k in col_keys))
        for dom, _ in dims["domain"].most_common():
            if dom == "(none)":
                continue
            cells = []
            for k in col_keys:
                v = cross[dom][k]
                cells.append("            ·" if not v else
                             f"{v:12,}▲" if v < n * 0.003 else f"{v:13,}")
            print(f"  {dom:15s}" + "".join(cells))
        print()

    matrix("domain × lifecycle", cross_dl, LIFECYCLE)
    matrix("domain × region", cross_dr,
           [(k, None) for k in ("US", "CN", "EU", "UK", "JP", "Global", "Nordic")])

    if args.json:
        Path(args.json).write_text(json.dumps(
            {d: dict(c) for d, c in dims.items()} |
            {"cross_domain_lifecycle": {k: dict(v) for k, v in cross_dl.items()},
             "cross_domain_region": {k: dict(v) for k, v in cross_dr.items()},
             "docs": n, "tokens_M": round(total_tok / 1e6)}, ensure_ascii=False, indent=1))
        print(f"aggregates -> {args.json}")


if __name__ == "__main__":
    main()
