"""
How another Python component (the main app, a UI backend...) uses the search engine.

Run from the project folder:
    python examples/integration_example.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))   # only needed because this example lives in a subfolder

import search_engine
from search_engine import CsvColumnMap, Indexer, SearchConfig, load_corpus

INDEX = ROOT / "sample_search_index.db"     # *.db: ignored by git

# --- 1. Build the index (normally done once by a setup script, not on every start;
#        here it is rebuilt each time because the sample takes a few milliseconds) ---
config = SearchConfig(index_path=INDEX, csv_columns=CsvColumnMap(keywords_columns=["topic"]))
documents, report = load_corpus(ROOT / "tests/sample_data/documents", ROOT / "tests/sample_data/metadata.csv", config)
Indexer(config).build(documents)

# --- 2. At app start-up: point the engine at the index ---------------------------------
search_engine.configure(index_path=INDEX)

# --- 3. Whenever the user submits a description ------------------------------------------
response = search_engine.search(
    query="A child has severe diarrhoea and dehydration",
    top_k=5,
)

print(response.message)
for result in response.results:
    print(f"{result.rank}. {result.title}  ({result.match_strength} match)")
    print(f"   open: {result.source_path}")
    print(f"   date: {result.metadata.get('publication_date', 'unknown')}, "
          f"version: {result.metadata.get('version', 'unknown')}")
    print(f"   {result.snippet[:160]}...")

# --- 4. Or hand everything to a web frontend as JSON ----------------------------------------
payload = response.to_dict()
print("\nJSON keys sent to the frontend:", list(payload))
print(json.dumps(payload["results"][0], indent=2, ensure_ascii=False)[:800], "...")
