# nekaise-corpus

A curated, license-tagged, **reproducible recipe** for assembling a building / HVAC /
**building-energy** corpus for training and evaluating LLMs in the building-energy domain.

> This repo ships the **registry + loader + provenance**, NOT the data bytes. The corpus mixes
> licenses (US-gov public-domain, CC-BY-SA, arXiv, and some non-redistributable vendor/standards
> material), so we cannot and do not host the files. You fetch your own copy with the loader and
> respect each source's license. (This is how RedPajama / The Pile-style corpora work.)

## What's here

| File | What it is |
|---|---|
| `sources.yaml` | The curated registry — each source's URL, topic, license, format. **Edit this to grow the corpus.** |
| `build_corpus.py` | The loader — downloads sources into `raw/`, extracts plain text into `text/`, dedups by sha256, writes the manifest. |
| `manifest.jsonl` | Provenance — id, url, license, topic, sha256, bytes for every fetched doc. |
| `skills/load-corpus.md` | The **skill** an AI agent runs to fetch + verify the corpus (mirrored to `.claude/skills/`). |
| `raw/`, `text/` | **Git-ignored.** Your local copy of the bytes / extracted text. Never committed. |

## Use it

Easiest: ask your AI coding agent (Claude Code / Codex) to **"load the building-energy corpus"** —
the [`load-corpus`](skills/load-corpus.md) skill drives the download and verifies it. Or run it
yourself:

```bash
pip install requests pyyaml pypdf beautifulsoup4
python build_corpus.py            # fetch missing sources (needs network)
python build_corpus.py --force    # re-fetch everything
python build_corpus.py --only controls_bas
```

## Topics

`controls_bas` · `equipment_systems` · `building_energy` · `commissioning_fdd` · `standards_protocols`

(~186 sources; ~14 MB extracted text / ~300 MB raw originals when fully fetched.)

## Licensing — read before you redistribute

Every source carries a `license` in `sources.yaml` / `manifest.jsonl`:

- **`public-domain`** — US government / national-lab reports (DOE, PNNL, LBNL, OSTI). Free to use.
- **`cc-by-sa`** — Wikipedia. Attribution + share-alike.
- **`open`** — arXiv papers. Check each paper's individual license; many are NOT freely
  redistributable.
- **`proprietary-internal`** — copyrighted vendor pages / standards (e.g. ASHRAE). Listed here as
  pointers for your own access only; **do NOT redistribute the bytes.**

`raw/` and `text/` are git-ignored for exactly this reason. What this project publishes is the
registry and manifest (pointers + metadata — our curation), plus the loader.

## Contributing

Add a source: append an entry to `sources.yaml` and open a PR. Prefer openly-licensed material
(public-domain gov reports, CC, arXiv). Keep copyrighted material `proprietary-internal` and never
add its bytes.

## License

The code, registry, and manifest in this repo are MIT. The referenced source documents retain their
own licenses (see above). Part of the [OpenNekaise](https://github.com/OpenNekaise) ecosystem;
consumed by [nekaise-studio](https://github.com/OpenNekaise/nekaise-studio) as domain-ceiling
material.
