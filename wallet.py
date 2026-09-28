"""
wallet.py — Carteira BRN com Schnorr (padrão) e ECDSA (fallback)
================================================================
✅ v3: Adiciona métodos públicos compatíveis com server.py e app_wallet.py
✅ v3: Adiciona classe WalletManager (fachada para a carteira desktop)
✅ v3: Adiciona assinatura/verificação de transações BRN
================================================================
"""
import json
import base64
import os
import secrets
from crypto import (
    sha256, pubkey_from_priv, sign_schnorr, verify_schnorr,
    sign_ecdsa, verify_ecdsa, generate_private_key,
)
from bech32 import address_from_pubkey

SIG_MODE = "schnorr"


class Wallet:
    def __init__(self, private_key_hex: str | None = None):
        if private_key_hex:
            self.priv = bytes.fromhex(private_key_hex)
        else:
            self.priv = generate_private_key()
        self.pub = pubkey_from_priv(self.priv)
        self.address = address_from_pubkey(self.pub)

    # ---------- propriedades ----------
    @property
    def priv_hex(self) -> str:
        return self.priv.hex()

    @property
    def pub_hex(self) -> str:
        return self.pub.hex()

    # ✅ NOVO: métodos que server.py estava chamando
    def private_key_hex(self) -> str:
        return self.priv_hex

    def public_key_hex(self) -> str:
        return self.pub_hex

    # ---------- assinatura ----------
    def sign(self, msg_hash: bytes) -> str:
        if SIG_MODE == "schnorr":
            return sign_schnorr(self.priv, msg_hash).hex()
        return sign_ecdsa(self.priv, msg_hash).hex()

    @staticmethod
    def verify(msg_hash: bytes, sig_hex: str, pub_hex: str) -> bool:
        try:
            sig = bytes.fromhex(sig_hex)
            pub = bytes.fromhex(pub_hex)
            if SIG_MODE == "schnorr":
                return verify_schnorr(pub, sig, msg_hash)
            return verify_ecdsa(pub, sig, msg_hash)
        except Exception:
            return False

    # ---------- serialização ----------
    def to_dict(self) -> dict:
        return {
            "private_key": self.priv_hex,
            "address": self.address,
            "pubkey": self.pub_hex,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Wallet":
        return cls(private_key_hex=d["private_key"])

    # ---------- criptografia local ----------
    def export_encrypted(self, password: str) -> str:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        salt = os.urandom(16)
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                         salt=salt, iterations=600_000)
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
        token = Fernet(key).encrypt(json.dumps(self.to_dict()).encode())
        return base64.b64encode(salt + token).decode()

    @classmethod
    def import_encrypted(cls, blob_b64: str, password: str) -> "Wallet":
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        blob = base64.b64decode(blob_b64)
        salt, token = blob[:16], blob[16:]
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                         salt=salt, iterations=600_000)
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
        data = json.loads(Fernet(key).decrypt(token).decode())
        return cls.from_dict(data)


# ============================================================
# ✅ NOVO: WalletManager (fachada usada pelo app_wallet.py)
# ============================================================
class WalletManager:
    """
    Fachada estática que o app_wallet.py (desktop) usa.
    Internamente delega para a classe Wallet.
    """

    WALLETS_DIR = os.environ.get("BRN_WALLETS_DIR", "wallets")

    @staticmethod
    def _garantir_dir():
        os.makedirs(WalletManager.WALLETS_DIR, exist_ok=True)

    @staticmethod
    def generate_keypair() -> dict:
        """Gera nova carteira. Retorna dict com address, sk, pk."""
        w = Wallet()
        return {
            "address": w.address,
            "private_key": w.priv_hex,
            "public_key": w.pub_hex,
        }

    @staticmethod
    def validate_address(addr: str) -> bool:
        """Valida endereço brn1... via bech32."""
        try:
            from bech32 import validate_address as _v
            return _v(addr)
        except Exception:
            # fallback mínimo
            return isinstance(addr, str) and addr.startswith("brn1") and len(addr) > 20

    @staticmethod
    def save_encrypted_wallet(filename: str, password: str,
                              address: str, sk: str, pk: str) -> dict:
        """Salva carteira cifrada em disco."""
        try:
            WalletManager._garantir_dir()
            if not filename.endswith(".wallet"):
                filename += ".wallet"
            path = os.path.join(WalletManager.WALLETS_DIR, filename)

            w = Wallet(private_key_hex=sk)
            blob = w.export_encrypted(password)

            with open(path, "w") as f:
                f.write(blob)

            return {"ok": True, "msg": f"Carteira salva em {path}", "path": path}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    @staticmethod
    def load_encrypted_wallet(filename: str, password: str) -> dict:
        """Carrega carteira cifrada do disco."""
        try:
            if not filename.endswith(".wallet"):
                filename += ".wallet"
            path = os.path.join(WalletManager.WALLETS_DIR, filename)

            with open(path) as f:
                blob = f.read()

            w = Wallet.import_encrypted(blob, password)
            return {
                "ok": True,
                "address": w.address,
                "private_key": w.priv_hex,
                "public_key": w.pub_hex,
            }
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    @staticmethod
    def list_wallets() -> list:
        """Lista carteiras salvas (sem revelar chaves)."""
        try:
            WalletManager._garantir_dir()
            out = []
            for fname in sorted(os.listdir(WalletManager.WALLETS_DIR)):
                if not fname.endswith(".wallet"):
                    continue
                path = os.path.join(WalletManager.WALLETS_DIR, fname)
                out.append({
                    "filename": fname,
                    "size": os.path.getsize(path),
                    "modified": os.path.getmtime(path),
                })
            return out
        except Exception:
            return []


# ============================================================
# ✅ NOVO: assinatura de transações BRN (usado por server.py)
# ============================================================
def sign_transaction(wallet: Wallet, tx_core: dict) -> str:
    """
    Assina o hash de uma transação BRN.
    Usa signing_hash de blockchain.py para garantir compatibilidade.
    """
    from blockchain import signing_hash
    h = signing_hash(tx_core)
    return wallet.sign(h)


def verify_transaction(tx: dict) -> tuple[bool, str]:
    """Verifica assinaturas de todos os inputs de uma tx."""
    try:
        from blockchain import signing_hash
        h = signing_hash(tx)
        for inp in tx.get("inputs", []):
            sig = inp.get("signature", "")
            pk = inp.get("pubkey", "")
            if not sig or not pk:
                return False, "input sem assinatura ou pubkey"
            if not Wallet.verify(h, sig, pk):
                return False, "assinatura inválida"
        return True, "ok"
    except Exception as e:
        return False, str(e)
