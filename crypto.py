"""
crypto.py — Primitivas criptograficas do BRN (v6)
================================================================
v6:
  - Reexporta Ed25519PrivateKey/Ed25519PublicKey (usados por
    p2p_secure.py, main.py).
  - Adiciona pubkey_to_address (delega para bech32).
  - verify_ecdsa rejeita high-S (anti-malleability).
  - ripemd160 nao faz fallback silencioso — levanta RuntimeError
    a menos que BRN_LEGACY_HASH160_FALLBACK=1 (compat com cadeias
    antigas criadas no modo fallback).

AVISO — NOMENCLATURA:
  sign_schnorr/verify_schnorr sao ALIASES de sign_ecdsa/verify_ecdsa.
  O BRN hoje usa ECDSA secp256k1, NAO BIP340. Os nomes foram mantidos
  por compatibilidade com wallet.py. Migre para BIP340 de forma
  versionada (adicione sig_type na tx) se quiser Schnorr real.

AVISO — IMPLEMENTACAO:
  ECDSA aqui e Python puro. pow() nao e constant-time. Um adversario
  capaz de medir tempos pode, em teoria, extrair a chave. Para
  producao com valor real, migre para a lib `cryptography` ou Ed25519.
================================================================
"""
import os
import hashlib
import secrets

# v6: reexport — permite `from crypto import Ed25519PrivateKey`
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


# ============================================================
# HASHES
# ============================================================
def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def double_sha256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


_ripemd_warned = False


def ripemd160(data: bytes) -> bytes:
    """
    RIPEMD-160 real. Sem fallback silencioso.

    Se o backend nao tem ripemd160 e BRN_LEGACY_HASH160_FALLBACK=1,
    usa sha256[:20] com warning (compat com carteiras antigas).
    Caso contrario, levanta RuntimeError.
    """
    global _ripemd_warned
    try:
        h = hashlib.new("ripemd160")
        h.update(data)
        return h.digest()
    except ValueError:
        if os.environ.get("BRN_LEGACY_HASH160_FALLBACK") == "1":
            if not _ripemd_warned:
                import sys
                print(
                    "[crypto] AVISO: ripemd160 indisponivel. Usando "
                    "sha256[:20] como fallback LEGADO. Defina "
                    "BRN_LEGACY_HASH160_FALLBACK=0 e reconstrua o "
                    "backend para sair deste modo.",
                    file=sys.stderr,
                )
                _ripemd_warned = True
            return hashlib.sha256(data).digest()[:20]
        raise RuntimeError(
            "ripemd160 indisponivel neste build.\n"
            "Instale pycryptodome OU habilite o provider legacy do "
            "OpenSSL. Para compatibilidade com carteiras antigas:\n"
            "  export BRN_LEGACY_HASH160_FALLBACK=1"
        )


def hash160(data: bytes) -> bytes:
    return ripemd160(sha256(data))


# ============================================================
# CURVA secp256k1
# ============================================================
P  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

_HALF_N = N // 2


def generate_private_key() -> bytes:
    while True:
        k = secrets.token_bytes(32)
        val = int.from_bytes(k, "big")
        if 1 <= val < N:
            return k


# ============================================================
# ARITMETICA DE CURVA (nao constante em tempo)
# ============================================================
def _inv_mod(a: int, m: int) -> int:
    return pow(a, m - 2, m)


def _point_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    if p1 == p2:
        lam = (3 * x1 * x1) * _inv_mod(2 * y1, P) % P
    else:
        lam = (y2 - y1) * _inv_mod(x2 - x1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    y3 = (lam * (x1 - x3) - y1) % P
    return (x3, y3)


def _point_mul(k: int, point):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result


# ============================================================
# PUBKEY
# ============================================================
def pubkey_from_priv(priv_bytes: bytes, compressed: bool = True) -> bytes:
    k = int.from_bytes(priv_bytes, "big")
    if k == 0 or k >= N:
        raise ValueError("Chave privada fora do range")
    x, y = _point_mul(k, (Gx, Gy))
    if compressed:
        prefix = b"\x02" if y % 2 == 0 else b"\x03"
        return prefix + x.to_bytes(32, "big")
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def pubkey_to_address(pubkey_hex: str) -> str:
    """v6: derivar endereco Bech32 de uma pubkey (hex)."""
    from bech32 import address_from_pubkey
    return address_from_pubkey(bytes.fromhex(pubkey_hex))


# ============================================================
# ECDSA secp256k1
# ============================================================
def sign_ecdsa(priv_bytes: bytes, msg_hash: bytes) -> bytes:
    z = int.from_bytes(msg_hash, "big")
    d = int.from_bytes(priv_bytes, "big")
    if d == 0 or d >= N:
        raise ValueError("Chave privada fora do range")
    while True:
        k = secrets.randbelow(N - 1) + 1
        x, y = _point_mul(k, (Gx, Gy))
        r = x % N
        if r == 0:
            continue
        s = (_inv_mod(k, N) * (z + r * d)) % N
        if s == 0:
            continue
        if s > _HALF_N:
            s = N - s
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def verify_ecdsa(pub_bytes: bytes, sig_bytes: bytes, msg_hash: bytes) -> bool:
    try:
        if len(sig_bytes) != 64:
            return False
        r = int.from_bytes(sig_bytes[:32], "big")
        s = int.from_bytes(sig_bytes[32:], "big")
        if not (1 <= r < N and 1 <= s < N):
            return False
        if s > _HALF_N:
            return False

        if len(pub_bytes) == 65 and pub_bytes[0] == 0x04:
            x = int.from_bytes(pub_bytes[1:33], "big")
            y = int.from_bytes(pub_bytes[33:], "big")
            Q = (x, y)
        elif len(pub_bytes) == 33 and pub_bytes[0] in (0x02, 0x03):
            x = int.from_bytes(pub_bytes[1:], "big")
            y_sq = (pow(x, 3, P) + 7) % P
            y = pow(y_sq, (P + 1) // 4, P)
            if (y % 2 == 0) != (pub_bytes[0] == 0x02):
                y = P - y
            Q = (x, y)
        else:
            return False

        z = int.from_bytes(msg_hash, "big")
        w = _inv_mod(s, N)
        u1 = (z * w) % N
        u2 = (r * w) % N
        P1 = _point_mul(u1, (Gx, Gy))
        P2 = _point_mul(u2, Q)
        R = _point_add(P1, P2)
        if R is None:
            return False
        return (R[0] % N) == r
    except Exception:
        return False


# ============================================================
# ALIASES (compat wallet.py)
# ============================================================
sign_schnorr   = sign_ecdsa
verify_schnorr = verify_ecdsa