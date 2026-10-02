"""
app_wallet_v3.py — Carteira desktop BRN (PyWebView) | v3.4.3
============================================================
v3.4.3:
  - [CRÍTICO] generate_wallet() agora PERSISTE em current_wallet.json.
    Antes, carteiras novas só existiam na memória do pywebview e o
    backend não conseguia achar a pubkey ao iniciar mineração.
  - load_wallet() também persiste em current_wallet.json.
  - start_mining() envia 'miner_pubkey' E 'pubkey' + 'private_key'.
  - _pubkey_no_db() usa get_utxos() (thread-safe) antes de conn.execute.

v3.4.2:
  - start_mining() manda 'miner_pubkey' e 'private_key' no payload.
  - mine_block() troca 'validator_pubkey' por 'miner_pubkey'.
  - call_faucet() envia 'miner_pubkey'/'pubkey' além de 'public_key'.

v3.4.1:
  - timeouts aumentados (leitura=30, tx=120, mina=120).
"""
import os
import sys
import time
import json
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

TIMEOUT_LEITURA   = 30
TIMEOUT_TX        = 120
TIMEOUT_MINERACAO = 120

CACHE_TTL = 5
WALLET_HTML = "index_wallet.html"
CURRENT_WALLET_FILE = "current_wallet.json"

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
            return {"ok": False,
                    "msg": data.get("msg") or data.get("error", "Requisicao invalida.")}
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
        try:
            data = r.json()
            real = data.get("msg") or data.get("error")
            if real:
                return {"ok": False, "msg": f"💥 {real}"}
        except Exception:
            pass
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

    # ============================================================
    # CARTEIRA LOCAL
    # ============================================================
    def _persist_wallet(self, address, public_key, private_key=""):
        """
        v3.4.3: grava current_wallet.json para o backend conseguir
        recuperar a pubkey mesmo sem payload explicito.
        """
        try:
            p = Path(__file__).parent / CURRENT_WALLET_FILE
            with open(p, "w", encoding="utf-8") as f:
                json.dump({
                    "address": address,
                    "public_key": public_key,
                    "private_key": private_key,
                }, f, indent=2)
            log.info(f"Carteira persistida em {p.name}: {address[:20]}...")
            return True
        except Exception as e:
            log.warning(f"Falha ao persistir carteira: {e}")
            return False

    def generate_wallet(self):
        """v3.4.3: persiste imediatamente para o backend encontrar a pubkey."""
        try:
            w = WalletManager.generate_keypair()
            # Persiste no disco
            self._persist_wallet(
                w["address"],
                w.get("public_key") or w.get("pubkey") or "",
                w.get("private_key", ""),
            )
            return w
        except Exception as e:
            log.exception("generate_wallet falhou")
            return {"erro": str(e)}

    def validate_address(self, addr):
        try:
            return WalletManager.validate_address(addr)
        except Exception:
            return False

    def get_active_wallet(self):
        try:
            p = Path(__file__).parent / CURRENT_WALLET_FILE
            if not p.exists():
                return {"ok": False, "msg": "Nenhuma carteira ativa"}
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            return {
                "ok": True,
                "address": data.get("address", ""),
                "public_key": data.get("public_key") or data.get("pubkey") or "",
                "private_key": data.get("private_key", ""),
            }
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ============================================================
    # LEITURA (nó)
    # ============================================================
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
            r = requests.get(f"{API_URL}/api/status", timeout=TIMEOUT_LEITURA)
            if r.status_code == 200:
                return {"ok": True, **r.json()}
            return _tratar_erro_http(r)
        except requests.exceptions.ConnectionError:
            return {"ok": False, "msg": f"🔌 No offline em {API_URL}."}
        except requests.exceptions.Timeout:
            return {"ok": False, "msg": "⏱️ Timeout ao consultar status."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def sync_info(self):
        try:
            r = requests.get(f"{API_URL}/api/sync-info",
                             auth=AUTH, timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return {"erro": f"HTTP {r.status_code}"}
            return r.json()
        except requests.exceptions.ConnectionError:
            return {"erro": f"🔌 No offline em {API_URL}."}
        except requests.exceptions.Timeout:
            return {"erro": "⏱️ Timeout no sync-info."}
        except Exception as e:
            return {"erro": str(e)}

    def chain_info(self):
        try:
            r = requests.get(f"{API_URL}/api/chain-info",
                             auth=AUTH, timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return {"ok": True, **r.json()}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ============================================================
    # EXPLORADOR
    # ============================================================
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
        except requests.exceptions.Timeout:
            return {"erro": "⏱️ Timeout no explorer."}
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

    def minhas_txs(self, addr):
        if not self.validate_address(addr):
            return {"ok": False, "msg": "Endereco invalido."}
        try:
            r = requests.get(f"{API_URL}/api/minhas-txs/{addr}",
                             auth=AUTH, timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return r.json()
        except requests.exceptions.ConnectionError:
            return {"ok": False, "msg": "🔌 No offline."}
        except requests.exceptions.Timeout:
            return {"ok": False, "msg": "⏱️ Timeout."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ============================================================
    # TRANSACOES
    # ============================================================
    def transfer(self, sender, to, asset_id, amount, sk, pk):
        if not self.validate_address(sender):
            return {"ok": False, "msg": "Remetente invalido."}
        if not self.validate_address(to):
            return {"ok": False, "msg": "Destinatario invalido."}
        if sender == to:
            return {"ok": False, "msg": "Nao pode enviar para o mesmo endereco."}
        if asset_id != "BRN":
            return {"ok": False, "msg": "Apenas BRN."}
        try:
            amount_f = float(amount)
        except (ValueError, TypeError):
            return {"ok": False, "msg": "Valor invalido."}
        if amount_f <= 0:
            return {"ok": False, "msg": "Valor deve ser positivo."}
        if amount_f < 0.00001:
            return {"ok": False, "msg": "Valor muito pequeno (minimo 0.00001)."}

        payload = {
            "type": "transfer", "asset_id": asset_id,
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

    def mine_block(self, addr, pk=""):
        """Minera UM bloco (sincrono). Preferir start_mining()."""
        if not self.validate_address(addr):
            return {"ok": False, "msg": "Endereco invalido."}
        try:
            payload = {"validator_address": addr}
            if pk:
                payload["miner_pubkey"] = pk
                payload["pubkey"] = pk
            r = requests.post(f"{API_URL}/api/mine", auth=AUTH,
                              json=payload, timeout=TIMEOUT_MINERACAO)
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
            payload = {
                "address": addr,
                "miner_pubkey": pk,
                "pubkey": pk,
                "public_key": pk,
                "private_key": sk,
            }
            r = requests.post(f"{API_URL}/api/faucet", auth=AUTH,
                              json=payload, timeout=TIMEOUT_TX)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            self._cache_invalidate(f"portfolio:{addr}")
            return r.json()
        except requests.exceptions.Timeout:
            return {"ok": False, "msg": "⏱️ Timeout no faucet."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ============================================================
    # MINER LOOP
    # ============================================================
    def start_mining(self, address, pubkey="", sk=""):
        """
        v3.4.3: se pubkey/sk vierem vazios do JS, tenta ler do
        current_wallet.json (que foi persistido por generate_wallet
        ou load_wallet).
        """
        if not self.validate_address(address):
            return {"ok": False, "msg": "Endereco invalido."}

        # Fallback: ler do disco
        if not pubkey or not sk:
            try:
                p = Path(__file__).parent / CURRENT_WALLET_FILE
                if p.exists():
                    with open(p, encoding="utf-8") as f:
                        d = json.load(f)
                    if d.get("address") == address:
                        if not pubkey:
                            pubkey = d.get("public_key") or d.get("pubkey") or ""
                        if not sk:
                            sk = d.get("private_key", "")
            except Exception:
                pass

        try:
            payload = {"validator_address": address}
            if pubkey:
                payload["miner_pubkey"] = pubkey
                payload["pubkey"] = pubkey
            if sk:
                payload["private_key"] = sk
            r = requests.post(f"{API_URL}/api/miner/start",
                              auth=AUTH, json=payload, timeout=TIMEOUT_MINERACAO)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return r.json()
        except requests.exceptions.ConnectionError:
            return {"ok": False, "msg": f"🔌 No offline em {API_URL}."}
        except requests.exceptions.Timeout:
            return {"ok": False, "msg": "⏱️ Timeout ao iniciar mineracao."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def stop_mining(self):
        try:
            r = requests.post(f"{API_URL}/api/miner/stop",
                              auth=AUTH, timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return r.json()
        except requests.exceptions.ConnectionError:
            return {"ok": False, "msg": f"🔌 No offline em {API_URL}."}
        except requests.exceptions.Timeout:
            return {"ok": False, "msg": "⏱️ Timeout ao parar mineracao."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def mining_status(self):
        try:
            r = requests.get(f"{API_URL}/api/miner/status",
                             auth=AUTH, timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return {"ok": False, "running": False, "msg": f"HTTP {r.status_code}"}
            return r.json()
        except requests.exceptions.ConnectionError:
            return {"ok": False, "running": False, "msg": "no offline"}
        except requests.exceptions.Timeout:
            return {"ok": False, "running": False, "msg": "timeout"}
        except Exception as e:
            return {"ok": False, "running": False, "msg": str(e)}

    # ============================================================
    # VERIFICACAO DE TX
    # ============================================================
    def tx_status(self, txid_str):
        try:
            r = requests.get(f"{API_URL}/api/tx-status/{txid_str}",
                             auth=AUTH, timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return r.json()
        except requests.exceptions.ConnectionError:
            return {"ok": False, "msg": "🔌 No offline."}
        except requests.exceptions.Timeout:
            return {"ok": False, "msg": "⏱️ Timeout."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def verificar_recebimento(self, addr, txid_str):
        if not self.validate_address(addr):
            return {"ok": False, "msg": "Endereco invalido."}
        try:
            r = requests.get(
                f"{API_URL}/api/verificar-recebimento/{addr}/{txid_str}",
                auth=AUTH, timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return r.json()
        except requests.exceptions.ConnectionError:
            return {"ok": False, "msg": "🔌 No offline."}
        except requests.exceptions.Timeout:
            return {"ok": False, "msg": "⏱️ Timeout."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ============================================================
    # BRIDGE (stub)
    # ============================================================
    def bridge_claim(self, txid, addr):
        return {"ok": False, "msg": "Bridge nao disponivel nesta versao"}

    # ============================================================
    # PERSISTENCIA LOCAL
    # ============================================================
    def save_wallet(self, filename, password, address, sk, pk):
        try:
            return WalletManager.save_encrypted_wallet(filename, password, address, sk, pk)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def load_wallet(self, filename, password):
        """v3.4.3: também persiste em current_wallet.json."""
        try:
            r = WalletManager.load_encrypted_wallet(filename, password)
            if r.get("ok"):
                self._persist_wallet(
                    r.get("address", ""),
                    r.get("public_key", ""),
                    r.get("private_key", ""),
                )
            return r
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
        sys.exit(1)

    api = WalletApi()

    log.info("=" * 60)
    log.info("  🚀 BRN Wallet v3.4.3")
    log.info(f"  API_URL      : {API_URL}")
    log.info(f"  EXPLORER_URL : {EXPLORER_URL}")
    log.info(f"  HTML         : {index_path.name}")
    log.info(f"  Timeouts     : leitura={TIMEOUT_LEITURA}s "
             f"tx={TIMEOUT_TX}s mina={TIMEOUT_MINERACAO}s")
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
