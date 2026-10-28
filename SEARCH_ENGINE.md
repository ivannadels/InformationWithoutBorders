# Offline Medical Document Search Engine

The search component for the MSF hackathon project. A user describes a situation in their own words, and the engine returns a short, ranked list of the most relevant local documents. Each result has an excerpt, the source file and its metadata.

* Runs **fully offline**: no internet, no cloud API, no server, no GPU.
* Needs **only Python's standard library** (SQLite with FTS5 is built into Python). `pypdf` is optional, for reading PDFs directly.
* Ranks with **BM25**, the standard algorithm used by search engines like Elasticsearch, through SQLite FTS5.

> The sample documents in `tests/sample_data/` are **synthetic test text written for software testing**. They are not clinical guidance.

## Quick start in this project

```bash
pip install -r requirements.txt
```

```bash
python index_documents.py
```

```bash
python -m search_engine iwb --db search_index.db "Adult with a cough for several weeks, fever and weight loss"
```

```bash
python examples/run_iwb_with_engine.py
```

1. Install the packages (once, with internet).
2. Build the team's database: `documentation.csv` + `html_files/<id>.html` → `search_index.db`.
3. Search it. The first run builds `search_engine_index.db` next to it, in about 3 seconds.
4. Or use the team's web page with this engine: open http://localhost:8000.

From Python: `from search_engine.iwb import IWBSearch; IWBSearch("search_index.db").search("...")`.
Tests: `python -m pytest`.

---

## 1. How it works

There are two separate steps:

```
          ONCE (or when documents change)                    EVERY QUERY (milliseconds)
 ┌──────────────────────────────────────────┐      ┌───────────────────────────────────────────┐
 │ PDF / HTML / TXT files + metadata.csv     │      │ "Child with severe diarrhoea and           │
 │        │  (loaders.py, or a teammate's    │      │  dehydration, limited supplies..."        │
 │        ▼   text-extraction component)     │      │        │ analyse: words, spelling variants,│
 │ Document objects (text + metadata)        │      │        ▼ negations (text_utils.py)         │
 │        │  split into passages,            │      │ SQLite FTS5 BM25: best passages            │
 │        ▼  detect duplicates (indexer.py)  │      │        │ group by document, check how much │
 │ search_engine_index.db ───────────────────┼─────►│        ▼ of the description each covers    │
 └──────────────────────────────────────────┘      │ collapse duplicates, drop weak matches     │
                                                    │        ▼                                   │
                                                    │ top 5 (or fewer) results + snippets        │
                                                    └───────────────────────────────────────────┘
```

**Indexing** reads every document once. It splits each document into passages (about 180 words) and stores them in a single SQLite file. SQLite FTS5 builds an "inverted index", like the index at the back of a book: for every word, it records which passages contain it. Searching never reopens the original PDFs.

**Searching:**

1. **Understand the query.** The engine lowercases the text, removes punctuation and drops filler words ("a", "the", "please find"). It adds spelling variants (diarrhea ↔ diarrhoea, haemorrhage ↔ hemorrhage ↔ bleeding). Words are reduced to their stem, so "treating", "treated" and "treatment" all match. Nothing is hidden: the response lists what was searched, what was ignored, and which words came after a negation ("no fever").
2. **Score passages with BM25.** A passage scores higher when it contains more of the query's words, especially rare ones. "Dehydration" counts for much more than "patient". Matches in the title and the CSV keywords count extra.
3. **Check coverage.** For each document, the engine measures what share of your description it mentions, weighted by how rare each word is. This is what stops "returns every document containing one matching word": a document that only shares the word "child" with a long description gets low coverage and is dropped.
4. **Collapse duplicates.** The same guideline as PDF and HTML, or a re-saved copy, becomes one result. The other copies are listed under `other_formats`.
5. **Return fewer, better results.** The engine returns at most `top_k` documents (default 5), and drops a document when it covers less than 40% of the description or scores below 35% of the best result. If nothing passes, it returns an empty list with a clear message. The frontend can then offer "show weaker matches", which re-runs the search with `min_coverage=0.2`.

### Files

