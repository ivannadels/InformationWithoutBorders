"""
Settings for the search engine.

Everything that you might want to tune lives here, so nothing is hard-coded
inside the indexing or search logic. Create a SearchConfig, change the fields
you care about, and pass it to Indexer / SearchEngine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


# Words that carry no search meaning on their own. They are removed ONLY from
# the full-text query sent to SQLite; the original query text is always kept
# and returned to the caller. Negation words ("no", "not", "without") are
# deliberately NOT in this list: they are detected separately and reported.
DEFAULT_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "for", "by",
    "with", "from", "into", "is", "are", "was", "were", "be", "been", "being",
    "has", "have", "had", "do", "does", "did", "this", "that", "these", "those",
    "it", "its", "as", "but", "if", "then", "so", "than", "there", "their",
    "they", "them", "he", "she", "his", "her", "we", "our", "you", "your", "i",
    "my", "me", "who", "whom", "which", "what", "when", "where", "how", "why",
    "can", "could", "should", "would", "will", "may", "might", "must", "shall",
    "some", "any", "all", "also", "very", "just", "about", "over", "such",
    "while", "after", "before", "during", "up", "down", "out", "only", "own",
    "same", "other", "both", "each", "more", "most", "being", "am",
}

# Words people use to ask for things ("find me relevant guidelines...") that
# say nothing about the medical situation itself.
DEFAULT_QUERY_META_WORDS = {
    "find", "finding", "relevant", "please", "looking", "need", "needs",
    "information", "info", "document", "documents", "help", "show", "search",
    "give", "provide", "want", "tell", "related", "regarding", "concerning",
    # Nearly every document in a medical guideline collection is a "clinical
    # guideline/protocol", so these words describe the request, not the case.
    "guideline", "guidelines", "protocol", "protocols", "guidance",
    "recommendation", "recommendations", "clinical", "medical",
}

# Words that flip the meaning of the words after them. Keyword search cannot
# truly understand negation, so we detect these and REPORT them to the caller
# instead of silently dropping or ignoring them.
DEFAULT_NEGATION_WORDS = {"no", "not", "without", "denies", "denied", "absent",
                          "negative", "never", "none", "nor", "non"}

# Spelling variants and a few common lay/clinical equivalents. Each key is
# expanded to "key OR value1 OR value2" in the full-text query. Keep this list
# short and reviewed by someone with medical knowledge; it is editable data,
# not code.
DEFAULT_SYNONYMS: Dict[str, List[str]] = {
    "diarrhoea": ["diarrhea"],
    "diarrhea": ["diarrhoea"],
    "haemorrhage": ["hemorrhage", "bleeding"],
    "hemorrhage": ["haemorrhage", "bleeding"],
    "bleeding": ["haemorrhage", "hemorrhage"],
    "anaemia": ["anemia"],
    "anemia": ["anaemia"],
    "paediatric": ["pediatric", "child", "children"],
    "pediatric": ["paediatric", "child", "children"],
    "child": ["children", "paediatric", "pediatric"],
    "children": ["child", "paediatric", "pediatric"],
    "kid": ["child", "children"],
    "kids": ["child", "children"],
    "baby": ["infant", "newborn"],
    "infant": ["baby", "newborn"],
    "newborn": ["neonate", "neonatal", "infant"],
    "oedema": ["edema"],
    "edema": ["oedema"],
    "oesophagus": ["esophagus"],
    "haemoglobin": ["hemoglobin"],
    "faeces": ["feces", "stool"],
    "stool": ["faeces", "feces"],
    "vomiting": ["vomit"],
    "dehydrated": ["dehydration"],
    "malnourished": ["malnutrition"],
    "tb": ["tuberculosis"],
    "tuberculosis": ["tb"],
    "ors": ["rehydration"],
    "sam": ["malnutrition"],
    "pregnant": ["pregnancy", "maternal"],
    "pregnancy": ["pregnant", "maternal"],
    "fever": ["febrile"],
    "snake": ["snakebite"],
}


@dataclass
class CsvColumnMap:
    """
    Tells the loader which CSV columns hold which information.

    If a field is None the loader tries common names automatically
    (e.g. "filename", "file", "path" for the file column). Every column that
    is not mapped below is still kept as metadata on the document.
    """
    file_column: Optional[str] = None       # column with file name or relative path
    id_column: Optional[str] = None         # column with a stable document id
    title_column: Optional[str] = None      # human-readable title
    group_column: Optional[str] = None      # same value = same document in different formats
    keywords_columns: List[str] = field(default_factory=list)  # extra searchable text (topics, summary...)


@dataclass
class SearchConfig:
    # ---- where the index lives -------------------------------------------
    # Not "search_index.db": that name is the team's database (index_documents.py).
    # Ending in .db keeps it out of git (.gitignore ignores *.db).
    index_path: Path = Path("folder_search_index.db")

    # ---- result size -------------------------------------------------------
    default_top_k: int = 5            # results returned when caller gives no top_k
    max_top_k: int = 50               # hard upper limit to protect the UI
    passages_per_result: int = 3      # matching sections shown per document

    # ---- passage splitting (done once, at index time) ---------------------
    passage_target_words: int = 180   # aim for passages around this size
    passage_max_words: int = 260      # never longer than this
    passage_overlap_words: int = 30   # overlap when a long paragraph is cut

    # ---- ranking -----------------------------------------------------------
    # BM25 weight per indexed column. Higher = a match there counts more.
    weight_title: float = 4.0
    weight_body: float = 1.0
    weight_keywords: float = 2.0
    weight_section: float = 2.0       # headings above the passage
    candidate_passages: int = 400     # passages fetched before grouping by document
    # Pages that list things rather than explain them (bibliographies, lists of
    # abbreviations or authors) match almost any query. Their score is
    # multiplied by `low_value_penalty` so they only win when nothing else does.
    low_value_title_pattern: str = (
        r"\b(main references|references|bibliograph\w*|r[ée]f[ée]rences|referencias|"
        r"abbreviations|abr[ée]viations|abreviaturas|acronyms|acronymes|acr[óo]nimos|"
        r"authors|auteurs|autores|contributors|contributeurs|colaboradores|"
        r"acknowledg\w*|remerciements|agradecimientos|المراجع)\b"
    )
    low_value_penalty: float = 0.3

    # ---- "is this result good enough?" thresholds --------------------------
    # coverage = share of the query's important terms (weighted by rarity)
    # that appear somewhere in the document. 0..1.
    min_coverage: float = 0.4         # below this a document is not returned at all
    relative_score_cutoff: float = 0.35  # drop docs scoring < 35% of the best doc
    strong_match_coverage: float = 0.6   # label "strong" at/above this coverage
    partial_match_coverage: float = 0.4  # label "partial" at/above this; else "weak"

    # ---- optional semantic search (only used if a retriever is plugged in) --
    semantic_min_similarity: float = 0.35  # ignore semantic hits below this cosine similarity
    rrf_k: int = 60                        # standard Reciprocal Rank Fusion constant

    # ---- duplicate detection (done once, at index time) --------------------
    duplicate_similarity: float = 0.8   # estimated text overlap to call two files duplicates
    fingerprint_size: int = 128         # bigger = more accurate, slower indexing

    # ---- snippets ----------------------------------------------------------
    snippet_tokens: int = 40
    highlight_start: str = "**"         # e.g. "<mark>" for an HTML frontend
    highlight_end: str = "**"

    # ---- language resources -------------------------------------------------
    stopwords: set = field(default_factory=lambda: set(DEFAULT_STOPWORDS))
    query_meta_words: set = field(default_factory=lambda: set(DEFAULT_QUERY_META_WORDS))
    negation_words: set = field(default_factory=lambda: set(DEFAULT_NEGATION_WORDS))
    synonyms: Dict[str, List[str]] = field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_SYNONYMS.items()})

    # ---- CSV metadata --------------------------------------------------------
    csv_columns: CsvColumnMap = field(default_factory=CsvColumnMap)

    def __post_init__(self) -> None:
        self.index_path = Path(self.index_path)
