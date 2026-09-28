"""tracker.py — Tracker HTTP para descoberta de peers BRN."""
from flask import Flask, request, jsonify
import os
import time

app = Flask(__name__)
PEERS = {}
TTL = 600


@app.route("/anunciar", methods=["POST"])
def anunciar():
    data = request.get_json(force=True, silent=True) or {}
    addr = (data.get("address") or "").strip()
    if not addr or ":" not in addr:
        return jsonify({"ok": False, "msg": "address invalido"}), 400
    PEERS[addr] = time.time()
    return jsonify({"ok": True, "total": len(PEERS)})


@app.route("/peers", methods=["GET"])
def peers():
    agora = time.time()
    for k in list(PEERS.keys()):
        if agora - PEERS[k] > TTL:
            del PEERS[k]
    return jsonify({"peers": list(PEERS.keys())})


@app.route("/", methods=["GET"])
def raiz():
    return f"BRN Tracker — {len(PEERS)} peers ativos"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "9000"))
    print(f"🚀 BRN Tracker — http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
