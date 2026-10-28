import sqlite3
import pandas as pd 
from bs4 import BeautifulSoup
import fitz 
import os 


DB_FILE = "serach_index.db"
DOCS_DIR = "documents"

conn = sqlite3.connect("search_index.db")
cursor = conn.cursor()

cursor.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS docs_index USING fts5(
    doc_id UNINDEXED, 
    genre UNINDEXED, 
    language UNINDEXED, 
    filepath UNINDEXED, 
    
    html_content);""")

def extract_text_from_html(filepath): 
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f: 
        soup = BeautifulSoup(f.read(), "html.parser")
        for tag in soup(["script", "style"]): 
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)


def extract_text_from_pdf(filepath): 
    text = ""
    with fitz.open(filepath) as doc: 
        for page in doc: 
            text += page.get_text() + " "
    return text.strip()

df = pd.read_csv("documentation.csv")

# Rows are synced between devices by doc_id: skip documents already indexed here
already_indexed = {r[0] for r in cursor.execute("SELECT doc_id FROM docs_index")}

for _, row in df.iterrows():
    doc_id = str(row["id"])
    if doc_id in already_indexed:
        continue
    genre = str(row["genre"])
    language = str(row["language"])

    html_path = os.path.join(DOCS_DIR, f"{doc_id}.html")
    pdf_path = os.path.join(DOCS_DIR, f"{doc_id}.pdf")

    filepath = None 
    extracted_text = ""

    if os.path.exists(html_path): 
        filepath = html_path
        try: 
            extracted_text = extract_text_from_html(html_path)
        except Exception as e: 
            print(f"HTML extraction error for {html_path}: {e}")

    elif os.path.exists(pdf_path):
        filepath = pdf_path
        try: 
            extracted_text = extract_text_from_pdf(pdf_path)
        except Exception as e: 
            print(f"Extraction error for PDF {pdf_path}: {e}")

    else:
        print(f"No file found for document ID: {doc_id}")

    # An empty row would stop sync from fetching this document from a device that has the file
    if not extracted_text:
        continue

    cursor.execute("""
        INSERT INTO docs_index (doc_id, genre, language, filepath, html_content)
        VALUES (?, ?, ?, ?, ?)
    """, (doc_id, genre, language, filepath, extracted_text))

conn.commit()
conn.close()
print("Indexation successful!")