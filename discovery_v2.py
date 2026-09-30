"""
discovery_v2.py — Descoberta de peers BRN com fallbacks
============================================================
Mecanismos (rodam em paralelo):
  1. mDNS/Bonjour  (opcional — pip install zeroconf)
  2. UDP Broadcast (255.255.255.255 + broadcast por subnet)
  3. UDP Multicast (239.255.42.99)
  4. Peers manuais (peers_manual.json + BRN_EXTRA_PEERS)

Todos os peers descobertos por qualquer mecanismo entram na mesma
estrutura e sao reportados via callback.
============================================================
"""
import os
import json
import time
import socket
import hashlib
import threading
from pathlib import Path


# ============================================================
# CONFIG
# ============================================================
MULTICAST_GROUP  = "239.255.42.99"
MULTICAST_PORT   = 50007
BROADCAST_PORT   = 50008
MDNS_SERVICE     = "_brn._udp.local."

PEERS_MANUAL_FILE = "peers_manual.json"

NETWORK_SECRET = os.environ.get("BRN_NETWORK_SECRET", "brunocoin-lan-2026")
TOKEN = hashlib.sha256(NETWORK_SECRET.encode()).hexdigest()[:8]

MULTICAST_TTL        = int(os.environ.get("BRN_MC_TTL", "2"))
ACCEPT_CROSS_SUBNET  = os.environ.get("BRN_ACCEPT_CROSS_SUBNET", "0") == "1"


# ============================================================
# UTILITARIOS DE REDE
# ============================================================
def get_all_local_ips() -> list[str]:
    """Retorna todos os IPs IPv4 locais plausiveis. Multi-metodo, sem DNS externo."""
    ips = set()

    for probe in ("10.255.255.255", "192.168.255.255", "172.16.255.255"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.2)
            s.connect((probe, 1))
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass

    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            ips.add(ip)
    except Exception:
        pass

    return sorted(ips)


def get_primary_ip() -> str:
    ips = get_all_local_ips()
    return ips[0] if ips else "127.0.0.1"


def same_subnet(ip1: str, ip2: str) -> bool:
    if ACCEPT_CROSS_SUBNET:
        return True
    try:
        return ip1.rsplit(".", 1)[0] == ip2.rsplit(".", 1)[0]
    except Exception:
        return False


# ============================================================
# PEERS MANUAIS
# ============================================================
def _load_manual_peers() -> set[str]:
    """
    Fontes:
      - peers_manual.json: ["1.2.3.4:6001", ...] ou {"peers": [...]}
      - BRN_EXTRA_PEERS="1.2.3.4:6001,5.6.7.8:6001"
    """
    peers = set()
    p = Path(PEERS_MANUAL_FILE)
    if p.exists():
        try:
            data = json.loads(p.read_text())
            if isinstance(data, list):
                peers.update(str(x).strip() for x in data if str(x).strip())
            elif isinstance(data, dict):
                peers.update(str(x).strip() for x in data.get("peers", []) if str(x).strip())
        except Exception:
            pass

    for x in os.environ.get("BRN_EXTRA_PEERS", "").split(","):
        x = x.strip()
        if x:
            peers.add(x)

    return peers