```
search_engine/
  __init__.py      public interface: search(), configure(), SearchEngine, ...
  config.py        every setting in one place (thresholds, weights, synonyms, CSV columns)
  models.py        data shapes: Document (input), SearchResult / SearchResponse (output)
  text_utils.py    normalisation, query analysis, negation detection, section-aware passage splitting, duplicate fingerprints
  html_extract.py  saved web page -> clean text: keeps headings, lists, tables; drops menus, footers, pop-ups
  loaders.py       read .txt/.md/.html/.pdf + CSV metadata, or a JSONL file from a teammate;
                   removes each website's repeated boilerplate and skips site homepages
  indexer.py       builds the SQLite FTS5 index (indexing only)
  search.py        answers queries (searching only)
  semantic.py      extension point for optional meaning-based (embedding) search
  iwb.py           reads the InformationWithoutBorders database (docs_index) and follows p2p sync
  evaluate.py      Precision@k, Recall@k, MRR, latency on labelled queries
  __main__.py      command line: index / search / evaluate / stats
tests/             54 automated tests, run with networking blocked (+1 optional on the real HTML files)
tests/sample_data/ 16 small test files, metadata.csv, eval_queries.json (labelled queries)
tests/real_data_eval_queries.json   draft labelled queries for the real data (ids = doc_id)
requirements.txt   pandas + beautifulsoup4 (index_documents.py), optional pypdf, pytest
pytest.ini         test settings
examples/          integration_example.py (Python), http_api.py (tiny local web API),
                   run_iwb_with_engine.py (team's web page + this engine)
```

---

## 2. Assumptions about your data (please check these)

I haven't seen the real dataset, so the code doesn't hard-code field names. It makes the following assumptions, each of which can be changed in `config.py`:

| Assumption | If it's wrong |
|---|---|
| The CSV has a column with the **file name or relative path**. It's detected automatically if called `filename`, `file`, `file_name`, `path`, `file_path`, `document` or `url`. | Set `CsvColumnMap(file_column="your_column")` or use `--csv-file-column`. |
| An optional **title** column (`title`, `document_title`, `name`) and **id** column (`doc_id`, `id`, ...). | Without a title, the engine uses the HTML `<title>`, the PDF title, the first line, or the file name. |
| **Every other CSV column** (date, version, language...) is kept as metadata and returned unchanged. | Nothing to do. |
| A CSV row is matched to a file by relative path, file name, or file name without extension. | Rows without a file and files without a row are both **reported as warnings**. Unmatched files are still indexed. |
| The same document in two formats has either the **same id in the CSV**, an optional `group_id` column, the **same name in the same folder** (`x.pdf` + `x.html`), or **≥80% identical text**. | Adjust `duplicate_similarity`, or add a group column. |
| Documents are mostly **English**. | The stemmer (Porter) is English-only. See Limitations. |
| PDFs contain **real text**, not scanned images. | Scanned PDFs are skipped with a warning ("needs OCR"). |
| The CSV encoding is UTF-8 (cp1252/latin-1 are tried as fallbacks), with `,`, `;`, tab or `|` separators. | Detected automatically. |

**To confirm these, please send me:** the folder structure (e.g. output of `tree -L 2`), the CSV header plus 3–5 rows, and one example of a document that exists in two formats.

---

## 3. Setup (beginner steps)

You need Python 3.9 or newer. Check with `python3 --version`.

**While you still have internet**, open a terminal in this project folder:

```bash
python3 -m venv .venv
```

```bash
source .venv/bin/activate
```

(On Windows: `.venv\Scripts\activate`.)

```bash
pip install -r requirements.txt
```

This installs only `pypdf` (for PDFs) and `pytest` (for tests). The search engine itself needs nothing else. Once this is done, you can work offline.

**Build the index** from the sample data:

```bash
python -m search_engine index --docs tests/sample_data/documents --csv tests/sample_data/metadata.csv --csv-keywords topic
```

`--csv-keywords topic` makes the CSV's `topic` column searchable. List any useful columns, e.g. `--csv-keywords topic summary`. This command creates `folder_search_index.db` (not `search_index.db`, which is the team's database).

**Search:**

```bash
python -m search_engine search "A child has severe diarrhoea and dehydration"
```

```bash
python -m search_engine search "heavy bleeding after delivery" --top-k 3 --json
```

**For the real 600 documents**, point `--docs` and `--csv` at your folders and choose where to save the index:

```bash
python -m search_engine index --docs /path/to/documents --csv /path/to/metadata.csv --index folder_search_index.db
```

