"""
Connection to the team's "InformationWithoutBorders" database.

That project (index_documents.py + p2p/) keeps every document as a row of an
SQLite FTS5 table:

    search_index.db
      docs_index(doc_id, genre, language, filepath, html_content)

and the p2p module copies rows between devices, so a device can have rows
whose original HTML file it never saw. This module:

  * reads that table (read-only: their database is never modified),
  * for each row, uses the original HTML file when it exists on this device
    (clean text with headings, sections and tables), and otherwise the text
    stored in the row (what arrived through sync),
  * builds this engine's own index in a separate file next to their database
    (named *.db so their .gitignore already ignores it),
  * rebuilds that index automatically when p2p sync adds or removes rows,
  * offers `legacy_rows()`, shaped like the rows their search.py web page shows.

Use:

    from search_engine.iwb import IWBSearch
    engine = IWBSearch("search_index.db")                 # builds the index the first time
    response = engine.search("Adult with a cough for several weeks, fever and weight loss")
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import SearchConfig
from .html_extract import extract_html_page
from .indexer import Indexer, IndexStats
from .loaders import LoadReport, read_text_file, remove_repeated_lines
from .models import Document, SearchResponse
from .search import SearchEngine

log = logging.getLogger(__name__)

TABLE = "docs_index"
KEY = "doc_id"
TEXT_COLUMN = "html_content"
SIGNATURE_KEY = "source_signature"
# Bump when the way documents are read or cleaned changes, so existing indexes
# are rebuilt even though the database itself did not change.
PIPELINE_VERSION = "iwb-3"


# ---------------------------------------------------------------------------
# Reading their database
# ---------------------------------------------------------------------------

def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    # timeout: p2p sync may be writing at the same moment
    return sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)


def database_signature(db_path: Path, table: str = TABLE) -> str:
    """
    A short fingerprint of which documents the database holds. It changes
    when p2p sync adds a row or the admin's removal purges one.
    """
    conn = _connect_read_only(db_path)
    try:
        rows = conn.execute(f'SELECT "{KEY}", length("{TEXT_COLUMN}") FROM {table} ORDER BY 1').fetchall()
    finally:
        conn.close()
    digest = hashlib.sha256()
    for doc_id, length in rows:
        digest.update(f"{doc_id}\x1f{length}\x1e".encode("utf-8", "replace"))
    return f"{PIPELINE_VERSION}:{len(rows)}:{digest.hexdigest()[:16]}"


_FLAT_TITLE_RE = re.compile(r"^(.{3,200}?)\s+(?:\|\s|Skip to main content|Aller au contenu|Ir al contenido|Menu\b)")


def _title_from_flat_text(text: str, fallback: str) -> str:
    """Their stored text starts with the page's <title> ("AMOXICILLIN oral | MSF Medical Guidelines ...")."""
    match = _FLAT_TITLE_RE.match(text.strip())
    if match:
        return match.group(1).strip()
    words = text.split()
    return " ".join(words[:12]) if words else fallback


