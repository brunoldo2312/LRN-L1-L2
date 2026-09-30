"""
secure_store.py — Armazenamento cifrado local para o no BRN
============================================================
Formato do arquivo:
    [MAGIC:5][hdr_len:4 BE][header_json][ciphertext + tag]

MAGIC        = b"BRNS1"
header_json  = JSON canonico (sort_keys, separators sem espaco)
ciphertext   = ChaCha20-Poly1305(key, nonce, AAD=header_completo)

KDF          = Argon2id (time=3, memory=64MiB, par=2, len=32)
AEAD         = ChaCha20-Poly1305 (nonce 12 bytes aleatorio por blob)

AAD (associated data) = MAGIC || hdr_len || header_json
  => qualquer alteracao no header (params do KDF, salt, nonce, versao)
     faz a decifragem falhar. Sem rebaixar parametros de KDF.

Escrita atomica: grava em .tmp e usa os.replace (POSIX atomico).

Limitacao reconhecida:
  Python nao permite zeroizar com garantia bytes imutaveis em memoria.
  Mitigamos nao mantendo referencias alem do necessario, mas nao ha
  garantia criptografica de wipe. Para seguranca real contra adversario
  com acesso a memoria, use HSM ou enclave.
"""
import os
import io
import json
import hmac
import base64
import secrets
from dataclasses import dataclass
from pathlib import Path

from argon2.low_level import hash_secret_raw, Type
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305


# ============================================================
# CONSTANTES DO FORMATO
# ============================================================
MAGIC          = b"BRNS1"
SALT_LEN       = 16
NONCE_LEN      = 12
KEY_LEN        = 32
HDR_LEN_BYTES  = 4
MAX_HDR_LEN    = 64 * 1024          # 64 KiB — folga generosa
MIN_PASSWORD_LEN = 12               # aviso abaixo disso


# ============================================================
# PARAMETROS DO KDF
# ============================================================
@dataclass(frozen=True)
class KDFParams:
    time_cost:    int = 3
    memory_cost:  int = 64 * 1024   # 64 MiB
    parallelism:  int = 2

    def to_header(self) -> dict:
        return {
            "tc": self.time_cost,
            "mc": self.memory_cost,
            "p":  self.parallelism,
        }

    @classmethod
    def from_header(cls, h: dict) -> "KDFParams":
        return cls(
            time_cost=int(h["tc"]),
            memory_cost=int(h["mc"]),
            parallelism=int(h["p"]),
        )


# ============================================================
# DERIVACAO DE CHAVE
# ============================================================
def _derive_key(password: str, salt: bytes, params: KDFParams) -> bytes:
    return hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=params.time_cost,
        memory_cost=params.memory_cost,
        parallelism=params.parallelism,
        hash_len=KEY_LEN,
        type=Type.ID,
    )


# ============================================================
# HEADER CANONICO
# ============================================================
def _canonical_header(h: dict) -> bytes:
    return json.dumps(
        h, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


# ============================================================
# CIFRAR
# ============================================================
def encrypt_blob(plaintext: bytes, password: str) -> bytes:
    """
    Cifra `plaintext` com a senha. Retorna bytes auto-contidos.
    """
    if not isinstance(password, str) or not password:
        raise ValueError("senha vazia ou invalida")

    if len(password) < MIN_PASSWORD_LEN:
        # warning nao bloqueia, mas deixa claro
        import sys
        print(
            f"[secure_store] AVISO: senha com {len(password)} caracteres. "
            f"Recomendado >= {MIN_PASSWORD_LEN}.",
            file=sys.stderr,
        )

    salt  = secrets.token_bytes(SALT_LEN)
    nonce = secrets.token_bytes(NONCE_LEN)
    params = KDFParams()

    header = {
        "v": 1,
        "kdf": "argon2id",
        "cipher": "chacha20poly1305",
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        **params.to_header(),
    }
    hbytes = _canonical_header(header)
    hlen   = len(hbytes).to_bytes(HDR_LEN_BYTES, "big")

    aad = MAGIC + hlen + hbytes

    key = _derive_key(password, salt, params)
    ct  = ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)

    return aad + ct


# ============================================================
# DECIFRAR
# ============================================================
def decrypt_blob(blob: bytes, password: str) -> bytes:
    """
    Decifra um blob produzido por encrypt_blob.
    Levanta ValueError com mensagem generica em caso de falha
    (nao revela se foi senha, corrupcao ou adulteracao).
    """
    if not isinstance(password, str) or not password:
        raise ValueError("senha vazia ou invalida")

    if len(blob) < len(MAGIC) + HDR_LEN_BYTES + 16:
        raise ValueError("blob invalido (curto demais)")

    if blob[:len(MAGIC)] != MAGIC:
        raise ValueError("formato desconhecido ou corrompido")

    off = len(MAGIC)
    hlen = int.from_bytes(blob[off:off + HDR_LEN_BYTES], "big")
    off += HDR_LEN_BYTES

    if hlen == 0 or hlen > MAX_HDR_LEN:
        raise ValueError("header invalido")

    if len(blob) < off + hlen + 16:
        raise ValueError("blob truncado")

    hbytes = blob[off:off + hlen]
    off += hlen
    ct = blob[off:]

    try:
        header = json.loads(hbytes.decode("utf-8"))
    except Exception:
        raise ValueError("header nao e JSON valido")

    # Validacao estrita do header — defesa contra downgrade
    if header.get("v") != 1:
        raise ValueError("versao de formato nao suportada")
    if header.get("kdf") != "argon2id":
        raise ValueError("KDF nao suportado")
    if header.get("cipher") != "chacha20poly1305":
        raise ValueError("cifra nao suportada")

    try:
        salt  = base64.b64decode(header["salt"], validate=True)
        nonce = base64.b64decode(header["nonce"], validate=True)
    except Exception:
        raise ValueError("salt/nonce invalidos no header")

    if len(salt) != SALT_LEN or len(nonce) != NONCE_LEN:
        raise ValueError("salt/nonce com tamanho invalido")

    params = KDFParams.from_header(header)

    # Sanidade dos parametros — evita DoS via memory_cost gigantesco
    if params.memory_cost > 1024 * 1024:      # 1 GiB
        raise ValueError("memory_cost excessivo no header")
    if params.time_cost > 16:
        raise ValueError("time_cost excessivo no header")
    if params.parallelism > 8:
        raise ValueError("parallelism excessivo no header")

    aad = MAGIC + hlen.to_bytes(HDR_LEN_BYTES, "big") + hbytes

    key = _derive_key(password, salt, params)
    try:
        return ChaCha20Poly1305(key).decrypt(nonce, ct, aad)
    except Exception:
        # Nao diferencia senha errada de arquivo adulterado
        raise ValueError("falha ao decifrar (senha errada ou arquivo adulterado)")