If a teammate already extracts text, they can write one JSON object per line (`doc_id`, `title`, `text`, `source_path`, `file_format`, `metadata`) and you index it with `--jsonl extracted.jsonl` instead of `--docs`.

---

## Using the real MSF HTML data (D4GC dataset)

Index only the HTML pages:

```bash
python -m search_engine index --docs "path/to/D4GC data/Original" --formats html
```

```bash
python -m search_engine search "Adult with a cough for several weeks, fever and weight loss"
```

**What's in the 342 HTML files.** They are saved web pages from six MSF sites, in English (202), French (51), Arabic (51) and Spanish (17). There is **no CSV**, so metadata is read from each page itself.

| Site | Pages indexed | Content |
|---|---|---|
| www.msf.org | 209 | News, field reports, country reports, annual reviews |
| medicalguidelines.msf.org | 70 | Clinical guideline chapters (several languages), incl. dosage tables |
| scienceportal.msf.org | 27 | Research abstracts |
| guide-humanitarian-law.org | 9 | Humanitarian law entries |
| epicentre.msf.org | 7 | Research publications (French) |
| msfaccess.org | 2 | Advocacy statements |

**How the HTML is cleaned** (`html_extract.py`):
* Only the page's main content area is kept. Menus, sidebars, breadcrumbs, language pickers, share buttons, "related articles", cookie scripts and footers are dropped (by tag, ARIA role and class name).
* **Headings, lists and tables are kept.** Headings become `## Heading` lines, and every passage remembers the headings above it (`section`, e.g. "Dosage" or "Clinical features"). Matches in section headings count extra in ranking. Tables become `cell | cell` rows with the header row first. Long tables are split between rows, with the header repeated.
* **Repeated website furniture is removed automatically:** any line that appears on at least half of one site's pages, e.g. "Permalink" or "Copyright…". Headings and table rows are never removed this way. This removed 1,334 lines in the real data.
* **Metadata** is read from the page: URL, site, language, description, publication or modification date, "Last updated" (215 of 324 pages have a date), breadcrumb, and DOI and authors for research pages. Guideline chapters get their book name added to the title, e.g. "Appendices (Management of a measles epidemic)".

**Problems found in the data:**
* **18 msfaccess.org files are copies of the msfaccess.org homepage.** The downloads were probably redirected. They are skipped with a warning, so **324 of 342** pages are indexed. Someone should re-download these.
* **27 science portal pages load their article with JavaScript.** The text is in the file but hidden until the page runs. The extractor handles this, and the team's existing `Parsed/*.md` files had partly missed it.
* `Dd4ee6d16b7.html` and `P07.html` are the same Gaza article. They are grouped and shown once.
* **Translations are kept separate.** The English, French and Spanish versions of a chapter are not merged as duplicates, even when their reference lists are identical.
* **The HTML pages have no clinical guideline on child diarrhoea or dehydration.** Only 3 pages mention "dehydration", all of them news. The documents that cover it in depth are **PDFs** (e.g. `P16`, `Dfee2e6ea98`, `D5da0aed2fd`). So the example query from the brief needs the PDFs indexed too.

**Measured on the real HTML index** (Apple M5 laptop): indexing 342 pages takes about 3 seconds. Search takes a median of 6 ms (slowest 12 ms). With the draft labels in `tests/real_data_eval_queries.json` (12 queries, **written by Claude from page titles and content; they need review by someone with clinical knowledge**):

| Metric | Value |
|---|---|
| Best document ranked first | 9 of 10 answerable queries (MRR 0.95) |
| Recall@5 | 1.0 |
| Precision@5 | 0.37: the labels count only the obviously best page, and extra results are usually related MSF news (e.g. maternal-care stories for "bleeding after birth") |
| Off-topic queries answered with nothing | 2 of 2 |
| Duplicate results | 0 |

```bash
python -m search_engine iwb --db search_index.db
python -m search_engine evaluate --queries tests/real_data_eval_queries.json --index search_engine_index.db
```

Example output (real data):

```
Found 3 matching documents.  (6.12 ms)
#1  Pulmonary tuberculosis (Clinical guidelines - Diagnosis and treatment manual)   [strong match, coverage 79%]
    file: .../D4GC data/Original/P21.html
    metadata: url=https://medicalguidelines.msf.org/en/viewport/CG/english/pulmonary-tuberculosis-16689588.html; language=en; ...
    excerpt [section: Clinical features]: - Prolonged **cough** (> 2 **weeks**) with or without sputum production ...
```

