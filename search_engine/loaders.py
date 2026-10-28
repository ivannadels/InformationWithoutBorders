"""
Simple loaders that turn files on disk into Document objects.

If another team member already extracts text, you do not need this file:
just create Document objects (or a JSONL file, see load_jsonl) and pass them
to the Indexer. These loaders exist so the search engine can be tested and
used on its own.

Supported formats:
  .txt .md        read as text
  .html .htm      tags removed with Python's built-in HTML parser
  .pdf            needs the optional `pypdf` package (pip install pypdf)
"""

from __future__ import annotations

import csv
import json
import logging
from collections import defaultdict
from urllib.parse import urlparse
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import CsvColumnMap, SearchConfig
from .html_extract import ExtractedPage, extract_html_page
from .models import Document

log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".txt", ".md", ".html", ".htm", ".pdf"}

# Column names we try automatically when CsvColumnMap leaves a field empty.
_FILE_CANDIDATES = ["file_path", "filepath", "path", "file", "filename", "file_name", "document", "url"]
_ID_CANDIDATES = ["doc_id", "document_id", "id", "identifier"]
_TITLE_CANDIDATES = ["title", "document_title", "name"]
_GROUP_CANDIDATES = ["group_id", "group", "canonical_id", "work_id"]


class LoadReport:
    """Collects human-readable warnings instead of crashing on one bad file."""

    def __init__(self) -> None:
        self.warnings: List[str] = []

    def warn(self, message: str) -> None:
        log.warning(message)
        self.warnings.append(message)


# ---------------------------------------------------------------------------
# Text extraction per format
# ---------------------------------------------------------------------------

def extract_html(raw: str) -> Tuple[str, str]:
    """Return (title, text) of a saved web page. See html_extract.py for details."""
    page = extract_html_page(raw)
    return page.title, page.text


def extract_pdf(path: Path) -> Tuple[str, str]:
    try:
        from pypdf import PdfReader  # optional dependency
    except ImportError as exc:
        raise RuntimeError("PDF support needs the 'pypdf' package: pip install pypdf") from exc
    reader = PdfReader(str(path))
    pages = [(page.extract_text() or "") for page in reader.pages]
    title = ""
    try:
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title)
    except Exception:  # broken metadata should not stop indexing
        pass
    return title.strip(), "\f".join(pages)   # \f = page break, used for page numbers


def read_text_file(path: Path) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def extract_file(path: Path) -> Tuple[str, str]:
    """Return (title_from_file_or_empty, text). Raises on unreadable files."""
    suffix = path.suffix.lower()
    if suffix in (".html", ".htm"):
        return extract_html(read_text_file(path))
    if suffix == ".pdf":
        return extract_pdf(path)
    return "", read_text_file(path)


# ---------------------------------------------------------------------------
# CSV metadata
# ---------------------------------------------------------------------------

def _pick(columns: List[str], explicit: Optional[str], candidates: List[str]) -> Optional[str]:
    if explicit:
        return explicit if explicit in columns else None
    lowered = {c.lower().strip(): c for c in columns}
    for name in candidates:
        if name in lowered:
            return lowered[name]
    return None


def load_csv_metadata(csv_path: Path, column_map: CsvColumnMap, report: LoadReport) -> Tuple[List[Dict[str, str]], Dict[str, Optional[str]]]:
    """Read the CSV. Returns (rows, resolved column names)."""
    text = read_text_file(Path(csv_path))
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    columns = reader.fieldnames or []
    resolved = {
        "file": _pick(columns, column_map.file_column, _FILE_CANDIDATES),
        "id": _pick(columns, column_map.id_column, _ID_CANDIDATES),
        "title": _pick(columns, column_map.title_column, _TITLE_CANDIDATES),
        "group": _pick(columns, column_map.group_column, _GROUP_CANDIDATES),
    }
    for key, explicit in (("file", column_map.file_column), ("id", column_map.id_column),
                          ("title", column_map.title_column), ("group", column_map.group_column)):
        if explicit and resolved[key] is None:
            report.warn(f"CSV column '{explicit}' (configured as {key} column) not found. Columns are: {columns}")
    if resolved["file"] is None and resolved["id"] is None:
        report.warn(f"Could not find a file-name or id column in the CSV (columns: {columns}). "
                    "Set SearchConfig.csv_columns.file_column.")
    rows = []
    for row in reader:
        rows.append({(k or "").strip(): (v or "").strip() for k, v in row.items() if k is not None})
    return rows, resolved


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------

