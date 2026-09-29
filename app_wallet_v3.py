"""
app_wallet_v3.py — Carteira desktop BRN (PyWebView) | Versão 3.5
================================================================
✅ v3.5: + start_mining / stop_mining / mining_status (loop contínuo)
✅ v3.4: + current_wallet.json (miner usa a carteira aberta)
✅ v3.3: + métodos de bridge
✅ v3.2: + métodos de explorador
================================================================
"""

import os
import sys
import json
import time
import logging
import threading
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

TIMEOUT_LEITURA = 8
TIMEOUT_TX = 15
TIMEOUT_MINERACAO = 30
TIMEOUT_BRIDGE = 45

CACHE_TTL = 5
WALLET_HTML = "index_wallet.html"
CURRENT_WALLET_FILE = "current_wallet.json"

MINING_INTERVAL = int(os.environ.get("BRN_WALLET_MINING_INTERVAL", "3"))  # segundos entre blocos

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
        return {"ok": False, "msg": "⏳ Muitas requisições. Aguarde."}
    if r.status_code >= 500:
        return {"ok": False, "msg": f"💥 Erro no servidor (HTTP {r.status_code})."}
    try:
        return r.json()
    except Exception:
        return {"ok": False, "msg": f"Resposta inválida (HTTP {r.status_code})."}


