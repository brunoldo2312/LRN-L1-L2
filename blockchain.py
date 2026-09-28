"""blockchain.py — Núcleo da blockchain BRN
================================================================
Módulo principal da cadeia: blocos, transações, PoW, difficulty,
halving, merkle, coinbase, validação e mineração.
================================================================
"""
import time
import orjson
from crypto import double_sha256, sha256
from db import ChainDB

# ============================================================
# CONSTANTES DA REDE
# ============================================================
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

GENESIS_PREV = "0" * 64
GENESIS_TIMESTAMP = 1700000000
GENESIS_REWARD = INITIAL_REWARD
GENESIS_ADDRESS = "brn1qxyzk7y0v2j4g0a8d9n5t3m2k7h4s6w8c9p2e"
GENESIS_NONCE = 0


# ============================================================
# FUNÇÕES BÁSICAS DE BLOCKCHAIN
# ============================================================
def block_hash(prev_hash: str, merkle: str, timestamp: int,
               nonce: int, difficulty: int) -> str:
    """Calcula o hash de um bloco (double SHA-256 do header)."""
    header = f"{prev_hash}{merkle}{timestamp}{nonce}{difficulty}"
    return double_sha256(header.encode()).hex()


def target_from_difficulty(difficulty: int) -> int:
    """Converte dificuldade em target (número alvo)."""
    return int("0" * difficulty + "f" * (64 - difficulty), 16)


def meets_difficulty(h: str, difficulty: int) -> bool:
    """Verifica se um hash atende à dificuldade (PoW)."""
    return int(h, 16) <= target_from_difficulty(difficulty)


def compute_merkle_root(txids: list) -> str:
    """Calcula a raiz de Merkle de uma lista de txids."""
    if not txids:
        return "0" * 64
    layer = [bytes.fromhex(t) for t in txids]
    while len(layer) > 1:
        if len(layer) % 2:
            layer.append(layer[-1])
        layer = [sha256(layer[i] + layer[i + 1]) for i in range(0, len(layer), 2)]
    return layer[0].hex()


def make_coinbase(address: str, height: int, reward: int) -> dict:
    """Cria uma transação coinbase (recompensa de bloco)."""
    cb = {
        "txid": "",
        "inputs": [{
            "txid": "0" * 64,
            "vout": 0xFFFFFFFF,
            "pubkey": "",
            "signature": ""
        }],
        "outputs": [{
            "address": address,
            "amount": reward,
            "pubkey": ""
        }],
        "timestamp": int(time.time()),
        "locktime": 0,
        "height": height,
    }
    cb["txid"] = txid(cb)
    return cb


def txid(tx: dict) -> str:
    """Calcula o txid de uma transação."""
    core = {
        "inputs": [{"txid": i["txid"], "vout": i["vout"]} for i in tx["inputs"]],
        "outputs": tx["outputs"],
        "timestamp": tx["timestamp"],
        "locktime": tx.get("locktime", 0),
    }
    if "height" in tx:
        core["height"] = tx["height"]
    return double_sha256(orjson.dumps(core, option=orjson.OPT_SORT_KEYS)).hex()


def signing_hash(tx: dict) -> bytes:
    """Gera o hash que será assinado (para assinatura digital)."""
    core = {
        "inputs": [
            {"txid": i["txid"], "vout": i["vout"], "pubkey": i.get("pubkey", "")}
            for i in tx["inputs"]
        ],
        "outputs": tx["outputs"],
        "timestamp": tx["timestamp"],
        "locktime": tx.get("locktime", 0),
    }
    return double_sha256(orjson.dumps(core, option=orjson.OPT_SORT_KEYS))


# ============================================================
# BLOCO GÊNESIS
# ============================================================
def build_genesis() -> dict:
    """Constrói o bloco gênesis da cadeia."""
    cb = {
        "txid": "",
        "inputs": [{
            "txid": "0" * 64,
            "vout": 0xFFFFFFFF,
            "pubkey": "",
            "signature": ""
        }],
        "outputs": [{
            "address": GENESIS_ADDRESS,
            "amount": GENESIS_REWARD,
            "pubkey": ""
        }],
        "timestamp": GENESIS_TIMESTAMP,
        "locktime": 0,
        "height": 0,
    }
    cb["txid"] = txid(cb)
    merkle = compute_merkle_root([cb["txid"]])
    nonce = 0
    while True:
        h = block_hash(GENESIS_PREV, merkle, GENESIS_TIMESTAMP, nonce, 1)
        if h.startswith("0"):
            break
        nonce += 1
    return {
        "height": 0,
        "hash": h,
        "prev_hash": GENESIS_PREV,
        "timestamp": GENESIS_TIMESTAMP,
        "nonce": nonce,
        "merkle": merkle,
        "difficulty": 1,
        "transactions": [cb],
    }


