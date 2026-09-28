"""crypto.py - Funcoes criptograficas do BRN"""
import hashlib
import secrets


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def double_sha256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def ripemd160(data: bytes) -> bytes:
    try:
        h = hashlib.new("ripemd160")
        h.update(data)
        return h.digest()
    except ValueError:
        return hashlib.sha256(data).digest()[:20]


def hash160(data: bytes) -> bytes:
    return ripemd160(sha256(data))


def generate_private_key() -> bytes:
    N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
    while True:
        k = secrets.token_bytes(32)
        val = int.from_bytes(k, "big")
        if 1 <= val < N:
            return k


P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8


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


def pubkey_from_priv(priv_bytes: bytes, compressed: bool = True) -> bytes:
    k = int.from_bytes(priv_bytes, "big")
    if k == 0 or k >= N:
        raise ValueError("Chave privada fora do range")
    x, y = _point_mul(k, (Gx, Gy))
    if compressed:
        prefix = b"\x02" if y % 2 == 0 else b"\x03"
        return prefix + x.to_bytes(32, "big")
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


def sign_ecdsa(priv_bytes: bytes, msg_hash: bytes) -> bytes:
    z = int.from_bytes(msg_hash, "big")
    d = int.from_bytes(priv_bytes, "big")
    while True:
        k = secrets.randbelow(N - 1) + 1
        x, y = _point_mul(k, (Gx, Gy))
        r = x % N
        if r == 0:
            continue
        s = (_inv_mod(k, N) * (z + r * d)) % N
        if s == 0:
            continue
        if s > N // 2:
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
        if pub_bytes[0] == 0x04 and len(pub_bytes) == 65:
            x = int.from_bytes(pub_bytes[1:33], "big")
            y = int.from_bytes(pub_bytes[33:], "big")
            Q = (x, y)
        elif pub_bytes[0] in (0x02, 0x03) and len(pub_bytes) == 33:
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


sign_schnorr = sign_ecdsa
verify_schnorr = verify_ecdsa
