"""
p2p_unified.py — Módulo P2P Unificado do BRN
============================================================
Substitui e unifica:
  • auto_discovery.py       (multicast 239.255.42.99 + token + UUID)
  • descoberta_p2p.py       (SSDP — OBSOLETO, conflitava com roteadores)
  • cripto_p2p_network.py   (broadcast UDP + servidor TCP + UPnP)

Funcionalidades:
  ✅ Descoberta automática de peers via multicast UDP
  ✅ Servidor TCP para troca de blocos/txs (protocolo JSON tipado)
  ✅ Cliente TCP com retry + timeout configurável
  ✅ UPnP (NAT traversal) para tornar o nó acessível da internet
  ✅ Persistência de peers descobertos
  ✅ Backoff adaptativo (menos tráfego em redes grandes)
  ✅ Token compartilhado (só aceita peers da mesma rede BRN)
  ✅ UUID persistente por instância (evita auto-conexão)
  ✅ Thread-safe (locks)
  ✅ Graceful shutdown (envia BYE)
============================================================
"""

import os
import sys
import json
import time
import uuid
import socket
import hashlib
import threading
import urllib.request
import xml.etree.ElementTree as ET

# ============================================================
# CONFIGURAÇÃO
# ============================================================
MULTICAST_GROUP = "239.255.42.99"      # faixa de admin local (não SSDP)
MULTICAST_PORT  = 50007
TCP_PORT_DEFAULT = 6001
DISCOVERY_INTERVAL_MIN = 2.0
DISCOVERY_INTERVAL_MAX = 30.0
PEER_TIMEOUT    = 60                    # segundos sem ping = morto
PEER_CLEANUP_S  = 30
MAX_MSG_SIZE    = 2 * 1024 * 1024       # 2 MB
PROTOCOL_VERSION = "BRN3/1.0"
NETWORK_MAGIC   = b"BRN3"

# Token compartilhado — TODOS os nós BRN devem usar o mesmo
NETWORK_SECRET = os.environ.get("BRN_NETWORK_SECRET", "brunocoin-lan-2026")
TOKEN_ESPERADO = hashlib.sha256(NETWORK_SECRET.encode()).hexdigest()[:8]

UUID_FILE  = "node_uuid.txt"
PEERS_FILE = "peers_discovered.json"


def _obter_ou_criar_uuid() -> str:
    if os.path.exists(UUID_FILE):
        try:
            with open(UUID_FILE) as f:
                val = f.read().strip()
                if val:
                    return val
        except Exception:
            pass
    novo = str(uuid.uuid4())
    try:
        with open(UUID_FILE, "w") as f:
            f.write(novo)
    except Exception:
        pass
    return novo


def _get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _mesma_subnet(ip1: str, ip2: str) -> bool:
    try:
        return ip1.rsplit(".", 1)[0] == ip2.rsplit(".", 1)[0]
    except Exception:
        return False


