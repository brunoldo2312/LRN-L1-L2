"""
main.py — Entrypoint unificado do no BRN (v6.0)
============================================================
v6.0 (breaking):
  - Identidade Ed25519 do no carregada/criada ANTES do P2P.
  - Senha do no: --password-file > --password > BRN_NODE_PASSWORD > prompt.
  - Senha default hardcoded da carteira REMOVIDA (BRN_WEB_PASS obrigatoria).
  - P2PManager recebe node_id_priv (handshake autenticado E2P).
  - --rotate-node-id gera nova identidade (backup automatico).

Features herdadas:
  --status        Diagnostico completo e sai
  --headless      Sem interface grafica
  --read-only     Nao minera, nao publica no GitHub
  --version       Mostra versao
  --check-update  Consulta GitHub por versao nova
  --log-level     DEBUG | INFO | WARNING | ERROR
  --log-file      Grava logs em JSON neste arquivo
  --config        Arquivo de configuracao (padrao: config.json)
  --discover      Diagnostico de descoberta de peers por 15s e sai
============================================================
"""
import os
import sys
import signal
import argparse
import threading
import time
import json
import urllib.request
import urllib.error
from pathlib import Path

from brn_config import Config
from brn_logger import setup_logger, get_logger
from version import VERSION, BUILD_DATE, GITHUB_USER, GITHUB_REPO


_shutdown = threading.Event()

NODE_ID_PATH = Path("node_identity.enc")


# ============================================================
# ARGUMENTOS
# ============================================================
def parse_args():
    p = argparse.ArgumentParser(prog="main.py", description="BRN Node v6")
    p.add_argument("--status", action="store_true", help="Diagnostico completo e sai")
    p.add_argument("--headless", action="store_true", help="Sem interface grafica")
    p.add_argument("--read-only", action="store_true", help="Nao minera nem publica")
    p.add_argument("--version", action="store_true", help="Mostra versao e sai")
    p.add_argument("--check-update", action="store_true", help="Verifica versao nova")
    p.add_argument("--config", default="config.json", help="Arquivo de configuracao")
    p.add_argument("--log-level", default=None, help="DEBUG|INFO|WARNING|ERROR")
    p.add_argument("--log-file", default=None, help="Log JSON neste arquivo")

    # v6
    p.add_argument("--password", default=None,
                   help="Senha do no (identidade Ed25519). Prefira --password-file "
                        "ou BRN_NODE_PASSWORD.")
    p.add_argument("--password-file", default=None,
                   help="Le a senha do no deste arquivo (mais seguro).")
    p.add_argument("--rotate-node-id", action="store_true",
                   help="Gera nova identidade (backup automatico da antiga). "
                        "Peers antigos nao vao reconhecer este no.")
    p.add_argument("--discover", action="store_true",
                   help="Diagnostico de descoberta de peers (15s) e sai")
    return p.parse_args()


# ============================================================
# v6: RESOLUCAO DE SENHA DO NO
# ============================================================
def _resolve_password(args) -> str:
    if args.password_file:
        p = Path(args.password_file)
        if not p.exists():
            raise SystemExit(f"--password-file nao encontrado: {p}")
        return p.read_text(encoding="utf-8").rstrip("\r\n")

    if args.password:
        return args.password

    env = os.environ.get("BRN_NODE_PASSWORD")
    if env:
        return env

    if sys.stdin.isatty():
        import getpass
        return getpass.getpass("Senha do no (identidade Ed25519): ")

    raise SystemExit(
        "Senha do no nao informada.\n"
        "Use --password-file, --password, ou defina BRN_NODE_PASSWORD."
    )


# ============================================================
# v6: IDENTIDADE Ed25519
# ============================================================
def load_or_create_node_identity(password: str, rotate: bool = False):
    from crypto import Ed25519PrivateKey
    from secure_store import save_wallet, load_wallet

    log = get_logger("identity")

    if rotate and NODE_ID_PATH.exists():
        backup = NODE_ID_PATH.with_suffix(".enc.bak")
        NODE_ID_PATH.replace(backup)
        log.warning(f"Identidade rotacionada. Backup em {backup}")

    if NODE_ID_PATH.exists():
        try:
            data = load_wallet(str(NODE_ID_PATH), password)
        except Exception as e:
            raise SystemExit(
                f"Falha ao decifrar {NODE_ID_PATH}: {e}\n"
                f"Senha errada? Arquivo corrompido?\n"
                f"Use --rotate-node-id para gerar nova identidade."
            )
        sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["sk"]))
        pub_hex = data["pub"]
        log.info(f"Identidade carregada: {pub_hex[:16]}...")
        return sk, pub_hex

    sk = Ed25519PrivateKey.generate()
    pub_hex = sk.public_key().public_bytes_raw().hex()
    save_wallet(str(NODE_ID_PATH), {
        "sk": sk.private_bytes_raw().hex(),
        "pub": pub_hex,
    }, password)
    log.info(f"Identidade nova criada: {pub_hex[:16]}... ({NODE_ID_PATH})")
    return sk, pub_hex