GENESIS_BLOCK = build_genesis()


# ============================================================
# CLASSE PRINCIPAL: Blockchain
# ============================================================
class Blockchain:
    """Gerencia toda a cadeia: blocos, mempool, validação, mineração."""

    def __init__(self, db_path: str = "brn_v2_chain.db",
                 genesis_address: str = None):
        self.db = ChainDB(db_path)
        if self.db.height() < 0:
            g = GENESIS_BLOCK
            if genesis_address:
                g = self._genesis_with_address(genesis_address)
            self.db.add_block(g)
            self.db.apply_tx(g["transactions"][0], 0, coinbase=True)
            self.db.set_meta("genesis_hash", g["hash"])

    # --------------------------------------------------------
    # GÊNESIS CUSTOMIZADO
    # --------------------------------------------------------
    @staticmethod
    def _genesis_with_address(address: str) -> dict:
        """Gera bloco gênesis com endereço customizado."""
        cb = {
            "txid": "",
            "inputs": [{
                "txid": "0" * 64,
                "vout": 0xFFFFFFFF,
                "pubkey": "",
                "signature": ""
            }],
            "outputs": [{
                "address": address,
                "amount": GENESIS_REWARD,
                "pubkey": ""
            }],
            "timestamp": GENESIS_TIMESTAMP,
            "locktime": 0,
            "height": 0,
        }
        cb["txid"] = txid(cb)
        merkle = compute_merkle_root([cb["txid"]])
        nonce = 0
        while True:
            h = block_hash(GENESIS_PREV, merkle, GENESIS_TIMESTAMP, nonce, 1)
            if h.startswith("0"):
                break
            nonce += 1
        return {
            "height": 0,
            "hash": h,
            "prev_hash": GENESIS_PREV,
            "timestamp": GENESIS_TIMESTAMP,
            "nonce": nonce,
            "merkle": merkle,
            "difficulty": 1,
            "transactions": [cb],
        }

    # --------------------------------------------------------
    # RECOMPENSA E DIFICULDADE
    # --------------------------------------------------------
    def current_reward(self, height: int) -> int:
        """Calcula a recompensa atual (halving a cada 210k blocos)."""
        halvings = height // HALVING_INTERVAL
        if halvings >= 64:
            return 0
        return INITIAL_REWARD >> halvings

    def current_difficulty(self) -> int:
        """Calcula a dificuldade atual baseada no tempo dos últimos blocos."""
        h = self.db.height()
        if h < DIFFICULTY_INTERVAL:
            return INITIAL_DIFFICULTY
        start = self.db.get_block(h - DIFFICULTY_INTERVAL + 1)
        end = self.db.get_block(h)
        if not start or not end:
            return INITIAL_DIFFICULTY
        actual = max(1, end["timestamp"] - start["timestamp"])
        expected = BLOCK_TIME * DIFFICULTY_INTERVAL
        prev = end["difficulty"]
        new = int(prev * expected / actual)
        new = max(prev // 4, min(prev * 4, new))
        return max(1, new)

    # --------------------------------------------------------
    # VALIDAÇÃO DE TRANSAÇÕES
    # --------------------------------------------------------
    def validate_tx(self, tx: dict, from_mempool: bool = False):
        """Valida uma transação (UTXOs, assinatura, saldo)."""
        from wallet import Wallet

        if tx.get("txid") != txid(tx):
            return False, "txid invalido"
        if not tx["inputs"] or not tx["outputs"]:
            return False, "tx sem inputs ou outputs"
        if tx["inputs"][0]["txid"] == "0" * 64:
            return False, "coinbase invalida"

        in_sum = 0
        seen = set()
        for inp in tx["inputs"]:
            key = (inp["txid"], inp["vout"])
            if key in seen:
                return False, "input duplicado"
            seen.add(key)
            u = self.db.get_utxo(inp["txid"], inp["vout"])
            if not u:
                return False, f"UTXO inexistente {inp['txid'][:12]}"
            if u["pubkey"] and inp.get("pubkey", "") != u["pubkey"]:
                return False, "pubkey nao corresponde ao UTXO"
            in_sum += u["amount"]

        out_sum = 0
        for o in tx["outputs"]:
            if o["amount"] <= 0:
                return False, "output invalido"
            out_sum += o["amount"]
        if out_sum > in_sum:
            return False, "outputs > inputs"

        sig_hash = signing_hash(tx)
        for inp in tx["inputs"]:
            if not Wallet.verify(sig_hash,
                                 inp.get("signature", ""),
                                 inp.get("pubkey", "")):
                return False, "assinatura invalida"
        return True, "ok"

    def submit_tx(self, tx: dict):
        """Adiciona uma transação à mempool (após validar)."""
        if self.db.has_mempool(tx["txid"]):
            return False, "ja na mempool"
        ok, msg = self.validate_tx(tx)
        if not ok:
            return False, msg
        fee = self.tx_fee(tx)
        if fee < 0:
            return False, "fee negativa"
        if not self.db.add_mempool(tx, fee):
            return False, "falha na mempool"
        return True, tx["txid"]

    def tx_fee(self, tx: dict) -> int:
        """Calcula a taxa de uma transação (inputs - outputs)."""
        in_sum = 0
        for inp in tx["inputs"]:
            u = self.db.get_utxo(inp["txid"], inp["vout"])
            if u:
                in_sum += u["amount"]
        return in_sum - sum(o["amount"] for o in tx["outputs"])

    # --------------------------------------------------------
    # VALIDAÇÃO DE BLOCOS
    # --------------------------------------------------------
    def validate_block(self, block: dict, prev_block: dict = None):
        """Valida um bloco inteiro (PoW, merkle, tx, coinbase)."""
        if block["prev_hash"] != (prev_block["hash"] if prev_block else self.db.tip_hash()):
            return False, "prev_hash incorreto"

        expected_height = (prev_block["height"] + 1) if prev_block else self.db.height() + 1
        if block["height"] != expected_height:
            return False, "altura invalida"

        if not meets_difficulty(block["hash"], block["difficulty"]):
            return False, "PoW invalido"

        h = block_hash(block["prev_hash"], block["merkle"],
                       block["timestamp"], block["nonce"], block["difficulty"])
        if h != block["hash"]:
            return False, "hash incorreto"

        if compute_merkle_root([t["txid"] for t in block["transactions"]]) != block["merkle"]:
            return False, "merkle incorreto"

        cb = block["transactions"][0]
        if cb["inputs"][0]["txid"] != "0" * 64:
            return False, "primeira tx nao e coinbase"

        reward = self.current_reward(block["height"])
        fees = 0
        for i, t in enumerate(block["transactions"][1:], 1):
            if t["txid"] != txid(t):
                return False, f"txid invalido na posicao {i}"
            ok, msg = self.validate_tx(t)
            if not ok:
                return False, msg
            fees += self.tx_fee(t)

        total_cb = sum(o["amount"] for o in cb["outputs"])
        if total_cb > reward + fees:
            return False, "coinbase acima do permitido"

        return True, "ok"

    def accept_block(self, block: dict):
        """Aceita um bloco após validação (aplica ao DB)."""
        prev = self.db.get_block_by_hash(block["prev_hash"])
        ok, msg = self.validate_block(block, prev)
        if not ok:
            return False, msg
        self.db.add_block(block)
        for i, t in enumerate(block["transactions"]):
            self.db.apply_tx(t, block["height"], coinbase=(i == 0))
            if i > 0:
                self.db.remove_mempool(t["txid"])
        return True, block["hash"]

    # --------------------------------------------------------
    # MINERAÇÃO
    # --------------------------------------------------------
    def mine_block(self, miner_address: str):
        """Minera um bloco (PoW loop até atender à dificuldade)."""
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
            if meets_difficulty(h, diff):
                break
            nonce += 1
            if nonce % 200000 == 0:
                ts = int(time.time())
        block = {
            "height": height,
            "hash": h,
            "prev_hash": prev_hash,
            "timestamp": ts,
            "nonce": nonce,
            "merkle": merkle,
            "difficulty": diff,
            "transactions": txs,
        }
        ok, msg = self.accept_block(block)
        return block if ok else None

    def mine_block_interruptible(self, miner_address: str, should_continue):
        """Mineração interrompível (usada pela GUI com botão Ativar/Parar)."""
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
            if meets_difficulty(h, diff):
                break
            nonce += 1
            if nonce % 50000 == 0:
                ts = int(time.time())
        block = {
            "height": height,
            "hash": h,
            "prev_hash": prev_hash,
            "timestamp": ts,
            "nonce": nonce,
            "merkle": merkle,
            "difficulty": diff,
            "transactions": txs,
        }
        ok, msg = self.accept_block(block)
        return block if ok else None


# ============================================================
# HOOK PARA chain_validator.py
# ============================================================
try:
    from chain_validator import verify_chain as _verify_ext

    def _verify_chain_method(self, full: bool = True):
        return _verify_ext(self)

    Blockchain.verify_chain = _verify_chain_method
    Blockchain.verify_chain_dict = lambda self: _verify_ext(self).to_dict()
    print("chain_validator.py plugado em Blockchain")
except ImportError:
    print("chain_validator.py nao encontrado (funcao verify_chain desabilitada)")
