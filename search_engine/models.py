"""
Data shapes shared by every part of the search engine.

These are the "contract" between components:
  * Document      -> what the document-processing component hands to the indexer
  * SearchResult  -> what the search engine hands back to the frontend
  * SearchResponse-> one full answer to one query (results + diagnostics)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Document:
    """
    One source document, already converted to plain text.

    doc_id       Stable unique id (e.g. from the CSV, or the file name).
    title        Human-readable title. Falls back to the file name.
    text         Extracted plain text. Use "\\f" (form feed) between PDF pages
                 if you want page numbers in the results.
    source_path  Where the original file lives, so users can open/verify it.
    file_format  "pdf", "html", "txt", ...
    metadata     Anything else (date, version, language, organisation...).
                 Kept as-is and returned with every result.
    keywords     Optional extra searchable text from metadata (topics, summary).
    group_key    Optional: documents with the same group_key are treated as
                 the same document in different formats.
    """
    doc_id: str
    title: str
    text: str
    source_path: str = ""
    file_format: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    keywords: str = ""
    group_key: Optional[str] = None


@dataclass
class Passage:
    """A section of a document, as returned with a search result."""
    passage_index: int        # 0 = first passage of the document
    page: Optional[int]       # page number if known (PDFs), else None
    section: str              # headings this passage sits under, e.g. "Treatment > Children"
    snippet: str              # short excerpt with matched words highlighted
    text: str                 # full passage text, for "show more"
    score: float              # BM25 score of this passage (higher = better match)


@dataclass
class SearchResult:
    rank: int                       # 1 = best match
    doc_id: str
    title: str
    source_path: str
    file_format: str
    snippet: str                    # best excerpt (same as passages[0].snippet)
    relevance_score: float          # keyword-match strength, NOT medical correctness
    match_coverage: float           # share of the query's key terms found (0..1)
    match_strength: str             # "strong" | "partial" | "weak" keyword match
    matched_terms: List[str]        # which query terms were found in this document
    missing_terms: List[str]        # which important query terms were NOT found
    metadata: Dict[str, Any]
    passages: List[Passage] = field(default_factory=list)
    other_formats: List[Dict[str, str]] = field(default_factory=list)  # duplicates collapsed into this result

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QueryAnalysis:
    """How the engine interpreted the user's text. Useful to show in the UI."""
    original_query: str
    normalized_query: str
    search_terms: List[str]                   # terms actually searched for
    expanded_terms: Dict[str, List[str]]      # synonyms/spelling variants added
    negated_terms: List[str]                  # terms that followed "no", "without", ...
    ignored_words: List[str]                  # stopwords / request words not searched
    fts_query: str                            # exact query sent to SQLite FTS5


@dataclass
class SearchResponse:
    query: str
    results: List[SearchResult]
    total_candidates: int          # documents that matched at least one term
    low_confidence: bool           # True if nothing matched well
    message: str                   # human-readable explanation for the UI
    elapsed_ms: float
    analysis: Optional[QueryAnalysis] = None
    disclaimer: str = (
        "Results are ranked by keyword match with your description, not by "
        "clinical appropriateness. Always check the original source, its date "
        "and its version before acting on it."
    )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
