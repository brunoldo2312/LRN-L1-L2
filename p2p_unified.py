"""
p2p_unified.py — Módulo P2P Unificado do BRN (v5)
============================================================
v5: + Sync por cumulative_work (Bitcoin-style fork choice)
    + accept_block (valida PoW, merkle, assinatura)
    + Peer scoring + Rate limit
    + Bootstrap (arquivo + env var)
    + Peer Exchange (PEX)
    + Tracker HTTP opcional
    + GitHub peer discovery
============================================================
NOTA: GH_USER/GH_REPO com defaults ajustados para
      repositório brunoldo2312/peers.json
============================================================
"""

import os
import json
import time
import uuid
import base64
import socket
import hashlib
import threading
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

# ============================================================
# CONFIGURAÇÃO
# ============================================================
MULTICAST_GROUP = "239.255.42.99"
MULTICAST_PORT  = 50007
TCP_PORT_DEFAULT = 6001
DISCOVERY_INTERVAL_MIN = 2.0
DISCOVERY_INTERVAL_MAX = 30.0
PEER_TIMEOUT    = 60
PEER_CLEANUP_S  = 30
MAX_MSG_SIZE    = 2 * 1024 * 1024
PROTOCOL_VERSION = "BRN5/1.0"
NETWORK_MAGIC   = b"BRN5"

PEER_SCORE_BAN_THRESHOLD = -100
PEER_SCORE_REWARD_GOOD   = 10
PEER_SCORE_PENALTY_BAD   = -50
RATE_LIMIT_MSGS_PER_SEC  = 20

NETWORK_SECRET = os.environ.get("BRN_NETWORK_SECRET", "brunocoin-lan-2026")
TOKEN_ESPERADO = hashlib.sha256(NETWORK_SECRET.encode()).hexdigest()[:8]

UUID_FILE      = "node_uuid.txt"
PEERS_FILE     = "peers_discovered.json"
BOOTSTRAP_FILE = "bootstrap_peers.json"
BOOTSTRAP_ENV  = os.environ.get("BRN_BOOTSTRAP_PEERS", "")

# GitHub peer discovery
# ✅ Ajustado: defaults apontam para o seu repositório real
GH_USER     = os.environ.get("BRN_GH_USER", "brunoldo2312").strip()
GH_REPO     = os.environ.get("BRN_GH_REPO", "peers.json").strip()
GH_TOKEN    = os.environ.get("BRN_GH_TOKEN", "").strip()
GH_BRANCH   = os.environ.get("BRN_GH_BRANCH", "main").strip()
GH_FILE     = os.environ.get("BRN_GH_FILE", "peers.json").strip()
GH_INTERVAL = int(os.environ.get("BRN_GH_INTERVAL", "180"))
GH_TTL      = 600
GH_API      = "https://api.github.com"

# Tracker HTTP opcional
TRACKER_URL       = os.environ.get("BRN_TRACKER", "").rstrip("/")
TRACKER_INTERVAL  = 120
BOOTSTRAP_PING_INTERVAL = 60


# ============================================================
# UTILIDADES
# ============================================================
def _obter_ou_criar_uuid():
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


def _get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _get_public_ip():
    for url in ("https://ifconfig.me/ip",
                "https://api.ipify.org",
                "https://icanhazip.com"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=5) as r:
                ip = r.read().decode().strip()
                if ip and "." in ip:
                    return ip
        except Exception:
            continue
    return ""


def _mesma_subnet(ip1, ip2):
    try:
        return ip1.rsplit(".", 1)[0] == ip2.rsplit(".", 1)[0]
    except Exception:
        return False


def _carregar_bootstrap():
    peers = set()
    if os.path.exists(BOOTSTRAP_FILE):
        try:
            with open(BOOTSTRAP_FILE) as f:
                data = json.load(f)
                if isinstance(data, list):
                    for p in data:
                        p = str(p).strip()
                        if p:
                            peers.add(p)
        except Exception:
            pass
    for p in BOOTSTRAP_ENV.split(","):
        p = p.strip()
        if p:
            peers.add(p)
    return peers


# ============================================================
# GITHUB PEER DISCOVERY
# ============================================================
def _gh_headers():
    h = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "brn-node/1.0",
    }
    if GH_TOKEN:
        h["Authorization"] = "token " + GH_TOKEN
    return h


