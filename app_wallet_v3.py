"""
app_wallet_v3.py — Carteira desktop BRN (PyWebView)
============================================================
Substitui app_wallet.py com:
  ✅ Cache de saldo (evita múltiplas consultas em pouco tempo)
  ✅ Timeout configurável por tipo de chamada
  ✅ Erros mais descritivos em PT-BR
  ✅ Log estruturado no console
  ✅ Fechamento graceful
============================================================
"""

import os
import sys
import time
import logging
from pathlib import Path
from typing import Any

import requests
import webview

from cripto_wallet import WalletManager

# ============================================================
# CONFIG
# ============================================================
API_PORT = int(os.environ.get("BRN_WEB_PORT", "5000"))
API_URL = os.environ.get("BRN_API_URL", f"http://127.0.0.1:{API_PORT}")
WEB_USER = os.environ.get("BRN_WEB_USER", "admin")
WEB_PASS = os.environ.get("BRN_WEB_PASS", "")

# Timeouts por tipo de operação (segundos)
TIMEOUT_LEITURA = 8
TIMEOUT_TX = 15
TIMEOUT_MINERACAO = 30

# Cache de saldo (evita spam)
CACHE_TTL = 5  # segundos

if not WEB_PASS:
    print("ERRO: defina BRN_WEB_PASS antes de rodar.", file=sys.stderr)
    sys.exit(1)

AUTH = (WEB_USER, WEB_PASS)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("brn.wallet")


def _tratar_erro_http(r: requests.Response) -> dict:
    """Converte resposta HTTP ruim em dict amigável."""
    if r.status_code == 401:
        return {"ok": False, "msg": "🔒 Senha incorreta (HTTP 401)."}
    if r.status_code == 403:
        return {"ok": False, "msg": "🚫 Acesso negado (HTTP 403)."}
    if r.status_code == 404:
        return {"ok": False, "msg": "❓ Endpoint não encontrado (HTTP 404)."}
    if r.status_code >= 500:
        return {"ok": False, "msg": f"💥 Erro no servidor (HTTP {r.status_code})."}
    try:
        return r.json()
    except Exception:
        return {"ok": False, "msg": f"Resposta inválida (HTTP {r.status_code})."}


