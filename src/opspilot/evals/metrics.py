"""Ranking metrics over document ids.

Retrieval returns chunks; metrics are computed on the ranked list of distinct
documents (first occurrence wins). Relevance is graded: 2 = primary, 1 = secondary.

- Recall@k: share of primary documents found in the top k.
- MRR@k: reciprocal rank of the first primary document within the top k (0 if absent).
- nDCG@k: graded gain ``2^rel - 1`` with log2 discount, normalised by the ideal ranking.
"""

import math
from collections.abc import Mapping, Sequence

PRIMARY = 2


def unique_docs(doc_ids: Sequence[str]) -> list[str]:
    """Distinct document ids in first-seen order."""
    return list(dict.fromkeys(doc_ids))


def recall_at_k(ranked: Sequence[str], relevant: Mapping[str, int], k: int) -> float:
    primary = {d for d, grade in relevant.items() if grade >= PRIMARY}
    if not primary:
        return 0.0
    return len(primary & set(ranked[:k])) / len(primary)


def mrr_at_k(ranked: Sequence[str], relevant: Mapping[str, int], k: int) -> float:
    for rank, doc in enumerate(ranked[:k], start=1):
        if relevant.get(doc, 0) >= PRIMARY:
            return 1.0 / rank
    return 0.0


def dcg(grades: Sequence[int]) -> float:
    return float(sum((2.0**g - 1) / math.log2(i + 2) for i, g in enumerate(grades)))


def ndcg_at_k(ranked: Sequence[str], relevant: Mapping[str, int], k: int) -> float:
    ideal = dcg(sorted(relevant.values(), reverse=True)[:k])
    if ideal == 0:
        return 0.0
    return dcg([relevant.get(doc, 0) for doc in ranked[:k]]) / ideal


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile (``pct`` in 0-100)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]
