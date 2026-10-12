#!/usr/bin/env python3
"""find_archive_scrape.py — Internet Archive built-environment texts via the Scraping API.

The old find_archive.py pages advancedsearch.php, which refuses results past 10,000 and so
stopped. This backend reads the cursor-based Scraping API
(https://archive.org/services/search/v1/scrape; robots.txt for `*` disallows only /control/ and
/report/) and walks TARGETS in order:

  0. collection:buildingtechnologyheritagelibrary — the Association for Preservation Technology's
     Building Technology Heritage Library: trade catalogs, house-plan books and technical
     building literature (16,764 items on 2026-10-12, 99.7% with OCR text);
  1. collection:averylibrarytradecatalogs — Columbia's Avery Library trade catalogs (3,592 items;
     all but 5 are also in target 0, dedup removes them);
  2+ the 34 subject/title queries of find_archive.py for texts dated up to 1928, now without the
     10,000-result cap and without the English-only filter.

Manufacturer literature is in scope (AGENTS.md), so the collection targets keep catalogs; the
query targets keep find_archive's title exclusion (catalog / price list / "tables of").

Persisted state is the walk's own key, not a stored service answer: every target is read with
`sorts=addeddate asc` (the service adds the identifier as the tie-break; naming it in `sorts`
breaks paging, HTTP 400 "Bad cursor" on page 2) and the rotation cursor is
`t=<target index> a=<addeddate> i=<identifier>` (or `a=START`): the full ordered key of the last
item the walk dealt with. The service's page cursor is that same key, base64 of
`[{"addeddate":"<a>.0Z"},{"identifierSorter":"<i>"}]`, so a run SEEKS: it builds the cursor from
(a, i) and the first answer starts after that key. The marker item does not have to exist any
more, and items already dealt with — however many share one timestamp — cost no page of the
run's budget. The service orders identifiers by code point ("Z" before "a"; measured
2026-10-12: 16,764 mixed-case BTHL identifiers in `identifierSorter` order are in Python string
order), so every answer is checked item by item: the full keys must increase strictly from the
persisted key on, inside a page and across pages — a repeated, tied-out-of-order or earlier key
HOLDs before anything is proposed. Every page that carries a cursor is checked against the key
of its last item (HOLD on a difference: the form this depends on changed). Every item dealt
with moves the position,
collectable or not, so uncollectable items cannot hold the walk. A run that ends without moving
it HOLDs. After
the last target the cursor becomes `watch:<YYYY-MM-DD>`; WATCH_DAYS later the walk starts again
from target 0, which finds new uploads, items added to a collection later and items that gained
OCR text (known items cost no request). The backend never reports EXHAUSTED.

Answer checks (an earlier review saw the service apply another query's result): every item of a
collection target must list that collection; every item of a query target must be a text dated
up to 1928 whose title or subject carries the query's words. A page where more than
WRONG_QUERY_SHARE of the items fail is an unexpected answer: HOLD, nothing proposed. So is an
item without a valid identifier or calendar timestamp, or one older than the request boundary or
than the item before it (across pages too). The service's `total` is never read: on 2026-10-12
`total_only=true` answered 0 without `sorts` and another query's cached total (3,592 for all 36
targets) with it, while the item pages were correct (34 query targets walked in full: 29,019
items, 7 failed the check, all carry addeddate).
Later that day the service ignored `q` ranges and cursors for a while and answered every request
with the first page of the collection; the order checks turn that into HOLD.

Full text: an item is proposed only when its scrape record lists a `DjVuTXT` derivative, and the
file name comes from https://archive.org/metadata/<identifier> (only one third of the BTHL items
use `<identifier>_djvu.txt`). Dark items, private (lending-only) text files and texts under
MIN_TEXT_BYTES (below the quality gate's thin floor) are skipped; so is an item the metadata
service reports as gone (`{}`) or dark. Any other metadata answer without a file list and a
metadata record (an `error` envelope, a partial answer) is a service failure: HOLD, nothing
proposed. The URL is the canonical
https://archive.org/download/<identifier>/<file>, fetched as format `txt` like the existing
`arc-` rows.

Identity is the full Archive identifier, never a title or a truncated slug:
  * id `arc-<slug, 43 chars>-<8 hex of sha1(identifier)>`: two identifiers never share an id, and
    a known id means this item is registered (no metadata request on a later walk);
  * a find_archive row of the item is found by its `<identifier>_djvu.txt` URL (that finder
    registered nothing else); the resolved URL is checked too (pruned-URL blocklist);
  * a collection item's registry title ends with `[<id>]`. Trade catalogs share titles
    ("Catalog no. 5"), and the round's proposal merger (run_round.merge_proposals) drops equal
    normalized titles, so the title is made unique by construction;
  * a query-target book keeps the title rule on purpose, with the volume in the title: another
    scan of the same volume and year is the same work and is dropped, another volume is kept.

Licence (collect regardless of licence; the tag picks the view), from the item's own metadata
record. EVERY value of `licenseurl`, `rights` and `possible-copyright-status` is one rights
statement, and each must be understood:
  * a canonical Creative Commons URL gives a tag — Public Domain Mark -> public-domain,
    CC0 -> cc0, BY / BY-SA -> cc-by / cc-by-sa (scripts/licenses.py), NC/ND -> the exact
    restricted tag, the retired CC "licenses/publicdomain" dedication -> public-domain;
  * a recognized public-domain statement (PD_STATEMENT: "NOT_IN_COPYRIGHT", "Public domain",
    "Copyright not renewed", ...) agrees with any tag and with the date rule;
  * any other text (a restriction, a negation, "CC BY-NC" in words, a contact line): unverified,
    whatever the other values say;
  * two different tags (CC BY in `licenseurl`, CC BY-NC in `rights`): unverified;
  * one tag: that tag;
  * no tag: public-domain only when every `date` / `year` value is a certain, real calendar
    date (YYYY, YYYY-MM-DD, an ISO timestamp or an ascending YYYY-YYYY range; not "c. 1920",
    "1920?", "[1920]", "1925-35", "192-", "1920-02-30") and the latest year named is 1928 or
    earlier (US copyright expired; the rule find_archive.py applied); else unverified.
The evidence quotes every value used.

    python scripts/find_archive_scrape.py --cursor "t=0 a=START" --max 20 --lookups 30
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import requests
import yaml

import dedup
import licenses
import registry
from find_archive import QUERIES
from finder_protocol import Report

SCRAPE = "https://archive.org/services/search/v1/scrape"
METADATA = "https://archive.org/metadata/"
# archive.org/developers/bots.html: every automated request names the tool and its version.
# Bump VERSION when the request pattern of this finder changes.
VERSION = "1.0"
UA = {"User-Agent": f"nekaise-corpus-find_archive_scrape/{VERSION} "
                    "(research corpus; robots.txt honoured)"}
DELAY = 1.0            # seconds between archive.org requests
COUNT = 500            # items per scrape page (the API minimum is 100)
MIN_TEXT_BYTES = 2000  # the quality gate drops shorter texts as thin
WATCH_DAYS = 30
WRONG_QUERY_SHARE = 0.5
MAX_YEAR = 1928        # US publications up to this year are in the public domain
SOURCE = "internet_archive"
PREFIX = "arc-"
START = "START"
STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
SORTS = "addeddate asc"  # the service adds the identifier tie-break itself (see docstring)
FIELDS = "identifier,title,date,year,licenseurl,language,addeddate,collection,format,mediatype,subject"
EXCLUDE_TITLE = re.compile(r"catalog|catalogue|price.?book|price list|tables of", re.I)

# Append-only: the cursor indexes this tuple. (kind, value, default topic)
TARGETS: tuple[tuple[str, str, str], ...] = (
    ("collection", "buildingtechnologyheritagelibrary", "materials"),
    ("collection", "averylibrarytradecatalogs", "materials"),
    *(("query", term, topic) for term, topic in QUERIES),
)
# Topic of a trade catalog from its title and subjects (first match; else the target default).
TOPIC_RULES = (
    ("equipment_systems", re.compile(
        r"heat|boiler|furnace|radiator|ventilat|\bfans?\b|air condition|refriger|plumb|"
        r"sanitary|bath|water closet|lavator|electric|lighting|lamp|fixture|elevator|pump|"
        r"stove|range|wiring|switch|thermostat", re.I)),
    ("architecture", re.compile(
        r"\bhomes?\b|\bhouses?\b|bungalow|cottage|house plans?|architect|interior|"
        r"furniture|garden|church", re.I)),
    ("structures_civil", re.compile(r"\bbridges?\b|structural|reinforc|trusses|girder", re.I)),
    ("construction", re.compile(r"\btools?\b|contractor|scaffold|construction|builders?\b",
                                re.I)),
)
# the same canonical suffixes as licenses._CC_URL (jurisdiction, /legalcode, /deed.xx)
RESTRICTED_CC = re.compile(
    r"https?://(?:www\.)?creativecommons\.org/licenses/"
    r"(by-nc-nd|by-nc-sa|by-nc|by-nd)/\d\.\d(?:/[a-z]{2}(?:-[a-z]{2})?)?"
    r"(?:/(?:legalcode(?:\.[a-z]{2})?|deed\.[a-z]{2}(?:-[a-z]{2})?)?)?/?", re.I)
LEGACY_PD = re.compile(r"https?://(?:www\.)?creativecommons\.org/licenses/publicdomain/?", re.I)
# The only rights TEXT that agrees with an open class or with the date rule; a whole value must
# match (so "Not public domain" or "Public domain in the US only" does not).
PD_STATEMENT = re.compile(
    r"not[ _-]in[ _-]copyright|(?:in the )?public domain|public domain mark(?: 1\.0)?|"
    r"(?:published )?pre-1923(?: document)?,? in (?:the )?public domain|copyright not renewed|"
    r"no known copyright(?: restrictions)?", re.I)
CURSOR = re.compile(r"^t=(\d+) a=(?:START|(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) i=(\S+))$")
WATCH = re.compile(r"^watch:(\d{4}-\d\d-\d\d)$")
YEAR = re.compile(r"(?<!\d)(1[5-9]\d\d|20\d\d)(?!\d)")
YEAR_RANGE = re.compile(r"(\d{4})\s?-\s?(\d{4})")
LANGS = {"eng": "en", "english": "en", "fre": "fr", "fra": "fr", "french": "fr", "ger": "de",
         "deu": "de", "german": "de", "spa": "es", "spanish": "es", "ita": "it",
         "italian": "it", "dut": "nl", "nld": "nl", "dutch": "nl", "por": "pt",
         "portuguese": "pt", "swe": "sv", "swedish": "sv", "dan": "da", "danish": "da",
         "nor": "no", "norwegian": "no", "rus": "ru", "russian": "ru", "jpn": "ja",
         "japanese": "ja", "chi": "zh", "zho": "zh", "chinese": "zh", "cze": "cs", "ces": "cs",
         "czech": "cs", "pol": "pl", "polish": "pl", "lat": "la", "latin": "la"}


class Unexpected(RuntimeError):
    """An answer that is not what the request asked for (error page, wrong query, bad shape)."""


def valid_stamp(stamp: str) -> bool:
    """A real calendar timestamp in the form the service returns (2010-02-30 is not one)."""
    try:
        return datetime.strptime(stamp, STAMP_FORMAT).strftime(STAMP_FORMAT) == stamp
    except ValueError:
        return False


def parse_cursor(cursor: str) -> tuple[int, str | None, str | None] | date:
    """(target index, addeddate, identifier) — the key of the last item dealt with, both None
    for START — or the date of a finished walk."""
    cursor = cursor.strip()
    if match := WATCH.match(cursor):
        return date.fromisoformat(match.group(1))  # ValueError on an impossible date
    if (match := CURSOR.match(cursor)) and (match.group(2) is None or valid_stamp(match.group(2))):
        return int(match.group(1)), match.group(2), match.group(3)
    raise ValueError("cursor must be 't=<index> a=START', 't=<index> a=<YYYY-MM-DDTHH:MM:SSZ> "
                     f"i=<identifier>' or 'watch:<YYYY-MM-DD>': {cursor!r}")


def format_cursor(t: int, since: str | None, marker: str | None) -> str:
    return f"t={t} a={START}" if since is None else f"t={t} a={since} i={marker}"


def seek_cursor(since: str, marker: str) -> str:
    """The service cursor that continues after the key (addeddate, identifier)."""
    key = [{"addeddate": since[:-1] + ".0Z"}, {"identifierSorter": marker}]
    return base64.b64encode(json.dumps(key, separators=(",", ":")).encode()).decode()


def cursor_key(service_cursor: str) -> tuple[str, str] | None:
    """(addeddate, identifier) a service cursor continues after; None if it is not that form."""
    try:
        key = json.loads(base64.b64decode(service_cursor, validate=True))
        (stamp,), (identifier,) = key[0].items(), key[1].items()
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        return None
    if (len(key) != 2 or stamp[0] != "addeddate" or identifier[0] != "identifierSorter"
            or not isinstance(stamp[1], str) or not isinstance(identifier[1], str)):
        return None
    return re.sub(r"\.0+Z$", "Z", stamp[1]), identifier[1]


def target_query(target: tuple[str, str, str]) -> str:
    kind, value, _topic = target
    if kind == "collection":
        return f"collection:{value} AND mediatype:texts"
    return (f'(subject:"{value}" OR title:"{value}") AND mediatype:texts '
            f"AND date:[1800-01-01 TO {MAX_YEAR}-12-31]")


def _get_json(url: str, params: dict | None = None) -> dict:
    response = requests.get(url, params=params, headers=UA, timeout=90)
    if response.status_code != 200:
        raise Unexpected(f"{url} answered HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise Unexpected(f"{url} answer is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise Unexpected(f"{url} answer is not a JSON object")
    return payload


def fetch_page(query: str, service_cursor: str | None) -> tuple[list[dict], str | None]:
    """One scrape page in (addeddate, identifier) order: (items, the service cursor for the
    next page or None at the end)."""
    params = {"q": query, "fields": FIELDS, "count": COUNT, "sorts": SORTS}
    if service_cursor:
        params["cursor"] = service_cursor
    payload = _get_json(SCRAPE, params)
    items = payload.get("items")
    if "error" in payload or not isinstance(items, list):
        raise Unexpected(f"scrape answer has no item list: {str(payload)[:160]}")
    if not all(isinstance(i, dict) for i in items):
        raise Unexpected("scrape answer carries a non-object item")
    next_cursor = payload.get("cursor")
    if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor):
        raise Unexpected("scrape answer carries an invalid cursor")
    return items, next_cursor


def fetch_metadata(identifier: str) -> dict | None:
    """The item's metadata answer, or None for an item the service reports as gone (`{}`) or
    dark. Anything else without a file list and a metadata record is a service failure."""
    meta = _get_json(METADATA + quote(identifier, safe=""))
    if meta == {} or meta.get("is_dark") is True:
        return None
    if ("error" in meta or not isinstance(meta.get("files"), list)
            or not isinstance(meta.get("metadata"), dict)):
        raise Unexpected(f"metadata answer for {identifier!r} is not an item record: "
                         f"{str(meta)[:160]}")
    return meta


def _values(value) -> list[str]:
    """A metadata field as a list of non-empty strings (the API returns a string or a list)."""
    raw = value if isinstance(value, (list, tuple)) else [value]
    return [text for v in raw if v is not None and (text := " ".join(str(v).split()))]


def _first(value) -> str:
    return (_values(value) or [""])[0]


def item_id(identifier: str) -> str:
    """The registry id of an Archive item: readable slug plus a hash of the FULL identifier, so
    identifiers that share a truncated or case/punctuation-folded slug never share an id."""
    digest = hashlib.sha1(identifier.encode()).hexdigest()[:8]
    return f"{PREFIX}{registry.slug(identifier)[:43].strip('-')}-{digest}"


def legacy_url(identifier: str) -> str:
    """The only URL form find_archive.py registered for an item."""
    return f"https://archive.org/download/{identifier}/{identifier}_djvu.txt"


def date_values(record: dict) -> list[str]:
    return [*_values(record.get("date")), *_values(record.get("year"))]


def item_year(item: dict) -> int | None:
    """The latest year an item's `year` and `date` values name, certain or not: for the answer
    check and for display, never for rights."""
    years = [int(y) for raw in date_values(item) for y in YEAR.findall(raw)]
    return max(years) if years else None


def _certain_years(value: str) -> list[int] | None:
    """The year(s) ONE date value states without doubt, or None ("c. 1920", "1920?", "[1920]",
    "1925-35", "1911-12", "192-", "n.d.", "1920-02-30", a descending range)."""
    if re.fullmatch(r"\d{4}", value):
        return [int(value)]
    if match := YEAR_RANGE.fullmatch(value):
        first, last = int(match.group(1)), int(match.group(2))
        return [first, last] if first <= last else None
    if re.fullmatch(r"\d{4}-\d\d-\d\d", value):
        try:
            return [date.fromisoformat(value).year]
        except ValueError:
            return None
    return [int(value[:4])] if valid_stamp(value) else None


def certain_year(record: dict) -> int | None:
    """The latest publication year, only when EVERY `date` / `year` value is a certain, real
    calendar date; None when there is no value or any value is doubtful."""
    years: list[int] = []
    for value in date_values(record):
        stated = _certain_years(value)
        if stated is None:
            return None
        years += stated
    if not years or min(years) < 1500 or max(years) > date.today().year:
        return None
    return max(years)


def _stems(term: str) -> list[str]:
    return [w[:5] for w in re.findall(r"[a-z]+", term.lower()) if len(w) > 3]


def applies(item: dict, target: tuple[str, str, str]) -> bool:
    """Whether a scrape item really answers the target's query (wrong-query guard)."""
    kind, value, _topic = target
    if _first(item.get("mediatype")) != "texts":
        return False
    if kind == "collection":
        return value in _values(item.get("collection"))
    year = item_year(item)
    text = " ".join(_values(item.get("title")) + _values(item.get("subject"))).lower()
    return year is not None and year <= MAX_YEAR and all(s in text for s in _stems(value))