def load_corpus(docs_dir: Path, csv_path: Optional[Path] = None,
                config: Optional[SearchConfig] = None,
                extensions: Optional[List[str]] = None) -> Tuple[List[Document], LoadReport]:
    """
    Walk `docs_dir`, extract text from every supported file, and attach CSV
    metadata where a CSV row matches the file (by relative path, file name,
    or file name without extension).

    extensions  Only load these formats, e.g. ["html", "htm"]. Default: all supported.

    HTML pages also get metadata read from the page itself (URL, language,
    dates, description, breadcrumb). CSV values win when both exist.
    """
    config = config or SearchConfig()
    docs_dir = Path(docs_dir)
    report = LoadReport()
    if not docs_dir.is_dir():
        raise FileNotFoundError(f"Documents folder not found: {docs_dir}")
    wanted = SUPPORTED_EXTENSIONS
    if extensions:
        wanted = {("." + e.lower().lstrip(".")) for e in extensions} & SUPPORTED_EXTENSIONS

    rows: List[Dict[str, str]] = []
    cols: Dict[str, Optional[str]] = {"file": None, "id": None, "title": None, "group": None}
    if csv_path:
        if Path(csv_path).is_file():
            rows, cols = load_csv_metadata(Path(csv_path), config.csv_columns, report)
        else:
            report.warn(f"Metadata CSV not found: {csv_path} (continuing without metadata)")

    # Build lookup tables: several keys per row so "guide.pdf", "docs/guide.pdf"
    # and "guide" can all find the same row.
    row_lookup: Dict[str, Dict[str, str]] = {}
    for row in rows:
        for col in (cols["file"], cols["id"]):
            value = row.get(col or "", "")
            if value:
                p = Path(value.replace("\\", "/"))
                for key in {value.lower(), p.name.lower(), p.stem.lower()}:
                    row_lookup.setdefault(key, row)
    used_rows: set = set()

    # ---- pass 1: extract text from every file -------------------------------
    extracted = []   # (path, rel, row, title, text, page_metadata, breadcrumb)
    files = sorted(p for p in docs_dir.rglob("*") if p.is_file() and p.suffix.lower() in wanted)
    for path in files:
        rel = path.relative_to(docs_dir).as_posix()
        row = (row_lookup.get(rel.lower()) or row_lookup.get(path.name.lower())
               or row_lookup.get(path.stem.lower()))
        if row is not None:
            used_rows.add(id(row))
        try:
            if path.suffix.lower() in (".html", ".htm"):
                page = extract_html_page(read_text_file(path))
                extracted.append([path, rel, row, page.title, page.text, page.metadata, page.breadcrumb])
            else:
                file_title, text = extract_file(path)
                extracted.append([path, rel, row, file_title, text, {}, []])
        except Exception as exc:
            report.warn(f"Could not read {rel}: {exc}")

    # ---- pass 2: remove each website's repeated furniture ------------------------
    removed = remove_repeated_lines(extracted)
    if removed:
        report.warn(f"Removed {removed} line(s) of website boilerplate repeated across pages of the same site")

    # ---- pass 3: build Document objects ------------------------------------------------
    documents: List[Document] = []
    for path, rel, row, file_title, text, page_meta, breadcrumb in extracted:
        url = page_meta.get("url", "")
        if url and urlparse(url).path.strip("/") == "" and not urlparse(url).query:
            report.warn(f"Skipped {rel}: it is the home page of {urlparse(url).netloc}, "
                        "not a document (the download was probably redirected)")
            continue
        if not text.strip():
            report.warn(f"No text extracted from {rel} (empty file, scanned PDF, "
                        "or a web page whose content is loaded by JavaScript)")
            continue

        doc_id = rel
        title = ""
        group_key = None
        keyword_parts: List[str] = list(breadcrumb)
        metadata: Dict[str, str] = dict(page_meta)
        if row is not None:
            metadata.update({k: v for k, v in row.items() if v})
            if cols["id"] and row.get(cols["id"]):
                doc_id = row[cols["id"]]
                # Same id on a PDF and an HTML file = same document, two formats.
                group_key = f"id:{doc_id}"
            if cols["title"]:
                title = row.get(cols["title"], "")
            if cols["group"] and row.get(cols["group"]):
                group_key = f"group:{row[cols['group']]}"
            keyword_parts += [row.get(c, "") for c in config.csv_columns.keywords_columns]
        elif rows:
            report.warn(f"No CSV row for {rel} (indexed with file-name title)")

        if group_key is None:
            # Same folder + same name, different extension -> same document.
            group_key = "path:" + path.with_suffix("").relative_to(docs_dir).as_posix().lower()

        if not title:
            title = file_title or _first_line_title(text) or path.stem.replace("_", " ")
            # Guideline chapters like "Appendices" or "Preface" mean nothing on
            # their own: add the book they belong to (first breadcrumb item).
            book = breadcrumb[0] if breadcrumb else ""
            if book and book.lower() not in title.lower() and title.lower() not in book.lower():
                metadata.setdefault("page_title", title)
                title = f"{title} ({book})"

        documents.append(Document(
            doc_id=doc_id, title=title, text=text, source_path=str(path),
            file_format=path.suffix.lower().lstrip("."), metadata=metadata,
            keywords=" ".join(k for k in keyword_parts if k), group_key=group_key,
        ))

    # If one CSV id covers several files (e.g. guide.pdf and guide.html),
    # make each doc_id unique by adding the format: "MSF-001:pdf".
    id_counts: Dict[str, int] = {}
    for doc in documents:
        id_counts[doc.doc_id] = id_counts.get(doc.doc_id, 0) + 1
    for doc in documents:
        if id_counts[doc.doc_id] > 1:
            doc.doc_id = f"{doc.doc_id}:{doc.file_format}"

    unmatched = [row.get(cols["file"] or "", "") or row.get(cols["id"] or "", "") or "?"
                 for row in rows if id(row) not in used_rows]
    if unmatched:
        shown = ", ".join(unmatched[:10]) + (" …" if len(unmatched) > 10 else "")
        report.warn(f"{len(unmatched)} CSV row(s) have no matching file in {docs_dir}"
                    f"{' (with formats ' + ', '.join(sorted(wanted)) + ')' if extensions else ''}: {shown}")
    return documents, report


