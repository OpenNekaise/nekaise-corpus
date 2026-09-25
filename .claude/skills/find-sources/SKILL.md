---
name: find-sources
description: Grow the corpus — run find_sources.py to discover new open-access sources via OpenAlex, review them (relevance, license, direct-PDF), add the good ones to the registry, then load + verify. Use when asked to find more data, enlarge, or grow the corpus.
---

# Skill: find-sources

Grow the corpus programmatically: discover new open-access built-environment sources, review them,
and add the good ones to the registry. The mechanical discovery lives in `scripts/find_sources.py`;
you (the agent) judge relevance + license and decide what to keep.

For routine growth use `python scripts/run_round.py --commit`; it advances the committed backend
rotation and runs the entire verified pipeline under the round lock. The examples below illustrate
a bounded source investigation, not a replacement scheduler. Use current rotation and configured
budgets; never reset a live cursor to zero or append while another round owns the lock.

## Steps

1. **Discover** (needs network; run outside a sandbox):
   ```
   python scripts/find_sources.py --per 100 --backends openalex \
     --query-cursor 0 --query-count 1                 # propose one budgeted query
   python scripts/find_sources.py --per 100 --backends openalex \
     --query-cursor 0 --query-count 1 --append        # append after review
   ```
   It queries **OpenAlex**, inspects every location of each work and keeps a copy only when it is on
   an allowed download host (exact host / subdomain match, never a fetch-suspended host) AND carries
   accepted rights evidence for that very copy (CC BY / BY-SA / CC0 / verified public domain;
   `scripts/oa_resolution.py`) — an unknown licence is never registered as `open`. The OSTI and
   arXiv backends fail closed (no per-record licence). It dedups against the manifest + the registry
   + `pruned_urls.txt` + DOI/OpenAlex identity, and prints ready-to-paste entries. OpenAlex anonymous
   access is metered (one search = 10 of 1,000 daily credits); routine rounds spend ONE search per
   round across the legacy 105-query cursor (`find_openalex`) and the building-simulation family
   (`find_openalex_sim`: `--family simulation --family-cursor …`, `scripts/openalex_families.py`),
   which walk a shared schedule. Do not run all OpenAlex queries at once.

2. **Review** (your judgment, not the script's):
   - **Relevance:** is it really built-environment / AEC / building-energy, and on-topic for its
     `topic` tag? OpenAlex search is broad — drop off-topic hits.
   - **License:** OpenAlex entries carry `license_evidence` for the selected copy; other sources
     may use `open` — check the per-source terms before any redistribution.
   - **URL:** confirm `url` is a direct PDF. Some OpenAlex `pdf_url`s are landing pages — the loader
     will fetch HTML or fail on those; fix or drop them.
   - **Dedup by meaning,** not just URL: skip near-duplicates of what is already in the corpus.

3. **Add + fetch:** use `--append` (routes entries to their registry shard), then run the
   **`load-corpus`** skill (`python scripts/build_corpus.py`) to download + verify. Inspect the new
   `text/*.md` for quality; drop any source that extracts to junk. Finish with
   `python scripts/clean_corpus.py` followed by `--check` so the new docs reach `corpus/`, the training-ready stage.

## Notes

- Tune `find_sources.py`'s `QUERIES` to target gaps. Use current coverage/yield evidence; equipment depth,
  commissioning checklists, codes and application data are examples to measure, not assumed gaps.
- **GitHub has its own backend:** `python scripts/find_github.py` walks a curated list of permissive
  building-sim repos (Modelica Buildings, EnergyPlus, OpenStudio, ResStock, …) and registers their
  README / `docs/*.md` / `*.rst` as raw-text entries (same propose -> `--append` -> load flow). The
  [`dig`](../dig/SKILL.md) skill runs it alongside this one.
- More backends: `scripts/find_osti.py` deep-harvests OSTI at scale; `scripts/find_books.py` pulls
  CC-BY open-access books from OAPEN. Others to add (same propose -> review -> load flow):
  CORE.ac.uk, OpenEI, Semantic Scholar. Extend `oa_resolution.ALLOWED_PDF_HOSTS` with
  reliably-downloadable OA hosts after probing them honestly; publisher landing pages
  (sciencedirect / springer / wiley / tandf / ieee) 403 bots and are deliberately excluded. SSRN is
  fetch-suspended (Cloudflare, all rights reserved): SSRN-origin works enter only through a
  separately licensed copy on another host.
- Never add `proprietary-internal` bytes; list paywalled high-value items as pointers only.
