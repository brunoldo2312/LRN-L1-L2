"""
crypto.py — Primitivas criptograficas do BRN (v6)
================================================================
v6:
  - Reexporta Ed25519PrivateKey/Ed25519PublicKey para uso em
    p2p_secure.py, main.py e secure_store consumers.
  - Adiciona pubkey_to_address (wrapper de bech32.address_from_pubkey).
  - verify_ecdsa passa a rejeitar high-S (malleability classica).
  - ripemd160 nao faz mais fallback silencioso para sha256[:20].
    Se o backend nao tem ripemd160, levanta RuntimeError — a menos
    que BRN_LEGACY_HASH160_FALLBACK=1 esteja setado (compat com
    cadeias ja existentes criadas no modo antigo).

AVISO — NOMENCLATURA:
  As funcoes sign_schnorr/verify_schnorr sao ALIASES de
  sign_ecdsa/verify_ecdsa. O BRN hoje usa ECDSA secp256k1, NAO
  BIP340 Schnorr. Os nomes "schnorr" foram mantidos por
  compatibilidade com wallet.py e outros modulos. Se voce quiser
  migrar para BIP340 real, faca de forma versionada: adicione
  SIG_TYPE = "bip340" nas txs, mantenha ECDSA por um periodo de
  transicao, depois force o novo esquema numa altura de bloco.

AVISO — IMPLEMENTACAO:
  ECDSA aqui e Python puro. pow() do Python NAO e constant-time.
  Um adversario capaz de medir tempos de assinatura pode, em teoria,
  extrair a chave privada. Para producao com valor real, migre para
  `cryptography` library ou Ed25519 — ver MIGRACAO no fim do arquivo.
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

    Se o build atual de Python/OpenSSL nao tem ripemd160 e a env
    BRN_LEGACY_HASH160_FALLBACK NAO esta setada, levanta RuntimeError.

    Se BRN_LEGACY_HASH160_FALLBACK=1, usa sha256[:20] com warning —
    isso mantem compatibilidade com carteiras criadas antes desta
    correcao, mas NAO e um esquema seguro nem interoperavel.
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
            "ripemd160 indisponivel neste build de Python/OpenSSL.\n"
            "Instale pycryptodome OU habilite o provider legacy do "
            "OpenSSL. Para manter compatibilidade com carteiras "
            "antigas criadas no modo fallback, defina:\n"
            "  export BRN_LEGACY_HASH160_FALLBACK=1"
        )


def hash160(data: bytes) -> bytes:
    return ripemd160(sha256(data))


# ============================================================
# CHAVE PRIVADA
# ============================================================
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
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
    """
    v6: derivar endereco Bech32 a partir de uma pubkey (hex).
    Delega para bech32.address_from_pubkey — a fonte unica da verdade
    do esquema de endereco. Nao reimplementa hashing aqui.
    """
    from bech32 import address_from_pubkey
    return address_from_pubkey(bytes.fromhex(pubkey_hex))


# ============================================================
# ECDSA secp256k1 (Schnorr NAO implementado aqui)
# ============================================================
def sign_ecdsa(priv_bytes: bytes, msg_hash: bytes) -> bytes:
    z = int.from_bytes(msg_hash, "big")
    # ECDSA trabalha com o truncamento do hash ao tamanho do campo.
    # Se msg_hash > 256 bits, já foi truncado pelo caller.
    d = int.from_bytes(priv_bytes, "big")
    if d == 0 or d >= N:
        raise ValueError("Chave privada fora do range")
    while True:
        # k uniforme em [1, N-1]
        k = secrets.randbelow(N - 1) + 1
        x, y = _point_mul(k, (Gx, Gy))
        r = x % N
        if r == 0:
            continue
        s = (_inv_mod(k, N) * (z + r * d)) % N
        if s == 0:
            continue
        # low-S (BIP62-like) — reduz malleability
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
        # v6: rejeita high-S (evita malleability)
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
# ALIASES LEGADOS (mantidos para wallet.py)
# ============================================================
# ATENCAO: nao sao BIP340 Schnorr. Sao ECDSA com outro nome.
# Nao mude o comportamento sem versionar o formato da tx.
sign_schnorr = sign_ecdsa
verify_schnorr = verify_ecdsa


# ============================================================
# MIGRACAO (para o futuro)
# ============================================================
# Quando o projeto tiver valor real, considere:
#
# 1) Substituir o ECDSA hand-rolled por `cryptography` library:
#    - constant-time (implementado em C)
#    - assinatura DER, precisa converter para r||s
#    - chave privada usa `ec.derive_private_key(d, ec.SECP256K1())`
#
# 2) Ou migrar para Ed25519:
#    - chaves de 32 bytes, assinaturas de 64 bytes fixas
#    - sem nonce (deterministico) — imune a falha de RNG
#    - ja disponivel via `cryptography` (que voce usa para o P2P)
#    - requer nova derivacao de endereco (pubkey 32B, nao 33B)
#
# Em qualquer caso, adicione sig_type na tx e mantenha os dois
# caminhos ativos por uma janela de transicao.