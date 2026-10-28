"""
Indexing: turn a list of Documents into one local SQLite file.

Run this ONCE (or whenever the document collection changes). Searching then
only reads this file; it never reopens the original PDFs/HTML files.

Tables inside the index file:
  meta       key/value info (schema version, build time, document count)
  documents  one row per source file: title, path, format, metadata (JSON),
             duplicate-group id
  passages   SQLite FTS5 full-text table: one row per passage of a document,
             with the title, metadata keywords and section headings repeated
             so they count in BM25 ranking
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .config import SearchConfig
from .models import Document
from .text_utils import estimate_similarity, fingerprint, split_into_passages

log = logging.getLogger(__name__)

SCHEMA_VERSION = "2"

# porter     = English stemming (treat, treating, treated -> treat)
# unicode61  = Unicode-aware word splitting
# remove_diacritics 2 = "œdème" matches "oedeme"
FTS_TOKENIZER = "porter unicode61 remove_diacritics 2"


@dataclass
class IndexStats:
    documents_indexed: int = 0
    passages_indexed: int = 0
    duplicate_groups: int = 0           # groups containing 2+ files
    skipped: List[Dict[str, str]] = field(default_factory=list)  # [{doc_id, reason}]
    seconds: float = 0.0
    index_path: str = ""


class Indexer:
    def __init__(self, config: Optional[SearchConfig] = None):
        self.config = config or SearchConfig()

    def build(self, documents: Iterable[Document], extra_meta: Optional[Dict[str, str]] = None) -> IndexStats:
        """
        Build a fresh index from `documents` and atomically replace the old one.

        The new index is written to a temporary file first, so an application
        that is searching the old index is never left with a half-built file.
        """
        started = time.perf_counter()
        target = self.config.index_path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target.with_name(target.name + ".building")
        if tmp_path.exists():
            tmp_path.unlink()

        stats = IndexStats(index_path=str(target))
        conn = sqlite3.connect(tmp_path)
        try:
            self._create_schema(conn)
            self._insert_all(conn, documents, stats)
            conn.execute("INSERT INTO passages(passages) VALUES('optimize')")
            conn.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                [("schema_version", SCHEMA_VERSION),
                 ("built_at", time.strftime("%Y-%m-%dT%H:%M:%S")),
                 ("document_count", str(stats.documents_indexed)),
                 ("passage_count", str(stats.passages_indexed))]
                + [(str(k), str(v)) for k, v in (extra_meta or {}).items()],
            )
            conn.commit()
        finally:
            conn.close()

        os.replace(tmp_path, target)
        stats.seconds = round(time.perf_counter() - started, 3)
        log.info("Indexed %d documents (%d passages) in %.2fs",
                 stats.documents_indexed, stats.passages_indexed, stats.seconds)
        return stats

    def update(self, add: Iterable[Document], remove: Iterable[str] = (),
               extra_meta: Optional[Dict[str, str]] = None) -> IndexStats:
        """
        Add and remove documents in the existing index without rebuilding it:
        seconds instead of minutes when p2p sync brings a few documents.
        Added documents are not compared with the existing ones for duplicate
        groups; a full build() does that.
        """
        started = time.perf_counter()
        stats = IndexStats(index_path=str(self.config.index_path))
        conn = sqlite3.connect(self.config.index_path, timeout=30)
        try:
            gone = [(str(d),) for d in remove]
            conn.executemany("DELETE FROM passages WHERE doc_id = ?", gone)
            conn.executemany("DELETE FROM documents WHERE doc_id = ?", gone)
            self._insert_all(conn, add, stats)
            documents = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            passages = conn.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
            conn.executemany(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                [("document_count", str(documents)), ("passage_count", str(passages))]
                + [(str(k), str(v)) for k, v in (extra_meta or {}).items()],
            )
            conn.commit()
        finally:
            conn.close()
        stats.seconds = round(time.perf_counter() - started, 3)
        log.info("Updated index: +%d documents, -%d, in %.2fs", stats.documents_indexed, len(gone), stats.seconds)
        return stats

    # ------------------------------------------------------------------
    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(f"""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE documents (
                doc_id        TEXT PRIMARY KEY,
                title         TEXT NOT NULL,
                source_path   TEXT,
                file_format   TEXT,
                metadata_json TEXT,
                group_id      TEXT NOT NULL,
                word_count    INTEGER,
                passage_count INTEGER
            );
            CREATE INDEX documents_group ON documents(group_id);
            CREATE VIRTUAL TABLE passages USING fts5(
                doc_id UNINDEXED,
                passage_index UNINDEXED,
                page UNINDEXED,
                title,
                keywords,
                section,
                body,
                tokenize = '{FTS_TOKENIZER}'
            );
        """)

    def _insert_all(self, conn: sqlite3.Connection, documents: Iterable[Document],
                    stats: IndexStats) -> None:
        cfg = self.config
        seen_ids: set = set()
        group_by_key: Dict[str, str] = {}              # explicit group_key -> group_id
        sketches: List[tuple] = []                       # (group_id, fingerprint, language)
        group_sizes: Dict[str, int] = {}

        for doc in documents:
            problem = self._validate(doc)
            if problem:
                stats.skipped.append({"doc_id": str(getattr(doc, "doc_id", "?")), "reason": problem})
                log.warning("Skipping %s: %s", getattr(doc, "doc_id", "?"), problem)
                continue

            doc_id = str(doc.doc_id)
            if doc_id in seen_ids:
                # Keep both files but make the id unique, and say so.
                n = 2
                while f"{doc_id}#{n}" in seen_ids:
                    n += 1
                log.warning("Duplicate doc_id %r renamed to %r", doc_id, f"{doc_id}#{n}")
                doc_id = f"{doc_id}#{n}"
            seen_ids.add(doc_id)

            # ---- which duplicate group does this file belong to? ----------
            sketch = fingerprint(doc.text, cfg.fingerprint_size)
            language = str((doc.metadata or {}).get("language", "")).lower()
            group_id = None
            if doc.group_key:
                group_id = group_by_key.get(doc.group_key)
            if group_id is None:
                for other_group, other_sketch, other_language in sketches:
                    # Translations are different documents for the reader, even
                    # if they share a lot of text (e.g. reference lists).
                    if language and other_language and language != other_language:
                        continue
                    if estimate_similarity(sketch, other_sketch, cfg.fingerprint_size) >= cfg.duplicate_similarity:
                        group_id = other_group
                        break
            if group_id is None:
                group_id = doc_id
            if doc.group_key:
                group_by_key.setdefault(doc.group_key, group_id)
            sketches.append((group_id, sketch, language))
            group_sizes[group_id] = group_sizes.get(group_id, 0) + 1

            # ---- passages ---------------------------------------------------
            title = (doc.title or "").strip() or doc_id
            chunks = split_into_passages(doc.text, cfg)
            conn.executemany(
                "INSERT INTO passages(doc_id, passage_index, page, title, keywords, section, body) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(doc_id, c.index, c.page, title, doc.keywords or "", c.section, c.text) for c in chunks],
            )
            conn.execute(
                "INSERT INTO documents(doc_id, title, source_path, file_format, metadata_json, "
                "group_id, word_count, passage_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (doc_id, title, doc.source_path or "", (doc.file_format or "").lower(),
                 json.dumps(doc.metadata or {}, ensure_ascii=False, default=str),
                 group_id, len(doc.text.split()), len(chunks)),
            )
            stats.documents_indexed += 1
            stats.passages_indexed += len(chunks)

        stats.duplicate_groups = sum(1 for size in group_sizes.values() if size > 1)

    @staticmethod
    def _validate(doc: Document) -> Optional[str]:
        if not isinstance(doc, Document):
            return f"not a Document object ({type(doc).__name__})"
        if not doc.doc_id or not str(doc.doc_id).strip():
            return "missing doc_id"
        if not isinstance(doc.text, str) or not doc.text.strip():
            return "no extractable text (empty file, or a scanned PDF that needs OCR)"
        return None


def build_index(documents: Iterable[Document], config: Optional[SearchConfig] = None) -> IndexStats:
    """Shortcut: build an index with the given (or default) settings."""
    return Indexer(config).build(documents)
