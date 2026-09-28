"""
app_wallet_v3.py — Carteira desktop BRN (PyWebView) | v3.2
============================================================
Abre index_wallet.html (não o index.html do explorer).
"""
import os
import sys
import time
import logging
from pathlib import Path

import requests
import webview

from wallet import WalletManager

# ============================================================
# CONFIG
# ============================================================
API_PORT = int(os.environ.get("BRN_WEB_PORT", "5000"))
API_URL = os.environ.get("BRN_API_URL", f"http://127.0.0.1:{API_PORT}")
EXPLORER_PORT = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))
EXPLORER_URL = os.environ.get("BRN_EXPLORER_URL", f"http://127.0.0.1:{EXPLORER_PORT}")

WEB_USER = os.environ.get("BRN_WEB_USER", "admin")
WEB_PASS = os.environ.get("BRN_WEB_PASS", "")

TIMEOUT_LEITURA   = 8
TIMEOUT_TX        = 15
TIMEOUT_MINERACAO = 30

CACHE_TTL = 5

WALLET_HTML = "index_wallet.html"

if not WEB_PASS:
    print("ERRO: defina BRN_WEB_PASS antes de rodar.", file=sys.stderr)
    print("      Ex: set BRN_WEB_PASS=carteira123", file=sys.stderr)
    sys.exit(1)

AUTH = (WEB_USER, WEB_PASS)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("brn.wallet")


def _tratar_erro_http(r):
    if r.status_code == 400:
        try:
            data = r.json()
            return {"ok": False, "msg": data.get("msg") or data.get("error", "Requisicao invalida.")}
        except Exception:
            return {"ok": False, "msg": "Requisicao invalida (HTTP 400)."}
    if r.status_code == 401:
        return {"ok": False, "msg": "🔒 Senha incorreta (HTTP 401)."}
    if r.status_code == 403:
        return {"ok": False, "msg": "🚫 Acesso negado (HTTP 403)."}
    if r.status_code == 404:
        return {"ok": False, "msg": "❓ Endpoint nao encontrado (HTTP 404)."}
    if r.status_code == 429:
        return {"ok": False, "msg": "⏳ Muitas requisicoes."}
    if r.status_code >= 500:
        return {"ok": False, "msg": f"💥 Erro no servidor (HTTP {r.status_code})."}
    try:
        return r.json()
    except Exception:
        return {"ok": False, "msg": f"Resposta invalida (HTTP {r.status_code})."}


