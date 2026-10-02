"""
main.py — Entrypoint unificado do no BRN (v6.2)
============================================================
v6.2:
  - [SEGURANÇA] Le BRN_NETWORK_SECRET e passa para Blockchain
    como network_secret. Blocos passam a carregar auth_tag=HMAC(secret).
    Sem o segredo, nenhum bloco e aceito (mesmo com PoW valido).
  - Auto-gera segredo persistente se BRN_NETWORK_SECRET nao existir,
    gravando em network_secret.txt (0600) e avisando o usuario.

v6.1:
  - --client-mode: cliente NAO origina genesis, espera da rede.
  - Blockchain recebe auto_genesis=not args.client_mode.
  - Espera de sincronizacao no boot do cliente (timeout configuravel).
  - --discover: diagnostico de descoberta de peers.
  - Aviso se BRN_NETWORK_SECRET estiver no default.

v6.0 (breaking):
  - Identidade Ed25519 do no carregada/criada ANTES do P2P.
  - Senha do no: --password-file > --password > BRN_NODE_PASSWORD > prompt.
  - Removida senha hardcoded da carteira (BRN_WEB_PASS obrigatoria via env).
  - P2PManager recebe node_id_priv (handshake autenticado E2P).
  - --rotate-node-id para gerar nova identidade (backup automatico).
============================================================
"""
import os
import sys
import signal
import argparse
import hashlib
import secrets
import threading
import time
import json
import socket
import urllib.request
import urllib.error
from pathlib import Path

from brn_config import Config
from brn_logger import setup_logger, get_logger
from version import VERSION, BUILD_DATE, GITHUB_USER, GITHUB_REPO


_shutdown = threading.Event()

# ============================================================
# CAMINHOS
# ============================================================
BASE_DIR     = Path(__file__).parent.resolve()
NODE_ID_PATH = BASE_DIR / "node_identity.enc"
SECRET_PATH  = BASE_DIR / "network_secret.txt"

CLIENT_BOOT_TIMEOUT = int(os.environ.get("BRN_CLIENT_BOOT_TIMEOUT", "120"))


# ============================================================
# ARGUMENTOS
# ============================================================
def parse_args():
    p = argparse.ArgumentParser(prog="main.py", description="BRN Node v6")
    p.add_argument("--status", action="store_true",
                   help="Diagnostico completo e sai")
    p.add_argument("--headless", action="store_true",
                   help="Sem interface grafica")
    p.add_argument("--read-only", action="store_true",
                   help="Nao minera nem publica")
    p.add_argument("--version", action="store_true",
                   help="Mostra versao e sai")
    p.add_argument("--check-update", action="store_true",
                   help="Verifica versao nova")
    p.add_argument("--config", default="config.json",
                   help="Arquivo de configuracao")
    p.add_argument("--log-level", default=None,
                   help="DEBUG|INFO|WARNING|ERROR")
    p.add_argument("--log-file", default=None,
                   help="Log JSON neste arquivo")

    p.add_argument("--password", default=None,
                   help="Senha do no (identidade Ed25519). Prefira "
                        "--password-file ou BRN_NODE_PASSWORD.")
    p.add_argument("--password-file", default=None,
                   help="Le a senha do no deste arquivo (mais seguro).")
    p.add_argument("--rotate-node-id", action="store_true",
                   help="Gera nova identidade (backup automatico).")

    p.add_argument("--client-mode", action="store_true",
                   help="Nao origina genesis — espera receber da rede.")
    p.add_argument("--discover", action="store_true",
                   help="Diagnostico de descoberta de peers (15s) e sai")

    p.add_argument("--rotate-secret", action="store_true",
                   help="Gera um novo BRN_NETWORK_SECRET e grava em "
                        "network_secret.txt. TODOS os nos precisam do "
                        "novo segredo (rede para de aceitar blocos antigos).")
    return p.parse_args()


