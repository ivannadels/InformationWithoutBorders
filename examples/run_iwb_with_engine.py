"""
Run the team's InformationWithoutBorders web page (their search.py, unchanged)
with this search engine instead of its LIKE '%query%' search.

    python examples/run_iwb_with_engine.py            (from the project folder)

Then open http://localhost:8000 as usual. p2p sync keeps working exactly as in
their app; rows it adds or removes are picked up by the engine within
--check-every seconds.

Nothing in their folder is edited: their module is imported and its search()
function is replaced in memory. The engine writes one extra file next to their
database, search_engine_index.db (ignored by their .gitignore, which ignores *.db).
"""

import argparse
import os
import sys
import threading
import time
from http.server import HTTPServer
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE_ROOT))

from search_engine.iwb import IWBSearch  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default=str(ENGINE_ROOT),
                        help="the InformationWithoutBorders folder (default: the one this file is in)")
    parser.add_argument("--files-dir", help="optional folder with the original <doc_id>.html files")
    parser.add_argument("--check-every", type=float, default=60, help="seconds between checks for synced rows")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    project = Path(args.project).resolve()
    sys.path.insert(0, str(project))
    os.chdir(project)                       # their code uses relative paths (search_index.db, p2p/state)

    import search as their_app              # their search.py

    # Searches never check for changes themselves: a rebuild after a big sync takes a while,
    # so a background thread starts it right after the sync instead of on the next search.
    engine = IWBSearch(their_app.DB, files_dir=args.files_dir, check_every=float("inf"))
    stats = engine.stats()
    print(f"Search engine ready: {stats['documents']} documents from {their_app.DB}")
    their_app.search = engine.legacy_rows   # the only change, in memory

    def follow_sync():
        while True:
            time.sleep(args.check_every)
            try:
                if engine.refresh():
                    print(f"Search engine updated: {engine.stats()['documents']} documents")
            except Exception as exc:        # database busy or file in use: try again next time
                print(f"Search engine update postponed: {exc}")
    threading.Thread(target=follow_sync, daemon=True).start()

    their_app.node.start()                  # same as their own __main__
    print(f"Serving on http://localhost:{args.port}")
    HTTPServer(("127.0.0.1", args.port), their_app.Handler).serve_forever()


if __name__ == "__main__":
    main()
