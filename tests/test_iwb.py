"""
Compatibility with the project's database
(index_documents.py creates it, p2p/ syncs its rows between devices).
"""

import hashlib
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from search_engine.iwb import IWBSearch, database_signature, load_iwb_database, remove_repeated_word_runs

# Exactly the table index_documents.py creates.
THEIR_SCHEMA = """
    CREATE VIRTUAL TABLE IF NOT EXISTS docs_index USING fts5(
    doc_id UNINDEXED,
    genre UNINDEXED,
    language UNINDEXED,
    filepath UNINDEXED,

    html_content);"""

MENU = ("Skip to main content Home Guidelines Updates Order About Contact FAQ All guidelines "
        "Essential drugs Clinical guidelines English Français Español")
FOOTER = "Copyright Médecins Sans Frontières Privacy policy Terms of use Contact us"


def flat(title, body):
    """What BeautifulSoup get_text(separator=' ') stores: title, menus, body, footer on one line."""
    return f"{title} | MSF Medical Guidelines {MENU} {body} {FOOTER}"


TOPICS = {
    "P21": ("Pulmonary tuberculosis", "Prolonged cough for more than two weeks, fever, night sweats and weight loss."),
    "P23": ("Early postpartum haemorrhage", "Heavy bleeding after childbirth: massage the uterus and give oxytocin."),
    "P24": ("AMOXICILLIN oral", "Dosage for children: 25 mg/kg two times daily; high dosage for pneumonia."),
    "P30": ("Cholera", "Watery diarrhoea and severe dehydration: oral rehydration solution and IV Ringer lactate."),
    "P31": ("Measles", "Mass vaccination campaign during a measles epidemic, vitamin A for children."),
    "P32": ("Malaria", "Fever in a child in an endemic area: rapid diagnostic test then artemisinin treatment."),
}

HTML_P21 = """<html lang="en"><head><title>Pulmonary tuberculosis | MSF Medical Guidelines</title>
<link rel="canonical" href="https://medicalguidelines.msf.org/en/viewport/CG/english/pulmonary-tuberculosis.html"></head>
<body><main><nav>Home Guidelines</nav><h1>Pulmonary tuberculosis</h1>
<h2>Clinical features</h2><p>Prolonged cough for more than two weeks, fever, night sweats and weight loss.</p>
<h2>Treatment</h2><table><tr><th>Phase</th><th>Drugs</th></tr><tr><td>Initial</td><td>HRZE</td></tr></table>
</main><footer>Copyright</footer></body></html>"""


def make_their_database(folder: Path, with_html_for=("P21",)) -> Path:
    """Build a database exactly like index_documents.py would (html_files/<id>.html paths)."""
    (folder / "html_files").mkdir(parents=True, exist_ok=True)
    db = folder / "search_index.db"
    conn = sqlite3.connect(db)
    conn.execute(THEIR_SCHEMA)
    for doc_id, (title, body) in TOPICS.items():
        if doc_id in with_html_for:
            (folder / "html_files" / f"{doc_id}.html").write_text(HTML_P21, encoding="utf-8")
        conn.execute("INSERT INTO docs_index (doc_id, genre, language, filepath, html_content) VALUES (?,?,?,?,?)",
                     (doc_id, "Clinical Guideline Chapter", "en", f"html_files/{doc_id}.html", flat(title, body)))
    conn.commit()
    conn.close()
    return db


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_reads_their_rows_with_their_ids_and_metadata(tmp_path):
    db = make_their_database(tmp_path)
    docs, report = load_iwb_database(db)
    by_id = {d.doc_id: d for d in docs}
    assert set(by_id) == set(TOPICS)                                  # their doc_id, not file names
    assert by_id["P24"].metadata["genre"] == "Clinical Guideline Chapter"
    assert by_id["P24"].metadata["language"] == "en"
    # P21's HTML file is on this device: clean, structured text
    assert by_id["P21"].metadata["text_source"] == "html_file"
    assert "## Clinical features" in by_id["P21"].text and "Phase | Drugs" in by_id["P21"].text
    # the others only exist as synced rows: stored text, menus and footer removed
    assert by_id["P24"].metadata["text_source"] == "database"
    assert by_id["P24"].title == "AMOXICILLIN oral"
    assert "Skip to main content" not in by_id["P24"].text and "Privacy policy" not in by_id["P24"].text
    assert "25 mg/kg" in by_id["P24"].text


def test_search_engine_never_modifies_their_database(tmp_path):
    db = make_their_database(tmp_path)
    before = file_hash(db)
    engine = IWBSearch(db, check_every=0)
    engine.search("cough fever weight loss")
    engine.refresh(force=True)
    engine.close()
    assert file_hash(db) == before
    assert (tmp_path / "search_engine_index.db").is_file()           # our own file, *.db = git-ignored