def load_iwb_database(db_path: Path, files_dir: Optional[Path] = None,
                      table: str = TABLE) -> Tuple[List[Document], LoadReport]:
    """
    Turn the rows of their `docs_index` table into Documents.

    db_path    their search_index.db
    files_dir  optional folder with the original files named <doc_id>.html, used
               when a row's own `filepath` does not exist on this device
    """
    db_path = Path(db_path)
    report = LoadReport()
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")
    conn = _connect_read_only(db_path)
    conn.row_factory = sqlite3.Row
    try:
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        missing = {KEY, TEXT_COLUMN} - columns
        if missing:
            raise ValueError(f"Table {table} in {db_path} has no column(s) {sorted(missing)}; found {sorted(columns)}")
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    finally:
        conn.close()

    base = db_path.resolve().parent       # their filepath is relative to the project folder
    extracted: list = []                  # same layout as loaders.load_corpus, for remove_repeated_lines
    meta_by_index: List[Dict[str, Any]] = []
    seen: set = set()
    from_file = from_db = 0
    for row in rows:
        data = dict(row)
        doc_id = str(data.get(KEY) or "").strip()
        if not doc_id:
            report.warn("Skipped a row without doc_id")
            continue
        if doc_id in seen:
            # Running their index_documents.py twice inserts every row again.
            report.warn(f"Duplicate rows for doc_id {doc_id}: kept the first")
            continue
        seen.add(doc_id)

        extra = {k: str(v) for k, v in data.items()
                 if k not in (KEY, TEXT_COLUMN, "filepath") and v not in (None, "", "nan")}
        stored_text = str(data.get(TEXT_COLUMN) or "")

        candidates = []
        if data.get("filepath"):
            fp = Path(str(data["filepath"]))
            candidates.append(fp if fp.is_absolute() else base / fp)
        if files_dir:
            candidates.append(Path(files_dir) / f"{doc_id}.html")
        html_file = next((p for p in candidates if p.is_file() and p.suffix.lower() in (".html", ".htm")), None)

        if html_file is not None:
            try:
                page = extract_html_page(read_text_file(html_file))
                page_meta = {**page.metadata, **extra, "text_source": "html_file"}
                extracted.append([html_file, doc_id, None, page.title, page.text, page_meta, page.breadcrumb])
                meta_by_index.append({"source_path": str(html_file)})
                from_file += 1
                continue
            except Exception as exc:  # fall back to the stored text
                report.warn(f"Could not re-read {html_file} ({exc}); using the text stored in the database")
        title = _title_from_flat_text(stored_text, doc_id)
        page_meta = {**extra, "text_source": "database"}
        extracted.append([None, doc_id, None, title, stored_text, page_meta, []])
        meta_by_index.append({"source_path": str(data.get("filepath") or "")})
        from_db += 1

    # Boilerplate removal: per site and per line for text re-read from HTML files,
    # by repeated word runs for flat text stored in the database.
    removed = remove_repeated_lines([item for item in extracted if item[0] is not None])
    if removed:
        report.warn(f"Removed {removed} line(s) of website boilerplate repeated across pages of the same site")
    flat = [item for item in extracted if item[0] is None]
    if len(flat) >= 5:
        cleaned, removed_words = remove_repeated_word_runs([item[4] for item in flat])
        for item, text in zip(flat, cleaned):
            item[4] = text
        if removed_words:
            report.warn(f"Removed {removed_words} words of website menus/footers repeated across "
                        "the database's stored texts")
    if from_db:
        report.warn(f"{from_db} document(s) use the flat text stored in the database (original HTML file not on "
                    f"this device): no headings/tables, and website menus are still in the text")

    documents: List[Document] = []
    for item, extra_info in zip(extracted, meta_by_index):
        path, doc_id, _, title, text, page_meta, breadcrumb = item
        url = page_meta.get("url", "")
        if url and re.fullmatch(r"https?://[^/]+/?", url):
            report.warn(f"Skipped {doc_id}: it is the home page of {url}, not a document")
            continue
        if not text.strip():
            report.warn(f"Skipped {doc_id}: no text")
            continue
        book = breadcrumb[0] if breadcrumb else ""
        if book and book.lower() not in title.lower() and title.lower() not in book.lower():
            page_meta.setdefault("page_title", title)
            title = f"{title} ({book})"
        documents.append(Document(
            doc_id=doc_id, title=title or doc_id, text=text,
            source_path=extra_info["source_path"], file_format="html",
            metadata=page_meta, keywords=" ".join(breadcrumb + [page_meta.get("genre", "")]).strip(),
        ))
    log.info("Loaded %d documents from %s (%d from HTML files, %d from stored text)",
             len(documents), db_path, from_file, from_db)
    return documents, report


def remove_repeated_word_runs(texts: List[str], run_words: int = 8, min_share: float = 0.05,
                              min_docs: int = 5) -> Tuple[List[str], int]:
    """
    Boilerplate removal for flat text (no line breaks), e.g. what BeautifulSoup's
    get_text() stores: any run of `run_words` consecutive words that appears in
    at least `min_share` of all documents (and in at least `min_docs`) is site
    furniture (menus, banners, footers) and is cut out.
    Returns (cleaned texts, number of words removed).
    """
    tokenised = [t.split() for t in texts]
    threshold = max(min_docs, min_share * len(texts))
    doc_freq: Dict[str, int] = {}
    for words in tokenised:
        for run in {" ".join(words[i:i + run_words]) for i in range(len(words) - run_words + 1)}:
            doc_freq[run] = doc_freq.get(run, 0) + 1
    common = {run for run, n in doc_freq.items() if n >= threshold}
    cleaned, removed = [], 0
    for words in tokenised:
        drop = [False] * len(words)
        for i in range(len(words) - run_words + 1):
            if " ".join(words[i:i + run_words]) in common:
                for j in range(i, i + run_words):
                    drop[j] = True
        removed += sum(drop)
        cleaned.append(" ".join(w for w, d in zip(words, drop) if not d))
    return cleaned, removed


