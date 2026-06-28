# Skill: load-corpus

Assemble (or refresh) the building-energy LLM corpus on this machine from the curated registry.
You (the agent) drive the download and **verify** it; the mechanical fetch/clean/dedup lives in
`build_corpus.py`. This repo ships the RECIPE (registry + loader), never the copyrighted bytes —
each user fetches their own copy.

## To load / refresh the corpus

1. **Run the loader** (needs network; run outside any sandbox):
   ```
   python build_corpus.py
   ```
   It reads `sources.yaml`, downloads any missing source into `raw/<source>/`, extracts plain text
   into `text/<id>.md`, dedups by sha256, and writes `manifest.jsonl`. Idempotent — re-running only
   fetches what is missing. `--force` re-fetches everything; `--only <topic>` limits scope.

2. **Verify** (your job, not the script's):
   - Read the printed summary: how many `ok` vs `failed`, by topic.
   - Investigate every failure. A 404 = a moved/dead URL → fix it in `sources.yaml`. A DNS error may
     be the environment (some hosts are blocked in sandboxes). Report failures, do not hide them.
   - Spot-check quality: open a few `text/*.md` and confirm they are real content, not error pages or
     near-empty stubs. Flag thin extractions (a few hundred chars) for a better source or re-extract.
   - Optionally re-hash a downloaded file and confirm it matches its `sha256` in `manifest.jsonl`.

3. **Summarize** what was fetched (doc count, MB, topics) and any license caveats.

## To grow the corpus

Add entries to `sources.yaml` (`id` / `title` / `url` / `source` / `license` / `topic` / `format`),
then re-run. Prefer openly-licensed sources (public-domain gov reports, CC, arXiv). Tag copyrighted
vendor/standards material `proprietary-internal` and do NOT add or redistribute its bytes.

## License discipline (important)

`raw/` and `text/` are git-ignored and must NEVER be committed — they hold copyrighted content under
mixed licenses (`public-domain` / `cc-by-sa` / `open` / `proprietary-internal`). This repo publishes
only the registry, the loader, and the provenance manifest. Respect each source's `license` field
before any downstream use that leaves this machine.