def _licence_tag(url: str) -> tuple[str, str] | None:
    """(tag, licence URL) of ONE canonical Creative Commons URL, else None."""
    tag, canonical = licenses.cc_license([url])
    if tag:
        return tag, canonical
    if match := RESTRICTED_CC.fullmatch(url):
        return f"cc-{match.group(1).lower()}", url
    if LEGACY_PD.fullmatch(url):
        return "public-domain", url
    return None


def licence(record: dict) -> tuple[str, str, str | None]:
    """(licence tag, evidence, licence URL) from the item's metadata record. Every value of
    `licenseurl`, `rights` and `possible-copyright-status` is one rights statement: a canonical
    Creative Commons URL (a tag), a recognized public-domain statement, or not understood."""
    urls = _values(record.get("licenseurl"))
    rights = [*_values(record.get("rights")), *_values(record.get("possible-copyright-status"))]
    dates = date_values(record)
    quoted = "Internet Archive item metadata"
    # the complete decision inputs: every value read, never shortened
    if urls:
        quoted += f" licenseurl {urls!r}"
    if rights:
        quoted += f" rights {rights!r}"
    quoted += f" date/year {dates!r}" if dates else " without a date"
    tags: dict[str, str] = {}
    for n, value in enumerate((*urls, *rights)):
        if found := _licence_tag(value):
            tags.setdefault(*found)
        elif n < len(urls):
            return ("unverified", f"{quoted}: licenseurl is not a canonical Creative Commons "
                    "URL", None)
        elif not PD_STATEMENT.fullmatch(value.rstrip(". ")):
            return ("unverified", f"{quoted}: a rights statement is not a recognized licence or "
                    "public-domain statement", None)
    if len(tags) > 1:
        return "unverified", f"{quoted}: the licence values conflict", None
    if tags:
        (tag, url), = tags.items()
        return tag, quoted, url
    year = certain_year(record)
    if year is not None and year <= MAX_YEAR:
        return ("public-domain", f"{quoted}: published in {year}, US copyright expired "
                f"(publications up to {MAX_YEAR}); the item states no licence", None)
    if year is None and dates:
        return "unverified", f"{quoted}: no licence and no certain publication date", None
    return "unverified", f"{quoted}: the item states no licence", None


