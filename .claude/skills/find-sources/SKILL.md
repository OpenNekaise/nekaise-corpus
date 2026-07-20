---
name: find-sources
description: Grow the corpus — run find_sources.py to discover new open-access sources via OpenAlex/OSTI/arXiv, review them (relevance, license, direct-PDF), add the good ones to the registry, then load + verify. Use when asked to find more data, enlarge, or grow the corpus.
---

# Skill: find-sources

Grow the corpus programmatically: discover new open-access built-environment sources, review them,
and add the good ones to the registry. The mechanical discovery lives in `scripts/find_sources.py`;
you (the agent) judge relevance + license and decide what to keep.

## Steps

1. **Discover** (needs network; run outside a sandbox):
   ```
   python scripts/find_sources.py --per 20            # propose
   python scripts/find_sources.py --per 20 --append   # append straight into the registry
   ```
   It queries three free backends -- **OpenAlex** (filtered to repository / gov / arXiv / PMC PDF
   copies, NOT publisher pages that 403 bots), **OSTI** (US DOE / national-lab reports,
   public-domain), and the **arXiv API** -- across the topics, keeps candidates whose PDF is on
   a download-friendly host, dedups against the manifest + the registry + `pruned_urls.txt`, and prints
   ready-to-paste entries. `--backends openalex,osti,arxiv` selects which to use.

2. **Review** (your judgment, not the script's):
   - **Relevance:** is it really built-environment / AEC / building-energy, and on-topic for its
     `topic` tag? OpenAlex search is broad — drop off-topic hits.
   - **License:** prefer `cc-by` / `cc-by-sa` / `cc0` / `public-domain` (redistributable). `open`
     means OA but check the per-source terms before any redistribution.
   - **URL:** confirm `url` is a direct PDF. Some OpenAlex `pdf_url`s are landing pages — the loader
     will fetch HTML or fail on those; fix or drop them.
   - **Dedup by meaning,** not just URL: skip near-duplicates of what is already in the corpus.

3. **Add + fetch:** use `--append` (routes entries to their registry shard), then run the
   **`load-corpus`** skill (`python scripts/build_corpus.py`) to download + verify. Inspect the new
   `text/*.md` for quality; drop any source that extracts to junk.

## Notes

- Tune `find_sources.py`'s `QUERIES` to target gaps. We are paper-heavy; thin on equipment depth,
  commissioning checklists, codes, and manufacturer application data.
- **GitHub has its own backend:** `python scripts/find_github.py` walks a curated list of permissive
  building-sim repos (Modelica Buildings, EnergyPlus, OpenStudio, ResStock, …) and registers their
  README / `docs/*.md` / `*.rst` as raw-text entries (same propose -> `--append` -> load flow). The
  [`dig`](../dig/SKILL.md) skill runs it alongside this one.
- More backends: `scripts/find_osti.py` deep-harvests OSTI at scale; `scripts/find_books.py` pulls
  CC-BY open-access books from OAPEN. Others to add (same propose -> review -> load flow):
  CORE.ac.uk, OpenEI, Semantic Scholar. Extend `WHITELIST` with more reliably-downloadable OA hosts
  as you find them; publisher landing pages (sciencedirect / springer / wiley / ieee) 403 bots and
  are deliberately excluded.
- Never add `proprietary-internal` bytes; list paywalled high-value items as pointers only.
