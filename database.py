"""База данных SQLite"""
import sqlite3
from datetime import datetime
import threading
import logging

logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path="accounts.db"):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.init_db()

    def init_db(self):
        """Создаёт таблицы если их нет"""
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'"
                )
                if cursor.fetchone():
                    logger.info("[DB] База данных уже существует")
                    return

                logger.info("[DB] Создаю новую базу данных...")
                cursor.execute("""
                    CREATE TABLE accounts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        roblox_login TEXT UNIQUE NOT NULL,
                        roblox_pass TEXT NOT NULL,
                        email TEXT UNIQUE NOT NULL,
                        status TEXT DEFAULT 'available',
                        sold_to TEXT,
                        sold_at TIMESTAMP,
                        funpay_order_id TEXT,
                        funpay_chat_id TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cursor.execute("""
                    CREATE TABLE orders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        funpay_order_id TEXT UNIQUE,
                        buyer_id TEXT NOT NULL,
                        buyer_name TEXT,
                        account_id INTEGER,
                        status TEXT DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        completed_at TIMESTAMP,
                        FOREIGN KEY (account_id) REFERENCES accounts (id)
                    )
                """)
                cursor.execute("""
                    CREATE TABLE messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        funpay_chat_id TEXT,
                        sender_id TEXT,
                        sender_name TEXT,
                        message_text TEXT,
                        sent_at TIMESTAMP,
                        processed BOOLEAN DEFAULT 0
                    )
                """)
                cursor.execute("""
                    CREATE TABLE settings (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )
                """)
                conn.commit()
                logger.info("[DB] ✅ База данных создана успешно!")

    def set_setting(self, key, value):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, value)
                )
                conn.commit()

    def get_setting(self, key):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
                result = cursor.fetchone()
                return result[0] if result else None

    def get_all_settings(self):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT key, value FROM settings")
                return dict(cursor.fetchall())

    def add_account(self, roblox_login, roblox_pass, email):
        with self.lock:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "INSERT INTO accounts (roblox_login, roblox_pass, email) VALUES (?, ?, ?)",
                        (roblox_login, roblox_pass, email)
                    )
                    conn.commit()
                    return True
            except sqlite3.IntegrityError:
                return False

    def get_available_account(self):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, roblox_login, roblox_pass, email, status
                    FROM accounts WHERE status = 'available' LIMIT 1
                """)
                return cursor.fetchone()

    def get_account_by_id(self, account_id):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, roblox_login, roblox_pass, email, status
                    FROM accounts WHERE id = ?
                """, (account_id,))
                return cursor.fetchone()

    def mark_account_sold(self, account_id, buyer_id, funpay_order_id, funpay_chat_id):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    UPDATE accounts
                    SET status = 'sold', sold_to = ?, sold_at = ?,
                        funpay_order_id = ?, funpay_chat_id = ?
                    WHERE id = ?
                """, (buyer_id, datetime.now(), funpay_order_id, funpay_chat_id, account_id))
                conn.commit()

    def mark_account_transferred(self, account_id):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE accounts SET status = 'transferred' WHERE id = ?",
                    (account_id,)
                )
                conn.commit()

    def get_account_by_funpay_chat(self, funpay_chat_id):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, roblox_login, roblox_pass, email, status
                    FROM accounts WHERE funpay_chat_id = ?
                """, (funpay_chat_id,))
                return cursor.fetchone()

    def delete_account(self, account_id):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
                conn.commit()
                return cursor.rowcount > 0

    def get_stats(self):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT status, COUNT(*) FROM accounts GROUP BY status")
                return dict(cursor.fetchall())

    def get_all_accounts(self, status=None, limit=50):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                if status:
                    cursor.execute("""
                        SELECT id, roblox_login, roblox_pass, email, status, sold_to, created_at
                        FROM accounts WHERE status = ? LIMIT ?
                    """, (status, limit))
                else:
                    cursor.execute("""
                        SELECT id, roblox_login, roblox_pass, email, status, sold_to, created_at
                        FROM accounts LIMIT ?
                    """, (limit,))
                return cursor.fetchall()

    def create_order(self, funpay_order_id, buyer_id, buyer_name, account_id):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR IGNORE INTO orders
                    (funpay_order_id, buyer_id, buyer_name, account_id)
                    VALUES (?, ?, ?, ?)
                """, (funpay_order_id, buyer_id, buyer_name, account_id))
                conn.commit()

    def update_order_status(self, funpay_order_id, status):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                if status == 'completed':
                    cursor.execute("""
                        UPDATE orders SET status = ?, completed_at = ?
                        WHERE funpay_order_id = ?
                    """, (status, datetime.now(), funpay_order_id))
                else:
                    cursor.execute(
                        "UPDATE orders SET status = ? WHERE funpay_order_id = ?",
                        (status, funpay_order_id)
                    )
                conn.commit()