---

## Combining with the InformationWithoutBorders project (database + p2p sync)

**Result: the two work together.** I tested this on a copy, without changing any file in the team's folder. The team's own `search.py` web page, running with this engine, answers natural-language questions. As written, it returns **0 results** for them, because `LIKE '%whole sentence%'` never matches a full description.

| Query | Their `search()` as written | Their page + this engine |
|---|---|---|
| "Adult with a cough for several weeks, fever and weight loss" | 0 results | 1. Pulmonary tuberculosis (P21), 2. Amoxicillin (P24), 3. TB treatment regimens (P49) |
| "How to organise a mass vaccination campaign during a measles epidemic" | 0 results | 1. Film: Organising an emergency mass vaccination campaign (measles guideline) |

### How the pieces fit

```
documentation.csv + html_files/*.html
        │  their index_documents.py
        ▼
search_index.db  ── table docs_index(doc_id, genre, language, filepath, html_content)
        │                         ▲
        │                         │ their p2p/ copies rows between devices (by doc_id)
        ▼
search_engine/iwb.py  (reads it, read-only)
        │  uses the original HTML file if it is on this device, else the row's stored text
        ▼
search_engine_index.db  (this engine's own index, next to their database; *.db = git-ignored)
        │  rebuilt automatically when sync adds or removes rows
        ▼
IWBSearch.search(...)  /  IWBSearch.legacy_rows(...)  → their web page
```

### Using it

From Python:

```python
from search_engine.iwb import IWBSearch

engine = IWBSearch("search_index.db")     # their database; builds our index the first time (~3 s)
response = engine.search("Pregnant woman with Ebola", top_k=5)
for r in response.results:
    print(r.rank, r.doc_id, r.title, r.metadata.get("genre"), r.snippet)
```

From the command line:

```bash
python -m search_engine iwb --db search_index.db "Pregnant woman with Ebola"
```

To run their web page with this engine, without editing their files:

```bash
python examples/run_iwb_with_engine.py
```

Then open http://localhost:8000. Search uses this engine, and p2p sync and the Peers page work as before.

### What I changed in the search engine to fit their code

* **New `search_engine/iwb.py`.** It reads their `docs_index` table read-only, using their `doc_id` as the id (so ids match p2p and `remove-doc`), and their `genre` and `language` as metadata. A filter like `{"genre": "Clinical Guideline Chapter"}` works.
* **Two sources of text per document.** If the HTML file exists on this device (their `filepath`, or `files_dir`), it's re-read with the structure-aware extractor (headings, sections, tables). If the row arrived through sync without its file, the stored text is used. Word sequences repeated across many documents (menus, banners, footers) are removed first. This keeps at least 98.6% of every clinical guideline's real text. More aggressive settings were measured to remove up to 27% of a guideline's real text, so they're not used.
* **Follows sync.** A fingerprint of their table is checked (default every 60 s). When p2p adds a row, or the admin's `remove-doc` purges one, the index is rebuilt (~3 s for 342 documents) and reopened. A processing version number is part of the check, so engine updates also trigger a rebuild.
* **`legacy_rows()`** returns rows with their column names (`doc_id`, `genre`, `html_content`), so their page displays our results unchanged: title + id, then genre · language · match strength, then section and excerpt.
* Their database is never written to. A test checks the file is byte-for-byte identical after searching.

### Measured (copy of their database, built by their own `index_documents.py` from the 342 HTML files)

| | Device that has the HTML files | Peer device with synced rows only |
|---|---|---|
| Documents | 324 (18 homepage copies skipped) | 324 (homepage copies become empty after menu removal, skipped) |
| Best document first (MRR, 12 draft queries) | 0.95 | 1.0 |
| Recall@5 | 1.0 | 0.94 |
| Off-topic queries answered with nothing | 2 of 2 | 2 of 2 |
| Section names in results | yes | no (their stored text has no headings) |
| Median search time | 6 ms | 8 ms |

### Problems found in their code (not changed; suggestions for the team)

