"""
network_guard.py — Bloqueia mineracao se nao houver outro no na rede.
Garante que nao exista cadeia paralela: 1 no sozinho nunca minera.
"""
import os
import time

# Se BRN_ALLOW_SOLO_MINING=1, permite minerar sozinho (modo dev).
# Padrao: 0 -> NUNCA minera sozinho, espera outro no.
ALLOW_SOLO = os.environ.get("BRN_ALLOW_SOLO_MINING", "0") == "1"

# Tempo minimo (seg) que um peer precisa estar visivel antes de liberar mineracao.
# Evita minerar no instante em que um peer aparece e some.
MIN_PEER_STABLE = int(os.environ.get("BRN_MIN_PEER_STABLE", "10"))

# Estado interno: quando vimos o primeiro peer
_first_peer_ts = None


def _log(msg):
    try:
        from brn_logger import log as _l
        _l.info(msg)
    except Exception:
        print(f"[NetGuard] {msg}", flush=True)


def reset():
    """Chamar quando p2p inicia."""
    global _first_peer_ts
    _first_peer_ts = None


def pode_minerar(p2p) -> tuple:
    """
    Retorna (pode: bool, motivo: str).
    - ALLOW_SOLO=True: sempre pode (modo dev)
    - ALLOW_SOLO=False: so pode se tiver peer estavel
    """
    global _first_peer_ts

    if ALLOW_SOLO:
        return True, "BRN_ALLOW_SOLO_MINING=1 (dev mode)"

    # conta peers ativos agora
    peers = []
    try:
        peers = p2p.discovery.listar_peers()
    except Exception:
        peers = []

    n = len(peers)

    if n == 0:
        _first_peer_ts = None
        return False, "sem peers — aguardando outro no entrar na rede"

    # tem peer: espera estabilizar
    if _first_peer_ts is None:
        _first_peer_ts = time.time()
        return False, f"peer detectado, aguardando estabilizar ({MIN_PEER_STABLE}s)"

    elapsed = time.time() - _first_peer_ts
    if elapsed < MIN_PEER_STABLE:
        return False, f"estabilizando peer ({int(elapsed)}/{MIN_PEER_STABLE}s)"

    return True, f"{n} peer(s) estavel(is)"