"""
chain_validator.py — Validação de integridade da cadeia BRN
============================================================
Extraído de blockchain.py (Single Responsibility Principle).
Uso:
    from chain_validator import verify_chain, verify_chain_dict

    result = verify_chain(blockchain)         # objeto ChainVerificationResult
    result = verify_chain_dict(blockchain)    # dict serializável JSON
============================================================
"""

import time


# ============================================================
# RESULTADO
# ============================================================
class ChainVerificationResult:
    def __init__(self):
        self.valid = True
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.blocks_checked = 0
        self.txs_checked = 0
        self.height = 0
        self.tip_hash = ""
        self.elapsed_s = 0.0

    def add_error(self, msg: str):
        self.valid = False
        self.errors.append(msg)

    def add_warning(self, msg: str):
        self.warnings.append(msg)

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "blocks_checked": self.blocks_checked,
            "txs_checked": self.txs_checked,
            "height": self.height,
            "tip_hash": self.tip_hash,
            "elapsed_s": self.elapsed_s,
            "summary": self.summary(),
        }

    def summary(self) -> str:
        if self.valid and not self.warnings:
            return "✅ Cadeia íntegra — nenhum problema encontrado."
        parts = ["✅ Cadeia válida" if self.valid else "❌ Cadeia INVÁLIDA"]
        if self.errors:
            parts.append(f"{len(self.errors)} erro(s)")
        if self.warnings:
            parts.append(f"{len(self.warnings)} aviso(s)")
        return " | ".join(parts)


# ============================================================
# VALIDAÇÃO
# ============================================================
def verify_chain(blockchain) -> ChainVerificationResult:
    """Verifica toda a cadeia do gênesis até o topo."""
    from blockchain import (
        block_hash, meets_difficulty, compute_merkle_root,
        txid as calc_txid, GENESIS_PREV,
    )

    result = ChainVerificationResult()
    t0 = time.time()

    db = blockchain.db
    height = db.height()
    result.height = height
    result.tip_hash = db.tip_hash()

    if height < 0:
        result.add_error("Cadeia vazia (nenhum bloco).")
        return result

    spent_utxos: set[tuple[str, int]] = set()
    all_txids: set[str] = set()
    prev_hash: str | None = None

    for h in range(height + 1):
        block = db.get_block(h)
        if not block:
            result.add_error(f"Bloco #{h} ausente no banco.")
            continue

        result.blocks_checked += 1
        prefixo = f"Bloco #{h}"

        if h == 0:
            if block["height"] != 0:
                result.add_error(f"{prefixo}: height != 0")
            if block["prev_hash"] != GENESIS_PREV:
                result.add_error(f"{prefixo}: prev_hash de gênese inválido")

        if block["height"] != h:
            result.add_error(f"{prefixo}: height declarado = {block['height']}")

        if h > 0 and block["prev_hash"] != prev_hash:
            result.add_error(f"{prefixo}: prev_hash não corresponde ao bloco #{h-1}")

        recalc = block_hash(
            block["prev_hash"], block["merkle"], block["timestamp"],
            block["nonce"], block["difficulty"],
        )
        if recalc != block["hash"]:
            result.add_error(f"{prefixo}: hash adulterado")

        if not meets_difficulty(block["hash"], block["difficulty"]):
            result.add_error(f"{prefixo}: não atende à dificuldade {block['difficulty']}")

        txids = [t["txid"] for t in block["transactions"]]
        merkle_calc = compute_merkle_root(txids)
        if merkle_calc != block["merkle"]:
            result.add_error(f"{prefixo}: merkle divergente")

        if not block["transactions"]:
            result.add_error(f"{prefixo}: bloco sem transações")
            prev_hash = block["hash"]
            continue

        cb = block["transactions"][0]
        is_cb = bool(cb["inputs"]) and cb["inputs"][0]["txid"] == "0" * 64
        if not is_cb:
            result.add_error(f"{prefixo}: primeira tx não é coinbase")

        if is_cb:
            reward_esp = blockchain.current_reward(h)
            fees = 0
            for t in block["transactions"][1:]:
                try:
                    fees += blockchain.tx_fee(t)
                except Exception:
                    pass
            cb_total = sum(o["amount"] for o in cb["outputs"])
            if cb_total > reward_esp + fees:
                result.add_error(f"{prefixo}: coinbase {cb_total} > recompensa+fees")

        for idx, t in enumerate(block["transactions"]):
            result.txs_checked += 1
            short = t.get("txid", "?")[:12]

            try:
                if t["txid"] != calc_txid(t):
                    result.add_error(f"{prefixo}: tx {short}… com txid adulterado")
            except Exception as e:
                result.add_error(f"{prefixo}: erro ao recalcular txid ({e})")

            if t["txid"] in all_txids:
                result.add_error(f"{prefixo}: txid duplicada {short}…")
            all_txids.add(t["txid"])

            if idx == 0 and is_cb:
                continue

            for inp in t["inputs"]:
                key = (inp["txid"], inp["vout"])
                if key in spent_utxos:
                    result.add_error(f"{prefixo}: gasto duplo — {inp['txid'][:12]}…:{inp['vout']}")
                spent_utxos.add(key)

        prev_hash = block["hash"]

    try:
        neg = db.conn.execute(
            "SELECT address, SUM(amount) AS s FROM utxos "
            "WHERE spent=0 GROUP BY address HAVING s < 0"
        ).fetchall()
        for row in neg:
            result.add_error(f"Saldo negativo: {row['address'][:16]}…")
    except Exception:
        pass

    result.elapsed_s = round(time.time() - t0, 3)
    return result


def verify_chain_dict(blockchain) -> dict:
    return verify_chain(blockchain).to_dict()