# ---------------------------------------------------------------------------
# A search engine that follows the database
# ---------------------------------------------------------------------------

class IWBSearch:
    """
    Search over their database, with the index kept up to date.

    db_path      their search_index.db
    index_path   where this engine keeps its own index
                 (default: search_engine_index.db next to their database)
    files_dir    optional folder of original <doc_id>.html files
    check_every  seconds between checks for rows added/removed by p2p sync
                 (0 = check on every search)
    """

    def __init__(self, db_path: str | Path = "search_index.db", index_path: Optional[str | Path] = None,
                 files_dir: Optional[str | Path] = None, config: Optional[SearchConfig] = None,
                 check_every: float = 60.0):
        self.db_path = Path(db_path)
        self.files_dir = Path(files_dir) if files_dir else None
        self.config = config or SearchConfig()
        self.config.index_path = Path(index_path) if index_path else self.db_path.resolve().parent / "search_engine_index.db"
        self.check_every = check_every
        self.last_report: Optional[LoadReport] = None
        self.last_stats: Optional[IndexStats] = None
        self._engine: Optional[SearchEngine] = None
        self._last_check = 0.0
        self._lock = threading.RLock()
        self.refresh(force=False)

    # ---- index maintenance ----------------------------------------------------
    def _index_signature(self) -> Optional[str]:
        try:
            conn = sqlite3.connect(self.config.index_path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT value FROM meta WHERE key = ?", (SIGNATURE_KEY,)).fetchone()
            finally:
                conn.close()
            return row[0] if row else None
        except sqlite3.Error:
            return None

    def refresh(self, force: bool = False) -> bool:
        """Rebuild the index if the database changed (or if force=True). Returns True if rebuilt."""
        with self._lock:
            self._last_check = time.monotonic()
            current = database_signature(self.db_path)
            rebuilt = False
            try:
                if force or not self.config.index_path.is_file() or self._index_signature() != current:
                    documents, report = load_iwb_database(self.db_path, self.files_dir)
                    self.last_report = report
                    if self._engine is not None:
                        self._engine.close()    # Windows cannot replace a file that is still open
                        self._engine = None
                    self.last_stats = Indexer(self.config).build(documents, extra_meta={SIGNATURE_KEY: current,
                                                                                        "source_database": str(self.db_path)})
                    rebuilt = True
            finally:
                if self._engine is None and self.config.index_path.is_file():
                    self._engine = SearchEngine(config=self.config)   # the new index, or the old one if the build failed
            return rebuilt

    def _maybe_refresh(self) -> None:
        if time.monotonic() - self._last_check >= self.check_every:
            try:
                self.refresh()
            except (sqlite3.Error, OSError) as exc:   # database busy, or index file in use: keep the current index
                log.warning("Could not check the database for changes: %s", exc)

    # ---- searching ---------------------------------------------------------------
    def search(self, query: str, top_k: Optional[int] = None, filters: Optional[Dict[str, Any]] = None,
               min_coverage: Optional[float] = None) -> SearchResponse:
        self._maybe_refresh()
        with self._lock:                     # a rebuild closes the engine, so search while holding the lock
            return self._engine.search(query, top_k=top_k, filters=filters, min_coverage=min_coverage)

    def legacy_rows(self, query: str, top_k: Optional[int] = None) -> List[Dict[str, str]]:
        """
        Results shaped like the rows their search.py web page displays
        (it shows row["doc_id"] as the title, then genre · html_content).
        To use it there:  search = IWBSearch(DB).legacy_rows
        """
        response = self.search(query, top_k=top_k)
        rows = []
        for r in response.results:
            snippet = r.snippet.replace(self.config.highlight_start, "").replace(self.config.highlight_end, "")
            section = r.passages[0].section if r.passages else ""
            rows.append({
                "doc_id": f"{r.title}  [{r.doc_id}]",
                "genre": " · ".join(x for x in (r.metadata.get("genre", ""), r.metadata.get("language", ""),
                                                 f"{r.match_strength} match") if x),
                "html_content": (f"Section: {section} — " if section else "") + snippet,
                "filepath": r.source_path,
            })
        if not rows and response.message:
            rows.append({"doc_id": "No good match", "genre": "", "html_content": response.message, "filepath": ""})
        return rows

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return self._engine.stats()

    def close(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.close()
                self._engine = None
