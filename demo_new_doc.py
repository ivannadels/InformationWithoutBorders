"""
Demo: adds one fictional document to this laptop's search_index.db, so the other
laptop (which already has the real library) has exactly one new document to sync.

Run:   python demo_new_doc.py            (on the laptop that will share it)
Then ask the other laptop, e.g.:
  hey im the new logistician at tesmara camp in nyabeto, the boreholes got contaminated
  in the floods, what did the last team do with the water points
("No good match" before the sync, this document right after it: the place names
Tesmara and Nyabeto are made up, so nothing in the real library mentions them)
"""
import sqlite3

DOC_ID = "DEMO001"
TITLE = "Tesmara camp (Nyabeto district): water point rehabilitation after the floods"
TEXT = (
    "DEMO DOCUMENT (fictional, for the peer-to-peer demo). Field report and handover notes: emergency "
    "water point rehabilitation at Tesmara camp, Nyabeto district. After the floods, three of the five "
    "boreholes in Tesmara camp were contaminated. The team chlorinated the remaining water points, set up "
    "two bladder tanks of 10,000 litres and distributed jerrycans and water purification tablets to "
    "1,200 households. Daily free residual chlorine checks at every water point kept levels between "
    "0.5 and 1 mg/l. Suspected cholera cases were referred to the oral rehydration point. "
    "Handover for the next logistician: the borehole pumps still need repair, and a local water "
    "committee in Nyabeto has been trained to run the chlorination."
)

conn = sqlite3.connect("search_index.db")
if conn.execute("SELECT 1 FROM docs_index WHERE doc_id = ?", (DOC_ID,)).fetchone():
    print(f"{DOC_ID} is already in search_index.db.")
else:
    with conn:
        conn.execute("INSERT INTO docs_index (doc_id, genre, language, filepath, html_content) VALUES (?, ?, ?, ?, ?)",
                     (DOC_ID, "Field Report (demo)", "en", "", f"{TITLE}. {TEXT}"))
    print(f"Added {DOC_ID}. Other laptops get it at their next sync.")
conn.close()
