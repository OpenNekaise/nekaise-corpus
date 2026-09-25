#!/usr/bin/env python3
"""find_regdocs.py — building-code, ESG-framework and BIM guidance documents (config-driven).

The compliance/ESG programme (Codex decision 2026-09-25; operator request: everything publicly
obtainable behind automated building-code compliance checking and ESG report extraction) needs
many small, heterogeneous official sources: Swedish statutes from Riksdagen's open data, the
Danish BR18 instruments on Retsinformation, the Finnish building decrees on Finlex, Boverket /
MCF / DiBK guidance web pages, EFRAG's ESRS guidance, FSB-hosted TCFD reports, UK BIM Framework
guidance, and the two companies' own pages. One finder serves them; `registry/regdocs.json`
describes each source:

  mechanism     static_list (reviewed URLs) | sitemap_pages (content pages listed by an XML
                sitemap, path-scoped) | link_pages (document links found on reviewed seed pages,
                optionally rewritten into one or more document URLs, e.g. a Finlex statute page
                -> its fi/sv PDFs on the open-data API)
  licence       license / license_url / license_evidence per source — honest tags only; tags
                outside the current registry vocabulary (proprietary, unverified, cc-by-nd, …)
                are never appended before the collect-all licence classes land
                (compliance_common.split_appendable), and such sources stay `enabled: false`
  access        `blocked: true` records a reviewed NO-GO (robots / ToU robot ban / login) for
                the audit trail; blocked sources can never run
  pacing        `delay` seconds between discovery requests (robots Crawl-delay wins when longer)

Every discovery request goes through polite_http (host policy + robots.txt + pacing + challenge
detection); the loader applies the same robots/pacing rules to the rows (build_corpus).

Rotation (dynamic cursor, JSON): {"s": source key, "o": offset into the sorted universe,
"f": universe fingerprint, "w": {source key: date its universe was last fully walked}}. One
source is walked per run from its offset; `--max` is a hard cap on proposed entries and
`--max-requests` on HTTP requests (robots.txt excluded). A finished source enters a WATCH_DAYS
watch phase and the cursor moves to the next enabled source; a changed universe restarts at 0
(dedup makes that safe). An access deferral reports HOLD; nothing here ever reports exhaustion
(sites grow).

    python scripts/find_regdocs.py --list
    python scripts/find_regdocs.py --source riksdagen-sfs --max 50        # propose one source
    python scripts/find_regdocs.py --cursor START --max 12 --max-requests 8 --append
"""
from __future__ import annotations

import argparse
import hashlib
import html as htmllib
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from xml.etree import ElementTree

import requests

import compliance_common
import dedup
import finder_protocol
import polite_http
import registry
import store

CONFIG_PATH = store.config_path("regdocs.json", registry.ROOT)
MECHANISMS = {"static_list", "sitemap_pages", "link_pages"}
FORMATS = {"pdf", "html", "txt"}
WATCH_DAYS = 7
MAX_UNIVERSE = 20_000
ANCHOR_RE = re.compile(r"""<a\s[^>]*?href\s*=\s*["']([^"'#]+)["'][^>]*>(.*?)</a>""", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
KEY_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,23}")


# ------------------------------------------------------------------------------------ config
def load_config(path: Path | None = None) -> dict[str, dict]:
    data = json.loads(Path(path or CONFIG_PATH).read_text())
    errors = validate(data)
    if errors:
        raise ValueError(f"invalid regdocs.json: " + "; ".join(errors))
    return data["sources"]


