"""
sync_manager.py — Sincronização de blocos entre nós BRN
- Lote de blocos (mais rápido)
- Verificação de trabalho acumulado
- Modo SPV (cabeçalhos apenas)
- Seleção da cadeia com mais trabalho
"""

import time
import threading
from typing import List, Dict, Optional, Tuple, Set
from dataclasses import dataclass, field
import json


@dataclass
class PeerStatus:
    node_id: str
    ip: str
    port: int
    height: int = 0
    cumulative_work: int = 0
    last_seen: float = field(default_factory=time.time)
    latency_ms: float = 0.0


@dataclass
class SyncStatus:
    mode: str = "full"  # "full" ou "spv"
    local_height: int = 0
    target_height: int = 0
    synced: bool = False
    syncing: bool = False
    progress: float = 0.0
    active_peer: Optional[str] = None
    peers_known: int = 0
    peers_synced: int = 0


class ChainSyncManager:
    def __init__(self, blockchain, db, p2p_manager):
        self.chain = blockchain
        self.db = db
        self.p2p = p2p_manager
        self.status = SyncStatus()
        self.peers: Dict[str, PeerStatus] = {}
        self.peer_lock = threading.Lock()
        self.sync_lock = threading.Lock()
        self.batch_size = 50  # Blocos por lote
        self.spv_headers_only = False
        self._stop_sync = threading.Event()
        self.sync_thread: Optional[threading.Thread] = None

    def set_sync_mode(self, mode: str = "full"):
        """Escolher entre nó completo ou nó leve (SPV)"""
        if mode not in ("full", "spv"):
            raise ValueError("Modo inválido: use 'full' ou 'spv'")
        self.spv_headers_only = (mode == "spv")
        self.status.mode = mode
        print(f"[Sync] Modo de sincronização: {mode.upper()}")

    def register_peer(self, node_id: str, ip: str, port: int,
                      height: int = 0, cumulative_work: int = 0):
        """Registrar/atualizar status de um peer"""
        with self.peer_lock:
            if node_id in self.peers:
                self.peers[node_id].height = height
                self.peers[node_id].cumulative_work = cumulative_work
                self.peers[node_id].last_seen = time.time()
            else:
                self.peers[node_id] = PeerStatus(
                    node_id=node_id, ip=ip, port=port,
                    height=height, cumulative_work=cumulative_work
                )
            self.status.peers_known = len(self.peers)

    def get_best_peer(self) -> Optional[PeerStatus]:
        """Escolher peer com maior trabalho acumulado"""
        with self.peer_lock:
            if not self.peers:
                return None
            best = max(self.peers.values(), key=lambda p: p.cumulative_work)
            if best.height <= self.status.local_height:
                return None
            return best

    def select_best_chain_peer(self) -> Optional[PeerStatus]:
        """Encontrar peer que oferece a melhor cadeia"""
        best_peer = None
        best_work = -1
        with self.peer_lock:
            for peer in self.peers.values():
                if peer.cumulative_work > best_work:
                    best_work = peer.cumulative_work
                    best_peer = peer
        return best_peer

    def request_block_batch(self, peer: PeerStatus,
                            start_height: int, count: int) -> List[Dict]:
        """Solicitar lote de blocos a um peer"""
        try:
            payload = {
                "action": "get_blocks",
                "start": start_height,
                "count": count,
                "headers_only": self.spv_headers_only
            }
            resp = self.p2p.send_to_peer(
                peer.ip, peer.port, json.dumps(payload), timeout=15
            )
            if not resp:
                return []
            data = json.loads(resp)
            return data.get("blocks", [])
        except Exception as e:
            print(f"[Sync] Erro ao buscar blocos de {peer.node_id}: {e}")
            return []

    def apply_block_batch(self, blocks: List[Dict]) -> Tuple[int, int]:
        """Aplicar lote de blocos recebidos"""
        applied = 0
        failed = 0
        for block_data in blocks:
            try:
                if self.spv_headers_only:
                    # SPV: salvar apenas cabeçalhos
                    self.db.save_header(
                        block_data["height"],
                        block_data["hash"],
                        block_data["prev_hash"],
                        block_data["merkle_root"],
                        block_data["timestamp"]
                    )
                else:
                    # Completo: validar e inserir bloco inteiro
                    if self.chain.validate_block(block_data):
                        self.chain.add_block(block_data, broadcast=False)
                applied += 1
            except Exception as e:
                print(f"[Sync] Falha ao aplicar bloco: {e}")
                failed += 1
        return applied, failed

    def sync_with_peer(self, peer: PeerStatus) -> bool:
        """Sincronizar com um peer específico"""
        local_h = self.chain.get_latest_height()
        self.status.local_height = local_h
        self.status.target_height = peer.height

        if local_h >= peer.height:
            self.status.synced = True
            return True

        print(f"[Sync] Iniciando sincronização com {peer.node_id}")
        print(f"[Sync] Local: {local_h} | Remoto: {peer.height}")

        current = local_h + 1
        total_blocks = peer.height - local_h

        while current <= peer.height and not self._stop_sync.is_set():
            end = min(current + self.batch_size - 1, peer.height)
            count = end - current + 1

            blocks = self.request_block_batch(peer, current, count)
            if not blocks:
                print(f"[Sync] Sem resposta no lote {current}..{end}")
                time.sleep(2)
                continue

            applied, failed = self.apply_block_batch(blocks)
            self.status.local_height = current + applied - 1
            self.status.progress = (
                self.status.local_height / self.status.target_height * 100
            )

            print(f"[Sync] {self.status.local_height}/{peer.height} "
                  f"({self.status.progress:.1f}%) — "
                  f"+{applied} blocos, {failed} falhas")

            current = end + 1
            time.sleep(0.05)

        if not self._stop_sync.is_set():
            self.status.synced = (
                self.status.local_height >= self.status.target_height
            )
            print(f"[Sync] {'✅ Sincronizado!' if self.status.synced else '⚠️ Incompleto'}")
        return self.status.synced

    def start_sync_loop(self):
        """Loop contínuo de sincronização em background"""
        if self.sync_lock.locked():
            return
        self.sync_lock.acquire()
        self._stop_sync.clear()
        self.status.syncing = True

        def _loop():
            try:
                while not self._stop_sync.is_set():
                    best = self.select_best_chain_peer()
                    if best and best.height > self.status.local_height:
                        self.status.active_peer = best.node_id
                        self.sync_with_peer(best)
                    else:
                        self.status.synced = True
                    time.sleep(5)
            finally:
                self.status.syncing = False
                self.status.active_peer = None
                self.sync_lock.release()

        self.sync_thread = threading.Thread(target=_loop, daemon=True)
        self.sync_thread.start()

    def stop(self):
        """Parar sincronização"""
        self._stop_sync.set()
        if self.sync_thread:
            self.sync_thread.join(timeout=5)

    def get_status(self) -> Dict:
        """Retornar status atual para API/UI"""
        return {
            "mode": self.status.mode,
            "local_height": self.status.local_height,
            "target_height": self.status.target_height,
            "synced": self.status.synced,
            "syncing": self.status.syncing,
            "progress": round(self.status.progress, 2),
            "active_peer": self.status.active_peer,
            "peers_known": self.status.peers_known,
            "peers_synced": self.status.peers_synced
        }