def text_file(meta: dict) -> str | None:
    """Name of the item's public OCR text file, or None (private, absent, too small)."""
    for entry in meta["files"]:
        if not isinstance(entry, dict) or entry.get("format") != "DjVuTXT":
            continue
        name = entry.get("name")
        if (not isinstance(name, str) or not name or "/" in name
                or str(entry.get("private", "")).lower() == "true"):
            continue
        try:
            size = int(entry.get("size"))
        except (TypeError, ValueError):
            continue
        if size >= MIN_TEXT_BYTES:
            return name
    return None


def topic_of(text: str, default: str) -> str:
    return next((topic for topic, rule in TOPIC_RULES if rule.search(text)), default)


def entry_for(item: dict, meta: dict | None, target: tuple[str, str, str],
              today: str) -> dict | None:
    """Registry entry from a scrape item and its metadata answer (fetch_metadata), or None when
    not collectable."""
    identifier = item["identifier"]
    if meta is None or (name := text_file(meta)) is None:
        return None
    record = meta["metadata"]
    if _first(record.get("identifier")) != identifier:
        raise Unexpected(f"metadata answer for {identifier!r} names another item")
    title = _first(record.get("title")) or _first(item.get("title"))
    if not title:
        return None
    sid = item_id(identifier)
    year = item_year(record) or item_year(item)
    tag, evidence, licence_url = licence(record)
    kind, _value, default_topic = target
    if kind == "collection":
        # unique by construction: trade catalogs share titles, the round merger drops equal ones
        shown = f"{title[:90]} ({year}) [{sid}]" if year else f"{title[:97]} [{sid}]"
    else:
        volume = _first(record.get("volume"))
        shown = title[:120] + (f", v. {volume[:16]}" if volume else "") + (
            f" ({year})" if year else "")
    subjects = " ".join(_values(record.get("subject")))
    entry = {
        "id": sid, "title": shown,
        "url": f"https://archive.org/download/{quote(identifier, safe='')}/{quote(name)}",
        "source": SOURCE, "license": tag,
        "topic": (topic_of(f"{title} {subjects}", default_topic) if kind == "collection"
                  else default_topic),
        "format": "txt", "license_evidence": evidence, "rights_verified_at": today,
    }
    if licence_url:
        entry["license_url"] = licence_url
    if kind == "collection":
        entry["document_type"] = "trade-literature"
    if code := LANGS.get(_first(record.get("language")).lower()):
        entry["language"] = code
    if year:
        entry["published_at"] = str(year)
    return entry


