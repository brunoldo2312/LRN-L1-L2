@echo off
cd /d "%~dp0"

echo === Criando crypto.py ===
(
echo """crypto.py - Funcoes criptograficas do BRN"""
echo import hashlib
echo import secrets
echo.
echo.
echo def sha256^(data: bytes^) -^> bytes:
echo     return hashlib.sha256^(data^).digest^(^)
echo.
echo.
echo def double_sha256^(data: bytes^) -^> bytes:
echo     return hashlib.sha256^(hashlib.sha256^(data^).digest^(^)^).digest^(^)
echo.
echo.
echo def ripemd160^(data: bytes^) -^> bytes:
echo     try:
echo         h = hashlib.new^("ripemd160"^)
echo         h.update^(data^)
echo         return h.digest^(^)
echo     except ValueError:
echo         return hashlib.sha256^(data^).digest^(^)[:20]
echo.
echo.
echo def hash160^(data: bytes^) -^> bytes:
echo     return ripemd160^(sha256^(data^)^)
echo.
echo.
echo def generate_private_key^(^) -^> bytes:
echo     N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
echo     while True:
echo         k = secrets.token_bytes^(32^)
echo         val = int.from_bytes^(k, "big"^)
echo         if 1 ^<= val ^< N:
echo             return k
echo.
echo.
echo P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
echo N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
echo Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
echo Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
echo.
echo.
echo def _inv_mod^(a: int, m: int^) -^> int:
echo     return pow^(a, m - 2, m^)
echo.
echo.
echo def _point_add^(p1, p2^):
echo     if p1 is None:
echo         return p2
echo     if p2 is None:
echo         return p1
echo     x1, y1 = p1
echo     x2, y2 = p2
echo     if x1 == x2 and ^(y1 + y2^) %% P == 0:
echo         return None
echo     if p1 == p2:
echo         lam = ^(3 * x1 * x1^) * _inv_mod^(2 * y1, P^) %% P
echo     else:
echo         lam = ^(y2 - y1^) * _inv_mod^(x2 - x1, P^) %% P
echo     x3 = ^(lam * lam - x1 - x2^) %% P
echo     y3 = ^(lam * ^(x1 - x3^) - y1^) %% P
echo     return ^(x3, y3^)
echo.
echo.
echo def _point_mul^(k: int, point^):
echo     result = None
echo     addend = point
echo     while k:
echo         if k ^& 1:
echo             result = _point_add^(result, addend^)
echo         addend = _point_add^(addend, addend^)
echo         k ^>^>= 1
echo     return result
echo.
echo.
echo def pubkey_from_priv^(priv_bytes: bytes, compressed: bool = True^) -^> bytes:
echo     k = int.from_bytes^(priv_bytes, "big"^)
echo     if k == 0 or k ^>= N:
echo         raise ValueError^("Chave privada fora do range"^)
echo     x, y = _point_mul^(k, ^(Gx, Gy^)^)
echo     if compressed:
echo         prefix = b"\x02" if y %% 2 == 0 else b"\x03"
echo         return prefix + x.to_bytes^(32, "big"^)
echo     return b"\x04" + x.to_bytes^(32, "big"^) + y.to_bytes^(32, "big"^)
echo.
echo.
echo def sign_ecdsa^(priv_bytes: bytes, msg_hash: bytes^) -^> bytes:
echo     z = int.from_bytes^(msg_hash, "big"^)
echo     d = int.from_bytes^(priv_bytes, "big"^)
echo     while True:
echo         k = secrets.randbelow^(N - 1^) + 1
echo         x, y = _point_mul^(k, ^(Gx, Gy^)^)
echo         r = x %% N
echo         if r == 0:
echo             continue
echo         s = ^(_inv_mod^(k, N^) * ^(z + r * d^)^) %% N
echo         if s == 0:
echo             continue
echo         if s ^> N // 2:
echo             s = N - s
echo         return r.to_bytes^(32, "big"^) + s.to_bytes^(32, "big"^)
echo.
echo.
echo def verify_ecdsa^(pub_bytes: bytes, sig_bytes: bytes, msg_hash: bytes^) -^> bool:
echo     try:
echo         if len^(sig_bytes^) != 64:
echo             return False
echo         r = int.from_bytes^(sig_bytes[:32], "big"^)
echo         s = int.from_bytes^(sig_bytes[32:], "big"^)
echo         if not ^(1 ^<= r ^< N and 1 ^<= s ^< N^):
echo             return False
echo         if pub_bytes[0] == 0x04 and len^(pub_bytes^) == 65:
echo             x = int.from_bytes^(pub_bytes[1:33], "big"^)
echo             y = int.from_bytes^(pub_bytes[33:], "big"^)
echo             Q = ^(x, y^)
echo         elif pub_bytes[0] in ^(0x02, 0x03^) and len^(pub_bytes^) == 33:
echo             x = int.from_bytes^(pub_bytes[1:], "big"^)
echo             y_sq = ^(pow^(x, 3, P^) + 7^) %% P
echo             y = pow^(y_sq, ^(P + 1^) // 4, P^)
echo             if ^(y %% 2 == 0^) != ^(pub_bytes[0] == 0x02^):
echo                 y = P - y
echo             Q = ^(x, y^)
echo         else:
echo             return False
echo         z = int.from_bytes^(msg_hash, "big"^)
echo         w = _inv_mod^(s, N^)
echo         u1 = ^(z * w^) %% N
echo         u2 = ^(r * w^) %% N
echo         P1 = _point_mul^(u1, ^(Gx, Gy^)^)
echo         P2 = _point_mul^(u2, Q^)
echo         R = _point_add^(P1, P2^)
echo         if R is None:
echo             return False
echo         return ^(R[0] %% N^) == r
echo     except Exception:
echo         return False
echo.
echo.
echo sign_schnorr = sign_ecdsa
echo verify_schnorr = verify_ecdsa
) > crypto.py

echo crypto.py criado!
pause