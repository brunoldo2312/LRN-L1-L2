"""
p2p_relay.py — Conexão indireta via nós intermediários
Se A não consegue conectar direto em B, usa C como ponte
"""

import threading
import time
import json
from typing import Dict, Set, Optional, List, Tuple
from dataclasses import dataclass, field
import uuid


@dataclass
class RelayRoute:
    route_id: str
    source_node: str
    target_node: str
    via_node: str
    created_at: float = field(default_factory=time.time)
    active: bool = True
    last_used: float = field(default_factory=time.time)


@dataclass
class PendingRequest:
    req_id: str
    requester_node: str
    target_node: str
    via_node: str
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending / active / rejected / expired


class RelayManager:
    def __init__(self, p2p_manager):
        self.p2p = p2p_manager
        self.routes: Dict[str, RelayRoute] = {}
        self.pending: Dict[str, PendingRequest] = {}
        self.peer_capabilities: Dict[str, Dict] = {}
        self.lock = threading.Lock()
        self.max_relay_hops = 2
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True
        )
        self._cleanup_thread.start()

    def announce_capabilities(self, node_id: str,
                              can_relay: bool = True,
                              bandwidth_tier: str = "medium"):
        """Anunciar se este nó pode atuar como relé"""
        with self.lock:
            self.peer_capabilities[node_id] = {
                "can_relay": can_relay,
                "bandwidth_tier": bandwidth_tier,
                "last_seen": time.time()
            }

    def find_relay_node(self, target_node: str,
                        exclude: Set[str] = None) -> Optional[str]:
        """Encontrar um nó intermediário que conheça o alvo"""
        exclude = exclude or set()
        exclude.add(target_node)

        with self.lock:
            for node_id, caps in self.peer_capabilities.items():
                if node_id in exclude:
                    continue
                if caps.get("can_relay", False):
                    if self.p2p.has_peer(target_node, via_node=node_id) or \
                       self.p2p.is_peer_connected(node_id):
                        return node_id
        return None

    def create_relay_request(self, target_node: str,
                             via_node: str) -> str:
        """Solicitar conexão via um nó intermediário"""
        req_id = str(uuid.uuid4())[:8]
        with self.lock:
            self.pending[req_id] = PendingRequest(
                req_id=req_id,
                requester_node=self.p2p.node_id,
                target_node=target_node,
                via_node=via_node
            )
        # Enviar solicitação ao nó intermediário
        payload = {
            "action": "relay_connect_request",
            "req_id": req_id,
            "target_node": target_node,
            "requester": self.p2p.node_id
        }
        self.p2p.send_to_node(via_node, json.dumps(payload))
        return req_id

    def handle_relay_request(self, from_node: str, data: dict) -> dict:
        """Recebido em um nó intermediário — encaminhar ao alvo"""
        req_id = data["req_id"]
        requester = data["requester"]
        target = data["target_node"]

        # Verificar se conhecemos o alvo
        if not self.p2p.is_peer_connected(target):
            return {
                "success": False,
                "req_id": req_id,
                "reason": "target_unreachable"
            }

        # Encaminhar solicitação ao alvo
        forward = {
            "action": "relay_connect_incoming",
            "req_id": req_id,
            "requester": requester,
            "via_node": from_node
        }
        ok = self.p2p.send_to_node(target, json.dumps(forward))

        return {
            "success": ok,
            "req_id": req_id,
            "via_node": from_node
        }

    def handle_incoming_relay(self, from_node: str, data: dict) -> dict:
        """O nó alvo recebe pedido de conexão via relé"""
        req_id = data["req_id"]
        requester = data["requester"]
        via_node = data["via_node"]

        # Aqui você pode pedir confirmação ao usuário
        # Por padrão: aceitar se vier de um nó conhecido
        auto_accept = self.p2p.is_peer_trusted(via_node)

        if auto_accept:
            route_id = str(uuid.uuid4())[:8]
            with self.lock:
                self.routes[route_id] = RelayRoute(
                    route_id=route_id,
                    source_node=requester,
                    target_node=self.p2p.node_id,
                    via_node=via_node
                )
            return {
                "success": True,
                "req_id": req_id,
                "route_id": route_id,
                "via_node": via_node
            }
        return {
            "success": False,
            "req_id": req_id,
            "reason": "rejected"
        }

    def relay_message(self, route_id: str, message: str) -> bool:
        """Enviar mensagem através de uma rota de relé estabelecida"""
        with self.lock:
            route = self.routes.get(route_id)
            if not route or not route.active:
                return False
            route.last_used = time.time()
            dest = route.target_node if route.source_node == self.p2p.node_id else route.source_node
            via = route.via_node
        payload = {
            "action": "relay_data",
            "route_id": route_id,
            "from": self.p2p.node_id,
            "to": dest,
            "data": message
        }
        return self.p2p.send_to_node(via, json.dumps(payload))

    def get_connection_path(self, target_node: str,
                            max_hops: int = None) -> List[str]:
        """Descobrir caminho: [eu → nó_intermediário → alvo]"""
        max_hops = max_hops or self.max_relay_hops
        if self.p2p.is_peer_connected(target_node):
            return [target_node]  # Direto

        via = self.find_relay_node(target_node)
        if via and max_hops >= 1:
            return [via, target_node]  # Uma ponte

        return []  # Sem caminho

    def _cleanup_loop(self):
        """Remover rotas inativas há mais de 10 minutos"""
        while True:
            time.sleep(60)
            now = time.time()
            with self.lock:
                expired = [
                    rid for rid, r in self.routes.items()
                    if now - r.last_used > 600
                ]
                for rid in expired:
                    del self.routes[rid]
                old_reqs = [
                    rid for rid, r in self.pending.items()
                    if now - r.created_at > 120
                ]
                for rid in old_reqs:
                    del self.pending[rid]

    def get_status(self) -> dict:
        with self.lock:
            return {
                "active_routes": len(self.routes),
                "pending_requests": len(self.pending),
                "known_relays": sum(
                    1 for c in self.peer_capabilities.values()
                    if c.get("can_relay", False)
                )
            }