# ============================================================
# UDP DISCOVERY (multicast + broadcast)
# ============================================================
class UDPDiscovery(threading.Thread):
    """
    Um socket UDP multicast + broadcast. Envia pings periodicos
    pelos dois canais. Qualquer resposta vira um peer.
    """
    def __init__(self, tcp_port: int, node_uuid: str, on_peer, running_flag):
        super().__init__(daemon=True, name="UDP-Discovery")
        self.tcp_port = tcp_port
        self.node_uuid = node_uuid
        self.on_peer = on_peer
        self.running_flag = running_flag
        self.sock = None
        self.local_ips = get_all_local_ips()
        self._seen = {}

    def _ping_msg(self) -> bytes:
        return f"BRN_NODE_PING:{self.tcp_port}:{self.node_uuid}:{TOKEN}".encode()

    def _open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        s.bind(("", MULTICAST_PORT))

        # Entra no grupo multicast por TODAS as interfaces locais.
        # Maquinas com varias NICs (WiFi + Ethernet + VPN) precisam disso.
        joined = False
        for ip in self.local_ips:
            try:
                mreq = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton(ip)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                joined = True
            except OSError:
                continue
        if not joined:
            try:
                mreq = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton("0.0.0.0")
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError:
                pass

        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, MULTICAST_TTL)
        except OSError:
            pass
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        except OSError:
            pass
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass

        s.settimeout(2.0)
        self.sock = s

    def _recv_loop(self):
        while self.running_flag():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception:
                continue

            remote_ip = addr[0]
            if not same_subnet(remote_ip, get_primary_ip()):
                continue
            try:
                msg = data.decode("utf-8", errors="ignore")
            except Exception:
                continue

            if msg.startswith("BRN_NODE_PING:"):
                parts = msg.split(":")
                if len(parts) < 4 or parts[-1] != TOKEN:
                    continue
                try:
                    r_port = int(parts[1])
                except ValueError:
                    continue
                r_uuid = parts[2]
                if r_uuid == self.node_uuid:
                    continue
                peer = f"{remote_ip}:{r_port}"
                self._register(peer, remote_ip, r_port)
                try:
                    self.sock.sendto(
                        f"BRN_NODE_PONG:{self.tcp_port}:{self.node_uuid}:{TOKEN}".encode(),
                        addr,
                    )
                except Exception:
                    pass

            elif msg.startswith("BRN_NODE_PONG:"):
                parts = msg.split(":")
                if len(parts) < 4 or parts[-1] != TOKEN:
                    continue
                try:
                    r_port = int(parts[1])
                except ValueError:
                    continue
                r_uuid = parts[2]
                if r_uuid == self.node_uuid:
                    continue
                peer = f"{remote_ip}:{r_port}"
                self._register(peer, remote_ip, r_port)

    def _send_loop(self):
        while self.running_flag():
            ping = self._ping_msg()
            try:
                self.sock.sendto(ping, (MULTICAST_GROUP, MULTICAST_PORT))
            except Exception:
                pass
            try:
                self.sock.sendto(ping, ("255.255.255.255", MULTICAST_PORT))
            except Exception:
                pass
            for ip in self.local_ips:
                try:
                    bcast = ip.rsplit(".", 1)[0] + ".255"
                    self.sock.sendto(ping, (bcast, MULTICAST_PORT))
                except Exception:
                    pass

            n = len(self._seen)
            interval = 2.0 if n < 2 else (10.0 if n < 5 else 30.0)
            time.sleep(interval)

    def _register(self, peer: str, ip: str, port: int):
        now = time.time()
        fresh = peer not in self._seen or (now - self._seen[peer]) > 30
        self._seen[peer] = now
        if fresh:
            try:
                self.on_peer(ip, port)
            except Exception:
                pass

    def run(self):
        try:
            self._open()
        except Exception as e:
            print(f"[UDP-Discovery] falha ao abrir socket: {e}")
            return
        print(f"[UDP-Discovery] ativo | IPs={self.local_ips} "
              f"| mc_ttl={MULTICAST_TTL} | cross_subnet={ACCEPT_CROSS_SUBNET}")
        t = threading.Thread(target=self._send_loop, daemon=True, name="UDP-Send")
        t.start()
        self._recv_loop()

    def stop(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass


# ============================================================
# mDNS (Zeroconf) — opcional
# ============================================================
class MDNSDiscovery(threading.Thread):
    """Usa zeroconf se instalado. Funciona onde multicast UDP puro falha."""
    def __init__(self, tcp_port: int, on_peer, running_flag, node_uuid: str = ""):
        super().__init__(daemon=True, name="mDNS-Discovery")
        self.tcp_port = tcp_port
        self.on_peer = on_peer
        self.running_flag = running_flag
        self.node_uuid = node_uuid
        self.zeroconf = None
        self.info = None

    def run(self):
        try:
            from zeroconf import ServiceInfo, Zeroconf, ServiceBrowser, ServiceListener
        except ImportError:
            print("[mDNS] lib 'zeroconf' nao instalada — pulando. "
                  "Instale com: pip install zeroconf")
            return

        from socket import inet_aton
        ip = get_primary_ip()

        class _Listener(ServiceListener):
            def __init__(self, on_peer):
                self.on_peer = on_peer

            def _handle(self, zc, type_, name):
                info = zc.get_service_info(type_, name)
                if not info:
                    return
                for a in info.parsed_addresses():
                    if a.startswith("127."):
                        continue
                    try:
                        self.on_peer(a, info.port)
                    except Exception:
                        pass

            def add_service(self, zc, type_, name):
                self._handle(zc, type_, name)

            def update_service(self, zc, type_, name):
                self._handle(zc, type_, name)

            def remove_service(self, zc, type_, name):
                pass

        try:
            self.zeroconf = Zeroconf()
            self.info = ServiceInfo(
                MDNS_SERVICE,
                f"BRN-{socket.gethostname()}.{MDNS_SERVICE}",
                addresses=[inet_aton(ip)],
                port=self.tcp_port,
                properties={"uuid": self.node_uuid},
                server=f"{socket.gethostname()}.local.",
            )
            self.zeroconf.register_service(self.info)
            ServiceBrowser(self.zeroconf, MDNS_SERVICE, _Listener(self.on_peer))
            print(f"[mDNS] registrado como {socket.gethostname()}.local:{self.tcp_port}")
            while self.running_flag():
                time.sleep(1)
        except Exception as e:
            print(f"[mDNS] erro: {e}")
        finally:
            try:
                if self.zeroconf and self.info:
                    self.zeroconf.unregister_service(self.info)
                if self.zeroconf:
                    self.zeroconf.close()
            except Exception:
                pass

    def stop(self):
        pass


# ============================================================
# LIVENESS — antes de remover peer, pinga
# ============================================================
class PeerLiveness(threading.Thread):
    """
    A cada 30s pinga cada peer conhecido. Apos 3 falhas consecutivas,
    chama remove_peer. Nao remove o peer do banco — so avisa o manager.
    """
    def __init__(self, get_peers, ping_peer, remove_peer, running_flag):
        super().__init__(daemon=True, name="PeerLiveness")
        self.get_peers = get_peers
        self.ping_peer = ping_peer
        self.remove_peer = remove_peer
        self.running_flag = running_flag
        self._misses = {}

    def run(self):
        while self.running_flag():
            time.sleep(30)
            try:
                peers = list(self.get_peers())
            except Exception:
                continue
            for p in peers:
                try:
                    ip, port = p.rsplit(":", 1)
                    ok = self.ping_peer(ip, int(port))
                except Exception:
                    ok = False
                if ok:
                    self._misses[p] = 0
                else:
                    self._misses[p] = self._misses.get(p, 0) + 1
                    if self._misses[p] >= 3:
                        try:
                            self.remove_peer(p)
                        except Exception:
                            pass
                        self._misses.pop(p, None)