"""
main.py — Entrypoint unificado do nó BRN
================================================================
Sobe em um único processo:
  1. Blockchain (abre DB)
  2. P2P Manager (servidor TCP + descoberta multicast + UPnP)
  3. Server HTTP (Flask, porta 5000)
  4. Explorer (Flask, porta 8080)
  5. Carteira desktop (opcional — só se BRN_WALLET=1)

Uso:
    python main.py

Variáveis de ambiente:
    BRN_DB                (default: brn_v2_chain.db)
    BRN_WEB_PORT          (default: 5000)
    BRN_EXPLORER_PORT     (default: 8080)
    BRN_P2P_PORT          (default: 6001)
    BRN_UPNP              (default: 1)
    BRN_WEB_PASS          (obrigatório se BRN_WALLET=1)
    BRN_WALLET            (default: 0)  # 1 = abre carteira desktop
"""
import os
import signal
import threading
import time

from blockchain import Blockchain
from server import app as http_app
from explorer import app as explorer_app
from p2p_unified import P2PManager

# ============================================================
# CONFIG
# ============================================================
DB_PATH = os.environ.get("BRN_DB", "brn_v2_chain.db")
HTTP_PORT = int(os.environ.get("BRN_WEB_PORT", "5000"))
EXPLORER_PORT = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))
P2P_PORT = int(os.environ.get("BRN_P2P_PORT", "6001"))
ENABLE_UPNP = os.environ.get("BRN_UPNP", "1") == "1"
ENABLE_WALLET = os.environ.get("BRN_WALLET", "0") == "1"

_shutdown = threading.Event()


# ============================================================
# THREADS
# ============================================================
def run_http():
    print(f"🌐 [HTTP]     http://0.0.0.0:{HTTP_PORT}")
    http_app.run(
        host="0.0.0.0", port=HTTP_PORT,
        threaded=True, debug=False, use_reloader=False,
    )


def run_explorer():
    print(f"🔍 [Explorer] http://0.0.0.0:{EXPLORER_PORT}")
    explorer_app.run(
        host="0.0.0.0", port=EXPLORER_PORT,
        threaded=True, debug=False, use_reloader=False,
    )


def run_wallet():
    """Sobe a carteira desktop (PyWebView) em thread separada."""
    try:
        from app_wallet_v3 import WalletApi
        import webview
        from pathlib import Path

        index_path = Path(__file__).parent / "index.html"
        if not index_path.exists():
            print(f"⚠️ [Wallet] index.html não encontrado em {index_path}")
            return

        api = WalletApi()
        print("💼 [Wallet] Abrindo janela desktop…")
        webview.create_window(
            "BRN RWA - Carteira Digital",
            url=index_path.resolve().as_uri(),
            js_api=api,
            width=1020, height=880,
            min_size=(820, 640),
            background_color="#0d1117",
        )
        webview.start(debug=False)
    except Exception as e:
        print(f"⚠️ [Wallet] Falha: {e}")


# ============================================================
# MAIN
# ============================================================
def main():
    print("=" * 64)
    print("  🚀 BRN Node v3 — Boot")
    print("=" * 64)

    # 1. Blockchain
    print(f"📦 [Chain] Abrindo DB: {DB_PATH}")
    chain = Blockchain(DB_PATH)
    print(f"           Altura atual : {chain.db.height()}")
    print(f"           Tip hash     : {chain.db.tip_hash()[:20]}…")

    # 2. P2P
    p2p = P2PManager(chain, tcp_port=P2P_PORT, enable_upnp=ENABLE_UPNP)
    p2p.start()
    print(f"🌐 [P2P]     TCP porta {P2P_PORT} "
          f"(UPnP={'ON' if ENABLE_UPNP else 'OFF'})")

    # 3. HTTP
    threading.Thread(target=run_http, daemon=True, name="HTTP").start()

    # 4. Explorer
    threading.Thread(target=run_explorer, daemon=True, name="Explorer").start()

    # 5. Wallet (opcional)
    if ENABLE_WALLET:
        threading.Thread(target=run_wallet, daemon=True, name="Wallet").start()

    print("\n✅ Nó pronto. Ctrl+C para encerrar.\n")

    # Loop de status
    try:
        while not _shutdown.is_set():
            time.sleep(30)
            peers = p2p.get_status()
            print(
                f"📊 Altura={chain.db.height()} | "
                f"Peers={peers['peer_count']} | "
                f"Mempool={len(chain.db.all_mempool(limit=1000))} | "
                f"UTXOs={chain.db.count_utxos()}"
            )
    except KeyboardInterrupt:
        pass

    # Shutdown
    print("\n🛑 Encerrando…")
    p2p.stop()
    chain.db.close()
    print("👋 Até logo.")


def _on_signal(signum, frame):
    _shutdown.set()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    main()
