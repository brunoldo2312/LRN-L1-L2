"""
sync_manual.py — Sincroniza o no local com um peer especifico.
Protocolo antigo BRN5 (texto claro) — compativel com nos BRN v5.
Uso: python sync_manual.py 192.168.0.10 6001
"""
import sys
import json
import socket

from blockchain import Blockchain


NETWORK_MAGIC = b"BRN5"
MAX_MSG_SIZE = 2 * 1024 * 1024


def _recv_msg(sock):
    raw = sock.recv(MAX_MSG_SIZE)
    if not raw or not raw.startswith(NETWORK_MAGIC):
        return None
    return json.loads(raw[len(NETWORK_MAGIC):].decode())


def _send_msg(sock, msg):
    sock.sendall(NETWORK_MAGIC + json.dumps(msg).encode())


def _rpc(ip, port, msg, timeout=15.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((ip, port))
    try:
        _send_msg(s, msg)
        return _recv_msg(s)
    finally:
        try:
            s.close()
        except Exception:
            pass


def main():
    if len(sys.argv) < 3:
        print("Uso: python sync_manual.py <ip> <porta>")
        return 1

    ip   = sys.argv[1]
    port = int(sys.argv[2])

    chain = Blockchain("brn_v2_chain.db", auto_genesis=False)
    local_h = chain.db.height()
    local_tip = chain.db.tip_hash()
    print(f"[sync] altura local: {local_h}")
    print(f"[sync] tip local   : {local_tip[:24]}...")

    r = _rpc(ip, port, {"type": "get_block", "height": local_h})
    if not r or "block" not in r or not r["block"]:
        print(f"[sync] peer nao tem bloco #{local_h}")
        return 1
    peer_blk = r["block"]
    peer_hash = peer_blk["hash"]
    print(f"[sync] hash peer   : {peer_hash[:24]}...")

    if peer_hash != local_tip:
        print(f"[sync] FORK! Bloco #{local_h} diverge entre local e peer")
        print(f"[sync] Este e o ponto exato de divergencia.")
        return 1

    print(f"[sync] bloco #{local_h} bate. Mesma cadeia ate aqui.")

    r = _rpc(ip, port, {"type": "get_chain_height"})
    remote_h = r.get("height", -1)
    print(f"[sync] altura peer: {remote_h}")

    if remote_h <= local_h:
        print("[sync] peer nao tem blocos novos. Nada a fazer.")
        return 0

    BATCH = 5
    applied = 0
    cursor = local_h + 1
    while cursor <= remote_h:
        end = min(cursor + BATCH, remote_h + 1)
        r = _rpc(ip, port, {"type": "get_blocks_range",
                            "start": cursor, "end": end})
        if not r or "blocks" not in r:
            print(f"[sync] falha no lote {cursor}-{end}")
            return 1
        lote = r["blocks"]
        if not lote:
            break
        print(f"[sync] lote {cursor}-{end}: {len(lote)} blocos")

        for blk in lote:
            expected_h = chain.db.height() + 1
            expected_prev = chain.db.tip_hash()
            if blk["height"] != expected_h:
                print(f"[sync] ERRO: bloco #{blk['height']} fora de sequencia "
                      f"(esperado #{expected_h})")
                return 1
            if blk["prev_hash"] != expected_prev:
                print(f"[sync] ERRO: prev_hash diverge em #{blk['height']}")
                return 1
            ok, msg = chain.accept_block(blk)
            if not ok:
                print(f"[sync] ERRO aplicando #{blk['height']}: {msg}")
                return 1
            applied += 1

        cursor = end

    print(f"[sync] OK! {applied} blocos aplicados")
    print(f"[sync] nova altura: {chain.db.height()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())