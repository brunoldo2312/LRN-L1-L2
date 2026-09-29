"""
miner_loop.py — Loop de mineracao automatica.
Mina SEMPRE para a carteira que estiver ABERTA no momento.
Fallback: endereco salvo em meta do banco.
"""
import os
import sys
import json
import time
import threading
import traceback

print(f"[Miner] Modulo carregado: {os.path.abspath(__file__)}", flush=True)

MINER_INTERVAL = int(os.environ.get("BRN_MINER_INTERVAL", "30"))
MINER_AUTO = os.environ.get("BRN_MINER_AUTO", "1") == "1"
MINER_ADDRESS_ENV = os.environ.get("BRN_MINER_ADDRESS", "").strip()

CURRENT_WALLET_FILE = "current_wallet.json"

_endereco_atual = ""


def _log(msg):
    try:
        from brn_logger import log as _l
        _l.info(msg)
        return
    except Exception:
        pass
    print(f"[Miner] {msg}", flush=True)


def _ler_carteira_aberta():
    """Le current_wallet.json — escrito pela carteira quando gera/carrega."""
    try:
        if not os.path.exists(CURRENT_WALLET_FILE):
            return ""
        with open(CURRENT_WALLET_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            addr = (d.get("address") or "").strip()
            if addr.startswith("brn1"):
                return addr
    except Exception:
        pass
    return ""


def _ler_endereco_salvo(blockchain):
    try:
        return blockchain.db.get_meta("miner_address") or ""
    except Exception:
        return ""


def _salvar_endereco(blockchain, addr):
    try:
        blockchain.db.set_meta("miner_address", addr)
    except Exception:
        pass


def _resolver_endereco(blockchain):
    """Prioridade: carteira aberta > env > meta > novo."""
    # 1) carteira aberta
    addr = _ler_carteira_aberta()
    if addr:
        return addr, "carteira_aberta"

    # 2) env var
    if MINER_ADDRESS_ENV:
        return MINER_ADDRESS_ENV, "env"

    # 3) endereco salvo no banco
    addr = _ler_endereco_salvo(blockchain)
    if addr:
        return addr, "salvo"

    # 4) gera novo
    try:
        from wallet import Wallet
        w = Wallet()
        _salvar_endereco(blockchain, w.address)
        _log(f"Endereco gerado: {w.address}")
        _log(f"Chave privada (GUARDE): {w.priv_hex}")
        return w.address, "novo"
    except Exception as e:
        _log(f"Falha ao gerar endereco: {e}")
        raise


def loop_mineracao(blockchain, intervalo=None):
    if intervalo is None:
        intervalo = MINER_INTERVAL

    if not MINER_AUTO:
        _log("Auto-mineracao DESATIVADA")
        return

    global _endereco_atual

    try:
        _endereco_atual, origem = _resolver_endereco(blockchain)
    except Exception as e:
        _log(f"Falha ao resolver endereco: {e}")
        return

    _log(f"Loop iniciado - a cada {intervalo}s")
    _log(f"Minerando para: {_endereco_atual} ({origem})")

    while True:
        try:
            # ⚡ re-checa a cada ciclo se a carteira aberta mudou
            novo_addr, origem = _resolver_endereco(blockchain)
            if novo_addr != _endereco_atual:
                _log(f"Endereco mudou: {_endereco_atual[:20]}... -> {novo_addr[:20]}... ({origem})")
                _endereco_atual = novo_addr

            time.sleep(intervalo)
            t0 = time.time()

            block = None
            try:
                block = blockchain.mine_block(_endereco_atual)
            except AttributeError:
                for nome in ("mine", "minerar", "mine_next", "mine_one"):
                    fn = getattr(blockchain, nome, None)
                    if callable(fn):
                        block = fn(_endereco_atual)
                        break
                if block is None:
                    raise RuntimeError("metodo de mineracao nao encontrado")

            if block:
                dt = time.time() - t0
                alt = block.get("height", "?") if isinstance(block, dict) else "?"
                txs = len(block.get("transactions", [])) if isinstance(block, dict) else 0
                _log(f"OK Bloco #{alt} minerado ({txs} txs, {dt:.1f}s) -> {_endereco_atual[:16]}...")

        except Exception as e:
            _log(f"Erro na mineracao: {e}")
            traceback.print_exc()
            time.sleep(5)


def iniciar_mineracao(blockchain, intervalo=None):
    t = threading.Thread(target=loop_mineracao, args=(blockchain, intervalo),
                         daemon=True, name="MinerLoop")
    t.start()
    return t


# aliases
start_miner = start_mining = iniciar_miner = iniciar_miner_loop = iniciar_mineracao
