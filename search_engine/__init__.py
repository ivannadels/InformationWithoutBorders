"""
Offline medical document search engine (SQLite FTS5 + BM25).

With the team's database (search_index.db, built by index_documents.py and
synced by p2p/), see search_engine/iwb.py:

    from search_engine.iwb import IWBSearch
    engine = IWBSearch("search_index.db")
    response = engine.search("A child has severe diarrhoea and dehydration", top_k=5)

With an index built from a folder of files (python -m search_engine index ...):

    import search_engine
    search_engine.configure(index_path="folder_search_index.db")   # once at start-up
    response = search_engine.search("A child has severe diarrhoea and dehydration", top_k=5)
    for r in response.results:
        print(r.rank, r.title, r.source_path, r.snippet)

    response.to_dict()   # plain dict, ready for json.dumps / a web API

Or create the engine object yourself:

    from search_engine import SearchEngine
    engine = SearchEngine("folder_search_index.db")
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from .config import CsvColumnMap, SearchConfig
from .indexer import Indexer, IndexStats, build_index
from .loaders import load_corpus, load_jsonl
from .models import Document, Passage, QueryAnalysis, SearchResponse, SearchResult
from .search import IndexNotFoundError, SearchEngine

__all__ = [
    "SearchEngine", "SearchConfig", "CsvColumnMap", "Indexer", "IndexStats", "build_index",
    "load_corpus", "load_jsonl", "Document", "Passage", "QueryAnalysis", "SearchResponse",
    "SearchResult", "IndexNotFoundError", "configure", "search",
]

# Environment variable that can point at the index without changing code.
INDEX_ENV_VAR = "MSF_SEARCH_INDEX"

_default_engine: Optional[SearchEngine] = None
_default_lock = threading.Lock()


def configure(index_path: Optional[str | Path] = None, config: Optional[SearchConfig] = None,
              **engine_kwargs: Any) -> SearchEngine:
    """Create (or replace) the engine used by search_engine.search()."""
    global _default_engine
    with _default_lock:
        if _default_engine is not None:
            _default_engine.close()
        path = index_path or os.environ.get(INDEX_ENV_VAR) or (config.index_path if config else None)
        _default_engine = SearchEngine(path, config=config, **engine_kwargs)
        return _default_engine


def search(query: str, top_k: Optional[int] = None, filters: Optional[Dict[str, Any]] = None,
           min_coverage: Optional[float] = None) -> SearchResponse:
    """Search with the default engine (configure() is called automatically on first use)."""
    engine = _default_engine or configure()
    return engine.search(query, top_k=top_k, filters=filters, min_coverage=min_coverage)
