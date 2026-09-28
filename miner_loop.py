"""
miner_loop.py — Loop de mineração automática do BRN.
Roda em thread daemon, minera um bloco a cada N segundos.
"""
import os
import threading
import time

MINER_INTERVAL = int(os.environ.get("BRN_MINER_INTERVAL", "30"))
MINER_ADDRESS = os.environ.get("BRN_MINER_ADDRESS", "").strip()
MINER_AUTO = os.environ.get("BRN_MINER_AUTO", "1") == "1"


def _resolver_endereco(blockchain):
    """Se não tem endereço configurado, gera um e salva no meta."""
    global MINER_ADDRESS
    if MINER_ADDRESS:
        return MINER_ADDRESS

    salvo = blockchain.db.get_meta("miner_address")
    if salvo:
        MINER_ADDRESS = salvo
        print(f"[Miner] Usando endereco salvo: {salvo}")
        return salvo

    from wallet import Wallet
    w = Wallet()
    MINER_ADDRESS = w.address
    blockchain.db.set_meta("miner_address", w.address)
    blockchain.db.set_meta("miner_private_key", w.priv_hex)
    print(f"[Miner] Endereco gerado: {w.address}")
    print(f"[Miner] Chave privada (GUARDE): {w.priv_hex}")
    return w.address


def loop_mineracao(blockchain):
    """Loop principal — minera bloco a cada N segundos."""
    if not MINER_AUTO:
        print("[Miner] Auto-mineracao DESATIVADA (BRN_MINER_AUTO=0)")
        return

    endereco = _resolver_endereco(blockchain)
    print(f"[Miner] Loop iniciado — a cada {MINER_INTERVAL}s")
    print(f"[Miner] Recompensa vai para: {endereco}")

    while True:
        try:
            time.sleep(MINER_INTERVAL)
            h_antes = blockchain.db.height()
            t0 = time.time()

            block = blockchain.mine_block(endereco)

            if block:
                dt = time.time() - t0
                print(f"[Miner] ✅ Bloco #{block['height']} minerado "
                      f"({len(block['transactions'])} txs, {dt:.1f}s)")
            else:
                # Se não conseguiu, avisa mas continua
                if blockchain.db.height() == h_antes:
                    pass  # silencioso — talvez outro nó achou primeiro
        except Exception as e:
            print(f"[Miner] Erro: {e}")
            time.sleep(5)


def iniciar_mineracao(blockchain, intervalo=None):
    """Sobe o loop em thread daemon."""
    global MINER_INTERVAL
    if intervalo is not None:
        MINER_INTERVAL = intervalo

    t = threading.Thread(
        target=loop_mineracao,
        args=(blockchain,),
        daemon=True,
        name="MinerLoop",
    )
    t.start()
    return t
