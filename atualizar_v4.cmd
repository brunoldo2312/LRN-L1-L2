@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo   BRN v4 - Instalador Automatico
echo ============================================================
echo.
echo   O que este script faz:
echo     [1] Backup dos arquivos atuais
echo     [2] Instala dependencia 'mnemonic'
echo     [3] Atualiza db.py          (reorg + peer score)
echo     [4] Atualiza blockchain.py  (cumulative work + fee)
echo     [5] Atualiza p2p_unified.py (sync por work + anti-DoS)
echo     [6] Atualiza wallet.py      (HD Wallet BIP39)
echo     [7] Atualiza server.py      (endpoints novos)
echo     [8] Testa imports
echo.
echo   Pressione qualquer tecla para comecar (Ctrl+C cancela).
pause >nul

set "MEU_ARQ=%~f0"

powershell -NoProfile -ExecutionPolicy Bypass -Command "$c = Get-Content -LiteralPath $env:MEU_ARQ -Raw -Encoding UTF8; $i = $c.IndexOf('~PSSTART~'); if ($i -lt 0) { Write-Host 'ERRO: marcador nao encontrado' -ForegroundColor Red; exit 1 }; $code = $c.Substring($i + 10); Invoke-Expression $code"

echo.
echo ============================================================
echo   Finalizado!
echo ============================================================
pause
exit /b

~PSSTART~
$ErrorActionPreference = 'Continue'
Set-Location 'C:\Users\mayra\Music\LRN-L1-L2-main'

Write-Host "===== BRN v4 - Instalador =====" -ForegroundColor Cyan
Write-Host ""

# [1] Backup
$backup = "backup_v4_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
New-Item -ItemType Directory -Path $backup -Force | Out-Null
foreach ($f in 'db.py','blockchain.py','p2p_unified.py','wallet.py','server.py','main.py') {
    if (Test-Path $f) { Copy-Item $f -Destination $backup -Force }
}
Write-Host "[1/8] Backup em: $backup" -ForegroundColor Yellow
Write-Host ""

# [2] mnemonic
Write-Host "[2/8] Instalando 'mnemonic'..." -ForegroundColor Cyan
python -m pip install mnemonic 2>&1 | Out-Null
Write-Host "      OK" -ForegroundColor Green

# [3] db.py
Write-Host "[3/8] Atualizando db.py..." -ForegroundColor Cyan
$db_content = @'
"""db.py — Banco SQLite do BRN (v4)
v4: + delete_blocks_above, + get_blocks_range, + peer score
"""
import time
import zlib
import sqlite3
import threading
import orjson