def _gh_url_file():
    return f"{GH_API}/repos/{GH_USER}/{GH_REPO}/contents/{GH_FILE}"


def _gh_ler_peers():
    if not (GH_USER and GH_REPO):
        return {}
    try:
        req = urllib.request.Request(_gh_url_file(), headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        conteudo = base64.b64decode(data["content"]).decode()
        obj = json.loads(conteudo)
        return obj if isinstance(obj, dict) else {}
    except urllib.error.HTTPError as e:
        # ✅ Aviso visível para diagnóstico (antes era silencioso)
        print(f"[GitHub] _gh_ler_peers HTTP {e.code}: {e.reason}")
        return {}
    except Exception as e:
        print(f"[GitHub] _gh_ler_peers erro: {e}")
        return {}


def _gh_escrever_peers(peers):
    if not (GH_USER and GH_REPO and GH_TOKEN):
        return False
    sha = None
    try:
        req = urllib.request.Request(_gh_url_file(), headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=10) as r:
            atual = json.loads(r.read())
            sha = atual.get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"[GitHub] _gh_escrever_peers GET HTTP {e.code}: {e.reason}")
            return False
    except Exception as e:
        print(f"[GitHub] _gh_escrever_peers GET erro: {e}")
        return False

    conteudo_b64 = base64.b64encode(
        json.dumps(peers, indent=2, sort_keys=True).encode()
    ).decode()
    body = {
        "message": "brn: update peers",
        "content": conteudo_b64,
        "branch": GH_BRANCH,
    }
    if sha:
        body["sha"] = sha

    try:
        req = urllib.request.Request(
            _gh_url_file(),
            data=json.dumps(body).encode(),
            headers={**_gh_headers(), "Content-Type": "application/json"},
            method="PUT",
        )
        urllib.request.urlopen(req, timeout=15)
        return True
    except urllib.error.HTTPError as e:
        # ✅ Aviso visível para diagnóstico
        print(f"[GitHub] _gh_escrever_peers PUT HTTP {e.code}: {e.reason}")
        return False
    except Exception as e:
        print(f"[GitHub] erro ao escrever: {e}")
        return False


