"""
p2p_unified.py — P2P BRN com Bootstrap via GitHub
Versão: 2.1 | Data: 29/09/2026
- ✅ Baixa lista de peers do GitHub automaticamente
- ✅ Fallback para arquivo local se sem internet
- ✅ Filtra peers ativos (atualizados na última hora)
- ✅ Conecta automaticamente ao iniciar
"""

import socket
import threading
import time
import json
import requests
import uuid
from typing import Dict, Set, List, Tuple, Optional
from dataclasses import dataclass, field


# ============================================================
# CONFIGURAÇÕES
# ============================================================

# 📡 URL do seu repositório GitHub com a lista de peers
PEERS_URL = "https://raw.githubusercontent.com/brunoldo2312/peers.json/main/peers.json"
PEERS_LOCAL_FILE = "peers.json"  # Fallback local
P2P_PORT = 6001
MULTICAST_GROUP = "239.255.42.99"
MULTICAST_PORT = 50007
PEER_TIMEOUT = 3600  # 1 hora sem atualizar = considerado inativo
BOOTSTRAP_INTERVAL = 300  # Tenta reconectar a cada 5 minutos


# ============================================================
# ESTRUTURA DE DADOS
# ============================================================

@dataclass
class Peer:
    ip: str
    port: int
    node_id: str
    height: int = 0
    last_seen: float = field(default_factory=time.time)
    connected: bool = False
    work: int = 0


# ============================================================
# GERENCIADOR P2P
# ============================================================

