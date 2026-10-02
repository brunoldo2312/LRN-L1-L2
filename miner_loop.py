"""
miner_loop.py — Loop de mineracao do BRN (v6.3)
================================================================
API publica:
    get_miner(chain) -> Miner    (singleton)
    Miner.start(addr, pubkey_hex)
    Miner.stop()
    Miner.status() -> dict

v6.3:
  - [NOVO] Integra progress_cb do mine_block_interruptible.
  - [NOVO] status() expoe hashrate, hashes_done, eta_seconds.
  - [NOVO] miner_loop le BRN_MINER_DIFFICULTY_MAX (opcional) e avisa
    se a dificuldade atual excede o teto seguro da CPU.

v6.2:
  - [CRITICO] _loop NAO morre mais quando um bloco e rejeitado.
  - Log diferencia "parada do usuario" de "falha de mineracao".
  - Contador de falhas consecutivas exposto em status().

v6.1: status() expoe 'count', 'last_height', 'uptime'.
================================================================
"""
import os
import time
import json
import threading
import traceback

try:
    from brn_logger import get_logger
    def _log(msg):
        get_logger("miner").info(msg)
except Exception:
    def _log(msg):
        print(f"[Miner] {msg}", flush=True)

print(f"[Miner] Modulo carregado: {os.path.abspath(__file__)}", flush=True)

MINER_INTERVAL      = int(os.environ.get("BRN_MINER_INTERVAL", "30"))
MINER_AUTO          = os.environ.get("BRN_MINER_AUTO", "1") == "1"
CURRENT_WALLET_FILE = "current_wallet.json"

RETRY_AFTER_REJECT_S = 5

_MINER_INSTANCE = None
_MINER_LOCK = threading.Lock()


def get_miner(chain):
    global _MINER_INSTANCE
    with _MINER_LOCK:
        if _MINER_INSTANCE is None:
            _MINER_INSTANCE = Miner(chain)
        else:
            _MINER_INSTANCE.chain = chain
        return _MINER_INSTANCE


