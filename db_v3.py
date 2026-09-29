"""
db_v3.py — Banco de Dados da Blockchain BRN
Versão: 3.2 | Data: 29/09/2026
- SQLite persistente para blocos e transações
- Índices para consultas rápidas
- Suporte a SPV (cabeçalhos de bloco)
- Consulta de altura de transações para confirmações
"""

import sqlite3
import json
import os
from typing import List, Dict, Optional, Tuple, Any


# ============================================================
# CONFIGURAÇÕES
# ============================================================

DB_FILE = "brn_v2_chain.db"
DB_TIMEOUT = 30.0


# ============================================================
# CLASSE PRINCIPAL DO BANCO DE DADOS
# ============================================================

class BlockchainDB:
    def __init__(self, db_path: str = DB_FILE):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        self._lock = False  # Controle simples de acesso
        self.connect()
        self.init_tables()

    def connect(self) -> None:
        """Conectar ao banco de dados"""
        self.conn = sqlite3.connect(
            self.db_path,
            timeout=DB_TIMEOUT,
            check_same_thread=False
        )
        self.conn.row_factory = sqlite3.Row
        # Melhorias de performance
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA cache_size=-2000")

    def close(self) -> None:
        """Fechar conexão com o banco"""
        if self.conn:
            self.conn.close()
            self.conn = None

    # ========================================================
    # INICIALIZAÇÃO DAS TABELAS
    # ========================================================

    def init_tables(self) -> None:
        """Criar todas as tabelas se não existirem"""
        cursor = self.conn.cursor()

        # Tabela de Blocos
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS blocks (
                height INTEGER PRIMARY KEY,
                hash TEXT UNIQUE NOT NULL,
                previous_hash TEXT NOT NULL,
                merkle_root TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                difficulty INTEGER NOT NULL,
                nonce INTEGER NOT NULL,
                cumulative_work INTEGER DEFAULT 0,
                transactions TEXT NOT NULL DEFAULT '[]'
            ) WITHOUT ROWID;
        """)

        # Tabela de Transações
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                txid TEXT PRIMARY KEY,
                block_height INTEGER NOT NULL,
                tipo TEXT NOT NULL,
                de TEXT NOT NULL,
                para TEXT NOT NULL,
                valor REAL NOT NULL,
                taxa REAL DEFAULT 0,
                timestamp INTEGER NOT NULL,
                assinatura TEXT,
                dados TEXT,
                FOREIGN KEY (block_height) REFERENCES blocks(height)
            ) WITHOUT ROWID;
        """)

        # Tabela de Cabeçalhos de Blocos (para modo SPV / Nó Leve)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS block_headers (
                height INTEGER PRIMARY KEY,
                block_hash TEXT NOT NULL UNIQUE,
                prev_hash TEXT NOT NULL,
                merkle_root TEXT NOT NULL,
                timestamp INTEGER NOT NULL
            ) WITHOUT ROWID;
        """)

        # Índices para consultas rápidas
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tx_address ON transactions(de, para);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_tx_block ON transactions(block_height);
        """)

        self.conn.commit()

    # ========================================================
    # OPERAÇÕES COM BLOCOS
    # ========================================================

    def save_block(self, block_data: Dict) -> bool:
        """Salvar um bloco completo no banco"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO blocks
                (height, hash, previous_hash, merkle_root,
                 timestamp, difficulty, nonce, cumulative_work, transactions)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                block_data["index"],
                block_data["hash"],
                block_data["previous_hash"],
                block_data["merkle_root"],
                int(block_data["timestamp"]),
                block_data["difficulty"],
                block_data["nonce"],
                block_data.get("cumulative_work", 0),
                json.dumps(block_data["transactions"])
            ))

            # Salvar também os cabeçalhos para modo SPV
            self.save_header(
                block_data["index"],
                block_data["hash"],
                block_data["previous_hash"],
                block_data["merkle_root"],
                int(block_data["timestamp"])
            )

            # Salvar cada transação
            for tx in block_data["transactions"]:
                self.save_transaction(tx, block_data["index"])

            self.conn.commit()
            return True
        except Exception as e:
            print(f"[DB] Erro ao salvar bloco: {e}")
            self.conn.rollback()
            return False

    def save_header(self, height: int, block_hash: str,
                    prev_hash: str, merkle_root: str, timestamp: int) -> None:
        """Salvar apenas cabeçalho do bloco (modo SPV)"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO block_headers
                (height, block_hash, prev_hash, merkle_root, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (height, block_hash, prev_hash, merkle_root, timestamp))
            self.conn.commit()
        except Exception as e:
            print(f"[DB] Erro ao salvar cabeçalho: {e}")

    def get_latest_block(self) -> Optional[Dict]:
        """Obter o bloco mais recente da cadeia"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM blocks ORDER BY height DESC LIMIT 1")
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_block(row)
        except Exception as e:
            print(f"[DB] Erro ao buscar último bloco: {e}")
            return None

    def get_block(self, height: int) -> Optional[Dict]:
        """Buscar bloco por altura"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM blocks WHERE height = ?", (height,))
            row = cursor.fetchone()
            return self._row_to_block(row) if row else None
        except Exception as e:
            print(f"[DB] Erro ao buscar bloco: {e}")
            return None

    def get_block_by_hash(self, block_hash: str) -> Optional[Dict]:
        """Buscar bloco por hash"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM blocks WHERE hash = ?", (block_hash,))
            row = cursor.fetchone()
            return self._row_to_block(row) if row else None
        except Exception as e:
            print(f"[DB] Erro ao buscar bloco por hash: {e}")
            return None

    def get_chain_height(self) -> int:
        """Retornar altura atual da cadeia"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT MAX(height) FROM blocks")
            row = cursor.fetchone()
            return row[0] if row and row[0] else 0
        except Exception as e:
            print(f"[DB] Erro ao obter altura: {e}")
            return 0

    def get_all_blocks(self, limit: int = 100, offset: int = 0) -> List[Dict]:
        """Listar blocos com paginação"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM blocks
                ORDER BY height DESC LIMIT ? OFFSET ?
            """, (limit, offset))
            return [self._row_to_block(row) for row in cursor.fetchall()]
        except Exception as e:
            print(f"[DB] Erro ao listar blocos: {e}")
            return []

    def _row_to_block(self, row: sqlite3.Row) -> Dict:
        """Converter linha do banco em dicionário de bloco"""
        return {
            "index": row["height"],
            "hash": row["hash"],
            "previous_hash": row["previous_hash"],
            "merkle_root": row["merkle_root"],
            "timestamp": row["timestamp"],
            "difficulty": row["difficulty"],
            "nonce": row["nonce"],
            "cumulative_work": row["cumulative_work"],
            "transactions": json.loads(row["transactions"])
        }

    # ========================================================
    # OPERAÇÕES COM TRANSAÇÕES
    # ========================================================

    def save_transaction(self, tx: Dict, block_height: int) -> bool:
        """Salvar uma transação"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO transactions
                (txid, block_height, tipo, de, para, valor,
                 taxa, timestamp, assinatura, dados)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                tx.get("txid"),
                block_height,
                tx.get("tipo", "transfer"),
                tx.get("de"),
                tx.get("para"),
                tx.get("valor", 0),
                tx.get("taxa", 0),
                int(tx.get("timestamp", 0)),
                tx.get("assinatura"),
                json.dumps(tx.get("dados", {})) if tx.get("dados") else None
            ))
            return True
        except Exception as e:
            print(f"[DB] Erro ao salvar transação: {e}")
            return False

    def get_transaction(self, txid: str) -> Optional[Dict]:
        """Buscar transação por TXID"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT * FROM transactions WHERE txid = ?", (txid,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            print(f"[DB] Erro ao buscar transação: {e}")
            return None

    def get_tx_block_height(self, txid: str) -> int:
        """
        Descobre em qual altura de bloco uma transação foi incluída
        Retorna -1 se não encontrada
        """
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT block_height FROM transactions 
                WHERE txid = ? LIMIT 1
            """, (txid,))
            row = cursor.fetchone()
            return row[0] if row else -1
        except Exception as e:
            print(f"[DB] Erro ao buscar altura da transação: {e}")
            return -1

    def get_transactions_by_address(self, address: str,
                                     limit: int = 50) -> List[Dict]:
        """Histórico de transações de um endereço"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM transactions
                WHERE de = ? OR para = ?
                ORDER BY timestamp DESC LIMIT ?
            """, (address, address, limit))
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            print(f"[DB] Erro ao buscar transações do endereço: {e}")
            return []

    def get_transactions_by_block(self, block_height: int) -> List[Dict]:
        """Buscar todas as transações de um bloco"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT * FROM transactions WHERE block_height = ?
            """, (block_height,))
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            print(f"[DB] Erro ao buscar transações do bloco: {e}")
            return []

    # ========================================================
    # INFORMAÇÕES E ESTATÍSTICAS
    # ========================================================

    def get_total_transactions(self) -> int:
        """Contar total de transações na rede"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM transactions")
            row = cursor.fetchone()
            return row[0] if row else 0
        except Exception as e:
            print(f"[DB] Erro ao contar transações: {e}")
            return 0

    def get_balance(self, address: str) -> float:
        """Calcular saldo de um endereço"""
        try:
            cursor = self.conn.cursor()
            # Entradas (recebido)
            cursor.execute("""
                SELECT COALESCE(SUM(valor), 0) FROM transactions WHERE para = ?
            """, (address,))
            received = cursor.fetchone()[0]

            # Saídas (enviado + taxas pagas)
            cursor.execute("""
                SELECT COALESCE(SUM(valor + taxa), 0) FROM transactions WHERE de = ?
            """, (address,))
            sent = cursor.fetchone()[0]

            return received - sent
        except Exception as e:
            print(f"[DB] Erro ao calcular saldo: {e}")
            return 0.0

    def chain_validity_check(self) -> Tuple[bool, int]:
        """Verificar integridade da cadeia inteira"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT height, hash, previous_hash FROM blocks ORDER BY height")
            rows = cursor.fetchall()

            if not rows:
                return True, 0  # Cadeia vazia = válida

            prev_hash = rows[0]["hash"]
            for i in range(1, len(rows)):
                if rows[i]["previous_hash"] != prev_hash:
                    return False, rows[i]["height"]
                prev_hash = rows[i]["hash"]

            return True, len(rows) - 1
        except Exception as e:
            print(f"[DB] Erro na verificação de cadeia: {e}")
            return False, -1

    # ========================================================
    # MANUTENÇÃO
    # ========================================================

    def clear_chain(self) -> bool:
        """⚠️ APAGA TODA A CADEIA — Usar com cuidado!"""
        try:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM transactions")
            cursor.execute("DELETE FROM blocks")
            cursor.execute("DELETE FROM block_headers")
            self.conn.commit()
            return True
        except Exception as e:
            print(f"[DB] Erro ao limpar cadeia: {e}")
            return False

    def backup_database(self, backup_path: str) -> bool:
        """Criar cópia de segurança do banco"""
        try:
            if os.path.exists(self.db_path):
                import shutil
                shutil.copy2(self.db_path, backup_path)
                return True
            return False
        except Exception as e:
            print(f"[DB] Erro no backup: {e}")
            return False