"""server.py — Backend HTTP do no BRN (v8.6)
v8.6: + /api/minhas-txs/<addr> (lista txs com direcao/status/conf)
      + fallback robusto em /api/transacoes (try/except por bloco)
v8.5: _pubkey_from_db_or_payload consulta user_wallets.json, aceita
      private_key no payload, e registra pubkey descoberta.
v8.4: + /api/miner/start e /api/miner/stop (miner_loop singleton)
      + aliases /api/miner/on, /api/start-mining, /api/send, /api/enviar
v8.3: + endpoints de contratos inteligentes (/api/contract/*)
      + /api/sync-info (usado pela carteira desktop)
      + /api/miner/status
v8.0: - removida bridge (nao usada)
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

# ============================================================
# CONFIG
# ============================================================
FAUCET_AMOUNT_BRN = 10
FAUCET_MAX_PER_ADDRESS = 3
FAUCET_COOLDOWN_S = 60 * 60
_faucet_history = {}

RATE_LIMIT = 30
RATE_WINDOW_S = 60
_ip_history = {}
_rate_lock = threading.Lock()

_cache_saldos = {}
_cache_lock = threading.Lock()
CACHE_TTL_S = 5


# ============================================================
# RATE LIMIT
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


# ============================================================
# CACHE DE SALDO
# ============================================================
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


# ============================================================
# WALLETS EM JSON
# ============================================================
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
# v8.5: RESOLUCAO DA PUBKEY (4 fontes + derivacao)
# ============================================================
def _pubkey_from_db_or_payload(addr: str, payload_pubkey: str = "",
                               payload_sk: str = "") -> str:
    """
    Retorna a pubkey do endereco. Ordem de prioridade:
      1. payload_pubkey (frontend manda)
      2. payload_sk   -> deriva pubkey e confere endereco
      3. user_wallets.json
      4. tabela utxos via get_utxos() (thread-safe)
    """
    # 1) Payload
    if payload_pubkey:
        return payload_pubkey.strip()

    # 2) Derivar da private key
    if payload_sk:
        try:
            _w = Wallet(private_key_hex=payload_sk.strip())
            if _w.address == addr:
                pk = _w.pub_hex
                try:
                    wallets = carregar_wallets()
                    if wallets.get(addr, {}).get("public_key") != pk:
                        wallets[addr] = {"public_key": pk}
                        salvar_wallets(wallets)
                except Exception:
                    pass
                return pk
        except Exception:
            pass

    # 3) user_wallets.json
    try:
        wallets = carregar_wallets()
        entry = wallets.get(addr) or {}
        pk = (entry.get("public_key") or entry.get("pubkey") or "").strip()
        if pk:
            return pk
    except Exception:
        pass

    # 4) Tabela utxos (usa get_utxos, thread-safe)
    try:
        for u in CHAIN.db.get_utxos(addr):
            pk = (u.get("pubkey") or "").strip()
            if pk:
                return pk
    except Exception:
        pass

    return ""


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
            "warning": "Guarde a chave privada."
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
    """
    Lista txs onde o endereco aparece como OUTPUT (recebidas).
    Versao robusta: try/except por bloco, para nao derrubar a rota
    se um bloco especifico tiver dado corrompido.
    """
    try:
        txs = []
        altura = CHAIN.db.height()
        for h in range(altura + 1):
            try:
                block = CHAIN.db.get_block(h)
            except Exception:
                continue
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


@app.route("/api/minhas-txs/<address>", methods=["GET"])
def minhas_txs(address):
    """
    v8.6: Lista todas as txs que envolvem o endereco.
    Inclui direcao (sent/received/self), status (pending/confirmed),
    valor e confirmacoes. Cobre tanto blocos quanto mempool.
    """
    try:
        altura = CHAIN.db.height()

        # Coleta pubkeys associadas ao endereco (utxos atuais)
        try:
            meus_utxos = CHAIN.db.get_utxos(address)
        except Exception:
            meus_utxos = []
        minhas_pubkeys = {u.get("pubkey") for u in meus_utxos if u.get("pubkey")}

        txs = []
        for h in range(altura + 1):
            try:
                block = CHAIN.db.get_block(h)
            except Exception:
                continue
            if not block:
                continue

            for tx in block["transactions"]:
                is_mine_out = any(
                    o.get("address") == address for o in tx["outputs"]
                )
                is_mine_in = False
                if minhas_pubkeys:
                    for inp in tx["inputs"]:
                        pk = inp.get("pubkey")
                        if pk and pk in minhas_pubkeys:
                            is_mine_in = True
                            break

                # Coinbase sem vinculo: se nao for output nosso, ignora
                if not (is_mine_out or is_mine_in):
                    continue

                meu_out = sum(
                    o["amount"] for o in tx["outputs"]
                    if o.get("address") == address
                )

                if is_mine_in and is_mine_out:
                    direction = "self"
                elif is_mine_in:
                    direction = "sent"
                else:
                    direction = "received"

                confs = max(0, altura - h)
                txs.append({
                    "txid": tx["txid"],
                    "direction": direction,
                    "amount": meu_out,
                    "status": "confirmed",
                    "block_height": h,
                    "confirmations": confs,
                    "timestamp": tx.get("timestamp", 0),
                })

        # Mempool: txs pendentes que envolvem o endereco
        try:
            mem = CHAIN.db.all_mempool(limit=500)
            for tx in mem:
                is_mine_out = any(
                    o.get("address") == address for o in tx["outputs"]
                )
                is_mine_in = False
                if minhas_pubkeys:
                    for inp in tx["inputs"]:
                        pk = inp.get("pubkey")
                        if pk and pk in minhas_pubkeys:
                            is_mine_in = True
                            break
                if not (is_mine_out or is_mine_in):
                    continue

                meu_out = sum(
                    o["amount"] for o in tx["outputs"]
                    if o.get("address") == address
                )
                if is_mine_in and is_mine_out:
                    direction = "self"
                elif is_mine_in:
                    direction = "sent"
                else:
                    direction = "received"

                txs.append({
                    "txid": tx["txid"],
                    "direction": direction,
                    "amount": meu_out,
                    "status": "pending",
                    "block_height": None,
                    "confirmations": 0,
                    "timestamp": tx.get("timestamp", 0),
                })
        except Exception:
            pass

        txs.sort(key=lambda t: (t.get("timestamp") or 0), reverse=True)
        return jsonify({
            "ok": True,
            "address": address,
            "count": len(txs),
            "transactions": txs[:100],
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


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
            return jsonify({"ok": False, "msg": "So BRN."}), 400
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
            return jsonify({"ok": False,
                            "msg": f"Saldo insuficiente ({total/1e8:.8f} BRN)."}), 400

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
                        "msg": "Aceita na mempool."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# Aliases de compatibilidade com a carteira desktop
@app.route("/api/send", methods=["POST"])
@app.route("/api/enviar", methods=["POST"])
def transfer_alias():
    return transfer()


# ============================================================
# MINERACAO (uma vez, sob demanda)
# ============================================================
@app.route("/api/mine", methods=["POST"])
@_rate_limit
def mine():
    try:
        data = request.get_json(force=True) or {}
        miner = (data.get("validator_address")
                 or data.get("address")
                 or "").strip()
        if not WalletManager.validate_address(miner):
            return jsonify({"ok": False, "msg": "Endereco invalido."}), 400

        miner_pubkey = _pubkey_from_db_or_payload(
            miner,
            data.get("miner_pubkey", "") or data.get("pubkey", ""),
            data.get("private_key", "") or data.get("privatekey", ""),
        )
        if not miner_pubkey:
            return jsonify({"ok": False,
                            "msg": "Pubkey do minerador desconhecida. "
                                   "Receba uma tx antes ou passe 'miner_pubkey'."}), 400

        block = CHAIN.mine_block(miner, miner_pubkey)
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
# v8.4: START/STOP DA MINERACAO (miner_loop singleton)
# ============================================================
@app.route("/api/miner/start", methods=["POST"])
@_rate_limit
def miner_start():
    try:
        data = request.get_json(force=True) or {}
        addr = (data.get("validator_address")
                or data.get("address")
                or "").strip()
        pubkey_payload = (data.get("miner_pubkey")
                          or data.get("pubkey")
                          or data.get("public_key")
                          or "").strip()
        sk_payload = (data.get("private_key")
                      or data.get("privatekey")
                      or "").strip()

        if not WalletManager.validate_address(addr):
            return jsonify({"ok": False, "msg": "Endereco invalido."}), 400

        pubkey = _pubkey_from_db_or_payload(addr, pubkey_payload, sk_payload)
        if not pubkey:
            return jsonify({
                "ok": False,
                "msg": "Pubkey desconhecida. Receba uma tx antes "
                       "ou passe 'miner_pubkey'."
            }), 400

        from miner_loop import get_miner
        m = get_miner(CHAIN)
        ok, msg = m.start(addr, pubkey)
        if not ok:
            return jsonify({"ok": False, "msg": msg, **m.status()}), 400
        return jsonify({"ok": True, "msg": "Minerador iniciado.", **m.status()})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/miner/stop", methods=["POST"])
@_rate_limit
def miner_stop():
    try:
        from miner_loop import get_miner
        m = get_miner(CHAIN)
        ok, msg = m.stop()
        return jsonify({"ok": ok, "msg": msg, **m.status()})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# Aliases
@app.route("/api/miner/on", methods=["POST"])
@app.route("/api/start-mining", methods=["POST"])
def miner_start_alias():
    return miner_start()


@app.route("/api/miner/off", methods=["POST"])
@app.route("/api/stop-mining", methods=["POST"])
def miner_stop_alias():
    return miner_stop()


# ============================================================
# v8.3: MINER STATUS
# ============================================================
@app.route("/api/miner/status", methods=["GET"])
def miner_status():
    try:
        from miner_loop import get_miner
        m = get_miner(CHAIN)
        st = m.status()
        return jsonify({
            "running": st.get("running", False),
            "address": st.get("address", ""),
            "pubkey": st.get("pubkey", ""),
            "blocks_mined": st.get("blocks_mined", 0),
            "count": st.get("count", st.get("blocks_mined", 0)),
            "last_height": st.get("last_height"),
            "uptime": st.get("uptime", 0),
            "consecutive_failures": st.get("consecutive_failures", 0),
            "height": CHAIN.db.height(),
            "difficulty": CHAIN.current_difficulty(),
            "last_error": st.get("last_error", ""),
        })
    except Exception as e:
        return jsonify({"running": False, "error": str(e)}), 200


# ============================================================
# FAUCET
# ============================================================
@app.route("/api/faucet", methods=["POST"])
@_rate_limit
def faucet():
    try:
        data = request.get_json(force=True) or {}
        addr = (data.get("address") or data.get("validator_address") or "").strip()
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

        miner_pubkey = _pubkey_from_db_or_payload(
            addr,
            data.get("miner_pubkey", "") or data.get("pubkey", ""),
            data.get("private_key", "") or data.get("privatekey", ""),
        )
        if not miner_pubkey:
            return jsonify({"ok": False,
                            "msg": "Pubkey desconhecida. Receba uma tx primeiro."}), 400

        block = CHAIN.mine_block(addr, miner_pubkey)
        if not block:
            return jsonify({"ok": False, "msg": "Falha ao minerar."}), 500
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
        "contracts": CHAIN.db.contract_count() if hasattr(CHAIN.db, "contract_count") else 0,
    })


# ============================================================
# FEE / WORK / NONCE
# ============================================================
@app.route("/api/fee-estimate", methods=["GET"])
def fee_estimate():
    return jsonify({"success": True,
                    "low": CHAIN.estimate_fee("low"),
                    "medium": CHAIN.estimate_fee("medium"),
                    "high": CHAIN.estimate_fee("high"),
                    "min_relay_fee": 1000})


@app.route("/api/work", methods=["GET"])
def work():
    return jsonify({"success": True, "height": CHAIN.db.height(),
                    "cumulative_work": CHAIN.cumulative_work()})


@app.route("/api/nonce/<pubkey>", methods=["GET"])
def get_nonce(pubkey):
    return jsonify({"success": True, "pubkey": pubkey,
                    "next_nonce": CHAIN.db.get_nonce_for_pubkey(pubkey)})


# ============================================================
# HD WALLET
# ============================================================
@app.route("/api/hd/create", methods=["POST"])
@_rate_limit
def hd_create():
    try:
        data = request.get_json(force=True) or {}
        strength = int(data.get("strength", 128))
        if strength not in (128, 160, 192, 224, 256):
            return jsonify({"ok": False, "msg": "strength invalido"}), 400
        result = HDWalletManager.create(strength=strength)
        try:
            wallets = carregar_wallets()
            wallets[result["address"]] = {"public_key": result["public_key"]}
            salvar_wallets(wallets)
        except Exception:
            pass
        return jsonify({"ok": True, "warning": "GUARDE o mnemonico.", **result})
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
        result = HDWalletManager.from_mnemonic(mn, index=index)
        try:
            wallets = carregar_wallets()
            wallets[result["address"]] = {"public_key": result["public_key"]}
            salvar_wallets(wallets)
        except Exception:
            pass
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# PEERS
# ============================================================
@app.route("/api/peers/score", methods=["GET"])
def peers_score():
    try:
        return jsonify({"success": True,
                        "peers": CHAIN.db.listar_peers(apenas_ativos=False),
                        "banned": CHAIN.db.listar_peers_maus(score_min=-100)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================
# v8.3: SYNC INFO (usado pela carteira desktop)
# ============================================================
@app.route("/api/sync-info", methods=["GET"])
def sync_info():
    try:
        peers = CHAIN.db.contar_peers(apenas_ativos=True)
        local_h = CHAIN.db.height()
        local_work = CHAIN.cumulative_work()

        target_h = local_h
        try:
            todos = CHAIN.db.listar_peers(apenas_ativos=False)
            if todos:
                heights = [p.get("height") or 0 for p in todos]
                if heights:
                    target_h = max(target_h, max(heights))
        except Exception:
            pass

        if target_h <= 0:
            percent = 100.0
        else:
            percent = min(100.0, round(local_h / target_h * 100, 2))

        return jsonify({
            "ok": True,
            "sync": {
                "percent": percent,
                "height": local_h,
                "target": target_h,
                "peers": peers,
                "work": local_work,
            },
            "miner_target": CHAIN.db.get_meta("miner_address") or "",
            "bridge": {
                "onramp_ativo": False,
                "offramp_ativo": False,
                "taxa": 0,
                "btc_cofre": "",
            },
            "bridge_onramp": {
                "processados": 0,
                "ultima_sync": None,
            },
        })
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500


# ============================================================
# v8.3: CONTRATOS INTELIGENTES
# ============================================================
@app.route("/api/contract/deploy", methods=["POST"])
@_rate_limit
def contract_deploy():
    try:
        data = request.get_json(force=True) or {}
        owner = (data.get("owner") or "").strip()
        code = data.get("code")
        metadata = data.get("metadata")

        if not WalletManager.validate_address(owner):
            return jsonify({"ok": False, "msg": "owner invalido"}), 400
        if not isinstance(code, dict):
            return jsonify({"ok": False, "msg": "code deve ser dict"}), 400

        try:
            from contracts import ContractVM, deploy
        except ImportError as e:
            return jsonify({"ok": False, "msg": f"contracts.py nao encontrado: {e}"}), 500

        ok, msg = ContractVM.validate(code)
        if not ok:
            return jsonify({"ok": False, "msg": msg}), 400

        r = deploy(CHAIN, owner, code, metadata)
        return jsonify(r)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/contract/call", methods=["POST"])
@_rate_limit
def contract_call():
    try:
        data = request.get_json(force=True) or {}
        contract_id = (data.get("contract_id") or "").strip()
        caller = (data.get("caller") or "").strip()
        args = data.get("args") or {}

        if not contract_id:
            return jsonify({"ok": False, "msg": "contract_id obrigatorio"}), 400

        try:
            from contracts import call as _call
        except ImportError as e:
            return jsonify({"ok": False, "msg": f"contracts.py nao encontrado: {e}"}), 500

        r = _call(CHAIN, contract_id, caller, args)
        return jsonify(r)
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/contract/<contract_id>", methods=["GET"])
def contract_info(contract_id):
    try:
        c = CHAIN.db.contract_get(contract_id)
        if not c:
            return jsonify({"ok": False, "msg": "contrato nao existe"}), 404
        return jsonify({
            "ok": True,
            "contract": c,
            "state": CHAIN.db.contract_get_state(contract_id),
            "events": CHAIN.db.contract_get_events(contract_id, limit=20),
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/contracts", methods=["GET"])
def contracts_list():
    try:
        owner = (request.args.get("owner") or "").strip() or None
        return jsonify({
            "ok": True,
            "contracts": CHAIN.db.contract_list(owner=owner, limit=100),
            "total": CHAIN.db.contract_count(),
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("BRN_WEB_PORT", "5000"))
    print(f"BRN Server v8.6 - http://0.0.0.0:{port}")

    try:
        from miner_loop import iniciar_mineracao
        iniciar_mineracao(CHAIN)
    except Exception as e:
        print(f"[server] aviso: auto-miner nao iniciado: {e}")

    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
