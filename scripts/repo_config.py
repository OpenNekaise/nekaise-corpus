#!/usr/bin/env python3
"""repo_config.py — registry/repositories.json: schema, loader and loader-host settings.

The reviewed configuration of the university / institute repository vein (scripts/find_repo.py).
It lives outside the finder so that the contracts gate (scripts/check_contracts.py) and the
loader (scripts/build_corpus.py) can validate and read it without importing a finder.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import registry
import store

CONFIG_PATH = store.config_path("repositories.json", registry.ROOT)
MIN_DELAY = 2.0        # seconds between requests when robots.txt states no Crawl-delay
RUN_SECONDS = 270.0    # loader budget per host and run (HOST_RUN_CAP = RUN_SECONDS / delay)
PLATFORMS = ("dspace-xoai",)
DECISIONS = ("go", "rejected", "later")
# languages a repository may declare: English plus the languages find_repo.AEC_LANG has terms for
LANGUAGES = ("en", "de", "fr", "es", "ca", "pt", "it", "nl", "sv", "no", "da", "fi")
KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def validate(data: object) -> list[str]:
    """Schema errors of registry/repositories.json (the contracts gate calls this too)."""
    if not isinstance(data, dict):
        return ["top level must be an object"]
    repos = data.get("repositories")
    if not isinstance(repos, dict):
        return ["repositories must be an object keyed by repository key"]
    errors: list[str] = []
    sources: dict[str, str] = {}
    for key, cfg in repos.items():
        label = f"repositories.{key}"
        if not isinstance(key, str) or not KEY.fullmatch(key):
            errors.append(f"{label}: key must be a short lowercase slug")
        if not isinstance(cfg, dict):
            errors.append(f"{label}: must be an object")
            continue
        for field in ("name", "country", "source", "platform", "oai", "topic", "rights"):
            if not cfg.get(field):
                errors.append(f"{label}: missing {field}")
        if cfg.get("platform") not in PLATFORMS:
            errors.append(f"{label}: platform must be one of {PLATFORMS}")
        source = cfg.get("source")
        if not isinstance(source, str) or not re.fullmatch(r"repo_[a-z0-9_]{2,24}", source):
            errors.append(f"{label}: source must look like repo_<name>")
        elif sources.setdefault(source, key) != key:
            errors.append(f"{label}: source {source} is also used by {sources[source]}")
        oai = urlsplit(str(cfg.get("oai") or ""))
        if oai.scheme != "https" or not oai.netloc or oai.query:
            errors.append(f"{label}: oai must be an https URL without a query")
        hosts = cfg.get("hosts")
        if (not isinstance(hosts, list) or not hosts
                or not all(isinstance(h, str) and re.fullmatch(r"[a-z0-9.-]+", h) for h in hosts)):
            errors.append(f"{label}: hosts must list the full-text host names")
            hosts = []
        try:
            pattern = re.compile(str(cfg.get("fulltext_pattern") or ""))
        except re.error as exc:
            errors.append(f"{label}: fulltext_pattern: {exc}")
        else:
            plain = pattern.pattern.replace("\\", "")
            if not any(plain.startswith(f"^https://{h}/") for h in hosts):
                errors.append(f"{label}: fulltext_pattern must start with ^https://<a listed "
                              "host>/ (escaped)")
        sets = cfg.get("sets")
        if (not isinstance(sets, list) or not sets
                or not all(isinstance(s, str) and re.fullmatch(r"[\w.:-]*", s) for s in sets)
                or len(set(sets)) != len(sets)):
            errors.append(f"{label}: sets must be a non-empty list of distinct setSpecs "
                          "('' = the whole repository)")
        delay = cfg.get("crawl_delay")
        if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay < MIN_DELAY:
            errors.append(f"{label}: crawl_delay must be a number >= {MIN_DELAY}")
        langs = cfg.get("languages")
        if (not isinstance(langs, list) or not langs
                or not all(lang in LANGUAGES for lang in langs)):
            errors.append(f"{label}: languages must be a non-empty list from "
                          f"{list(LANGUAGES)}")
        if cfg.get("relevance", "gate") not in ("gate", "all"):
            errors.append(f"{label}: relevance must be 'gate' or 'all'")
        all_sets = cfg.get("all_sets", [])
        if not isinstance(all_sets, list) or not set(all_sets) <= set(
                sets if isinstance(sets, list) else []):
            errors.append(f"{label}: all_sets must be a list of setSpecs from sets")
        patterns = {field: cfg[field] for field in ("types", "skip_types")
                    if field in cfg and cfg[field] != "*"}
        relevant_fields = cfg.get("relevant_fields", {})
        if not isinstance(relevant_fields, dict):
            errors.append(f"{label}: relevant_fields must map a field path to a pattern")
        else:
            patterns.update({f"relevant_fields.{k}": v for k, v in relevant_fields.items()})
        for field, pattern_ in patterns.items():
            try:
                if not isinstance(pattern_, str) or not pattern_:
                    raise re.error("must be a non-empty pattern")
                re.compile(pattern_)
            except re.error as exc:
                errors.append(f"{label}: {field}: {exc}")
        ignore = cfg.get("ignore_fields", [])
        if not isinstance(ignore, list) or not all(isinstance(f, str) and f for f in ignore):
            errors.append(f"{label}: ignore_fields must be a list of field paths")
        if cfg.get("skip_types") == "*":
            errors.append(f"{label}: skip_types cannot be '*'")
        access = cfg.get("open_access")
        if access is not None:
            if (not isinstance(access, dict) or not access.get("field")
                    or not access.get("pattern")):
                errors.append(f"{label}: open_access needs field and pattern")
            else:
                try:
                    re.compile(str(access["pattern"]))
                except re.error as exc:
                    errors.append(f"{label}: open_access.pattern: {exc}")
        if not isinstance(cfg.get("enabled"), bool):
            errors.append(f"{label}: enabled must be true or false")
        rights = cfg.get("rights")
        if not isinstance(rights, dict):
            errors.append(f"{label}: rights must be an object")
            continue
        for field in ("robots", "terms_url", "terms_excerpt", "reviewed_at", "decision"):
            if not isinstance(rights.get(field), str) or not rights[field].strip():
                errors.append(f"{label}: rights.{field} is required")
        if rights.get("decision") not in DECISIONS:
            errors.append(f"{label}: rights.decision must be one of {DECISIONS}")
        if not DATE.fullmatch(str(rights.get("reviewed_at") or "")):
            errors.append(f"{label}: rights.reviewed_at must be YYYY-MM-DD")
        if cfg.get("enabled") and rights.get("decision") != "go":
            errors.append(f"{label}: an enabled repository needs rights.decision 'go'")
    backlog = data.get("backlog", [])
    if not isinstance(backlog, list):
        errors.append("backlog must be a list")
    else:
        for index, row in enumerate(backlog):
            if (not isinstance(row, dict) or not row.get("name") or not row.get("reason")
                    or row.get("status") not in ("later", "rejected")):
                errors.append(f"backlog[{index}]: needs name, status later|rejected and reason")
    return errors


def load_config(path: Path | None = None) -> dict[str, dict]:
    """Repository configs keyed by key; fails closed on schema problems."""
    path = Path(path or CONFIG_PATH)
    data = json.loads(path.read_text())
    errors = validate(data)
    if errors:
        raise ValueError(f"invalid {path}: " + "; ".join(errors))
    return data["repositories"]


def loader_hosts(repos: dict[str, dict]) -> dict[str, dict]:
    """host -> {"delay": seconds, "cap": downloads per run} for EVERY configured repository,
    enabled or not: rows registered before a repository was disabled are still fetched, and
    must keep the same protections (serial, paced, honest UA, per-run cap)."""
    out: dict[str, dict] = {}
    for cfg in repos.values():
        delay = max(float(cfg["crawl_delay"]), MIN_DELAY)
        for host in cfg["hosts"]:
            delay_ = max(delay, out.get(host, {}).get("delay", 0.0))
            out[host] = {"delay": delay_, "cap": max(1, int(RUN_SECONDS // delay_))}
    return out