1. **Their `.venv` is missing `pandas` and `beautifulsoup4`,** so `index_documents.py` fails to start. Fix: `pip install pandas beautifulsoup4` (needs internet once).
2. **`index_documents.py` expects a folder `html_files/`** next to it. The data is in `D4GC data/Original`, so copy or link it there.
3. **Running `index_documents.py` twice stores every document twice.** `CREATE ... IF NOT EXISTS` keeps the old rows, and FTS5 has no unique key. Their `LIKE` search then shows duplicates. This engine keeps the first row per `doc_id` and warns.
4. **The stored text is the whole page flattened to one line,** including the app banner, language pickers and menus. Peer devices that only receive rows therefore get lower-quality text: no headings, sections or tables. Suggested change in `index_documents.py`, so every device gets clean, structured text through sync:
   ```python
   from search_engine.html_extract import extract_html_page   # needs this project's search_engine/ on the path
   # instead of the BeautifulSoup get_text() block:
   text_content = extract_html_page(f.read()).text
   ```
5. **Only HTML is indexed.** The 240 PDFs (which hold the clinical guidelines on diarrhoea and dehydration) and 182 CSV ids without any file are skipped.
6. To use the engine permanently instead of through the launcher, their `search.py` would only need:
   ```python
   from search_engine.iwb import IWBSearch
   search = IWBSearch(DB).legacy_rows        # replaces their def search(q)
   ```

## 4. Connecting it to the main application

### From Python (same program)

```python
import search_engine

search_engine.configure(index_path="folder_search_index.db")   # once, at app start-up

response = search_engine.search(
    query="A child has severe diarrhoea and dehydration",
    top_k=5,
)

response.results        # list of SearchResult, best first (may be fewer than top_k, or empty)
response.low_confidence # True if nothing matched well -> show a warning in the UI
response.message        # human-readable explanation, e.g. "No document mentions: ebola."
response.disclaimer     # "ranked by keyword match, not clinical appropriateness..."
response.to_dict()      # plain dict, ready for JSON
```

Instead of `configure`, you can set the environment variable `MSF_SEARCH_INDEX=folder_search_index.db`. You can also create the object yourself: `engine = SearchEngine("folder_search_index.db")`. It's safe to share between threads, e.g. inside a Flask or FastAPI app.

Optional metadata filter: `search_engine.search("...", filters={"language": "en", "file_format": "html"})`. For the real data, `{"site": "medicalguidelines.msf.org"}` restricts results to clinical guidelines.

"Show weaker matches" button: `search_engine.search("...", min_coverage=0.2)` relaxes the 40% coverage rule for one search.

**Each result contains:**

| Field | Meaning |
|---|---|
| `rank` | 1 = best match |
| `doc_id`, `title`, `source_path`, `file_format` | What it is and where to open the original |
| `snippet` | Best excerpt, with matched words wrapped in `**` (set `highlight_start="<mark>"` for HTML) |
| `passages` | Up to 3 best sections, each with `snippet`, full `text`, `section` (headings above it), `page` (PDFs) and `score` |
| `metadata` | Every CSV column for this file (date, version, ...) |
| `relevance_score` | Keyword-match strength. Only meaningful for comparing results of the **same** query. **Not a probability, and not a measure of medical correctness.** |
| `match_coverage`, `match_strength` | Share of the description this document mentions (0–1). `strong` / `partial` / `weak` |
| `matched_terms`, `missing_terms` | Which of your words it does or doesn't mention. Useful to show the user |
| `other_formats` | The same document in other formats or copies |

Run `python examples/integration_example.py` to see a full example.

### From a non-Python frontend (JavaScript, mobile, ...)

`examples/http_api.py` is a tiny local web API using only the standard library:

```bash
python examples/http_api.py --index search_engine_index.db --port 8780
```

The frontend then calls `http://127.0.0.1:8780/search?q=child+with+diarrhoea&top_k=5` (port 8765 is used by p2p) and receives the same JSON as `response.to_dict()`. It listens on this computer only.

---

## 5. Example queries and results (measured on the sample data)

Measured on an Apple M5 laptop with the 15-document sample set (16 files; the empty one is skipped).

**Query from the brief:** *"A doctor in a remote clinic is treating a child with severe diarrhoea and signs of dehydration, but the clinic has limited medical supplies. Find relevant clinical guidelines or protocols."*