# ============================================================
# SEGREDO DA REDE (auth_tag dos blocos)
# ============================================================
def resolve_network_secret(args) -> str:
    log = get_logger("secret")

    if getattr(args, "rotate_secret", False):
        new = secrets.token_hex(32)
        try:
            fd = os.open(str(SECRET_PATH),
                         os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new)
                f.flush()
                os.fsync(f.fileno())
            log.warning(
                f"Novo segredo gerado e gravado em {SECRET_PATH}. "
                f"Copie para as outras maquinas antes de reiniciar."
            )
        except Exception as e:
            log.error(f"Falha ao gravar segredo: {e}")
        return new

    env = os.environ.get("BRN_NETWORK_SECRET", "").strip()
    if env:
        if not SECRET_PATH.exists():
            try:
                fd = os.open(str(SECRET_PATH),
                             os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(env)
            except Exception:
                pass
        return env

    if SECRET_PATH.exists():
        try:
            sec = SECRET_PATH.read_text(encoding="utf-8").strip()
            if sec:
                return sec
        except Exception as e:
            log.warning(f"Falha ao ler {SECRET_PATH}: {e}")

    new = secrets.token_hex(32)
    try:
        fd = os.open(str(SECRET_PATH),
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
            f.flush()
            os.fsync(f.fileno())
        log.warning(
            f"BRN_NETWORK_SECRET nao definido. Segredo novo gerado e "
            f"gravado em {SECRET_PATH}.\n"
            f"  Copie este arquivo (ou o conteudo) para TODAS as outras "
            f"maquinas da rede, senao elas nao vao aceitar seus blocos."
        )
    except Exception as e:
        log.error(f"Falha ao gravar segredo: {e}")
    return new


# ============================================================
# RESOLUCAO DE SENHA DO NO
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
# IDENTIDADE Ed25519
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
    print(f"  Base dir    : {BASE_DIR}")
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

    print("  Auth de blocos (BRN_NETWORK_SECRET):")
    sec_src = "?"
    sec_val = ""
    env_sec = os.environ.get("BRN_NETWORK_SECRET", "").strip()
    if env_sec:
        sec_val = env_sec
        sec_src = "env"
    elif SECRET_PATH.exists():
        try:
            sec_val = SECRET_PATH.read_text(encoding="utf-8").strip()
            sec_src = f"arquivo {SECRET_PATH.name}"
        except Exception:
            pass
    if sec_val:
        print(f"     Status : ATIVO ({sec_src})")
        print(f"     Fingerprint: {hashlib.sha256(sec_val.encode()).hexdigest()[:16]}...")
    else:
        print("     Status : INATIVO (nenhum segredo configurado)")
        print("     Rode com --rotate-secret para gerar um.")
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
            gen = conn.execute("SELECT hash FROM blocks WHERE height=0").fetchone()
            conn.close()
            print(f"  DB height   : {h if h is not None else '(vazio)'}")
            if gen and gen[0]:
                print(f"  DB genesis  : {gen[0][:24]}...")
        except Exception as e:
            print(f"  DB height   : erro ({e})")
    print()

    print(f"  Node ID     : ", end="")
    if NODE_ID_PATH.exists():
        print(f"{NODE_ID_PATH.name} ({os.path.getsize(NODE_ID_PATH)} bytes)")
    else:
        print("(nao criada ainda — sera criada no proximo boot)")
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


def _do_discover_diagnostic(cfg):
    print("=" * 64)
    print("  BRN Discover Diagnostic")
    print("=" * 64)

    try:
        from discovery_v2 import get_all_local_ips, get_primary_ip, _load_manual_peers
    except ImportError as e:
        print(f"  discovery_v2 nao disponivel: {e}")
        return

    ips = get_all_local_ips()
    print(f"\n  IPs locais detectados:")
    for ip in ips:
        print(f"    - {ip}")
    if not ips:
        print("    (nenhum! problema de rede ou firewall local)")

    print(f"\n  Peers manuais: {_load_manual_peers() or '(nenhum)'}")

    tracker = os.environ.get("BRN_TRACKER", "")
    print(f"  Tracker       : {tracker or '(nao configurado)'}")

    secret = os.environ.get("BRN_NETWORK_SECRET", "")
    if not secret and SECRET_PATH.exists():
        try:
            secret = SECRET_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    token = hashlib.sha256(secret.encode()).hexdigest()[:8] if secret else "(vazio)"
    print(f"  Network token : {token}")

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

    found = []
    def on_peer(ip, port):
        found.append(f"{ip}:{port}")
        print(f"    [achou] {ip}:{port}")

    stop_flag = [False]
    try:
        from discovery_v2 import UDPDiscovery
        udp = UDPDiscovery(p2p_port, "diag", on_peer, lambda: not stop_flag[0])
        udp.start()
        time.sleep(15)
        stop_flag[0] = True
        udp.stop()
    except Exception as e:
        print(f"  falha ao iniciar UDPDiscovery: {e}")

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
        index_path = BASE_DIR / "index_wallet.html"
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
    log.info(f"  Modo  : {'CLIENTE (nao origina genesis)' if args.client_mode else 'ORIGEM'}")
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

    try:
        upd = check_for_update(timeout=3)
        if upd.get("ok") and upd.get("update_available"):
            log.warning(f"Versao nova: {upd['remote']} (voce tem {upd['local']}) -> {upd['url']}")
    except Exception:
        pass

    # ============================================================
    # v6.2 — Segredo da rede (auth_tag dos blocos)
    # ============================================================
    network_secret = resolve_network_secret(args)
    fingerprint = hashlib.sha256(network_secret.encode()).hexdigest()[:16]
    log.info(f"Auth de blocos: ATIVO (fingerprint {fingerprint}...)")
    os.environ["BRN_NETWORK_SECRET"] = network_secret

    # ============================================================
    # Identidade Ed25519
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

    # ============================================================
    # Blockchain
    # ============================================================
    from blockchain import Blockchain
    from p2p_unified import P2PManager

    db_path = cfg["db_path"]
    log.info(f"Abrindo DB: {db_path}")

    chain = Blockchain(
        db_path,
        auto_genesis=not args.client_mode,
        network_secret=network_secret,
    )

    if args.client_mode:
        if chain.db.height() < 0:
            log.info("Cliente: DB vazio. Aguardando genesis da rede...")
        else:
            gen_row = None
            try:
                gen_row = chain.db.conn.execute(
                    "SELECT hash FROM blocks WHERE height=0"
                ).fetchone()
            except Exception:
                pass
            gen_hash = gen_row[0][:16] if gen_row and gen_row[0] else "?"
            log.info(
                f"Cliente: DB ja tem altura {chain.db.height()} "
                f"(genesis {gen_hash}...)"
            )

    log.info(f"Altura atual: {chain.db.height()}")

    # ============================================================
    # P2P
    # ============================================================
    p2p = P2PManager(
        chain,
        node_id_priv,
        tcp_port=cfg["p2p_port"],
        enable_upnp=cfg["upnp"],
    )
    p2p.start()
    log.info(f"P2P porta {cfg['p2p_port']} | No ID: {node_id_pub[:16]}...")

    # ============================================================
    # HTTP + Explorer
    # ============================================================
    threading.Thread(target=run_http,     daemon=True, name="HTTP").start()
    threading.Thread(target=run_explorer, daemon=True, name="Explorer").start()

    # ============================================================
    # Miner
    # ============================================================
    if chain.db.height() >= 0:
        try:
            from miner_loop import get_miner
            get_miner(chain)
            log.info("Miner: aguardando botao 'Iniciar Mineracao' na carteira")
        except ImportError:
            log.warning("miner_loop.py nao encontrado")
        except Exception as e:
            log.error(f"Miner erro: {e}")
    else:
        log.info("Cliente: DB sem genesis — miner bloqueado ate sincronizar")

    threading.Thread(target=run_status_loop, args=(chain, p2p),
                     daemon=True, name="StatusLoop").start()

    log.info("No pronto. Ctrl+C para encerrar.")

    # ============================================================
    # Cliente sem genesis
    # ============================================================
    if args.client_mode and chain.db.height() < 0:
        log.info(
            f"Aguardando genesis de um peer (timeout {CLIENT_BOOT_TIMEOUT}s)..."
        )
        t0 = time.time()
        while chain.db.height() < 0 and (time.time() - t0) < CLIENT_BOOT_TIMEOUT:
            if _shutdown.is_set():
                break
            time.sleep(1)

        if chain.db.height() < 0:
            log.warning(
                "TIMEOUT esperando genesis. O no continua rodando em modo "
                "sincronizacao (nao minera) ate um peer enviar o bloco 0.\n"
                "Verifique:\n"
                "  - BRN_TRACKER aponta para o servidor do tracker\n"
                "  - O no de origem (A) esta online e anunciando\n"
                "  - BRN_NETWORK_SECRET identico entre A e B\n"
                "  - Firewall libera TCP 6001 e UDP 50007"
            )
        else:
            log.info(f"Genesis recebido! Altura: {chain.db.height()}")
            try:
                from miner_loop import get_miner
                get_miner(chain)
                log.info("Miner reinicializado apos sync do genesis")
            except Exception as e:
                log.warning(f"Nao foi possivel reiniciar miner: {e}")

    # ============================================================
    # Loop principal
    # ============================================================
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
