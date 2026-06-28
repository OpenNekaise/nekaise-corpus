# Skill: find-sources

Grow the corpus programmatically: discover new open-access building-energy sources, review them, and
add the good ones to the registry. The mechanical discovery lives in `find_sources.py`; you (the
agent) judge relevance + license and decide what to keep.

## Steps

1. **Discover** (needs network; run outside a sandbox):
   ```
   python find_sources.py --per 20
   ```
   It queries the free OpenAlex API across the five topics, keeps open-access works that have a
   fetchable PDF and a usable license, dedups against `manifest.jsonl` + `sources.yaml`, and prints
   ready-to-paste `sources.yaml` entries.

2. **Review** (your judgment, not the script's):
   - **Relevance:** is it really building / HVAC / building-energy, and on-topic for its `topic`
     tag? OpenAlex search is broad — drop off-topic hits.
   - **License:** prefer `cc-by` / `cc-by-sa` / `cc0` / `public-domain` (redistributable). `open`
     means OA but check the per-source terms before any redistribution.
   - **URL:** confirm `url` is a direct PDF. Some OpenAlex `pdf_url`s are landing pages — the loader
     will fetch HTML or fail on those; fix or drop them.
   - **Dedup by meaning,** not just URL: skip near-duplicates of what is already in the corpus.

3. **Add + fetch:** paste the accepted entries under `sources:` in `sources.yaml`, then run the
   **`load-corpus`** skill (`python build_corpus.py`) to download + verify. Inspect the new
   `text/*.md` for quality; drop any source that extracts to junk.

## Notes

- Tune `find_sources.py`'s `QUERIES` to target gaps. We are paper-heavy; thin on equipment depth,
  commissioning checklists, codes, and manufacturer application data.
- Other discovery backends worth adding (same propose -> review -> load flow): the OSTI.gov API
  (US-gov / national-lab reports), the arXiv API, CORE.ac.uk, OpenEI.
- Never add `proprietary-internal` bytes; list paywalled high-value items as pointers only.
