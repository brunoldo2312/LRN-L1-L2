"""
p2p_unified.py — BRN P2P Network v6.0
============================================================
Novidades v6.0:
  - Handshake autenticado (X25519 + Ed25519) em toda conexao TCP.
  - Canal cifrado por sessao (ChaCha20-Poly1305).
  - TOFU store de pubkeys (peer_pubkeys.json).
  - TrackerClient integrado (tracker_client.py).
  - UDPDiscovery + MDNSDiscovery + PeerLiveness (discovery_v2.py).
  - Node ID Ed25519 obrigatorio (vem do main.py).

Herdado da v5.1:
  - Sync por cumulative_work
  - accept_block (valida PoW/merkle)
  - Peer scoring + Rate limit
  - Bootstrap + PEX + Tracker + GitHub
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
from collections import defaultdict
from pathlib import Path

# v6: E2P
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from p2p_secure import (
    handshake_client, handshake_server, SecureChannel, MAX_FRAME,
)


# ============================================================
# CONFIGURACAO
# ============================================================
MULTICAST_GROUP = "239.255.42.99"
MULTICAST_PORT  = 50007
TCP_PORT_DEFAULT = 6001
DISCOVERY_INTERVAL_MIN = 2.0
DISCOVERY_INTERVAL_MAX = 30.0
PEER_TIMEOUT    = 60
PEER_CLEANUP_S  = 30
MAX_MSG_SIZE    = 2 * 1024 * 1024
PROTOCOL_VERSION = "BRN6/1.0"

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

# ---- GitHub peer discovery ----
GH_USER     = os.environ.get("BRN_GH_USER", "").strip()
GH_REPO     = os.environ.get("BRN_GH_REPO", "brn-peers").strip()
GH_TOKEN    = os.environ.get("BRN_GH_TOKEN", "").strip()
GH_BRANCH   = os.environ.get("BRN_GH_BRANCH", "main").strip()
GH_FILE     = os.environ.get("BRN_GH_FILE", "peers.json").strip()
GH_INTERVAL = int(os.environ.get("BRN_GH_INTERVAL", "180"))
GH_TTL      = 600
GH_API      = "https://api.github.com"

# ---- Tracker ----
TRACKER_URL       = os.environ.get("BRN_TRACKER", "").rstrip("/")
TRACKER_INTERVAL  = 120
BOOTSTRAP_PING_INTERVAL = 60

# ---- v6.0: configs ----
READ_ONLY          = os.environ.get("BRN_READ_ONLY", "0") == "1"
SYNC_BATCH_SIZE    = int(os.environ.get("BRN_SYNC_BATCH", "100"))
SYNC_RETRY_MAX     = int(os.environ.get("BRN_SYNC_RETRY_MAX", "5"))
SYNC_RETRY_BASE_S  = float(os.environ.get("BRN_SYNC_RETRY_BASE", "1.0"))
SYNC_RETRY_MAX_S   = float(os.environ.get("BRN_SYNC_RETRY_MAX_S", "300.0"))
SYNC_DEEP_FALLBACK = int(os.environ.get("BRN_SYNC_DEEP_FALLBACK", "200"))


# ============================================================
# v6: TOFU STORE
# ============================================================
PEER_PUBKEYS_FILE = "peer_pubkeys.json"
_pubkey_lock = threading.Lock()


def _load_peer_pubkeys() -> dict:
    try:
        with open(PEER_PUBKEYS_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_peer_pubkeys(d: dict) -> None:
    try:
        tmp = PEER_PUBKEYS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f, indent=2, sort_keys=True)
        os.replace(tmp, PEER_PUBKEYS_FILE)
    except Exception:
        pass


def _get_expected_pubkey(addr: str) -> str | None:
    with _pubkey_lock:
        return _load_peer_pubkeys().get(addr)


def _remember_peer_pubkey(addr: str, pub_hex: str) -> None:
    with _pubkey_lock:
        d = _load_peer_pubkeys()
        if addr not in d:
            d[addr] = pub_hex
            _save_peer_pubkeys(d)


# ============================================================
# UTILIDADES
# ============================================================
def _obter_ou_criar_uuid():
    port = os.environ.get("BRN_P2P_PORT", str(TCP_PORT_DEFAULT))
    uuid_file = f"node_uuid_{port}.txt"
    if os.path.exists(uuid_file):
        try:
            with open(uuid_file) as f:
                val = f.read().strip()
                if val:
                    return val
        except Exception:
            pass
    novo = str(uuid.uuid4())
    try:
        with open(uuid_file, "w") as f:
            f.write(novo)
        print(f"[P2P] UUID criado: {uuid_file} -> {novo[:8]}")
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
    for url in ("https://ifconfig.me/ip", "https://api.ipify.org", "https://icanhazip.com"):
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
# BACKOFF EXPONENCIAL
# ============================================================
class ExponentialBackoff:
    def __init__(self, base=1.0, max_s=300.0, jitter=0.1):
        self.base = base
        self.max_s = max_s
        self.jitter = jitter
        self.attempts = 0

    def next_sleep(self):
        self.attempts += 1
        exp = self.base * (2 ** (self.attempts - 1))
        exp = min(exp, self.max_s)
        import random
        jit = exp * self.jitter * (random.random() * 2 - 1)
        return max(0.1, exp + jit)

    def reset(self):
        self.attempts = 0

    def give_up(self, max_attempts):
        return self.attempts >= max_attempts


# ============================================================
# METRICAS
# ============================================================
class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.start_time = time.time()
        self.data = {
            "blocks_received": 0,
            "blocks_accepted": 0,
            "blocks_rejected": 0,
            "txs_received": 0,
            "txs_accepted": 0,
            "txs_rejected": 0,
            "bytes_in": 0,
            "bytes_out": 0,
            "messages_in": 0,
            "messages_out": 0,
            "peers_total_seen": 0,
            "sync_runs": 0,
            "sync_success": 0,
            "sync_failed": 0,
            "sync_blocks_applied": 0,
            "last_sync_duration_ms": 0.0,
            "last_sync_height": 0,
        }
        self.peer_metrics = defaultdict(lambda: {
            "last_seen": 0,
            "latency_ms": 0.0,
            "blocks_contributed": 0,
            "tokens_sent": 0,
            "errors": 0,
        })

    def inc(self, key, n=1):
        with self._lock:
            if key in self.data:
                self.data[key] += n

    def set(self, key, val):
        with self._lock:
            self.data[key] = val

    def peer_update(self, addr, **kwargs):
        with self._lock:
            for k, v in kwargs.items():
                if k in self.peer_metrics[addr]:
                    self.peer_metrics[addr][k] = v
            self.peer_metrics[addr]["last_seen"] = time.time()

    def peer_add_tokens(self, addr, n):
        with self._lock:
            self.peer_metrics[addr]["tokens_sent"] += n

    def snapshot(self):
        with self._lock:
            d = dict(self.data)
            d["uptime_s"] = round(time.time() - self.start_time, 1)
            d["peers_tracked"] = len(self.peer_metrics)
            d["peers_active_5min"] = sum(
                1 for p in self.peer_metrics.values()
                if time.time() - p["last_seen"] < 300
            )
            top_peers = sorted(
                self.peer_metrics.items(),
                key=lambda kv: kv[1]["blocks_contributed"],
                reverse=True
            )[:10]
            d["top_peers"] = [
                {"addr": a, "blocks": p["blocks_contributed"],
                 "latency_ms": round(p["latency_ms"], 1),
                 "last_seen_s": round(time.time() - p["last_seen"], 1)}
                for a, p in top_peers
            ]
            return d


# ============================================================
# GITHUB
# ============================================================
def _gh_headers():
    h = {"Accept": "application/vnd.github.v3+json", "User-Agent": "brn-node/1.0"}
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
    except urllib.error.HTTPError:
        return {}
    except Exception:
        return {}


def _gh_escrever_peers(peers):
    if READ_ONLY:
        return False
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
            return False
    except Exception:
        return False

    conteudo_b64 = base64.b64encode(
        json.dumps(peers, indent=2, sort_keys=True).encode()
    ).decode()
    body = {"message": "brn: update peers", "content": conteudo_b64, "branch": GH_BRANCH}
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
    except Exception as e:
        print(f"[GitHub] erro ao escrever: {e}")
        return False


# ============================================================
# TRACKER (helpers antigos mantidos para compat)
# ============================================================
def _anunciar_no_tracker(addr):
    if READ_ONLY or not TRACKER_URL:
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
            req = urllib.request.Request(self.control_url, data=body.encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status == 200
        except Exception:
            return False

    def add_port_mapping(self, ext_port, int_port, int_ip, description="BRN Node", protocol="TCP"):
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
            req = urllib.request.Request(self.control_url, data=body.encode(), headers=headers)
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
# SERVIDOR TCP (com handshake autenticado)
# ============================================================
class P2PServer(threading.Thread):
    def __init__(self, blockchain, port, metrics, node_id_priv,
                 on_new_block=None, on_new_tx=None,
                 on_peer_bad=None, on_peer_good=None, get_peers_callback=None):
        super().__init__(daemon=True, name="P2P-Server")
        self.bc = blockchain
        self.port = port
        self.metrics = metrics
        self.node_id_priv = node_id_priv
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
                threading.Thread(target=self._handle_conn, args=(conn, addr), daemon=True).start()
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"[P2P] Erro no accept: {e}")

    def _handle_conn(self, conn, addr):
        peer_ip = addr[0]
        peer_addr = f"{peer_ip}:{addr[1]}"
        try:
            if not self._check_rate(peer_ip):
                return
            if self.bc.db.get_peer_score(peer_ip) <= PEER_SCORE_BAN_THRESHOLD:
                return

            conn.settimeout(15)

            try:
                channel, peer_pub_hex = handshake_server(conn, self.node_id_priv)
            except Exception as e:
                self.metrics.peer_update(peer_addr, errors=1)
                print(f"[P2P] Handshake falhou com {peer_addr}: {e}")
                return

            known = _get_expected_pubkey(peer_addr)
            if known is None:
                _remember_peer_pubkey(peer_addr, peer_pub_hex)
            elif known != peer_pub_hex:
                print(f"[P2P] ALERTA: {peer_addr} mudou de identidade. "
                      f"Conhecida={known[:16]}... Nova={peer_pub_hex[:16]}...")
                return

            raw = channel.recv(conn)
            if len(raw) > MAX_MSG_SIZE:
                raise ValueError(f"payload maior que MAX_MSG_SIZE: {len(raw)}")
            self.metrics.inc("messages_in")
            self.metrics.inc("bytes_in", len(raw))
            msg = json.loads(raw.decode())

            response = self._process_message(msg, addr)
            if response:
                payload = json.dumps(response).encode()
                channel.send(conn, payload)
                self.metrics.inc("messages_out")
                self.metrics.inc("bytes_out", len(payload))

        except Exception as e:
            self.metrics.peer_update(peer_addr, errors=1)
            print(f"[P2P] Erro com {peer_addr}: {e}")
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
                end = min(end, start + SYNC_BATCH_SIZE)
                blocks = self.bc.db.get_blocks_range(start, end)
                self.metrics.peer_add_tokens(peer_ip, len(blocks))
                return {"type": "blocks_range", "blocks": blocks}
            if mtype == "new_block":
                if READ_ONLY:
                    return {"type": "ack", "ok": False, "reason": "read_only"}
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
                tx_dict = msg.get("txool"})

   ")
                if tx_dict and self.on_new_tx:
                    ok = self.on_new_tx(tx_dict)
                    if ok and self.on_peer_good:
                        self.on_peer_good(peer_ip)
                    return {"type": "ack", "ok": bool(ok)}
                return {"type": "ack", "ok": False}
            if mtype == "get_mempool":
                return {"type": "mempool", "txs": self.bc.db.all_mempool(limit=200)}
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
# CLIENTE TCP (com handshake autenticado)
# ============================================================
class P2PClient:
    _node_id_priv: Ed25519PrivateKey | None = None

    @classmethod
    def set_identity(cls, node_id_priv: Ed25519PrivateKey):
        cls._node_id_priv = node_id_priv

    @classmethod
    def send_message(cls, ip, port, message, timeout=5.0):
        if cls._node_id_priv is None:
            raise RuntimeError("P2PClient.set_identity() nunca foi chamado")

        addr = f"{ip}:{port}"
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            t0 = time.time()
            s.connect((ip, port))

            expected = _get_expected_pubkey(addr)
            try:
                channel, peer_pub_hex = handshake_client(
                    s, cls._node_id_priv, expected
                )
            except Exception as e:
                s.close()
                print(f"[P2P] Handshake client->{addr} falhou: {e}")
                return None, 0.0

            if expected is None:
                _remember_peer_pubkey(addr, peer_pub_hex)

            channel.send(s, json.dumps(message).encode())
            raw = channel.recv(s)
            latency_ms = (time.time() - t0) * 1000
            s.close()
            return json.loads(raw.decode()), latency_ms
        except Exception as e:
            return None, 0.0

    @classmethod
    def ping(cls, ip, port):
        return cls.send_message(ip, port, {"type": "ping"})

    @classmethod
    def get_chain_height(cls, ip, port):
        return cls.send_message(ip, port, {"type": "get_chain_height"})

    @classmethod
    def get_blocks_range(cls, ip, port, s, e):
        return cls.send_message(ip, port, {"type": "get_blocks_range", "start": s, "end": e})

    @classmethod
    def send_block(cls, ip, port, block):
        return cls.send_message(ip, port, {"type": "new_block", "block": block})

    @classmethod
    def send_tx(cls, ip, port, tx):
        return cls.send_message(ip, port, {"type": "new_tx", "tx": tx})

    @classmethod
    def get_mempool(cls, ip, port):
        return cls.send_message(ip, port, {"type": "get_memp @classmethod
    def get_peers(cls, ip, port):
        return cls.send_message(ip, port, {"type": "get_peers"})


# ============================================================
# DESCOBERTA (multicast legado + GitHub + Bootstrap)
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
                    self.client_socket.sendto(msg.encode("utf-8"), (MULTICAST_GROUP, MULTICAST_PORT))
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
                            resp, _ = P2PClient.ping(ip, port)
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

    def _github_loop(self):
        if not (GH_USER and GH_REPO):
            return
        if not GH_TOKEN:
            print("[GitHub] BRN_GH_TOKEN vazio - so leitura")
        if READ_ONLY:
            print("[GitHub] modo READ_ONLY — nao publica")
        print(f"[GitHub] Loop iniciado - {GH_USER}/{GH_REPO}/{GH_FILE} @ {GH_BRANCH}")

        public_ip = _get_public_ip()
        if not public_ip:
            print("[GitHub] Nao consegui descobrir IP publico - abortando")
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
                                    self.on_peer_found(ip, int(port))
                            except Exception:
                                pass

                if not READ_ONLY:
                    peers[meu_addr] = {
                        "ts": int(time.time()),
                        "h": self.bc.db.height() if self.bc else 0,
                        "id": self.node_uuid[:8],
                    }
                    _gh_escrever_peers(peers)
                self._salvar_peers()
            except Exception as e:
                print(f"[GitHub] erro: {e}")
            time.sleep(GH_INTERVAL)

    def _limpar_peers_mortos(self):
        while self.running:
            try:
                time.sleep(PEER_CLEANUP_S)
                agora = time.time()
                with self.peers_lock:
                    for addr, ts in list(self.discovered_peers.items()):
                        if ts > 0 and (agora - ts) > PEER_TIMEOUT:
                            del self.discovered_peers[addr]
                self._salvar_peers()
            except Exception:
                pass

    def run(self):
        if self.running:
            return
        self.running = True
        threads = [
            threading.Thread(target=self._start_server, daemon=True, name="Disc-UDP"),
            threading.Thread(target=self._start_client, daemon=True, name="Disc-UDP-Client"),
            threading.Thread(target=self._limpar_peers_mortos, daemon=True, name="Disc-Cleanup"),
            threading.Thread(target=self._ping_bootstrap_loop, daemon=True, name="Disc-Bootstrap"),
        ]
        if GH_USER and GH_REPO:
            threads.append(threading.Thread(target=self._github_loop, daemon=True, name="Disc-GitHub"))
        for t in threads:
            t.start()
            self._threads.append(t)

    def stop(self):
        if not self.running:
            return
        self.running = False
        try:
            if self.client_socket:
                bye = f"BRN_NODE_BYE:{self.tcp_port}:{self.node_uuid}:{TOKEN_ESPERADO}"
                self.client_socket.sendto(bye.encode("utf-8"), (MULTICAST_GROUP, MULTICAST_PORT))
        except Exception:
            pass
        for sock in (self.server_socket, self.client_socket):
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
        self._salvar_peers()

    def listar_peers(self):
        with self.peers_lock:
            return list(self.discovered_peers.keys())

    def listar_peers_com_altura(self):
        out = []
        with self.peers_lock:
            peers = list(self.discovered_peers.keys())
        for p in peers[:50]:
            try:
                ip, port = p.split(":")
                resp, latency = P2PClient.get_chain_height(ip, int(port))
                if resp:
                    out.append({
                        "address": p,
                        "height": resp.get("height", 0),
                        "work": resp.get("work", 0),
                        "latency_ms": round(latency, 1),
                    })
            except Exception:
                continue
        return out


# ============================================================
# MANAGER v6
# ============================================================
class P2PManager:
    def __init__(self, blockchain, node_id_priv,
                 tcp_port=TCP_PORT_DEFAULT, enable_upnp=True):
        self.bc = blockchain
        self.node_id_priv = node_id_priv
        self.tcp_port = tcp_port
        self.enable_upnp = enable_upnp
        self.read_only = READ_ONLY
        self.node_uuid = _obter_ou_criar_uuid()
        self.metrics = Metrics()
        self.external_ip = None
        self.upnp = None

        import threading as _th
        self._shutdown = _th.Event()

        P2PClient.set_identity(node_id_priv)

        self._backoffs = defaultdict(lambda: ExponentialBackoff(
            base=SYNC_RETRY_BASE_S, max_s=SYNC_RETRY_MAX_S
        ))
        self._backoff_lock = threading.Lock()

        self.server = P2PServer(
            blockchain, tcp_port, self.metrics, node_id_priv,
            on_new_block=self._on_new_block,
            on_new_tx=self._on_new_tx,
            on_peer_bad=self._on_peer_bad,
            on_peer_good=self._on_peer_good,
            get_peers_callback=self._get_peers_for_pex,
        )
        self.discovery = PeerDiscovery(
            tcp_port, self.node_uuid,
            on_peer_found=self._on_peer_found,
            blockchain=blockchain,
        )
        if enable_upnp and not READ_ONLY:
            threading.Thread(target=self._setup_upnp, daemon=True, name="UPnP").start()

    def start(self):
        mode = "READ-ONLY" if self.read_only else "FULL"
        print(f"[P2P] Manager iniciado (node_id={self.node_uuid[:8]}) | modo={mode}")

        # 1) Servidor TCP
        self.server.start()

        # 2) Descoberta multicast classica
        self.discovery.run()

        # 3) Tracker (cross-subnet, cross-rede)
        from tracker_client import TrackerClient, has_tracker
        if has_tracker():
            node_pub = ""
            try:
                node_pub = self.node_id_priv.public_key().public_bytes_raw().hex()
            except Exception:
                pass

            def _state():
                return {
                    "height": self.bc.db.height(),
                    "work": self.bc.cumulative_work(),
                    "read_only": self.read_only,
                }

            self._tracker = TrackerClient(
                get_state=_state,
                on_peer=self._on_peer_found,
                running_flag=lambda: not self._shutdown.is_set(),
                on_peer_dead=self._on_peer_dead,
                node_id_pub_hex=node_pub,
            )
            self._tracker.start()
        else:
            self._tracker = None
            print("[P2P] Tracker nao configurado (BRN_TRACKER vazio)")

        # 4) Descoberta reforcada (UDP + mDNS + manuais)
        from discovery_v2 import (
            UDPDiscovery, MDNSDiscovery, PeerLiveness, _load_manual_peers,
        )

        def _running():
            return not self._shutdown.is_set()

        self._udp_disc = UDPDiscovery(
            tcp_port=self.tcp_port,
            node_uuid=self.node_uuid,
            on_peer=self._on_peer_found,
            running_flag=_running,
        )
        self._udp_disc.start()

        self._mdns_disc = MDNSDiscovery(
            tcp_port=self.tcp_port,
            on_peer=self._on_peer_found,
            running_flag=_running,
            node_uuid=self.node_uuid,
        )
        self._mdns_disc.start()

        # 5) Peers manuais
        for p in _load_manual_peers():
            try:
                ip, port = p.rsplit(":", 1)
                print(f"[Manual] conectando em {p}")
                self._on_peer_found(ip, int(port))
            except Exception:
                pass

        # 6) Liveness
        self._liveness = PeerLiveness(
            get_peers=lambda: self.discovery.listar_peers(),
            ping_peer=lambda ip, port: P2PClient.ping(ip, port)[0] is not None,
            remove_peer=self._on_peer_dead,
            running_flag=_running,
        )
        self._liveness.start()

    def stop(self):
        try:
            self._shutdown.set()
        except Exception:
            pass
        self.server.stop()
        self.discovery.stop()
        if getattr(self, "_tracker", None):
            try: self._tracker.stop()
            except Exception: pass
        if hasattr(self, "_udp_disc"):
            try: self._udp_disc.stop()
            except Exception: pass
        if hasattr(self, "_mdns_disc"):
            try: self._mdns_disc.stop()
            except Exception: pass
        if self.upnp and self.external_ip:
            try:
                self.upnp.delete_port_mapping(self.tcp_port, "TCP")
            except Exception:
                pass

    def _setup_upnp(self):
        try:
            self.upnp = UPnPClient()
            if not self.upnp.control_url:
                print("[UPnP] Roteador nao suporta")
                return
            local_ip = _get_local_ip()
            ok = self.upnp.add_port_mapping(
                self.tcp_port, self.tcp_port, local_ip, description="BRN Node"
            )
            if ok:
                self.external_ip = self.upnp.get_external_ip()
                print(f"[UPnP] Porta {self.tcp_port} mapeada. IP publico: {self.external_ip}")
        except Exception:
            pass

    def _get_peers_for_pex(self):
        try:
            return self.discovery.listar_peers_com_altura()
        except Exception:
            return []

    def _on_peer_found(self, ip, port):
        try:
            resp, latency = P2PClient.get_chain_height(ip, port)
            if not resp:
                return
            self.metrics.peer_update(f"{ip}:{port}", latency_ms=latency)
            remote_work = resp.get("work", 0)
            local_work = self.bc.cumulative_work()
            if remote_work > local_work:
                print(f"[P2P] Peer {ip}:{port} com mais work "
                      f"({remote_work} > {local_work}). Sincronizando...")
                threading.Thread(
                    target=self._sync_with_retry,
                    args=(ip, port),
                    daemon=True,
                    name=f"Sync-{ip}-{port}",
                ).start()

            peers_resp, _ = P2PClient.get_peers(ip, port)
            if peers_resp and "peers" in peers_resp:
                novos = 0
                for p in peers_resp["peers"]:
                    addr = p.get("address", "") if isinstance(p, dict) else str(p)
                    if not addr or ":" not in addr:
                        continue
                    with self.discovery.peers_lock:
                        if addr not in self.discovery.discovered_peers:
                            self.discovery.discovered_peers[addr] = 0
                            novos += 1
                if novos > 0:
                    print(f"[PEX] +{novos} peer(s) via {ip}")
                    self.discovery._salvar_peers()
        except Exception:
            pass

    def _on_peer_dead(self, peer_addr: str):
        try:
            with self.discovery.peers_lock:
                self.discovery.discovered_peers.pop(peer_addr, None)
            self.discovery._salvar_peers()
            print(f"[P2P] Peer removido: {peer_addr}")
        except Exception:
            pass

    # --------------------------------------------------------
    # SYNC
    # --------------------------------------------------------
    def _sync_with_retry(self, ip, port):
        addr = f"{ip}:{port}"
        backoff = self._backoffs[addr]
        self.metrics.inc("sync_runs")

        while True:
            try:
                ok, msg = self._sync_incremental(ip, port)
                if ok:
                    backoff.reset()
                    self.metrics.inc("sync_success")
                    return
                else:
                    if "sem mais trabalho" in msg or "nada novo" in msg:
                        self.metrics.inc("sync_success")
                        return
                    self.metrics.inc("sync_failed")
                    if backoff.give_up(SYNC_RETRY_MAX):
                        print(f"[Sync] Desistindo de {addr} apos {SYNC_RETRY_MAX} tentativas ({msg})")
                        return
                    wait = backoff.next_sleep()
                    print(f"[Sync] {addr} falhou ({msg}), retry em {wait:.1f}s "
                          f"(tentativa {backoff.attempts}/{SYNC_RETRY_MAX})")
                    time.sleep(wait)
            except Exception as e:
                self.metrics.inc("sync_failed")
                if backoff.give_up(SYNC_RETRY_MAX):
                    print(f"[Sync] Desistindo de {addr}: {e}")
                    return
                wait = backoff.next_sleep()
                print(f"[Sync] {addr} exception ({e}), retry em {wait:.1f}s")
                time.sleep(wait)

    def _sync_incremental(self, ip, port):
        t0 = time.time()
        addr = f"{ip}:{port}"

        resp, latency = P2PClient.get_chain_height(ip, port)
        if not resp:
            return False, "peer nao respondeu"
        self.metrics.peer_update(addr, latency_ms=latency)

        remote_height = resp.get("height", -1)
        remote_work = resp.get("work", 0)

        local_height = self.bc.db.height()
        local_work = self.bc.cumulative_work()

        if remote_work <= local_work and remote_height <= local_height:
            return True, "sem mais trabalho"

        start = local_height + 1
        if start > remote_height:
            return True, "nada novo"

        print(f"[Sync] {addr}: {local_height} -> {remote_height} "
              f"({remote_height - local_height} blocos, {SYNC_BATCH_SIZE}/lote)")

        blocks_to_apply = []
        cursor = start
        while cursor <= remote_height:
            end = min(cursor + SYNC_BATCH_SIZE, remote_height + 1)
            resp, lat = P2PClient.get_blocks_range(ip, port, cursor, end)
            if not resp or "blocks" not in resp:
                return False, f"falha no lote {cursor}-{end}"
            lote = resp["blocks"]
            if not lote:
                break
            self.metrics.peer_update(addr, latency_ms=lat, tokens_sent=0)
            blocks_to_apply.extend(lote)
            cursor = end

        if not blocks_to_apply:
            return False, "nenhum bloco recebido"

        prev = self.bc.db.get_block(local_height)
        prev_hash = prev["hash"] if prev else "0" * 64
        expected_height = local_height + 1
        validos = 0
        rejeitados = 0

        from blockchain import block_hash, meets_difficulty, compute_merkle_root

        for blk in blocks_to_apply:
            try:
                if blk.get("height") != expected_height:
                    rejeitados += 1
                    break
                if blk.get("prev_hash") != prev_hash:
                    rejeitados += 1
                    break
                h_recalc = block_hash(
                    blk["prev_hash"], blk["merkle"], blk["timestamp"],
                    blk["nonce"], blk["difficulty"]
                )
                if h_recalc != blk["hash"]:
                    print(f"[Sync] BLOCO ADULTERADO #{blk['height']} (hash nao bate)")
                    rejeitados += 1
                    break
                if not meets_difficulty(blk["hash"], blk["difficulty"]):
                    print(f"[Sync] PoW invalido #{blk['height']}")
                    rejeitados += 1
                    break
                txids = [t["txid"] for t in blk["transactions"]]
                if compute_merkle_root(txids) != blk["merkle"]:
                    print(f"[Sync] merkle invalido #{blk['height']}")
                    rejeitados += 1
                    break
                prev_hash = blk["hash"]
                expected_height += 1
                validos += 1
            except Exception as e:
                print(f"[Sync] erro validando bloco: {e}")
                rejeitados += 1
                break

        if rejeitados > 0:
            self.metrics.inc("blocks_rejected", rejeitados)
            return False, f"{rejeitados} blocos rejeitados na verificacao"

        self.metrics.inc("blocks_received", len(blocks_to_apply))
        self.metrics.inc("blocks_accepted", validos)
        self.metrics.peer_update(addr, blocks_contributed=validos)

        ok, msg = self.bc.reorg_to(blocks_to_apply)
        dt_ms = (time.time() - t0) * 1000
        self.metrics.set("last_sync_duration_ms", round(dt_ms, 1))
        self.metrics.set("last_sync_height", self.bc.db.height())
        self.metrics.inc("sync_blocks_applied", validos)

        if ok:
            print(f"[Sync] {addr}: OK — +{validos} blocos em {dt_ms:.0f}ms "
                  f"(altura {self.bc.db.height()})")
        else:
            print(f"[Sync] {addr}: reorg nao aplicado ({msg})")
        return ok, msg

    def sync_with_peer(self, ip, port):
        return self._sync_incremental(ip, port)

    # --------------------------------------------------------
    # BROADCAST
    # --------------------------------------------------------
    def broadcast_block(self, block_dict):
        if self.read_only:
            return
        for peer in self.discovery.listar_peers():
            try:
                ip, port = peer.rsplit(":", 1)
                threading.Thread(
                    target=P2PClient.send_block,
                    args=(ip, int(port), block_dict),
                    daemon=True,
                ).start()
            except Exception:
                pass

    def broadcast_tx(self, tx_dict):
        for peer in self.discovery.listar_peers():
            try:
                ip, port = peer.rsplit(":", 1)
                threading.Thread(
                    target=P2PClient.send_tx,
                    args=(ip, int(port), tx_dict),
                    daemon=True,
                ).start()
            except Exception:
                pass

    # --------------------------------------------------------
    # HANDLERS
    # --------------------------------------------------------
    def _on_new_block(self, block_dict):
        try:
            h = block_dict.get("height", -1)
            if h == self.bc.db.height() + 1:
                ok, msg = self.bc.accept_block(block_dict)
                if ok:
                    self.metrics.inc("blocks_accepted")
                    print(f"[P2P] Novo bloco aceito: #{h}")
                else:
                    self.metrics.inc("blocks_rejected")
                return ok
            return False
        except Exception:
            return False

    def _on_new_tx(self, tx_dict):
        try:
            ok, msg = self.bc.submit_tx(tx_dict)
            if ok:
                self.metrics.inc("txs_accepted")
                print(f"[P2P] Nova tx: {str(tx_dict.get('txid', '?'))[:16]}")
            else:
                self.metrics.inc("txs_rejected")
            return ok
        except Exception:
            return False

    def _on_peer_bad(self, peer_ip):
        try:
            novo = self.bc.db.add_peer_score(peer_ip, PEER_SCORE_PENALTY_BAD)
            if novo <= PEER_SCORE_BAN_THRESHOLD:
                print(f"[P2P] BANINDO {peer_ip}")
                self.bc.db.remover_peer_por_endereco(peer_ip)
        except Exception:
            pass

    def _on_peer_good(self, peer_ip):
        try:
            self.bc.db.add_peer_score(peer_ip, PEER_SCORE_REWARD_GOOD)
        except Exception:
            pass

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------
    def get_status(self):
        peers = self.discovery.listar_peers()
        tracker_stats = {}
        try:
            if getattr(self, "_tracker", None):
                tracker_stats = self._tracker.stats()
        except Exception:
            pass

        return {
            "node_id": self.node_uuid,
            "port": self.tcp_port,
            "external_ip": self.external_ip,
            "peer_count": len(peers),
            "peers": peers,
            "height": self.bc.db.height(),
            "work": self.bc.cumulative_work(),
            "bootstrap": list(_carregar_bootstrap()),
            "github": f"{GH_USER}/{GH_REPO}" if GH_USER else "(desativado)",
            "tracker": TRACKER_URL or "(desativado)",
            "tracker_stats": tracker_stats,
            "read_only": self.read_only,
            "protocol": PROTOCOL_VERSION,
        }

    def get_metrics(self):
        return self.metrics.snapshot()

    def get_peer_stats(self):
        return self.discovery.listar_peers_com_altura()

    def is_read_only(self):
        return self.read_only


if __name__ == "__main__":
    print("Rode via main.py")