# ============================================================
# API DE ALTO NIVEL — WALLET / IDENTIDADE
# ============================================================
def save_wallet(path: str, wallet_json: dict, password: str) -> None:
    """
    Salva um dict como JSON cifrado. Escrita atomica.
    Cria diretorio pai se nao existir.
    """
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)

    data = json.dumps(wallet_json, separators=(",", ":"), sort_keys=True).encode("utf-8")
    blob = encrypt_blob(data, password)

    tmp = p.with_suffix(p.suffix + ".tmp")
    # Permissoes restritas ANTES de escrever
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.unlink(str(tmp))
        except Exception:
            pass
        raise

    os.replace(str(tmp), str(p))


def load_wallet(path: str, password: str) -> dict:
    """
    Le um dict JSON de um arquivo cifrado.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"arquivo nao existe: {p}")

    with open(p, "rb") as f:
        blob = f.read()

    data = decrypt_blob(blob, password)
    return json.loads(data.decode("utf-8"))


# ============================================================
# UTILITARIOS
# ============================================================
def is_encrypted_file(path: str) -> bool:
    """Verifica se o arquivo comeca com MAGIC (heuristica barata)."""
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except Exception:
        return False


def reencrypt_file(path: str, old_password: str, new_password: str) -> None:
    """
    Troca a senha de um arquivo cifrado, reescrevendo-o com nova senha.
    Le, decifra, cifra com nova senha, grava atomicamente.
    """
    if not old_password or not new_password:
        raise ValueError("senhas obrigatorias")
    data = load_wallet(path, old_password)   # dict
    save_wallet(path, data, new_password)


def _zeroize_bytearray(buf: bytearray) -> None:
    """
    Sobrescreve um bytearray com zeros. So funciona para bytearray
    (mutable). bytes/str sao imutaveis em Python e nao podem ser
    zeroizados com garantia — o GC pode ter copiado.
    """
    for i in range(len(buf)):
        buf[i] = 0


# ============================================================
# AUTOTESTE
# ============================================================
if __name__ == "__main__":
    print("Testando secure_store...")

    # round-trip
    secret = b"chave-privada-super-secreta-12345"
    pw = "minha-senha-forte-de-teste-2026"
    blob = encrypt_blob(secret, pw)
    assert decrypt_blob(blob, pw) == secret, "round-trip falhou"
    print("  [ok] round-trip")

    # senha errada
    try:
        decrypt_blob(blob, "senha-errada-xxxxxxx")
        raise AssertionError("senha errada NAO deveria decifrar")
    except ValueError:
        print("  [ok] senha errada rejeitada")

    # adulteracao no ciphertext
    tampered = bytearray(blob)
    tampered[-1] ^= 0x01
    try:
        decrypt_blob(bytes(tampered), pw)
        raise AssertionError("adulteracao NAO deveria decifrar")
    except ValueError:
        print("  [ok] adulteracao detectada")

    # adulteracao no header (downgrade de KDF)
    import base64 as _b64
    h_len = int.from_bytes(blob[len(MAGIC):len(MAGIC)+HDR_LEN_BYTES], "big")
    off_h = len(MAGIC) + HDR_LEN_BYTES
    hdr = json.loads(blob[off_h:off_h+h_len].decode())
    hdr["tc"] = 1                        # tentativa de downgrade
    new_hdr = json.dumps(hdr, sort_keys=True, separators=(",", ":")).encode()
    downgraded = (
        MAGIC
        + len(new_hdr).to_bytes(HDR_LEN_BYTES, "big")
        + new_hdr
        + blob[off_h + h_len:]
    )
    try:
        decrypt_blob(downgraded, pw)
        raise AssertionError("downgrade NAO deveria decifrar")
    except ValueError:
        print("  [ok] downgrade de KDF detectado")

    # arquivo + round-trip em disco
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "test.enc")
        save_wallet(p, {"a": 1, "b": [2, 3]}, pw)
        assert is_encrypted_file(p)
        d = load_wallet(p, pw)
        assert d == {"a": 1, "b": [2, 3]}
        print("  [ok] disco round-trip")

        # reencrypt
        pw2 = "outra-senha-bem-forte-2026"
        reencrypt_file(p, pw, pw2)
        assert load_wallet(p, pw2) == {"a": 1, "b": [2, 3]}
        try:
            load_wallet(p, pw)
            raise AssertionError("senha antiga NAO deveria funcionar")
        except ValueError:
            print("  [ok] reencrypt")

    print("Todos os testes passaram.")