class P2PManager:
    def __init__(self, local_ip: str = "0.0.0.0"):
        self.node_id = str(uuid.uuid4())[:8]
        self.local_ip = local_ip
        self.peers: Dict[str, Peer] = {}
        self.connected_peers: Set[Tuple[str, int]] = set()
        self.running = False
        
        # Sockets
        self.tcp_server: Optional[socket.socket] = None
        self.udp_discovery: Optional[socket.socket] = None
        
        # Threads
        self._threads: List[threading.Thread] = []
        self._bootstrap_lock = threading.Lock()
        
        print(f"[P2P] Meu Node ID: {self.node_id}")

    # ========================================================
    # 🔄 SISTEMA DE BOOTSTRAP VIA GITHUB
    # ========================================================

    def fetch_peers_from_github(self) -> Dict[str, dict]:
        """
        Baixa a lista de nós conhecidos do GitHub
        Retorna dicionário vazio se falhar
        """
        try:
            print(f"[Bootstrap] Baixando lista de peers do GitHub...")
            resp = requests.get(PEERS_URL, timeout=15)
            resp.raise_for_status()
            peers = resp.json()
            print(f"[Bootstrap] ✅ {len(peers)} nó(s) encontrado(s) no GitHub")
            return peers
        except requests.exceptions.ConnectionError:
            print(f"[Bootstrap] ⚠️ Sem conexão com a internet")
            return {}
        except Exception as e:
            print(f"[Bootstrap] ⚠️ Falha ao baixar: {e}")
            return {}

    def load_peers_from_local(self) -> Dict[str, dict]:
        """Carrega lista de peers do arquivo local"""
        try:
            with open(PEERS_LOCAL_FILE, "r", encoding="utf-8") as f:
                peers = json.load(f)
                print(f"[Bootstrap] ✅ {len(peers)} nó(s) carregados do arquivo local")
                return peers
        except FileNotFoundError:
            print(f"[Bootstrap] ⚠️ Arquivo {PEERS_LOCAL_FILE} não encontrado")
            return {}
        except Exception as e:
            print(f"[Bootstrap] ⚠️ Erro ao ler arquivo local: {e}")
            return {}

    def get_bootstrap_peers(self) -> List[Tuple[str, int, dict]]:
        """
        Obtém lista de peers para se conectar:
        1. Tenta baixar do GitHub primeiro
        2. Se falhar, usa arquivo local como fallback
        Filtra apenas peers ativos (atualizados na última hora)
        """
        # Tenta GitHub primeiro
        peers = self.fetch_peers_from_github()
        
        # Fallback para arquivo local
        if not peers:
            peers = self.load_peers_from_local()
        
        if not peers:
            print(f"[Bootstrap] ❌ Nenhum peer encontrado")
            return []
        
        # Filtra apenas peers ativos
        agora = time.time()
        ativos = []
        for endereco, info in peers.items():
            try:
                ip, porta_str = endereco.rsplit(":", 1)
                porta = int(porta_str)
                ts = info.get("ts", 0)
                
                # Verifica se está atualizado
                if agora - ts < PEER_TIMEOUT:
                    ativos.append((ip, porta, info))
                else:
                    print(f"[Bootstrap] ⏳ Ignorando {endereco} — desatualizado")
            except Exception as e:
                print(f"[Bootstrap] ⚠️ Formato inválido: {endereco} — {e}")
        
        print(f"[Bootstrap] 🎯 {len(ativos)} nó(s) ativo(s) para conectar")
        return ativos

    def connect_to_bootstrap_peers(self) -> int:
        """Conecta a todos os peers de bootstrap encontrados"""
        with self._bootstrap_lock:
            peers = self.get_bootstrap_peers()
            conectados = 0
            
            for ip, porta, info in peers:
                # Não conecta a si mesmo
                if ip == self.local_ip and porta == P2P_PORT:
                    continue
                
                # Não reconecta se já está conectado
                if (ip, porta) in self.connected_peers:
                    continue
                
                print(f"[Bootstrap] Tentando: {ip}:{porta}")
                if self.connect_to_peer(ip, porta):
                    conectados += 1
                    # Registra informações do peer
                    peer_id = info.get("id", "???????")
                    altura = info.get("h", 0)
                    if peer_id in self.peers:
                        self.peers[peer_id].height = altura
                        self.peers[peer_id].work = altura  # Simplificado
                    print(f"[Bootstrap] ✅ Conectado a {ip}:{porta} ({peer_id})")
            
            if conectados == 0 and not peers:
                print(f"[Bootstrap] ℹ️ Sem peers externos — rede local apenas")
            
            return conectados

    def update_peer_list_online(self):
        """
        Envia informações deste nó para o servidor (opcional)
        Em um sistema totalmente P2P, você pode atualizar o peers.json manualmente no GitHub
        ou usar um serviço simples como o Gist
        """
        # Esta função é apenas informativa — você atualiza o peers.json no GitHub
        pass

    # ========================================================
    # CONEXÃO E DESCOBERTA
    # ========================================================

    def start(self):
        """Inicia todos os subsistemas P2P"""
        self.running = True
        print("[P2P] Iniciando...")
        
        # Inicializa servidor TCP
        self.start_tcp_server()
        
        # Inicializa descoberta UDP
        self.start_discovery()
        
        # 🔄 CONECTA AOS PEERS DO GITHUB AO INICIAR
        print("\n" + "="*50)
        print("🔍 FASE DE BOOTSTRAP — Conectando à rede BRN")
        print("="*50)
        self.connect_to_bootstrap_peers()
        print("="*50 + "\n")
        
        # Inicia thread de reconexão periódica
        threading.Thread(target=self._auto_reconnect_loop, daemon=True).start()
        
        print("[P2P] ✅ Pronto")

    def start_tcp_server(self):
        """Inicia servidor TCP para conexões de outros nós"""
        self.tcp_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp_server.bind(("0.0.0.0", P2P_PORT))
        self.tcp_server.listen(5)
        print(f"[P2P] Servidor TCP escutando na porta {P2P_PORT}")
        
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def start_discovery(self):
        """Inicia descoberta via multicast UDP"""
        self.udp_discovery = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_discovery.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp_discovery.bind(("", MULTICAST_PORT))
        
        # Entra no grupo multicast
        import struct
        mreq = struct.pack("4sl", socket.inet_aton(MULTICAST_GROUP), socket.INADDR_ANY)
        self.udp_discovery.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        
        print(f"[Discovery] Escutando {MULTICAST_GROUP}:{MULTICAST_PORT}")
        threading.Thread(target=self._discovery_loop, daemon=True).start()
        threading.Thread(target=self._announce_loop, daemon=True).start()

    def connect_to_peer(self, ip: str, port: int) -> bool:
        """Estabelece conexão TCP com um peer"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10)
            sock.connect((ip, port))
            
            # Apresenta-se
            hello = json.dumps({
                "type": "hello",
                "node_id": self.node_id,
                "port": P2P_PORT
            })
            sock.send((hello + "\n").encode())
            
            # Resposta
            data = sock.recv(4096).decode().strip()
            if not data:
                return False
            
            msg = json.loads(data)
            remote_id = msg.get("node_id", "???????")
            
            # Registra peer
            self.peers[remote_id] = Peer(
                ip=ip,
                port=port,
                node_id=remote_id,
                last_seen=time.time()
            )
            self.connected_peers.add((ip, port))
            
            sock.close()
            return True
            
        except Exception as e:
            # print(f"[P2P] Falha ao conectar em {ip}:{porta}: {e}")
            return False

    def send_to_peer(self, ip: str, port: int, message: str, timeout: int = 5) -> Optional[str]:
        """Envia mensagem e aguarda resposta"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((ip, port))
            sock.send((message + "\n").encode())
            
            resp = sock.recv(8192).decode().strip()
            sock.close()
            return resp
        except Exception:
            return None

    # ========================================================
    # LOOPs INTERNOS
    # ========================================================

    def _accept_loop(self):
        """Aceita conexões TCP de entrada"""
        while self.running:
            try:
                conn, addr = self.tcp_server.accept()
                threading.Thread(
                    target=self._handle_connection,
                    args=(conn, addr),
                    daemon=True
                ).start()
            except Exception as e:
                if self.running:
                    print(f"[P2P] Erro na conexão: {e}")

    def _handle_connection(self, conn: socket.socket, addr):
        """Processa mensagens de um peer conectado"""
        try:
            data = conn.recv(4096).decode().strip()
            if not data:
                return
            
            msg = json.loads(data)
            msg_type = msg.get("type", "unknown")
            
            if msg_type == "hello":
                remote_id = msg.get("node_id", "???????")
                remote_port = msg.get("port", P2P_PORT)
                ip = addr[0]
                
                self.peers[remote_id] = Peer(
                    ip=ip,
                    port=remote_port,
                    node_id=remote_id,
                    last_seen=time.time()
                )
                self.connected_peers.add((ip, remote_port))
                
                # Responde com nosso hello
                resp = json.dumps({
                    "type": "hello",
                    "node_id": self.node_id,
                    "port": P2P_PORT
                })
                conn.send((resp + "\n").encode())
            
            elif msg_type == "get_blocks":
                # Sincronização de blocos
                start = msg.get("start", 0)
                count = msg.get("count", 50)
                headers_only = msg.get("headers_only", False)
                # Resposta tratada no blockchain
                pass
        
        except Exception as e:
            print(f"[P2P] Erro ao processar mensagem: {e}")
        finally:
            conn.close()

    def _discovery_loop(self):
        """Escuta anúncios UDP de outros nós na rede local"""
        while self.running:
            try:
                data, addr = self.udp_discovery.recvfrom(4096)
                msg = json.loads(data.decode())
                
                if msg.get("type") == "announce" and msg.get("node_id") != self.node_id:
                    node_id = msg["node_id"]
                    port = msg.get("port", P2P_PORT)
                    
                    if node_id not in self.peers:
                        print(f"[Discovery] Encontrado: {addr[0]}:{port} ({node_id})")
                    
                    self.peers[node_id] = Peer(
                        ip=addr[0],
                        port=port,
                        node_id=node_id,
                        last_seen=time.time()
                    )
                    
                    # Responde com nosso anúncio
                    response = json.dumps({
                        "type": "announce",
                        "node_id": self.node_id,
                        "port": P2P_PORT
                    })
                    self.udp_discovery.sendto(response.encode(), (addr[0], MULTICAST_PORT))
            
            except Exception as e:
                if self.running:
                    print(f"[Discovery] Erro: {e}")

    def _announce_loop(self):
        """Anuncia presença na rede a cada 30 segundos"""
        while self.running:
            try:
                msg = json.dumps({
                    "type": "announce",
                    "node_id": self.node_id,
                    "port": P2P_PORT
                })
                self.udp_discovery.sendto(msg.encode(), (MULTICAST_GROUP, MULTICAST_PORT))
            except Exception as e:
                print(f"[Announce] Erro: {e}")
            
            time.sleep(30)

    def _auto_reconnect_loop(self):
        """Tenta reconectar a cada 5 minutos se ficar sem peers"""
        while self.running:
            time.sleep(BOOTSTRAP_INTERVAL)
            
            if len(self.connected_peers) == 0:
                print("\n[Bootstrap] 🔄 Tentando reconectar à rede...")
                self.connect_to_bootstrap_peers()

    # ========================================================
    # UTILITÁRIOS
    # ========================================================

    def get_status(self) -> dict:
        """Retorna status da rede para API"""
        return {
            "node_id": self.node_id,
            "peers_conhecidos": len(self.peers),
            "peers_conectados": len(self.connected_peers),
            "peers": [
                {"ip": p.ip, "port": p.port, "node_id": p.node_id, "height": p.height}
                for p in self.peers.values()
                if time.time() - p.last_seen < 300
            ]
        }

    def stop(self):
        """Encerrar todos os subsistemas"""
        self.running = False
        if self.tcp_server:
            self.tcp_server.close()
        if self.udp_discovery:
            self.udp_discovery.close()
        print("[P2P] Encerrado")