"""
Demo: adds one fictional document to this laptop's search_index.db, so the other
laptop (which already has the real library) has exactly one new document to sync.

Run:   python demo_new_doc.py            (on the laptop that will share it)
Then search the other laptop for:  Lake Oroma
("No good match" before the sync, this document right after it)
"""
import sqlite3

DOC_ID = "DEMO001"
TEXT = (
    "DEMO DOCUMENT (fictional, for the peer-to-peer demo). Field report: emergency water point "
    "rehabilitation around Lake Oroma. After the floods, three of the five boreholes in the Lake Oroma "
    "settlement were contaminated. The team chlorinated the remaining water points, set up two "
    "bladder tanks of 10,000 litres and distributed jerrycans and water purification tablets to "
    "1,200 households. Daily free residual chlorine checks at every water point kept levels between "
    "0.5 and 1 mg/l. Suspected cholera cases were referred to the oral rehydration point. "
    "Next steps: repair the borehole pumps and train a local water committee to run the chlorination."
)

conn = sqlite3.connect("search_index.db")
if conn.execute("SELECT 1 FROM docs_index WHERE doc_id = ?", (DOC_ID,)).fetchone():
    print(f"{DOC_ID} is already in search_index.db.")
else:
    with conn:
        conn.execute("INSERT INTO docs_index (doc_id, genre, language, filepath, html_content) VALUES (?, ?, ?, ?, ?)",
                     (DOC_ID, "Field Report (demo)", "en", "", "Lake Oroma water point rehabilitation. " + TEXT))
    print(f"Added {DOC_ID}. Other laptops get it at their next sync.")
conn.close()