def checked(items: list[dict], t: int, key: tuple[str, str] | None,
            service_cursor: str | None) -> tuple[str, str] | None:
    """Validate a page; return the key (addeddate, identifier) of its last item. `key` is the
    persisted key or the key of the previous page's last item: every item's key must be
    strictly greater than the key before it, in the service's order (timestamp, then identifier
    by code point). A page's cursor must be the key of its last item (the form seek_cursor
    builds)."""
    for item in items:
        identifier, stamp = item.get("identifier"), _first(item.get("addeddate"))
        if not isinstance(identifier, str) or not identifier or identifier.split() != [identifier]:
            raise Unexpected(f"target {t}: an item has no valid identifier")
        if not valid_stamp(stamp):
            raise Unexpected(f"target {t}: {identifier!r} has no valid addeddate")
        if key is not None and (stamp, identifier) <= key:
            raise Unexpected(f"target {t}: key ({stamp}, {identifier!r}) does not follow "
                             f"({key[0]}, {key[1]!r}); the answer is not in (addeddate, "
                             "identifier) order after the requested key")
        key = (stamp, identifier)
    if service_cursor is not None and (not items or cursor_key(service_cursor) != key):
        raise Unexpected(f"target {t}: the page cursor is not the key of the page's last item; "
                         "the cursor form this walk seeks with changed")
    return key


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", default=f"t=0 a={START}",
                    help="'t=<target index> a=<addeddate> i=<identifier>', 't=<index> a=START' "
                         "or 'watch:<YYYY-MM-DD>'")
    ap.add_argument("--pages", type=int, default=4, help="max scrape pages this run")
    ap.add_argument("--lookups", type=int, default=100, help="max metadata requests this run")
    ap.add_argument("--max", type=int, default=60, help="max new entries this run")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/archive.yaml)")
    args = ap.parse_args()
    if min(args.pages, args.lookups, args.max) < 1:
        ap.error("--pages, --lookups and --max must be positive")
    try:
        parsed = parse_cursor(args.cursor)
    except ValueError as exc:
        ap.error(str(exc))
    report = Report()
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    if isinstance(parsed, date):
        if now.date() < parsed + timedelta(days=WATCH_DAYS):
            print(f"# 0 NEW Internet Archive texts (walk finished {parsed}; next walk "
                  f"{parsed + timedelta(days=WATCH_DAYS)})")
            report.next(args.cursor.strip())
            return
        t, since, marker = 0, None, None
    else:
        t, since, marker = parsed
    if t > len(TARGETS):
        ap.error(f"cursor target index {t} is past the {len(TARGETS)} targets")

    keys = dedup.open_keys()
    out: list[dict] = []
    seen: set[str] = set()  # identifiers this run looked up (an item can answer two targets)
    pages = lookups = scanned = requests_made = 0
    budget_hit = False

    def pause() -> None:
        nonlocal requests_made
        if requests_made:
            time.sleep(DELAY)
        requests_made += 1

    try:
        while t < len(TARGETS) and not budget_hit and pages < args.pages:
            target, query = TARGETS[t], target_query(TARGETS[t])
            # seek: the first page starts after the persisted key (addeddate, identifier)
            service_cursor = seek_cursor(since, marker) if since else None
            key, finished = ((since, marker) if since else None), False
            position = (since, marker)
            while not budget_hit and pages < args.pages:
                pause()
                items, service_cursor = fetch_page(query, service_cursor)
                pages += 1
                scanned += len(items)
                key = checked(items, t, key, service_cursor)
                wrong = {id(i) for i in items if not applies(i, target)}
                if len(wrong) > WRONG_QUERY_SHARE * len(items):
                    raise Unexpected(f"{len(wrong)}/{len(items)} items do not answer target "
                                     f"{t} ({target[1]!r})")
                keys.prefetch(ids=[item_id(i["identifier"]) for i in items],
                              urls=[legacy_url(i["identifier"]) for i in items])
                for item in items:
                    identifier, stamp = item["identifier"], _first(item.get("addeddate"))
                    title = _first(item.get("title"))
                    if (id(item) in wrong or "DjVuTXT" not in _values(item.get("format"))
                            or not title or identifier in seen
                            or item_id(identifier) in keys.ids
                            or legacy_url(identifier) in keys.urls
                            or (target[0] == "query" and EXCLUDE_TITLE.search(title))):
                        position = (stamp, identifier)
                        continue
                    if lookups >= args.lookups or len(out) >= args.max:
                        budget_hit = True
                        break
                    pause()
                    lookups += 1
                    seen.add(identifier)
                    entry = entry_for(item, fetch_metadata(identifier), target, today)
                    position = (stamp, identifier)
                    if entry is None or entry["url"].rstrip("/") in keys.urls:
                        continue
                    shown = registry.norm(entry["title"])
                    if target[0] == "query":
                        if shown in keys.titles:  # another scan of the same volume and year
                            continue
                        keys.titles.add(shown)
                    keys.urls.add(entry["url"].rstrip("/"))
                    out.append(entry)
                if not budget_hit and service_cursor is None:
                    finished = True
                    break
            if finished:
                t, since, marker = t + 1, None, None
            elif position == (since, marker) and not budget_hit:
                raise Unexpected(f"target {t}: {pages} scrape pages did not pass "
                                 f"{format_cursor(t, since, marker)!r}; the walk cannot advance")
            else:
                since, marker = position
    except Unexpected as exc:
        report.hold(f"{exc}; nothing proposed, retrying next round")
        print("# 0 NEW Internet Archive texts (unexpected answer)")
        return
    except Exception as exc:
        print(f"# ERROR: Internet Archive request failed at {format_cursor(t, since, marker)}: "
              f"{exc}; refusing a partial append so rotation does not advance", file=sys.stderr)
        raise SystemExit(1)

    keys.uniquify_ids(out)
    by_licence: dict[str, int] = {}
    for entry in out:
        by_licence[entry["license"]] = by_licence.get(entry["license"], 0) + 1
    print(f"# {len(out)} NEW Internet Archive texts (scanned {scanned} items over {pages} scrape "
          f"pages, {lookups} metadata lookups; deduped vs manifest + registry + blocklist)")
    print(f"# by licence: {by_licence}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))
    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    if t >= len(TARGETS):
        report.next(f"watch:{today}")
    else:
        report.next(format_cursor(t, since, marker))


if __name__ == "__main__":
    main()
