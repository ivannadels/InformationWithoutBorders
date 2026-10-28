import sqlite3
import pandas as pd 
from bs4 import BeautifulSoup
import os 

conn = sqlite3.connect("search_index.db")
cursor = conn.cursor()

cursor.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS docs_index USING fts5(
    doc_id UNINDEXED, 
    genre UNINDEXED, 
    language UNINDEXED, 
    filepath UNINDEXED, 
    
    html_content);""")

df = pd.read_csv("documentataion.csv")

HTML_DIR = "html_files"

for _, row in df.iterrows(): 
    doc_id = str(row["id"])
    genre = str(row["genre"])
    language = str(row["language"])

    html_path = os.path.join(HTML_DIR, f"{doc_id}.html")

    text_content = ""
    if os.path.exists(html_path): 
        try: 
            with open(html_path, "r", encoding="utf-8") as f: 
                soup = BeautifulSoup(f.read(), "html.parser")
                for script_or_style in soup(["script", "style"]): 
                    script_or_style.decompose()
                text_content = soup.get_text(separator=" ", strip=True)

        except Exception as e: 
            print(f"Reading error for {html_path}: {e}")
            continue 
    else: 
        print(f"HTML file unfound : {html_path}")
        continue


    cursor.execute("""
        INSERT INTO docs_index (doc_id, genre, language, filepath, html_content)
        VALUES (?, ?, ?, ?, ?)
    """, (doc_id, genre, language, html_path, text_content))

conn.commit()
conn.close()
print("Indexation successful")