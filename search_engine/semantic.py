"""
Extension point for OPTIONAL semantic (meaning-based) search.

Nothing here downloads or runs a model. It defines the small interface a
future local embedding component must follow, and the function used to merge
its results with the keyword (BM25) results.

To add semantic search later, write a class with this one method:

    class MyEmbeddingRetriever:
        def search_documents(self, query: str, limit: int) -> list[tuple[str, float]]:
            # return [(doc_id, similarity_0_to_1), ...], best first
            ...

and pass it in:  SearchEngine(index_path, semantic_retriever=MyEmbeddingRetriever())

The keyword engine keeps working unchanged if no retriever is given.
See README section "Adding semantic search later" for model options and costs.
"""

from __future__ import annotations

from typing import Dict, List, Protocol, Sequence, Tuple


class DocumentRetriever(Protocol):
    def search_documents(self, query: str, limit: int) -> List[Tuple[str, float]]:
        ...


def reciprocal_rank_fusion(rankings: Sequence[Sequence[str]], k: int = 60) -> List[Tuple[str, float]]:
    """
    Merge several ranked lists of ids into one.

    Each list gives an item 1 / (k + position) points; points are summed.
    It only uses positions, so BM25 scores and cosine similarities (which are
    on completely different scales) never need to be compared directly.
    """
    scores: Dict[str, float] = {}
    for ranking in rankings:
        for position, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + position)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
