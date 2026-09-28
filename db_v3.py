"""
db_v3.py — Banco de dados SQLite do BRN (v3)
============================================================
Substitui db.py com:
  ✅ Índice adicional em transactions(txid) para busca rápida
  ✅ Correção do rollback_block (não deleta UTXOs de coinbase corretamente)
  ✅ Novo método get_stats() para o explorer
  ✅ Novo método vacuum() para manutenção
  ✅ Lock RLock em todas as operações de escrita
  ✅ Métodos de consulta paginada (para o explorer)
============================================================
"""

import time
import zlib
import sqlite3
import threading
import orjson


class ChainDB:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()

        for p in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA temp_store=MEMORY",
            "PRAGMA cache_size=-20000",
            "PRAGMA mmap_size=268435456",
            "PRAGMA foreign_keys=ON",
        ):
            self.conn.execute(p)

        self._schema()

    def _schema(self):
        with self.lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS blocks (
                height     INTEGER PRIMARY KEY,
                hash       TEXT UNIQUE NOT NULL,
                prev_hash  TEXT NOT NULL,
                timestamp  INTEGER NOT NULL,
                nonce      INTEGER NOT NULL,
                merkle     TEXT NOT NULL,
                difficulty INTEGER NOT NULL,
                raw        BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_blocks_hash ON blocks(hash);
            CREATE INDEX IF NOT EXISTS idx_blocks_prev ON blocks(prev_hash);

            CREATE TABLE IF NOT EXISTS transactions (
                txid         TEXT PRIMARY KEY,
                block_height INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tx_block ON transactions(block_height);

            CREATE TABLE IF NOT EXISTS utxos (
                txid         TEXT NOT NULL,
                vout         INTEGER NOT NULL,
                address      TEXT NOT NULL,
                amount       INTEGER NOT NULL,
                pubkey       TEXT NOT NULL,
                block_height INTEGER NOT NULL,
                spent        INTEGER NOT NULL DEFAULT 0,
                spent_by     TEXT,
                PRIMARY KEY (txid, vout)
            );
            CREATE INDEX IF NOT EXISTS idx_utxo_addr  ON utxos(address, spent);
            CREATE INDEX IF NOT EXISTS idx_utxo_spent ON utxos(spent);
            CREATE INDEX IF NOT EXISTS idx_utxo_h     ON utxos(block_height);

            CREATE TABLE IF NOT EXISTS mempool (
                txid        TEXT PRIMARY KEY,
                raw         BLOB NOT NULL,
                fee         INTEGER NOT NULL,
                received_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_mp_fee  ON mempool(fee DESC);
            CREATE INDEX IF NOT EXISTS idx_mp_time ON mempool(received_at);

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS peers (
                address      TEXT PRIMARY KEY NOT NULL,
                node_id      TEXT NOT NULL,
                genesis_hash TEXT NOT NULL,
                version      TEXT,
                height       INTEGER DEFAULT 0,
                is_miner     INTEGER DEFAULT 0,
                public_key   TEXT,
                metadata     TEXT,
                first_seen   INTEGER NOT NULL,
                last_seen    INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_peers_last_seen ON peers(last_seen);
            CREATE INDEX IF NOT EXISTS idx_peers_genesis   ON peers(genesis_hash);
            CREATE INDEX IF NOT EXISTS idx_peers_node_id   ON peers(node_id);

            CREATE TABLE IF NOT EXISTS network_events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    INTEGER NOT NULL,
                event_type   TEXT NOT NULL,
                peer_address TEXT,
                details      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts ON network_events(timestamp DESC);
            """)

    # ==================== BLOCOS ====================
    def add_block(self, block: dict):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE TRANSACTION")
            try:
                blob = zlib.compress(orjson.dumps(block), level=6)
                self.conn.execute(
                    "INSERT INTO blocks(height,hash,prev_hash,timestamp,nonce,merkle,difficulty,raw)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (block["height"], block["hash"], block["prev_hash"],
                     block["timestamp"], block["nonce"], block["merkle"],
                     block["difficulty"], blob),
                )
                for tx in block["transactions"]:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO transactions(txid,block_height) VALUES (?,?)",
                        (tx["txid"], block["height"]),
                    )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def accept_block_atomic(self, block: dict):
        """Aceita o bloco INTEIRO em uma única transação SQL (atômico)."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE TRANSACTION")
            try:
                blob = zlib.compress(orjson.dumps(block), level=6)
                self.conn.execute(
                    "INSERT INTO blocks(height,hash,prev_hash,timestamp,nonce,merkle,difficulty,raw)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (block["height"], block["hash"], block["prev_hash"],
                     block["timestamp"], block["nonce"], block["merkle"],
                     block["difficulty"], blob),
                )

                for i, tx in enumerate(block["transactions"]):
                    is_coinbase = (i == 0)

                    self.conn.execute(
                        "INSERT OR REPLACE INTO transactions(txid,block_height) VALUES (?,?)",
                        (tx["txid"], block["height"]),
                    )

                    if not is_coinbase:
                        for inp in tx["inputs"]:
                            self.conn.execute(
                                "UPDATE utxos SET spent=1, spent_by=? "
                                "WHERE txid=? AND vout=? AND spent=0",
                                (tx["txid"], inp["txid"], inp["vout"]),
                            )

                    for j, out in enumerate(tx["outputs"]):
                        self.conn.execute(
                            "INSERT OR REPLACE INTO utxos"
                            "(txid,vout,address,amount,pubkey,block_height,spent)"
                            " VALUES (?,?,?,?,?,?,0)",
                            (tx["txid"], j, out["address"], out["amount"],
                             out.get("pubkey", ""), block["height"]),
                        )

                    self.conn.execute("DELETE FROM mempool WHERE txid=?", (tx["txid"],))

                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def get_block(self, height: int) -> dict | None:
        row = self.conn.execute(
            "SELECT raw FROM blocks WHERE height=?", (height,)
        ).fetchone()
        return orjson.loads(zlib.decompress(row["raw"])) if row else None

    def get_block_by_hash(self, h: str) -> dict | None:
        row = self.conn.execute(
            "SELECT raw FROM blocks WHERE hash=?", (h,)
        ).fetchone()
        return orjson.loads(zlib.decompress(row["raw"])) if row else None

    def height(self) -> int:
        row = self.conn.execute("SELECT MAX(height) AS h FROM blocks").fetchone()
        return int(row["h"]) if row and row["h"] is not None else -1

    def tip(self) -> dict | None:
        row = self.conn.execute(
            "SELECT hash,prev_hash FROM blocks ORDER BY height DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def tip_hash(self) -> str:
        t = self.tip()
        return t["hash"] if t else "0" * 64

    def headers(self, start: int, limit: int = 2000) -> list[dict]:
        rows = self.conn.execute(
            "SELECT height,hash,prev_hash,timestamp,nonce,merkle,difficulty"
            " FROM blocks WHERE height>=? ORDER BY height LIMIT ?",
            (start, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_blocks(self, n: int = 10) -> list[dict]:
        """✅ NOVO: últimos N blocos (útil para o explorer)."""
        top = self.height()
        if top < 0:
            return []
        out = []
        for h in range(max(0, top - n + 1), top + 1):
            b = self.get_block(h)
            if b:
                out.append(b)
        return out

    # ==================== UTXO ====================
    def balance(self, address: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM utxos WHERE address=? AND spent=0",
            (address,),
        ).fetchone()
        return int(row["s"])

    def utxos_for(self, address: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT txid,vout,amount,pubkey FROM utxos"
            " WHERE address=? AND spent=0 ORDER BY amount DESC",
            (address,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_utxo(self, txid: str, vout: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM utxos WHERE txid=? AND vout=? AND spent=0", (txid, vout)
        ).fetchone()
        return dict(row) if row else None

    def apply_tx(self, tx: dict, height: int, coinbase: bool = False):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE TRANSACTION")
            try:
                if not coinbase:
                    for inp in tx["inputs"]:
                        self.conn.execute(
                            "UPDATE utxos SET spent=1, spent_by=? "
                            "WHERE txid=? AND vout=? AND spent=0",
                            (tx["txid"], inp["txid"], inp["vout"]),
                        )
                for i, out in enumerate(tx["outputs"]):
                    self.conn.execute(
                        "INSERT OR REPLACE INTO utxos"
                        "(txid,vout,address,amount,pubkey,block_height,spent)"
                        " VALUES (?,?,?,?,?,?,0)",
                        (tx["txid"], i, out["address"], out["amount"],
                         out.get("pubkey", ""), height),
                    )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def rollback_block(self, height: int):
        """✅ CORRIGIDO: preserva UTXOs criados por outros blocos."""
        with self.lock:
            block = self.get_block(height)
            if not block:
                return

            self.conn.execute("BEGIN IMMEDIATE TRANSACTION")
            try:
                for tx in reversed(block["transactions"]):
                    for i in range(len(tx["outputs"])):
                        self.conn.execute(
                            "DELETE FROM utxos WHERE txid=? AND vout=?",
                            (tx["txid"], i),
                        )
                    if height > 0:
                        for inp in tx.get("inputs", []):
                            if inp["txid"] == "0" * 64:
                                continue
                            self.conn.execute(
                                "UPDATE utxos SET spent=0, spent_by=NULL "
                                "WHERE txid=? AND vout=?",
                                (inp["txid"], inp["vout"]),
                            )
                    self.conn.execute(
                        "DELETE FROM transactions WHERE txid=?", (tx["txid"],)
                    )
                self.conn.execute("DELETE FROM blocks WHERE height=?", (height,))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # ==================== MEMPOOL ====================
    def add_mempool(self, tx: dict, fee: int) -> bool:
        with self.lock:
            try:
                self.conn.execute(
                    "INSERT INTO mempool(txid,raw,fee,received_at) VALUES (?,?,?,?)",
                    (tx["txid"], zlib.compress(orjson.dumps(tx)), fee, time.time()),
                )
            except sqlite3.IntegrityError:
                return False
            self._prune_mempool()
            return True

    def _prune_mempool(self, max_size: int = 1000, ttl: int = 3600):
        self.conn.execute(
            "DELETE FROM mempool WHERE received_at < ?", (time.time() - ttl,)
        )
        n = self.conn.execute("SELECT COUNT(*) AS c FROM mempool").fetchone()["c"]
        if n > max_size:
            self.conn.execute("""
                DELETE FROM mempool WHERE txid IN (
                    SELECT txid FROM mempool ORDER BY fee ASC, received_at ASC LIMIT ?
                )
            """, (n - max_size,))

    def has_mempool(self, txid: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM mempool WHERE txid=?", (txid,)
        ).fetchone() is not None

    def get_mempool_tx(self, txid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT raw FROM mempool WHERE txid=?", (txid,)
        ).fetchone()
        return orjson.loads(zlib.decompress(row["raw"])) if row else None

    def all_mempool(self, limit: int = 1000) -> list[dict]:
        rows = self.conn.execute(
            "SELECT raw FROM mempool ORDER BY fee DESC LIMIT ?", (limit,)
        ).fetchall()
        return [orjson.loads(zlib.decompress(r["raw"])) for r in rows]

    def remove_mempool(self, txid: str):
        with self.lock:
            self.conn.execute("DELETE FROM mempool WHERE txid=?", (txid,))

    def mempool_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS c FROM mempool").fetchone()["c"]

    # ==================== ESTATÍSTICAS (NOVO) ====================
    def get_stats(self) -> dict:
        """✅ NOVO: estatísticas agregadas para o explorer."""
        return {
            "height": self.height(),
            "tip_hash": self.tip_hash(),
            "utxos": self.count_utxos(),
            "mempool": self.mempool_count(),
            "blocks": self.height() + 1,
        }

    # ==================== PODA / SNAPSHOT ====================
    def prune_spent_utxos(self, keep_height: int = 1000) -> int:
        cutoff = max(0, self.height() - keep_height)
        with self.lock:
            n = self.conn.execute(
                "DELETE FROM utxos WHERE spent=1 AND block_height < ?", (cutoff,)
            ).rowcount
        return n

    def snapshot_utxo(self, path: str = "utxo_snapshot.db"):
        with self.lock:
            snap = sqlite3.connect(path)
            snap.execute("DROP TABLE IF EXISTS utxos")
            snap.execute("CREATE TABLE utxos AS SELECT * FROM utxos WHERE spent=0")
            snap.commit()
            snap.close()

    def count_utxos(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS c FROM utxos WHERE spent=0"
        ).fetchone()["c"]

    def vacuum(self):
        """✅ NOVO: compacta o arquivo SQLite (rodar após prune)."""
        with self.lock:
            self.conn.execute("VACUUM")

    # ==================== META ====================
    def set_meta(self, key: str, value: str):
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (key, value)
            )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else None

    # ==================== PEERS ====================
    def upsert_peer(self, node_id: str, address: str, genesis_hash: str,
                    version: str = "?", height: int = 0,
                    is_miner: bool = False, public_key: str = "",
                    metadata: dict | None = None) -> bool:
        with self.lock:
            agora = int(time.time())
            existente = self.conn.execute(
                "SELECT node_id FROM peers WHERE address=?", (address,)
            ).fetchone()
            novo = existente is None
            node_id_antigo = existente["node_id"] if existente else None

            self.conn.execute("""
                INSERT INTO peers
                    (address, node_id, genesis_hash, version, height,
                     is_miner, public_key, metadata, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    node_id      = excluded.node_id,
                    genesis_hash = excluded.genesis_hash,
                    version      = excluded.version,
                    height       = excluded.height,
                    is_miner     = excluded.is_miner,
                    public_key   = excluded.public_key,
                    metadata     = excluded.metadata,
                    last_seen    = excluded.last_seen
            """, (
                address, node_id, genesis_hash, version, height,
                1 if is_miner else 0, public_key,
                orjson.dumps(metadata or {}).decode(),
                agora, agora
            ))

            if novo:
                self._log_event("peer_joined", address, f"node_id={node_id[:12]}…")
            elif node_id_antigo and node_id_antigo != node_id:
                self._log_event("peer_node_changed", address,
                                f"{node_id_antigo[:12]}… → {node_id[:12]}…")

            return novo

    def listar_peers(self, apenas_ativos: bool = True, janela: int = 300) -> list[dict]:
        if apenas_ativos:
            cutoff = int(time.time()) - janela
            rows = self.conn.execute(
                "SELECT * FROM peers WHERE last_seen >= ? ORDER BY last_seen DESC",
                (cutoff,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM peers ORDER BY last_seen DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def contar_peers(self, apenas_ativos: bool = True, janela: int = 300) -> int:
        if apenas_ativos:
            cutoff = int(time.time()) - janela
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM peers WHERE last_seen >= ?", (cutoff,)
            ).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM peers").fetchone()
        return int(row["n"])

    def remover_peer(self, node_id: str):
        with self.lock:
            self.conn.execute("DELETE FROM peers WHERE node_id=?", (node_id,))

    def remover_peer_por_endereco(self, address: str):
        with self.lock:
            self.conn.execute("DELETE FROM peers WHERE address=?", (address,))

    def limpar_peers_inativos(self, janela: int = 1800):
        with self.lock:
            self.conn.execute(
                "DELETE FROM peers WHERE last_seen < ?",
                (int(time.time()) - janela,)
            )

    # ==================== EVENTOS ====================
    def _log_event(self, tipo: str, peer: str = "", details: str = ""):
        with self.lock:
            self.conn.execute(
                "INSERT INTO network_events (timestamp, event_type, peer_address, details) "
                "VALUES (?,?,?,?)",
                (int(time.time()), tipo, peer, details)
            )

    def ultimos_eventos(self, n: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM network_events ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ==================== CLOSE ====================
    def close(self):
        self.conn.close()
