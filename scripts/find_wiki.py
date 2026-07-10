#!/usr/bin/env python3
"""find_wiki.py — multilingual Wikipedia (+ Wikibooks) expansion for the corpus.

registry/curated.yaml hand-picks ~100 English Wikipedia articles (`wiki-*`, source `wikipedia`) on
building-energy / controls / standards topics. The corpus accepts all languages; this backend grows
the multilingual side three ways, all CC-BY-SA and all handled by the loader's existing MediaWiki
extraction (div.mw-parser-output selector — no loader change needed):

1. langlinks (always): batch the curated EN titles through the English API (`prop=langlinks`) and
   register the SAME articles in each requested language — no new topic judgment needed, the
   English entry's topic is copied over. ids `wik-<lang>-<slug of the EN title>`.
2. --categories: walk per-language seed category trees (CATEGORY_SEEDS below — (category, topic)
   pairs, verified to exist; extend freely) via `list=categorymembers`, recursing into
   subcategories to --depth, ns-0 pages only, visited-set + per-language cap (--lang-cap).
   The topic comes from the seed category. Noisier than langlinks — always load + prune after.
3. --books: enumerate Wikibooks textbook subpages by title prefix (BOOK_SEEDS below) via
   `list=allpages&apprefix` — e.g. the zh construction-management licensing-exam textbooks.
   ids `wik-<lang>wb-...`, source `wikibooks_<lang>`.

Flagged flows run first; langlinks fills whatever --max budget remains (its already-registered
entries dedup away). CJK/Cyrillic native titles slug to nothing under registry.slug (ascii-only) —
ids fall back to a short content hash of the title (see native_id).

    python scripts/find_wiki.py                                   # propose, langlinks only
    python scripts/find_wiki.py --categories --langs zh,de --max 300
    python scripts/find_wiki.py --books --max 100 --append
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from urllib.parse import quote, unquote

import requests
import yaml

import registry

EN_API = "https://en.wikipedia.org/w/api.php"
UA = {"User-Agent": "nekaise-corpus/find_wiki (research)"}
BATCH = 50  # titles per langlinks query — well under the anonymous apihighlimits-free cap

# --categories seeds: lang -> [(category title, topic)]. Category names are the LIVE ones on that
# wiki (each verified via action=query — several obvious guesses don't exist: zh HVAC is literally
# "Category:HVAC", de heating is "Heiztechnik" not "Heizungstechnik", ja A/C is "空気調和設備").
# Extend freely; the walker warns on any seed that resolves to zero members so typos surface.
CATEGORY_SEEDS: dict[str, list[tuple[str, str]]] = {
    "zh": [("建筑", "architecture"), ("土木工程", "structures_civil"),
           ("建筑构造", "construction"), ("HVAC", "equipment_systems"),
           ("城市规划", "urban"), ("橋梁", "structures_civil"),
           ("隧道", "structures_civil"), ("建筑材料", "materials")],
    "de": [("Bauwesen", "construction"), ("Haustechnik", "equipment_systems"),
           ("Versorgungstechnik", "equipment_systems"), ("Baustoff", "materials"),
           ("Heiztechnik", "equipment_systems"), ("Stadtplanung", "urban"),
           ("Brückenbau", "structures_civil")],
    "fr": [("Construction", "construction"), ("Génie civil", "structures_civil"),
           ("Chauffage", "equipment_systems"), ("Urbanisme", "urban")],
    "ja": [("建築", "architecture"), ("土木工学", "structures_civil"),
           ("空気調和設備", "equipment_systems"), ("都市計画", "urban")],
    "es": [("Construcción", "construction"), ("Ingeniería civil", "structures_civil"),
           ("Urbanismo", "urban")],
    "ru": [("Строительство", "construction"), ("Архитектура", "architecture")],
    "ko": [("건축", "architecture"), ("토목공학", "structures_civil")],
    "it": [("Edilizia", "construction"), ("Ingegneria civile", "structures_civil")],
}

# --books seeds: wikibooks lang -> [(page-title prefix, topic)]. zh.wikibooks hosts the Chinese
# constructor licensing-exam textbooks as /-separated subpage trees under these roots.
BOOK_SEEDS: dict[str, list[tuple[str, str]]] = {
    "zh": [("机电工程管理与实务", "construction"), ("建筑工程管理与实务", "construction")],
}


def native_id(lang: str, title: str) -> str:
    """`wik-<lang>-<slug>` from the NATIVE title. registry.slug keeps only ascii alnum, so pure
    CJK/Cyrillic titles slug to (almost) nothing — fall back to a short sha1 of the title so ids
    stay non-empty and collision-free, keeping any latin fragment for readability
    (e.g. wik-zh-bim-..., wik-ja-8f3a2b91c0)."""
    s = registry.slug(title)[:40]
    if len(s) < 3:
        s = f"{s}-{hashlib.sha1(title.encode('utf-8')).hexdigest()[:10]}".strip("-")
    return f"wik-{lang}-{s}"


def page_url(host: str, title: str) -> str:
    # safe='/#': subpage slashes and section anchors stay literal (a langlink can point at a
    # section of a broader article; percent-encoding '#' would bounce through a 301).
    return f"https://{host}/wiki/{quote(title.replace(' ', '_'), safe='/#')}"


def origin_titles() -> dict[str, str]:
    """{english title (raw, underscore form) -> topic} for every curated source: wikipedia entry."""
    out: dict[str, str] = {}
    for e in registry.load_entries():
        if e.get("source") != "wikipedia":
            continue
        url = e.get("url") or ""
        if "/wiki/" not in url:
            continue
        title = unquote(url.rsplit("/wiki/", 1)[1])
        out[title] = e.get("topic", "")
    return out


def resolve_map(query: dict) -> dict[str, str]:
    """{final (post-normalize/redirect) title -> originally-requested raw title}, so a langlinks
    hit on a normalized/redirected page can still be traced back to its topic."""
    norm = {n["from"]: n["to"] for n in query.get("normalized", [])}
    redir = {r["from"]: r["to"] for r in query.get("redirects", [])}
    reverse: dict[str, str] = {}
    for raw in set(norm) | set(redir):
        t = norm.get(raw, raw)
        t = redir.get(t, t)
        reverse[t] = raw
    return reverse


def api_get(session: requests.Session, api: str, params: dict) -> dict:
    """One API call, 0.5s politeness sleep, and a Retry-After backoff on HTTP 429 — long category
    walks trip the anonymous rate limit after ~100 sustained calls, and a dropped seed loses its
    whole subtree."""
    for attempt in (1, 2, 3):
        r = session.get(api, params={"format": "json", **params}, timeout=30)
        if r.status_code == 429 and attempt < 3:
            wait = min(int(r.headers.get("Retry-After") or 30), 120)
            print(f"# 429 from {api} — backing off {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        r.raise_for_status()
        data = r.json()
        time.sleep(0.5)  # politeness between API calls
        return data
    raise RuntimeError("unreachable")


def walk_categories(session: requests.Session, lang: str, seeds: list[tuple[str, str]],
                    depth: int, budget: int) -> list[tuple[str, str]]:
    """BFS each (seed category, topic) tree on {lang}.wikipedia, recursing into subcategories
    (ns 14) to `depth` levels; returns up to `budget` [(ns-0 article title, seed topic)].
    The visited-set spans seeds, so overlapping trees aren't re-walked (first seed's topic wins);
    the generic English "Category:" prefix resolves on every wiki."""
    api = f"https://{lang}.wikipedia.org/w/api.php"
    visited: set[str] = set()
    seen_pages: set[str] = set()
    found: list[tuple[str, str]] = []
    for seed, topic in seeds:
        if len(found) >= budget:
            break
        queue: list[tuple[str, int]] = [(f"Category:{seed}", 0)]
        seed_hits = 0
        while queue and len(found) < budget:
            cat, d = queue.pop(0)
            if cat in visited:
                continue
            visited.add(cat)
            cont: dict = {}
            while True:
                try:
                    data = api_get(session, api, {
                        "action": "query", "list": "categorymembers", "cmtitle": cat,
                        "cmtype": "page|subcat", "cmlimit": 500, **cont})
                except Exception as e:
                    print(f"# categorymembers({lang}, {cat}) failed: {e}", file=sys.stderr)
                    break
                for m in data.get("query", {}).get("categorymembers", []):
                    t = m.get("title") or ""
                    if m.get("ns") == 0 and t and t not in seen_pages:
                        seen_pages.add(t)
                        found.append((t, topic))
                        seed_hits += 1
                        if len(found) >= budget:
                            break
                    elif m.get("ns") == 14 and d < depth:
                        queue.append((t, d + 1))
                cont = data.get("continue") or {}
                if not cont or len(found) >= budget:
                    break
        if not seed_hits and len(found) < budget:
            print(f"# seed Category:{seed} ({lang}) yielded 0 pages — typo or empty? "
                  f"check CATEGORY_SEEDS", file=sys.stderr)
    return found


def walk_books(session: requests.Session, lang: str, seeds: list[tuple[str, str]],
               budget: int) -> list[tuple[str, str]]:
    """Enumerate {lang}.wikibooks ns-0 pages under each title prefix (a textbook and its
    /-separated subpage tree); returns up to `budget` [(page title, seed topic)]."""
    api = f"https://{lang}.wikibooks.org/w/api.php"
    found: list[tuple[str, str]] = []
    for prefix, topic in seeds:
        if len(found) >= budget:
            break
        cont: dict = {}
        while len(found) < budget:
            try:
                data = api_get(session, api, {
                    "action": "query", "list": "allpages", "apprefix": prefix,
                    "aplimit": 500, **cont})
            except Exception as e:
                print(f"# allpages({lang}wb, {prefix}) failed: {e}", file=sys.stderr)
                break
            for p in data.get("query", {}).get("allpages", []):
                t = p.get("title") or ""
                if t:
                    found.append((t, topic))
                    if len(found) >= budget:
                        break
            cont = data.get("continue") or {}
            if not cont:
                break
    return found


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--langs", default="zh,de,fr,ja,es,ru,ko,it",
                     help="comma-separated Wikipedia language codes to expand into")
    ap.add_argument("--categories", action="store_true",
                     help="also walk each language's CATEGORY_SEEDS trees (list=categorymembers)")
    ap.add_argument("--depth", type=int, default=2,
                     help="subcategory recursion depth for --categories")
    ap.add_argument("--lang-cap", type=int, default=300,
                     help="hard cap on category-walk entries per language")
    ap.add_argument("--books", action="store_true",
                     help="also enumerate BOOK_SEEDS wikibooks textbook trees (list=allpages)")
    ap.add_argument("--max", type=int, default=800, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true", help="append into the registry (registry/wiki.yaml)")
    args = ap.parse_args()

    langs = [l.strip() for l in args.langs.split(",") if l.strip()]
    urls, titles, reg_ids = registry.existing_keys()
    session = requests.Session()
    session.headers.update(UA)

    out: list[dict] = []
    counts: dict[str, dict[str, int]] = {}  # flow -> {lang: n}

    def push(flow: str, lang: str, sid: str, native_title: str, display_title: str,
             url: str, source: str, topic: str) -> None:
        if len(out) >= args.max or url.rstrip("/") in urls or registry.norm(native_title) in titles:
            return
        urls.add(url.rstrip("/"))
        titles.add(registry.norm(native_title))
        out.append({"id": sid, "title": display_title, "url": url, "source": source,
                    "license": "cc-by-sa", "topic": topic, "format": "html"})
        by_lang = counts.setdefault(flow, {})
        by_lang[lang] = by_lang.get(lang, 0) + 1

    if args.categories:
        for lang in langs:
            seeds = CATEGORY_SEEDS.get(lang)
            if not seeds:
                print(f"# --categories: no seeds for '{lang}' — extend CATEGORY_SEEDS", file=sys.stderr)
                continue
            budget = min(args.lang_cap, args.max - len(out))
            if budget <= 0:
                break
            for title, topic in walk_categories(session, lang, seeds, args.depth, budget):
                push("categories", lang, native_id(lang, title), title,
                     f"{title} ({lang} Wikipedia)", page_url(f"{lang}.wikipedia.org", title),
                     f"wikipedia_{lang}", topic)

    if args.books:
        for lang, seeds in BOOK_SEEDS.items():  # wikibooks seeds are their own axis, not --langs
            budget = args.max - len(out)
            if budget <= 0:
                break
            for title, topic in walk_books(session, lang, seeds, budget):
                push("books", lang, native_id(f"{lang}wb", title), title,
                     f"{title} ({lang} Wikibooks)", page_url(f"{lang}.wikibooks.org", title),
                     f"wikibooks_{lang}", topic)

    # langlinks (always) — the curated EN seed set, expanded into each requested language.
    titles_topics = origin_titles()
    raw_titles = list(titles_topics)
    hit_articles: set[str] = set()
    for i in range(0, len(raw_titles), BATCH):
        if len(out) >= args.max:
            break
        batch = raw_titles[i:i + BATCH]
        try:
            data = api_get(session, EN_API, {
                "action": "query", "titles": "|".join(batch), "prop": "langlinks",
                "lllimit": 500, "redirects": 1})
        except Exception as e:
            print(f"# langlinks query failed for batch {i}: {e}", file=sys.stderr)
            continue
        query = data.get("query", {})
        reverse = resolve_map(query)
        for page in query.get("pages", {}).values():
            if "missing" in page:
                continue
            orig_title = reverse.get(page.get("title", ""), page.get("title", ""))
            topic = titles_topics.get(orig_title)
            if topic is None:
                continue  # couldn't trace this page back to a source entry — skip rather than guess
            for ll in page.get("langlinks", []):
                lang, native_title = ll.get("lang"), ll.get("*")
                if lang not in langs or not native_title:
                    continue
                n_before = len(out)
                push("langlinks", lang, f"wik-{lang}-{registry.slug(orig_title)[:46]}",
                     native_title, f"{native_title} ({lang} Wikipedia)",
                     page_url(f"{lang}.wikipedia.org", native_title),
                     f"wikipedia_{lang}", topic)
                if len(out) > n_before:
                    hit_articles.add(orig_title)

    registry.uniquify_ids(out, reg_ids)
    print(f"# {len(out)} NEW entries (langs={langs}, categories={args.categories}, "
          f"books={args.books}; deduped vs manifest + registry + blocklist)")
    if args.categories:
        print(f"# categories (depth {args.depth}, cap {args.lang_cap}/lang): "
              f"{counts.get('categories', {})}")
    if args.books:
        print(f"# books: {counts.get('books', {})}")
    print(f"# langlinks: {counts.get('langlinks', {})} from {len(hit_articles)}/"
          f"{len(raw_titles)} curated EN articles")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        appended = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {appended}", file=sys.stderr)


if __name__ == "__main__":
    main()