def test_natural_language_queries_and_section_results(tmp_path):
    engine = IWBSearch(make_their_database(tmp_path), check_every=0)
    top = engine.search("Adult with a cough for several weeks, fever and weight loss").results[0]
    assert top.doc_id == "P21" and top.passages[0].section == "Clinical features"
    assert engine.search("woman with heavy bleeding after childbirth").results[0].doc_id == "P23"
    assert engine.search("How to repair a motorbike engine").results == []
    engine.close()


def test_rows_added_or_removed_by_sync_are_picked_up(tmp_path):
    db = make_their_database(tmp_path)
    engine = IWBSearch(db, check_every=0)
    assert engine.search("snakebite antivenom").results == []
    conn = sqlite3.connect(db)          # what p2p Node._insert / Node.purge do
    conn.execute("INSERT INTO docs_index (doc_id, genre, language, filepath, html_content) VALUES (?,?,?,?,?)",
                 ("P40", "Clinical Guideline Chapter", "en", "html_files/P40.html",
                  flat("Snakebite", "Snakebite envenoming: immobilise the limb and give antivenom.")))
    conn.execute("DELETE FROM docs_index WHERE doc_id = ?", ("P32",))
    conn.commit()
    conn.close()
    assert engine.search("snakebite antivenom").results[0].doc_id == "P40"
    assert all(r.doc_id != "P32" for r in engine.search("malaria fever rapid diagnostic test").results)
    engine.close()


def test_index_is_reused_when_nothing_changed(tmp_path):
    db = make_their_database(tmp_path)
    IWBSearch(db).close()
    engine = IWBSearch(db)
    assert engine.last_stats is None                                    # no rebuild needed
    assert engine.refresh() is False
    engine.close()


def test_duplicate_rows_from_running_their_indexer_twice(tmp_path):
    db = make_their_database(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO docs_index SELECT * FROM docs_index WHERE doc_id = 'P24'")
    conn.commit()
    conn.close()
    docs, report = load_iwb_database(db)
    assert [d.doc_id for d in docs].count("P24") == 1
    assert any("Duplicate rows for doc_id P24" in w for w in report.warnings)


def test_legacy_rows_fit_their_web_page(tmp_path):
    engine = IWBSearch(make_their_database(tmp_path), check_every=0)
    rows = engine.legacy_rows("cough for weeks with night sweats")
    # their page reads row["doc_id"] (title) and the other COLUMNS: genre, html_content
    assert {"doc_id", "genre", "html_content"} <= set(rows[0])
    assert rows[0]["doc_id"].startswith("Pulmonary tuberculosis") and "[P21]" in rows[0]["doc_id"]
    assert "**" not in rows[0]["html_content"]                       # their page escapes HTML itself
    assert engine.legacy_rows("motorbike engine repair")[0]["doc_id"] == "No good match"
    engine.close()


def test_missing_table_or_database_gives_clear_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_iwb_database(tmp_path / "nothing.db")
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE docs_index (id TEXT, body TEXT)")
    with pytest.raises(ValueError, match="no column"):
        load_iwb_database(other)


def test_repeated_word_runs_keep_document_specific_text():
    bodies = [f"{topic} guidance for the field team in {place}" for topic, place in
              zip(["cholera", "measles", "malaria", "burns", "tuberculosis", "snakebite", "rabies", "anaemia", "hepatitis", "typhoid"],
                  ["Chad", "Yemen", "Haiti", "Sudan", "Gaza", "Mali", "Niger", "Iraq", "Peru", "Laos"])]
    texts = [f"{MENU} {body} {FOOTER}" for body in bodies]
    cleaned, removed = remove_repeated_word_runs(texts)
    assert removed > 0
    assert cleaned == bodies


# ---- optional: with the team's real code -------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parent.parent      # the folder that contains p2p/


@pytest.mark.skipif(not (PROJECT_DIR / "p2p").is_dir(), reason="p2p package not found next to tests/")
def test_with_the_projects_p2p_insert_code(tmp_path, monkeypatch):
    """Rows inserted by the project's own p2p.Node._insert are found by the engine."""
    monkeypatch.syspath_prepend(str(PROJECT_DIR))
    import p2p
    db = make_their_database(tmp_path)
    engine = IWBSearch(db, check_every=0)
    node = p2p.Node(str(db), "docs_index", "doc_id")
    row = {"doc_id": "P41", "genre": "Clinical Guideline Chapter", "language": "en",
           "filepath": "html_files/P41.html",
           "html_content": flat("Rabies", "Rabies post-exposure prophylaxis: wash the wound and vaccinate.")}
    assert node._insert([row], {"P41"}) == 1
    assert engine.search("dog bite rabies vaccine wound").results[0].doc_id == "P41"
    engine.close()