def validate(data: object) -> list[str]:
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        return ["top level must be an object with a `sources` object"]
    errors: list[str] = []
    topics = set(__import__("lint_registry").TOPICS)
    for key, cfg in data["sources"].items():
        label = f"sources.{key}"
        if not KEY_RE.fullmatch(key or ""):
            errors.append(f"{label}: key must be a short lowercase slug")
        if not isinstance(cfg, dict):
            errors.append(f"{label}: must be an object")
            continue
        for field in ("name", "mechanism", "source", "license", "license_url",
                      "license_evidence", "topic", "rights_reviewed_at"):
            if not cfg.get(field):
                errors.append(f"{label}: missing {field}")
        if cfg.get("mechanism") not in MECHANISMS:
            errors.append(f"{label}: mechanism must be one of {sorted(MECHANISMS)}")
        if cfg.get("license") not in compliance_common.KNOWN_LICENSES:
            errors.append(f"{label}: unknown licence tag {cfg.get('license')!r}")
        if cfg.get("topic") not in topics:
            errors.append(f"{label}: unknown topic {cfg.get('topic')!r}")
        if cfg.get("blocked") and cfg.get("enabled"):
            errors.append(f"{label}: a blocked source can never be enabled")
        if not cfg.get("enabled") and not cfg.get("reason"):
            errors.append(f"{label}: a disabled source needs a reason")
        if cfg.get("enabled") and cfg.get("license") not in compliance_common.CURRENT_LICENSES:
            errors.append(f"{label}: licence {cfg.get('license')!r} awaits the collect-all "
                          "licence classes; the source must stay disabled")
        if not str(cfg.get("license_url", "")).startswith(("http://", "https://")):
            errors.append(f"{label}: license_url must be http(s)")
        if cfg.get("format", "html") not in FORMATS:
            errors.append(f"{label}: format must be one of {sorted(FORMATS)}")
        try:
            float(cfg.get("delay", 1))
        except (TypeError, ValueError):
            errors.append(f"{label}: delay must be a number")
        mech = cfg.get("mechanism")
        if mech == "static_list":
            items = cfg.get("items")
            if not isinstance(items, list) or not items:
                errors.append(f"{label}: static_list needs items")
            else:
                for n, it in enumerate(items):
                    if not isinstance(it, dict) or not str(it.get("url", "")).startswith("http") \
                            or not it.get("title"):
                        errors.append(f"{label}.items[{n}]: needs url and title")
                    elif it.get("format", cfg.get("format", "html")) not in FORMATS:
                        errors.append(f"{label}.items[{n}]: bad format")
        elif mech == "sitemap_pages":
            if not cfg.get("sitemaps") or not cfg.get("include"):
                errors.append(f"{label}: sitemap_pages needs sitemaps and include")
        elif mech == "link_pages":
            if not cfg.get("seeds") or not cfg.get("link_include"):
                errors.append(f"{label}: link_pages needs seeds and link_include")
        for rx in ("include", "exclude", "link_include", "link_exclude", "label_strip"):
            if cfg.get(rx):
                try:
                    re.compile(cfg[rx])
                except re.error as exc:
                    errors.append(f"{label}.{rx}: {exc}")
        for n, rw in enumerate(cfg.get("rewrites") or []):
            if not isinstance(rw, dict) or "pattern" not in rw or "replace" not in rw:
                errors.append(f"{label}.rewrites[{n}]: needs pattern and replace")
                continue
            try:
                re.compile(rw["pattern"])
            except re.error as exc:
                errors.append(f"{label}.rewrites[{n}]: {exc}")
        if cfg.get("quality_profile") not in (None, "normative"):
            errors.append(f"{label}: quality_profile must be 'normative' when set")
        if cfg.get("quality_profile") and (not isinstance(cfg.get("hosts"), list)
                                           or not cfg.get("hosts")):
            errors.append(f"{label}: a quality_profile source must declare its hosts")
        if cfg.get("quality_profile") and cfg.get("license") not in ("public-domain", "cc-by",
                                                                     "cc0", "open"):
            errors.append(f"{label}: the normative profile is for statutory sources only")
        probe = cfg.get("version_probe")
        if probe is not None:
            if not isinstance(probe, dict) or not probe.get("pattern"):
                errors.append(f"{label}.version_probe: needs a pattern")
            else:
                for rx in ("pattern", "unamended"):
                    if probe.get(rx):
                        try:
                            re.compile(probe[rx])
                        except re.error as exc:
                            errors.append(f"{label}.version_probe.{rx}: {exc}")
        for n, rule in enumerate(cfg.get("topic_rules") or []):
            if not (isinstance(rule, list) and len(rule) == 2 and rule[1] in topics):
                errors.append(f"{label}.topic_rules[{n}]: must be [regex, topic]")
    return errors


def runnable(sources: dict[str, dict], today: date | None = None) -> list[str]:
    """Enabled, not blocked, and access terms reviewed within RIGHTS_REVIEW_DAYS (a source whose
    review is due is skipped — and reported — until someone re-reviews its terms)."""
    keys = []
    for k, c in sources.items():
        if not c.get("enabled") or c.get("blocked"):
            continue
        if compliance_common.review_due(c.get("rights_reviewed_at"), today):
            print(f"# {k}: rights review older than {compliance_common.RIGHTS_REVIEW_DAYS} days; "
                  "skipped until re-reviewed", file=sys.stderr)
            continue
        keys.append(k)
    return keys