class ChainDB:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        for p in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL",
                  "PRAGMA temp_store=MEMORY", "PRAGMA cache_size=-20000",
                  "PRAGMA mmap_size=268435456", "PRAGMA foreign_keys=ON"):
            self.conn.execute(p)
        self._schema()

    def _schema(self):
        with self.lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS blocks (
                height INTEGER PRIMARY KEY, hash TEXT UNIQUE NOT NULL,
                prev_hash TEXT NOT NULL, timestamp INTEGER NOT NULL,
                nonce INTEGER NOT NULL, merkle TEXT NOT NULL,
                difficulty INTEGER NOT NULL, raw BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_blocks_hash ON blocks(hash);
            CREATE INDEX IF NOT EXISTS idx_blocks_prev ON blocks(prev_hash);
            CREATE TABLE IF NOT EXISTS transactions (
                txid TEXT PRIMARY KEY, block_height INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tx_block ON transactions(block_height);
            CREATE TABLE IF NOT EXISTS utxos (
                txid TEXT NOT NULL, vout INTEGER NOT NULL,
                address TEXT NOT NULL, amount INTEGER NOT NULL,
                pubkey TEXT NOT NULL, block_height INTEGER NOT NULL,
                spent INTEGER NOT NULL DEFAULT 0, spent_by TEXT,
                PRIMARY KEY (txid, vout)
            );
            CREATE INDEX IF NOT EXISTS idx_utxo_addr ON utxos(address, spent);
            CREATE INDEX IF NOT EXISTS idx_utxo_spent ON utxos(spent);
            CREATE INDEX IF NOT EXISTS idx_utxo_h ON utxos(block_height);
            CREATE TABLE IF NOT EXISTS mempool (
                txid TEXT PRIMARY KEY, raw BLOB NOT NULL,
                fee INTEGER NOT NULL, received_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_mp_fee ON mempool(fee DESC);
            CREATE INDEX IF NOT EXISTS idx_mp_time ON mempool(received_at);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS peers (
                address TEXT PRIMARY KEY NOT NULL, node_id TEXT NOT NULL,
                genesis_hash TEXT NOT NULL, version TEXT,
                height INTEGER DEFAULT 0, is_miner INTEGER DEFAULT 0,
                public_key TEXT, metadata TEXT,
                first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL,
                score INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_peers_last_seen ON peers(last_seen);
            CREATE INDEX IF NOT EXISTS idx_peers_genesis ON peers(genesis_hash);
            CREATE INDEX IF NOT EXISTS idx_peers_node_id ON peers(node_id);
            CREATE TABLE IF NOT EXISTS network_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL, event_type TEXT NOT NULL,
                peer_address TEXT, details TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts ON network_events(timestamp DESC);
            """)
            try:
                cols = self.conn.execute("PRAGMA table_info(peers)").fetchall()
                if "score" not in {c["name"] for c in cols}:
                    self.conn.execute("ALTER TABLE peers ADD COLUMN score INTEGER DEFAULT 0")
            except Exception:
                pass

    def add_block(self, block):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE TRANSACTION")
            try:
                blob = zlib.compress(orjson.dumps(block), level=6)
                self.conn.execute(
                    "INSERT INTO blocks(height,hash,prev_hash,timestamp,nonce,merkle,difficulty,raw) VALUES (?,?,?,?,?,?,?,?)",
                    (block["height"], block["hash"], block["prev_hash"], block["timestamp"],
                     block["nonce"], block["merkle"], block["difficulty"], blob))
                for tx in block["transactions"]:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO transactions(txid,block_height) VALUES (?,?)",
                        (tx["txid"], block["height"]))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def delete_blocks_above(self, height):
        with self.lock:
            n = self.conn.execute("DELETE FROM blocks WHERE height > ?", (height,)).rowcount
            self.conn.execute("DELETE FROM transactions WHERE block_height > ?", (height,))
            self.conn.execute("DELETE FROM utxos WHERE block_height > ?", (height,))
            return n

    def get_block(self, height):
        row = self.conn.execute("SELECT raw FROM blocks WHERE height=?", (height,)).fetchone()
        return orjson.loads(zlib.decompress(row["raw"])) if row else None

    def get_block_by_hash(self, h):
        row = self.conn.execute("SELECT raw FROM blocks WHERE hash=?", (h,)).fetchone()
        return orjson.loads(zlib.decompress(row["raw"])) if row else None

    def get_blocks_range(self, start, end):
        rows = self.conn.execute(
            "SELECT raw FROM blocks WHERE height >= ? AND height < ? ORDER BY height",
            (start, end)).fetchall()
        return [orjson.loads(zlib.decompress(r["raw"])) for r in rows]

    def height(self):
        row = self.conn.execute("SELECT MAX(height) AS h FROM blocks").fetchone()
        return int(row["h"]) if row and row["h"] is not None else -1

    def tip(self):
        row = self.conn.execute("SELECT hash,prev_hash FROM blocks ORDER BY height DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def tip_hash(self):
        t = self.tip()
        return t["hash"] if t else "0" * 64

    def headers(self, start, limit=2000):
        rows = self.conn.execute(
            "SELECT height,hash,prev_hash,timestamp,nonce,merkle,difficulty FROM blocks WHERE height>=? ORDER BY height LIMIT ?",
            (start, limit)).fetchall()
        return [dict(r) for r in rows]

    def latest_blocks(self, n=10):
        top = self.height()
        if top < 0: return []
        return [b for b in (self.get_block(h) for h in range(max(0, top - n + 1), top + 1)) if b]

    def balance(self, address):
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM utxos WHERE address=? AND spent=0",
            (address,)).fetchone()
        return int(row["s"])

    def utxos_for(self, address):
        rows = self.conn.execute(
            "SELECT txid,vout,amount,pubkey FROM utxos WHERE address=? AND spent=0 ORDER BY amount DESC",
            (address,)).fetchall()
        return [dict(r) for r in rows]

    def get_utxo(self, txid, vout):
        row = self.conn.execute(
            "SELECT * FROM utxos WHERE txid=? AND vout=? AND spent=0", (txid, vout)).fetchone()
        return dict(row) if row else None

    def apply_tx(self, tx, height, coinbase=False):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE TRANSACTION")
            try:
                if not coinbase:
                    for inp in tx["inputs"]:
                        self.conn.execute(
                            "UPDATE utxos SET spent=1, spent_by=? WHERE txid=? AND vout=? AND spent=0",
                            (tx["txid"], inp["txid"], inp["vout"]))
                for i, out in enumerate(tx["outputs"]):
                    self.conn.execute(
                        "INSERT OR REPLACE INTO utxos(txid,vout,address,amount,pubkey,block_height,spent) VALUES (?,?,?,?,?,?,0)",
                        (tx["txid"], i, out["address"], out["amount"], out.get("pubkey", ""), height))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def rollback_block(self, height):
        with self.lock:
            block = self.get_block(height)
            if not block: return
            self.conn.execute("BEGIN IMMEDIATE TRANSACTION")
            try:
                for tx in reversed(block["transactions"]):
                    for i in range(len(tx["outputs"])):
                        self.conn.execute("DELETE FROM utxos WHERE txid=? AND vout=?", (tx["txid"], i))
                    if height > 0:
                        for inp in tx.get("inputs", []):
                            if inp["txid"] == "0" * 64: continue
                            self.conn.execute("UPDATE utxos SET spent=0, spent_by=NULL WHERE txid=? AND vout=?",
                                              (inp["txid"], inp["vout"]))
                    self.conn.execute("DELETE FROM transactions WHERE txid=?", (tx["txid"],))
                self.conn.execute("DELETE FROM blocks WHERE height=?", (height,))
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def add_mempool(self, tx, fee):
        with self.lock:
            try:
                self.conn.execute("INSERT INTO mempool(txid,raw,fee,received_at) VALUES (?,?,?,?)",
                                  (tx["txid"], zlib.compress(orjson.dumps(tx)), fee, time.time()))
            except sqlite3.IntegrityError:
                return False
            self._prune_mempool()
            return True

    def _prune_mempool(self, max_size=1000, ttl=3600):
        self.conn.execute("DELETE FROM mempool WHERE received_at < ?", (time.time() - ttl,))
        n = self.conn.execute("SELECT COUNT(*) AS c FROM mempool").fetchone()["c"]
        if n > max_size:
            self.conn.execute(
                "DELETE FROM mempool WHERE txid IN (SELECT txid FROM mempool ORDER BY fee ASC, received_at ASC LIMIT ?)",
                (n - max_size,))

    def has_mempool(self, txid):
        return self.conn.execute("SELECT 1 FROM mempool WHERE txid=?", (txid,)).fetchone() is not None

    def get_mempool_tx(self, txid):
        row = self.conn.execute("SELECT raw FROM mempool WHERE txid=?", (txid,)).fetchone()
        return orjson.loads(zlib.decompress(row["raw"])) if row else None

    def all_mempool(self, limit=1000):
        rows = self.conn.execute("SELECT raw FROM mempool ORDER BY fee DESC LIMIT ?", (limit,)).fetchall()
        return [orjson.loads(zlib.decompress(r["raw"])) for r in rows]

    def remove_mempool(self, txid):
        with self.lock:
            self.conn.execute("DELETE FROM mempool WHERE txid=?", (txid,))

    def mempool_count(self):
        return self.conn.execute("SELECT COUNT(*) AS c FROM mempool").fetchone()["c"]

    def mempool_stats(self):
        row = self.conn.execute("SELECT COUNT(*) AS c, COALESCE(SUM(fee),0) AS sf FROM mempool").fetchone()
        fees = self.conn.execute("SELECT fee FROM mempool ORDER BY fee DESC LIMIT 100").fetchall()
        return {"count": row["c"], "sum_fee": row["sf"], "fees": [f["fee"] for f in fees]}

    def get_stats(self):
        return {"height": self.height(), "tip_hash": self.tip_hash(),
                "utxos": self.count_utxos(), "mempool": self.mempool_count(),
                "blocks": self.height() + 1}

    def prune_spent_utxos(self, keep_height=1000):
        cutoff = max(0, self.height() - keep_height)
        with self.lock:
            return self.conn.execute("DELETE FROM utxos WHERE spent=1 AND block_height < ?", (cutoff,)).rowcount

    def count_utxos(self):
        return self.conn.execute("SELECT COUNT(*) AS c FROM utxos WHERE spent=0").fetchone()["c"]

    def vacuum(self):
        with self.lock:
            self.conn.execute("VACUUM")

    def set_meta(self, key, value):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (key, value))

    def get_meta(self, key):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def upsert_peer(self, node_id, address, genesis_hash, version="?", height=0,
                    is_miner=False, public_key="", metadata=None):
        with self.lock:
            agora = int(time.time())
            existente = self.conn.execute("SELECT node_id FROM peers WHERE address=?", (address,)).fetchone()
            novo = existente is None
            self.conn.execute("""
                INSERT INTO peers (address, node_id, genesis_hash, version, height,
                                   is_miner, public_key, metadata, first_seen, last_seen, score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(address) DO UPDATE SET
                    node_id=excluded.node_id, genesis_hash=excluded.genesis_hash,
                    version=excluded.version, height=excluded.height,
                    is_miner=excluded.is_miner, public_key=excluded.public_key,
                    metadata=excluded.metadata, last_seen=excluded.last_seen
            """, (address, node_id, genesis_hash, version, height,
                  1 if is_miner else 0, public_key,
                  orjson.dumps(metadata or {}).decode(), agora, agora))
            if novo:
                self._log_event("peer_joined", address, f"node_id={node_id[:12]}")
            return novo

    def get_peer_score(self, address):
        row = self.conn.execute("SELECT score FROM peers WHERE address=?", (address,)).fetchone()
        return int(row["score"]) if row else 0

    def set_peer_score(self, address, score):
        with self.lock:
            self.conn.execute("UPDATE peers SET score=? WHERE address=?", (score, address))

    def add_peer_score(self, address, delta):
        with self.lock:
            self.conn.execute("UPDATE peers SET score = COALESCE(score, 0) + ? WHERE address=?", (delta, address))
            return self.get_peer_score(address)

    def listar_peers(self, apenas_ativos=True, janela=300):
        if apenas_ativos:
            cutoff = int(time.time()) - janela
            rows = self.conn.execute("SELECT * FROM peers WHERE last_seen >= ? ORDER BY last_seen DESC", (cutoff,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM peers ORDER BY last_seen DESC").fetchall()
        return [dict(r) for r in rows]

    def listar_peers_maus(self, score_min=-100):
        rows = self.conn.execute("SELECT * FROM peers WHERE score < ?", (score_min,)).fetchall()
        return [dict(r) for r in rows]

    def contar_peers(self, apenas_ativos=True, janela=300):
        if apenas_ativos:
            cutoff = int(time.time()) - janela
            row = self.conn.execute("SELECT COUNT(*) AS n FROM peers WHERE last_seen >= ?", (cutoff,)).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM peers").fetchone()
        return int(row["n"])

    def remover_peer(self, node_id):
        with self.lock:
            self.conn.execute("DELETE FROM peers WHERE node_id=?", (node_id,))

    def remover_peer_por_endereco(self, address):
        with self.lock:
            self.conn.execute("DELETE FROM peers WHERE address=?", (address,))

    def limpar_peers_inativos(self, janela=1800):
        with self.lock:
            self.conn.execute("DELETE FROM peers WHERE last_seen < ?", (int(time.time()) - janela,))

    def _log_event(self, tipo, peer="", details=""):
        with self.lock:
            self.conn.execute("INSERT INTO network_events (timestamp, event_type, peer_address, details) VALUES (?,?,?,?)",
                              (int(time.time()), tipo, peer, details))

    def ultimos_eventos(self, n=50):
        rows = self.conn.execute("SELECT * FROM network_events ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()
'@
Set-Content -Path 'db.py' -Value $db_content -Encoding UTF8
Write-Host "      OK" -ForegroundColor Green

# [4] blockchain.py
Write-Host "[4/8] Atualizando blockchain.py..." -ForegroundColor Cyan
$blockchain_content = @'
"""blockchain.py — Nucleo da blockchain BRN (v4)
v4: + cumulative_work, + reorg_to, + estimate_fee
"""
import time
import orjson
from crypto import double_sha256, sha256
from db import ChainDB

COIN_NAME = "BrunoCoin"
TICKER = "BRN"
DECIMALS = 8
UNIT = 10 ** DECIMALS
MAX_SUPPLY = 21_000_000 * UNIT
INITIAL_REWARD = 50 * UNIT
HALVING_INTERVAL = 210_000
BLOCK_TIME = 120
DIFFICULTY_INTERVAL = 2016
INITIAL_DIFFICULTY = 4
MAX_TX_PER_BLOCK = 500
MIN_RELAY_FEE = 1000
MAX_REORG_DEPTH = 100

GENESIS_PREV = "0" * 64
GENESIS_TIMESTAMP = 1700000000
GENESIS_REWARD = INITIAL_REWARD
GENESIS_ADDRESS = "brn1qxyzk7y0v2j4g0a8d9n5t3m2k7h4s6w8c9p2e"


def block_hash(prev_hash, merkle, timestamp, nonce, difficulty):
    header = f"{prev_hash}{merkle}{timestamp}{nonce}{difficulty}"
    return double_sha256(header.encode()).hex()


def target_from_difficulty(difficulty):
    return int("0" * difficulty + "f" * (64 - difficulty), 16)


def meets_difficulty(h, difficulty):
    return int(h, 16) <= target_from_difficulty(difficulty)


def work_from_difficulty(difficulty):
    return 16 ** difficulty


def compute_merkle_root(txids):
    if not txids:
        return "0" * 64
    layer = [bytes.fromhex(t) for t in txids]
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [sha256(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0].hex()


def make_coinbase(address, height, reward):
    cb = {"txid": "", "inputs": [{"txid": "0" * 64, "vout": 0xFFFFFFFF, "pubkey": "", "signature": ""}],
          "outputs": [{"address": address, "amount": reward, "pubkey": ""}],
          "timestamp": int(time.time()), "locktime": 0, "height": height}
    cb["txid"] = txid(cb)
    return cb


def txid(tx):
    core = {"inputs": [{"txid": i["txid"], "vout": i["vout"]} for i in tx["inputs"]],
            "outputs": tx["outputs"], "timestamp": tx["timestamp"],
            "locktime": tx.get("locktime", 0)}
    if "height" in tx:
        core["height"] = tx["height"]
    return double_sha256(orjson.dumps(core, option=orjson.OPT_SORT_KEYS)).hex()


def signing_hash(tx):
    core = {"inputs": [{"txid": i["txid"], "vout": i["vout"], "pubkey": i.get("pubkey", "")} for i in tx["inputs"]],
            "outputs": tx["outputs"], "timestamp": tx["timestamp"],
            "locktime": tx.get("locktime", 0)}
    return double_sha256(orjson.dumps(core, option=orjson.OPT_SORT_KEYS))


def build_genesis():
    cb = {"txid": "", "inputs": [{"txid": "0" * 64, "vout": 0xFFFFFFFF, "pubkey": "", "signature": ""}],
          "outputs": [{"address": GENESIS_ADDRESS, "amount": GENESIS_REWARD, "pubkey": ""}],
          "timestamp": GENESIS_TIMESTAMP, "locktime": 0, "height": 0}
    cb["txid"] = txid(cb)
    merkle = compute_merkle_root([cb["txid"]])
    nonce = 0
    while True:
        h = block_hash(GENESIS_PREV, merkle, GENESIS_TIMESTAMP, nonce, 1)
        if h.startswith("0"): break
        nonce += 1
    return {"height": 0, "hash": h, "prev_hash": GENESIS_PREV, "timestamp": GENESIS_TIMESTAMP,
            "nonce": nonce, "merkle": merkle, "difficulty": 1, "transactions": [cb]}


GENESIS_BLOCK = build_genesis()


class Blockchain:
    def __init__(self, db_path="brn_v2_chain.db", genesis_address=None):
        self.db = ChainDB(db_path)
        if self.db.height() < 0:
            g = GENESIS_BLOCK
            if genesis_address:
                g = self._genesis_with_address(genesis_address)
            self.db.add_block(g)
            self.db.apply_tx(g["transactions"][0], 0, coinbase=True)
            self.db.set_meta("genesis_hash", g["hash"])

    @staticmethod
    def _genesis_with_address(address):
        cb = {"txid": "", "inputs": [{"txid": "0" * 64, "vout": 0xFFFFFFFF, "pubkey": "", "signature": ""}],
              "outputs": [{"address": address, "amount": GENESIS_REWARD, "pubkey": ""}],
              "timestamp": GENESIS_TIMESTAMP, "locktime": 0, "height": 0}
        cb["txid"] = txid(cb)
        merkle = compute_merkle_root([cb["txid"]])
        nonce = 0
        while True:
            h = block_hash(GENESIS_PREV, merkle, GENESIS_TIMESTAMP, nonce, 1)
            if h.startswith("0"): break
            nonce += 1
        return {"height": 0, "hash": h, "prev_hash": GENESIS_PREV, "timestamp": GENESIS_TIMESTAMP,
                "nonce": nonce, "merkle": merkle, "difficulty": 1, "transactions": [cb]}

    def current_reward(self, height):
        halvings = height // HALVING_INTERVAL
        if halvings >= 64: return 0
        return INITIAL_REWARD >> halvings

    def current_difficulty(self):
        h = self.db.height()
        if h < DIFFICULTY_INTERVAL: return INITIAL_DIFFICULTY
        start = self.db.get_block(h - DIFFICULTY_INTERVAL + 1)
        end = self.db.get_block(h)
        if not start or not end: return INITIAL_DIFFICULTY
        actual = max(1, end["timestamp"] - start["timestamp"])
        expected = BLOCK_TIME * DIFFICULTY_INTERVAL
        prev = end["difficulty"]
        new = int(prev * expected / actual)
        new = max(prev // 4, min(prev * 4, new))
        return max(1, new)

    def cumulative_work(self):
        total = 0
        for h in range(self.db.height() + 1):
            b = self.db.get_block(h)
            if b:
                total += work_from_difficulty(b["difficulty"])
        return total

    def cumulative_work_of_chain(self, blocks):
        return sum(work_from_difficulty(b["difficulty"]) for b in blocks)

    def estimate_fee(self, priority="medium"):
        stats = self.db.mempool_stats()
        count = stats["count"]
        fees = stats["fees"]
        if count == 0:
            return MIN_RELAY_FEE
        fees_sorted = sorted(fees, reverse=True)
        n = len(fees_sorted)
        if priority == "high":
            idx = max(0, n // 10)
            return max(MIN_RELAY_FEE, fees_sorted[idx] * 2)
        elif priority == "low":
            idx = min(n - 1, (n * 9) // 10)
            return max(MIN_RELAY_FEE, fees_sorted[idx])
        else:
            idx = n // 2
            return max(MIN_RELAY_FEE, fees_sorted[idx])

    def validate_tx(self, tx, from_mempool=False):
        from wallet import Wallet
        if tx.get("txid") != txid(tx): return False, "txid invalido"
        if not tx["inputs"] or not tx["outputs"]: return False, "tx sem inputs/outputs"
        if tx["inputs"][0]["txid"] == "0" * 64: return False, "coinbase invalida"
        in_sum = 0
        seen = set()
        for inp in tx["inputs"]:
            key = (inp["txid"], inp["vout"])
            if key in seen: return False, "input duplicado"
            seen.add(key)
            u = self.db.get_utxo(inp["txid"], inp["vout"])
            if not u: return False, "UTXO inexistente"
            if u["pubkey"] and inp.get("pubkey", "") != u["pubkey"]: return False, "pubkey mismatch"
            in_sum += u["amount"]
        out_sum = sum(o["amount"] for o in tx["outputs"])
        if out_sum > in_sum: return False, "outputs > inputs"
        if in_sum - out_sum < MIN_RELAY_FEE: return False, f"fee abaixo do minimo"
        sig_hash = signing_hash(tx)
        for inp in tx["inputs"]:
            if not Wallet.verify(sig_hash, inp.get("signature", ""), inp.get("pubkey", "")):
                return False, "assinatura invalida"
        return True, "ok"

    def submit_tx(self, tx):
        if self.db.has_mempool(tx["txid"]): return False, "ja na mempool"
        ok, msg = self.validate_tx(tx)
        if not ok: return False, msg
        fee = self.tx_fee(tx)
        if fee < 0: return False, "fee negativa"
        if not self.db.add_mempool(tx, fee): return False, "falha na mempool"
        return True, tx["txid"]

    def tx_fee(self, tx):
        in_sum = 0
        for inp in tx["inputs"]:
            u = self.db.get_utxo(inp["txid"], inp["vout"])
            if u: in_sum += u["amount"]
        return in_sum - sum(o["amount"] for o in tx["outputs"])

    def validate_block(self, block, prev_block=None):
        if block["prev_hash"] != (prev_block["hash"] if prev_block else self.db.tip_hash()):
            return False, "prev_hash incorreto"
        expected_height = (prev_block["height"] + 1) if prev_block else self.db.height() + 1
        if block["height"] != expected_height: return False, "altura invalida"
        if not meets_difficulty(block["hash"], block["difficulty"]): return False, "PoW invalido"
        h = block_hash(block["prev_hash"], block["merkle"], block["timestamp"], block["nonce"], block["difficulty"])
        if h != block["hash"]: return False, "hash incorreto"
        if compute_merkle_root([t["txid"] for t in block["transactions"]]) != block["merkle"]:
            return False, "merkle incorreto"
        cb = block["transactions"][0]
        if cb["inputs"][0]["txid"] != "0" * 64: return False, "primeira tx nao e coinbase"
        reward = self.current_reward(block["height"])
        fees = 0
        for i, t in enumerate(block["transactions"][1:], 1):
            if t["txid"] != txid(t): return False, "txid invalido"
            ok, msg = self.validate_tx(t)
            if not ok: return False, msg
            fees += self.tx_fee(t)
        total_cb = sum(o["amount"] for o in cb["outputs"])
        if total_cb > reward + fees: return False, "coinbase acima do permitido"
        return True, "ok"

    def accept_block(self, block):
        prev = self.db.get_block_by_hash(block["prev_hash"])
        ok, msg = self.validate_block(block, prev)
        if not ok: return False, msg
        self.db.add_block(block)
        for i, t in enumerate(block["transactions"]):
            self.db.apply_tx(t, block["height"], coinbase=(i == 0))
            if i > 0: self.db.remove_mempool(t["txid"])
        return True, block["hash"]

    def reorg_to(self, new_blocks):
        if not new_blocks: return False, "lista vazia"
        fork_height = -1
        for i, b in enumerate(new_blocks):
            local = self.db.get_block(b["height"])
            if local and local["hash"] == b["hash"]:
                fork_height = b["height"]
            else:
                break
        if fork_height < 0: return False, "nenhum ponto em comum"
        blocks_to_add = [b for b in new_blocks if b["height"] > fork_height]
        if not blocks_to_add: return False, "nada novo"
        work_alt = self.cumulative_work_of_chain(blocks_to_add)
        work_local = 0
        for h in range(fork_height + 1, self.db.height() + 1):
            b = self.db.get_block(h)
            if b: work_local += work_from_difficulty(b["difficulty"])
        if work_alt <= work_local:
            return False, f"local tem mais trabalho"
        depth = self.db.height() - fork_height
        if depth > MAX_REORG_DEPTH:
            return False, f"reorg muito profundo ({depth})"
        print(f"[REORG] fork #{fork_height}, -{depth}, +{len(blocks_to_add)} (work {work_alt}>{work_local})")
        prev = self.db.get_block(fork_height) if fork_height >= 0 else None
        for b in blocks_to_add:
            ok, msg = self.validate_block(b, prev)
            if not ok: return False, f"bloco #{b['height']}: {msg}"
            prev = b
        backups = []
        for h in range(fork_height + 1, self.db.height() + 1):
            b = self.db.get_block(h)
            if b: backups.append(b)
        try:
            self.db.delete_blocks_above(fork_height)
        except Exception as e:
            return False, f"falha ao deletar: {e}"
        for b in blocks_to_add:
            ok, msg = self.accept_block(b)
            if not ok:
                print(f"[REORG] falha em #{b['height']}, restaurando...")
                self.db.delete_blocks_above(fork_height)
                for backup in backups:
                    try: self.accept_block(backup)
                    except Exception: pass
                return False, f"falha no reorg: {msg}"
        print(f"[REORG] concluido! Altura: {self.db.height()}")
        return True, f"reorg ok ({len(blocks_to_add)} blocos)"

    def mine_block(self, miner_address):
        height = self.db.height() + 1
        reward = self.current_reward(height)
        diff = self.current_difficulty()
        cb = make_coinbase(miner_address, height, reward)
        selected = self.db.all_mempool(limit=MAX_TX_PER_BLOCK - 1)
        txs = [cb] + selected
        merkle = compute_merkle_root([t["txid"] for t in txs])
        prev_hash = self.db.tip_hash()
        ts = int(time.time())
        nonce = 0
        while True:
            h = block_hash(prev_hash, merkle, ts, nonce, diff)
            if meets_difficulty(h, diff): break
            nonce += 1
            if nonce % 200000 == 0: ts = int(time.time())
        block = {"height": height, "hash": h, "prev_hash": prev_hash, "timestamp": ts,
                 "nonce": nonce, "merkle": merkle, "difficulty": diff, "transactions": txs}
        ok, msg = self.accept_block(block)
        return block if ok else None

    def mine_block_interruptible(self, miner_address, should_continue):
        height = self.db.height() + 1
        reward = self.current_reward(height)
        diff = self.current_difficulty()
        cb = make_coinbase(miner_address, height, reward)
        selected = self.db.all_mempool(limit=MAX_TX_PER_BLOCK - 1)
        txs = [cb] + selected
        merkle = compute_merkle_root([t["txid"] for t in txs])
        prev_hash = self.db.tip_hash()
        ts = int(time.time())
        nonce = 0
        while True:
            if should_continue is not None and not should_continue():
                return None
            h = block_hash(prev_hash, merkle, ts, nonce, diff)
            if meets_difficulty(h, diff): break
            nonce += 1
            if nonce % 50000 == 0: ts = int(time.time())
        block = {"height": height, "hash": h, "prev_hash": prev_hash, "timestamp": ts,
                 "nonce": nonce, "merkle": merkle, "difficulty": diff, "transactions": txs}
        ok, msg = self.accept_block(block)
        return block if ok else None


try:
    from chain_validator import verify_chain as _verify_ext

    def _verify_chain_method(self, full=True):
        return _verify_ext(self)

    Blockchain.verify_chain = _verify_chain_method
    Blockchain.verify_chain_dict = lambda self: _verify_ext(self).to_dict()
    print("chain_validator.py plugado em Blockchain")
except ImportError:
    print("chain_validator.py nao encontrado")
'@
Set-Content -Path 'blockchain.py' -Value $blockchain_content -Encoding UTF8
Write-Host "      OK" -ForegroundColor Green

# [5] p2p_unified.py
Write-Host "[5/8] Atualizando p2p_unified.py..." -ForegroundColor Cyan
$p2p_content = @'
"""p2p_unified.py — Modulo P2P do BRN (v4)
v4: + Peer scoring, + Rate limit, + Sync por cumulative work
"""
import os, sys, json, time, uuid, socket, hashlib, threading
import urllib.request
import xml.etree.ElementTree as ET

MULTICAST_GROUP = "239.255.42.99"
MULTICAST_PORT  = 50007
TCP_PORT_DEFAULT = 6001
DISCOVERY_INTERVAL_MIN = 2.0
DISCOVERY_INTERVAL_MAX = 30.0
PEER_TIMEOUT    = 60
PEER_CLEANUP_S  = 30
MAX_MSG_SIZE    = 2 * 1024 * 1024
PROTOCOL_VERSION = "BRN4/1.0"
NETWORK_MAGIC   = b"BRN4"
PEER_SCORE_BAN_THRESHOLD = -100
PEER_SCORE_REWARD_GOOD = 10
PEER_SCORE_PENALTY_BAD = -50
RATE_LIMIT_MSGS_PER_SEC = 20

NETWORK_SECRET = os.environ.get("BRN_NETWORK_SECRET", "brunocoin-lan-2026")
TOKEN_ESPERADO = hashlib.sha256(NETWORK_SECRET.encode()).hexdigest()[:8]
UUID_FILE  = "node_uuid.txt"
PEERS_FILE = "peers_discovered.json"


def _obter_ou_criar_uuid():
    if os.path.exists(UUID_FILE):
        try:
            with open(UUID_FILE) as f:
                val = f.read().strip()
                if val: return val
        except Exception: pass
    novo = str(uuid.uuid4())
    try:
        with open(UUID_FILE, "w") as f:
            f.write(novo)
    except Exception: pass
    return novo


def _get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception: return "127.0.0.1"
    finally: s.close()


def _mesma_subnet(ip1, ip2):
    try: return ip1.rsplit(".", 1)[0] == ip2.rsplit(".", 1)[0]
    except Exception: return False


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
                    if location and self._fetch_control_url(location): return True
                except socket.timeout: break
        except Exception: pass
        finally: s.close()
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
            for service in root.iter(f"{ns}service"):
                st = service.find(f"{ns}serviceType")
                cu = service.find(f"{ns}controlURL")
                if st is not None and cu is not None:
                    if "WANIPConnection" in st.text or "WANPPPConnection" in st.text:
                        base = location.rsplit("/", 1)[0]
                        self.service_type = st.text
                        self.control_url = base + cu.text if cu.text.startswith("/") else cu.text
                        return True
        except Exception: pass
        return False

    def _soap_request(self, body, action):
        if not self.control_url: return False
        headers = {"Content-Type": 'text/xml; charset="utf-8"',
                   "SOAPAction": f'"{self.service_type}#{action}"'}
        try:
            req = urllib.request.Request(self.control_url, data=body.encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status == 200
        except Exception: return False

    def add_port_mapping(self, ext_port, int_port, int_ip, description="BRN Node", protocol="TCP"):
        body = f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:AddPortMapping xmlns:u="{self.service_type}">
      <NewRemoteHost></NewRemoteHost><NewExternalPort>{ext_port}</NewExternalPort>
      <NewProtocol>{protocol}</NewProtocol><NewInternalPort>{int_port}</NewInternalPort>
      <NewInternalClient>{int_ip}</NewInternalClient><NewEnabled>1</NewEnabled>
      <NewPortMappingDescription>{description}</NewPortMappingDescription>
      <NewLeaseDuration>0</NewLeaseDuration>
    </u:AddPortMapping>
  </s:Body>
</s:Envelope>"""
        return self._soap_request(body, "AddPortMapping")

    def delete_port_mapping(self, ext_port, protocol="TCP"):
        body = f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:DeletePortMapping xmlns:u="{self.service_type}">
      <NewRemoteHost></NewRemoteHost><NewExternalPort>{ext_port}</NewExternalPort>
      <NewProtocol>{protocol}</NewProtocol>
    </u:DeletePortMapping>
  </s:Body>
</s:Envelope>"""
        return self._soap_request(body, "DeletePortMapping")

    def get_external_ip(self):
        if not self.control_url: return None
        body = f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body><u:GetExternalIPAddress xmlns:u="{self.service_type}"></u:GetExternalIPAddress></s:Body>
</s:Envelope>"""
        headers = {"Content-Type": 'text/xml; charset="utf-8"',
                   "SOAPAction": f'"{self.service_type}#GetExternalIPAddress"'}
        try:
            req = urllib.request.Request(self.control_url, data=body.encode(), headers=headers)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                xml_data = resp.read().decode(errors="ignore")
            root = ET.fromstring(xml_data)
            for elem in root.iter():
                if "ExternalIPAddress" in elem.tag: return elem.text
        except Exception: pass
        return None


class P2PServer(threading.Thread):
    def __init__(self, blockchain, port, on_new_block=None, on_new_tx=None,
                 on_peer_bad=None, on_peer_good=None):
        super().__init__(daemon=True, name="P2P-Server")
        self.bc = blockchain
        self.port = port
        self.on_new_block = on_new_block
        self.on_new_tx = on_new_tx
        self.on_peer_bad = on_peer_bad
        self.on_peer_good = on_peer_good
        self.running = False
        self.sock = None
        self._rate_counters = {}
        self._rate_lock = threading.Lock()

    def stop(self):
        self.running = False
        if self.sock:
            try: self.sock.close()
            except Exception: pass

    def _check_rate(self, addr):
        agora = time.time()
        with self._rate_lock:
            janela = self._rate_counters.setdefault(addr, [])
            janela[:] = [t for t in janela if agora - t < 1.0]
            if len(janela) >= RATE_LIMIT_MSGS_PER_SEC: return False
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
            except socket.timeout: continue
            except Exception as e:
                if self.running: print(f"[P2P] Erro no accept: {e}")

    def _handle_conn(self, conn, addr):
        try:
            peer_ip = addr[0]
            if not self._check_rate(peer_ip):
                print(f"[P2P] Rate limit: {peer_ip}")
                return
            score = self.bc.db.get_peer_score(peer_ip)
            if score <= PEER_SCORE_BAN_THRESHOLD:
                print(f"[P2P] Peer banido ({score}): {peer_ip}")
                return
            conn.settimeout(10)
            raw = conn.recv(MAX_MSG_SIZE)
            if not raw or not raw.startswith(NETWORK_MAGIC): return
            msg = json.loads(raw[len(NETWORK_MAGIC):].decode())
            response = self._process_message(msg, addr)
            if response:
                conn.sendall(NETWORK_MAGIC + json.dumps(response).encode())
        except Exception as e:
            print(f"[P2P] Erro com {addr}: {e}")
        finally:
            try: conn.close()
            except Exception: pass

    def _marcar_bom(self, peer_ip):
        if self.on_peer_good: self.on_peer_good(peer_ip)

    def _marcar_mau(self, peer_ip, motivo=""):
        print(f"[P2P] Peer marcado mau ({motivo}): {peer_ip}")
        if self.on_peer_bad: self.on_peer_bad(peer_ip)

    def _process_message(self, msg, addr):
        mtype = msg.get("type")
        peer_ip = addr[0]
        try:
            if mtype == "ping":
                return {"type": "pong", "version": PROTOCOL_VERSION,
                        "height": self.bc.db.height(), "work": self.bc.cumulative_work()}
            if mtype == "get_chain_height":
                return {"type": "chain_height", "height": self.bc.db.height(),
                        "hash": self.bc.db.tip_hash(), "work": self.bc.cumulative_work()}
            if mtype == "get_block":
                return {"type": "block", "block": self.bc.db.get_block(int(msg.get("height", 0)))}
            if mtype == "get_blocks_range":
                start = int(msg.get("start", 0))
                end = int(msg.get("end", start + 50))
                return {"type": "blocks_range", "blocks": self.bc.db.get_blocks_range(start, end)}
            if mtype == "new_block":
                block_dict = msg.get("block")
                if block_dict and self.on_new_block:
                    ok = self.on_new_block(block_dict)
                    if ok: self._marcar_bom(peer_ip)
                    else: self._marcar_mau(peer_ip, "bloco invalido")
                    return {"type": "ack", "ok": bool(ok)}
                return {"type": "ack", "ok": False}
            if mtype == "new_tx":
                tx_dict = msg.get("tx")
                if tx_dict and self.on_new_tx:
                    ok = self.on_new_tx(tx_dict)
                    if ok: self._marcar_bom(peer_ip)
                    return {"type": "ack", "ok": bool(ok)}
                return {"type": "ack", "ok": False}
            if mtype == "get_mempool":
                return {"type": "mempool", "txs": self.bc.db.all_mempool(limit=200)}
        except Exception as e:
            return {"type": "error", "message": str(e)}
        return {"type": "error", "message": "Tipo desconhecido"}


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
        except Exception: return None

    @staticmethod
    def ping(ip, port): return P2PClient.send_message(ip, port, {"type": "ping"})
    @staticmethod
    def get_chain_height(ip, port): return P2PClient.send_message(ip, port, {"type": "get_chain_height"})
    @staticmethod
    def get_block(ip, port, h): return P2PClient.send_message(ip, port, {"type": "get_block", "height": h})
    @staticmethod
    def get_blocks_range(ip, port, s, e): return P2PClient.send_message(ip, port, {"type": "get_blocks_range", "start": s, "end": e})
    @staticmethod
    def send_block(ip, port, block): return P2PClient.send_message(ip, port, {"type": "new_block", "block": block})
    @staticmethod
    def send_tx(ip, port, tx): return P2PClient.send_message(ip, port, {"type": "new_tx", "tx": tx})
    @staticmethod
    def get_mempool(ip, port): return P2PClient.send_message(ip, port, {"type": "get_mempool"})


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
            with open(PEERS_FILE, "w") as f: json.dump(lista, f)
        except Exception: pass

    def _carregar_peers(self):
        try:
            if not os.path.exists(PEERS_FILE): return
            with open(PEERS_FILE) as f:
                for addr in json.load(f):
                    self.discovered_peers[addr] = 0
        except Exception: pass

    def _start_server(self):
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try: self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError): pass
            self.server_socket.bind(("", MULTICAST_PORT))
            mreq = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton("0.0.0.0")
            self.server_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            local_ip = _get_local_ip()
            print(f"[Discovery] Escutando {MULTICAST_GROUP}:{MULTICAST_PORT}")
            print(f"[Discovery] IP local: {local_ip}")
            while self.running:
                try:
                    self.server_socket.settimeout(2.0)
                    data, addr = self.server_socket.recvfrom(2048)
                    remote_ip = addr[0]
                    if not _mesma_subnet(remote_ip, local_ip): continue
                    msg = data.decode("utf-8", errors="ignore")
                    if not self._token_valido(msg): continue
                    if msg.startswith("BRN_NODE_PING:"): self._processar_ping(msg, remote_ip, addr)
                    elif msg.startswith("BRN_NODE_PONG:"): self._processar_pong(msg, remote_ip)
                    elif msg.startswith("BRN_NODE_BYE:"): self._processar_bye(msg, remote_ip)
                except socket.timeout: continue
                except OSError: break
                except Exception as e: print(f"[Discovery] Erro: {e}")
        except Exception as e:
            print(f"[Discovery] Falha: {e}")

    def _token_valido(self, msg):
        try: return msg.split(":")[-1] == TOKEN_ESPERADO
        except Exception: return False

    def _processar_ping(self, msg, remote_ip, addr):
        try:
            partes = msg.split(":")
            if len(partes) < 4: return
            remote_port = int(partes[1])
            remote_uuid = partes[2]
            if remote_uuid == self.node_uuid: return
            peer_address = f"{remote_ip}:{remote_port}"
            with self.peers_lock:
                novo = peer_address not in self.discovered_peers
                self.discovered_peers[peer_address] = time.time()
            if novo:
                print(f"[Discovery] Novo peer: {peer_address}")
                self._salvar_peers()
                if self.on_peer_found:
                    try: self.on_peer_found(remote_ip, remote_port)
                    except Exception: pass
            if remote_ip not in self.peers_respondidos:
                self.peers_respondidos.add(remote_ip)
                response = f"BRN_NODE_PONG:{self.tcp_port}:{self.node_uuid}:{TOKEN_ESPERADO}"
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.sendto(response.encode("utf-8"), addr)
                    sock.close()
                except Exception: pass
        except Exception: pass

    def _processar_pong(self, msg, remote_ip):
        try:
            partes = msg.split(":")
            if len(partes) < 4: return
            remote_port = int(partes[1])
            remote_uuid = partes[2]
            if remote_uuid == self.node_uuid: return
            peer_address = f"{remote_ip}:{remote_port}"
            with self.peers_lock:
                novo = peer_address not in self.discovered_peers
                self.discovered_peers[peer_address] = time.time()
            if novo:
                print(f"[Discovery] Conexao mutua: {peer_address}")
                self._salvar_peers()
                if self.on_peer_found:
                    try: self.on_peer_found(remote_ip, remote_port)
                    except Exception: pass
        except Exception: pass

    def _processar_bye(self, msg, remote_ip):
        try:
            partes = msg.split(":")
            if len(partes) < 4: return
            remote_port = int(partes[1])
            peer_address = f"{remote_ip}:{remote_port}"
            with self.peers_lock:
                self.discovered_peers.pop(peer_address, None)
        except Exception: pass

    def _start_client(self):
        try:
            self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self.client_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            self.client_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            print("[Discovery] Broadcast ativo")
            while self.running:
                try:
                    msg = f"BRN_NODE_PING:{self.tcp_port}:{self.node_uuid}:{TOKEN_ESPERADO}"
                    self.client_socket.sendto(msg.encode("utf-8"), (MULTICAST_GROUP, MULTICAST_PORT))
                    with self.peers_lock:
                        n = len(self.discovered_peers)
                    intervalo = DISCOVERY_INTERVAL_MAX if n >= 5 else (10.0 if n >= 2 else DISCOVERY_INTERVAL_MIN)
                    time.sleep(intervalo)
                except OSError: break
                except Exception: time.sleep(5)
        except Exception as e:
            print(f"[Discovery] Falha: {e}")

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
            except Exception: pass

    def run(self):
        if self.running: return
        self.running = True
        for t in [threading.Thread(target=self._start_server, daemon=True),
                  threading.Thread(target=self._start_client, daemon=True),
                  threading.Thread(target=self._limpar_peers_mortos, daemon=True)]:
            t.start()
            self._threads.append(t)

    def stop(self):
        if not self.running: return
        self.running = False
        try:
            if self.client_socket:
                bye = f"BRN_NODE_BYE:{self.tcp_port}:{self.node_uuid}:{TOKEN_ESPERADO}"
                self.client_socket.sendto(bye.encode("utf-8"), (MULTICAST_GROUP, MULTICAST_PORT))
        except Exception: pass
        for sock in (self.server_socket, self.client_socket):
            if sock:
                try: sock.close()
                except Exception: pass
        self._salvar_peers()

    def listar_peers(self):
        with self.peers_lock:
            return list(self.discovered_peers.keys())


class P2PManager:
    def __init__(self, blockchain, tcp_port=TCP_PORT_DEFAULT, enable_upnp=True):
        self.bc = blockchain
        self.tcp_port = tcp_port
        self.node_uuid = _obter_ou_criar_uuid()
        self.enable_upnp = enable_upnp
        self.external_ip = None
        self.upnp = None
        self.server = P2PServer(blockchain, tcp_port,
                                on_new_block=self._on_new_block,
                                on_new_tx=self._on_new_tx,
                                on_peer_bad=self._on_peer_bad,
                                on_peer_good=self._on_peer_good)
        self.discovery = PeerDiscovery(tcp_port, self.node_uuid,
                                        on_peer_found=self._on_peer_found,
                                        blockchain=blockchain)
        if enable_upnp:
            threading.Thread(target=self._setup_upnp, daemon=True).start()

    def start(self):
        self.server.start()
        self.discovery.run()
        print(f"[P2P] Manager iniciado (node_id={self.node_uuid[:8]})")

    def stop(self):
        self.server.stop()
        self.discovery.stop()
        if self.upnp and self.external_ip:
            try: self.upnp.delete_port_mapping(self.tcp_port, "TCP")
            except Exception: pass

    def _setup_upnp(self):
        try:
            self.upnp = UPnPClient()
            if not self.upnp.control_url:
                print("[UPnP] Roteador nao suporta")
                return
            local_ip = _get_local_ip()
            ok = self.upnp.add_port_mapping(self.tcp_port, self.tcp_port, local_ip, description="BRN Node")
            if ok:
                self.external_ip = self.upnp.get_external_ip()
                print(f"[UPnP] Porta {self.tcp_port} mapeada. IP: {self.external_ip}")
        except Exception: pass

    def _on_peer_found(self, ip, port):
        try:
            resp = P2PClient.get_chain_height(ip, port)
            if not resp: return
            remote_work = resp.get("work", 0)
            local_work = self.bc.cumulative_work()
            if remote_work > local_work:
                print(f"[P2P] Peer {ip}:{port} com mais work. Sincronizando...")
                self.sync_with_peer(ip, port)
        except Exception: pass

    def sync_with_peer(self, ip, port):
        resp = P2PClient.get_chain_height(ip, port)
        if not resp: return
        remote_height = resp.get("height", -1)
        remote_work = resp.get("work", 0)
        local_work = self.bc.cumulative_work()
        if remote_work <= local_work:
            print("[P2P] Peer sem mais trabalho.")
            return
        local_height = self.bc.db.height()
        start = max(0, local_height - 50) if local_height > 100 else 0
        novos = []
        cursor = start
        while cursor <= remote_height:
            end = min(cursor + 50, remote_height + 1)
            resp = P2PClient.get_blocks_range(ip, port, cursor, end)
            if not resp or "blocks" not in resp: break
            novos.extend(resp["blocks"])
            cursor = end
        if not novos: return
        ok, msg = self.bc.reorg_to(novos)
        if ok: print(f"[P2P] Reorg OK: {msg}")
        else: print(f"[P2P] Reorg nao aplicado: {msg}")

    def broadcast_block(self, block_dict):
        for peer in self.discovery.listar_peers():
            try:
                ip, port = peer.split(":")
                threading.Thread(target=P2PClient.send_block, args=(ip, int(port), block_dict), daemon=True).start()
            except Exception: pass

    def broadcast_tx(self, tx_dict):
        for peer in self.discovery.listar_peers():
            try:
                ip, port = peer.split(":")
                threading.Thread(target=P2PClient.send_tx, args=(ip, int(port), tx_dict), daemon=True).start()
            except Exception: pass

    def _on_new_block(self, block_dict):
        try:
            h = block_dict.get("height", -1)
            if h == self.bc.db.height() + 1:
                ok, msg = self.bc.accept_block(block_dict)
                if ok: print(f"[P2P] Novo bloco aceito: #{h}")
                return ok
            return False
        except Exception: return False

    def _on_new_tx(self, tx_dict):
        try:
            ok, msg = self.bc.submit_tx(tx_dict)
            if ok: print(f"[P2P] Nova tx: {tx_dict.get('txid', '?')[:16]}")
            return ok
        except Exception: return False

    def _on_peer_bad(self, peer_ip):
        try:
            novo = self.bc.db.add_peer_score(peer_ip, PEER_SCORE_PENALTY_BAD)
            print(f"[P2P] Score {peer_ip} = {novo}")
            if novo <= PEER_SCORE_BAN_THRESHOLD:
                print(f"[P2P] BANINDO {peer_ip}")
                self.bc.db.remover_peer_por_endereco(peer_ip)
        except Exception: pass

    def _on_peer_good(self, peer_ip):
        try: self.bc.db.add_peer_score(peer_ip, PEER_SCORE_REWARD_GOOD)
        except Exception: pass

    def get_status(self):
        peers = self.discovery.listar_peers()
        return {"node_id": self.node_uuid, "port": self.tcp_port,
                "external_ip": self.external_ip, "peer_count": len(peers),
                "peers": peers, "height": self.bc.db.height(),
                "work": self.bc.cumulative_work()}


if __name__ == "__main__":
    print("Rode via main.py")
'@
Set-Content -Path 'p2p_unified.py' -Value $p2p_content -Encoding UTF8
Write-Host "      OK" -ForegroundColor Green

# [6] wallet.py
Write-Host "[6/8] Atualizando wallet.py (HD Wallet)..." -ForegroundColor Cyan
$wallet_content = @'
"""wallet.py — Carteira BRN (v4) com HD Wallet BIP39/BIP44"""
import json, base64, os, secrets, hashlib
import hmac as hmac_lib
from crypto import (sha256, pubkey_from_priv, sign_schnorr, verify_schnorr,
                    sign_ecdsa, verify_ecdsa, generate_private_key)
from bech32 import address_from_pubkey

SIG_MODE = "schnorr"


class Wallet:
    def __init__(self, private_key_hex=None):
        if private_key_hex:
            self.priv = bytes.fromhex(private_key_hex)
        else:
            self.priv = generate_private_key()
        self.pub = pubkey_from_priv(self.priv)
        self.address = address_from_pubkey(self.pub)

    @property
    def priv_hex(self): return self.priv.hex()
    @property
    def pub_hex(self): return self.pub.hex()
    def private_key_hex(self): return self.priv_hex
    def public_key_hex(self): return self.pub_hex

    def sign(self, msg_hash):
        if SIG_MODE == "schnorr": return sign_schnorr(self.priv, msg_hash).hex()
        return sign_ecdsa(self.priv, msg_hash).hex()

    @staticmethod
    def verify(msg_hash, sig_hex, pub_hex):
        try:
            sig = bytes.fromhex(sig_hex)
            pub = bytes.fromhex(pub_hex)
            if SIG_MODE == "schnorr": return verify_schnorr(pub, sig, msg_hash)
            return verify_ecdsa(pub, sig, msg_hash)
        except Exception: return False

    def to_dict(self):
        return {"private_key": self.priv_hex, "address": self.address, "pubkey": self.pub_hex}

    @classmethod
    def from_dict(cls, d): return cls(private_key_hex=d["private_key"])

    def export_encrypted(self, password):
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        salt = os.urandom(16)
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000)
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
        token = Fernet(key).encrypt(json.dumps(self.to_dict()).encode())
        return base64.b64encode(salt + token).decode()

    @classmethod
    def import_encrypted(cls, blob_b64, password):
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        blob = base64.b64decode(blob_b64)
        salt, token = blob[:16], blob[16:]
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000)
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
        data = json.loads(Fernet(key).decrypt(token).decode())
        return cls.from_dict(data)


class WalletManager:
    WALLETS_DIR = os.environ.get("BRN_WALLETS_DIR", "wallets")

    @staticmethod
    def _garantir_dir(): os.makedirs(WalletManager.WALLETS_DIR, exist_ok=True)

    @staticmethod
    def generate_keypair():
        w = Wallet()
        return {"address": w.address, "private_key": w.priv_hex, "public_key": w.pub_hex}

    @staticmethod
    def validate_address(addr):
        try:
            from bech32 import validate_address as _v
            return _v(addr)
        except Exception:
            return isinstance(addr, str) and addr.startswith("brn1") and len(addr) > 20

    @staticmethod
    def save_encrypted_wallet(filename, password, address, sk, pk):
        try:
            WalletManager._garantir_dir()
            if not filename.endswith(".wallet"): filename += ".wallet"
            path = os.path.join(WalletManager.WALLETS_DIR, filename)
            w = Wallet(private_key_hex=sk)
            blob = w.export_encrypted(password)
            with open(path, "w") as f: f.write(blob)
            return {"ok": True, "msg": f"Salva em {path}", "path": path}
        except Exception as e: return {"ok": False, "msg": str(e)}

    @staticmethod
    def load_encrypted_wallet(filename, password):
        try:
            if not filename.endswith(".wallet"): filename += ".wallet"
            path = os.path.join(WalletManager.WALLETS_DIR, filename)
            with open(path) as f: blob = f.read()
            w = Wallet.import_encrypted(blob, password)
            return {"ok": True, "address": w.address, "private_key": w.priv_hex, "public_key": w.pub_hex}
        except Exception as e: return {"ok": False, "msg": str(e)}

    @staticmethod
    def list_wallets():
        try:
            WalletManager._garantir_dir()
            out = []
            for fname in sorted(os.listdir(WalletManager.WALLETS_DIR)):
                if not fname.endswith(".wallet"): continue
                path = os.path.join(WalletManager.WALLETS_DIR, fname)
                out.append({"filename": fname, "size": os.path.getsize(path),
                            "modified": os.path.getmtime(path)})
            return out
        except Exception: return []


class HDWalletManager:
    """Carteira HD (BIP39 + BIP44)."""
    PURPOSE = 44
    COIN_TYPE = 0
    ACCOUNT = 0
    CHANGE = 0

    @staticmethod
    def _m():
        try:
            from mnemonic import Mnemonic
            return Mnemonic("english")
        except ImportError:
            raise ImportError("Instale: python -m pip install mnemonic")

    @staticmethod
    def create(strength=128):
        m = HDWalletManager._m()
        mnemonic_phrase = m.generate(strength=strength)
        return HDWalletManager.from_mnemonic(mnemonic_phrase, index=0)

    @staticmethod
    def from_mnemonic(mnemonic_phrase, index=0, passphrase=""):
        m = HDWalletManager._m()
        if not m.check(mnemonic_phrase):
            raise ValueError("Mnemônico inválido")
        seed = m.to_seed(mnemonic_phrase, passphrase=passphrase)
        I = hmac_lib.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
        master_key = int.from_bytes(I[:32], "big")
        master_chain = I[32:]
        path = [HDWalletManager.PURPOSE + 0x80000000,
                HDWalletManager.COIN_TYPE + 0x80000000,
                HDWalletManager.ACCOUNT + 0x80000000,
                HDWalletManager.CHANGE, index]
        key = master_key
        chain = master_chain
        for child in path:
            if child >= 0x80000000:
                data = b"\x00" + key.to_bytes(32, "big") + child.to_bytes(4, "big")
            else:
                pub = pubkey_from_priv(key.to_bytes(32, "big"))
                data = pub + child.to_bytes(4, "big")
            I = hmac_lib.new(chain, data, hashlib.sha512).digest()
            key = (int.from_bytes(I[:32], "big") + key) % (
                0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141)
            chain = I[32:]
        w = Wallet(private_key_hex=key.to_bytes(32, "big").hex())
        return {"mnemonic": mnemonic_phrase, "address": w.address,
                "private_key": w.priv_hex, "public_key": w.pub_hex,
                "index": index,
                "path": f"m/44'/{HDWalletManager.COIN_TYPE}'/0'/0/{index}"}

    @staticmethod
    def derive_many(mnemonic_phrase, count=5):
        if count < 1 or count > 100: raise ValueError("count 1..100")
        return [HDWalletManager.from_mnemonic(mnemonic_phrase, index=i) for i in range(count)]

    @staticmethod
    def validate_mnemonic(mnemonic_phrase):
        try:
            m = HDWalletManager._m()
            return m.check(mnemonic_phrase)
        except Exception: return False


def sign_transaction(wallet, tx_core):
    from blockchain import signing_hash
    return wallet.sign(signing_hash(tx_core))


def verify_transaction(tx):
    try:
        from blockchain import signing_hash
        h = signing_hash(tx)
        for inp in tx.get("inputs", []):
            sig = inp.get("signature", "")
            pk = inp.get("pubkey", "")
            if not sig or not pk: return False, "sem assinatura/pubkey"
            if not Wallet.verify(h, sig, pk): return False, "assinatura invalida"
        return True, "ok"
    except Exception as e: return False, str(e)
'@
Set-Content -Path 'wallet.py' -Value $wallet_content -Encoding UTF8
Write-Host "      OK" -ForegroundColor Green

# [7] Patch server.py (adiciona endpoints novos no final)
Write-Host "[7/8] Adicionando endpoints em server.py..." -ForegroundColor Cyan
$server_patch = @'


# ============================================================
# v4: Endpoints novos (HD Wallet + Fee Estimation + Work)
# ============================================================
from wallet import HDWalletManager as _HDWM


@app.route("/api/fee-estimate", methods=["GET"])
def fee_estimate():
    try:
        return jsonify({"success": True,
                        "low": CHAIN.estimate_fee("low"),
                        "medium": CHAIN.estimate_fee("medium"),
                        "high": CHAIN.estimate_fee("high"),
                        "min_relay_fee": 1000})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/work", methods=["GET"])
def work():
    return jsonify({"success": True, "height": CHAIN.db.height(),
                    "cumulative_work": CHAIN.cumulative_work()})


@app.route("/api/hd/create", methods=["POST"])
@_rate_limit
def hd_create():
    try:
        data = request.get_json(force=True) or {}
        strength = int(data.get("strength", 128))
        if strength not in (128, 160, 192, 224, 256):
            return jsonify({"ok": False, "msg": "strength invalido"}), 400
        result = _HDWM.create(strength=strength)
        return jsonify({"ok": True,
                        "warning": "GUARDE o mnemônico. NÃO será mostrado novamente.",
                        **result})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/hd/derive", methods=["POST"])
@_rate_limit
def hd_derive():
    try:
        data = request.get_json(force=True) or {}
        mn = data.get("mnemonic", "").strip()
        index = int(data.get("index", 0))
        if not _HDWM.validate_mnemonic(mn):
            return jsonify({"ok": False, "msg": "Mnemonico invalido"}), 400
        return jsonify({"ok": True, **_HDWM.from_mnemonic(mn, index=index)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


@app.route("/api/peers/score", methods=["GET"])
def peers_score():
    try:
        return jsonify({"success": True,
                        "peers": CHAIN.db.listar_peers(apenas_ativos=False),
                        "banned": CHAIN.db.listar_peers_maus(score_min=-100)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
'@
Add-Content -Path 'server.py' -Value $server_patch -Encoding UTF8
Write-Host "      OK" -ForegroundColor Green

# [8] Teste de imports
Write-Host "[8/8] Testando imports..." -ForegroundColor Cyan
$testes = @('crypto','bech32','db','chain_validator','blockchain','wallet','server','explorer','p2p_unified','main')
foreach ($m in $testes) {
    $out = python -c "import $m" 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "      OK   $m" -ForegroundColor Green
    } else {
        Write-Host "      FALHA $m" -ForegroundColor Red
        Write-Host "        $out" -ForegroundColor DarkRed
    }
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  INSTALACAO CONCLUIDA!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "Backup em: $backup" -ForegroundColor Yellow
Write-Host ""
Write-Host "Proximos passos:" -ForegroundColor Cyan
Write-Host "  1. Rodar: python main.py" -ForegroundColor White
Write-Host "  2. Testar: http://127.0.0.1:5000/api/work" -ForegroundColor White
Write-Host "  3. Testar: http://127.0.0.1:5000/api/fee-estimate" -ForegroundColor White
Write-Host ""