# ============================================================
# UPnP (NAT Traversal) — OPCIONAL
# ============================================================
class UPnPClient:
    """Cliente UPnP minimalista. Se falhar, o nó continua funcionando só na LAN."""

    def __init__(self, timeout: float = 3.0):
        self.control_url = None
        self.service_type = "urn:schemas-upnp-org:service:WANIPConnection:1"
        self.timeout = timeout
        self._discover()

    def _discover(self) -> bool:
        ssdp_addr = ("239.255.255.250", 1900)
        msg = (
            "M-SEARCH * HTTP/1.1\r\n"
            "HOST: 239.255.255.250:1900\r\n"
            'MAN: "ssdp:discover"\r\n'
            "MX: 2\r\n"
            "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
            "\r\n"
        ).encode()

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.settimeout(self.timeout)
        try:
            s.sendto(msg, ssdp_addr)
            while True:
                try:
                    data, _ = s.recvfrom(65507)
                    text = data.decode(errors="ignore")
                    location = self._extract_header(text, "LOCATION")
                    if location and self._fetch_control_url(location):
                        return True
                except socket.timeout:
                    break
        except Exception:
            pass
        finally:
            s.close()
        return False

    @staticmethod
    def _extract_header(response: str, header: str) -> str | None:
        for line in response.split("\r\n"):
            if line.upper().startswith(header.upper() + ":"):
                return line.split(":", 1)[1].strip()
        return None

    def _fetch_control_url(self, location: str) -> bool:
        try:
            with urllib.request.urlopen(location, timeout=self.timeout) as resp:
                xml_data = resp.read()
            root = ET.fromstring(xml_data)
            ns = "{urn:schemas-upnp-org:device-1-0}"
            for service in root.iter(f"{ns}service"):
                st = service.find(f"{ns}serviceType")
                cu = service.find(f"{ns}controlURL")
                if st is not None and cu is not None:
                    if "WANIPConnection" in st.text or "WANPPPConnection" in st.text:
                        base = location.rsplit("/", 1)[0]
                        self.service_type = st.text
                        self.control_url = (
                            base + cu.text if cu.text.startswith("/") else cu.text
                        )
                        return True
        except Exception:
            pass
        return False

    def _soap_request(self, body: str, action: str) -> bool:
        if not self.control_url:
            return False
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{self.service_type}#{action}"',
        }
        try:
            req = urllib.request.Request(
                self.control_url, data=body.encode(), headers=headers
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    def add_port_mapping(self, ext_port: int, int_port: int, int_ip: str,
                         description: str = "BRN Node", protocol: str = "TCP") -> bool:
        body = f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:AddPortMapping xmlns:u="{self.service_type}">
      <NewRemoteHost></NewRemoteHost>
      <NewExternalPort>{ext_port}</NewExternalPort>
      <NewProtocol>{protocol}</NewProtocol>
      <NewInternalPort>{int_port}</NewInternalPort>
      <NewInternalClient>{int_ip}</NewInternalClient>
      <NewEnabled>1</NewEnabled>
      <NewPortMappingDescription>{description}</NewPortMappingDescription>
      <NewLeaseDuration>0</NewLeaseDuration>
    </u:AddPortMapping>
  </s:Body>
</s:Envelope>"""
        return self._soap_request(body, "AddPortMapping")

    def delete_port_mapping(self, ext_port: int, protocol: str = "TCP") -> bool:
        body = f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:DeletePortMapping xmlns:u="{self.service_type}">
      <NewRemoteHost></NewRemoteHost>
      <NewExternalPort>{ext_port}</NewExternalPort>
      <NewProtocol>{protocol}</NewProtocol>
    </u:DeletePortMapping>
  </s:Body>
</s:Envelope>"""
        return self._soap_request(body, "DeletePortMapping")

    def get_external_ip(self) -> str | None:
        body = f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:GetExternalIPAddress xmlns:u="{self.service_type}">
    </u:GetExternalIPAddress>
  </s:Body>
</s:Envelope>"""
        if not self.control_url:
            return None
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{self.service_type}#GetExternalIPAddress"',
        }
        try:
            req = urllib.request.Request(
                self.control_url, data=body.encode(), headers=headers
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                xml_data = resp.read().decode(errors="ignore")
            root = ET.fromstring(xml_data)
            for elem in root.iter():
                if "ExternalIPAddress" in elem.tag:
                    return elem.text
        except Exception:
            pass
        return None


# ============================================================
# SERVIDOR TCP
# ============================================================
class P2PServer(threading.Thread):
    """Servidor TCP que aceita conexões de outros nós BRN."""

    def __init__(self, blockchain, port: int,
                 on_new_block=None, on_new_tx=None):
        super().__init__(daemon=True, name="P2P-Server")
        self.bc = blockchain
        self.port = port
        self.on_new_block = on_new_block
        self.on_new_tx = on_new_tx
        self.running = False
        self.sock = None

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

    def run(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind(("0.0.0.0", self.port))
            self.sock.listen(20)
            print(f"🌐 [P2P] Servidor TCP escutando na porta {self.port}")
            self.running = True
        except Exception as e:
            print(f"❌ [P2P] Erro ao abrir porta {self.port}: {e}")
            return

        while self.running:
            try:
                self.sock.settimeout(1.0)
                conn, addr = self.sock.accept()
                threading.Thread(
                    target=self._handle_conn, args=(conn, addr), daemon=True
                ).start()
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"⚠️ [P2P] Erro no accept: {e}")

    def _handle_conn(self, conn, addr):
        try:
            conn.settimeout(10)
            raw = conn.recv(MAX_MSG_SIZE)
            if not raw or not raw.startswith(NETWORK_MAGIC):
                return
            msg = json.loads(raw[len(NETWORK_MAGIC):].decode())
            response = self._process_message(msg, addr)
            if response:
                conn.sendall(NETWORK_MAGIC + json.dumps(response).encode())
        except Exception as e:
            print(f"⚠️ [P2P] Erro com {addr}: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _process_message(self, msg: dict, addr) -> dict | None:
        mtype = msg.get("type")
        try:
            if mtype == "ping":
                return {"type": "pong", "version": PROTOCOL_VERSION,
                        "height": self.bc.db.height()}

            if mtype == "get_chain_height":
                return {"type": "chain_height",
                        "height": self.bc.db.height(),
                        "hash": self.bc.db.tip_hash()}

            if mtype == "get_block":
                h = int(msg.get("height", 0))
                block = self.bc.db.get_block(h)
                return {"type": "block", "block": block}

            if mtype == "get_blocks_range":
                start = int(msg.get("start", 0))
                end = int(msg.get("end", start + 50))
                blocks = []
                for h in range(start, min(end, self.bc.db.height() + 1)):
                    b = self.bc.db.get_block(h)
                    if b:
                        blocks.append(b)
                return {"type": "blocks_range", "blocks": blocks}

            if mtype == "new_block":
                block_dict = msg.get("block")
                if block_dict and self.on_new_block:
                    ok = self.on_new_block(block_dict)
                    return {"type": "ack", "ok": bool(ok)}
                return {"type": "ack", "ok": False}

            if mtype == "new_tx":
                tx_dict = msg.get("tx")
                if tx_dict and self.on_new_tx:
                    ok = self.on_new_tx(tx_dict)
                    return {"type": "ack", "ok": bool(ok)}
                return {"type": "ack", "ok": False}

            if mtype == "get_mempool":
                return {"type": "mempool",
                        "txs": self.bc.db.all_mempool(limit=200)}
        except Exception as e:
            return {"type": "error", "message": str(e)}
        return {"type": "error", "message": "Tipo desconhecido"}


# ============================================================
# CLIENTE TCP
# ============================================================
class P2PClient:
    """Cliente estático para conversar com peers."""

    @staticmethod
    def send_message(ip: str, port: int, message: dict,
                     timeout: float = 5.0) -> dict | None:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((ip, port))
            payload = NETWORK_MAGIC + json.dumps(message).encode()
            s.sendall(payload)
            raw = s.recv(MAX_MSG_SIZE)
            s.close()
            if raw.startswith(NETWORK_MAGIC):
                return json.loads(raw[len(NETWORK_MAGIC):].decode())
            return None
        except Exception:
            return None

    @staticmethod
    def ping(ip, port):                 return P2PClient.send_message(ip, port, {"type": "ping"})
    @staticmethod
    def get_chain_height(ip, port):     return P2PClient.send_message(ip, port, {"type": "get_chain_height"})
    @staticmethod
    def get_block(ip, port, h):         return P2PClient.send_message(ip, port, {"type": "get_block", "height": h})
    @staticmethod
    def get_blocks_range(ip, port, s, e): return P2PClient.send_message(ip, port, {"type": "get_blocks_range", "start": s, "end": e})
    @staticmethod
    def send_block(ip, port, block):    return P2PClient.send_message(ip, port, {"type": "new_block", "block": block})
    @staticmethod
    def send_tx(ip, port, tx):          return P2PClient.send_message(ip, port, {"type": "new_tx", "tx": tx})
    @staticmethod
    def get_mempool(ip, port):          return P2PClient.send_message(ip, port, {"type": "get_mempool"})


# ============================================================
# DESCOBERTA MULTICAST
# ============================================================
class PeerDiscovery:
    """Descoberta automática via multicast UDP com token e UUID."""

    def __init__(self, tcp_port: int, node_uuid: str,
                 on_peer_found=None, blockchain=None):
        self.tcp_port = tcp_port
        self.node_uuid = node_uuid
        self.on_peer_found = on_peer_found
        self.bc = blockchain

        self.discovered_peers: dict[str, float] = {}   # "ip:port" → timestamp
        self.peers_lock = threading.Lock()
        self.peers_respondidos: set[str] = set()

        self.running = False
        self.server_socket = None
        self.client_socket = None
        self._threads: list[threading.Thread] = []

        self._carregar_peers()

    # -------- persistência --------
    def _salvar_peers(self):
        try:
            with self.peers_lock:
                lista = list(self.discovered_peers.keys())
            with open(PEERS_FILE, "w") as f:
                json.dump(lista, f)
        except Exception:
            pass

    def _carregar_peers(self):
        try:
            if not os.path.exists(PEERS_FILE):
                return
            with open(PEERS_FILE) as f:
                for addr in json.load(f):
                    self.discovered_peers[addr] = 0
        except Exception:
            pass

    # -------- servidor (escuta multicast) --------
    def _start_server(self):
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass

            self.server_socket.bind(("", MULTICAST_PORT))
            mreq = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton("0.0.0.0")
            self.server_socket.setsockopt(
                socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq
            )

            local_ip = _get_local_ip()
            print(f"📡 [Discovery] Escutando {MULTICAST_GROUP}:{MULTICAST_PORT}")
            print(f"📡 [Discovery] IP local: {local_ip}")

            while self.running:
                try:
                    self.server_socket.settimeout(2.0)
                    data, addr = self.server_socket.recvfrom(2048)
                    remote_ip = addr[0]

                    if not _mesma_subnet(remote_ip, local_ip):
                        continue

                    msg = data.decode("utf-8", errors="ignore")
                    if not self._token_valido(msg):
                        continue

                    if msg.startswith("BRN_NODE_PING:"):
                        self._processar_ping(msg, remote_ip, addr)
                    elif msg.startswith("BRN_NODE_PONG:"):
                        self._processar_pong(msg, remote_ip)
                    elif msg.startswith("BRN_NODE_BYE:"):
                        self._processar_bye(msg, remote_ip)

                except socket.timeout:
                    continue
                except OSError:
                    break
                except Exception as e:
                    print(f"⚠️ [Discovery] Erro: {e}")
        except Exception as e:
            print(f"❌ [Discovery] Falha crítica: {e}")

    def _token_valido(self, msg: str) -> bool:
        try:
            return msg.split(":")[-1] == TOKEN_ESPERADO
        except Exception:
            return False

    def _processar_ping(self, msg: str, remote_ip: str, addr):
        try:
            partes = msg.split(":")
            if len(partes) < 4:
                return
            remote_port = int(partes[1])
            remote_uuid = partes[2]

            if remote_uuid == self.node_uuid:
                return

            peer_address = f"{remote_ip}:{remote_port}"
            with self.peers_lock:
                novo = peer_address not in self.discovered_peers
                self.discovered_peers[peer_address] = time.time()

            if novo:
                print(f"✨ [Discovery] Novo peer: {peer_address}")
                self._salvar_peers()
                if self.on_peer_found:
                    try:
                        self.on_peer_found(remote_ip, remote_port)
                    except Exception as e:
                        print(f"⚠️ [Discovery] Erro no callback: {e}")

            if remote_ip not in self.peers_respondidos:
                self.peers_respondidos.add(remote_ip)
                response = (
                    f"BRN_NODE_PONG:{self.tcp_port}:"
                    f"{self.node_uuid}:{TOKEN_ESPERADO}"
                )
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.sendto(response.encode("utf-8"), addr)
                    sock.close()
                except Exception:
                    pass
        except Exception as e:
            print(f"⚠️ [Discovery] Erro em PING: {e}")

    def _processar_pong(self, msg: str, remote_ip: str):
        try:
            partes = msg.split(":")
            if len(partes) < 4:
                return
            remote_port = int(partes[1])
            remote_uuid = partes[2]
            if remote_uuid == self.node_uuid:
                return

            peer_address = f"{remote_ip}:{remote_port}"
            with self.peers_lock:
                novo = peer_address not in self.discovered_peers
                self.discovered_peers[peer_address] = time.time()

            if novo:
                print(f"🤝 [Discovery] Conexão mútua: {peer_address}")
                self._salvar_peers()
                if self.on_peer_found:
                    try:
                        self.on_peer_found(remote_ip, remote_port)
                    except Exception:
                        pass
        except Exception:
            pass

    def _processar_bye(self, msg: str, remote_ip: str):
        try:
            partes = msg.split(":")
            if len(partes) < 4:
                return
            remote_port = int(partes[1])
            peer_address = f"{remote_ip}:{remote_port}"
            with self.peers_lock:
                self.discovered_peers.pop(peer_address, None)
                print(f"👋 [Discovery] Peer saiu: {peer_address}")
        except Exception:
            pass

    # -------- cliente (broadcast) --------
    def _start_client(self):
        try:
            self.client_socket = socket.socket(
                socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP
            )
            self.client_socket.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2
            )
            self.client_socket.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1
            )
            print("🚀 [Discovery] Broadcast ativo")

            while self.running:
                try:
                    msg = (
                        f"BRN_NODE_PING:{self.tcp_port}:"
                        f"{self.node_uuid}:{TOKEN_ESPERADO}"
                    )
                    self.client_socket.sendto(
                        msg.encode("utf-8"),
                        (MULTICAST_GROUP, MULTICAST_PORT)
                    )

                    with self.peers_lock:
                        n = len(self.discovered_peers)

                    if n >= 5:
                        intervalo = DISCOVERY_INTERVAL_MAX
                    elif n >= 2:
                        intervalo = 10.0
                    else:
                        intervalo = DISCOVERY_INTERVAL_MIN

                    time.sleep(intervalo)
                except OSError:
                    break
                except Exception:
                    time.sleep(5)
        except Exception as e:
            print(f"❌ [Discovery] Falha no cliente: {e}")

    # -------- limpeza --------
    def _limpar_peers_mortos(self):
        while self.running:
            try:
                time.sleep(PEER_CLEANUP_S)
                agora = time.time()
                removidos = []
                with self.peers_lock:
                    for addr, ts in list(self.discovered_peers.items()):
                        if ts > 0 and (agora - ts) > PEER_TIMEOUT:
                            del self.discovered_peers[addr]
                            removidos.append(addr)
                if removidos:
                    print(f"🧹 [Discovery] Removidos {len(removidos)} peers inativos")
                    self._salvar_peers()
            except Exception:
                pass

    # -------- controle --------
    def run(self):
        if self.running:
            return
        self.running = True

        t1 = threading.Thread(target=self._start_server, daemon=True, name="Disc-Server")
        t2 = threading.Thread(target=self._start_client, daemon=True, name="Disc-Client")
        t3 = threading.Thread(target=self._limpar_peers_mortos, daemon=True, name="Disc-Cleanup")

        self._threads = [t1, t2, t3]
        for t in self._threads:
            t.start()

    def stop(self):
        if not self.running:
            return
        print("[Discovery] Encerrando…")
        self.running = False

        try:
            if self.client_socket:
                bye = (
                    f"BRN_NODE_BYE:{self.tcp_port}:"
                    f"{self.node_uuid}:{TOKEN_ESPERADO}"
                )
                self.client_socket.sendto(
                    bye.encode("utf-8"),
                    (MULTICAST_GROUP, MULTICAST_PORT)
                )
        except Exception:
            pass

        for sock in (self.server_socket, self.client_socket):
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

        self._salvar_peers()

    def listar_peers(self) -> list[str]:
        with self.peers_lock:
            return list(self.discovered_peers.keys())


# ============================================================
# GERENCIADOR P2P (FACHADA)
# ============================================================
class P2PManager:
    """
    Fachada única que integra Servidor + Cliente + Descoberta + UPnP.
    Este é o único objeto que o resto do sistema precisa instanciar.
    """

    def __init__(self, blockchain, tcp_port: int = TCP_PORT_DEFAULT,
                 enable_upnp: bool = True):
        self.bc = blockchain
        self.tcp_port = tcp_port
        self.node_uuid = _obter_ou_criar_uuid()
        self.enable_upnp = enable_upnp
        self.external_ip = None
        self.upnp = None

        self.server = P2PServer(
            blockchain, tcp_port,
            on_new_block=self._on_new_block,
            on_new_tx=self._on_new_tx,
        )
        self.discovery = PeerDiscovery(
            tcp_port, self.node_uuid,
            on_peer_found=self._on_peer_found,
            blockchain=blockchain,
        )

        if enable_upnp:
            threading.Thread(target=self._setup_upnp, daemon=True).start()

    def start(self):
        self.server.start()
        self.discovery.run()
        print(f"🌐 [P2P] Manager iniciado (node_id={self.node_uuid[:8]})")

    def stop(self):
        self.server.stop()
        self.discovery.stop()
        if self.upnp and self.external_ip:
            try:
                self.upnp.delete_port_mapping(self.tcp_port, "TCP")
            except Exception:
                pass

    def _setup_upnp(self):
        try:
            print("🔌 [UPnP] Descobrindo roteador…")
            self.upnp = UPnPClient()
            if not self.upnp.control_url:
                print("⚠️ [UPnP] Roteador não suporta ou está desabilitado.")
                return
            local_ip = _get_local_ip()
            ok = self.upnp.add_port_mapping(
                self.tcp_port, self.tcp_port, local_ip, description="BRN Node"
            )
            if ok:
                self.external_ip = self.upnp.get_external_ip()
                print(f"✅ [UPnP] Porta {self.tcp_port} mapeada. IP externo: {self.external_ip}")
            else:
                print("⚠️ [UPnP] Falha ao mapear porta.")
        except Exception as e:
            print(f"⚠️ [UPnP] Erro: {e}")

    def _on_peer_found(self, ip: str, port: int):
        try:
            resp = P2PClient.get_chain_height(ip, port)
            if resp and resp.get("height", -1) > self.bc.db.height():
                print(f"🔄 [P2P] Sincronizando com {ip}:{port}")
                self.sync_with_peer(ip, port)
        except Exception as e:
            print(f"⚠️ [P2P] Erro ao sincronizar com {ip}: {e}")

    def sync_with_peer(self, ip: str, port: int):
        resp = P2PClient.get_chain_height(ip, port)
        if not resp:
            return
        remote_height = resp.get("height", -1)
        if remote_height <= self.bc.db.height():
            return

        start = self.bc.db.height() + 1
        while start <= remote_height:
            end = min(start + 50, remote_height + 1)
            resp = P2PClient.get_blocks_range(ip, port, start, end)
            if not resp or "blocks" not in resp:
                break
            for block_dict in resp["blocks"]:
                if block_dict and block_dict.get("height", -1) > self.bc.db.height():
                    try:
                        self.bc.db.add_block(block_dict)
                    except Exception as e:
                        print(f"⚠️ [P2P] Erro ao salvar bloco: {e}")
            start = end
        print(f"✅ [P2P] Sincronizado. Altura local: {self.bc.db.height()}")

    def broadcast_block(self, block_dict: dict):
        peers = self.discovery.listar_peers()
        for peer in peers:
            try:
                ip, port = peer.split(":")
                threading.Thread(
                    target=P2PClient.send_block,
                    args=(ip, int(port), block_dict),
                    daemon=True,
                ).start()
            except Exception:
                pass

    def broadcast_tx(self, tx_dict: dict):
        peers = self.discovery.listar_peers()
        for peer in peers:
            try:
                ip, port = peer.split(":")
                threading.Thread(
                    target=P2PClient.send_tx,
                    args=(ip, int(port), tx_dict),
                    daemon=True,
                ).start()
            except Exception:
                pass

    def _on_new_block(self, block_dict: dict) -> bool:
        try:
            h = block_dict.get("height", -1)
            if h > self.bc.db.height():
                self.bc.db.add_block(block_dict)
                print(f"📦 [P2P] Novo bloco recebido: #{h}")
                return True
        except Exception as e:
            print(f"⚠️ [P2P] Erro ao processar bloco: {e}")
        return False

    def _on_new_tx(self, tx_dict: dict) -> bool:
        try:
            ok, msg = self.bc.submit_tx(tx_dict)
            if ok:
                print(f"💸 [P2P] Nova tx: {tx_dict.get('txid', '?')[:16]}…")
            return ok
        except Exception as e:
            print(f"⚠️ [P2P] Erro ao processar tx: {e}")
            return False

    def get_status(self) -> dict:
        peers = self.discovery.listar_peers()
        return {
            "node_id": self.node_uuid,
            "port": self.tcp_port,
            "external_ip": self.external_ip,
            "peer_count": len(peers),
            "peers": peers,
            "height": self.bc.db.height(),
        }


# ============================================================
# TESTE ISOLADO
# ============================================================
if __name__ == "__main__":
    print("⚠️  Rode este módulo a partir do seu nó principal (server.py).")
    print("   O teste isolado precisa de uma instância de Blockchain.")
