"""
A network member: syncs rows with peers, answers peers' requests, finds peers on the
local network. See __init__.py for setup and crypto.py for the encryption.

Discovery: every running node broadcasts a small UDP announcement on each local network
it is connected to. Peers are tracked by the random id in that announcement, so a device
reachable at several addresses is still one peer.

Protocol, one round trip per request after a challenge:
  1. GET /challenge -> server sends a one-time nonce and a fresh X25519 key, signed with
     its member key. The client checks the server is a member before sending anything.
  2. POST /api -> client sends its own fresh X25519 key and the request encrypted with
     the shared secret, signed with its member key. The server checks the client is a
     member and replies encrypted with the same session.
"""
import base64
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from . import crypto, ed25519

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
IDENTITY = os.path.join(STATE, "identity.key")   # this device's private key (keep it private)
ADMIN = os.path.join(STATE, "admin.key")         # network admin's private key (admin only)
NETWORK = os.path.join(STATE, "network.json")    # network name + admin public key
MEMBERS = os.path.join(STATE, "members.json")    # admin-signed member public keys + removed document ids

P2P_PORT = 8765
DISCOVERY_PORT = 8766
ANNOUNCE_EVERY = 15            # seconds between local-network announcements
OFFLINE_AFTER = 60             # seconds without an announcement before a peer counts as offline
FORGET_AFTER = 3600            # seconds before an offline peer is dropped from the list
SYNC_EVERY = 120               # seconds between automatic syncs
BATCH = 500                    # rows per request
MAX_BODY = 64 * 1024 * 1024

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class NotAuthorized(Exception):
    pass


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def _save(path, obj):
    os.makedirs(STATE, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def _broadcast_addresses():
    """The global broadcast address plus a /24 broadcast for each local IPv4 address."""
    addrs = {"255.255.255.255"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                addrs.add(ip.rsplit(".", 1)[0] + ".255")
    except OSError:
        pass
    return addrs


def _members_payload(m):
    return json.dumps({k: m[k] for k in ("network", "version", "members", "removed")}, sort_keys=True).encode()


def sign_members(m, admin_secret):
    m["sig"] = ed25519.sign(admin_secret, _members_payload(m)).hex()
    _save(MEMBERS, m)


def members_valid(m, admin_pub):
    try:
        return (m["network"] == admin_pub and isinstance(m["version"], int) and isinstance(m["members"], dict)
                and isinstance(m["removed"], list) and all(isinstance(d, str) for d in m["removed"])
                and ed25519.verify(bytes.fromhex(admin_pub), _members_payload(m), bytes.fromhex(m["sig"])))
    except (KeyError, TypeError, ValueError):
        return False


def removed_docs():
    """Document ids the admin has removed from the network (empty if this device has no valid list)."""
    net, m = _load(NETWORK), _load(MEMBERS)
    return set(m["removed"]) if net and m and members_valid(m, net["admin"]) else set()


def _verify(key_hex, msg, sig_hex):
    try:
        return ed25519.verify(bytes.fromhex(key_hex), msg, bytes.fromhex(sig_hex))
    except ValueError:
        return False


def _transcript(kind, network, *parts):
    return hashlib.sha256("|".join((f"iwb-{kind}", network) + parts).encode()).digest()


def _session_keys(secret, other_eph, network, nonce, server_eph, client_eph):
    shared = crypto.x25519(secret, bytes.fromhex(other_eph))
    return crypto.derive(shared, _transcript("keys", network, nonce, server_eph, client_eph))


# Rows are identified by their key column (e.g. a catalogue id), which is the same on every device.
# Documents are only ever added; removal is done by the admin through the signed member list.
def _enc(v):
    return {"b64": base64.b64encode(v).decode()} if isinstance(v, bytes) else v


def _dec(v):
    return base64.b64decode(v["b64"]) if isinstance(v, dict) else v


def _columns(con, table):
    info = con.execute(f"PRAGMA table_info({table})").fetchall()  # cid, name, type, notnull, default, pk
    pks = [c for c in info if c[5]]
    local_id = {pks[0][1]} if len(pks) == 1 and pks[0][2].upper() == "INTEGER" else set()
    return [c[1] for c in info if c[1] not in local_id]


def _cols_sql(cols):
    return ", ".join(f'"{c}"' for c in cols)


def _peer(addr):
    return {"addr": addr, "name": "", "status": "not synced yet", "last": None, "seen": time.time()}


class Node:
    def __init__(self, db, table, key, port=P2P_PORT):
        self.db, self.table, self.key, self.port = db, table, key, port
        self.peers = {}                # announced instance id -> _peer(...)
        self.challenges = {}           # nonce -> (expiry, our X25519 secret, our X25519 public hex)
        self.lock = threading.Lock()
        self.sync_lock = threading.Lock()
        self.instance = secrets.token_hex(8)
        ident, self.network, self.members = _load(IDENTITY), _load(NETWORK), _load(MEMBERS)
        self.problem = ""
        if not ident:
            self.problem = 'No identity key yet. Create one with: python -m p2p keygen "Your Name"'
        elif not (self.network and self.members):
            self.problem = "No network.json / members.json in p2p/state yet. Get both files from your network admin."
        elif not members_valid(self.members, self.network["admin"]):
            self.problem = "members.json is not signed by this network's admin."
        self.enabled = not self.problem
        if ident:
            self.name = ident["name"]
            self.secret = bytes.fromhex(ident["secret"])
            self.pub = ed25519.public_key(self.secret).hex()

    def is_member(self):
        return self.enabled and self.pub in self.members["members"]

    def adopt_members(self, m):
        with self.lock:
            if (isinstance(m, dict) and members_valid(m, self.network["admin"])
                    and m["version"] > self.members["version"]):
                self.members = m
                _save(MEMBERS, m)

    # --- local database ---

    def _ids(self):
        con = sqlite3.connect(self.db, timeout=30)
        ids = {r[0] for r in con.execute(f'SELECT "{self.key}" FROM {self.table}')}
        con.close()
        return ids

    def _fetch(self, ids):
        ids = [i for i in ids[:BATCH] if isinstance(i, str)]
        con = sqlite3.connect(self.db, timeout=30)
        cols = _columns(con, self.table)
        sql = f'SELECT {_cols_sql(cols)} FROM {self.table} WHERE "{self.key}" IN ({", ".join("?" * len(ids))})'
        rows = [dict(zip(cols, map(_enc, r))) for r in con.execute(sql, ids)] if ids else []
        con.close()
        return rows

    def _insert(self, rows, wanted):
        con = sqlite3.connect(self.db, timeout=30)
        cols = _columns(con, self.table)
        sql = f"INSERT INTO {self.table} ({_cols_sql(cols)}) VALUES ({', '.join('?' * len(cols))})"
        added = 0
        with con:
            for row in rows:
                doc = row.get(self.key) if isinstance(row, dict) else None
                if isinstance(doc, str) and doc in wanted:   # only accept ids we asked for, once each
                    wanted.discard(doc)
                    con.execute(sql, [_dec(row.get(c)) for c in cols])
                    added += 1
        con.close()
        return added

    def purge(self):
        """Delete the documents the admin has removed from the network."""
        removed = list(self.members["removed"])
        con = sqlite3.connect(self.db, timeout=30)
        with con:
            for k in range(0, len(removed), BATCH):
                part = removed[k:k + BATCH]
                con.execute(f'DELETE FROM {self.table} WHERE "{self.key}" IN ({", ".join("?" * len(part))})', part)
        con.close()

    # --- server side ---

    def new_challenge(self):
        nonce, now = secrets.token_hex(16), time.time()
        eph = secrets.token_bytes(32)
        eph_pub = crypto.x25519_public(eph).hex()
        with self.lock:
            if len(self.challenges) > 10000:
                self.challenges = {n: c for n, c in self.challenges.items() if c[0] > now}
            self.challenges[nonce] = (now + 60, eph, eph_pub)
        sig = ed25519.sign(self.secret, _transcript("hello", self.network["admin"], nonce, eph_pub))
        return {"nonce": nonce, "eph": eph_pub, "key": self.pub, "sig": sig.hex()}

    def use_challenge(self, nonce):
        with self.lock:
            c = self.challenges.pop(nonce, None)
        return c if c and c[0] > time.time() else None

    def answer(self, req):
        op = req.get("op")
        if op == "members":
            return self.members
        if op == "ids":
            return sorted(self._ids())
        if op == "rows":
            return self._fetch(req.get("ids", []))
        raise ValueError(f"unknown op {op!r}")

    # --- client side ---

    def _call(self, addr, op, signed=True, **args):
        net = self.network["admin"]
        with _opener.open(f"http://{addr}/challenge", timeout=30) as r:
            hello = json.loads(r.read())
        key, nonce, server_eph = hello["key"], hello["nonce"], hello["eph"]
        if key not in self.members["members"] or not _verify(key, _transcript("hello", net, nonce, server_eph), hello["sig"]):
            raise NotAuthorized()
        eph = secrets.token_bytes(32)
        client_eph = crypto.x25519_public(eph).hex()
        keys = _session_keys(eph, server_eph, net, nonce, server_eph, client_eph)
        body = crypto.seal(keys[0], keys[1], json.dumps({"op": op, **args}).encode())
        headers = {"Content-Type": "application/octet-stream", "X-IWB-Nonce": nonce, "X-IWB-Eph": client_eph}
        if signed:
            msg = _transcript("req", net, nonce, server_eph, client_eph, hashlib.sha256(body).hexdigest())
            headers.update({"X-IWB-Key": self.pub, "X-IWB-Sig": ed25519.sign(self.secret, msg).hex()})
        with _opener.open(urllib.request.Request(f"http://{addr}/api", body, headers), timeout=60) as r:
            resp = r.read()
        return json.loads(crypto.unseal(keys[2], keys[3], resp)), key

    def sync_with(self, instance):
        with self.sync_lock:
            with self.lock:
                peer = self.peers.get(instance)
            if not peer:
                return
            addr = peer["addr"]
            peer["status"] = "syncing…"
            try:
                self.adopt_members(_load(MEMBERS))  # pick up changes the admin made while we were running
                # Unsigned on purpose: a peer with an older list may not know us yet. It only counts if the admin signed it.
                self._call(addr, "push_members", signed=False, members=self.members)
                members, key = self._call(addr, "members")
                self.adopt_members(members)
                peer["name"] = self.members["members"].get(key, "")
                self.purge()
                ids, _ = self._call(addr, "ids")
                skip = self._ids() | set(self.members["removed"])
                missing = [i for i in ids if isinstance(i, str) and i not in skip]
                added = 0
                for k in range(0, len(missing), BATCH):
                    rows, _ = self._call(addr, "rows", ids=missing[k:k + BATCH])
                    added += self._insert(rows, set(missing[k:k + BATCH]))
                peer["status"] = f"synced, {added} new row(s)"
            except NotAuthorized:
                peer["status"] = "ignored: not a member of this network"
            except urllib.error.HTTPError as e:
                peer["status"] = "refused us: we are not on their member list" if e.code == 403 else f"error: HTTP {e.code}"
            except OSError as e:
                peer["status"] = f"unreachable: {e}"
            except (ValueError, KeyError, TypeError) as e:
                peer["status"] = f"failed: {e}"
            peer["last"] = time.time()

    def online(self, peer):
        return time.time() - peer["seen"] < OFFLINE_AFTER

    def sync_all(self):
        now = time.time()
        with self.lock:
            self.peers = {i: p for i, p in self.peers.items() if now - p["seen"] < FORGET_AFTER}
            online = [i for i, p in self.peers.items() if self.online(p)]
        for instance in online:
            self.sync_with(instance)

    # --- background threads ---

    def _announce(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        msg = json.dumps({"iwb": self.network["admin"][:16], "id": self.instance, "port": self.port}).encode()
        while True:
            for addr in _broadcast_addresses():  # re-read each time: Wi-Fi can come and go
                try:
                    s.sendto(msg, (addr, DISCOVERY_PORT))
                except OSError:
                    pass  # that network is down right now
            time.sleep(ANNOUNCE_EVERY)

    def _listen(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", DISCOVERY_PORT))
        except OSError as e:
            print(f"Local discovery off ({e}); add peers by address instead.")
            return
        while True:
            data, (ip, _) = s.recvfrom(1024)
            try:
                m = json.loads(data)
                instance = str(m["id"])
                if m.get("iwb") != self.network["admin"][:16] or instance == self.instance:
                    continue
                addr = f"{ip}:{int(m['port'])}"
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            with self.lock:
                peer = self.peers.get(instance)
                came_online = not peer or not self.online(peer)
                if peer:
                    peer["addr"], peer["seen"] = addr, time.time()
                else:
                    self.peers[instance] = _peer(addr)
            if came_online:
                threading.Thread(target=self.sync_with, args=(instance,), daemon=True).start()

    def _sync_loop(self):
        while True:
            self.sync_all()
            time.sleep(SYNC_EVERY)

    def start(self):
        if not self.enabled:
            print(f"Peer-to-peer off: {self.problem}")
            return
        server = ThreadingHTTPServer(("0.0.0.0", self.port), _handler(self))
        for target in (server.serve_forever, self._announce, self._listen, self._sync_loop):
            threading.Thread(target=target, daemon=True).start()
        print(f"Peer-to-peer on port {self.port} as {self.name} (network: {self.network['name']})")


def _handler(node):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body=b"", content_type="application/octet-stream"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/challenge":
                return self._send(200, json.dumps(node.new_challenge()).encode(), "application/json")
            self._send(404)

        def do_POST(self):
            if self.path != "/api":
                return self._send(404)
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BODY:
                return self._send(413)
            body = self.rfile.read(length)
            net = node.network["admin"]
            nonce, client_eph = self.headers.get("X-IWB-Nonce", ""), self.headers.get("X-IWB-Eph", "")
            challenge = node.use_challenge(nonce)
            if not challenge:
                return self._send(403)
            _, eph, server_eph = challenge
            try:
                keys = _session_keys(eph, client_eph, net, nonce, server_eph, client_eph)
                req = json.loads(crypto.unseal(keys[0], keys[1], body))
                if req.get("op") == "push_members":
                    node.adopt_members(req.get("members"))
                    resp = None
                else:
                    key = self.headers.get("X-IWB-Key", "")
                    msg = _transcript("req", net, nonce, server_eph, client_eph, hashlib.sha256(body).hexdigest())
                    if key not in node.members["members"] or not _verify(key, msg, self.headers.get("X-IWB-Sig", "")):
                        return self._send(403)
                    resp = node.answer(req)
            except (ValueError, AttributeError, TypeError):
                return self._send(400)
            self._send(200, crypto.seal(keys[2], keys[3], json.dumps(resp).encode()))

    return Handler