def remove_repeated_lines(extracted: list, min_share: float = 0.5, min_pages: int = 3) -> int:
    """
    Remove lines that appear on at least `min_share` of one website's pages
    (and on at least `min_pages` pages): menus, footers, "latest news" lists...
    Headings and table rows are never removed, because they carry structure.
    Also drops page descriptions that are identical across a site.
    Works in place on the `extracted` list; returns the number of lines removed.
    """
    by_site = defaultdict(list)
    for item in extracted:
        site = item[5].get("site")
        if site:
            by_site[site].append(item)
    removed = 0
    for site, items in by_site.items():
        if len(items) < min_pages:
            continue
        threshold = max(min_pages, min_share * len(items))
        line_pages: Dict[str, int] = defaultdict(int)
        desc_pages: Dict[str, int] = defaultdict(int)
        for item in items:
            for line in {l.strip() for l in item[4].splitlines() if l.strip()}:
                line_pages[line] += 1
            if item[5].get("description"):
                desc_pages[item[5]["description"]] += 1
        repeated = {line for line, n in line_pages.items()
                    if n >= threshold and not line.startswith("#") and " | " not in line}
        for item in items:
            kept_blocks = []
            for block in item[4].split("\n\n"):
                lines = block.split("\n")
                kept = [l for l in lines if l.strip() not in repeated]
                removed += len(lines) - len(kept)
                if kept:
                    kept_blocks.append("\n".join(kept))
            # drop headings left with nothing under them at the end of the page
            while kept_blocks and kept_blocks[-1].startswith("#"):
                kept_blocks.pop()
            item[4] = "\n\n".join(kept_blocks)
            if desc_pages.get(item[5].get("description", ""), 0) >= threshold:
                item[5].pop("description", None)
    return removed


def _first_line_title(text: str) -> str:
    for line in text.splitlines():
        line = line.strip().strip("#").strip()
        if line:
            return line if len(line) <= 120 else ""
    return ""


def load_jsonl(path: Path) -> Tuple[List[Document], LoadReport]:
    """
    Load documents prepared by another component. One JSON object per line:

      {"doc_id": "...", "title": "...", "text": "...",
       "source_path": "...", "file_format": "pdf",
       "metadata": {...}, "keywords": "...", "group_key": "..."}

    Only doc_id and text are required.
    """
    report = LoadReport()
    documents: List[Document] = []
    allowed = set(Document.__dataclass_fields__)
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                data = {k: v for k, v in data.items() if k in allowed}
                data.setdefault("title", data.get("doc_id", ""))
                documents.append(Document(**data))
            except (json.JSONDecodeError, TypeError) as exc:
                report.warn(f"{path}:{line_no}: skipped malformed record ({exc})")
    return documents, report
