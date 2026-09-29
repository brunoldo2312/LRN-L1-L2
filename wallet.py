"""
wallet.py — Carteira BRN com Schnorr (padrão) e ECDSA (fallback)
================================================================
✅ v5: Recupera HDWalletManager (BIP39 + BIP44)
✅ v4: Adiciona WalletManager (fachada para app_wallet.py)
✅ v3: Adiciona assinatura/verificação de transações BRN
================================================================
"""
import json
import base64
import os
import hashlib
import hmac as hmac_lib
from crypto import (
    sha256, pubkey_from_priv, sign_schnorr, verify_schnorr,
    sign_ecdsa, verify_ecdsa, generate_private_key,
)
from bech32 import address_from_pubkey

SIG_MODE = "schnorr"


# ============================================================
# WALLET SIMPLES (chave única)
# ============================================================
class Wallet:
    def __init__(self, private_key_hex: str | None = None):
        if private_key_hex:
            self.priv = bytes.fromhex(private_key_hex)
        else:
            self.priv = generate_private_key()
        self.pub = pubkey_from_priv(self.priv)
        self.address = address_from_pubkey(self.pub)

    @property
    def priv_hex(self) -> str:
        return self.priv.hex()

    @property
    def pub_hex(self) -> str:
        return self.pub.hex()

    def private_key_hex(self) -> str:
        return self.priv_hex

    def public_key_hex(self) -> str:
        return self.pub_hex

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

    def to_dict(self) -> dict:
        return {
            "private_key": self.priv_hex,
            "address": self.address,
            "pubkey": self.pub_hex,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Wallet":
        return cls(private_key_hex=d["private_key"])

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
# WALLET MANAGER (fachada para app_wallet.py)
# ============================================================
class WalletManager:
    WALLETS_DIR = os.environ.get("BRN_WALLETS_DIR", "wallets")

    @staticmethod
    def _garantir_dir():
        os.makedirs(WalletManager.WALLETS_DIR, exist_ok=True)

    @staticmethod
    def generate_keypair() -> dict:
        w = Wallet()
        return {
            "address": w.address,
            "private_key": w.priv_hex,
            "public_key": w.pub_hex,
        }

    @staticmethod
    def validate_address(addr: str) -> bool:
        try:
            from bech32 import validate_address as _v
            return _v(addr)
        except Exception:
            return isinstance(addr, str) and addr.startswith("brn1") and len(addr) > 20

    @staticmethod
    def save_encrypted_wallet(filename: str, password: str,
                              address: str, sk: str, pk: str) -> dict:
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
# HD WALLET (BIP39 + BIP44) — restaurado
# ============================================================
class HDWalletManager:
    """
    Carteira HD (BIP39 + BIP44) compatível com server.py e app_wallet_v3.py.
    """
    PURPOSE = 44
    COIN_TYPE = 0
    ACCOUNT = 0
    CHANGE = 0

    @staticmethod
    def _m():
        try:
            from mnemonic import Mnemonic
            return Mnemonic("english")
        except ImportError:
            raise ImportError("Instale: python -m pip install mnemonic")

    @staticmethod
    def create(strength=128):
        m = HDWalletManager._m()
        mnemonic_phrase = m.generate(strength=strength)
        return HDWalletManager.from_mnemonic(mnemonic_phrase, index=0)

    @staticmethod
    def from_mnemonic(mnemonic_phrase, index=0, passphrase=""):
        m = HDWalletManager._m()
        if not m.check(mnemonic_phrase):
            raise ValueError("Mnemônico inválido")

        seed = m.to_seed(mnemonic_phrase, passphrase=passphrase)
        I = hmac_lib.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
        master_key = int.from_bytes(I[:32], "big")
        master_chain = I[32:]

        path = [
            HDWalletManager.PURPOSE + 0x80000000,
            HDWalletManager.COIN_TYPE + 0x80000000,
            HDWalletManager.ACCOUNT + 0x80000000,
            HDWalletManager.CHANGE,
            index,
        ]

        key = master_key
        chain = master_chain
        for child in path:
            if child >= 0x80000000:
                data = b"\x00" + key.to_bytes(32, "big") + child.to_bytes(4, "big")
            else:
                pub = pubkey_from_priv(key.to_bytes(32, "big"))
                data = pub + child.to_bytes(4, "big")
            I = hmac_lib.new(chain, data, hashlib.sha512).digest()
            key = (int.from_bytes(I[:32], "big") + key) % (
                0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
            )
            chain = I[32:]

        w = Wallet(private_key_hex=key.to_bytes(32, "big").hex())
        return {
            "mnemonic": mnemonic_phrase,
            "address": w.address,
            "private_key": w.priv_hex,
            "public_key": w.pub_hex,
            "index": index,
            "path": f"m/44'/{HDWalletManager.COIN_TYPE}'/0'/0/{index}",
        }

    @staticmethod
    def derive_many(mnemonic_phrase, count=5):
        if count < 1 or count > 100:
            raise ValueError("count deve estar entre 1 e 100")
        return [
            HDWalletManager.from_mnemonic(mnemonic_phrase, index=i)
            for i in range(count)
        ]

    @staticmethod
    def validate_mnemonic(mnemonic_phrase):
        try:
            m = HDWalletManager._m()
            return m.check(mnemonic_phrase)
        except Exception:
            return False


# ============================================================
# HELPERS
# ============================================================
def sign_transaction(wallet: Wallet, tx_core: dict) -> str:
    from blockchain import signing_hash
    h = signing_hash(tx_core)
    return wallet.sign(h)


def verify_transaction(tx: dict) -> tuple[bool, str]:
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
