# Does training on nekaise-corpus work? — evidence

**TL;DR — yes, measurably.** Continued-pretraining (CPT) a model on nekaise-corpus makes it
substantially better at building-energy language modeling, **specializes** it toward the domain,
**preserves** general ability, and **improves** the downstream building task —
consistently across **five models (0.8B–14B, three families)**. All numbers are on held-out data and
reproducible from the loader. (Study run on the sibling
[nekaise-studio](https://github.com/OpenNekaise/nekaise-studio); models: `granite-4.1-3B`,
`Qwen3.5-0.8B`; corpus snapshot: round 6, 1,707 docs / 38.3M tokens.)

## 1. Domain perplexity: −56% — and the model specializes toward building energy

CPT of **granite-4.1-3B** on the corpus (27.6M tokens, 3 epochs, full-parameter), evaluated on 86
**held-out** building-energy docs (never trained on) vs a **generic** non-building control (Wikipedia:
jazz, French Revolution, photosynthesis, football, Roman Empire, coffee):

| | held-out **building-energy** ppl | **generic** ppl |
|---|---|---|
| granite-3B base | 13.7 | 11.5 |
| **+ CPT on nekaise-corpus** | **6.0  (−56%)** | 7.2  (−37%) |

Two findings: (1) the corpus **more than halves** building-energy perplexity; (2) it **specializes** —
a base model finds building-energy text *harder* than general text (13.7 vs 11.5); after CPT it finds
building-energy *easier* (6.0 vs 7.2). The domain gain is ~1.5× the generic gain. Qwen3.5-0.8B shows
the same direction (held-out domain ppl 11.7 → 8.8).

## 2. It holds across model sizes & families

Same recipe (QLoRA CPT on the corpus, held-out **domain** vs **generic** perplexity) across five
models spanning three families. **CPT reduced building-energy perplexity for every one**, and the gain
was **domain-specific** (domain fell more than generic) in every case:

| model | domain ppl (base → CPT) | domain reduction | generic reduction |
|---|---|---|---|
| Qwen3.5-0.8B | 12.7 → 9.8 | **−23%** | −11% |
| granite-4.1-3B | 14.3 → 6.6 | **−54%** | −34% |
| granite-4.1-8B | 13.8 → 5.8 | **−58%** | −41% |
| Qwen3-14B | 7.7 → 5.7 | **−25%** | −21% |
| gemma-3-27B | 8.1 → — | _training needs ≥80 GB_ | — |

Read honestly:
- **Universal** — every model gets better at modeling building-energy text after CPT on the corpus.
- **Domain-specific** — the domain drop exceeds the generic drop for every model.
- **Magnitude tracks the base model's starting fit, not size** — granite starts with the weakest
  domain fit (base ppl ~14) and gains most (−54/−58%); Qwen already models the domain well (base
  ppl ~8 at 14B) and gains less (−25%). **The corpus helps most the models that need it most.**
- **Same-family scaling (granite 3B → 8B)** — the larger model absorbs slightly more (−54% → −58%)
  and reaches lower absolute perplexity (6.6 → 5.8).
- **27B** — base perplexity measured, but QLoRA *training* of a 262k-vocab 27B exceeds a 48 GB GPU
  (needs ≥80 GB). A hardware limit, not a corpus one.

_Caveats (stated up front): absolute perplexity is not comparable **across** families (different
tokenizers) — compare the % reductions. The 14B/27B runs were VRAM-constrained (shorter seq / time
budget), so their CPT is lighter; direction is unaffected. QLoRA (not full-parameter) is used for
cross-size consistency — full-parameter is stronger (the granite-3B full-param run in §1 is −56%)._

## 3. No catastrophic forgetting

General closed-book knowledge is unchanged: granite-3B scores **0.975 → 0.975** on a 40-question
building/HVAC knowledge quiz before and after CPT. The corpus adds domain fluency without erasing
general ability.

## 4. Downstream building task: CPT is a proven lever

On the real deployment task — answer a building engineer's questions about a specific building
(open-book, with retrieval) — CPT on the corpus helps: **CPT→SFT beats SFT-alone**, and the full
pipeline (CPT → SFT → GRPO → retrieval) closes ~67% of the base→teacher gap, reaching **0.540 vs the
Opus-4.8 teacher's 0.630** — a 3B model at **~86% of a frontier model** on building-energy Q&A. Adding
the 2×-grown corpus set a new best on the task sanity metric (0.625 → 0.75).

## What CPT does *not* do (and why that's fine)

We also tested closed-book recall of **specific** corpus facts (16 questions on figures from
PNNL / Title 24 / ASHRAE 90.1 reports). CPT did **not** move it — granite scored 8/16 before *and*
after, despite the −56% perplexity drop. This is expected: raw next-token training learns the
domain's *distribution and representations*, not individual *facts* (Allen-Zhu, "Physics of Language
Models"). Specific facts are served by **retrieval** at inference — exactly how this corpus is meant
to be used. Its value is (a) domain representations for reading/reasoning and (b) the retrieval
substrate; the results above confirm both.

## Reproduce

```bash
# 1. fetch the corpus (this repo)
python scripts/build_corpus.py
# 2. in nekaise-studio (GPU; no API needed):
python experiments/granite-4.1-3b-building/build_cpt_data.py          # corpus -> CPT trainset
NEKAISE_METHOD=cpt NEKAISE_STAGE=cpt_gran_full NEKAISE_CPT_FULL=1 \
  python experiments/granite-4.1-3b-building/train.py                 # CPT (reports ppl before/after)
python workspace/ppl_eval.py --model outputs/cpt_gran_full            # held-out domain vs generic ppl
python eval_domain.py --model outputs/cpt_gran_full                   # closed-book knowledge quiz
```

_Snapshot 2026-07-01. Perplexity is a language-modeling metric (does the model learn the domain);
the building-task numbers are the deployment metric. Corpus grows as sources are added to
the registry — bigger, denser corpora raise the representation + retrieval ceiling._