# ============================================================
# VERSION CHECK
# ============================================================
def _parse_version(s):
    try:
        return tuple(int(p) for p in s.strip().lstrip("v").split(".")[:3])
    except Exception:
        return (0, 0, 0)


def check_for_update(timeout=5):
    url = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main/version.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "brn-node"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        remote = data.get("version", "0.0.0")
        return {
            "ok": True,
            "local": VERSION,
            "remote": remote,
            "update_available": _parse_version(remote) > _parse_version(VERSION),
            "url": data.get("url", f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}"),
            "notes": data.get("notes", ""),
        }
    except Exception as e:
        return {"ok": False, "local": VERSION, "error": str(e)}


# ============================================================
# DIAGNOSTICO
# ============================================================
def _try_local_api(port, path="/api/status", timeout=2):
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            headers={"User-Agent": "brn-node"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def do_status(cfg):
    print("=" * 64)
    print(f"  BRN Node — Diagnostico  (v{VERSION})")
    print("=" * 64)
    print()
    print(f"  Versao      : {VERSION}  ({BUILD_DATE})")
    print(f"  Config      : {cfg.source}")
    print(f"  Python      : {sys.version.split()[0]}")
    print(f"  CWD         : {os.getcwd()}")
    print()
    print(f"  Web port    : {cfg['web_port']}")
    print(f"  Explorer    : {cfg['explorer_port']}")
    print(f"  P2P port    : {cfg['p2p_port']}")
    print()
    print(f"  Read-only   : {cfg['read_only']}")
    print(f"  Headless    : {cfg['headless']}")
    print(f"  Miner       : {'ON' if cfg['miner_enabled'] else 'off'}")
    print(f"  UPnP        : {'ON' if cfg['upnp'] else 'off'}")
    print()
    gh = cfg['github']
    tok = gh.get('token') or ""
    print(f"  GitHub user : {gh.get('user') or '(nao configurado)'}")
    print(f"  GitHub repo : {gh.get('repo') or '(nao configurado)'}")
    print(f"  GitHub token: {'***' + tok[-4:] if tok else '(vazio)'}")
    print()
    print(f"  Tracker     : {cfg['tracker_url'] or '(desativado)'}")
    print()
    bs = cfg.get("bootstrap_peers", [])
    print(f"  Bootstrap   : {len(bs)} peer(s)")
    for p in bs[:5]:
        print(f"     - {p}")
    print()
    db_path = cfg["db_path"]
    print(f"  DB path     : {db_path}")
    if not os.path.exists(db_path):
        print("  DB status   : (nao existe ainda)")
    else:
        print(f"  DB size     : {os.path.getsize(db_path)/1024:.1f} KB")
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            h = conn.execute("SELECT MAX(height) FROM blocks").fetchone()[0]
            conn.close()
            print(f"  DB height   : {h if h is not None else '(vazio)'}")
        except Exception as e:
            print(f"  DB height   : erro ({e})")
    print()
    print(f"  Node ID     : ", end="")
    if NODE_ID_PATH.exists():
        print(f"{NODE_ID_PATH} ({os.path.getsize(NODE_ID_PATH)} bytes)")
    else:
        print("(nao criada ainda)")
    print()

    live = _try_local_api(cfg["web_port"])
    if live:
        print("  No rodando  : SIM")
        print(f"     Altura    : {live.get('height')}")
        print(f"     Peers     : {live.get('peers')}")
        print(f"     Mempool   : {live.get('mempool')}")
        print(f"     UTXOs     : {live.get('utxos')}")
        print(f"     Dificuldade: {live.get('difficulty')}")
    else:
        print(f"  No rodando  : nao (porta {cfg['web_port']} nao responde)")
    print()
    ms = _try_local_api(cfg["web_port"], "/api/miner/status")
    if ms:
        print(f"  Miner       : {'RODANDO' if ms.get('running') else 'parado'}")
        print(f"     Alvo     : {ms.get('address', '(nenhum)')}")
        print(f"     Blocos   : {ms.get('blocks_mined', 0)}")
    print()
    print("=" * 64)


# ============================================================
# v6: DIAGNOSTICO DE DESCOBERTA
# ============================================================
def _do_discover_diagnostic(cfg):
    import socket
    print("=" * 64)
    print("  BRN Discover Diagnostic")
    print("=" * 64)

    from discovery_v2 import get_all_local_ips, get_primary_ip, _load_manual_peers

    ips = get_all_local_ips()
    print(f"\n  IPs locais detectados:")
    for ip in ips:
        print(f"    - {ip}")
    if not ips:
        print("    (nenhum! problema de rede ou firewall local)")

    print(f"\n  Peers manuais: {_load_manual_peers() or '(nenhum)'}")

    tracker = os.environ.get("BRN_TRACKER", "")
    print(f"  Tracker       : {tracker or '(nao configurado)'}")

    secret = os.environ.get("BRN_NETWORK_SECRET", "brunocoin-lan-2026")
    import hashlib
    token = hashlib.sha256(secret.encode()).hexdigest()[:8]
    print(f"  Network token : {token}  (deve ser igual em todos os PCs)")

    p2p_port = cfg["p2p_port"]
    print(f"\n  Porta P2P TCP {p2p_port}: ", end="")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(("127.0.0.1", p2p_port))
        print("ABERTA localmente")
    except Exception as e:
        print(f"fechada ({e})")
    finally:
        s.close()

    print(f"\n  Escutando multicast + broadcast por 15s...")
    print(f"  (Abra o no em outro PC da rede AGORA para testar)")
    from discovery_v2 import UDPDiscovery
    found = []
    def on_peer(ip, port):
        found.append(f"{ip}:{port}")
        print(f"    [achou] {ip}:{port}")

    stop_flag = [False]
    udp = UDPDiscovery(p2p_port, "diag", on_peer, lambda: not stop_flag[0])
    udp.start()
    time.sleep(15)
    stop_flag[0] = True
    udp.stop()

    print(f"\n  Resultado: {len(found)} peer(s) descoberto(s).")
    if not found:
        print("\n  Nenhum peer descoberto. Verifique:")
        print("    1. Mesmo BRN_NETWORK_SECRET em ambos os PCs")
        print("    2. Firewall permitindo UDP 50007 e TCP " + str(p2p_port))
        print("    3. Mesmo Wi-Fi/LAN (sem client isolation)")
        print("    4. Modo de rede = Privada no Windows")
        print("    5. BRN_TRACKER apontando para o servidor do tracker")
    print("=" * 64)


# ============================================================
# THREADS
# ============================================================
def run_http():
    from server import app as http_app
    log = get_logger("http")
    port = int(os.environ.get("BRN_WEB_PORT", "5000"))
    log.info(f"HTTP         http://0.0.0.0:{port}")
    http_app.run(host="0.0.0.0", port=port,
                 threaded=True, debug=False, use_reloader=False)


def run_explorer():
    from explorer import app as explorer_app
    log = get_logger("explorer")
    port = int(os.environ.get("BRN_EXPLORER_PORT", "8080"))
    log.info(f"Explorer     http://0.0.0.0:{port}")
    explorer_app.run(host="0.0.0.0", port=port,
                     threaded=True, debug=False, use_reloader=False)


def run_status_loop(chain, p2p):
    log = get_logger("status")
    while not _shutdown.is_set():
        time.sleep(30)
        try:
            peers = p2p.get_status()
            log.info(
                f"Altura={chain.db.height()} "
                f"Peers={peers.get('peer_count', 0)} "
                f"Mempool={len(chain.db.all_mempool(limit=1000))} "
                f"UTXOs={chain.db.count_utxos()}"
            )
        except Exception:
            pass


def run_wallet_main_thread():
    try:
        if not os.environ.get("BRN_WEB_PASS"):
            get_logger("wallet").error(
                "BRN_WEB_PASS nao definida. Defina a senha da carteira "
                "como variavel de ambiente antes de iniciar o no:\n"
                "  export BRN_WEB_PASS='sua-senha-forte'   (Linux/macOS)\n"
                "  $env:BRN_WEB_PASS='sua-senha-forte'     (PowerShell)"
            )
            return

        from app_wallet_v3 import WalletApi
        import webview

        log = get_logger("wallet")
        index_path = Path(__file__).parent / "index_wallet.html"
        if not index_path.exists():
            log.error(f"index_wallet.html nao encontrado: {index_path}")
            return

        api = WalletApi()
        log.info("Abrindo janela desktop (main thread)")
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
        get_logger("wallet").error(f"Falha: {e}")


# ============================================================
# MAIN
# ============================================================
def main():
    args = parse_args()
    cfg = Config(args.config)

    if args.read_only:
        cfg.data["read_only"] = True
    if args.headless:
        cfg.data["headless"] = True
    if args.log_level:
        cfg.data["log_level"] = args.log_level
    if args.log_file:
        cfg.data["log_file"] = args.log_file

    cfg.apply_to_env()

    log = setup_logger(
        level=cfg["log_level"],
        log_file=cfg["log_file"] or None,
    )
    log.info("=" * 60)
    log.info(f"  BRN Node v{VERSION} ({BUILD_DATE})")
    log.info(f"  Config: {cfg.source}")
    log.info("=" * 60)

    if args.version:
        print(VERSION)
        return

    if args.status:
        do_status(cfg)
        return

    if args.discover:
        _do_discover_diagnostic(cfg)
        return

    if args.check_update:
        r = check_for_update()
        if not r.get("ok"):
            print(f"Falha: {r.get('error')}")
            return
        print(f"Local : {r['local']}")
        print(f"Remoto: {r['remote']}")
        if r["update_available"]:
            print(f"\n>>> ATUALIZACAO DISPONIVEL: {r['url']}")
            if r.get("notes"):
                print(f"Notas: {r['notes']}")
        else:
            print("\nVoce esta na ultima versao.")
        return

    # Auto-check silencioso
    try:
        upd = check_for_update(timeout=3)
        if upd.get("ok") and upd.get("update_available"):
            log.warning(f"Versao nova: {upd['remote']} (voce tem {upd['local']}) -> {upd['url']}")
    except Exception:
        pass

    # ============================================================
    # v6: IDENTIDADE Ed25519 ANTES DO P2P
    # ============================================================
    node_password = _resolve_password(args)
    try:
        node_id_priv, node_id_pub = load_or_create_node_identity(
            node_password, rotate=args.rotate_node_id
        )
    finally:
        try:
            del node_password
        except Exception:
            pass

    log.info(f"No ID (pubkey): {node_id_pub}")

    from blockchain import Blockchain
    from p2p_unified import P2PManager

    db_path = cfg["db_path"]
    log.info(f"Abrindo DB: {db_path}")
    chain = Blockchain(db_path)
    log.info(f"Altura atual: {chain.db.height()}")

    # v6: P2PManager recebe node_id_priv (2o argumento posicional)
    p2p = P2PManager(
        chain,
        node_id_priv,
        tcp_port=cfg["p2p_port"],
        enable_upnp=cfg["upnp"],
    )
    p2p.start()
    log.info(f"P2P porta {cfg['p2p_port']} | No ID: {node_id_pub[:16]}...")

    threading.Thread(target=run_http,     daemon=True, name="HTTP").start()
    threading.Thread(target=run_explorer, daemon=True, name="Explorer").start()

    try:
        from miner_loop import get_miner
        get_miner(chain)
        log.info("Miner: aguardando botao 'Iniciar Mineracao' na carteira")
    except ImportError:
        log.warning("miner_loop.py nao encontrado")
    except Exception as e:
        log.error(f"Miner erro: {e}")

    threading.Thread(target=run_status_loop, args=(chain, p2p),
                     daemon=True, name="StatusLoop").start()

    log.info("No pronto. Ctrl+C para encerrar.")

    use_wallet = not cfg["headless"]
    if use_wallet:
        try:
            run_wallet_main_thread()
        except KeyboardInterrupt:
            pass
    else:
        log.info("Modo headless (sem GUI)")
        try:
            while not _shutdown.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    log.info("Encerrando...")
    _shutdown.set()
    try:
        p2p.stop()
    except Exception:
        pass
    try:
        chain.db.close()
    except Exception:
        pass
    log.info("Ate logo.")


def _on_signal(signum, frame):
    _shutdown.set()


if __name__ == "__main__":
    signal.signal(signal.SIGINT,  _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    main()