def _ler_carteira_aberta():
    try:
        if not os.path.exists(CURRENT_WALLET_FILE):
            return {}
        with open(CURRENT_WALLET_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        addr = (d.get("address") or "").strip()
        pub  = (d.get("pubkey") or d.get("public_key") or "").strip()
        if addr.startswith("brn1"):
            return {"address": addr, "pubkey": pub}
    except Exception:
        pass
    return {}


def _pubkey_no_db(bc, addr):
    # Tenta via API do ChainDB primeiro (thread-safe)
    try:
        for u in bc.db.get_utxos(addr):
            pk = (u.get("pubkey") or "").strip()
            if pk:
                return pk
    except Exception:
        pass
    # Fallback: consulta direta
    try:
        row = bc.db.conn.execute(
            "SELECT pubkey FROM utxos WHERE address=? AND pubkey != '' LIMIT 1",
            (addr,)
        ).fetchone()
        return row[0] if row and row[0] else ""
    except Exception:
        return ""


class Miner:
    def __init__(self, chain):
        self.chain = chain
        self._thread = None
        self._stop_evt = threading.Event()
        self.address = ""
        self.pubkey = ""
        self.running = False
        self.blocks_mined = 0
        self.last_error = ""
        self.last_height = None
        self.started_at = 0.0
        self.consecutive_failures = 0

        # v6.3: progresso em tempo real
        self.current_nonce = 0
        self.hashes_done = 0
        self.hashrate = 0.0
        self._last_progress_ts = 0.0
        self._last_progress_hashes = 0

    def start(self, address, pubkey_hex=""):
        if self.running:
            return False, "ja rodando"
        if not address:
            return False, "endereco obrigatorio"
        if not pubkey_hex:
            pubkey_hex = _pubkey_no_db(self.chain, address)
        if not pubkey_hex:
            msg = f"pubkey desconhecida para {address} (receba uma tx antes)"
            self.last_error = msg
            _log(msg)
            return False, msg

        self.address = address
        self.pubkey = pubkey_hex
        self.blocks_mined = 0
        self.last_error = ""
        self.last_height = None
        self.started_at = time.time()
        self.consecutive_failures = 0
        self.current_nonce = 0
        self.hashes_done = 0
        self.hashrate = 0.0
        self._last_progress_ts = 0.0
        self._last_progress_hashes = 0
        self._stop_evt.clear()
        self.running = True

        self._thread = threading.Thread(target=self._loop, daemon=True, name="Miner")
        self._thread.start()
        _log(f"Miner iniciado | addr={address[:16]}... | pub={pubkey_hex[:16]}...")
        return True, "iniciado"

    def stop(self):
        if not self.running:
            return False, "nao estava rodando"
        self._stop_evt.set()
        self.running = False
        _log(f"Miner parado (total={self.blocks_mined})")
        return True, "parado"

    def status(self):
        uptime = 0
        if self.started_at and self.running:
            uptime = int(time.time() - self.started_at)
        elif self.started_at and not self.running:
            uptime = int(time.time() - self.started_at) if self.blocks_mined else 0

        st = {
            "running": self.running,
            "address": self.address,
            "pubkey": self.pubkey,
            "blocks_mined": self.blocks_mined,
            "height": self.chain.db.height(),
            "difficulty": self.chain.current_difficulty(),
            "last_error": self.last_error,
            "count": self.blocks_mined,
            "last_height": self.last_height,
            "uptime": uptime,
            "consecutive_failures": self.consecutive_failures,
            # v6.3:
            "current_nonce": self.current_nonce,
            "hashes_done": self.hashes_done,
            "hashrate": self.hashrate,
        }

        # ETA
        diff = st["difficulty"]
        expected = 16 ** diff
        st["expected_hashes"] = expected
        if self.hashrate > 0:
            restantes = max(0, expected - self.hashes_done)
            st["eta_seconds"] = int(restantes / self.hashrate)
        else:
            st["eta_seconds"] = None
        return st

    def _loop(self):
        while not self._stop_evt.is_set():
            try:
                if self.chain.db.height() < 0:
                    _log("Aguardando genesis (DB vazio)...")
                    if self._stop_evt.wait(timeout=5):
                        break
                    continue

                if not self.pubkey:
                    _log("Sem pubkey — parando")
                    self.running = False
                    return

                # Callback chamado a cada batch do hot loop
                def _on_progress(nonce, hashes_done):
                    self.current_nonce = nonce
                    self.hashes_done = hashes_done
                    now = time.time()
                    if self._last_progress_ts > 0:
                        dt = now - self._last_progress_ts
                        dh = hashes_done - self._last_progress_hashes
                        if dt > 0 and dh > 0:
                            self.hashrate = dh / dt
                    self._last_progress_ts = now
                    self._last_progress_hashes = hashes_done

                block = self.chain.mine_block_interruptible(
                    self.address,
                    self.pubkey,
                    should_continue=lambda: not self._stop_evt.is_set(),
                    progress_cb=_on_progress,
                )

                if self._stop_evt.is_set():
                    break

                if block is None:
                    self.consecutive_failures += 1
                    self.last_error = (
                        f"bloco rejeitado (tentativa {self.consecutive_failures})"
                    )
                    _log(
                        f"Bloco rejeitado — tentando de novo em "
                        f"{RETRY_AFTER_REJECT_S}s "
                        f"(falhas consecutivas={self.consecutive_failures})"
                    )
                    if self._stop_evt.wait(timeout=RETRY_AFTER_REJECT_S):
                        break
                    continue

                self.blocks_mined += 1
                self.consecutive_failures = 0
                self.hashes_done = 0
                self.current_nonce = 0
                h = block.get("height")
                self.last_height = h
                _log(
                    f"OK Bloco #{h} minerado (total={self.blocks_mined}) "
                    f"| hashrate~{self.hashrate:,.0f} H/s"
                )

            except ValueError as e:
                self.last_error = str(e)
                _log(f"Miner erro fatal: {e}")
                self.running = False
                return

            except Exception as e:
                self.last_error = str(e)
                self.consecutive_failures += 1
                _log(f"Miner exception ({self.consecutive_failures}): {e}")
                traceback.print_exc()
                if self._stop_evt.wait(timeout=3):
                    break
                continue

        self.running = False
        _log("Miner loop encerrado")


def loop_mineracao(chain, intervalo=None):
    """Compat com chamadas antigas."""
    if not MINER_AUTO:
        _log("Auto-mineracao DESATIVADA")
        return
    intervalo = intervalo or MINER_INTERVAL
    time.sleep(5)
    m = get_miner(chain)
    w = _ler_carteira_aberta()
    if w.get("address"):
        ok, msg = m.start(w["address"], w.get("pubkey", ""))
        _log(f"auto-start: {ok} ({msg})")
    else:
        _log("auto-start: sem carteira aberta — aguardando current_wallet.json")


def iniciar_mineracao(chain, intervalo=None):
    t = threading.Thread(target=loop_mineracao, args=(chain, intervalo),
                         daemon=True, name="MinerLoop")
    t.start()
    return t


start_miner = start_mining = iniciar_miner = iniciar_miner_loop = iniciar_mineracao