# ------------------------------------------------------------------------------------ helpers
def doc_id(key: str, url: str) -> str:
    """Deterministic id: reg-<key>-<slug of the URL path tail>-<sha1(url)[:8]>."""
    parts = urlsplit(url)
    segs = [s for s in unquote(parts.path).split("/") if s][-3:] or [parts.netloc]
    tail = registry.slug("-".join(segs))
    digest = hashlib.sha1(url.encode()).hexdigest()[:8]
    return f"reg-{key}-{tail[-36:].strip('-') or 'doc'}-{digest}"


def text_of(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(TAG_RE.sub(" ", fragment))).strip()


def format_for(cfg: dict, url: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if re.search(r"\.pdf(?:$|[?#])|/main\.pdf|/pdf$", url, re.I):
        return "pdf"
    return cfg.get("format", "html")


def title_from_path(cfg: dict, url: str) -> str:
    path = unquote(urlsplit(url).path).strip("/")
    root = cfg.get("title_strip", "")
    if root and path.startswith(root.strip("/")):
        path = path[len(root.strip("/")):].strip("/")
    words = " / ".join(seg.replace("-", " ").replace("_", " ").strip()
                       for seg in path.split("/") if seg)
    return f"{cfg.get('title_prefix', cfg['name'])} — {words or 'start'}"


def topic_for(cfg: dict, url: str, title: str) -> str:
    hay = f"{url} {title}"
    for rx, topic in cfg.get("topic_rules") or []:
        if re.search(rx, hay, re.I):
            return topic
    return cfg["topic"]


TITLE_MAX = 180


def make_entry(key: str, cfg: dict, url: str, title: str, today: str,
               extra: dict | None = None, suffix: str = "") -> dict:
    """One candidate. `suffix` (language/version qualifier) is kept whole: the base title is
    truncated before it, so qualified variants never collapse under title dedup."""
    extra = dict(extra or {})
    fmt = format_for(cfg, url, extra.pop("format", None))
    base = re.sub(r"\s+", " ", title).strip()
    room = max(20, TITLE_MAX - len(suffix))
    if len(base) > room:  # truncated: a stable URL qualifier keeps distinct documents distinct
        tag = f" [#{hashlib.sha1(url.encode()).hexdigest()[:8]}]"
        base = base[:max(20, room - len(tag))].rstrip() + tag
    title = base[:room] + suffix
    entry = {
        "id": doc_id(key, url), "title": title, "url": url,
        "source": cfg["source"], "license": extra.pop("license", cfg["license"]),
        "topic": extra.pop("topic", None) or topic_for(cfg, url, title), "format": fmt,
        "license_url": extra.pop("license_url", cfg["license_url"]),
        "license_evidence": extra.pop("license_evidence", cfg["license_evidence"]),
        "rights_verified_at": cfg.get("rights_reviewed_at") or today,
    }
    for field in ("language", "jurisdiction", "document_type"):
        value = extra.pop(field, None) or cfg.get(field)
        if value:
            entry[field] = value
    entry.update({k: v for k, v in extra.items() if k in registry.OPTIONAL_FIELDS and v})
    return entry


# ------------------------------------------------------------------------------------ universes
def _xml_locs(data: bytes) -> tuple[str, list[str]]:
    root = ElementTree.fromstring(data)
    tag = root.tag.rsplit("}", 1)[-1]
    locs = [(el.text or "").strip() for el in root.iter()
            if el.tag.rsplit("}", 1)[-1] == "loc" and (el.text or "").strip()]
    return ("index" if tag == "sitemapindex" else "urlset"), locs


class Budget:
    def __init__(self, n: int):
        self.left = n

    def take(self, what: str) -> None:
        if self.left <= 0:
            raise polite_http.Deferred(f"request budget exhausted before {what}")
        self.left -= 1


class Incomplete(Exception):
    """A sitemap walk ran out of this run's request budget; its progress is cached."""


CACHE_TTL_DAYS = WATCH_DAYS


def _cache_path(key: str) -> Path:
    import ops
    return ops.WORKSPACE / "regdocs-cache" / f"{key}.json"


def _load_walk(key: str, cfg: dict, today: str) -> dict:
    """The resumable sitemap walk of one source (scratch: losing it only restarts the walk)."""
    try:
        walk = json.loads(_cache_path(key).read_text())
        fresh = (date.fromisoformat(today) - date.fromisoformat(walk["started"])).days
        if walk.get("sitemaps") == list(cfg["sitemaps"]) and fresh < CACHE_TTL_DAYS:
            return walk
    except (OSError, ValueError, KeyError):
        pass
    return {"started": today, "sitemaps": list(cfg["sitemaps"]),
            "pending": list(cfg["sitemaps"]), "locs": [], "children": 0}


def _save_walk(key: str, walk: dict) -> None:
    import ops
    path = _cache_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    ops.atomic_write_text(path, json.dumps(walk))


def universe(key: str, cfg: dict, budget: Budget, today: str, fetch=None) -> list[dict]:
    """Every candidate entry of one source, sorted by URL (no dedup yet)."""
    fetch = fetch or (lambda url, expect: polite_http.get(
        url, delay=float(cfg.get("delay", 1)), expect=expect).content)
    mech = cfg["mechanism"]
    out: dict[str, dict] = {}
    if mech == "static_list":
        for it in cfg["items"]:
            extra = {k: v for k, v in it.items() if k not in ("url", "title", "version_probe")}
            e = make_entry(key, cfg, it["url"], it["title"], today, extra)
            if it.get("version_probe") or cfg.get("version_probe"):
                e["_probe"] = it.get("version_probe") or cfg["version_probe"]
            out[it["url"]] = e
    elif mech == "sitemap_pages":
        include = re.compile(cfg["include"])
        exclude = re.compile(cfg["exclude"]) if cfg.get("exclude") else None
        walk = _load_walk(key, cfg, today)
        while walk["pending"]:
            sm = walk["pending"][0]
            if budget.left <= 0:
                _save_walk(key, walk)
                raise Incomplete(f"{key}: {len(walk['pending'])} sitemap(s) left")
            budget.take(sm)
            kind, locs = _xml_locs(fetch(sm, "xml"))
            walk["pending"].pop(0)
            if kind == "index":
                for child in locs:
                    if cfg.get("sitemap_filter") and not re.search(cfg["sitemap_filter"], child):
                        continue
                    walk["children"] += 1
                    if walk["children"] > 200:
                        raise ValueError(f"{key}: sitemap index too large; add sitemap_filter")
                    walk["pending"].append(child)
                continue
            walk["locs"].extend(loc for loc in locs
                                if include.search(loc) and not (exclude and exclude.search(loc)))
        _save_walk(key, walk)
        for loc in walk["locs"]:
            out[loc] = make_entry(key, cfg, loc, title_from_path(cfg, loc), today)
    elif mech == "link_pages":
        include = re.compile(cfg["link_include"])
        exclude = re.compile(cfg["link_exclude"]) if cfg.get("link_exclude") else None
        for seed in cfg["seeds"]:
            budget.take(seed)
            page = fetch(seed, "html").decode("utf-8", "replace")
            for href, inner in ANCHOR_RE.findall(page):
                url = urljoin(seed, htmllib.unescape(href.strip()))
                if not include.search(url) or (exclude and exclude.search(url)):
                    continue
                label = text_of(inner) or unquote(urlsplit(url).path.rsplit("/", 1)[-1])
                if cfg.get("label_strip"):
                    label = re.sub(cfg["label_strip"], "", label).strip()
                label = cfg.get("label_prefix", "") + label
                rewrites = cfg.get("rewrites") or []
                if not rewrites:
                    _keep_best(out, make_entry(key, cfg, url, label, today))
                for rw in rewrites:
                    m = re.search(rw["pattern"], url)
                    if not m:
                        continue
                    target = m.expand(rw["replace"])
                    extra = {k: rw[k] for k in ("language", "format", "document_type",
                                                "license", "license_url", "license_evidence")
                             if rw.get(k)}
                    suffix = m.expand(rw.get("title_suffix", ""))
                    _keep_best(out, make_entry(key, cfg, target, label, today, extra, suffix))
    if len(out) > MAX_UNIVERSE:
        raise ValueError(f"{key}: universe of {len(out)} exceeds {MAX_UNIVERSE}")
    entries = [out[u] for u in sorted(out)]
    _disambiguate(entries)
    return entries


def _disambiguate(entries: list[dict]) -> None:
    """Distinct URLs sharing one normalized title (generic anchors, identical page titles) get
    a stable URL qualifier, so title dedup never silently drops one of them."""
    groups: dict[str, list[dict]] = {}
    for e in entries:
        groups.setdefault(registry.norm(e["title"]), []).append(e)
    for group in groups.values():
        if len(group) < 2:
            continue
        for e in group:
            tag = f" [#{hashlib.sha1(e['url'].encode()).hexdigest()[:8]}]"
            e["title"] = e["title"][:TITLE_MAX - len(tag)].rstrip() + tag


def _keep_best(out: dict[str, dict], entry: dict) -> None:
    """A document linked several times keeps the most informative (longest) anchor title."""
    cur = out.get(entry["url"])
    if cur is None or len(entry["title"]) > len(cur["title"]):
        out[entry["url"]] = entry


def fingerprint(entries: list[dict]) -> str:
    """Of the WHOLE sorted universe: any change anywhere restarts the walk (dedup makes a
    restart safe; an offset into a changed list could skip unseen entries)."""
    return hashlib.sha1("\n".join(e["url"] for e in entries).encode()).hexdigest()[:12]


def versioned(key: str, cfg: dict, c: dict, budget: Budget, today: str) -> dict | None:
    """A mutable document (`version_probe` on its static item) as a DATED SNAPSHOT: the probe
    reads the head of the current text, and the entry's URL carries the version as a fragment
    (never sent to the server), its id/title the version, so a later consolidation becomes a
    new row instead of being frozen by URL dedup. None = version not established (skipped)."""
    probe = c.pop("_probe", None)
    if not probe:
        return c
    budget.take(c["url"])
    head = polite_http.get(c["url"], delay=float(cfg.get("delay", 1)), expect="text",
                           prefix=int(probe.get("bytes", 800))).content.decode("utf-8", "replace")
    m = re.search(probe["pattern"], head)
    if m:
        token = "tom-sfs-" + "-".join(m.groups())
        label = f" (t.o.m. SFS {':'.join(m.groups())})"
    elif re.search(probe.get("unamended", r"(?!x)x"), head):
        token, label = "tom-orig", " (grundförfattning, oändrad)"
    else:
        print(f"# {c['url']}: version not recognised; skipped this run", file=sys.stderr)
        return None
    url = f"{c['url']}#{token}"
    extra = {k: c[k] for k in ("language", "jurisdiction", "persistent_id") if c.get(k)}
    extra["document_type"] = "statute-consolidation"
    base = re.sub(r"\s*\[riksdagen\.se, gällande lydelse\]$", "", c["title"])
    return make_entry(key, cfg, url, base, today, extra, suffix=label)


# ------------------------------------------------------------------------------------ rotation
def parse_cursor(value: str) -> dict:
    if value in ("", "START"):
        return {"s": "", "o": 0, "f": "", "w": {}}
    data = json.loads(value)
    if not isinstance(data, dict):
        raise ValueError("cursor must be a JSON object")
    return {"s": str(data.get("s", "")), "o": int(data.get("o", 0)),
            "f": str(data.get("f", "")), "w": dict(data.get("w") or {})}


def dump_cursor(cur: dict) -> str:
    return json.dumps(cur, separators=(",", ":"), sort_keys=True)


def run(cursor: str, maxn: int, max_requests: int, sources: dict[str, dict], keys,
        report, today: date | None = None, fetch=None) -> list[dict]:
    today = today or date.today()
    iso = today.isoformat()
    cur = parse_cursor(cursor)
    order = runnable(sources, today)
    cur["w"] = {k: v for k, v in cur["w"].items() if k in order}
    if not order:
        report.next(dump_cursor(cur))
        return []
    start = order.index(cur["s"]) if cur["s"] in order else 0
    if cur["s"] not in order:
        cur.update({"s": order[0], "o": 0, "f": ""})
    # the first source (from the cursor on) that is not in its watch phase
    for step in range(len(order)):
        key = order[(start + step) % len(order)]
        seen = cur["w"].get(key)
        if seen and today - date.fromisoformat(seen) < timedelta(days=WATCH_DAYS):
            continue
        if key != cur["s"]:
            cur.update({"s": key, "o": 0, "f": ""})
        break
    else:
        report.next(dump_cursor(cur))  # every source is in its watch phase
        return []
    key, cfg = cur["s"], sources[cur["s"]]
    budget = Budget(max_requests)
    try:
        cands = universe(key, cfg, budget, iso, fetch)
    except Incomplete as exc:  # progress is in the walk cache: the next run continues it
        print(f"# {exc}", file=sys.stderr)
        report.next(dump_cursor(cur))
        return []
    except (polite_http.Deferred, polite_http.Refused, polite_http.TooLarge,
            requests.RequestException, ElementTree.ParseError, ValueError) as exc:
        report.hold(f"{key}: {exc}")
        return []
    fp = fingerprint(cands)
    offset = cur["o"] if cur["f"] == fp else 0
    keys.prefetch(urls=[c["url"] for c in cands[offset:]],
                  titles=[registry.norm(c["title"]) for c in cands[offset:]],
                  ids=[c["id"] for c in cands[offset:]])
    out: list[dict] = []
    i = offset
    while i < len(cands):
        c = cands[i]
        if c.get("_probe"):
            if len(out) >= maxn:
                break
            try:
                c = versioned(key, cfg, dict(c), budget, iso)
            except polite_http.Deferred as exc:
                if i == offset and not out:
                    report.hold(f"{key}: {exc}")
                    return []
                break  # budget spent or access deferred: resume at this item
            except (polite_http.Refused, polite_http.TooLarge, requests.RequestException) as exc:
                print(f"# {cands[i]['url']}: {exc}; skipped this run", file=sys.stderr)
                c = None
            if c is None:
                i += 1
                continue
            keys.prefetch(urls=[c["url"]], titles=[registry.norm(c["title"])], ids=[c["id"]])
        known = (c["url"].rstrip("/") in keys.urls or c["id"] in keys.ids
                 or registry.norm(c["title"]) in keys.titles)
        if not known:
            if len(out) >= maxn:
                break
            keys.urls.add(c["url"].rstrip("/"))
            keys.ids.add(c["id"])
            keys.titles.add(registry.norm(c["title"]))
            out.append(c)
        i += 1
    if i >= len(cands):
        cur["w"][key] = iso
        nxt = order[(order.index(key) + 1) % len(order)]
        cur.update({"s": nxt, "o": 0, "f": ""})
    else:
        cur.update({"o": i, "f": fp})
    report.next(dump_cursor(cur))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", default="START", help="dynamic rotation cursor (JSON)")
    ap.add_argument("--source", default="", help="walk one source by key (manual run)")
    ap.add_argument("--max", type=int, default=12, help="hard cap on proposed entries")
    ap.add_argument("--max-requests", type=int, default=8, help="HTTP request budget")
    ap.add_argument("--list", action="store_true", help="show sources and their state")
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args()

    sources = compliance_common.pinned_config("regdocs.json", validate)["sources"]
    if args.list:
        for k, c in sources.items():
            state = ("BLOCKED" if c.get("blocked") else "enabled" if c.get("enabled")
                     else "disabled")
            print(f"{k:24} {state:9} {c['mechanism']:13} {c['license']:14} {c['name']}"
                  + (f"  [{c.get('reason')}]" if c.get("reason") else ""))
        return
    if args.source:
        if args.source not in sources or sources[args.source].get("blocked"):
            raise SystemExit(f"unknown or blocked source {args.source!r}")
        # a manual walk of one source, even a disabled one (propose-only pilots); its held
        # candidates are still never appended
        sources = {args.source: {**sources[args.source], "enabled": True}}
        args.cursor = "START"
    keys = dedup.open_keys()
    report = finder_protocol.Report()
    out = run(args.cursor, args.max, args.max_requests, sources, keys, report)
    ok, held = compliance_common.split_appendable(out)
    by_source: dict[str, int] = {}
    for e in out:
        by_source[e["source"]] = by_source.get(e["source"], 0) + 1
    print(f"# {len(out)} NEW regdocs candidates ({len(ok)} appendable, {len(held)} held): "
          f"{by_source}")
    for e in out:
        print(f"#   {e['id']}  [{e['license']}] {e['title'][:90]}")
    if args.append and ok:
        counts = registry.append_entries(ok)
        print(f"# appended {len(ok)} entries: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