class WalletApi:
    """API exposta ao JavaScript via pywebview."""

    def __init__(self):
        self._cache_saldos: dict[str, tuple[float, dict]] = {}

    # ---------- utilidades ----------
    def _cache_get(self, key: str) -> dict | None:
        if key in self._cache_saldos:
            ts, val = self._cache_saldos[key]
            if time.time() - ts < CACHE_TTL:
                return val
        return None

    def _cache_set(self, key: str, val: dict):
        self._cache_saldos[key] = (time.time(), val)

    def _cache_invalidate(self, prefix: str = ""):
        for k in list(self._cache_saldos.keys()):
            if k.startswith(prefix):
                del self._cache_saldos[k]

    # ---------- carteira ----------
    def generate_wallet(self) -> dict:
        try:
            return WalletManager.generate_keypair()
        except Exception as e:
            log.exception("generate_wallet falhou")
            return {"erro": str(e)}

    def validate_address(self, addr: str) -> bool:
        try:
            return WalletManager.validate_address(addr)
        except Exception:
            return False

    # ---------- leitura ----------
    def portfolio(self, addr: str) -> dict:
        if not self.validate_address(addr):
            return {"erro": "Endereço inválido."}

        cached = self._cache_get(f"portfolio:{addr}")
        if cached:
            return cached

        try:
            r = requests.get(
                f"{API_URL}/api/portfolio/{addr}",
                auth=AUTH, timeout=TIMEOUT_LEITURA,
            )
            if r.status_code != 200:
                return _tratar_erro_http(r)
            data = r.json().get("portfolio", {})
            self._cache_set(f"portfolio:{addr}", data)
            return data
        except requests.exceptions.ConnectionError:
            return {"erro": f"🔌 Nó offline em {API_URL}."}
        except requests.exceptions.Timeout:
            return {"erro": "⏱️ Timeout ao consultar o nó."}
        except Exception as e:
            log.exception("portfolio falhou")
            return {"erro": str(e)}

    def node_status(self) -> dict:
        try:
            r = requests.get(f"{API_URL}/api/status", timeout=5)
            if r.status_code == 200:
                return {"ok": True, **r.json()}
            return _tratar_erro_http(r)
        except requests.exceptions.ConnectionError:
            return {"ok": False, "msg": f"🔌 Nó offline em {API_URL}."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ---------- transações ----------
    def transfer(self, sender: str, to: str, asset_id: str,
                 amount, sk: str, pk: str) -> dict:
        if not self.validate_address(sender):
            return {"ok": False, "msg": "Remetente inválido."}
        if not self.validate_address(to):
            return {"ok": False, "msg": "Destinatário inválido."}
        if asset_id == "KYC":
            return {"ok": False, "msg": "KYC só via API admin."}
        try:
            amount_f = float(amount)
        except (ValueError, TypeError):
            return {"ok": False, "msg": "Valor inválido."}
        if amount_f <= 0:
            return {"ok": False, "msg": "Valor deve ser positivo."}

        payload = {
            "type": "transfer",
            "asset_id": asset_id,
            "from": sender,
            "to": to,
            "amount": amount_f,
            "public_key": pk,
            "private_key": sk,
            "nonce": int(time.time() * 1000),
        }
        try:
            r = requests.post(
                f"{API_URL}/api/transfer",
                auth=AUTH, json=payload, timeout=TIMEOUT_TX,
            )
            # Invalida cache do remetente e destinatário
            self._cache_invalidate("portfolio:")
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return r.json()
        except requests.exceptions.ConnectionError:
            return {"ok": False, "msg": f"🔌 Nó offline em {API_URL}."}
        except requests.exceptions.Timeout:
            return {"ok": False, "msg": "⏱️ Timeout ao enviar transação."}
        except Exception as e:
            log.exception("transfer falhou")
            return {"ok": False, "msg": str(e)}

    def mine_block(self, addr: str) -> dict:
        if not self.validate_address(addr):
            return {"ok": False, "msg": "Endereço inválido."}
        try:
            r = requests.post(
                f"{API_URL}/api/mine",
                auth=AUTH,
                json={"validator_address": addr},
                timeout=TIMEOUT_MINERACAO,
            )
            if r.status_code == 200:
                return r.json()
            return _tratar_erro_http(r)
        except requests.exceptions.ConnectionError:
            return {"ok": False, "msg": f"🔌 Nó offline em {API_URL}."}
        except requests.exceptions.Timeout:
            return {"ok": False, "msg": "⏱️ Timeout na mineração."}
        except Exception as e:
            log.exception("mine_block falhou")
            return {"ok": False, "msg": str(e)}

    def call_faucet(self, addr: str, sk: str, pk: str) -> dict:
        if not self.validate_address(addr):
            return {"ok": False, "msg": "Endereço inválido."}
        try:
            r = requests.post(
                f"{API_URL}/api/faucet",
                auth=AUTH,
                json={"address": addr, "private_key": sk, "public_key": pk},
                timeout=TIMEOUT_TX,
            )
            if r.status_code != 200:
                return _tratar_erro_http(r)
            self._cache_invalidate(f"portfolio:{addr}")
            return r.json()
        except requests.exceptions.ConnectionError:
            return {"ok": False, "msg": f"🔌 Nó offline em {API_URL}."}
        except Exception as e:
            log.exception("call_faucet falhou")
            return {"ok": False, "msg": str(e)}

    # ---------- persistência local ----------
    def save_wallet(self, filename: str, password: str,
                    address: str, sk: str, pk: str) -> dict:
        return WalletManager.save_encrypted_wallet(
            filename, password, address, sk, pk
        )

    def load_wallet(self, filename: str, password: str) -> dict:
        return WalletManager.load_encrypted_wallet(filename, password)

    def list_wallets(self) -> list:
        return WalletManager.list_wallets()


# ============================================================
# MAIN
# ============================================================
def main():
    index_path = Path(__file__).parent / "index.html"
    if not index_path.exists():
        print(f"ERRO: {index_path} não encontrado.", file=sys.stderr)
        sys.exit(1)

    api = WalletApi()

    log.info(f"🚀 BRN Wallet v3 — API: {API_URL}")
    log.info(f"   index.html: {index_path}")

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
        webview.start(gui="gtk", debug=False)
    except Exception:
        webview.start(debug=False)


if __name__ == "__main__":
    main()
