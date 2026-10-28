import json
import sqlite3

import pytest

from search_engine import Document, Indexer, SearchConfig, load_corpus, load_jsonl
from search_engine.loaders import extract_html

from .conftest import SAMPLE_DIR


def test_sample_corpus_loads_with_clear_warnings(sample_index):
    report = sample_index["report"]
    docs = {d.doc_id: d for d in sample_index["documents"]}
    assert "snakebite.txt" in docs and docs["snakebite.txt"].metadata == {}   # no CSV row: still indexed
    assert docs["diarrhoea_children.txt"].metadata["publication_date"] == "2021-03-01"
    assert docs["diarrhoea_children.txt"].metadata["version"] == "2.1"
    assert any("empty_scan.txt" in w for w in report.warnings)       # empty file reported
    assert any("missing_file.pdf" in w for w in report.warnings)     # CSV row without file reported
    assert "empty_scan.txt" not in docs


def test_format_duplicates_and_near_copies_share_one_group(sample_index):
    conn = sqlite3.connect(sample_index["path"])
    groups = dict(conn.execute("SELECT doc_id, group_id FROM documents"))
    assert groups["diarrhoea_children.txt"] == groups["diarrhoea_children.html"]
    assert groups["acute_diarrhoea_copy_from_field_drive.txt"] == groups["diarrhoea_children.txt"]
    assert groups["cholera_outbreak.txt"] != groups["diarrhoea_children.txt"]
    assert sample_index["stats"].duplicate_groups == 1


def test_html_extraction_drops_scripts_navigation_and_styles():
    title, text = extract_html("<html><head><title>T</title><script>bad()</script></head>"
                               "<body><nav>menu</nav><p>Hello</p><p>World</p></body></html>")
    assert title == "T"
    assert "bad" not in text and "menu" not in text
    assert "Hello" in text and "World" in text


def test_indexer_skips_malformed_documents_and_renames_duplicate_ids(tmp_path):
    cfg = SearchConfig(index_path=tmp_path / "i.sqlite")
    stats = Indexer(cfg).build([
        Document(doc_id="a", title="A", text="cholera treatment"),
        Document(doc_id="a", title="A again", text="malaria treatment"),
        Document(doc_id="", title="no id", text="text"),
        Document(doc_id="b", title="empty", text="   "),
        "not a document",
    ])
    assert stats.documents_indexed == 2
    assert len(stats.skipped) == 3
    ids = {r[0] for r in sqlite3.connect(cfg.index_path).execute("SELECT doc_id FROM documents")}
    assert ids == {"a", "a#2"}


def test_rebuild_replaces_index_atomically(tmp_path):
    cfg = SearchConfig(index_path=tmp_path / "i.sqlite")
    Indexer(cfg).build([Document(doc_id="old", title="Old", text="old text")])
    Indexer(cfg).build([Document(doc_id="new", title="New", text="new text")])
    ids = [r[0] for r in sqlite3.connect(cfg.index_path).execute("SELECT doc_id FROM documents")]
    assert ids == ["new"]
    assert not (tmp_path / "i.sqlite.building").exists()


def test_jsonl_interface_for_other_components(tmp_path):
    path = tmp_path / "docs.jsonl"
    path.write_text(
        json.dumps({"doc_id": "x1", "title": "Measles", "text": "measles vaccination",
                    "metadata": {"date": "2020"}, "unknown_field": 1}) + "\n"
        + "{this is not json}\n"
        + json.dumps({"doc_id": "x2", "text": "no title given"}) + "\n", encoding="utf-8")
    docs, report = load_jsonl(path)
    assert [d.doc_id for d in docs] == ["x1", "x2"]
    assert docs[1].title == "x2"
    assert len(report.warnings) == 1


def test_missing_folder_and_missing_csv(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_corpus(tmp_path / "nope")
    (tmp_path / "a.txt").write_text("hello world", encoding="utf-8")
    docs, report = load_corpus(tmp_path, tmp_path / "missing.csv")
    assert len(docs) == 1 and any("not found" in w for w in report.warnings)


def _tiny_pdf(pages):
    """Write a minimal valid PDF with one line of text per page (no libraries needed)."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None,
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        content_no = len(objects)
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_no} 0 R >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def test_pdf_pages_become_page_numbers(tmp_path):
    pytest.importorskip("pypdf")
    from search_engine import SearchEngine
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "guide.pdf").write_bytes(_tiny_pdf(["Introduction to the guide", "Snakebite antivenom section"]))
    docs, report = load_corpus(docs_dir)
    assert len(docs) == 1 and docs[0].file_format == "pdf", report.warnings
    cfg = SearchConfig(index_path=tmp_path / "i.sqlite")
    Indexer(cfg).build(docs)
    with SearchEngine(config=cfg) as engine:
        result = engine.search("antivenom").results[0]
    assert result.passages[0].page == 2
