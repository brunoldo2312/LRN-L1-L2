"""server.py — Backend HTTP do nó BRN (v7)
v7: + bridge bidirecional (BRN <-> BTC) via blueprints
"""
from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import time
import json
import threading
from functools import wraps

from wallet import Wallet, WalletManager, HDWalletManager
from blockchain import Blockchain, make_coinbase, txid as calc_txid, signing_hash

app = Flask(__name__)
CORS(app)

CHAIN = Blockchain("brn_v2_chain.db")
WALLETS_FILE = "user_wallets.json"

# Faucet
FAUCET_AMOUNT_BRN = 10
FAUCET_MAX_PER_ADDRESS = 3
FAUCET_COOLDOWN_S = 60 * 60
_faucet_history = {}

# Rate limit
RATE_LIMIT = 30
RATE_WINDOW_S = 60
_ip_history = {}
_rate_lock = threading.Lock()

# Cache de saldo
_cache_saldos = {}
_cache_lock = threading.Lock()
CACHE_TTL_S = 5


# ============================================================
# UTILITARIOS
# ============================================================
def _rate_limit(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        ip = request.remote_addr or "?"
        agora = time.time()
        with _rate_lock:
            hist = _ip_history.setdefault(ip, [])
            hist[:] = [t for t in hist if agora - t < RATE_WINDOW_S]
            if len(hist) >= RATE_LIMIT:
                return jsonify({"success": False,
                                "error": "Muitas requisicoes."}), 429
            hist.append(agora)
        return f(*args, **kwargs)
    return wrapper


def _saldo_cache_get(addr):
    with _cache_lock:
        if addr in _cache_saldos:
            ts, val = _cache_saldos[addr]
            if time.time() - ts < CACHE_TTL_S:
                return val
    return None


def _saldo_cache_set(addr, val):
    with _cache_lock:
        _cache_saldos[addr] = (time.time(), val)


def _saldo_cache_invalidate(addr):
    with _cache_lock:
        _cache_saldos.pop(addr, None)


def carregar_wallets():
    if not os.path.exists(WALLETS_FILE):
        return {}
    try:
        with open(WALLETS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def salvar_wallets(w):
    with open(WALLETS_FILE, "w") as f:
        json.dump(w, f, indent=2)


# ============================================================
# CARTEIRA
# ============================================================
@app.route("/api/nova-carteira", methods=["POST"])
@_rate_limit
def nova_carteira():
    try:
        w = Wallet()
        dados = {
            "success": True,
            "address": w.address,
            "private_key": w.priv_hex,
            "public_key": w.pub_hex,
            "warning": "Guarde a chave privada. NUNCA compartilhe."
        }
        wallets = carregar_wallets()
        wallets[w.address] = {"public_key": w.pub_hex}
        salvar_wallets(wallets)
        return jsonify(dados)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# SALDO / PORTFOLIO
# ============================================================
@app.route("/api/saldo/<address>", methods=["GET"])
def saldo(address):
    try:
        cached = _saldo_cache_get(address)
        if cached is None:
            utxos = CHAIN.db.get_utxos(address)
            cached = sum(u["amount"] for u in utxos)
            _saldo_cache_set(address, cached)
        return jsonify({
            "success": True, "address": address,
            "balance_sats": cached, "balance_brn": cached / 10**8,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/portfolio/<address>", methods=["GET"])
def portfolio(address):
    try:
        cached = _saldo_cache_get(address)
        if cached is None:
            utxos = CHAIN.db.get_utxos(address)
            cached = sum(u["amount"] for u in utxos)
            _saldo_cache_set(address, cached)
        return jsonify({"portfolio": {"BRN": cached / 10**8, "KYC": 0}})
    except Exception as e:
        return jsonify({"portfolio": {}, "error": str(e)}), 500


# ============================================================
# TRANSACOES
# ============================================================
@app.route("/api/transacoes/<address>", methods=["GET"])
def transacoes(address):
    try:
        txs = []
        for h in range(CHAIN.db.height() + 1):
            block = CHAIN.db.get_block(h)
            if not block:
                continue
            for tx in block["transactions"]:
                if any(o.get("address") == address for o in tx["outputs"]):
                    txs.append({
                        "txid": tx["txid"],
                        "block_height": h,
                        "timestamp": tx.get("timestamp", 0),
                        "outputs": tx["outputs"],
                    })
        return jsonify({"success": True, "address": address,
                        "count": len(txs), "transactions": txs[-50:]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/transfer", methods=["POST"])
@_rate_limit
def transfer():
    try:
        data = request.get_json(force=True) or {}
        sender = data.get("from", "").strip()
        to = data.get("to", "").strip()
        asset_id = data.get("asset_id", "BRN")
        amount = data.get("amount")
        sk = data.get("private_key", "")
        pk = data.get("public_key", "")

        if asset_id != "BRN":
            return jsonify({"ok": False, "msg": "So BRN via esta API."}), 400
        if not WalletManager.validate_address(sender):
            return jsonify({"ok": False, "msg": "Remetente invalido."}), 400
        if not WalletManager.validate_address(to):
            return jsonify({"ok": False, "msg": "Destinatario invalido."}), 400
        if sender == to:
            return jsonify({"ok": False, "msg": "Nao pode enviar para si."}), 400

        try:
            amount_sats = int(float(amount) * 10**8)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "msg": "Valor invalido."}), 400
        if amount_sats <= 0:
            return jsonify({"ok": False, "msg": "Valor deve ser > 0."}), 400

        try:
            w = Wallet(private_key_hex=sk)
        except Exception:
            return jsonify({"ok": False, "msg": "Chave privada invalida."}), 400
        if w.address != sender:
            return jsonify({"ok": False, "msg": "Chave privada nao corresponde."}), 400

        utxos = CHAIN.db.get_utxos(sender)
        utxos.sort(key=lambda u: u["amount"], reverse=True)
        total, escolhidos = 0, []
        for u in utxos:
            escolhidos.append(u)
            total += u["amount"]
            if total >= amount_sats + 1000:
                break
        if total < amount_sats:
            return jsonify({"ok": False, "msg": f"Saldo insuficiente ({total/1e8:.8f} BRN)."}), 400

        FEE = 1000
        troco = total - amount_sats - FEE
        outputs = [{"address": to, "amount": amount_sats, "pubkey": ""}]
        if troco > 0:
            outputs.append({"address": sender, "amount": troco, "pubkey": ""})

        inputs = [{"txid": u["txid"], "vout": u["vout"],
                   "pubkey": w.pub_hex, "signature": ""} for u in escolhidos]

        nonce = CHAIN.db.get_nonce_for_pubkey(w.pub_hex)
        tx = {"txid": "", "inputs": inputs, "outputs": outputs,
              "timestamp": int(time.time()), "locktime": 0, "nonce": nonce}

        h = signing_hash(tx)
        for inp in tx["inputs"]:
            inp["signature"] = w.sign(h)
        tx["txid"] = calc_txid(tx)

        ok, msg = CHAIN.submit_tx(tx)
        if not ok:
            return jsonify({"ok": False, "msg": msg}), 400

        _saldo_cache_invalidate(sender)
        _saldo_cache_invalidate(to)
        return jsonify({"ok": True, "txid": tx["txid"], "nonce": nonce,
                        "msg": "Transacao aceita na mempool."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# MINERACAO
# ============================================================
@app.route("/api/mine", methods=["POST"])
@_rate_limit
def mine():
    try:
        data = request.get_json(force=True) or {}
        miner = data.get("validator_address", "").strip()
        if not WalletManager.validate_address(miner):
            return jsonify({"ok": False, "msg": "Endereco invalido."}), 400
        block = CHAIN.mine_block(miner)
        if not block:
            return jsonify({"ok": False, "msg": "Falha ao minerar."}), 500
        _saldo_cache_invalidate(miner)
        return jsonify({"ok": True,
                        "msg": f"Bloco #{block['height']} minerado!",
                        "block": {"height": block["height"], "hash": block["hash"],
                                  "txs": len(block["transactions"]),
                                  "difficulty": block["difficulty"],
                                  "nonce": block["nonce"]}})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# FAUCET
# ============================================================
@app.route("/api/faucet", methods=["POST"])
@_rate_limit
def faucet():
    try:
        data = request.get_json(force=True) or {}
        addr = data.get("address", "").strip()
        if not WalletManager.validate_address(addr):
            return jsonify({"ok": False, "msg": "Endereco invalido."}), 400

        agora = time.time()
        hist = _faucet_history.setdefault(addr, [])
        hist[:] = [t for t in hist if agora - t < FAUCET_COOLDOWN_S]
        if len(hist) >= FAUCET_MAX_PER_ADDRESS:
            return jsonify({"ok": False, "msg": "Limite atingido."}), 429
        if hist and agora - hist[-1] < FAUCET_COOLDOWN_S:
            falta = int(FAUCET_COOLDOWN_S - (agora - hist[-1]))
            return jsonify({"ok": False, "msg": f"Aguarde {falta}s."}), 429

        block = CHAIN.mine_block(addr)
        if not block:
            return jsonify({"ok": False, "msg": "Falha."}), 500
        hist.append(agora)
        _saldo_cache_invalidate(addr)
        return jsonify({"ok": True,
                        "msg": f"Faucet enviado! +{FAUCET_AMOUNT_BRN} BRN",
                        "txid": block["transactions"][0]["txid"],
                        "amount": FAUCET_AMOUNT_BRN})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# INFO DA CADEIA
# ============================================================
@app.route("/api/chain-info", methods=["GET"])
def chain_info():
    return jsonify({
        "success": True, "name": "BrunoCoin", "ticker": "BRN",
        "height": CHAIN.db.height(), "tip_hash": CHAIN.db.tip_hash(),
        "reward": CHAIN.current_reward(CHAIN.db.height() + 1),
        "difficulty": CHAIN.current_difficulty(),
    })


@app.route("/api/status", methods=["GET"])
def status():
    return jsonify({
        "name": "BrunoCoin", "ticker": "BRN",
        "height": CHAIN.db.height(), "tip_hash": CHAIN.db.tip_hash(),
        "utxos": CHAIN.db.count_utxos(),
        "mempool": len(CHAIN.db.all_mempool(limit=10000)),
        "peers": CHAIN.db.contar_peers(apenas_ativos=True),
        "reward": CHAIN.current_reward(CHAIN.db.height() + 1),
        "difficulty": CHAIN.current_difficulty(),
    })


# ============================================================
# v4: ENDPOINTS ADICIONAIS
# ============================================================
@app.route("/api/fee-estimate", methods=["GET"])
def fee_estimate():
    try:
        return jsonify({"success": True,
                        "low": CHAIN.estimate_fee("low"),
                        "medium": CHAIN.estimate_fee("medium"),
                        "high": CHAIN.estimate_fee("high"),
                        "min_relay_fee": 1000})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/work", methods=["GET"])
def work():
    return jsonify({"success": True, "height": CHAIN.db.height(),
                    "cumulative_work": CHAIN.cumulative_work()})


@app.route("/api/hd/create", methods=["POST"])
@_rate_limit
def hd_create():
    try:
        data = request.get_json(force=True) or {}
        strength = int(data.get("strength", 128))
        if strength not in (128, 160, 192, 224, 256):
            return jsonify({"ok": False, "msg": "strength invalido"}), 400
        result = HDWalletManager.create(strength=strength)
        return jsonify({"ok": True,
                        "warning": "GUARDE o mnemonico.",
                        **result})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/hd/derive", methods=["POST"])
@_rate_limit
def hd_derive():
    try:
        data = request.get_json(force=True) or {}
        mn = data.get("mnemonic", "").strip()
        index = int(data.get("index", 0))
        if not HDWalletManager.validate_mnemonic(mn):
            return jsonify({"ok": False, "msg": "Mnemonico invalido"}), 400
        return jsonify({"ok": True, **HDWalletManager.from_mnemonic(mn, index=index)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/peers/score", methods=["GET"])
def peers_score():
    try:
        return jsonify({"success": True,
                        "peers": CHAIN.db.listar_peers(apenas_ativos=False),
                        "banned": CHAIN.db.listar_peers_maus(score_min=-100)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/nonce/<pubkey>", methods=["GET"])
def get_nonce(pubkey):
    try:
        return jsonify({"success": True, "pubkey": pubkey,
                        "next_nonce": CHAIN.db.get_nonce_for_pubkey(pubkey)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# v7: REGISTRA BLUEPRINTS DA BRIDGE
# ============================================================
# Bridge BRN -> BTC (sua, já existente)
try:
    from bridge.api_bridge import bridge_bp
    from bridge.db_bridge import init_db as _bridge_init
    app.register_blueprint(bridge_bp)
    _bridge_init()
    print("[Bridge] Blueprint BRN->BTC registrado em /api/bridge/*")
except Exception as e:
    print(f"[Bridge] Falha ao registrar bridge BRN->BTC: {e}")

# Bridge BTC -> BRN (onramp)
try:
    from bridge.onramp import onramp_bp
    app.register_blueprint(onramp_bp)
    print("[Bridge] Blueprint BTC->BRN registrado em /api/bridge/onramp/*")
except Exception as e:
    print(f"[Bridge] Falha ao registrar onramp: {e}")


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("BRN_WEB_PORT", "5000"))
    print(f"BRN Server v7 - http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
