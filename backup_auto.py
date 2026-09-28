"""backup_auto.py - Backup rotativo do DB do BRN (v2)"""
import os
import shutil
import glob
from datetime import datetime

DB_FILE = "brn_v2_chain.db"
BACKUP_DIR = "backups"
MAX_BACKUPS = 10


def _copy_db_atomic(src, dst):
    """Copia o DB + WAL + SHM de forma atomica."""
    # Copia para arquivos .tmp primeiro
    shutil.copy2(src, dst + ".tmp")
    if os.path.exists(src + "-wal"):
        shutil.copy2(src + "-wal", dst + ".tmp-wal")
    if os.path.exists(src + "-shm"):
        shutil.copy2(src + "-shm", dst + ".tmp-shm")

    # Renomeia TODOS de uma vez (atômico)
    os.rename(dst + ".tmp", dst)
    if os.path.exists(dst + ".tmp-wal"):
        os.rename(dst + ".tmp-wal", dst + "-wal")
    if os.path.exists(dst + ".tmp-shm"):
        os.rename(dst + ".tmp-shm", dst + "-shm")


def backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if not os.path.exists(DB_FILE):
        print(f"[BACKUP] {DB_FILE} nao encontrado")
        return False

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"chain_{ts}.db")

    try:
        _copy_db_atomic(DB_FILE, dest)
        size_kb = os.path.getsize(dest) / 1024
        print(f"[BACKUP] OK: {dest} ({size_kb:.1f} KB)")
    except Exception as e:
        print(f"[BACKUP] ERRO: {e}")
        return False

    # Remove backups antigos
    backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "chain_*.db")))
    # Ignora .tmp
    backups = [b for b in backups if ".tmp" not in b]
    while len(backups) > MAX_BACKUPS:
        old = backups.pop(0)
        try:
            os.remove(old)
            for ext in ("-wal", "-shm"):
                if os.path.exists(old + ext):
                    os.remove(old + ext)
            print(f"[BACKUP] Removido antigo: {old}")
        except Exception:
            pass
    return True


if __name__ == "__main__":
    backup()