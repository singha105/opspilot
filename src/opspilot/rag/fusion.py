"""Reciprocal Rank Fusion."""

from collections.abc import Sequence

RRF_K = 60


def rrf(
    rankings: Sequence[Sequence[str]],
    k: int = RRF_K,
    weights: Sequence[float] | None = None,
) -> list[tuple[str, float]]:
    """Fuse ranked id lists: ``score(d) = sum_i w_i / (k + rank_i(d))`` with 1-based ranks.

    Ties are broken by the order in which ids first appear, so the result is
    deterministic. Weights default to 1 for every ranking.
    """
    if weights is None:
        weights = [1.0] * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("one weight per ranking is required")
    scores: dict[str, float] = {}
    first_seen: dict[str, int] = {}
    for ranking, weight in zip(rankings, weights, strict=True):
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + weight / (k + rank)
            first_seen.setdefault(item, len(first_seen))
    return sorted(scores.items(), key=lambda kv: (-kv[1], first_seen[kv[0]]))