class WalletApi:
    """API exposta ao JavaScript via pywebview."""

    def __init__(self):
        self._cache_saldos = {}

    def _cache_get(self, key):
        if key in self._cache_saldos:
            ts, val = self._cache_saldos[key]
            if time.time() - ts < CACHE_TTL:
                return val
        return None

    def _cache_set(self, key, val):
        self._cache_saldos[key] = (time.time(), val)

    def _cache_invalidate(self, prefix=""):
        for k in list(self._cache_saldos.keys()):
            if k.startswith(prefix):
                del self._cache_saldos[k]

    # ---------- carteira ----------
    def generate_wallet(self):
        try:
            return WalletManager.generate_keypair()
        except Exception as e:
            log.exception("generate_wallet falhou")
            return {"erro": str(e)}

    def validate_address(self, addr):
        try:
            return WalletManager.validate_address(addr)
        except Exception:
            return False

    # ---------- leitura ----------
    def portfolio(self, addr):
        if not self.validate_address(addr):
            return {"erro": "Endereco invalido."}
        cached = self._cache_get(f"portfolio:{addr}")
        if cached:
            return cached
        try:
            r = requests.get(f"{API_URL}/api/portfolio/{addr}",
                             auth=AUTH, timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            data = r.json().get("portfolio", {})
            self._cache_set(f"portfolio:{addr}", data)
            return data
        except requests.exceptions.ConnectionError:
            return {"erro": f"🔌 No offline em {API_URL}."}
        except requests.exceptions.Timeout:
            return {"erro": "⏱️ Timeout."}
        except Exception as e:
            return {"erro": str(e)}

    def node_status(self):
        try:
            r = requests.get(f"{API_URL}/api/status", timeout=5)
            if r.status_code == 200:
                return {"ok": True, **r.json()}
            return _tratar_erro_http(r)
        except requests.exceptions.ConnectionError:
            return {"ok": False, "msg": f"🔌 No offline em {API_URL}."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ---------- explorador ----------
    def list_blocks(self, start=0, limit=15):
        try:
            r = requests.get(f"{EXPLORER_URL}/api/latest", timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            blocos = r.json() or []
            if not isinstance(blocos, list):
                return {"erro": "Resposta invalida do explorer."}
            return {"ok": True, "blocks": blocos[:max(1, min(int(limit), 100))]}
        except requests.exceptions.ConnectionError:
            return {"erro": f"🔌 Explorer offline em {EXPLORER_URL}."}
        except Exception as e:
            return {"erro": str(e)}

    def get_block(self, height):
        try:
            r = requests.get(f"{EXPLORER_URL}/api/block/{int(height)}",
                             timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return {"ok": True, "block": r.json()}
        except Exception as e:
            return {"erro": str(e)}

    def list_transactions(self, addr):
        if not self.validate_address(addr):
            return {"erro": "Endereco invalido."}
        try:
            r = requests.get(f"{API_URL}/api/transacoes/{addr}",
                             auth=AUTH, timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            data = r.json() or {}
            return {"ok": True,
                    "count": data.get("count", 0),
                    "transactions": data.get("transactions", [])}
        except Exception as e:
            return {"erro": str(e)}

    # ---------- transacoes ----------
    def transfer(self, sender, to, asset_id, amount, sk, pk):
        if not self.validate_address(sender):
            return {"ok": False, "msg": "Remetente invalido."}
        if not self.validate_address(to):
            return {"ok": False, "msg": "Destinatario invalido."}
        if sender == to:
            return {"ok": False, "msg": "Nao pode enviar para o mesmo endereco."}
        try:
            amount_f = float(amount)
        except (ValueError, TypeError):
            return {"ok": False, "msg": "Valor invalido."}
        if amount_f <= 0:
            return {"ok": False, "msg": "Valor deve ser positivo."}

        payload = {
            "type": "transfer", "asset_id": "BRN",
            "from": sender, "to": to, "amount": amount_f,
            "public_key": pk, "private_key": sk,
            "nonce": int(time.time() * 1000),
        }
        try:
            r = requests.post(f"{API_URL}/api/transfer",
                              auth=AUTH, json=payload, timeout=TIMEOUT_TX)
            self._cache_invalidate("portfolio:")
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return r.json()
        except requests.exceptions.ConnectionError:
            return {"ok": False, "msg": f"🔌 No offline."}
        except requests.exceptions.Timeout:
            return {"ok": False, "msg": "⏱️ Timeout."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def mine_block(self, addr):
        if not self.validate_address(addr):
            return {"ok": False, "msg": "Endereco invalido."}
        try:
            r = requests.post(f"{API_URL}/api/mine", auth=AUTH,
                              json={"validator_address": addr},
                              timeout=TIMEOUT_MINERACAO)
            if r.status_code == 200:
                resp = r.json()
                if resp.get("ok"):
                    self._cache_invalidate(f"portfolio:{addr}")
                return resp
            return _tratar_erro_http(r)
        except requests.exceptions.ConnectionError:
            return {"ok": False, "msg": f"🔌 No offline."}
        except requests.exceptions.Timeout:
            return {"ok": False, "msg": "⏱️ Timeout na mineracao."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def call_faucet(self, addr, sk, pk):
        if not self.validate_address(addr):
            return {"ok": False, "msg": "Endereco invalido."}
        try:
            r = requests.post(f"{API_URL}/api/faucet", auth=AUTH,
                              json={"address": addr, "private_key": sk, "public_key": pk},
                              timeout=TIMEOUT_TX)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            self._cache_invalidate(f"portfolio:{addr}")
            return r.json()
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ---------- persistencia local ----------
    def save_wallet(self, filename, password, address, sk, pk):
        try:
            return WalletManager.save_encrypted_wallet(filename, password, address, sk, pk)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def load_wallet(self, filename, password):
        try:
            return WalletManager.load_encrypted_wallet(filename, password)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def list_wallets(self):
        try:
            return WalletManager.list_wallets()
        except Exception:
            return []


# ============================================================
# MAIN
# ============================================================
def main():
    index_path = Path(__file__).parent / WALLET_HTML

    if not index_path.exists():
        print(f"ERRO: {index_path} nao encontrado.", file=sys.stderr)
        print(f"      Crie o arquivo {WALLET_HTML} na pasta.", file=sys.stderr)
        sys.exit(1)

    api = WalletApi()

    log.info("=" * 60)
    log.info("  🚀 BRN Wallet v3.2")
    log.info(f"  API_URL      : {API_URL}")
    log.info(f"  EXPLORER_URL : {EXPLORER_URL}")
    log.info(f"  HTML         : {index_path.name}")
    log.info("=" * 60)

    webview.create_window(
        "BRN RWA - Carteira Digital",
        url=index_path.resolve().as_uri(),
        js_api=api,
        width=1020,
        height=880,
        min_size=(820, 640),
        background_color="#0d1117",
    )

    try:
        webview.start(debug=False)
    except Exception as e:
        log.error(f"Falha ao iniciar webview: {e}")
        sys.exit(1)

    log.info("👋 Ate logo.")


if __name__ == "__main__":
    main()
