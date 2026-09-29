"""
update_peers.py — Atualiza automaticamente o timestamp do nó local
no arquivo peers.json e opcionalmente envia para o GitHub
"""

import json
import time
import os
from pathlib import Path

# Configurações
PEERS_FILE = "peers.json"
SEU_ENDERECO = "177.82.132.98:6001"  # IP:porta do seu nó


def update_local_timestamp():
    """Atualiza o ts no arquivo peers.json local"""
    agora = int(time.time())
    
    # Carrega arquivo existente
    if os.path.exists(PEERS_FILE):
        with open(PEERS_FILE, "r", encoding="utf-8") as f:
            peers = json.load(f)
    else:
        peers = {}
    
    # Atualiza ou adiciona seu nó
    if SEU_ENDERECO in peers:
        peers[SEU_ENDERECO]["ts"] = agora
    else:
        peers[SEU_ENDERECO] = {
            "h": 0,  # Será atualizado pelo main.py
            "id": "",
            "ts": agora
        }
    
    # Salva
    with open(PEERS_FILE, "w", encoding="utf-8") as f:
        json.dump(peers, f, indent=2, ensure_ascii=False)
    
    print(f"✅ Timestamp atualizado: {agora}")
    return agora


if __name__ == "__main__":
    update_local_timestamp()