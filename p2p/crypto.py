"""
Encryption for peer-to-peer traffic, standard library only.

Key exchange: X25519 (RFC 7748) in pure Python, following the RFC's Montgomery ladder.
Messages: a SHAKE-256 keystream XORed with the plaintext, then HMAC-SHA256 over the
ciphertext (encrypt-then-MAC). Python ships no standard cipher, and these hashlib
primitives run in C, unlike a pure-Python ChaCha20 or AES. Every request derives its own
keys from a fresh key exchange, so a key never encrypts more than one message.
"""
import hashlib
import hmac

p = 2**255 - 19
A24 = 121665


def x25519(k, u):
    if len(k) != 32 or len(u) != 32:
        raise ValueError("X25519 keys are 32 bytes")
    k = bytearray(k)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    k = int.from_bytes(k, "little")
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in reversed(range(255)):
        bit = (k >> t) & 1
        if swap ^ bit:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b, c, d = x2 + z2, x2 - z2, x3 + z3, x3 - z3
        aa, bb, da, cb = a * a % p, b * b % p, d * a % p, c * b % p
        e = aa - bb
        x3, z3 = (da + cb) ** 2 % p, x1 * (da - cb) ** 2 % p
        x2, z2 = aa * bb % p, e * (aa + A24 * e) % p
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, p - 2, p) % p).to_bytes(32, "little")


def x25519_public(secret):
    return x25519(secret, (9).to_bytes(32, "little"))


def derive(shared, context):
    """Four 32-byte keys (request enc/mac, response enc/mac) from a key exchange."""
    if shared == bytes(32):
        raise ValueError("bad key exchange")
    prk = hmac.new(context, shared, hashlib.sha256).digest()
    return [hmac.new(prk, label, hashlib.sha256).digest()
            for label in (b"req-enc", b"req-mac", b"resp-enc", b"resp-mac")]


def _xor(data, enc_key):
    stream = hashlib.shake_256(b"iwb-stream" + enc_key).digest(len(data))
    return (int.from_bytes(data, "little") ^ int.from_bytes(stream, "little")).to_bytes(len(data), "little")


def seal(enc_key, mac_key, data):
    ct = _xor(data, enc_key)
    return ct + hmac.new(mac_key, ct, hashlib.sha256).digest()


def unseal(enc_key, mac_key, blob):
    ct, tag = blob[:-32], blob[-32:]
    if len(blob) < 32 or not hmac.compare_digest(tag, hmac.new(mac_key, ct, hashlib.sha256).digest()):
        raise ValueError("message failed authentication")
    return _xor(ct, enc_key)