```
Found 1 matching document. No document mentions: doctor, remote.
Searched for: doctor, remote, clinic, treating, child, severe, diarrhoea, signs, dehydration, limited, supplies
#1  Management of Acute Diarrhoea and Dehydration in Children   [partial match, coverage 56%]
    also as: acute_diarrhoea_copy_from_field_drive.txt, diarrhoea_children.txt   <- 3 copies, shown once
    metadata: publication_date=2021-03-01; version=2.1; ...
```

The cholera document (which covers severe dehydration) wasn't shown: it scored far below the top result. See the trade-off in section 7.

| Query | Result |
|---|---|
| "Diarrhea in kids" (US spelling, informal) | Diarrhoea guideline (spelling variants + kids→children) |
| "Heavy bleeding after delivery, the mother is losing a lot of blood" | Postpartum Haemorrhage (bleeding→haemorrhage) |
| "How do we keep vaccines cold while transporting them to the camp?" | 1. Cold Chain and Supply Logistics, 2. Measles Vaccination Campaigns |
| "How to repair a motorbike engine" | **No results**, low-confidence message |
| "Child with Ebola haemorrhagic fever" (no Ebola document exists) | **No results**: the 3 partial word matches cover under 40% of the description and are hidden |
| "A woman is very distressed after violence, crying and unable to cope" | **No results**. The relevant document uses different words ("psychological first aid"). A keyword engine can't bridge this; semantic search can. |

### Evaluation results (`python -m search_engine evaluate --queries tests/sample_data/eval_queries.json`)

