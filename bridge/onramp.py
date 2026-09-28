"""
bridge/onramp.py — BTC → BRN (usuário envia BTC, recebe BRN)
Blueprint Flask sob /api/bridge/onramp/*
"""
import os
import json
import time
import threading

import requests
from flask import Blueprint, request, jsonify

from .config import (
    BTC_RPC, ENDERECO_COFRE_BTC, ENDERECO_BRIDGE_LRN,
    BRN_POR_BTC, MIN_SAQUE_SATS, CONFIRMACOES_BTC,
)
from .btc_sender import saldo_cofre_sats

onramp_bp = Blueprint("onramp", __name__, url_prefix="/api/bridge/onramp")

STATE_FILE = os.environ.get("BRN_BRIDGE_ONRAMP_STATE", "bridge_onramp_state.json")
_state_lock = threading.Lock()
_state = None


def _load_state():
    global _state
    if _state is not None:
        return _state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                _state = json.load(f)
        except Exception:
            _state = {"processados": {}, "ultima_sync": None}
    else:
        _state = {"processados": {}, "ultima_sync": None}
    return _state


def _save_state():
    with _state_lock:
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(_state, f, indent=2)
        except Exception as e:
            print(f"[Onramp] erro salvando estado: {e}")


def _btc_tx(txid):
    try:
        r = requests.get(f"{BTC_RPC}/tx/{txid}", timeout=10)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def _btc_tx_status(txid):
    try:
        r = requests.get(f"{BTC_RPC}/tx/{txid}/status", timeout=10)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def _mine_block_with_outputs(blockchain, outputs):
    from blockchain import block_hash, meets_difficulty, compute_merkle_root, txid as calc_txid

    height = blockchain.db.height() + 1
    reward = blockchain.current_reward(height)
    total_out = sum(o["amount"] for o in outputs)
    if total_out > reward:
        fator = reward / total_out
        for o in outputs:
            o["amount"] = int(o["amount"] * fator)
        total_out = sum(o["amount"] for o in outputs)

    troco = reward - total_out
    if troco > 0 and ENDERECO_BRIDGE_LRN:
        outputs = list(outputs) + [{"address": ENDERECO_BRIDGE_LRN,
                                     "amount": troco, "pubkey": ""}]

    cb = {
        "txid": "",
        "inputs": [{"txid": "0"*64, "vout": 0xFFFFFFFF, "pubkey": "", "signature": ""}],
        "outputs": outputs,
        "timestamp": int(time.time()),
        "locktime": 0,
        "height": height,
        "nonce": 0,
    }
    cb["txid"] = calc_txid(cb)

    selected = blockchain.db.all_mempool(limit=499)
    txs = [cb] + selected
    merkle = compute_merkle_root([t["txid"] for t in txs])
    prev_hash = blockchain.db.tip_hash()
    diff = blockchain.current_difficulty()
    ts = int(time.time())
    nonce = 0
    while True:
        h = block_hash(prev_hash, merkle, ts, nonce, diff)
        if meets_difficulty(h, diff):
            break
        nonce += 1
        if nonce % 200000 == 0:
            ts = int(time.time())

    block = {
        "height": height, "hash": h, "prev_hash": prev_hash,
        "timestamp": ts, "nonce": nonce, "merkle": merkle,
        "difficulty": diff, "transactions": txs,
    }
    ok, msg = blockchain.accept_block(block)
    return block if ok else None


# ---------------- ROTAS ----------------
@onramp_bp.route("/status", methods=["GET"])
def status():
    s = _load_state()
    saldo = saldo_cofre_sats()
    return jsonify({
        "ok": True,
        "rede": "testnet" if "testnet" in BTC_RPC else "mainnet",
        "cofre_btc": ENDERECO_COFRE_BTC or "(nao configurado)",
        "saldo_btc_sat": saldo,
        "taxa": BRN_POR_BTC,
        "min_sat": MIN_SAQUE_SATS,
        "conf": CONFIRMACOES_BTC,
        "ultima_sync": s.get("ultima_sync"),
        "processados": len(s.get("processados", {})),
    })


@onramp_bp.route("/sync", methods=["POST"])
def sync():
    global _state
    _state = None
    s = _load_state()
    s["ultima_sync"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_state()
    return jsonify({"ok": True, "msg": "Sincronizado"})


@onramp_bp.route("/claim", methods=["POST"])
def claim():
    data = request.get_json(force=True) or {}
    txid = (data.get("btc_txid") or "").strip()
    brn_addr = (data.get("brn_address") or "").strip()

    if not txid or not brn_addr:
        return jsonify({"ok": False, "msg": "Parâmetros ausentes"}), 400

    from wallet import WalletManager
    if not WalletManager.validate_address(brn_addr):
        return jsonify({"ok": False, "msg": "Endereço BRN inválido"}), 400

    s = _load_state()
    if txid in s.get("processados", {}):
        return jsonify({"ok": False, "msg": "TX já processada"}), 400

    tx = _btc_tx(txid)
    if not tx:
        return jsonify({"ok": False, "msg": "TX não encontrada no Bitcoin"}), 400

    st = _btc_tx_status(txid)
    confs = st.get("confirmations", 0)
    if confs < CONFIRMACOES_BTC:
        return jsonify({"ok": False,
                        "msg": f"Aguardando confirmações ({confs}/{CONFIRMACOES_BTC})"}), 400

    recebido = 0
    for vout in tx.get("vout", []):
        if vout.get("scriptpubkey_address") == ENDERECO_COFRE_BTC:
            recebido += vout.get("value", 0)

    if recebido <= 0:
        return jsonify({"ok": False, "msg": "Nenhum BTC recebido pelo cofre nessa TX"}), 400
    if recebido < MIN_SAQUE_SATS:
        return jsonify({"ok": False, "msg": f"Mínimo {MIN_SAQUE_SATS} sats"}), 400

    brn_sat = int(recebido * BRN_POR_BTC)
    if brn_sat <= 0:
        return jsonify({"ok": False, "msg": "Conversão resultou em zero"}), 400

    from server import CHAIN
    block = _mine_block_with_outputs(CHAIN,
        [{"address": brn_addr, "amount": brn_sat, "pubkey": ""}])
    if not block:
        return jsonify({"ok": False, "msg": "Falha ao creditar BRN"}), 500

    s.setdefault("processados", {})[txid] = {
        "brn_address": brn_addr,
        "btc_sat": recebido,
        "brn_sat": brn_sat,
        "bloco_brn": block["height"],
        "ts": int(time.time()),
    }
    s["ultima_sync"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_state()

    return jsonify({
        "ok": True,
        "msg": f"{recebido/1e8:.8f} BTC → {brn_sat/1e8:.8f} BRN",
        "brn_recebido": brn_sat / 1e8,
        "bloco": block["height"],
        "btc_sat": recebido,
        "btc_txid": txid,
    })


@onramp_bp.route("/historico", methods=["GET"])
def historico():
    s = _load_state()
    itens = []
    for txid, info in s.get("processados", {}).items():
        itens.append({
            "btc_txid": txid,
            "brn_recebido": info.get("brn_sat", 0) / 1e8,
            "bloco_brn": info.get("bloco_brn"),
            "brn_address": info.get("brn_address", ""),
            "btc_sat": info.get("btc_sat", 0),
            "status": "ok",
            "ts": info.get("ts"),
        })
    itens.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return jsonify({"itens": itens[:50]})