# ============================================================
# TRACKER HTTP
# ============================================================
def _anunciar_no_tracker(addr):
    if not TRACKER_URL:
        return
    try:
        req = urllib.request.Request(
            TRACKER_URL + "/anunciar",
            data=json.dumps({"address": addr}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _peers_do_tracker():
    if not TRACKER_URL:
        return []
    try:
        with urllib.request.urlopen(TRACKER_URL + "/peers", timeout=5) as r:
            return json.loads(r.read()).get("peers", [])
    except Exception:
        return []


# ============================================================
# UPnP
# ============================================================
class UPnPClient:
    def __init__(self, timeout=3.0):
        self.control_url = None
        self.service_type = "urn:schemas-upnp-org:service:WANIPConnection:1"
        self.timeout = timeout
        self._discover()

    def _discover(self):
        ssdp_addr = ("239.255.255.250", 1900)
        msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
               'MAN: "ssdp:discover"\r\nMX: 2\r\n'
               "ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n").encode()
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
    def _extract_header(response, header):
        for line in response.split("\r\n"):
            if line.upper().startswith(header.upper() + ":"):
                return line.split(":", 1)[1].strip()
        return None

    def _fetch_control_url(self, location):
        try:
            with urllib.request.urlopen(location, timeout=self.timeout) as resp:
                xml_data = resp.read()
            root = ET.fromstring(xml_data)
            ns = "{urn:schemas-upnp-org:device-1-0}"
            for service in root.iter(ns + "service"):
                st = service.find(ns + "serviceType")
                cu = service.find(ns + "controlURL")
                if st is not None and cu is not None:
                    if "WANIPConnection" in st.text or "WANPPPConnection" in st.text:
                        base = location.rsplit("/", 1)[0]
                        self.service_type = st.text
                        self.control_url = base + cu.text if cu.text.startswith("/") else cu.text
                        return True
        except Exception:
            pass
        return False

    def _soap_request(self, body, action):
        if not self.control_url:
            return False
        headers = {"Content-Type": 'text/xml; charset="utf-8"',
                   "SOAPAction": '"' + self.service_type + "#" + action + '"'}
        try:
            req = urllib.request.Request(self.control_url,
                                          data=body.encode(),
                                          headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    def add_port_mapping(self, ext_port, int_port, int_ip,
                         description="BRN Node", protocol="TCP"):
        body = '<?xml version="1.0"?>'
        body += '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        body += "<s:Body>"
        body += '<u:AddPortMapping xmlns:u="' + self.service_type + '">'
        body += "<NewRemoteHost></NewRemoteHost>"
        body += "<NewExternalPort>" + str(ext_port) + "</NewExternalPort>"
        body += "<NewProtocol>" + protocol + "</NewProtocol>"
        body += "<NewInternalPort>" + str(int_port) + "</NewInternalPort>"
        body += "<NewInternalClient>" + int_ip + "</NewInternalClient>"
        body += "<NewEnabled>1</NewEnabled>"
        body += "<NewPortMappingDescription>" + description + "</NewPortMappingDescription>"
        body += "<NewLeaseDuration>0</NewLeaseDuration>"
        body += "</u:AddPortMapping></s:Body></s:Envelope>"
        return self._soap_request(body, "AddPortMapping")

    def delete_port_mapping(self, ext_port, protocol="TCP"):
        body = '<?xml version="1.0"?>'
        body += '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        body += "<s:Body>"
        body += '<u:DeletePortMapping xmlns:u="' + self.service_type + '">'
        body += "<NewRemoteHost></NewRemoteHost>"
        body += "<NewExternalPort>" + str(ext_port) + "</NewExternalPort>"
        body += "<NewProtocol>" + protocol + "</NewProtocol>"
        body += "</u:DeletePortMapping></s:Body></s:Envelope>"
        return self._soap_request(body, "DeletePortMapping")

    def get_external_ip(self):
        if not self.control_url:
            return None
        body = '<?xml version="1.0"?>'
        body += '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        body += "<s:Body>"
        body += '<u:GetExternalIPAddress xmlns:u="' + self.service_type + '"></u:GetExternalIPAddress>'
        body += "</s:Body></s:Envelope>"
        headers = {"Content-Type": 'text/xml; charset="utf-8"',
                   "SOAPAction": '"' + self.service_type + "#GetExternalIPAddress" + '"'}
        try:
            req = urllib.request.Request(self.control_url,
                                          data=body.encode(),
                                          headers=headers)
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
    def __init__(self, blockchain, port, on_new_block=None, on_new_tx=None,
                 on_peer_bad=None, on_peer_good=None, get_peers_callback=None):
        super().__init__(daemon=True, name="P2P-Server")
        self.bc = blockchain
        self.port = port
        self.on_new_block = on_new_block
        self.on_new_tx = on_new_tx
        self.on_peer_bad = on_peer_bad
        self.on_peer_good = on_peer_good
        self.get_peers_callback = get_peers_callback
        self.running = False
        self.sock = None
        self._rate_counters = {}
        self._rate_lock = threading.Lock()

    def stop(self):
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

    def _check_rate(self, addr):
        agora = time.time()
        with self._rate_lock:
            janela = self._rate_counters.setdefault(addr, [])
            janela[:] = [t for t in janela if agora - t < 1.0]
            if len(janela) >= RATE_LIMIT_MSGS_PER_SEC:
                return False
            janela.append(agora)
            return True

    def run(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind(("0.0.0.0", self.port))
            self.sock.listen(20)
            print(f"[P2P] Servidor TCP escutando na porta {self.port}")
            self.running = True
        except Exception as e:
            print(f"[P2P] Erro ao abrir porta {self.port}: {e}")
            return
        while self.running:
            try:
                self.sock.settimeout(1.0)
                conn, addr = self.sock.accept()
                threading.Thread(target=self._handle_conn,
                                 args=(conn, addr), daemon=True).start()
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"[P2P] Erro no accept: {e}")

    def _handle_conn(self, conn, addr):
        try:
            peer_ip = addr[0]
            if not self._check_rate(peer_ip):
                return
            score = self.bc.db.get_peer_score(peer_ip)
            if score <= PEER_SCORE_BAN_THRESHOLD:
                print(f"[P2P] Peer banido ({score}): {peer_ip}")
                return
            conn.settimeout(10)
            raw = conn.recv(MAX_MSG_SIZE)
            if not raw or not raw.startswith(NETWORK_MAGIC):
                return
            msg = json.loads(raw[len(NETWORK_MAGIC):].decode())
            response = self._process_message(msg, addr)
            if response:
                conn.sendall(NETWORK_MAGIC + json.dumps(response).encode())
        except Exception as e:
            print(f"[P2P] Erro com {addr}: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _process_message(self, msg, addr):
        mtype = msg.get("type")
        peer_ip = addr[0]
        try:
            if mtype == "ping":
                return {"type": "pong", "version": PROTOCOL_VERSION,
                        "height": self.bc.db.height(),
                        "work": self.bc.cumulative_work()}

            if mtype == "get_chain_height":
                return {"type": "chain_height",
                        "height": self.bc.db.height(),
                        "hash": self.bc.db.tip_hash(),
                        "work": self.bc.cumulative_work()}

            if mtype == "get_block":
                return {"type": "block",
                        "block": self.bc.db.get_block(int(msg.get("height", 0)))}

            if mtype == "get_blocks_range":
                start = int(msg.get("start", 0))
                end = int(msg.get("end", start + 50))
                return {"type": "blocks_range",
                        "blocks": self.bc.db.get_blocks_range(start, end)}

            if mtype == "new_block":
                block_dict = msg.get("block")
                if block_dict and self.on_new_block:
                    ok = self.on_new_block(block_dict)
                    if ok and self.on_peer_good:
                        self.on_peer_good(peer_ip)
                    elif not ok and self.on_peer_bad:
                        self.on_peer_bad(peer_ip)
                    return {"type": "ack", "ok": bool(ok)}
                return {"type": "ack", "ok": False}

            if mtype == "new_tx":
                tx_dict = msg.get("tx")
                if tx_dict and self.on_new_tx:
                    ok = self.on_new_tx(tx_dict)
                    if ok and self.on_peer_good:
                        self.on_peer_good(peer_ip)
                    return {"type": "ack", "ok": bool(ok)}
                return {"type": "ack", "ok": False}

            if mtype == "get_mempool":
                return {"type": "mempool",
                        "txs": self.bc.db.all_mempool(limit=200)}

            if mtype == "get_peers":
                peers = []
                if self.get_peers_callback:
                    try:
                        peers = self.get_peers_callback()
                    except Exception:
                        peers = []
                return {"type": "peers", "peers": peers}
        except Exception as e:
            return {"type": "error", "message": str(e)}
        return {"type": "error", "message": "Tipo desconhecido"}


# ============================================================
# CLIENTE TCP
# ============================================================
class P2PClient:
    @staticmethod
    def send_message(ip, port, message, timeout=5.0):
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
    @staticmethod
    def get_peers(ip, port):            return P2PClient.send_message(ip, port, {"type": "get_peers"})


# ============================================================
# DESCOBERTA
# ============================================================
class PeerDiscovery:
    def __init__(self, tcp_port, node_uuid, on_peer_found=None, blockchain=None):
        self.tcp_port = tcp_port
        self.node_uuid = node_uuid
        self.on_peer_found = on_peer_found
        self.bc = blockchain
        self.discovered_peers = {}
        self.peers_lock = threading.Lock()
        self.peers_respondidos = set()
        self.running = False
        self.server_socket = None
        self.client_socket = None
        self._threads = []
        self._carregar_peers()

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
            if os.path.exists(PEERS_FILE):
                with open(PEERS_FILE) as f:
                    for addr in json.load(f):
                        self.discovered_peers[addr] = 0
        except Exception:
            pass
        for p in _carregar_bootstrap():
            self.discovered_peers.setdefault(p, 0)
        if self.discovered_peers:
            print(f"[Discovery] {len(self.discovered_peers)} peer(s) do disco/bootstrap")

    # ---------- multicast ----------
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
            self.server_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            local_ip = _get_local_ip()
            print(f"[Discovery] Multicast: {MULTICAST_GROUP}:{MULTICAST_PORT}")
            print(f"[Discovery] IP local: {local_ip}")
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
                    print(f"[Discovery] Erro: {e}")
        except Exception as e:
            print(f"[Discovery] Falha multicast: {e}")

    def _token_valido(self, msg):
        try:
            return msg.split(":")[-1] == TOKEN_ESPERADO
        except Exception:
            return False

    def _processar_ping(self, msg, remote_ip, addr):
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
                print(f"[Discovery] Novo peer LAN: {peer_address}")
                self._salvar_peers()
                if self.on_peer_found:
                    try:
                        self.on_peer_found(remote_ip, remote_port)
                    except Exception:
                        pass
            if remote_ip not in self.peers_respondidos:
                self.peers_respondidos.add(remote_ip)
                response = f"BRN_NODE_PONG:{self.tcp_port}:{self.node_uuid}:{TOKEN_ESPERADO}"
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.sendto(response.encode("utf-8"), addr)
                    sock.close()
                except Exception:
                    pass
        except Exception:
            pass

    def _processar_pong(self, msg, remote_ip):
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
                print(f"[Discovery] Conexao mutua: {peer_address}")
                self._salvar_peers()
                if self.on_peer_found:
                    try:
                        self.on_peer_found(remote_ip, remote_port)
                    except Exception:
                        pass
        except Exception:
            pass

    def _processar_bye(self, msg, remote_ip):
        try:
            partes = msg.split(":")
            if len(partes) < 4:
                return
            remote_port = int(partes[1])
            peer_address = f"{remote_ip}:{remote_port}"
            with self.peers_lock:
                self.discovered_peers.pop(peer_address, None)
        except Exception:
            pass

    def _start_client(self):
        try:
            self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self.client_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            self.client_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            print("[Discovery] Multicast broadcast ativo")
            while self.running:
                try:
                    msg = f"BRN_NODE_PING:{self.tcp_port}:{self.node_uuid}:{TOKEN_ESPERADO}"
                    self.client_socket.sendto(msg.encode("utf-8"),
                                               (MULTICAST_GROUP, MULTICAST_PORT))
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
            print(f"[Discovery] Falha multicast client: {e}")

    # ---------- bootstrap ----------
    def _ping_bootstrap_loop(self):
        print(f"[Bootstrap] Loop iniciado (a cada {BOOTSTRAP_PING_INTERVAL}s)")
        while self.running:
            try:
                peers = _carregar_bootstrap()
                if peers:
                    for peer in peers:
                        try:
                            ip, port = peer.split(":")
                            port = int(port)
                            resp = P2PClient.ping(ip, port)
                            if resp:
                                with self.peers_lock:
                                    novo = peer not in self.discovered_peers
                                    self.discovered_peers[peer] = time.time()
                                if novo:
                                    print(f"[Bootstrap] Conectado: {peer}")
                                    self._salvar_peers()
                                if self.on_peer_found:
                                    try:
                                        self.on_peer_found(ip, port)
                                    except Exception:
                                        pass
                        except Exception:
                            pass
            except Exception as e:
                print(f"[Bootstrap] erro: {e}")
            time.sleep(BOOTSTRAP_PING_INTERVAL)

    # ---------- GitHub ----------
    def _github_loop(self):
        if not (GH_USER and GH_REPO):
            return
        if not GH_TOKEN:
            print("[GitHub] BRN_GH_TOKEN vazio — só leitura (não vai anunciar)")
        print(f"[GitHub] Loop iniciado — {GH_USER}/{GH_REPO}/{GH_FILE} @ {GH_BRANCH}")

        public_ip = _get_public_ip()
        if not public_ip:
            print("[GitHub] Não consegui descobrir IP público — abortando")
            return
        meu_addr = f"{public_ip}:{self.tcp_port}"
        print(f"[GitHub] Anunciando como {meu_addr}")

        while self.running:
            try:
                peers = _gh_ler_peers()
                agora = time.time()
                peers = {k: v for k, v in peers.items()
                         if isinstance(v, dict) and (agora - v.get("ts", 0)) < GH_TTL}

                for addr, info in peers.items():
                    if addr == meu_addr:
                        continue
                    with self.peers_lock:
                        if addr not in self.discovered_peers:
                            self.discovered_peers[addr] = time.time()
                            print(f"[GitHub] Descoberto: {addr} (h={info.get('h', '?')})")
                            try:
                                ip, port = addr.split(":")
                                if self.on_peer_found:
                                    self.on_peer
