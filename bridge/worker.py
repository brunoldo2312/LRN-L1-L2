"""
bridge/worker.py — Processa a fila (saques E depósitos pendentes).
"""
import threading
import time

from .db_bridge import (
    saques_para_processar,
    atualizar_saque,
    total_enviado_ultimas_24h,
    listar_depositos_pendentes,
    atualizar_deposito,
)
from .btc_sender import enviar_btc, saldo_cofre_sats
from .config import RESERVA_MINIMA_SATS, LIMITE_DIARIO_SATS, DEBUG


def _pode_enviar(valor_sats):
    saldo = saldo_cofre_sats()
    if saldo < valor_sats + RESERVA_MINIMA_SATS:
        return False, f"cofre {saldo} sats < {valor_sats + RESERVA_MINIMA_SATS}"
    hoje = total_enviado_ultimas_24h()
    if hoje + valor_sats > LIMITE_DIARIO_SATS:
        return False, f"limite diário ({hoje + valor_sats} > {LIMITE_DIARIO_SATS})"
    return True, "ok"


def processar_saques():
    pendentes = saques_para_processar()
    for s in pendentes:
        sid = s["id"]
        valor = s["valor_btc_sats"]
        destino = s["endereco_btc"]
        ok, motivo = _pode_enviar(valor)
        if not ok:
            print(f"[Worker] Saque {sid} adiado: {motivo}")
            continue
        try:
            txid = enviar_btc(destino, valor)
            atualizar_saque(sid, status="pago", btc_txid=txid)
            print(f"[Worker] ✅ Saque {sid} pago: {txid}")
        except Exception as e:
            print(f"[Worker] ❌ Erro saque {sid}: {e}")
            atualizar_saque(sid, status="erro", btc_txid=f"ERRO: {e}")


def processar_depositos():
    pendentes = listar_depositos_pendentes()
    for d in pendentes:
        txid_lrn = d["lrn_txid"]
        btc_sats = d["valor_btc_sats"]
        destino = d["endereco_btc"]

        if btc_sats <= 0:
            atualizar_deposito(txid_lrn, status="erro")
            continue

        ok, motivo = _pode_enviar(btc_sats)
        if not ok:
            print(f"[Worker] Depósito {txid_lrn[:16]} adiado: {motivo}")
            continue

        try:
            txid_btc = enviar_btc(destino, btc_sats)
            atualizar_deposito(txid_lrn, status="pago", btc_txid=txid_btc)
            print(f"[Worker] ✅ Depósito {txid_lrn[:16]} pago: {txid_btc}")
        except Exception as e:
            print(f"[Worker] ❌ Erro depósito {txid_lrn[:16]}: {e}")
            atualizar_deposito(txid_lrn, status="erro", btc_txid=f"ERRO: {e}")


def processar_fila():
    processar_depositos()
    processar_saques()


def loop_worker(intervalo=60):
    print(f"[Worker] Iniciado (a cada {intervalo}s)")
    while True:
        try:
            processar_fila()
        except Exception as e:
            print(f"[Worker] Erro: {e}")
        time.sleep(intervalo)


def iniciar_worker(intervalo=60):
    t = threading.Thread(target=loop_worker, args=(intervalo,), daemon=True, name="BridgeWorker")
    t.start()
    return t
