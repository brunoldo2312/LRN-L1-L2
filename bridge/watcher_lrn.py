"""
bridge/watcher_lrn.py — Observa a blockchain BRN e detecta depósitos
no endereço da bridge. Marca a intenção mais antiga como 'pendente'.
"""
import threading
import time

from .config import (
    ENDERECO_BRIDGE_LRN, MIN_DEPOSITO_BRN_SATS,
    BRN_POR_BTC, TAXA_BRIDGE_BPS, DEBUG,
)
from .db_bridge import (
    _conn,
    buscar_intencao_deposito_mais_antiga,
    registrar_deposito,
    atualizar_deposito,
)


def _ja_processado(txid):
    with _conn() as c:
        r = c.execute("SELECT 1 FROM depositos WHERE lrn_txid=?", (txid,)).fetchone()
        return r is not None


def processar_depositos_lrn():
    if not ENDERECO_BRIDGE_LRN:
        return

    from server import CHAIN
    utxos = CHAIN.db.utxos_for(ENDERECO_BRIDGE_LRN)
    if not utxos:
        return

    for u in utxos:
        txid = u["txid"]
        amount = u["amount"]

        if amount < MIN_DEPOSITO_BRN_SATS:
            continue
        if _ja_processado(txid):
            continue

        intencao = buscar_intencao_deposito_mais_antiga()
        if not intencao:
            if DEBUG:
                print(f"[Watcher] BRN recebido ({amount/1e8:.8f}) mas sem intenção registrada")
            continue

        endereco_btc = intencao["endereco_btc"]
        endereco_brn = intencao.get("endereco_brn") or ""

        btc_bruto = int(amount / BRN_POR_BTC)
        taxa = btc_bruto * TAXA_BRIDGE_BPS // 10000
        btc_sats = btc_bruto - taxa

        if btc_sats <= 0:
            continue

        ok = registrar_deposito(txid, endereco_brn, endereco_btc, amount, btc_sats)
        if ok:
            atualizar_deposito(txid, status="pendente")
            print(f"[Watcher] Depósito: {amount/1e8:.8f} BRN → {btc_sats} sats BTC")
            print(f"[Watcher]   destino: {endereco_btc[:20]}...")


def loop_watcher(intervalo=30):
    if not ENDERECO_BRIDGE_LRN:
        print("[Watcher] ENDERECO_BRIDGE_LRN não configurado — desativado")
        return
    print(f"[Watcher] Iniciado (a cada {intervalo}s) @ {ENDERECO_BRIDGE_LRN[:20]}...")
    while True:
        try:
            processar_depositos_lrn()
        except Exception as e:
            print(f"[Watcher] Erro: {e}")
        time.sleep(intervalo)


def iniciar_watcher(intervalo=30):
    t = threading.Thread(target=loop_watcher, args=(intervalo,), daemon=True, name="WatcherLRN")
    t.start()
    return t
  