| Metric | Value |
|---|---|
| | default (`min_coverage` 0.4) | with 0.25 |
|---|---|---|
| Mean Precision@5 (11 answerable queries) | 0.636 | 0.909 |
| Mean Recall@5 | 0.545 | 0.818 |
| MRR (first relevant result's position) | 0.636 | 0.909 |
| Off-topic queries correctly answered with nothing | 3 of 3 | 2 of 3 |
| Duplicate results | 0 | 0 |
| Latency | median 0.18 ms | median 0.19 ms |

In every query where the engine returned something, the first result was relevant. The default was raised from 0.25 to 0.4 after testing on the **real** data, where 0.4 removes only nonsense results. On this tiny synthetic set (15 short documents, so many query words appear nowhere and coverage stays low), 0.4 also hides three correct answers: malaria, postpartum bleeding and burns. The "show weaker matches" option (`min_coverage=0.2`) brings them back. This is the most important setting to tune on real labelled queries. These numbers come from a **tiny synthetic set** and say nothing about the real 600 documents. Re-measure on real data (next section).

### How to measure Precision@5 and Recall@5 on the real collection

1. Write 20–40 realistic queries, ideally phrased by medical team members the way they would in the field.
2. For each query, have someone with clinical knowledge list the document ids they would want to see. To find candidates, run a generous search (`--top-k 20`) and also browse by topic. Write them in `eval_queries.json` as `{"query": "...", "relevant": ["id1", "id2"]}`. Use `"relevant": []` for queries the collection can't answer.
3. Run `python -m search_engine evaluate --queries eval_queries.json --index search_engine_index.db`.

* **Precision@5** = of the results shown, the share that were relevant. "Does it waste the user's time?"
* **Recall@5** = of all relevant documents, the share that appeared in the top 5. "Does it miss things?"
* **MRR** = 1 / position of the first relevant result. 1.0 means the best document is always first.

Re-run the evaluation after every change to settings or synonyms, and keep the change only if the numbers improve.

---

## 6. Testing without internet

```bash
python -m pytest -q
```

All 54 tests run with **networking disabled inside Python**: `tests/conftest.py` replaces the socket functions with ones that raise an error. Any attempt to reach the internet makes a test fail. The tests cover normalisation, negation reporting, synonyms, passage and page splitting, duplicate detection, malformed and empty files, missing CSV rows, missing files, empty queries, `top_k` limits, off-topic queries, metadata filters, JSON output, thread safety, speed, the evaluation metrics, the semantic plug-in point, and HTML cleaning (menus and hidden React content, tables in the right order with section names, per-site boilerplate, homepages, translations).

To also run the smoke test on the real HTML files:

```bash
MSF_DATA_DIR="path/to/D4GC data/Original" python -m pytest tests/test_html.py
```

`tests/test_iwb.py` also checks that rows inserted by the project's own `p2p` insert code are found by the search engine.

**For a real-world offline check**, turn off Wi-Fi (or unplug the network cable), then run:

```bash
python -m search_engine iwb --db search_index.db "child with severe diarrhoea"
```

Nothing in the engine ever opens a network connection. The index is a single local file that you can copy onto a USB stick together with the code.

---

## 7. Limitations and realistic next steps

**Known limitations (seen in the evaluation above):**

* **Different words for the same thing.** "Distressed after violence" doesn't find "psychological first aid". BM25 only matches words (and their stems). The synonym list in `config.py` helps for known cases, but it must be maintained by hand.
* **Negation isn't understood.** "Diarrhoea **without** fever" still matches documents about fever. The engine detects the negation and **warns** in `response.message`, but doesn't remove or penalise these documents: guessing wrongly could hide a document the user needs.
* **Precision vs. recall trade-off.** The defaults favour short lists, so some secondary documents are hidden. Two settings control this: lower `relative_score_cutoff` (0.35) or `min_coverage` (0.4) in `SearchConfig` to show more. Tune them with the evaluation on real queries, not by feel.
* **Partial word overlap can mislead.** "Ebola haemorrhagic fever" partly matches "postpartum haemorrhage". The UI should always show `match_strength`, `missing_terms` and `message`.
* **The HTML collection is mostly news.** For clinical questions, results can include MSF news stories next to the guideline. Filtering with `{"site": "medicalguidelines.msf.org"}`, or a "guidelines only" toggle in the UI, gives a cleaner clinical list.
* **Bibliographies and abbreviation lists** match almost everything, so they are ranked lower (`low_value_title_pattern`, `low_value_penalty` in `config.py`). They are not removed.
* **English only.** The stemmer is English. French, Spanish and Arabic pages (119 of the 324 real pages) still match exact words but not word variants, and the synonym list is English. An English query rarely finds a French page.
* **Scanned PDFs** need OCR (e.g. Tesseract) in the document-processing step.
* When duplicates are collapsed, the copy with the best score is shown, not necessarily the official PDF.
* The ranking never considers how recent or authoritative a document is. The date and version are returned so the user can judge.

**Recommended improvements, in order:**

1. **Index the PDFs too** (240 files): the core clinical guidelines, including diarrhoea and dehydration, are there. The engine already reads PDFs. Alternatively, use the team's `Parsed/*.md` text for the PDFs through the JSONL interface.
2. **Have a clinician review `tests/real_data_eval_queries.json`** and add 20–30 real queries, then tune `min_coverage` and `relative_score_cutoff` on it.
3. **Have a clinician review the synonym list** in `config.py`: lay terms → clinical terms, French/English, abbreviations (ORS, SAM, PPH...). It's cheap and has a large effect.
4. **Index useful CSV columns** (`--csv-keywords topic summary keywords`).
5. **Optional semantic search** (below), if the evaluation shows many vocabulary mismatches.
6. **UI:** show the date and version prominently, highlight `missing_terms`, and always link to the original file.

### Adding semantic search later

`semantic.py` already defines the plug-in point, and a test proves it works with a stand-in model. A semantic retriever is any class with a `search_documents(query, limit) -> [(doc_id, similarity)]` method. The engine merges its results with BM25 using Reciprocal Rank Fusion. With no retriever, nothing changes.

What it would cost (approximate figures from public model cards; **verify before deciding**):

| | Approximate requirement |
|---|---|
| Model | e.g. `all-MiniLM-L6-v2` (English, ~90 MB) or `paraphrase-multilingual-MiniLM-L12-v2` (50+ languages, ~470 MB) |
| Python packages | `sentence-transformers` (pulls in PyTorch, roughly 1–2 GB installed), or the lighter ONNX-based `fastembed` |
| Index storage | 384 numbers per passage. About 600 docs × ~50 passages × 384 × 4 bytes ≈ 45 MB |
| Hardware | CPU is enough. Encoding the whole collection takes minutes on a laptop; one query takes a fraction of a second (measure on your machine) |
| Offline | Download the model **once** while online, save it to a local folder, then load it from that folder |

**Benefit:** it finds documents that describe the same concept in different words, like the "distressed after violence" query. **Risk:** it can also return confident-looking but loosely related documents. That's why fusion keeps BM25 in the loop and drops results below `semantic_min_similarity`. I'd add it only after measuring how many real queries fail because of vocabulary mismatch.

No generative AI model is used anywhere. The engine only retrieves existing documents. It never writes medical advice.
