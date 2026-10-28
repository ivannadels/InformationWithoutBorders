"""
Searching: read the index built by indexer.py and answer queries.

How one query is answered (all locally, typically in a few milliseconds):

 1. Analyse the description -> search terms, spelling variants, negations.
 2. For each term, find which documents contain it. Rare terms (e.g.
    "dehydration") get more weight than common ones (e.g. "patient").
    -> "coverage": how much of the description a document talks about.
 3. Ask SQLite FTS5 for the best-matching PASSAGES, ranked with BM25.
 4. Group passages by document. A document's score is its best passage's
    BM25 score, scaled by its coverage.
 5. Collapse duplicates (same document as PDF + HTML) into one result.
 6. Drop weak matches (low coverage, or far below the best result) and
    return at most top_k results, each with snippets and metadata.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .config import SearchConfig
from .indexer import SCHEMA_VERSION
from .models import Passage, QueryAnalysis, SearchResponse, SearchResult
from .semantic import DocumentRetriever, reciprocal_rank_fusion
from .text_utils import analyze_query, group_to_fts, term_groups


class IndexNotFoundError(FileNotFoundError):
    pass


class SearchEngine:
    """
    Main entry point for other components.

        engine = SearchEngine("folder_search_index.db")
        response = engine.search("child with severe diarrhoea", top_k=5)
        for r in response.results:
            print(r.rank, r.title, r.snippet)

    The engine is safe to share between threads (e.g. a Flask/FastAPI app).
    """

    def __init__(self, index_path: Optional[str | Path] = None,
                 config: Optional[SearchConfig] = None,
                 semantic_retriever: Optional[DocumentRetriever] = None):
        self.config = config or SearchConfig()
        if index_path is not None:
            self.config.index_path = Path(index_path)
        path = self.config.index_path.resolve()
        if not path.is_file():
            raise IndexNotFoundError(
                f"Search index not found at {path}. Build it first, e.g.\n"
                f"  python -m search_engine index --docs <folder> --csv <metadata.csv> --index {path}")
        # Read-only connection: searching can never modify the index.
        self._conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, check_same_thread=False)
        self._lock = threading.Lock()
        self.semantic = semantic_retriever

        meta = dict(self._conn.execute("SELECT key, value FROM meta").fetchall())
        if meta.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError(f"Index {path} was built with an incompatible version; rebuild it.")
        self._meta = meta
        self._doc_count = self._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        pattern = self.config.low_value_title_pattern
        self._low_value_re = re.compile(pattern, re.I) if pattern else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def search(self, query: str, top_k: Optional[int] = None,
               filters: Optional[Dict[str, Any]] = None,
               min_coverage: Optional[float] = None) -> SearchResponse:
        """
        query    Free-text description of the situation.
        top_k    Maximum number of results (default: config.default_top_k).
                 Fewer are returned if fewer documents match well.
        filters  Optional exact-match metadata filters, e.g. {"language": "en"}.
                 "file_format" can also be used.
        min_coverage  Override config.min_coverage for this search only, e.g. 0.2
                 for a "show weaker matches" button when nothing strong is found.
        """
        started = time.perf_counter()
        cfg = self.config
        top_k = cfg.default_top_k if top_k is None else max(1, min(int(top_k), cfg.max_top_k))

        if not query or not query.strip():
            return self._empty(query, started, "Please describe the situation you need information about.")

        analysis = analyze_query(query, cfg)
        if not analysis.search_terms:
            return self._empty(query, started,
                               "Your query only contained very common words. Please add more detail "
                               "(symptoms, condition, setting, population).", analysis)

        groups = term_groups(analysis.search_terms, analysis.expanded_terms)
        with self._lock:
            group_docs = [self._docs_matching(g) for g in groups]
            passages = self._candidate_passages(analysis.fts_query)

        # ---- term weights (rarer term = more informative) -----------------
        n = max(self._doc_count, 1)
        idf = [math.log(1 + (n - len(d) + 0.5) / (len(d) + 0.5)) for d in group_docs]
        total_idf = sum(idf) or 1.0
        not_found_anywhere = [g[0] for g, d in zip(groups, group_docs) if not d]

        def coverage_of(doc_id: str) -> float:
            return sum(w for w, docs in zip(idf, group_docs) if doc_id in docs) / total_idf

        # ---- group passages by document --------------------------------------
        by_doc: Dict[str, List[Tuple[float, int, int, Optional[int], str, str]]] = {}
        for rowid, doc_id, p_index, page, rank, snippet, section in passages:
            by_doc.setdefault(doc_id, []).append((-rank, rowid, p_index, page, snippet, section or ""))
        doc_rows = self._load_documents(list(by_doc))

        scored: List[Dict[str, Any]] = []
        for doc_id, doc_passages in by_doc.items():
            row = doc_rows.get(doc_id)
            if row is None or not self._passes_filters(row, filters):
                continue
            doc_passages.sort(key=lambda p: p[0], reverse=True)
            coverage = coverage_of(doc_id)
            best = doc_passages[0][0]
            score = best * (0.3 + 0.7 * coverage)
            if self._low_value_re and self._low_value_re.search(row.get("metadata", {}).get("page_title") or row["title"]):
                score *= cfg.low_value_penalty
            scored.append({"doc_id": doc_id, "row": row, "passages": doc_passages,
                           "coverage": coverage, "score": score})
        scored.sort(key=lambda d: d["score"], reverse=True)

        # ---- one result per duplicate group ---------------------------------------
        unique: List[Dict[str, Any]] = []
        seen_groups: Set[str] = set()
        for item in scored:
            if item["row"]["group_id"] not in seen_groups:
                seen_groups.add(item["row"]["group_id"])
                unique.append(item)

        # ---- quality filter ------------------------------------------------------
        top_score = unique[0]["score"] if unique else 0.0
        coverage_floor = cfg.min_coverage if min_coverage is None else min_coverage
        kept = [d for d in unique
                if d["coverage"] >= coverage_floor and d["score"] >= cfg.relative_score_cutoff * top_score]
        hidden_weak = sum(1 for d in unique if d["coverage"] < coverage_floor)

        if self.semantic is not None:
            kept = self._merge_semantic(query, kept, coverage_of, filters, seen_groups)

        kept = kept[:top_k]
        results = self._build_results(kept, groups, group_docs)
        total_candidates = len(set().union(*group_docs)) if group_docs else 0

        # ---- message for the user ---------------------------------------------------
        notes = []
        if not_found_anywhere:
            notes.append("No document mentions: " + ", ".join(not_found_anywhere) + ".")
        if analysis.negated_terms:
            notes.append("Note: keyword search does not understand negation; documents mentioning "
                         + ", ".join(analysis.negated_terms) + " may still be shown.")
        low_confidence = not results or results[0].match_strength == "weak"
        if not results:
            head = ("No document matched your description well enough. "
                    "Try rephrasing or adding specific terms (condition, symptoms, setting).")
            if hidden_weak:
                head += f" {hidden_weak} weaker match{'es' if hidden_weak != 1 else ''} hidden."
        elif low_confidence:
            head = f"Only weak matches found ({len(results)}). Check them carefully against your situation."
        else:
            head = f"Found {len(results)} matching document{'s' if len(results) != 1 else ''}."
        return SearchResponse(
            query=query, results=results, total_candidates=total_candidates,
            low_confidence=low_confidence, message=" ".join([head] + notes),
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2), analysis=analysis)

    def get_document(self, doc_id: str) -> Optional[Dict[str, Any]]:
        """Metadata and all passages of one document (for a 'details' page)."""
        with self._lock:
            row = self._load_documents([doc_id]).get(doc_id)
            if row is None:
                return None
            passages = self._conn.execute(
                "SELECT passage_index, page, section, body FROM passages WHERE doc_id = ? "
                "ORDER BY CAST(passage_index AS INTEGER)", (doc_id,)).fetchall()
        return {**row, "passages": [{"passage_index": int(i), "page": p, "section": sec, "text": b}
                                    for i, p, sec, b in passages]}

    def stats(self) -> Dict[str, Any]:
        return {**self._meta, "index_path": str(self.config.index_path), "documents": self._doc_count}

    def group_of(self, doc_id: str) -> Optional[str]:
        """Duplicate-group id of a document (used by the evaluation script)."""
        with self._lock:
            row = self._conn.execute("SELECT group_id FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _docs_matching(self, group: Sequence[str]) -> Set[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT doc_id FROM passages WHERE passages MATCH ?", (group_to_fts(group),))
        return {r[0] for r in rows}

    def _candidate_passages(self, fts_query: str) -> List[tuple]:
        cfg = self.config
        # bm25() weights follow the column order:
        # doc_id, passage_index, page (unindexed, weight 0), title, keywords, section, body
        sql = (
            "SELECT rowid, doc_id, passage_index, page, "
            "       bm25(passages, 0, 0, 0, ?, ?, ?, ?) AS rank, "
            "       snippet(passages, 6, ?, ?, '…', ?), section "
            "FROM passages WHERE passages MATCH ? ORDER BY rank LIMIT ?")
        params = (cfg.weight_title, cfg.weight_keywords, cfg.weight_section, cfg.weight_body,
                  cfg.highlight_start, cfg.highlight_end, min(max(cfg.snippet_tokens, 1), 64),
                  fts_query, cfg.candidate_passages)
        try:
            return self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # Should not happen because every term is quoted, but never crash the UI.
            return []

    def _load_documents(self, doc_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for start in range(0, len(doc_ids), 500):          # SQLite parameter limit
            chunk = doc_ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            for row in self._conn.execute(
                    f"SELECT doc_id, title, source_path, file_format, metadata_json, group_id "
                    f"FROM documents WHERE doc_id IN ({marks})", chunk):
                try:
                    metadata = json.loads(row[4] or "{}")
                except json.JSONDecodeError:
                    metadata = {}
                out[row[0]] = {"doc_id": row[0], "title": row[1], "source_path": row[2],
                               "file_format": row[3], "metadata": metadata, "group_id": row[5]}
        return out

    @staticmethod
    def _passes_filters(row: Dict[str, Any], filters: Optional[Dict[str, Any]]) -> bool:
        if not filters:
            return True
        for key, wanted in filters.items():
            value = row.get(key) if key in ("file_format", "doc_id") else row["metadata"].get(key)
            wanted_values = wanted if isinstance(wanted, (list, tuple, set)) else [wanted]
            if value is None or str(value).strip().lower() not in {str(w).strip().lower() for w in wanted_values}:
                return False
        return True

    def _build_results(self, kept: List[Dict[str, Any]], groups: List[List[str]],
                       group_docs: List[Set[str]]) -> List[SearchResult]:
        cfg = self.config
        results: List[SearchResult] = []
        for rank, item in enumerate(kept, start=1):
            row = item["row"]
            top_passages = item["passages"][:cfg.passages_per_result]
            with self._lock:
                bodies = dict(self._conn.execute(
                    f"SELECT rowid, body FROM passages WHERE rowid IN ({','.join('?' * len(top_passages))})",
                    [p[1] for p in top_passages]).fetchall()) if top_passages else {}
                siblings = self._conn.execute(
                    "SELECT doc_id, source_path, file_format FROM documents "
                    "WHERE group_id = ? AND doc_id != ?", (row["group_id"], row["doc_id"])).fetchall()
            passages = [Passage(passage_index=int(p[2]), page=p[3], section=p[5], snippet=p[4],
                                text=bodies.get(p[1], ""), score=round(p[0], 3)) for p in top_passages]
            coverage = item["coverage"]
            strength = ("strong" if coverage >= cfg.strong_match_coverage
                        else "partial" if coverage >= cfg.partial_match_coverage else "weak")
            results.append(SearchResult(
                rank=rank, doc_id=row["doc_id"], title=row["title"],
                source_path=row["source_path"], file_format=row["file_format"],
                snippet=passages[0].snippet if passages else "",
                relevance_score=round(item["score"], 3), match_coverage=round(coverage, 2),
                match_strength=strength,
                matched_terms=[g[0] for g, d in zip(groups, group_docs) if row["doc_id"] in d],
                missing_terms=[g[0] for g, d in zip(groups, group_docs) if row["doc_id"] not in d],
                metadata=row["metadata"], passages=passages,
                other_formats=[{"doc_id": s[0], "source_path": s[1], "file_format": s[2]} for s in siblings],
            ))
        return results

    def _merge_semantic(self, query, kept, coverage_of, filters, seen_groups):
        """
        Optional: combine keyword results with a local embedding model's results
        using Reciprocal Rank Fusion. Only runs if a semantic_retriever was given.
        """
        cfg = self.config
        hits = [(d, s) for d, s in self.semantic.search_documents(query, limit=50)
                if s >= cfg.semantic_min_similarity]
        semantic_ids = [d for d, _ in hits]
        keyword_ids = [d["doc_id"] for d in kept]
        fused = reciprocal_rank_fusion([keyword_ids, semantic_ids], k=cfg.rrf_k)

        by_id = {d["doc_id"]: d for d in kept}
        with self._lock:
            rows = self._load_documents([d for d in semantic_ids if d not in by_id])
        merged = []
        groups_used: Set[str] = set()
        for doc_id, fused_score in fused:
            item = by_id.get(doc_id)
            if item is None:
                row = rows.get(doc_id)
                if row is None or not self._passes_filters(row, filters):
                    continue
                with self._lock:
                    first = self._conn.execute(
                        "SELECT rowid, passage_index, page, substr(body, 1, 300), section FROM passages "
                        "WHERE doc_id = ? ORDER BY CAST(passage_index AS INTEGER) LIMIT 1", (doc_id,)).fetchone()
                passages = [(0.0, first[0], first[1], first[2], first[3] + "…", first[4])] if first else []
                item = {"doc_id": doc_id, "row": row, "passages": passages, "coverage": coverage_of(doc_id)}
            if item["row"]["group_id"] in groups_used:
                continue
            groups_used.add(item["row"]["group_id"])
            merged.append({**item, "score": fused_score})
        return merged

    def _empty(self, query: str, started: float, message: str,
               analysis: Optional[QueryAnalysis] = None) -> SearchResponse:
        return SearchResponse(query=query or "", results=[], total_candidates=0, low_confidence=True,
                              message=message, elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
                              analysis=analysis)
