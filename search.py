"""
Tiny offline "search engine" web page over a SQLite database.
Uses only the Python standard library, so no internet or pip installs are needed.
Members of a network sync their databases with each other; see p2p/__init__.py for setup.

Run:   python3 search.py
Open:  http://localhost:8000
"""
import html
import sqlite3
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

import p2p

DB = "search_index.db"         # built by index_documents.py
TABLE = "docs_index"           # table to search
KEY = "doc_id"                 # column that identifies a document on every device
COLUMNS = ["doc_id", "genre", "html_content"]  # text columns to search in; the first is the result title
LIMIT = 50

node = p2p.Node(DB, TABLE, KEY)

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Local Search</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 720px; margin: 60px auto; padding: 0 16px; }
  nav { text-align: right; font-size: 14px; }
  nav a { margin-left: 14px; color: #1a0dab; }
  h1 { text-align: center; font-weight: 500; }
  form { display: flex; gap: 8px; margin: 8px 0; }
  input { flex: 1; padding: 12px 18px; font-size: 18px; border: 1px solid #ccc; border-radius: 24px; }
  button { padding: 0 22px; border: 1px solid #ccc; border-radius: 24px; background: #f4f4f4; cursor: pointer; }
  .count { color: #777; margin: 18px 0 0; font-size: 14px; }
  .r { margin: 22px 0; }
  .r h3 { margin: 0; color: #1a0dab; font-weight: 400; }
  .r p { margin: 4px 0; color: #444; }
  mark { background: #fff3a0; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 18px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #eee; }
</style></head><body>
<nav><a href="/">Search</a><a href="/peers">Peers</a></nav>
__BODY__
</body></html>"""

SEARCH = """<h1>Local Search</h1>
<form action="/"><input name="q" value="__Q__" autofocus placeholder="Search..."><button>Search</button></form>
__RESULTS__"""


def search(q):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    where = " OR ".join(f"{c} LIKE ?" for c in COLUMNS)
    sql = f"SELECT * FROM {TABLE} WHERE {where} LIMIT {LIMIT}"
    rows = con.execute(sql, [f"%{q}%"] * len(COLUMNS)).fetchall()
    con.close()
    return rows


def highlight(text, q):
    text = html.escape(str(text))
    if not q:
        return text
    eq = html.escape(q)
    i = text.lower().find(eq.lower())
    if i == -1:
        return text[:300]
    start = max(0, i - 120)
    snippet = text[start:i + len(eq) + 180]
    return ("…" if start else "") + snippet.replace(text[i:i + len(eq)], f"<mark>{text[i:i + len(eq)]}</mark>")


def peers_page(msg):
    out = "<h1>Peers</h1>"
    if msg:
        out += f'<p class="count">{html.escape(msg)}</p>'
    if not node.enabled:
        return out + f"<p>Peer-to-peer sync is off. {html.escape(node.problem)}</p><p>Setup steps are at the top of p2p/__init__.py.</p>"
    me = "a member" if node.is_member() else "<b>not on the member list</b>"
    out += (f"<p>You are {html.escape(node.name)} on network {html.escape(node.network['name'])} ({me}). "
            f"Member list version {node.members['version']}, {len(node.members['members'])} member(s).</p>")
    out += '<form method="post" action="/sync"><button>Sync now</button></form>'
    with node.lock:
        peers = sorted(node.peers.values(), key=lambda p: (not node.online(p), p["name"]))
    rows = ""
    for p in peers:
        last = time.strftime("%H:%M", time.localtime(p["last"])) if p["last"] else "—"
        status = p["status"] if node.online(p) else "offline"
        rows += (f"<tr><td>{html.escape(p['name'] or '?')}</td><td>{html.escape(p['addr'])}</td>"
                 f"<td>{html.escape(status)}</td><td>{last}</td></tr>")
    rows = rows or '<tr><td colspan="4">No peers found yet. Peers on the same network appear here by themselves.</td></tr>'
    return out + f"<table><tr><th>Member</th><th>Address</th><th>Status</th><th>Last sync</th></tr>{rows}</table>"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/peers":
            return self._page(peers_page(query.get("msg", [""])[0]))
        q = query.get("q", [""])[0].strip()
        results = ""
        if q:
            try:
                rows = search(q)
                results = f'<p class="count">{len(rows)} result(s)</p>'
                for row in rows:
                    title = highlight(row[COLUMNS[0]], q)
                    body = " · ".join(highlight(row[c], q) for c in COLUMNS[1:] if row[c])
                    results += f'<div class="r"><h3>{title}</h3><p>{body}</p></div>'
            except Exception as e:
                results = f'<p class="count">Error: {html.escape(str(e))}</p>'
        self._page(SEARCH.replace("__Q__", html.escape(q)).replace("__RESULTS__", results))

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        msg = ""
        if node.enabled and self.path == "/sync":
            threading.Thread(target=node.sync_all, daemon=True).start()
            msg = "Syncing with all peers…"
        self.send_response(303)
        self.send_header("Location", "/peers?msg=" + quote(msg))
        self.end_headers()

    def _page(self, body):
        page = PAGE.replace("__BODY__", body)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode())


if __name__ == "__main__":
    node.start()
    print("Serving on http://localhost:8000")
    HTTPServer(("127.0.0.1", 8000), Handler).serve_forever()
