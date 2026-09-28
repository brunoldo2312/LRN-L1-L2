"""
explorer.py — Explorador de blocos BRN (Flask)
============================================================
Substitui o explorer.py atual com:
  ✅ CORS habilitado (para o site web acessar)
  ✅ Novos endpoints: /api/peers, /api/events, /api/stats
  ✅ Rate limit simples por IP
  ✅ Página 404 customizada em JSON
  ✅ Uso correto de ChainDB.close()
============================================================
"""

import os
import time
import threading
from collections import defaultdict
from flask import Flask, jsonify, send_from_directory, request, abort

from db import ChainDB

app = Flask(__name__, static_folder=".")
DB_PATH = os.environ.get("BRN_DB", "brn_v2_chain.db")
PORT = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))
CORS_ORIGIN = os.environ.get("BRN_CORS_ORIGIN", "*")

# Rate limit simples: máx 60 req/min por IP
RATE_LIMIT = 60
_rate_window: dict[str, list[float]] = defaultdict(list)
_rate_lock = threading.Lock()


def _rate_limit_ok(ip: str) -> bool:
    agora = time.time()
    with _rate_lock:
        janela = _rate_window[ip]
        janela[:] = [t for t in janela if agora - t < 60]
        if len(janela) >= RATE_LIMIT:
            return False
        janela.append(agora)
        return True


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = CORS_ORIGIN
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp


@app.before_request
def check_rate_limit():
    if request.method == "OPTIONS":
        return ("", 200)
    ip = request.remote_addr or "0.0.0.0"
    if not _rate_limit_ok(ip):
        abort(429)


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "not found"}), 404


@app.errorhandler(429)
def too_many(e):
    return jsonify({"error": "rate limit exceeded"}), 429


def _db() -> ChainDB:
    return ChainDB(DB_PATH)


# ==================== ROTAS ====================
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/status")
def status():
    db = _db()
    try:
        return jsonify(db.get_stats())
    finally:
        db.close()


@app.route("/api/stats")
def stats():
    db = _db()
    try:
        return jsonify(db.get_stats())
    finally:
        db.close()


@app.route("/api/block/<int:h>")
def block(h):
    db = _db()
    try:
        b = db.get_block(h)
        return jsonify(b) if b else ({"error": "not found"}, 404)
    finally:
        db.close()


@app.route("/api/latest")
def latest():
    db = _db()
    try:
        return jsonify(db.latest_blocks(n=10))
    finally:
        db.close()


@app.route("/api/blocks")
def blocks_paginado():
    """✅ NOVO: paginação de blocos ?start=0&limit=20"""
    try:
        start = int(request.args.get("start", 0))
        limit = min(int(request.args.get("limit", 20)), 100)
    except ValueError:
        return {"error": "parâmetros inválidos"}, 400

    db = _db()
    try:
        top = db.height()
        out = []
        for h in range(start, min(start + limit, top + 1)):
            b = db.get_block(h)
            if b:
                out.append({
                    "height": b["height"],
                    "hash": b["hash"],
                    "timestamp": b["timestamp"],
                    "txs": len(b["transactions"]),
                    "difficulty": b["difficulty"],
                })
        return jsonify({"start": start, "count": len(out), "blocks": out})
    finally:
        db.close()


@app.route("/api/balance/<address>")
def balance(address):
    db = _db()
    try:
        return jsonify({
            "address": address,
            "balance": db.balance(address),
            "utxos": db.utxos_for(address),
        })
    finally:
        db.close()


@app.route("/api/mempool")
def mempool():
    db = _db()
    try:
        return jsonify(db.all_mempool(limit=200))
    finally:
        db.close()


@app.route("/api/peers")
def peers():
    """✅ NOVO: peers conhecidos pelo nó."""
    db = _db()
    try:
        return jsonify({
            "count": db.contar_peers(apenas_ativos=False),
            "active": db.contar_peers(apenas_ativos=True),
            "peers": db.listar_peers(apenas_ativos=False),
        })
    finally:
        db.close()


@app.route("/api/events")
def events():
    """✅ NOVO: últimos eventos da rede."""
    try:
        n = min(int(request.args.get("n", 50)), 200)
    except ValueError:
        n = 50
    db = _db()
    try:
        return jsonify(db.ultimos_eventos(n))
    finally:
        db.close()


@app.route("/api/verify")
def verify():
    """✅ NOVO: verifica integridade da cadeia (pode ser lento)."""
    from blockchain import Blockchain
    from chain_validator import verify_chain_dict
    bc = Blockchain(db_path=DB_PATH)
    try:
        return jsonify(verify_chain_dict(bc))
    finally:
        bc.db.close()


if __name__ == "__main__":
    print(f"🔍 Explorer BRN rodando em http://0.0.0.0:{PORT}")
    print(f"   DB: {DB_PATH}")
    print(f"   CORS: {CORS_ORIGIN}")
    app.run(host="0.0.0.0", port=PORT, threaded=True, debug=False)
