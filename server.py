"""
server.py — Backend HTTP do nó BRN
================================================================
✅ v3: Adiciona endpoints que o app_wallet.py precisa:
      • GET  /api/portfolio/<address>
      • POST /api/transfer
      • POST /api/mine
      • POST /api/faucet
✅ v3: Corrige chamada a métodos inexistentes de Wallet
✅ v3: Tratamento de erro consistente (sempre JSON)
✅ v3: Cache de saldo (evita spam do SQLite)
✅ v3: Faucet com limite por endereço + rate limit por IP
================================================================
"""
from flask import Flask, request, jsonify, g
from flask_cors import CORS
import os
import time
import json
import threading
from functools import wraps

from wallet import Wallet, WalletManager
from blockchain import Blockchain, make_coinbase, txid as calc_txid, signing_hash

# ============================================================
# CONFIG
# ============================================================
app = Flask(__name__)
CORS(app)

CHAIN = Blockchain("brn_v2_chain.db")
WALLETS_FILE = "user_wallets.json"

# Faucet
FAUCET_AMOUNT_BRN = 10
FAUCET_MAX_PER_ADDRESS = 3          # máximo de vezes por endereço
FAUCET_COOLDOWN_S = 60 * 60         # 1h entre faucets do mesmo endereço
_faucet_history: dict[str, list[float]] = {}

# Rate limit simples por IP
RATE_LIMIT = 30
RATE_WINDOW_S = 60
_ip_history: dict[str, list[float]] = {}
_rate_lock = threading.Lock()

# Cache de saldo
_cache_saldos: dict[str, tuple[float, int]] = {}
_cache_lock = threading.Lock()
CACHE_TTL_S = 5


# ============================================================
# UTILITÁRIOS
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
                                "error": "Muitas requisições. Aguarde."}), 429
            hist.append(agora)
        return f(*args, **kwargs)
    return wrapper


def _saldo_cache_get(addr: str) -> int | None:
    with _cache_lock:
        if addr in _cache_saldos:
            ts, val = _cache_saldos[addr]
            if time.time() - ts < CACHE_TTL_S:
                return val
    return None


def _saldo_cache_set(addr: str, val: int):
    with _cache_lock:
        _cache_saldos[addr] = (time.time(), val)


def _saldo_cache_invalidate(addr: str):
    with _cache_lock:
        _cache_saldos.pop(addr, None)


