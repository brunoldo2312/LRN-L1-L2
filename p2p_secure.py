"""
p2p_secure.py — Handshake autenticado + canal cifrado para BRN P2P
====================================================================
Protocolo:
  Cliente -> Servidor:  PROTO(9) || id_pub_A(32) || eph_A(32) || sig_A(64)
  Servidor -> Cliente:  PROTO(9) || id_pub_B(32) || eph_B(32) || sig_B(64)

  Chave de sessao:
      shared = X25519(eph_A, eph_B)
      salt   = SHA256(eph_A || eph_B)
      key    = HKDF-SHA256(shared, salt, info="BRN-P2P-session-v1", len=32)

  Framing: [len(4, BE)][ciphertext ChaCha20-Poly1305]
  Nonce:   contador 12 bytes BE, separado por direcao.
====================================================================
"""
import struct
import socket
import hashlib
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

PROTO     = b"BRN-P2P/1"
ROLE_C    = b"|client"
ROLE_S    = b"|server"
MAX_FRAME = 4 * 1024 * 1024
HKDF_INFO = b"BRN-P2P-session-v1"


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("conexao fechada antes do fim do frame")
        buf += chunk
    return buf


def _derive_key(shared, eph_a, eph_b):
    salt = hashlib.sha256(eph_a + eph_b).digest()
    return HKDF(
        algorithm=hashes.SHA256(), length=32,
        salt=salt, info=HKDF_INFO,
    ).derive(shared)


class SecureChannel:
    def __init__(self, key):
        self.aead = ChaCha20Poly1305(key)
        self.tx_nonce = 0
        self.rx_nonce = 0

    def _nonce(self, i):
        return i.to_bytes(12, "big")

    def encrypt(self, plaintext):
        n = self._nonce(self.tx_nonce); self.tx_nonce += 1
        ct = self.aead.encrypt(n, plaintext, None)
        return struct.pack(">I", len(ct)) + ct

    def decrypt_frame(self, frame):
        if len(frame) < 4:
            raise ValueError("frame curto demais")
        (ln,) = struct.unpack(">I", frame[:4])
        if ln > MAX_FRAME:
            raise ValueError(f"frame grande demais: {ln}")
        ct = frame[4:4 + ln]
        n = self._nonce(self.rx_nonce); self.rx_nonce += 1
        return self.aead.decrypt(n, ct, None)

    def send(self, sock, data):
        sock.sendall(self.encrypt(data))

    def recv(self, sock):
        hdr = _recv_exact(sock, 4)
        (ln,) = struct.unpack(">I", hdr)
        if ln > MAX_FRAME:
            raise ValueError(f"frame grande demais: {ln}")
        body = _recv_exact(sock, ln)
        return self.decrypt_frame(hdr + body)


def handshake_client(sock, my_id_priv, expected_pub_hex):
    eph     = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes_raw()
    id_pub  = my_id_priv.public_key().public_bytes_raw()

    sig = my_id_priv.sign(PROTO + id_pub + eph_pub + ROLE_C)
    sock.sendall(PROTO + id_pub + eph_pub + sig)

    if _recv_exact(sock, len(PROTO)) != PROTO:
        raise ConnectionError("proto invalido (server)")
    peer_id_pub  = _recv_exact(sock, 32)
    peer_eph_pub = _recv_exact(sock, 32)
    peer_sig     = _recv_exact(sock, 64)

    try:
        Ed25519PublicKey.from_public_bytes(peer_id_pub).verify(
            peer_sig,
            PROTO + peer_id_pub + peer_eph_pub + eph_pub + ROLE_S,
        )
    except Exception as e:
        raise ConnectionError(f"assinatura do servidor invalida: {e}")

    peer_hex = peer_id_pub.hex()
    if expected_pub_hex is not None and peer_hex != expected_pub_hex:
        raise ConnectionError(
            f"identidade do peer mudou: esperado={expected_pub_hex[:16]}... "
            f"recebido={peer_hex[:16]}..."
        )

    shared = eph.exchange(X25519PublicKey.from_public_bytes(peer_eph_pub))
    key = _derive_key(shared, eph_pub, peer_eph_pub)
    return SecureChannel(key), peer_hex


def handshake_server(sock, my_id_priv):
    if _recv_exact(sock, len(PROTO)) != PROTO:
        raise ConnectionError("proto invalido (client)")
    peer_id_pub  = _recv_exact(sock, 32)
    peer_eph_pub = _recv_exact(sock, 32)
    peer_sig     = _recv_exact(sock, 64)

    try:
        Ed25519PublicKey.from_public_bytes(peer_id_pub).verify(
            peer_sig,
            PROTO + peer_id_pub + peer_eph_pub + ROLE_C,
        )
    except Exception as e:
        raise ConnectionError(f"assinatura do cliente invalida: {e}")

    eph     = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes_raw()
    id_pub  = my_id_priv.public_key().public_bytes_raw()

    sig = my_id_priv.sign(PROTO + id_pub + eph_pub + peer_eph_pub + ROLE_S)
    sock.sendall(PROTO + id_pub + eph_pub + sig)

    shared = eph.exchange(X25519PublicKey.from_public_bytes(peer_eph_pub))
    key = _derive_key(shared, peer_eph_pub, eph_pub)
    return SecureChannel(key), peer_id_pub.hex()