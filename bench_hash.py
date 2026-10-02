"""
bench_hash.py — Mede o hashrate real do seu PC com o hot loop do BRN.
Rode antes de escolher MAX_DIFFICULTY.
"""
import time
import hashlib
import os

def bench(seconds=3):
    prefix = b"0" * 64 + b"a" * 64 + b"1700000000"
    diff_bytes = b"5"
    target = int("0" * 5 + "f" * (64 - 5), 16)
    sha = hashlib.sha256

    t0 = time.time()
    nonce = 0
    while time.time() - t0 < seconds:
        for _ in range(5000):
            data = prefix + str(nonce).encode() + diff_bytes
            h = sha(sha(data).digest()).digest()
            if int.from_bytes(h, "big") <= target:
                nonce = 0  # ignora, é só bench
            nonce += 1
    dur = time.time() - t0
    return nonce / dur

if __name__ == "__main__":
    print(f"CPU         : {os.cpu_count()} nucleo(s)")
    print("Aquecendo...")
    bench(1)
    print("Rodando benchmark (5s)...")
    hr = bench(5)
    print(f"Hashrate    : {hr:,.0f} H/s")
    print()
    print("Sugestao de MAX_DIFFICULTY para tempo-alvo de 10 min/bloco:")
    for d in range(5, 12):
        expected = 16 ** d
        seg = expected / hr
        if seg < 60:
            tempo = f"{seg:.1f} s"
        elif seg < 3600:
            tempo = f"{seg/60:.1f} min"
        elif seg < 86400:
            tempo = f"{seg/3600:.1f} h"
        else:
            tempo = f"{seg/86400:.1f} dias"
        sugestao = " <-- recomendado" if 300 < seg < 1200 else ""
        print(f"  d={d:>2}  ~ {tempo:>12}{sugestao}")