def carregar_wallets() -> dict:
    if not os.path.exists(WALLETS_FILE):
        return {}
    try:
        with open(WALLETS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def salvar_wallets(w: dict):
    with open(WALLETS_FILE, "w") as f:
        json.dump(w, f, indent=2)


# ============================================================
# CARTEIRA
# ============================================================
@app.route("/api/nova-carteira", methods=["POST"])
@_rate_limit
def nova_carteira():
    """Gera carteira BRN nova e retorna address + pubkey + privkey (1x)."""
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
# SALDO / PORTFOLIO  (dois nomes para compatibilidade)
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
            "success": True,
            "address": address,
            "balance_sats": cached,
            "balance_brn": cached / 10**8,
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/portfolio/<address>", methods=["GET"])
def portfolio(address):
    """
    ✅ NOVO: usado pelo app_wallet.py.
    Retorna portfolio estruturado: {portfolio: {BRN: saldo_brn, KYC: 0}}
    """
    try:
        cached = _saldo_cache_get(address)
        if cached is None:
            utxos = CHAIN.db.get_utxos(address)
            cached = sum(u["amount"] for u in utxos)
            _saldo_cache_set(address, cached)
        return jsonify({
            "portfolio": {
                "BRN": cached / 10**8,
                "KYC": 0,
            }
        })
    except Exception as e:
        return jsonify({"portfolio": {}, "error": str(e)}), 500


# ============================================================
# TRANSAÇÕES
# ============================================================
@app.route("/api/transacoes/<address>", methods=["GET"])
def transacoes(address):
    """Lista transações que envolvem um endereço (varre o DB)."""
    try:
        txs = []
        for h in range(CHAIN.db.height() + 1):
            block = CHAIN.db.get_block(h)
            if not block:
                continue
            for tx in block["transactions"]:
                envolve = any(
                    o.get("address") == address for o in tx["outputs"]
                )
                if envolve:
                    txs.append({
                        "txid": tx["txid"],
                        "block_height": h,
                        "timestamp": tx.get("timestamp", 0),
                        "outputs": tx["outputs"],
                    })
        return jsonify({
            "success": True,
            "address": address,
            "count": len(txs),
            "transactions": txs[-50:],
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/transfer", methods=["POST"])
@_rate_limit
def transfer():
    """
    ✅ NOVO: usado pelo app_wallet.py.
    Body esperado:
    {
      "type": "transfer",
      "asset_id": "BRN",
      "from": "brn1...",
      "to": "brn1...",
      "amount": 10.5,
      "public_key": "hex",
      "private_key": "hex",
      "nonce": 1234567890
    }
    """
    try:
        data = request.get_json(force=True) or {}

        sender = data.get("from", "").strip()
        to = data.get("to", "").strip()
        asset_id = data.get("asset_id", "BRN")
        amount = data.get("amount")
        sk = data.get("private_key", "")
        pk = data.get("public_key", "")

        if asset_id != "BRN":
            return jsonify({"ok": False, "msg": "Só BRN via esta API."}), 400
        if not WalletManager.validate_address(sender):
            return jsonify({"ok": False, "msg": "Remetente inválido."}), 400
        if not WalletManager.validate_address(to):
            return jsonify({"ok": False, "msg": "Destinatário inválido."}), 400
        if sender == to:
            return jsonify({"ok": False, "msg": "Não pode enviar para si."}), 400

        try:
            amount_sats = int(float(amount) * 10**8)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "msg": "Valor inválido."}), 400
        if amount_sats <= 0:
            return jsonify({"ok": False, "msg": "Valor deve ser > 0."}), 400

        # Reconstrói wallet do remetente
        try:
            w = Wallet(private_key_hex=sk)
        except Exception:
            return jsonify({"ok": False, "msg": "Chave privada inválida."}), 400
        if w.address != sender:
            return jsonify({"ok": False, "msg": "Chave privada não corresponde ao endereço."}), 400

        # Seleciona UTXOs suficientes
        utxos = CHAIN.db.get_utxos(sender)
        utxos.sort(key=lambda u: u["amount"], reverse=True)
        total = 0
        escolhidos = []
        for u in utxos:
            escolhidos.append(u)
            total += u["amount"]
            if total >= amount_sats + 1000:  # 1000 sats de fee mínima
                break
        if total < amount_sats:
            return jsonify({"ok": False, "msg": f"Saldo insuficiente ({total/1e8:.8f} BRN)."}), 400

        # Fee fixa (1000 sats)
        FEE = 1000
        troco = total - amount_sats - FEE
        outputs = [{"address": to, "amount": amount_sats, "pubkey": ""}]
        if troco > 0:
            outputs.append({"address": sender, "amount": troco, "pubkey": ""})

        inputs = [
            {"txid": u["txid"], "vout": u["vout"],
             "pubkey": w.pub_hex, "signature": ""}
            for u in escolhidos
        ]

        tx = {
            "txid": "",
            "inputs": inputs,
            "outputs": outputs,
            "timestamp": int(time.time()),
            "locktime": 0,
        }

        # Assina
        h = signing_hash(tx)
        for inp in tx["inputs"]:
            inp["signature"] = w.sign(h)

        tx["txid"] = calc_txid(tx)

        # Envia à blockchain
        ok, msg = CHAIN.submit_tx(tx)
        if not ok:
            return jsonify({"ok": False, "msg": msg}), 400

        # Invalida cache
        _saldo_cache_invalidate(sender)
        _saldo_cache_invalidate(to)

        return jsonify({
            "ok": True,
            "txid": tx["txid"],
            "msg": "Transação aceita na mempool.",
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# MINERAÇÃO
# ============================================================
@app.route("/api/mine", methods=["POST"])
@_rate_limit
def mine():
    """
    ✅ NOVO: usado pelo app_wallet.py.
    Body: {"validator_address": "brn1..."}
    Minera 1 bloco.
    """
    try:
        data = request.get_json(force=True) or {}
        miner = data.get("validator_address", "").strip()
        if not WalletManager.validate_address(miner):
            return jsonify({"ok": False, "msg": "Endereço inválido."}), 400

        block = CHAIN.mine_block(miner)
        if not block:
            return jsonify({"ok": False, "msg": "Falha ao minerar."}), 500

        _saldo_cache_invalidate(miner)
        return jsonify({
            "ok": True,
            "msg": f"Bloco #{block['height']} minerado!",
            "block": {
                "height": block["height"],
                "hash": block["hash"],
                "txs": len(block["transactions"]),
                "difficulty": block["difficulty"],
                "nonce": block["nonce"],
            },
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# FAUCET
# ============================================================
@app.route("/api/faucet", methods=["POST"])
@_rate_limit
def faucet():
    """✅ NOVO: envia BRN grátis para novas carteiras."""
    try:
        data = request.get_json(force=True) or {}
        addr = data.get("address", "").strip()

        if not WalletManager.validate_address(addr):
            return jsonify({"ok": False, "msg": "Endereço inválido."}), 400

        agora = time.time()
        hist = _faucet_history.setdefault(addr, [])
        hist[:] = [t for t in hist if agora - t < FAUCET_COOLDOWN_S]
        if len(hist) >= FAUCET_MAX_PER_ADDRESS:
            return jsonify({"ok": False,
                            "msg": "Limite de faucet atingido para este endereço."}), 429
        if hist and agora - hist[-1] < FAUCET_COOLDOWN_S:
            falta = int(FAUCET_COOLDOWN_S - (agora - hist[-1]))
            return jsonify({"ok": False,
                            "msg": f"Aguarde {falta}s para novo faucet."}), 429

        # Faucet = minera um bloco creditando o endereço
        block = CHAIN.mine_block(addr)
        if not block:
            return jsonify({"ok": False, "msg": "Falha ao processar faucet."}), 500

        hist.append(agora)
        _saldo_cache_invalidate(addr)

        return jsonify({
            "ok": True,
            "msg": f"Faucet enviado! +{FAUCET_AMOUNT_BRN} BRN (bloco #{block['height']})",
            "txid": block["transactions"][0]["txid"],
            "amount": FAUCET_AMOUNT_BRN,
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# INFO DA CADEIA
# ============================================================
@app.route("/api/chain-info", methods=["GET"])
def chain_info():
    return jsonify({
        "success": True,
        "name": "BrunoCoin",
        "ticker": "BRN",
        "height": CHAIN.db.height(),
        "tip_hash": CHAIN.db.tip_hash(),
        "reward": CHAIN.current_reward(CHAIN.db.height() + 1),
        "difficulty": CHAIN.current_difficulty(),
    })


@app.route("/api/status", methods=["GET"])
def status():
    """✅ NOVO: usado pelo app_wallet.node_status()."""
    return jsonify({
        "name": "BrunoCoin",
        "ticker": "BRN",
        "height": CHAIN.db.height(),
        "tip_hash": CHAIN.db.tip_hash(),
        "utxos": CHAIN.db.count_utxos(),
        "mempool": len(CHAIN.db.all_mempool(limit=10000)),
        "peers": CHAIN.db.contar_peers(apenas_ativos=True),
        "reward": CHAIN.current_reward(CHAIN.db.height() + 1),
        "difficulty": CHAIN.current_difficulty(),
    })


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("BRN_WEB_PORT", "5000"))
    print(f"🚀 BRN Server v3 — http://0.0.0.0:{port}")
    print(f"   DB: brn_v2_chain.db")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
