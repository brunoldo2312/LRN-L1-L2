"""
miner_loop.py — Loop de mineracao do BRN (v6.2)
================================================================
API publica:
    get_miner(chain) -> Miner    (singleton)
    Miner.start(addr, pubkey_hex)
    Miner.stop()
    Miner.status() -> dict

v6.2:
  - [CRITICO] _loop NAO morre mais quando um bloco e rejeitado
    (accept_block retorna False). Antes, uma unica tx invalida na
    mempool derrubava o miner permanentemente. Agora ele espera 5s
    e tenta de novo.
  - Log diferencia "parada do usuario" de "falha de mineracao".
  - Contador de falhas consecutivas exposto em status().

v6.1: status() agora expoe 'count', 'last_height', 'uptime'
      para a carteira web exibir progresso.
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

# v6.2: pausa apos bloco rejeitado (evita busy-loop em tx ruim)
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
    """
    v6.2: prefere get_utxos (thread-safe) em vez de conn.execute cru.
    """
    # Tenta primeiro via API do ChainDB
    try:
        for u in bc.db.get_utxos(addr):
            pk = (u.get("pubkey") or "").strip()
            if pk:
                return pk
    except Exception:
        pass

    # Fallback: consulta direta (alguns ChainDB antigos nao tem get_utxos
    # com pubkey; nesse caso, conn.execute serve)
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
        # v6.1: campos de progresso
        self.last_height = None
        self.started_at = 0.0
        # v6.2: contador de falhas consecutivas
        self.consecutive_failures = 0

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

        return {
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
            # v6.2:
            "consecutive_failures": self.consecutive_failures,
        }

    def _loop(self):
        """
        v6.2: NAO morre por bloco rejeitado.
        - Se _stop_evt estiver setado -> sai (parada do usuario).
        - Se bloco veio None mas nao foi parada -> espera RETRY_AFTER_REJECT_S
          e tenta de novo, incrementando consecutive_failures.
        - Se a excecao for ValueError (ex: pubkey obrigatoria), aí sim para,
          porque nao adianta tentar de novo.
        """
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

                block = self.chain.mine_block_interruptible(
                    self.address,
                    self.pubkey,
                    should_continue=lambda: not self._stop_evt.is_set(),
                )

                # Caso 1: usuario pediu para parar
                if self._stop_evt.is_set():
                    break

                # Caso 2: bloco rejeitado por accept_block (ex: tx ruim
                # na mempool). NAO mata o miner.
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

                # Caso 3: sucesso
                self.blocks_mined += 1
                self.consecutive_failures = 0
                h = block.get("height")
                self.last_height = h
                _log(f"OK Bloco #{h} minerado (total={self.blocks_mined})")

            except ValueError as e:
                # Erro de programacao / pre-condicao (ex: pubkey ausente).
                # Aqui sim vale parar, porque nao adianta tentar de novo.
                self.last_error = str(e)
                _log(f"Miner erro fatal: {e}")
                self.running = False
                return

            except Exception as e:
                # Erro inesperado (ex: sqlite travado, disco cheio).
                # Registra, espera e tenta de novo.
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
