# Information Without Borders

Offline search over MSF's medical and field documents, for teams working where there is no internet.

A field worker describes a situation in their own words ("a woman is bleeding heavily after giving birth") and gets a short, ranked list of the most relevant local documents: clinical guideline chapters, field reports and research. Everything runs on one laptop, with no internet, cloud service or GPU. Devices in the same team find each other on a local network (Wi-Fi or a router, no internet needed) and copy each other's documents, so every device ends up able to search the whole library.

No generative AI is used. The engine only finds existing documents; it never writes medical advice.

## Quick start

You need Python 3.9 or newer.

**1. Install the packages** (once, while you have internet):

```bash
pip install -r requirements.txt
```

**2. Build the database.** Put the source files in `documents/` (named `<id>.html` or `<id>.pdf`, matching the `id` column of `documentation.csv`), then run:

```bash
python index_documents.py
```

This creates `search_index.db`. Files missing on this device are skipped, and running it again only adds new documents.

**3. Start the search page:**

```bash
python examples/run_iwb_with_engine.py
```

Open http://localhost:8000. The first start builds the search index (`search_engine_index.db`) next to the database; this takes around 30 seconds for the full library and is only done once.

## Searching

Type a question in plain language. Each result shows the document title and id, its genre, how well it matches, and the most relevant excerpt.

Example questions that work well on the current library (579 documents):

| Question | Top result |
|---|---|
| a woman is bleeding heavily after giving birth | 8.2 Early postpartum haemorrhage (Essential obstetric and newborn care) |
| measles vaccination campaign in an emergency | Organising an emergency mass vaccination campaign (Management of a measles epidemic) |
| how to prepare oral rehydration solution | Appendices (Management of a cholera epidemic) |
| cholera cases rising in the camp how do we stop the outbreak | MSF field reports on cholera responses in South Sudan |
| drug-resistant tuberculosis treatment | Research on drug-resistant TB treatment outcomes |

Tips: spelling variants (diarrhea / diarrhoea) and everyday words (bleeding → haemorrhage) are handled, but typos are not, and "not" or "without" is not understood. Queries work best in English.

**Not here? Search nearby devices.** When the network is set up (below), this button under the results syncs with every device in reach and runs the same search again, so documents another team member has show up within seconds.

You can also search from the command line:

```bash
python -m search_engine iwb --db search_index.db "a woman is bleeding heavily after giving birth"
```

## Sharing documents between devices (p2p)

Only members of a network can sync. All traffic is encrypted and both sides prove who they are before any data is exchanged. Run these from the project folder:

| Who | Command |
|---|---|
| Admin, once | `python -m p2p init "Network Name"` |
| Each member | `python -m p2p keygen "Your Name"` (prints your public key; send it to the admin) |
| Admin, per member | `python -m p2p add <public key> "Their Name"` |

The admin then gives each member a copy of `p2p/state/network.json` and `p2p/state/members.json` (on a USB stick, for example). After that, devices on the same local network find each other and sync automatically, and the search page picks up new documents within a minute.

Other admin commands:

```bash
python -m p2p members                  # list members
python -m p2p remove <key or name>     # remove a member
python -m p2p remove-doc <doc id>      # delete a document from every device as they sync
python -m p2p restore-doc <doc id>     # undo remove-doc
```

Keep `p2p/state/admin.key` private and backed up: whoever has it controls membership, and without it membership can never be changed. `p2p/state/` is never committed to git.

## Project layout

```
documentation.csv     catalogue: id, genre, language of every document
documents/            source HTML and PDF files (per device, not in git)
index_documents.py    builds search_index.db from documentation.csv + documents/
search.py             the search web page and Peers page (port 8000)
examples/run_iwb_with_engine.py   starts search.py with the search engine below (use this one)
search_engine/        offline ranked search (BM25 via SQLite FTS5), standard library only
p2p/                  peer discovery, membership and encrypted sync (ports 8765, 8766)
tests/                automated tests, run with networking blocked
demo_seed.py, demo_new_doc.py     helpers for a two-laptop sync demo (not needed otherwise)
```

## Tests

```bash
python -m pytest -q
```

The tests block every network connection, so any attempt to reach the internet makes a test fail.

## More detail

[SEARCH_ENGINE.md](SEARCH_ENGINE.md) explains how the search engine ranks results, its settings, how to measure its quality, and its known limitations.
