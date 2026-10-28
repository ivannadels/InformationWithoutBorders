"""
Text helpers: normalisation, query analysis, passage splitting and
duplicate fingerprints.

Important design rule: we never throw away the user's original text. We build
a *search* version of the query, but the original, the ignored words and any
detected negations are all returned so the UI can show them.
"""

from __future__ import annotations

import re
import unicodedata
import zlib
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .config import SearchConfig
from .models import QueryAnalysis

# Same idea as SQLite's "unicode61" tokenizer: runs of letters/digits.
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Clause boundaries: a negation stops applying after these.
_CLAUSE_SPLIT_RE = re.compile(r"[.,;:!?()\[\]\n]|\bbut\b|\bhowever\b|\bwhereas\b")
_MAX_NEGATED_WORDS = 3   # "no signs of dehydration" -> signs, dehydration


def normalize_text(text: str) -> str:
    """Lowercase, unify unicode (é -> e, ﬁ -> fi) and collapse whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> List[str]:
    """Split normalised text into word tokens (letters and digits only)."""
    return _TOKEN_RE.findall(normalize_text(text))


def _is_searchable(token: str, config: SearchConfig) -> bool:
    if token in config.stopwords or token in config.query_meta_words:
        return False
    if token in config.negation_words:
        return False
    # Single letters are noise, but keep numbers ("5 years", "3 days").
    return len(token) >= 2 or token.isdigit()


def _fts_quote(term: str) -> str:
    """Quote a term for FTS5 so words like 'and'/'not'/'near' are never read as operators."""
    return '"' + term.replace('"', '""') + '"'


def analyze_query(query: str, config: SearchConfig) -> QueryAnalysis:
    """
    Turn a free-text description into:
      * a list of search terms (unique, in the order the user wrote them)
      * synonym / spelling-variant expansions
      * negated terms (reported, not hidden)
      * an FTS5 query string

    The FTS5 query is an OR of "concepts"; each concept is (term OR variants).
    OR is deliberate: long descriptions rarely have every word in one
    document. BM25 already rewards documents matching more and rarer terms,
    and the search step adds a coverage check so documents that match only one
    common word are filtered out.
    """
    original = query or ""
    normalized = normalize_text(original)

    # --- detect negations clause by clause ---------------------------------
    negated: List[str] = []
    for clause in _CLAUSE_SPLIT_RE.split(normalized):
        tokens = _TOKEN_RE.findall(clause)
        remaining = 0
        for tok in tokens:
            if tok in config.negation_words:
                remaining = _MAX_NEGATED_WORDS
                continue
            if remaining and _is_searchable(tok, config):
                if tok not in negated:
                    negated.append(tok)
                remaining -= 1

    # --- choose search terms ------------------------------------------------
    search_terms: List[str] = []
    ignored: List[str] = []
    for tok in _TOKEN_RE.findall(normalized):
        if _is_searchable(tok, config):
            if tok not in search_terms:
                search_terms.append(tok)
        elif tok not in ignored:
            ignored.append(tok)

    # --- synonyms / spelling variants --------------------------------------
    expanded: Dict[str, List[str]] = {}
    for term in search_terms:
        variants = [v for v in config.synonyms.get(term, []) if v != term]
        if variants:
            expanded[term] = variants

    fts_query = build_fts_query(term_groups(search_terms, expanded))

    return QueryAnalysis(
        original_query=original,
        normalized_query=normalized,
        search_terms=search_terms,
        expanded_terms=expanded,
        negated_terms=negated,
        ignored_words=ignored,
        fts_query=fts_query,
    )


def term_groups(search_terms: Sequence[str], expanded: Dict[str, List[str]]) -> List[List[str]]:
    """One group per concept: [term, variant1, variant2, ...]."""
    groups = []
    for term in search_terms:
        group = [term]
        for variant in expanded.get(term, []):
            # Multi-word synonyms are split into words; each must be searchable.
            for word in tokenize(variant):
                if word not in group:
                    group.append(word)
        groups.append(group)
    return groups


def group_to_fts(group: Sequence[str]) -> str:
    if len(group) == 1:
        return _fts_quote(group[0])
    return "(" + " OR ".join(_fts_quote(t) for t in group) + ")"


def build_fts_query(groups: Sequence[Sequence[str]]) -> str:
    return " OR ".join(group_to_fts(g) for g in groups)


# ---------------------------------------------------------------------------
# Passage splitting
# ---------------------------------------------------------------------------

@dataclass
class TextChunk:
    index: int
    page: Optional[int]
    text: str
    section: str = ""     # heading path, e.g. "Treatment > Children"


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_MIN_SECTION_WORDS = 40   # don't start a new passage for a heading if the current one is tiny


def _split_paragraphs(page_text: str) -> List[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", page_text) if p.strip()]
    if len(paragraphs) <= 1 and page_text.count("\n") > 3 and " | " not in page_text:
        # PDF extraction often gives single newlines only: fall back to
        # sentences.
        joined = re.sub(r"\s*\n\s*", " ", page_text)
        paragraphs = [s.strip() for s in re.split(r"(?<=[.!?])\s+", joined) if s.strip()]
    return paragraphs


def _split_long(paragraph: str, maximum: int, overlap: int) -> List[str]:
    """Cut a paragraph that is too long: by lines first (keeps table rows whole), then by words."""
    lines = paragraph.split("\n")
    if len(lines) > 1 and all(len(l.split()) <= maximum for l in lines):
        pieces, current, count = [], [], 0
        header = lines[0] if " | " in lines[0] else None   # repeat a table's header row
        for line in lines:
            n = len(line.split())
            if current and count + n > maximum:
                pieces.append("\n".join(current))
                current, count = ([header], len(header.split())) if header else ([], 0)
            current.append(line)
            count += n
        if current:
            pieces.append("\n".join(current))
        return pieces
    words = paragraph.split()
    step = max(maximum - overlap, 1)
    pieces = []
    for start in range(0, len(words), step):
        pieces.append(" ".join(words[start:start + maximum]))
        if start + maximum >= len(words):
            break
    return pieces


def split_into_passages(text: str, config: SearchConfig) -> List[TextChunk]:
    """
    Split a document into passages of roughly `passage_target_words` words.

    * Paragraphs are kept together where possible.
    * Markdown headings ("## Treatment") start a new passage, and every
      passage remembers the headings it sits under ("Treatment > Children").
      A level-1 heading is the document title and is not repeated.
    * Long tables are cut between rows, repeating the header row.
    * Form-feed characters ("\\f") mark page breaks; passages never cross a
      page so page numbers stay correct.
    """
    chunks: List[TextChunk] = []
    pages = text.split("\f")
    has_pages = len(pages) > 1
    target, maximum = config.passage_target_words, config.passage_max_words
    overlap = min(config.passage_overlap_words, maximum // 2)
    headings: List[Tuple[int, str]] = []      # current heading stack: (level, text)

    def section_path() -> str:
        return " > ".join(t for level, t in headings if level > 1)

    for page_no, page_text in enumerate(pages, start=1):
        page = page_no if has_pages else None
        buffer: List[str] = []
        buffer_words = 0
        buffer_section = section_path()

        def flush() -> None:
            nonlocal buffer, buffer_words
            if buffer:
                chunks.append(TextChunk(len(chunks), page, "\n\n".join(buffer), buffer_section))
            buffer, buffer_words = [], 0

        for para in _split_paragraphs(page_text):
            heading = _HEADING_RE.match(para) if "\n" not in para else None
            if heading:
                level, title = len(heading.group(1)), heading.group(2).strip()
                if buffer_words >= _MIN_SECTION_WORDS:
                    flush()
                headings = [h for h in headings if h[0] < level] + [(level, title)]
                if all(_HEADING_RE.match(p) for p in buffer):
                    # nothing but headings so far: the passage belongs to the newest one
                    buffer_section = section_path()
                buffer.append(para)
                buffer_words += len(para.split())
                continue
            words = len(para.split())
            if words > maximum:
                flush()
                for piece in _split_long(para, maximum, overlap):
                    chunks.append(TextChunk(len(chunks), page, piece, section_path()))
                buffer_section = section_path()
                continue
            if buffer_words + words > maximum or buffer_words >= target:
                flush()
                buffer_section = section_path()
            if not buffer:
                buffer_section = section_path()
            buffer.append(para)
            buffer_words += words
        flush()
    # A passage made only of headings carries no information on its own.
    chunks = [c for c in chunks if not all(_HEADING_RE.match(p) for p in c.text.split("\n\n"))]
    for i, c in enumerate(chunks):
        c.index = i
    return chunks


# ---------------------------------------------------------------------------
# Duplicate detection: "bottom-k" sketch of 5-word shingles
# ---------------------------------------------------------------------------
# Two files of the same document (PDF vs HTML) rarely have byte-identical
# text: line breaks, headers and hyphenation differ. Instead we compare
# overlapping 5-word sequences. Storing only the k smallest hashes keeps the
# fingerprint small while still estimating overlap well.

_SHINGLE_SIZE = 5


def fingerprint(text: str, size: int) -> List[int]:
    tokens = tokenize(text)
    if not tokens:
        return []
    if len(tokens) < _SHINGLE_SIZE:
        shingles: Iterable[str] = [" ".join(tokens)]
    else:
        shingles = (" ".join(tokens[i:i + _SHINGLE_SIZE])
                    for i in range(len(tokens) - _SHINGLE_SIZE + 1))
    hashes = {zlib.crc32(s.encode("utf-8")) for s in shingles}
    return sorted(hashes)[:size]


def estimate_similarity(a: Sequence[int], b: Sequence[int], size: int) -> float:
    """Estimated Jaccard similarity (0..1) of the two texts' shingle sets."""
    if not a or not b:
        return 0.0
    set_a, set_b = set(a), set(b)
    union_sketch = sorted(set_a | set_b)[:size]
    both = sum(1 for h in union_sketch if h in set_a and h in set_b)
    return both / len(union_sketch)
