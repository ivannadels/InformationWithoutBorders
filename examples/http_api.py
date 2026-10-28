"""
A tiny local web API around the search engine, using only Python's standard
library (no Flask/FastAPI needed). A JavaScript or other frontend can call it.

    python examples/http_api.py --index search_engine_index.db --port 8780

    GET http://127.0.0.1:8780/search?q=child+with+severe+diarrhoea&top_k=5
    GET http://127.0.0.1:8780/document?id=P21

(search_engine_index.db is created next to search_index.db by IWBSearch or
`python -m search_engine iwb --db search_index.db`. Port 8765 is taken by p2p.)

It listens on 127.0.0.1 only (this computer), so nothing is exposed to a network.
"""

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from search_engine import SearchEngine  # noqa: E402

ENGINE: SearchEngine


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")   # allow a local frontend on another port
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        params = parse_qs(url.query)
        if url.path == "/search":
            query = params.get("q", [""])[0]
            try:
                top_k = int(params.get("top_k", ["5"])[0])
            except ValueError:
                return self._send(400, {"error": "top_k must be a number"})
            return self._send(200, ENGINE.search(query, top_k=top_k).to_dict())
        if url.path == "/document":
            doc = ENGINE.get_document(params.get("id", [""])[0])
            return self._send(200, doc) if doc else self._send(404, {"error": "not found"})
        if url.path == "/health":
            return self._send(200, ENGINE.stats())
        self._send(404, {"error": "use /search?q=... or /document?id=..."})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", default="search_engine_index.db")
    parser.add_argument("--port", type=int, default=8780)   # 8765/8766 are used by p2p
    args = parser.parse_args()
    ENGINE = SearchEngine(args.index)
    print(f"Search API on http://127.0.0.1:{args.port}/search?q=...  (Ctrl+C to stop)")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