class WalletApi:
    """API exposta ao JavaScript via pywebview."""

    def __init__(self):
        self._cache_saldos: dict = {}
        self._wallet_path = Path(__file__).parent / CURRENT_WALLET_FILE

        # estado da mineração contínua
        self._mining_lock = threading.Lock()
        self._mining_thread: threading.Thread | None = None
        self._mining_event = threading.Event()   # set = rodando; clear = parar
        self._mining_addr = ""
        self._mining_count = 0
        self._mining_last_height = None
        self._mining_started_at = 0

    # ---------- cache ----------
    def _cache_get(self, key: str):
        if key in self._cache_saldos:
            ts, val = self._cache_saldos[key]
            if time.time() - ts < CACHE_TTL:
                return val
        return None

    def _cache_set(self, key: str, val):
        self._cache_saldos[key] = (time.time(), val)

    def _cache_invalidate(self, prefix: str = ""):
        for k in list(self._cache_saldos.keys()):
            if k.startswith(prefix):
                del self._cache_saldos[k]

    # ---------- carteira ativa ----------
    def _salvar_carteira_ativa(self, address: str,
                               private_key: str = "",
                               public_key: str = ""):
        try:
            with open(self._wallet_path, "w", encoding="utf-8") as f:
                json.dump({
                    "address": address,
                    "private_key": private_key,
                    "public_key": public_key,
                    "updated_at": int(time.time()),
                }, f, indent=2)
            log.info(f"current_wallet.json -> {address}")
        except Exception as e:
            log.warning(f"Falha ao salvar {CURRENT_WALLET_FILE}: {e}")

    def set_active_wallet(self, address: str,
                          private_key: str = "",
                          public_key: str = "") -> dict:
        if not self.validate_address(address):
            return {"ok": False, "msg": "Endereço inválido."}
        self._salvar_carteira_ativa(address, private_key, public_key)
        return {"ok": True, "address": address}

    def get_active_wallet(self) -> dict:
        try:
            if not self._wallet_path.exists():
                return {"ok": False, "msg": "Nenhuma carteira ativa."}
            with open(self._wallet_path, encoding="utf-8") as f:
                return {"ok": True, **json.load(f)}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ---------- MINERAÇÃO CONTÍNUA (toggle) ----------
    def _mining_loop(self):
        """Loop que minera continuamente até stop_mining()."""
        log.info(f"[Mining] Loop INICIADO -> {self._mining_addr}")
        try:
            while self._mining_event.is_set():
                addr = self._mining_addr
                try:
                    r = self.mine_block(addr)
                    if r.get("ok"):
                        with self._mining_lock:
                            self._mining_count += 1
                            self._mining_last_height = (r.get("block") or {}).get("height")
                        log.info(f"[Mining] Bloco #{self._mining_last_height} "
                                 f"({self._mining_count} no total)")
                    elif r.get("msg"):
                        log.warning(f"[Mining] {r.get('msg')}")
                except Exception as e:
                    log.exception(f"[Mining] erro no loop: {e}")

                # espera MINING_INTERVAL segundos ou até parar
                for _ in range(MINING_INTERVAL * 10):
                    if not self._mining_event.is_set():
                        return
                    time.sleep(0.1)
        finally:
            log.info(f"[Mining] Loop PARADO ({self._mining_count} blocos minerados)")

    def start_mining(self, address: str) -> dict:
        """Inicia mineração contínua para o endereço."""
        if not self.validate_address(address):
            return {"ok": False, "msg": "Endereço inválido."}

        with self._mining_lock:
            if self._mining_thread and self._mining_thread.is_alive():
                return {"ok": False, "msg": "Já está minerando."}

            self._mining_addr = address
            self._mining_count = 0
            self._mining_last_height = None
            self._mining_started_at = time.time()
            self._mining_event.set()

            self._mining_thread = threading.Thread(
                target=self._mining_loop,
                daemon=True,
                name="WalletMiner",
            )
            self._mining_thread.start()

        return {"ok": True, "msg": "Mineração iniciada."}

    def stop_mining(self) -> dict:
        """Para a mineração contínua."""
        self._mining_event.clear()
        return {
            "ok": True,
            "msg": "Mineração parada.",
            "count": self._mining_count,
        }

    def mining_status(self) -> dict:
        """Estado atual da mineração."""
        with self._mining_lock:
            running = (self._mining_event.is_set()
                       and self._mining_thread is not None
                       and self._mining_thread.is_alive())
            uptime = int(time.time() - self._mining_started_at) if running else 0
            return {
                "running": running,
                "address": self._mining_addr,
                "count": self._mining_count,
                "last_height": self._mining_last_height,
                "uptime": uptime,
            }

    # ---------- carteira ----------
    def generate_wallet(self) -> dict:
        try:
            w = WalletManager.generate_keypair()
            if w.get("address"):
                self._salvar_carteira_ativa(
                    w["address"], w.get("private_key", ""), w.get("public_key", ""))
            return w
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
            r = requests.get(f"{API_URL}/api/portfolio/{addr}",
                             auth=AUTH, timeout=TIMEOUT_LEITURA)
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

    def sync_info(self) -> dict:
        try:
            r = requests.get(f"{API_URL}/api/sync-info",
                             auth=AUTH, timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return r.json()
        except requests.exceptions.ConnectionError:
            return {"erro": f"🔌 Nó offline em {API_URL}."}
        except Exception as e:
            return {"erro": str(e)}

    # ---------- explorador ----------
    def list_blocks(self, start: int = 0, limit: int = 15) -> dict:
        try:
            r = requests.get(f"{EXPLORER_URL}/api/latest", timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            blocos = r.json() or []
            if not isinstance(blocos, list):
                return {"erro": "Resposta inválida do explorer."}
            blocos = blocos[: max(1, min(int(limit), 100))]
            return {"ok": True, "blocks": blocos}
        except requests.exceptions.ConnectionError:
            return {"erro": f"🔌 Explorer offline em {EXPLORER_URL}."}
        except Exception as e:
            log.exception("list_blocks falhou")
            return {"erro": str(e)}

    def get_block(self, height: int) -> dict:
        try:
            r = requests.get(f"{EXPLORER_URL}/api/block/{int(height)}",
                             timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return {"ok": True, "block": r.json()}
        except requests.exceptions.ConnectionError:
            return {"erro": f"🔌 Explorer offline em {EXPLORER_URL}."}
        except Exception as e:
            log.exception("get_block falhou")
            return {"erro": str(e)}

    def list_transactions(self, addr: str) -> dict:
        if not self.validate_address(addr):
            return {"erro": "Endereço inválido."}
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
            log.exception("list_transactions falhou")
            return {"erro": str(e)}

    def chain_info(self) -> dict:
        try:
            r = requests.get(f"{API_URL}/api/chain-info",
                             auth=AUTH, timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return {"ok": True, **r.json()}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ---------- transações ----------
    def transfer(self, sender: str, to: str, asset_id: str,
                 amount, sk: str, pk: str) -> dict:
        if not self.validate_address(sender):
            return {"ok": False, "msg": "Remetente inválido."}
        if not self.validate_address(to):
            return {"ok": False, "msg": "Destinatário inválido."}
        if sender == to:
            return {"ok": False, "msg": "Não pode enviar para o mesmo endereço."}
        if asset_id != "BRN":
            return {"ok": False, "msg": "Apenas BRN é suportado."}
        try:
            amount_f = float(amount)
        except (ValueError, TypeError):
            return {"ok": False, "msg": "Valor inválido."}
        if amount_f <= 0:
            return {"ok": False, "msg": "Valor deve ser positivo."}
        if amount_f < 0.00001:
            return {"ok": False, "msg": "Valor muito pequeno (mínimo 0.00001 BRN)."}

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
            r = requests.post(f"{API_URL}/api/mine",
                              auth=AUTH,
                              json={"validator_address": addr},
                              timeout=TIMEOUT_MINERACAO)
            if r.status_code == 200:
                resp = r.json()
                if resp.get("ok"):
                    self._cache_invalidate(f"portfolio:{addr}")
                return resp
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
            r = requests.post(f"{API_URL}/api/faucet",
                              auth=AUTH,
                              json={"address": addr,
                                    "private_key": sk,
                                    "public_key": pk},
                              timeout=TIMEOUT_TX)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            self._cache_invalidate(f"portfolio:{addr}")
            return r.json()
        except requests.exceptions.ConnectionError:
            return {"ok": False, "msg": f"🔌 Nó offline em {API_URL}."}
        except Exception as e:
            log.exception("call_faucet falhou")
            return {"ok": False, "msg": str(e)}

    # ---------- bridge ----------
    def bridge_status(self) -> dict:
        try:
            r = requests.get(f"{API_URL}/api/bridge/status",
                             auth=AUTH, timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return {"ok": True, **r.json()}
        except requests.exceptions.ConnectionError:
            return {"ok": False, "msg": "🔌 Bridge offline."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def bridge_sync(self) -> dict:
        try:
            r = requests.post(f"{API_URL}/api/bridge/sync",
                              auth=AUTH, timeout=TIMEOUT_BRIDGE)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return r.json()
        except requests.exceptions.ConnectionError:
            return {"ok": False, "msg": "🔌 Bridge offline."}
        except requests.exceptions.Timeout:
            return {"ok": False, "msg": "⏱️ Timeout na sync."}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    def bridge_claim(self, btc_txid: str, brn_address: str) -> dict:
        if not self.validate_address(brn_address):
            return {"ok": False, "msg": "Endereço BRN inválido."}
        try:
            r = requests.post(
                f"{API_URL}/api/bridge/claim",
                auth=AUTH,
                json={"btc_txid": btc_txid, "brn_address": brn_address},
                timeout=TIMEOUT_BRIDGE,
            )
            if r.status_code != 200:
                return _tratar_erro_http(r)
            self._cache_invalidate(f"portfolio:{brn_address}")
            return r.json()
        except requests.exceptions.ConnectionError:
            return {"ok": False, "msg": "🔌 Bridge offline."}
        except requests.exceptions.Timeout:
            return {"ok": False, "msg": "⏱️ Timeout na conversão."}
        except Exception as e:
            log.exception("bridge_claim falhou")
            return {"ok": False, "msg": str(e)}

    def bridge_historico(self) -> dict:
        try:
            r = requests.get(f"{API_URL}/api/bridge/historico",
                             auth=AUTH, timeout=TIMEOUT_LEITURA)
            if r.status_code != 200:
                return _tratar_erro_http(r)
            return r.json()
        except Exception as e:
            return {"itens": [], "erro": str(e)}

    # ---------- persistência ----------
    def save_wallet(self, filename: str, password: str,
                    address: str, sk: str, pk: str) -> dict:
        try:
            return WalletManager.save_encrypted_wallet(filename, password, address, sk, pk)
        except Exception as e:
            log.exception("save_wallet falhou")
            return {"ok": False, "msg": str(e)}

    def load_wallet(self, filename: str, password: str) -> dict:
        try:
            r = WalletManager.load_encrypted_wallet(filename, password)
            if r.get("ok") and r.get("address"):
                self._salvar_carteira_ativa(
                    r["address"], r.get("private_key", ""), r.get("public_key", ""))
            return r
        except Exception as e:
            log.exception("load_wallet falhou")
            return {"ok": False, "msg": str(e)}

    def list_wallets(self) -> list:
        try:
            return WalletManager.list_wallets()
        except Exception as e:
            log.exception("list_wallets falhou")
            return []


# ============================================================
# MAIN
# ============================================================
def main():
    index_path = Path(__file__).parent / WALLET_HTML

    if not index_path.exists():
        print(f"ERRO: {index_path} não encontrado.", file=sys.stderr)
        sys.exit(1)

    api = WalletApi()

    log.info("=" * 60)
    log.info("  🚀 BRN Wallet v3.5")
    log.info(f"  API_URL      : {API_URL}")
    log.info(f"  EXPLORER_URL : {EXPLORER_URL}")
    log.info(f"  HTML         : {index_path.name}")
    log.info(f"  Mining loop  : a cada {MINING_INTERVAL}s")
    log.info("=" * 60)

    webview.create_window(
        "BRN RWA - Carteira Digital",
        url=index_path.resolve().as_uri(),
        js_api=api,
        width=1020, height=880,
        min_size=(820, 640),
        background_color="#0d1117",
    )

    try:
        webview.start(gui="gtk", debug=False)
    except Exception as e:
        log.warning(f"GTK falhou ({e}), tentando backend padrão…")
        try:
            webview.start(debug=False)
        except Exception as e2:
            log.error(f"Falha ao iniciar webview: {e2}")
            sys.exit(1)

    log.info("👋 Até logo.")


if __name__ == "__main__":
    main()
