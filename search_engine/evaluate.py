"""
Measure search quality on a hand-labelled set of queries.

Labelled queries file (JSON list):

  [
    {"query": "child with severe diarrhoea and dehydration",
     "relevant": ["diarrhoea_children"]},
    {"query": "how to repair a motorbike engine",
     "relevant": []}          # empty = the engine SHOULD return nothing
  ]

"relevant" lists the doc_ids a medic would want to see for that query.
List each real document once; other formats of it (PDF + HTML) are
recognised automatically through the duplicate groups.

Metrics (k = 5 by default):
  Precision@k  of the results shown, what share were relevant?
               = relevant results shown / results shown
               (we divide by results actually shown, because the engine
               deliberately shows fewer than k when matches are weak)
  Recall@k     of all relevant documents, what share did we show?
               = relevant results shown / total relevant documents
  MRR          1 / position of the first relevant result (1.0 = top result)
  Abstention   for queries with no relevant documents: did we return nothing?
  Latency      measured wall-clock time per query (median and slowest)
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence

from .search import SearchEngine


def precision_at_k(returned: Sequence[str], relevant: set, k: int) -> float:
    shown = list(returned)[:k]
    if not shown:
        return 0.0
    return sum(1 for g in shown if g in relevant) / len(shown)


def recall_at_k(returned: Sequence[str], relevant: set, k: int) -> float:
    if not relevant:
        return 0.0
    return len(set(list(returned)[:k]) & relevant) / len(relevant)


def reciprocal_rank(returned: Sequence[str], relevant: set) -> float:
    for position, g in enumerate(returned, start=1):
        if g in relevant:
            return 1.0 / position
    return 0.0


def evaluate(engine: SearchEngine, labelled: List[Dict[str, Any]], k: int = 5) -> Dict[str, Any]:
    per_query = []
    latencies = []
    for item in labelled:
        relevant_groups = {engine.group_of(d) or d for d in item.get("relevant", [])}
        t0 = time.perf_counter()
        response = engine.search(item["query"], top_k=k)
        latencies.append((time.perf_counter() - t0) * 1000)
        returned_groups = [engine.group_of(r.doc_id) or r.doc_id for r in response.results]
        row: Dict[str, Any] = {
            "query": item["query"],
            "returned": [r.doc_id for r in response.results],
            "relevant": sorted(item.get("relevant", [])),
            "n_returned": len(returned_groups),
            "low_confidence": response.low_confidence,
        }
        if relevant_groups:
            row.update(precision=precision_at_k(returned_groups, relevant_groups, k),
                       recall=recall_at_k(returned_groups, relevant_groups, k),
                       rr=reciprocal_rank(returned_groups, relevant_groups),
                       duplicates_in_results=len(returned_groups) - len(set(returned_groups)))
        else:
            row.update(abstained=len(returned_groups) == 0)
        per_query.append(row)

    answerable = [r for r in per_query if "precision" in r]
    unanswerable = [r for r in per_query if "abstained" in r]

    def mean(key: str) -> float:
        return round(statistics.mean(r[key] for r in answerable), 3) if answerable else 0.0

    return {
        "k": k,
        "queries": len(per_query),
        f"mean_precision@{k}": mean("precision"),
        f"mean_recall@{k}": mean("recall"),
        "mrr": mean("rr"),
        "mean_results_returned": round(statistics.mean(r["n_returned"] for r in per_query), 2) if per_query else 0,
        "duplicate_results": sum(r.get("duplicates_in_results", 0) for r in per_query),
        "abstention_rate": (round(sum(r["abstained"] for r in unanswerable) / len(unanswerable), 3)
                            if unanswerable else None),
        "latency_ms_median": round(statistics.median(latencies), 2) if latencies else 0,
        "latency_ms_max": round(max(latencies), 2) if latencies else 0,
        "per_query": per_query,
    }


def load_labelled_queries(path: Path) -> List[Dict[str, Any]]:
    """Accepts a JSON list, or {"queries": [...]} so the file can carry a note."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data["queries"] if isinstance(data, dict) else data


def print_report(report: Dict[str, Any]) -> None:
    k = report["k"]
    print(f"\nEvaluated {report['queries']} queries (k={k})")
    print("-" * 72)
    for r in report["per_query"]:
        if "precision" in r:
            print(f"P@{k}={r['precision']:.2f} R@{k}={r['recall']:.2f} RR={r['rr']:.2f} "
                  f"n={r['n_returned']}  {r['query'][:60]}")
        else:
            status = "OK, returned nothing" if r["abstained"] else f"returned {r['n_returned']} (expected none)"
            print(f"[no relevant docs] {status}  {r['query'][:50]}")
    print("-" * 72)
    print(f"Mean Precision@{k}: {report[f'mean_precision@{k}']}")
    print(f"Mean Recall@{k}:    {report[f'mean_recall@{k}']}")
    print(f"MRR:                {report['mrr']}")
    print(f"Avg results shown:  {report['mean_results_returned']}")
    print(f"Duplicate results:  {report['duplicate_results']}")
    if report["abstention_rate"] is not None:
        print(f"Correctly returned nothing for off-topic queries: {report['abstention_rate']:.0%}")
    print(f"Latency: median {report['latency_ms_median']} ms, slowest {report['latency_ms_max']} ms")
