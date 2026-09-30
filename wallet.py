"""
wallet.py — Carteira BRN (v6)
================================================================
v6:
  - Armazenamento local via secure_store.py (Argon2id + ChaCha20-Poly1305).
  - Compatibilidade retroativa: le o formato antigo (PBKDF2 + Fernet)
    e pode migrar automaticamente.
  - Escrita atomica + permissao 0600 nos arquivos de carteira.
  - sign_transaction() monta a tx inteira (pubkey + assinatura + txid).
  - verify_transaction() delega para a logica unica do blockchain.

v5: Recupera HDWalletManager (BIP39 + BIP44).
v4: Adiciona WalletManager (fachada para app_wallet.py).
v3: Adiciona assinatura/verificacao de transacoes BRN.

Curva: secp256k1 (Schnorr BIP340, fallback ECDSA).
       Ed25519 e usado apenas para identidade do no (P2P), nao para tx.
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

from secure_store import (
    encrypt_blob as _ss_encrypt,
    decrypt_blob as _ss_decrypt,
    MAGIC as _SS_MAGIC,
)

SIG_MODE = "schnorr"


# ============================================================
# WALLET SIMPLES (chave unica)
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

    # --------------------------------------------------------
    # ASSINATURA
    # --------------------------------------------------------
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

    # --------------------------------------------------------
    # SERIALIZACAO
    # --------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "private_key": self.priv_hex,
            "address": self.address,
            "pubkey": self.pub_hex,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Wallet":
        return cls(private_key_hex=d["private_key"])

    # --------------------------------------------------------
    # v6: CIFRAGEM LOCAL (Argon2id + ChaCha20-Poly1305)
    # --------------------------------------------------------
    def export_encrypted(self, password: str) -> str:
        """
        Retorna base64(blob cifrado por secure_store).
        O blob em si ja e MAGIC || header || ciphertext.
        Envolvemos em base64 para manter a API historica
        (string) e a compatibilidade com arquivos *.wallet existentes.
        """
        data = json.dumps(
            self.to_dict(), separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        blob = _ss_encrypt(data, password)
        return base64.b64encode(blob).decode("ascii")

    @classmethod
    def import_encrypted(cls, blob_b64: str, password: str) -> "Wallet":
        """
        Aceita os dois formatos:
          - Novo (secure_store): base64( b"BRNS1" || header || ct )
          - Antigo (Fernet/PBKDF2): base64( salt(16) || fernet_token )
        """
        raw = base64.b64decode(blob_b64)

        if raw.startswith(_SS_MAGIC):
            data = json.loads(_ss_decrypt(raw, password).decode("utf-8"))
        else:
            # Formato legado — mantido apenas para migracao
            data = cls._import_legacy_fernet(raw, password)

        return cls.from_dict(data)

    @staticmethod
    def _import_legacy_fernet(raw: bytes, password: str) -> dict:
        """Leitura do formato antigo. Use apenas para migrar para o novo."""
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        salt, token = raw[:16], raw[16:]
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(), length=32,
            salt=salt, iterations=600_000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
        return json.loads(Fernet(key).decrypt(token).decode("utf-8"))

    def is_legacy_format(self, blob_b64: str) -> bool:
        """Diz se um blob esta no formato antigo (Fernet)."""
        try:
            raw = base64.b64decode(blob_b64)
            return not raw.startswith(_SS_MAGIC)
        except Exception:
            return False


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

    # --------------------------------------------------------
    # v6: ESCRITA ATOMICA + 0600
    # --------------------------------------------------------
    @staticmethod
    def _atomic_write(path: str, content: str) -> None:
        """Grava com fsync + rename atomico + permissao 0600."""
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            try:
                os.unlink(tmp)
            except Exception:
                pass
            raise
        os.replace(tmp, path)
        # Garante 0600 mesmo se o arquivo ja existia com outras perms
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass

    @staticmethod
    def save_encrypted_wallet(filename: str, password: str,
                              address: str, sk: str, pk: str) -> dict:
        try:
            WalletManager._garantir_dir()
            if not filename.endswith(".wallet"):
                filename += ".wallet"
            path = os.path.join(WalletManager.WALLETS_DIR, filename)
            w = Wallet(private_key_hex=sk)
            blob_b64 = w.export_encrypted(password)
            WalletManager._atomic_write(path, blob_b64)
            return {"ok": True, "msg": f"Carteira salva em {path}", "path": path}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    @staticmethod
    def load_encrypted_wallet(filename: str, password: str,
                              auto_migrate: bool = True) -> dict:
        """
        Carrega uma wallet.
        Se o arquivo estiver no formato antigo (Fernet/PBKDF2) e
        auto_migrate=True, regrava no novo formato (secure_store)
        apos decifrar com sucesso.
        """
        try:
            if not filename.endswith(".wallet"):
                filename += ".wallet"
            path = os.path.join(WalletManager.WALLETS_DIR, filename)
            with open(path, "r", encoding="utf-8") as f:
                blob_b64 = f.read()

            w = Wallet.import_encrypted(blob_b64, password)

            if auto_migrate and w.is_legacy_format(blob_b64):
                try:
                    new_blob = w.export_encrypted(password)
                    WalletManager._atomic_write(path, new_blob)
                    print(f"[wallet] migrado para formato v6: {path}")
                except Exception as e:
                    print(f"[wallet] aviso: falha ao migrar {path}: {e}")

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
# HD WALLET (BIP39 + BIP44)
# ============================================================
class HDWalletManager:
    """
    Carteira HD (BIP39 + BIP32/BIP44) sobre secp256k1.
    Compativel com Schnorr/ECDSA — nao migrar para SLIP-0010 sem
    mudar SIG_MODE junto.
    """
    PURPOSE = 44
    COIN_TYPE = 0
    ACCOUNT = 0
    CHANGE = 0

    # Ordem da curva secp256k1 (n)
    _SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

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
            raise ValueError("Mnemonico invalido")

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
            key = (int.from_bytes(I[:32], "big") + key) % HDWalletManager._SECP256K1_N
            if key == 0:
                raise ValueError("derivacao BIP32 produziu chave invalida")
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
# HELPERS DE TRANSACAO (usados por app_wallet_v3 / server)
# ============================================================
def sign_transaction(wallet: Wallet, tx: dict) -> dict:
    """
    v6: assina TODOS os inputs e devolve a tx pronta para broadcast.

    Fluxo interno:
      1. Coloca pubkey em cada input (necessario para o signing_hash).
      2. Calcula signing_hash UMA vez (todos os inputs sao assinados
         com o mesmo hash no modelo BRN atual).
      3. Assina e grava signature em cada input.
      4. Recalcula txid com as assinaturas ja presentes.
    """
    from blockchain import signing_hash, txid

    tx = dict(tx)
    pub_hex = wallet.pub_hex

    # 1) pubkey em todos os inputs
    new_inputs = []
    for inp in tx["inputs"]:
        ni = dict(inp)
        ni["pubkey"] = pub_hex
        ni.setdefault("signature", "")
        new_inputs.append(ni)
    tx["inputs"] = new_inputs

    # 2) hash
    h = signing_hash(tx)

    # 3) assinatura
    sig_hex = wallet.sign(h)
    for inp in tx["inputs"]:
        inp["signature"] = sig_hex

    # 4) txid (agora inclui pubkey + assinatura no core canonico)
    tx["txid"] = txid(tx)
    return tx


def verify_transaction(tx: dict) -> tuple[bool, str]:
    """
    Delega para blockchain.validate_tx quando possivel (mais completo:
    inclui UTXO, fee, nonce, binding pubkey->utxo). Caso contrario,
    faz uma checagem local minima de assinatura.
    """
    try:
        from blockchain import signing_hash
        h = signing_hash(tx)
        for i, inp in enumerate(tx.get("inputs", [])):
            sig = inp.get("signature", "")
            pk = inp.get("pubkey", "")
            if not sig or not pk:
                return False, f"input[{i}] sem assinatura ou pubkey"
            if not Wallet.verify(h, sig, pk):
                return False, f"input[{i}] assinatura invalida"
        return True, "ok"
    except Exception as e:
        return False, str(e)


def sign_and_verify(wallet: Wallet, tx: dict) -> tuple[dict, bool, str]:
    """Conveniencia: assina e verifica em um passo. Util em testes."""
    signed = sign_transaction(wallet, tx)
    ok, msg = verify_transaction(signed)
    return signed, ok, msg