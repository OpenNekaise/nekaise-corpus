# Skill: crawl-docs

Add a multi-page documentation site to the corpus. The single-URL loader fetches one page; this
discovers all the content pages of a doc site and registers them, so the loader can fetch each. Use
for software docs (EnergyPlus, VOLTTRON, Modelica), ontology docs (Brick, Haystack, ASHRAE 223P /
open223), and similar references that aren't a single PDF.

## Steps

1. **Find the seed + path prefix.** Open the doc site; note the domain and the path the doc pages
   share (the prefix keeps the crawl inside the docs, off the rest of the site):
   - readthedocs: `--seed https://x.readthedocs.io/en/latest/ --prefix /en/latest/`
   - mkdocs / sphinx at a docs root: `--seed https://docs.x.org/ --prefix /`
   - a subsection: `--seed https://site/modelica/userGuide/ --prefix /modelica/userGuide/`

2. **Crawl + register** (needs network; run outside a sandbox):
   ```
   python crawl_docs.py --seed <url> --prefix <path> --source <tag> \
       --topic <topic> --license <license> --max 80 --append
   ```
   BFS within the domain + prefix, collects up to `--max` content pages, and (with `--append`)
   writes them into `sources.yaml` as `html` sources (id prefix `crawl-`). Pick the right `--topic`
   and a redistributable `--license`.

3. **Load + gate:** run the **`load-corpus`** skill (`build_corpus.py`) to fetch each page, then
   `python prune_corpus.py --apply` to drop thin nav / stub / off-topic pages (it gates `crawl-`
   pages too). Spot-check a few `text/crawl-*.md`.

## Reproducibility

The crawl runs ONCE (by us). `sources.yaml` freezes the page list, and each page keeps its own
sha256 in the manifest, so a clone fetches that frozen list -- it does NOT re-crawl -- and the corpus
can't drift from a changing site. Re-run the crawl only to intentionally refresh a site.

## Notes

- `extract_html` (in `build_corpus.py`) targets sphinx / readthedocs / mkdocs main-content
  containers and strips nav / sidebar / footer. Check a sample page if a new site extracts poorly.
- Respect each site's license and robots / ToS. Prefer openly-licensed docs (gov, open-source, CC).
  For forums (e.g. Unmet Hours, CC-BY-SA) keep `--max` modest and attribute.
- Pick `--prefix` carefully: too broad crawls the whole site; too narrow misses pages.
