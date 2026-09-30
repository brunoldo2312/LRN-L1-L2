"""fix_mine.py — Adiciona fallback de pubkey se estiver vazia."""
import re

with open("blockchain.py", "r", encoding="utf-8") as f:
    src = f.read()

# 1) mine_block: adiciona fallback se nao tiver
if "if not miner_pubkey:" not in src:
    src = src.replace(
        'def mine_block(self, miner_address, miner_pubkey=""):\n',
        'def mine_block(self, miner_address, miner_pubkey=""):\n'
        '        if not miner_pubkey:\n'
        '            miner_pubkey = "00" * 33\n',
        1,
    )

# 2) mine_block_interruptible: adiciona fallback
if src.count("if not miner_pubkey:") < 2:
    src = src.replace(
        'def mine_block_interruptible(self, miner_address, miner_pubkey="", should_continue=None):\n',
        'def mine_block_interruptible(self, miner_address, miner_pubkey="", should_continue=None):\n'
        '        if not miner_pubkey:\n'
        '            miner_pubkey = "00" * 33\n',
        1,
    )

# 3) make_coinbase: tira o raise
src = src.replace(
    'if not pubkey_hex:\n        raise ValueError("make_coinbase: pubkey_hex é obrigatória")',
    'if not pubkey_hex:\n        pubkey_hex = "00" * 33',
)

with open("blockchain.py", "w", encoding="utf-8") as f:
    f.write(src)

print("OK")