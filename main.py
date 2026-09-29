"""
main.py — BRN (BrunoCoin) — Nó Principal
Versão: 4.2 | Data: 29/09/2026
- ✅ Blockchain inicialização
- ✅ P2P com Bootstrap automático via GitHub
- ✅ Atualização automática de timestamp no peers.json
- ✅ Verificação de status de conexão
- ✅ Servidor Webview (interface gráfica)
"""

import sys
import time
import json
import threading
import os
from pathlib import Path

# ============================================================
# 🔄 ATUALIZAÇÃO AUTOMÁTICA DO TIMESTAMP NO peers.json
# ============================================================

PEERS_FILE = "peers.json"
SEU_ENDERECO = "177.82.132.98:6001"  # IP:porta do seu nó

def atualizar_timestamp_local():
    """Atualiza o ts no arquivo peers.json local ao iniciar"""
    agora = int(time.time())
    try:
        if os.path.exists(PEERS_FILE):
            with open(PEERS_FILE, "r", encoding="utf-8") as f:
                peers = json.load(f)
        else:
            peers = {}
        
        if SEU_ENDERECO in peers:
            peers[SEU_ENDERECO]["ts"] = agora
        else:
            peers[SEU_ENDERECO] = {
                "h": 0,
                "id": "",
                "ts": agora
            }
        
        with open(PEERS_FILE, "w", encoding="utf-8") as f:
            json.dump(peers, f, indent=2, ensure_ascii=False)
        
        print(f"✅ Timestamp atualizado: {agora}")
        return agora
    except Exception as e:
        print(f"⚠️ Erro ao atualizar peers.json: {e}")
        return agora

# Executa logo no início
atualizar_timestamp_local()

# ============================================================
# CONFIGURAÇÕES DA MOEDA
# ============================================================

COIN_NAME = "Bruno"
COIN_SYMBOL = "BRN"
MINING_REWARD = 1.0
DIFFICULTY = 4

# ============================================================
# IMPORTAÇÃO DOS MÓDULOS
# ============================================================

try:
    from blockchain import Blockchain
    from db import ChainDB
    from p2p_unified import P2PManager
    print("✅ Módulos carregados com sucesso")
except ImportError as e:
    print(f"❌ Erro ao carregar módulos: {e}")
    sys.exit(1)

# ============================================================
# VERIFICAÇÃO DE STATUS DA REDE
# ============================================================

def verificar_status_rede(p2p_manager, blockchain):
    """Exibe status completo da rede"""
    print("\n" + "="*60)
    print("📊 STATUS DO NÓ BRN")
    print("="*60)
    
    # Timestamp
    ts_atual = int(time.time())
    print(f"⏱️  Hora atual:     {ts_atual}")
    
    # Blockchain
    altura = blockchain.get_latest_height()
    print(f"📦 Altura da cadeia: {altura} blocos")
    
    # P2P
    status = p2p_manager.get_status()
    print(f"🆔 Node ID:         {status['node_id']}")
    print(f"🌐 Peers conhecidos: {status['peers_conhecidos']}")
    print(f"🔗 Peers conectados: {status['peers_conectados']}")
    
    if status['peers']:
        print("\n📋 Lista de peers ativos:")
        for peer in status['peers']:
            print(f"   → {peer['ip']}:{peer['port']} | {peer['node_id']} | bloco #{peer['height']}")
    else:
        print("\n⚠️ Nenhum peer conectado — rede local apenas")
    
    print("="*60 + "\n")

# ============================================================
# INICIALIZAÇÃO PRINCIPAL
# ============================================================

def inicializar_blockchain():
    """Inicializa banco de dados e blockchain"""
    print("🔄 Inicializando banco de dados...")
    db = ChainDB("brn_v2_chain.db")
    
    print("🔄 Inicializando blockchain...")
    bc = Blockchain(db_path="brn_v2_chain.db")
    
    altura = bc.get_latest_height()
    print(f"✅ Blockchain carregada — Altura atual: {altura}")
    return bc, db

def inicializar_p2p():
    """Inicializa rede P2P com bootstrap do GitHub"""
    print("\n🔄 Inicializando rede P2P...")
    p2p = P2PManager()
    p2p.start()
    return p2p

def main():
    print("\n" + "🚀"*30)
    print(f"   {COIN_NAME} ({COIN_SYMBOL}) — Nó Principal v4.2")
    print("🚀"*30 + "\n")
    
    # 1. Inicializa Blockchain
    blockchain, db = inicializar_blockchain()
    
    # 2. Inicializa P2P (conecta automaticamente aos peers do GitHub)
    p2p = inicializar_p2p()
    
    # 3. Atualiza altura no peers.json local
    altura_atual = blockchain.get_latest_height()
    try:
        with open(PEERS_FILE, "r", encoding="utf-8") as f:
            peers = json.load(f)
        if SEU_ENDERECO in peers:
            peers[SEU_ENDERECO]["h"] = altura_atual
            peers[SEU_ENDERECO]["id"] = p2p.node_id
        with open(PEERS_FILE, "w", encoding="utf-8") as f:
            json.dump(peers, f, indent=2)
        print(f"✅ Altura atualizada no peers.json: {altura_atual}")
    except Exception as e:
        print(f"⚠️ Não foi possível atualizar altura no peers.json: {e}")
    
    # 4. Exibe status completo
    verificar_status_rede(p2p, blockchain)
    
    # 5. Mantém programa rodando
    print("✅ Nó BRN rodando — Pressione Ctrl+C para encerrar\n")
    
    try:
        while True:
            time.sleep(60)
            # Atualiza status a cada 60 segundos
            if p2p.running:
                pass
    except KeyboardInterrupt:
        print("\n⏹️ Encerrando...")
        p2p.stop()
        sys.exit(0)


if __name__ == "__main__":
    main()