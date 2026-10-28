"""
Peer-to-peer sync for the offline search engine.
Uses only the Python standard library.

Members of a network copy each other's rows into their own database, so every device
can search everything locally, and keeps it after the peers go away. Peers find each
other automatically when they are on the same local network (Wi-Fi or router, no
internet needed) and sync whenever they meet.

Setup (run from the folder that contains p2p/):
  Admin, once:        python -m p2p init "Network Name"
  Each member:        python -m p2p keygen "Your Name"       (prints your public key)
  Admin, per member:  python -m p2p add <public key> "Their Name"
  The admin gives each member a copy of p2p/state/network.json and p2p/state/members.json
  (USB stick etc.). Later membership changes spread to peers by themselves during sync.

  Remove someone:     python -m p2p remove <public key or name>
  List members:       python -m p2p members

Documents are identified by their catalogue id and are only ever added by members.
Only the admin can take one out of the network:
  Remove a document:  python -m p2p remove-doc <doc id>     (deleted on every device as they sync)
  Undo that:          python -m p2p restore-doc <doc id>

Security: only keys on the admin-signed member list can sync. All traffic between peers
is encrypted with fresh keys per request (see crypto.py), and both sides prove who they
are before any data is sent. The local-network announcements are not encrypted: they
reveal that a device on this network runs this software, but nothing about the data.
"""
from .node import Node, removed_docs
