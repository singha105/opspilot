# ADR-0004: Retrieval defaults

- Status: accepted
- Date: 2026-10-05

## Context

The retriever can combine three search modes, two chunkers, an optional cross-encoder
reranker and a hybrid weight `alpha`. Each choice trades quality against latency, and
the agent calls retrieval several times per incident on an 8 GB laptop. Day 2 measured
every combination on 60 graded queries (`evals/retrieval/RESULTS.md`, raw data in
`evals/results/retrieval-2026-10-05.json`). The rule set before measuring was: pick the
best nDCG@5 whose p95 latency is under 300 ms on this laptop.

## Decision

Default retrieval is **Weaviate, `markdown_section` chunks, hybrid search with
`alpha = 0.5`, no reranking, k = 6** (settings `OPSPILOT_RETRIEVAL_*`).

| config | R@1 | R@5 | MRR@10 | nDCG@5 | p95 ms |
|---|---|---|---|---|---|
| **weaviate / section / hybrid α=0.5 (default)** | 0.583 | 0.933 | 0.746 | **0.817** | **42** |
| weaviate / section / hybrid α=0.5 + rerank | 0.650 | 0.950 | 0.781 | 0.852 | 1314 |
| chroma / section / hybrid α=0.5 + rerank (best overall) | 0.667 | 0.950 | 0.789 | 0.857 | 1912 |
| weaviate / section / keyword | 0.583 | 0.883 | 0.711 | 0.778 | 7 |
| weaviate / section / dense | 0.483 | 0.783 | 0.612 | 0.672 | 14 |

What the numbers say:

- **Hybrid beats either side alone overall.** On section chunks, hybrid (0.817) beats
  keyword (0.778) and dense (0.672). By query type it wins on symptom queries (0.836 vs
  keyword 0.794, dense 0.647) and log snippets (0.862 vs 0.791 and 0.787), and ties
  keyword on the hard near-distractors (0.695 vs 0.701).
- **Reranking helps, but it is expensive here.** FlashRank MiniLM-L-12 over 20
  candidates adds 0.035 nDCG@5 to hybrid and 0.13 to dense, and helps the hard
  near-distractor queries most (0.695 → 0.787). It costs 0.5–4.5 s p95 per query on
  this laptop, so it is opt-in (`--rerank`, `OPSPILOT_RETRIEVAL_RERANK=true`).
- **Alpha 0.5 is the best of the sweep** (0.25: 0.801, 0.75: 0.769).
- **Section chunks beat fixed chunks** for hybrid and keyword search (0.817 vs 0.801;
  0.778 vs 0.762). Dense-only search is the exception (0.672 vs 0.684). Section chunks
  also keep the contextual header, which the agent cites.
- **Weaviate's hybrid beats Chroma's RRF** on the same vectors (0.817 vs 0.784): relative
  score fusion keeps the magnitude of BM25 matches that RRF reduces to a rank. With
  reranking they are nearly equal (0.857 Chroma, 0.852 Weaviate), because the reranker
  reorders largely the same candidates.
  Dense results are identical across stores, as expected from shared vectors.

## Alternatives considered

- **Hybrid + rerank as default.** About 0.035 higher nDCG@5, but more than 4x over the
  latency budget. The agent can still request reranking for a final evidence pass.
- **A smaller reranker (TinyBERT-L-2) or a shorter `max_length`.** Likely within budget,
  but not measured yet; added to the Day 5 ablations.
- **Chroma as the default store.** Same quality with reranking, lower without it, and
  no native hybrid; it stays the zero-infrastructure option and the CI smoke store.

## Consequences

- The default misses the primary document in its top 5 for 4 of 60 queries. In three of
  them a closely related postmortem outranks the runbook (q002, q019, q053). That is
  tolerable for the agent, because postmortems link to their runbooks, but it is the
  first thing to improve.
- Latency was measured while the laptop was swapping heavily (8–10 GB of swap in use, with
  other VMs running), so every p95 here is an upper bound for this hardware. The
  ranking of configurations does not depend on that.
- CI guards retrieval quality with a 15-query Chroma smoke eval that fails if Recall@5
  drops more than 0.05 below today's value (1.000).
