"""
Demo data until the real index is ready: fills search_index.db with placeholder text for
half of the catalogue, so two laptops start with different documents and sync fills the gap.
Same table as index_documents.py. Standard library only.

Run:   python demo_seed.py A      (on one laptop: every even row of documentation.csv)
       python demo_seed.py B      (on the other: every odd row)
"""
import csv
import os
import sqlite3
import sys

if len(sys.argv) != 2 or sys.argv[1] not in ("A", "B"):
    sys.exit("Usage: python demo_seed.py A|B")
half = 0 if sys.argv[1] == "A" else 1

conn = sqlite3.connect("search_index.db")
conn.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS docs_index USING fts5(
    doc_id UNINDEXED, genre UNINDEXED, language UNINDEXED, filepath UNINDEXED, html_content);""")
have = {r[0] for r in conn.execute("SELECT doc_id FROM docs_index")}

with open("documentation.csv", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))[half::2]

added = 0
with conn:
    for row in rows:
        if row["id"] in have:
            continue
        text = f"Placeholder text for document {row['id']}, a {row['genre']} in {row['language']}. Seeded on laptop {sys.argv[1]}."
        conn.execute("INSERT INTO docs_index VALUES (?, ?, ?, ?, ?)",
                     (row["id"], row["genre"], row["language"], os.path.join("html_files", f"{row['id']}.html"), text))
        added += 1
conn.close()
print(f"Added {added} demo document(s) for laptop {sys.argv[1]}.")
