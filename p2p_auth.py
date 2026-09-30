"""
p2p_auth.py — Autenticacao Ed25519 para handshake P2P do BRN.

Nivel 2: cada mensagem P2P carrega um bloco "_auth" com a assinatura
Ed25519 do remetente sobre (domain || pub || nonce || ts).
NAO criptografa o trafego. So prova posse da chave privada.

Modos (env BRN_P2P_AUTH):
  off       - desabilitado (nao valida nada)
  optional  - valida se vier; aceita se nao vier (com aviso) [padrao]
  required  - rejeita quem nao autentica

Allowlist (env BRN_P2P_ALLOWLIST):
  hex,hex,...  - se preenchida, so aceita pubkeys nesta lista
  vazio        - aceita qualquer pubkey com assinatura valida

Janela (env BRN_P2P_AUTH_WINDOW):
  segundos de tolerancia no timestamp (padrao: 30)
"""
import os
import time
import secrets

# ------------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------------
AUTH_MODE = os.environ.get("BRN_P2P_AUTH", "optional").lower()
ALLOWLIST_RAW = os.environ.get("BRN_P2P_ALLOWLIST", "").strip()
AUTH_WINDOW = int(os.environ.get("BRN_P2P_AUTH_WINDOW", "30"))

ALLOWLIST = set()
for x in ALLOWLIST_RAW.split(","):
    x = x.strip().lower()
    if x:
        ALLOWLIST.add(x)

DOMAIN = b"BRN-AUTH-v1|"

# Global: setado uma vez pelo P2PManager
_NODE_ID_PRIV = None


def set_node_id_priv(priv):
    """Registra a chave privada Ed25519 do no (chamado pelo P2PManager)."""
    global _NODE_ID_PRIV
    _NODE_ID_PRIV = priv


def get_node_id_priv():
    return _NODE_ID_PRIV


# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------
def auth_enabled() -> bool:
    return AUTH_MODE != "off"


def auth_required() -> bool:
    return AUTH_MODE == "required"


def _payload(pub_hex: str, nonce_hex: str, ts: int) -> bytes:
    return DOMAIN + f"{pub_hex}|{nonce_hex}|{ts}".encode()


def build_auth(node_id_priv=None) -> dict:
    """Cria bloco _auth assinado pela chave privada Ed25519."""
    if node_id_priv is None:
        node_id_priv = _NODE_ID_PRIV
    if node_id_priv is None:
        raise RuntimeError("node_id_priv nao configurado")

    pub_hex = node_id_priv.public_key().public_bytes_raw().hex()
    nonce = secrets.token_bytes(32).hex()
    ts = int(time.time())
    sig = node_id_priv.sign(_payload(pub_hex, nonce, ts)).hex()
    return {"pub": pub_hex, "nonce": nonce, "ts": ts, "sig": sig}


def verify_auth(auth: dict, allowlist=None) -> tuple:
    """Verifica um bloco _auth. Retorna (ok, motivo)."""
    if not isinstance(auth, dict):
        return False, "auth nao e dict"

    for k in ("pub", "nonce", "ts", "sig"):
        if k not in auth:
            return False, f"auth faltando campo {k}"

    pub_hex = str(auth["pub"]).lower()
    nonce_hex = str(auth["nonce"])

    try:
        ts = int(auth["ts"])
    except Exception:
        return False, "ts invalido"

    delta = abs(time.time() - ts)
    if delta > AUTH_WINDOW:
        return False, f"ts fora da janela ({delta:.0f}s > {AUTH_WINDOW}s)"

    try:
        pub_bytes = bytes.fromhex(pub_hex)
        nonce_bytes = bytes.fromhex(nonce_hex)
        sig_bytes = bytes.fromhex(str(auth["sig"]))
    except Exception:
        return False, "hex invalido"

    if len(pub_bytes) != 32:
        return False, "pubkey deve ter 32 bytes (Ed25519)"
    if len(nonce_bytes) != 32:
        return False, "nonce deve ter 32 bytes"
    if len(sig_bytes) != 64:
        return False, "assinatura deve ter 64 bytes"

    al = ALLOWLIST if allowlist is None else allowlist
    if al and pub_hex not in al:
        return False, "pubkey nao esta na allowlist"

    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pk = Ed25519PublicKey.from_public_bytes(pub_bytes)
        pk.verify(sig_bytes, _payload(pub_hex, nonce_hex, ts))
    except Exception:
        return False, "assinatura Ed25519 invalida"

    return True, ""