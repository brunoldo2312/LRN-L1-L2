"""bridge/config.py — Configuração da bridge BRN <-> BTC.
Tudo vem de variáveis de ambiente, com defaults sensatos.
"""
import os


def _env(k, d=""):
    return (os.environ.get(k, d) or "").strip()


def _env_int(k, d=0):
    try:
        return int(os.environ.get(k, str(d)))
    except (ValueError, TypeError):
        return d


def _env_float(k, d=0.0):
    try:
        return float(os.environ.get(k, str(d)))
    except (ValueError, TypeError):
        return d


def _env_bool(k, d=False):
    v = (os.environ.get(k, str(d)) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


# ============================================================
# REDE BTC
# ============================================================
BRIDGE_NET = _env("BRN_BRIDGE_NET", "mainnet").lower()

if BRIDGE_NET == "testnet":
    BTC_NETWORK = "testnet"
    BTC_RPC = _env("BRN_BRIDGE_BTC_RPC", "https://blockstream.info/testnet/api")
    _BTC_PREFIXES = ("tb1", "m", "n", "2")
else:
    BTC_NETWORK = "bitcoin"
    BTC_RPC = _env("BRN_BRIDGE_BTC_RPC", "https://blockstream.info/api")
    _BTC_PREFIXES = ("bc1", "1", "3")


# ============================================================
# ENDEREÇOS E CHAVES
# ============================================================
# Cofre BRN — onde usuários enviam BRN para receber BTC
ENDERECO_BRIDGE_LRN = _env("BRN_BRIDGE_COFRE_BRN", "")

# Cofre BTC — recebe BTC (onramp) e envia BTC (off-ramp)
ENDERECO_COFRE_BTC = _env("BRN_BRIDGE_COFRE_BTC", "")

# WIF privada do cofre BTC — necessária apenas para SAQUES (BRN → BTC)
CHAVE_PRIVADA_BTC = _env("BRN_BRIDGE_WIF", "")


# ============================================================
# COTAÇÃO E TAXAS
# ============================================================
# 1 BTC = quantos BRN
BRN_POR_BTC = _env_float("BRN_BRIDGE_RATE", 100_000.0)

# Taxa da bridge em basis points (100 bps = 1%)
TAXA_BRIDGE_BPS = _env_int("BRN_BRIDGE_FEE_BPS", 100)

# Taxa de rede BTC por transação (sats)
TAXA_REDE_BTC_SATS = _env_int("BRN_BRIDGE_NET_FEE_SAT", 500)


# ============================================================
# LIMITES
# ============================================================
MIN_DEPOSITO_BRN_SATS = _env_int("BRN_BRIDGE_MIN_DEP_SAT", 10 * 10**8)  # 10 BRN
MIN_SAQUE_SATS = _env_int("BRN_BRIDGE_MIN_SAQ_SAT", 10_000)             # 0.0001 BTC
RESERVA_MINIMA_SATS = _env_int("BRN_BRIDGE_RESERVA_SAT", 50_000)
LIMITE_DIARIO_SATS = _env_int("BRN_BRIDGE_LIMITE_SAT", 5_000_000)

CONFIRMACOES_LRN = _env_int("BRN_BRIDGE_CONF_BRN", 1)
CONFIRMACOES_BTC = _env_int("BRN_BRIDGE_CONF_BTC", 1)


# ============================================================
# PERSISTÊNCIA
# ============================================================
DB_BRIDGE_PATH = _env("BRN_BRIDGE_DB", "bridge_state.db")
DEBUG = _env_bool("BRN_BRIDGE_DEBUG", True)


# ============================================================
# VALIDAÇÃO
# ============================================================
def validar_config():
    erros = []
    if not ENDERECO_BRIDGE_LRN:
        erros.append("ENDERECO_BRIDGE_LRN vazio (defina BRN_BRIDGE_COFRE_BRN)")
    if not ENDERECO_COFRE_BTC:
        erros.append("ENDERECO_COFRE_BTC vazio (defina BRN_BRIDGE_COFRE_BTC)")
    elif not any(ENDERECO_COFRE_BTC.startswith(p) for p in _BTC_PREFIXES):
        erros.append(f"ENDERECO_COFRE_BTC não parece {BTC_NETWORK}")
    if not CHAVE_PRIVADA_BTC:
        erros.append("CHAVE_PRIVADA_BTC vazia (BRN_BRIDGE_WIF) — saques desabilitados")
    if BRN_POR_BTC <= 0:
        erros.append("BRN_POR_BTC deve ser > 0")
    return erros
