"""
verify.py — Verifica se os arquivos essenciais estao na versao v6/v7.
Nao modifica nada. So reporta.
Uso: python verify.py
"""
import sys, os, inspect, importlib
from pathlib import Path

BASE = Path(__file__).parent

# --- Configuracao esperada ---
CHECKS = [
    # (arquivo,          imports_esperados,                          minimo_bytes)
    ("crypto.py",        ["Ed25519PrivateKey", "pubkey_to_address"], 3000),
    ("secure_store.py",  ["encrypt_blob", "decrypt_blob"],           3000),
    ("p2p_secure.py",    ["handshake_client", "handshake_server"],   3000),
    ("blockchain.py",    ["Blockchain", "txid", "signing_hash",
                          "_tx_core", "_CORE_V"],                    6000),
    ("chain_validator.py",["verify_chain", "_verify_tx_signature"],  3000),
    ("wallet.py",        ["Wallet", "WalletManager"],                4000),
    ("db.py",            ["ChainDB"],                                4000),
    ("miner_loop.py",    ["get_miner", "Miner"],                     2000),
    ("main.py",          None,                                       4000),
    ("server.py",        None,                                       1000),
    ("bech32.py",        ["address_from_pubkey"],                    500),
]

# --- Variaveis de ambiente ---
ENV_CHECKS = [
    ("BRN_TRACKER",        "IP do tracker (ex: http://192.168.0.10:8000)"),
    ("BRN_NETWORK_SECRET", "frase longa, IGUAL em todos os PCs"),
    ("BRN_NODE_PASSWORD",  "senha do no (por PC)"),
    ("BRN_WEB_PASS",       "senha da carteira"),
]

def line():
    print("-" * 60)

def main():
    print("=" * 60)
    print("  BRN — Verificacao de arquivos e ambiente")
    print("=" * 60)
    print()

    erros = 0
    avisos = 0

    # 1) Arquivos no disco
    print("Arquivos:")
    for nome, imports, min_bytes in CHECKS:
        p = BASE / nome
        if not p.exists():
            print(f"  [X] {nome:<22} NAO EXISTE")
            erros += 1
            continue
        size = p.stat().st_size
        if size < min_bytes:
            print(f"  [!] {nome:<22} {size} bytes (esperado >={min_bytes})")
            avisos += 1
        else:
            print(f"  [ok] {nome:<22} {size} bytes")
    print()

    # 2) Imports e simbolos
    print("Conteudo dos modulos:")
    for nome, imports, _ in CHECKS:
        if not imports:
            continue
        modname = nome[:-3]
        try:
            mod = importlib.import_module(modname)
        except ImportError as e:
            print(f"  [X] {modname:<22} ImportError: {e}")
            erros += 1
            continue
        except Exception as e:
            print(f"  [X] {modname:<22} {type(e).__name__}: {e}")
            erros += 1
            continue

        faltando = [a for a in imports if not hasattr(mod, a)]
        if faltando:
            print(f"  [X] {modname:<22} faltando: {faltando}")
            erros += 1
        else:
            print(f"  [ok] {modname:<22} expoe {imports}")
    print()

    # 3) blockchain tem auto_genesis?
    try:
        import blockchain
        sig = inspect.signature(blockchain.Blockchain.__init__)
        params = list(sig.parameters)
        if "auto_genesis" not in params:
            print("  [X] Blockchain.__init__ NAO tem auto_genesis")
            print(f"       parametros atuais: {params}")
            erros += 1
        else:
            print("  [ok] Blockchain.__init__ aceita auto_genesis")
    except Exception as e:
        print(f"  [X] nao consegui inspecionar Blockchain: {e}")
        erros += 1
    print()

    # 4) Env vars
    print("Variaveis de ambiente:")
    for var, desc in ENV_CHECKS:
        val = os.environ.get(var, "")
        if val:
            mostra = val if var == "BRN_TRACKER" else f"{val[:4]}..."
            print(f"  [ok] {var:<22} = {mostra}")
        else:
            print(f"  [!] {var:<22} NAO definida — {desc}")
            avisos += 1
    print()

    # 5) Resumo
    line()
    if erros == 0 and avisos == 0:
        print("  TUDO OK — pode rodar: python main.py --headless")
    elif erros == 0:
        print(f"  OK com {avisos} aviso(s)")
    else:
        print(f"  {erros} erro(s), {avisos} aviso(s) — corrija antes de rodar")
    line()
    return 1 if erros else 0

if __name__ == "__main__":
    sys.exit(main())