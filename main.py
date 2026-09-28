"""
main.py - Entrypoint unificado
Sobe: Blockchain + P2P + HTTP + Explorer + Bridge (2 direções) + Wallet
"""
import os
import signal
import threading
import time

from blockchain import Blockchain
from server import app as http_app
from explorer import app as explorer_app
from p2p_unified import P2PManager

DB_PATH       = os.environ.get("BRN_DB", "brn_v2_chain.db")
HTTP_PORT     = int(os.environ.get("BRN_WEB_PORT", "5000"))
EXPLORER_PORT = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))
P2P_PORT      = int(os.environ.get("BRN_P2P_PORT", "6001"))

ENABLE_UPNP   = os.environ.get("BRN_UPNP", "1") == "1"
ENABLE_WALLET = os.environ.get("BRN_WALLET", "0") == "1"
ENABLE_BRIDGE = os.environ.get("BRN_BRIDGE", "1") == "1"

_shutdown = threading.Event()


def run_http():
    print(f"[HTTP]     http://0.0.0.0:{HTTP_PORT}")
    http_app.run(host="0.0.0.0", port=HTTP_PORT,
                 threaded=True, debug=False, use_reloader=False)


def run_explorer():
    print(f"[Explorer] http://0.0.0.0:{EXPLORER_PORT}")
    explorer_app.run(host="0.0.0.0", port=EXPLORER_PORT,
                     threaded=True, debug=False, use_reloader=False)


def run_wallet():
    try:
        os.environ.setdefault("BRN_WEB_PASS", "carteira123")
        from app_wallet_v3 import WalletApi
        import webview
        from pathlib import Path

        index_path = Path(__file__).parent / "index_wallet.html"
        if not index_path.exists():
            print(f"[Wallet] index_wallet.html nao encontrado: {index_path}")
            return

        api = WalletApi()
        print("[Wallet] Abrindo janela desktop...")
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
        print(f"[Wallet] Falha: {e}")


def run_bridge():
    """Sobe watcher (BRN->BTC) e worker (envio BTC) como threads."""
    try:
        from bridge.config import validar_config
        from bridge.watcher_lrn import iniciar_watcher
        from bridge.worker import iniciar_worker

        erros = validar_config()
        if erros:
            print("[Bridge] Configuração incompleta:")
            for e in erros:
                print(f"[Bridge]   - {e}")
            print("[Bridge] Bridge continuará mas alguns fluxos ficarão desativados.")

        iniciar_watcher(intervalo=30)
        iniciar_worker(intervalo=60)
        print("[Bridge] Watcher + Worker iniciados")
    except Exception as e:
        print(f"[Bridge] Falha: {e}")


def main():
    print("=" * 64)
    print("  BRN Node v7 + Bridge bidirecional + Wallet")
    print("=" * 64)

    print(f"[Chain] Abrindo DB: {DB_PATH}")
    chain = Blockchain(DB_PATH)
    print(f"        Altura atual : {chain.db.height()}")
    print(f"        Tip hash     : {chain.db.tip_hash()[:20]}...")

    p2p = P2PManager(chain, tcp_port=P2P_PORT, enable_upnp=ENABLE_UPNP)
    p2p.start()
    print(f"[P2P]     TCP porta {P2P_PORT} (UPnP={'ON' if ENABLE_UPNP else 'OFF'})")

    threading.Thread(target=run_http,     daemon=True, name="HTTP").start()
    threading.Thread(target=run_explorer, daemon=True, name="Explorer").start()

    if ENABLE_BRIDGE:
        threading.Thread(target=run_bridge, daemon=True, name="Bridge").start()

    if ENABLE_WALLET:
        threading.Thread(target=run_wallet, daemon=True, name="Wallet").start()

    print()
    print("No pronto. Ctrl+C para encerrar.")
    print()

    try:
        while not _shutdown.is_set():
            time.sleep(30)
            try:
                peers = p2p.get_status()
                print(f"[Status] Altura={chain.db.height()} | "
                      f"Peers={peers['peer_count']} | "
                      f"Mempool={len(chain.db.all_mempool(limit=1000))} | "
                      f"UTXOs={chain.db.count_utxos()}")
            except Exception:
                pass
    except KeyboardInterrupt:
        pass

    print()
    print("Encerrando...")
    p2p.stop()
    chain.db.close()
    print("Ate logo.")


def _on_signal(signum, frame):
    _shutdown.set()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    main()
