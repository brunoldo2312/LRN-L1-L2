"""
blockchain.py — Núcleo da Blockchain BRN
Versão: 6.0 | Data: 29/09/2026

Changelog v6.0 (breaking):
- [CRÍTICO] make_coinbase/build_genesis exigem pubkey na saída.
- [CRÍTICO] validate_tx exige pubkey EXATA entre input e UTXO.
- [CRÍTICO] txid() e signing_hash() derivam do MESMO _tx_core().
- [MELHORIA] signing_hash prefixa b"BRN-TX-v1|" (domain separation).
- [MELHORIA] versão explícita (_CORE_V) na serialização.
- [API] mine_block e mine_block_interruptible exigem miner_pubkey.

v5.1: + confirmações de transação
v5:   + nonce (proteção replay) em signing_hash, txid e validate_tx
v4:   + cumulative_work, + reorg_to, + estimate_fee
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
GENESIS_ADDRESS = "brn1qxyzk7y0v2j4g0a8d9n5t2k7h4s6w8c9p2e"

# v6: pubkey correspondente ao GENESIS_ADDRESS.
# Se ficar vazia, a UTXO do gênese é NÃO-GASTÁVEL (validate_tx exige
# correspondência exata entre input.pubkey e utxo.pubkey).
GENESIS_PUBKEY = ""

# ============================================================
# v6: DOMAIN SEPARATION
# ============================================================
SIGNING_DOMAIN = b"BRN-TX-v1|"
_CORE_V = 1


# ============================================================
# PoW / DIFICULDADE
# ============================================================
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


# ============================================================
# v6: SERIALIZAÇÃO CANÔNICA
# ============================================================
def _tx_core(tx):
    """
    Núcleo canônico. TUDO que importa para identidade e autoria
    passa por aqui — txid() e signing_hash() usam este mesmo core.
    """
    inputs = [
        {
            "txid": i["txid"],
            "vout": i["vout"],
            "pubkey": i.get("pubkey", ""),
        }
        for i in tx["inputs"]
    ]
    outputs = [
        {
            "address": o["address"],
            "amount": o["amount"],
            "pubkey": o.get("pubkey", ""),
        }
        for o in tx["outputs"]
    ]
    core = {
        "v": _CORE_V,
        "inputs": inputs,
        "outputs": outputs,
        "timestamp": tx["timestamp"],
        "locktime": tx.get("locktime", 0),
        "nonce": tx.get("nonce", 0),
    }
    if "height" in tx:
        core["height"] = tx["height"]
    return core


def _serialize_core(tx):
    return orjson.dumps(_tx_core(tx), option=orjson.OPT_SORT_KEYS)


def txid(tx):
    """Identidade da tx. Derivada do mesmo core que é assinado."""
    return double_sha256(_serialize_core(tx)).hex()


def signing_hash(tx):
    """Hash efetivamente assinado, com domain separation."""
    return double_sha256(SIGNING_DOMAIN + _serialize_core(tx))


# ============================================================
# v6: COINBASE E GÊNESE
# ============================================================
def make_coinbase(address, pubkey_hex, height, reward):
    """Cria uma coinbase. pubkey_hex é OBRIGATÓRIA."""
    if not pubkey_hex:
        raise ValueError("make_coinbase: pubkey_hex é obrigatória")
    cb = {
        "txid": "",
        "inputs": [{"txid": "0" * 64, "vout": 0xFFFFFFFF,
                    "pubkey": "", "signature": ""}],
        "outputs": [{"address": address, "amount": reward,
                     "pubkey": pubkey_hex}],
        "timestamp": int(time.time()),
        "locktime": 0,
        "height": height,
        "nonce": 0,
    }
    cb["txid"] = txid(cb)
    return cb


def build_genesis():
    cb = {
        "txid": "",
        "inputs": [{"txid": "0" * 64, "vout": 0xFFFFFFFF,
                    "pubkey": "", "signature": ""}],
        "outputs": [{"address": GENESIS_ADDRESS, "amount": GENESIS_REWARD,
                     "pubkey": GENESIS_PUBKEY}],
        "timestamp": GENESIS_TIMESTAMP,
        "locktime": 0,
        "height": 0,
        "nonce": 0,
    }
    cb["txid"] = txid(cb)
    merkle = compute_merkle_root([cb["txid"]])
    nonce = 0
    while True:
        h = block_hash(GENESIS_PREV, merkle, GENESIS_TIMESTAMP, nonce, 1)
        if h.startswith("0"):
            break
        nonce += 1
    return {"height": 0, "hash": h, "prev_hash": GENESIS_PREV,
            "timestamp": GENESIS_TIMESTAMP, "nonce": nonce,
            "merkle": merkle, "difficulty": 1, "transactions": [cb]}


GENESIS_BLOCK = build_genesis()


# ============================================================
# BLOCKCHAIN
# ============================================================
class Blockchain:
    def __init__(self, db_path="brn_v2_chain.db",
                 genesis_address=None, genesis_pubkey=None):
        self.db = ChainDB(db_path)
        if self.db.height() < 0:
            g = GENESIS_BLOCK
            if genesis_address:
                g = self._genesis_with_address(
                    genesis_address,
                    genesis_pubkey if genesis_pubkey is not None else GENESIS_PUBKEY,
                )
            self.db.add_block(g)
            self.db.apply_tx(g["transactions"][0], 0, coinbase=True)
            self.db.set_meta("genesis_hash", g["hash"])

    @staticmethod
    def _genesis_with_address(address, pubkey_hex=""):
        cb = {
            "txid": "",
            "inputs": [{"txid": "0" * 64, "vout": 0xFFFFFFFF,
                        "pubkey": "", "signature": ""}],
            "outputs": [{"address": address, "amount": GENESIS_REWARD,
                         "pubkey": pubkey_hex}],
            "timestamp": GENESIS_TIMESTAMP,
            "locktime": 0,
            "height": 0,
            "nonce": 0,
        }
        cb["txid"] = txid(cb)
        merkle = compute_merkle_root([cb["txid"]])
        nonce = 0
        while True:
            h = block_hash(GENESIS_PREV, merkle, GENESIS_TIMESTAMP, nonce, 1)
            if h.startswith("0"):
                break
            nonce += 1
        return {"height": 0, "hash": h, "prev_hash": GENESIS_PREV,
                "timestamp": GENESIS_TIMESTAMP, "nonce": nonce,
                "merkle": merkle, "difficulty": 1, "transactions": [cb]}

    # --------------------------------------------------------
    # RECOMPENSA / DIFICULDADE / TRABALHO
    # --------------------------------------------------------
    def current_reward(self, height):
        halvings = height // HALVING_INTERVAL
        if halvings >= 64:
            return 0
        return INITIAL_REWARD >> halvings

    def current_difficulty(self):
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

    def cumulative_work(self):
        total = 0
        for h in range(self.db.height() + 1):
            b = self.db.get_block(h)
            if b:
                total += work_from_difficulty(b["difficulty"])
        return total

    def cumulative_work_of_chain(self, blocks):
        return sum(work_from_difficulty(b["difficulty"]) for b in blocks)

    # --------------------------------------------------------
    # FEE
    # --------------------------------------------------------
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

    def tx_fee(self, tx):
        in_sum = 0
        for inp in tx["inputs"]:
            u = self.db.get_utxo(inp["txid"], inp["vout"])
            if u:
                in_sum += u["amount"]
        return in_sum - sum(o["amount"] for o in tx["outputs"])

    # --------------------------------------------------------
    # v6: VALIDAÇÃO DE TRANSAÇÃO
    # --------------------------------------------------------
    def validate_tx(self, tx, from_mempool=False):
        from wallet import Wallet

        if not tx.get("inputs") or not tx.get("outputs"):
            return False, "tx sem inputs/outputs"
        if tx["inputs"][0]["txid"] == "0" * 64:
            return False, "coinbase invalida"

        if tx.get("txid") != txid(tx):
            return False, "txid invalido"

        for inp in tx["inputs"]:
            if not inp.get("pubkey"):
                return False, "input sem pubkey"

        tx_nonce = tx.get("nonce", 0)
        for inp in tx["inputs"]:
            pk = inp["pubkey"]
            expected = self.db.get_nonce_for_pubkey(pk)
            if tx_nonce != expected:
                return False, (
                    f"nonce invalido para {pk[:16]}... "
                    f"(esperado {expected}, recebido {tx_nonce})"
                )

        in_sum = 0
        seen = set()
        for inp in tx["inputs"]:
            key = (inp["txid"], inp["vout"])
            if key in seen:
                return False, "input duplicado"
            seen.add(key)

            u = self.db.get_utxo(inp["txid"], inp["vout"])
            if not u:
                return False, "UTXO inexistente"

            if inp["pubkey"] != u["pubkey"]:
                return False, (
                    f"pubkey mismatch: input={inp['pubkey'][:16]}... "
                    f"utxo={u['pubkey'][:16] if u['pubkey'] else '(vazio)'}"
                )
            in_sum += u["amount"]

        out_sum = sum(o["amount"] for o in tx["outputs"])
        if out_sum > in_sum:
            return False, "outputs > inputs"
        if in_sum - out_sum < MIN_RELAY_FEE:
            return False, "fee abaixo do minimo"

        sig_hash = signing_hash(tx)
        for inp in tx["inputs"]:
            if not Wallet.verify(sig_hash, inp.get("signature", ""), inp["pubkey"]):
                return False, "assinatura invalida"

        return True, "ok"

    def submit_tx(self, tx):
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

    # --------------------------------------------------------
    # v6: VALIDAÇÃO DE BLOCO
    # --------------------------------------------------------
    def validate_block(self, block, prev_block=None):
        if block["prev_hash"] != (prev_block["hash"] if prev_block else self.db.tip_hash()):
            return False, "prev_hash incorreto"
        expected_height = (prev_block["height"] + 1) if prev_block else self.db.height() + 1
        if block["height"] != expected_height:
            return False, "altura invalida"
        if not meets_difficulty(block["hash"], block["difficulty"]):
            return False, "PoW invalido"
        h = block_hash(block["prev_hash"], block["merkle"], block["timestamp"],
                       block["nonce"], block["difficulty"])
        if h != block["hash"]:
            return False, "hash incorreto"
        if compute_merkle_root([t["txid"] for t in block["transactions"]]) != block["merkle"]:
            return False, "merkle incorreto"

        cb = block["transactions"][0]
        if cb["inputs"][0]["txid"] != "0" * 64:
            return False, "primeira tx nao e coinbase"
        if cb.get("txid") != txid(cb):
            return False, "coinbase txid invalido"
        for out in cb["outputs"]:
            if not out.get("pubkey"):
                return False, "coinbase output sem pubkey"
        for inp in cb["inputs"]:
            if inp.get("signature"):
                return False, "coinbase input com assinatura"

        reward = self.current_reward(block["height"])
        fees = 0
        for i, t in enumerate(block["transactions"][1:], 1):
            if t["txid"] != txid(t):
                return False, "txid invalido"
            ok, msg = self.validate_tx(t)
            if not ok:
                return False, f"tx #{i}: {msg}"
            fees += self.tx_fee(t)

        total_cb = sum(o["amount"] for o in cb["outputs"])
        if total_cb > reward + fees:
            return False, "coinbase acima do permitido"
        return True, "ok"

    def accept_block(self, block):
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
    # REORG
    # --------------------------------------------------------
    def reorg_to(self, new_blocks):
        if not new_blocks:
            return False, "lista vazia"
        fork_height = -1
        for i, b in enumerate(new_blocks):
            local = self.db.get_block(b["height"])
            if local and local["hash"] == b["hash"]:
                fork_height = b["height"]
            else:
                break
        if fork_height < 0:
            return False, "nenhum ponto em comum"
        blocks_to_add = [b for b in new_blocks if b["height"] > fork_height]
        if not blocks_to_add:
            return False, "nada novo"
        work_alt = self.cumulative_work_of_chain(blocks_to_add)
        work_local = 0
        for h in range(fork_height + 1, self.db.height() + 1):
            b = self.db.get_block(h)
            if b:
                work_local += work_from_difficulty(b["difficulty"])
        if work_alt <= work_local:
            return False, "local tem mais trabalho"
        depth = self.db.height() - fork_height
        if depth > MAX_REORG_DEPTH:
            return False, "reorg muito profundo"
        print(f"[REORG] fork #{fork_height}, -{depth}, +{len(blocks_to_add)}")
        prev = self.db.get_block(fork_height) if fork_height >= 0 else None
        for b in blocks_to_add:
            ok, msg = self.validate_block(b, prev)
            if not ok:
                return False, f"bloco #{b['height']}: {msg}"
            prev = b
        backups = []
        for h in range(fork_height + 1, self.db.height() + 1):
            b = self.db.get_block(h)
            if b:
                backups.append(b)
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
                    try:
                        self.accept_block(backup)
                    except Exception:
                        pass
                return False, f"falha no reorg: {msg}"
        print(f"[REORG] concluido! Altura: {self.db.height()}")
        return True, f"reorg ok ({len(blocks_to_add)} blocos)"

    # --------------------------------------------------------
    # v6: MINERAÇÃO
    # --------------------------------------------------------
    def mine_block(self, miner_address, miner_pubkey):
        height = self.db.height() + 1
        reward = self.current_reward(height)
        diff = self.current_difficulty()
        cb = make_coinbase(miner_address, miner_pubkey, height, reward)
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
        block = {"height": height, "hash": h, "prev_hash": prev_hash,
                 "timestamp": ts, "nonce": nonce, "merkle": merkle,
                 "difficulty": diff, "transactions": txs}
        ok, msg = self.accept_block(block)
        return block if ok else None

    def mine_block_interruptible(self, miner_address, miner_pubkey, should_continue):
        height = self.db.height() + 1
        reward = self.current_reward(height)
        diff = self.current_difficulty()
        cb = make_coinbase(miner_address, miner_pubkey, height, reward)
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
        block = {"height": height, "hash": h, "prev_hash": prev_hash,
                 "timestamp": ts, "nonce": nonce, "merkle": merkle,
                 "difficulty": diff, "transactions": txs}
        ok, msg = self.accept_block(block)
        return block if ok else None

    # --------------------------------------------------------
    # CONFIRMAÇÕES
    # --------------------------------------------------------
    def get_latest_height(self) -> int:
        return self.db.height()

    def get_transaction_block_height(self, txid: str) -> int:
        return self.db.get_tx_block_height(txid)

    def count_confirmations(self, txid: str) -> int:
        tx_height = self.get_transaction_block_height(txid)
        if tx_height == -1:
            return 0
        current_height = self.get_latest_height()
        return current_height - tx_height


# ------------------------------------------------------------
# Plug do chain_validator
# ------------------------------------------------------------
try:
    from chain_validator import verify_chain as _verify_ext

    def _verify_chain_method(self, full=True):
        return _verify_ext(self)

    Blockchain.verify_chain = _verify_chain_method
    Blockchain.verify_chain_dict = lambda self: _verify_ext(self).to_dict()
    print("chain_validator.py plugado em Blockchain")
except ImportError:
    print("chain_validator.py nao encontrado")