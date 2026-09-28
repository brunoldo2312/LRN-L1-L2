"""
app_wallet_v3.py — Carteira desktop BRN (PyWebView) | Versão 3
================================================================
✅ v3: Cache de saldo (evita múltiplas consultas em pouco tempo)
✅ v3: Timeout configurável por tipo de chamada
✅ v3: Erros mais descritivos em PT-BR
✅ v3: Log estruturado no console
✅ v3: Fechamento graceful
✅ v3: Import corrigido (WalletManager vive em wallet.py)
================================================================
"""

import os
import sys
import time
import logging
from pathlib import Path

import requests
import webview

# ✅ CORRIGIDO — WalletManager agora vive em wallet.py
from wallet import WalletManager

# ============================================================
# CONFIG
# ============================================================
API_PORT = int(os.environ.get("BRN_WEB_PORT", "5000"))
API_URL = os.environ.get("BRN_API_URL", f"http://127.0.0.1:{API_PORT}")
WEB_USER = os.environ.get("BRN_WEB_USER", "admin")
WEB_PASS = os.environ.get("BRN_WEB_PASS", "")

TIMEOUT_LEITURA = 8
TIMEOUT_TX = 15
TIMEOUT_MINERACAO = 30

CACHE_TTL = 5

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
    if r.status_code == 400:
        try:
            data = r.json()
            return {"ok": False, "msg": data.get("msg") or data.get("error", "Requisição inválida.")}
        except Exception:
            return {"ok": False, "msg": "Requisição inválida (HTTP 400)."}
    if r.status_code == 401:
        return {"ok": False, "msg": "🔒 Senha incorreta (HTTP 401)."}
    if r.status_code == 403:
        return {"ok": False, "msg": "🚫 Acesso negado (HTTP 403)."}
    if r.status_code == 404:
        return {"ok": False, "msg": "❓ Endpoint não encontrado (HTTP 404)."}
    if r.status_code == 429:
        return {"ok": False, "msg": "⏳ Muitas requisições. Aguarde alguns segundos."}
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

    # ---------- cache ----------
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
            return {"
