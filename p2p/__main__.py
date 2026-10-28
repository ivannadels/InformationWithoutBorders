"""Network admin and setup commands: python -m p2p --help"""
import argparse
import os
import re
import secrets
import sys

from . import ed25519
from .node import ADMIN, IDENTITY, MEMBERS, NETWORK, _load, _save, sign_members


def _admin_and_members():
    admin = _load(ADMIN)
    if not admin:
        sys.exit("p2p/state/admin.key not found. Only the network admin can change membership.")
    return bytes.fromhex(admin["secret"]), _load(MEMBERS)


ap = argparse.ArgumentParser(prog="python -m p2p", description="Manage your peer-to-peer search network.")
sub = ap.add_subparsers(dest="cmd", required=True)
sub.add_parser("keygen", help="create this device's identity").add_argument("name")
sub.add_parser("init", help="create a new network (you become its admin)").add_argument("name")
p = sub.add_parser("add", help="admin: authorize a member")
p.add_argument("key")
p.add_argument("name")
sub.add_parser("remove", help="admin: remove a member").add_argument("who", help="public key or name")
sub.add_parser("members", help="list members")
a = ap.parse_args()

if a.cmd == "keygen":
    if os.path.exists(IDENTITY):
        sys.exit("p2p/state/identity.key already exists. Delete it first if you really want a new identity.")
    secret = secrets.token_bytes(32)
    _save(IDENTITY, {"name": a.name, "secret": secret.hex()})
    print("Your public key (send this to the network admin):")
    print(ed25519.public_key(secret).hex())

elif a.cmd == "init":
    if os.path.exists(NETWORK):
        sys.exit("p2p/state/network.json already exists.")
    admin = secrets.token_bytes(32)
    pub = ed25519.public_key(admin).hex()
    _save(ADMIN, {"secret": admin.hex()})
    _save(NETWORK, {"name": a.name, "admin": pub})
    members = {"network": pub, "version": 1, "members": {}}
    ident = _load(IDENTITY)
    if ident:
        members["members"][ed25519.public_key(bytes.fromhex(ident["secret"])).hex()] = ident["name"]
    sign_members(members, admin)
    print(f'Created network "{a.name}".')
    print("Keep p2p/state/admin.key private and backed up: whoever has it controls membership,")
    print("and without it membership can never be changed again.")
    print("Give p2p/state/network.json and p2p/state/members.json to each member.")

elif a.cmd == "add":
    admin, m = _admin_and_members()
    key = a.key.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        sys.exit("That doesn't look like a public key (64 hex characters).")
    m["members"][key] = a.name
    m["version"] += 1
    sign_members(m, admin)
    print(f"Added {a.name}. Member list is now version {m['version']} and will spread to peers as they sync.")

elif a.cmd == "remove":
    admin, m = _admin_and_members()
    found = [k for k, n in m["members"].items() if a.who in (k, n)]
    if len(found) != 1:
        sys.exit("No such member." if not found else "More than one member has that name; use the public key.")
    name = m["members"].pop(found[0])
    m["version"] += 1
    sign_members(m, admin)
    print(f"Removed {name}. Member list is now version {m['version']} and will spread to peers as they sync.")

elif a.cmd == "members":
    net, m = _load(NETWORK), _load(MEMBERS)
    if not (net and m):
        sys.exit("No network.json / members.json in p2p/state.")
    print(f"{net['name']}: member list version {m['version']}")
    for k, n in m["members"].items():
        print(f"  {n:24} {k}")
