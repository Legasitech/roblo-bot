"""
🤖 Roblox Seller Bot — Панель управления через Telegram
(ИСПРАВЛЕННАЯ ВЕРСИЯ v3: FSM + Chat States в SQLite, данные не теряются при перезапуске)
"""
import asyncio
import logging
import time
import re
import json
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey, StateType
from bs4 import BeautifulSoup

from FunPayAPI import Runner
from FunPayAPI.common import enums as fp_enums
from FunPayAPI.common import exceptions as fp_exceptions

from database import Database
from funpay_service import create_account
from email_checker import EmailChecker
import config

# ============================================================================
# ЛОГИРОВАНИЕ
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logging.getLogger("FunPayAPI").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ============================================================================
# SQLITE FSM STORAGE (данные не теряются при перезапуске!)
# ============================================================================
class SQLiteStorage(BaseStorage):
    """Кастомное FSM-хранилище в SQLite вместо MemoryStorage"""
    
    def __init__(self, db_path: str = "fsm_storage.db"):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        import sqlite3
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fsm_data (
                    key TEXT PRIMARY KEY,
                    state TEXT,
                    data TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
    
    def _make_key(self, key: StorageKey) -> str:
        return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.destiny}"
    
    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        import sqlite3
        state_str = state.state if isinstance(state, State) else state
        db_key = self._make_key(key)
        with sqlite3.connect(self.db_path) as conn:
            # Получаем текущие data
            cursor = conn.execute("SELECT data FROM fsm_data WHERE key = ?", (db_key,))
            row = cursor.fetchone()
            data = row[0] if row else "{}"
            
            if state_str is None:
                # Удаляем запись если state очищен
                conn.execute("DELETE FROM fsm_data WHERE key = ?", (db_key,))
            else:
                conn.execute(
                    """INSERT OR REPLACE INTO fsm_data (key, state, data, updated_at) 
                       VALUES (?, ?, ?, ?)""",
                    (db_key, state_str, data, datetime.now())
                )
            conn.commit()
    
    async def get_state(self, key: StorageKey) -> Optional[str]:
        import sqlite3
        db_key = self._make_key(key)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT state FROM fsm_data WHERE key = ?", (db_key,))
            row = cursor.fetchone()
            return row[0] if row else None
    
    async def set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        import sqlite3
        db_key = self._make_key(key)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT state FROM fsm_data WHERE key = ?", (db_key,))
            row = cursor.fetchone()
            state = row[0] if row else None
            
            conn.execute(
                """INSERT OR REPLACE INTO fsm_data (key, state, data, updated_at) 
                   VALUES (?, ?, ?, ?)""",
                (db_key, state, json.dumps(data), datetime.now())
            )
            conn.commit()
    
    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        import sqlite3
        db_key = self._make_key(key)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT data FROM fsm_data WHERE key = ?", (db_key,))
            row = cursor.fetchone()
            if row and row[0]:
                try:
                    return json.loads(row[0])
                except json.JSONDecodeError:
                    return {}
            return {}
    
    async def close(self) -> None:
        pass


# ============================================================================
# DATABASE — добавляем chat_states таблицу
# ============================================================================
class Database:
    def __init__(self, db_path="accounts.db"):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.init_db()

    def init_db(self):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Accounts
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='accounts'"
                )
                if not cursor.fetchone():
                    logger.info("[DB] Создаю таблицу accounts...")
                    cursor.execute("""
                        CREATE TABLE accounts (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            roblox_login TEXT NOT NULL,
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
                
                # Orders
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='orders'"
                )
                if not cursor.fetchone():
                    logger.info("[DB] Создаю таблицу orders...")
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
                
                # Settings
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='settings'"
                )
                if not cursor.fetchone():
                    logger.info("[DB] Создаю таблицу settings...")
                    cursor.execute("""
                        CREATE TABLE settings (
                            key TEXT PRIMARY KEY,
                            value TEXT
                        )
                    """)
                
                # Chat States — НОВАЯ ТАБЛИЦА! Сохраняет стадии диалогов с покупателями
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_states'"
                )
                if not cursor.fetchone():
                    logger.info("[DB] Создаю таблицу chat_states...")
                    cursor.execute("""
                        CREATE TABLE chat_states (
                            chat_id TEXT PRIMARY KEY,
                            stage TEXT DEFAULT 'new',
                            account_id INTEGER,
                            buyer_id TEXT,
                            buyer_name TEXT,
                            order_id TEXT,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                
                # FSM Backup — резервная копия FSM состояний (доп. защита)
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='fsm_backup'"
                )
                if not cursor.fetchone():
                    cursor.execute("""
                        CREATE TABLE fsm_backup (
                            user_id TEXT PRIMARY KEY,
                            state TEXT,
                            data TEXT,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                
                conn.commit()
                logger.info("[DB] База данных готова!")

    # === SETTINGS ===
    def set_setting(self, key, value):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                    (key, value),
                )
                conn.commit()

    def get_setting(self, key):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
                row = cursor.fetchone()
                return row[0] if row else None

    def get_all_settings(self):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT key, value FROM settings")
                return dict(cursor.fetchall())

    # === ACCOUNTS ===
    def add_account(self, roblox_login, roblox_pass, email):
        with self.lock:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        "INSERT INTO accounts (roblox_login, roblox_pass, email) VALUES (?, ?, ?)",
                        (roblox_login, roblox_pass, email),
                    )
                    conn.commit()
                    return True
            except sqlite3.IntegrityError:
                return False

    def get_available_account(self):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, roblox_login, roblox_pass, email, status "
                    "FROM accounts WHERE status = 'available' LIMIT 1"
                )
                return cursor.fetchone()

    def get_account_by_id(self, account_id):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, roblox_login, roblox_pass, email, status "
                    "FROM accounts WHERE id = ?",
                    (account_id,),
                )
                return cursor.fetchone()

    def mark_account_sold(self, account_id, buyer_id, funpay_order_id, funpay_chat_id):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE accounts SET status='sold', sold_to=?, sold_at=?, "
                    "funpay_order_id=?, funpay_chat_id=? WHERE id=?",
                    (buyer_id, datetime.now(), funpay_order_id, funpay_chat_id, account_id),
                )
                conn.commit()

    def mark_account_transferred(self, account_id):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "UPDATE accounts SET status='transferred' WHERE id=?",
                    (account_id,),
                )
                conn.commit()

    def get_account_by_funpay_chat(self, funpay_chat_id):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, roblox_login, roblox_pass, email, status "
                    "FROM accounts WHERE funpay_chat_id = ?",
                    (funpay_chat_id,),
                )
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
                    cursor.execute(
                        "SELECT id, roblox_login, roblox_pass, email, status, sold_to, created_at "
                        "FROM accounts WHERE status = ? LIMIT ?",
                        (status, limit),
                    )
                else:
                    cursor.execute(
                        "SELECT id, roblox_login, roblox_pass, email, status, sold_to, created_at "
                        "FROM accounts LIMIT ?",
                        (limit,),
                    )
                return cursor.fetchall()

    # === ORDERS ===
    def create_order(self, funpay_order_id, buyer_id, buyer_name, account_id):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO orders "
                    "(funpay_order_id, buyer_id, buyer_name, account_id) VALUES (?, ?, ?, ?)",
                    (funpay_order_id, buyer_id, buyer_name, account_id),
                )
                conn.commit()

    # === CHAT STATES — НОВЫЕ МЕТОДЫ! ===
    def get_chat_state(self, chat_id: str) -> Optional[dict]:
        """Получить стадию диалога с покупателем"""
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT stage, account_id, buyer_id, buyer_name, order_id "
                    "FROM chat_states WHERE chat_id = ?",
                    (str(chat_id),),
                )
                row = cursor.fetchone()
                if row:
                    return {
                        "stage": row[0],
                        "account_id": row[1],
                        "buyer_id": row[2],
                        "buyer_name": row[3],
                        "order_id": row[4],
                    }
                return None

    def set_chat_state(self, chat_id: str, stage: str, account_id=None, buyer_id=None, buyer_name=None, order_id=None):
        """Сохранить стадию диалога с покупателем"""
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO chat_states 
                       (chat_id, stage, account_id, buyer_id, buyer_name, order_id, updated_at) 
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (str(chat_id), stage, account_id, buyer_id, buyer_name, order_id, datetime.now()),
                )
                conn.commit()

    def delete_chat_state(self, chat_id: str):
        """Удалить стадию диалога (например, после завершения)"""
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM chat_states WHERE chat_id = ?", (str(chat_id),))
                conn.commit()

    def get_all_chat_states(self) -> Dict[str, dict]:
        """Получить ВСЕ стадии диалогов (для восстановления после перезапуска)"""
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT chat_id, stage, account_id, buyer_id, buyer_name, order_id FROM chat_states"
                )
                result = {}
                for row in cursor.fetchall():
                    result[row[0]] = {
                        "stage": row[1],
                        "account_id": row[2],
                        "buyer_id": row[3],
                        "buyer_name": row[4],
                        "order_id": row[5],
                    }
                return result

    # === FSM BACKUP ===
    def backup_fsm_state(self, user_id: str, state: str, data: dict):
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO fsm_backup (user_id, state, data, updated_at) VALUES (?, ?, ?, ?)",
                    (user_id, state, json.dumps(data), datetime.now()),
                )
                conn.commit()

    def restore_fsm_state(self, user_id: str) -> Optional[tuple]:
        with self.lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT state, data FROM fsm_backup WHERE user_id = ?", (user_id,))
                row = cursor.fetchone()
                if row:
                    try:
                        return row[0], json.loads(row[1])
                    except json.JSONDecodeError:
                        return row[0], {}
                return None


# ============================================================================
# BOT INIT
# ============================================================================
bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=SQLiteStorage(db_path="fsm_storage.db"))  # SQLite вместо Memory!
db = Database()

# Глобальное состояние
funpay_account = None
email_checker = None
last_processed_msg_ids = {}
processed_messages = {}
pending_tasks = {}

DEBOUNCE_SECONDS = 1.5
MAX_PROCESSED_IDS = 200
NOTIFY_COOLDOWN = 30
_last_notify_time = {}

# ============================================================================
# FSM STATES
# ============================================================================
class SetupStates(StatesGroup):
    funpay_key = State()
    gmail_email = State()
    gmail_app_password = State()

class AddAccountStates(StatesGroup):
    login = State()
    password = State()
    confirm = State()

class DeleteAccountStates(StatesGroup):
    account_id = State()
    confirm_delete = State()

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================
def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS

def main_menu():
    kb = [
        [KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="➕ Добавить аккаунт"), KeyboardButton(text="📋 Все аккаунты")],
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="🔍 Найти аккаунт")],
        [KeyboardButton(text="🗑️ Удалить аккаунт")],
        [KeyboardButton(text="▶️ Запустить FunPay"), KeyboardButton(text="⏹ Остановить FunPay")],
        [KeyboardButton(text="📧 Проверить почту"), KeyboardButton(text="🧪 Тест подключений")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def settings_menu():
    kb = [
        [KeyboardButton(text="🔑 Настроить FunPay")],
        [KeyboardButton(text="📧 Настроить Gmail")],
        [KeyboardButton(text="◀️ Назад")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def back_menu():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="◀️ Назад")]], resize_keyboard=True)

def confirm_delete_menu():
    kb = [
        [KeyboardButton(text="✅ Да, удалить"), KeyboardButton(text="❌ Отмена")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def init_services():
    global funpay_account, email_checker
    settings = db.get_all_settings()

    if settings.get('funpay_key') and funpay_account is None:
        try:
            funpay_account = create_account(settings['funpay_key'])
            logger.info(f"[INIT] ✅ FunPay: {funpay_account.username}")
        except Exception as e:
            logger.error(f"[INIT] ❌ FunPay init error: {e}")
            funpay_account = None

    if settings.get('gmail_email') and settings.get('gmail_app_password') and email_checker is None:
        try:
            email_checker = EmailChecker(settings['gmail_email'], settings['gmail_app_password'])
            logger.info("[INIT] ✅ Email checker инициализирован")
        except Exception as e:
            logger.error(f"[INIT] ❌ Email checker error: {e}")
            email_checker = None

async def safe_notify_admin(admin_chat_id: int, text: str, parse_mode: str = "HTML"):
    now = time.time()
    key = f"{admin_chat_id}:{hash(text[:50])}"
    if now - _last_notify_time.get(key, 0) < NOTIFY_COOLDOWN:
        return
    _last_notify_time[key] = now
    try:
        await bot.send_message(admin_chat_id, text, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"[NOTIFY] ❌ {e}")

# ============================================================================
# ПРОВЕРКА ОПЛАТЫ
# ============================================================================
async def check_order_payment(fp, chat_id, buyer_id):
    try:
        response = fp.method(
            request_method='get',
            api_method='/orders',
            headers={'accept': 'application/json'},
            payload={'buyer_id': buyer_id, 'status': 'paid'}
        )

        if response.status_code == 200:
            data = response.json()
            orders = data.get('orders', [])
            for order in orders:
                if str(order.get('chat_id')) == str(chat_id):
                    return True, {
                        'id': order.get('id', 'unknown'),
                        'status': order.get('status', 'paid')
                    }

        response = fp.method(
            request_method='get',
            api_method='/transactions/sales',
            headers={'accept': 'text/html,application/xhtml+xml'},
            payload={}
        )

        if response.status_code != 200:
            return False, None

        soup = BeautifulSoup(response.text, 'html.parser')
        orders_table = soup.find('table', {'class': 'dataTable'})
        if not orders_table:
            return False, None

        for row in orders_table.find_all('tr')[1:]:
            cells = row.find_all('td')
            if len(cells) < 3:
                continue
            row_text = row.get_text()
            if str(buyer_id) in row_text or str(chat_id) in row_text:
                status_cell = row.find('td', {'class': 'status'})
                if status_cell:
                    status_text = status_cell.get_text(strip=True).lower()
                    if any(s in status_text for s in ['оплачен', 'paid', 'подтверждён', 'confirmed']):
                        order_link = row.find('a', href=re.compile(r'/orders/'))
                        order_id = order_link['href'].split('/')[-1] if order_link else 'unknown'
                        return True, {'id': order_id, 'status': status_text}
                    else:
                        return False, {'status': status_text}

        return False, None

    except Exception as e:
        logger.error(f"[CHECK] ❌ Ошибка проверки оплаты: {e}")
        return False, None

# ============================================================================
# TELEGRAM COMMAND HANDLERS
# ============================================================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id

    if not is_admin(user_id):
        await message.answer("❌ У тебя нет доступа к боту.")
        return

    # Восстанавливаем FSM если было сохранено
    current_state = await state.get_state()
    if current_state:
        await state.clear()
        await message.answer("♻️ Предыдущая сессия восстановлена и очищена.")

    init_services()
    settings = db.get_all_settings()

    status_lines = []
    status_lines.append("✅ FunPay настроен" if settings.get('funpay_key') else "❌ FunPay НЕ настроен")
    status_lines.append("✅ Gmail настроен" if settings.get('gmail_email') else "❌ Gmail НЕ настроен")

    # Показываем сколько активных диалогов
    chat_states_count = len(db.get_all_chat_states())
    if chat_states_count > 0:
        status_lines.append(f"💬 Активных диалогов: {chat_states_count}")

    await message.answer(
        f"🎮 <b>Roblox Seller Bot</b>\n\n"
        f"{'\n'.join(status_lines)}\n\n"
        f"Выбери действие:",
        reply_markup=main_menu(),
        parse_mode="HTML"
    )

@dp.message(F.text == "⚙️ Настройки")
async def settings_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("⚙️ Настройки бота:", reply_markup=settings_menu())

@dp.message(F.text == "◀️ Назад")
async def back_cmd(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    current_state = await state.get_state()
    if current_state:
        await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu())

@dp.message(F.text == "🔑 Настроить FunPay")
async def setup_funpay(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🔑 Введи <b>Golden Key</b> из Cookie-Editor:\n\n"
        "1️⃣ Установи расширение Cookie-Editor в Chrome\n"
        "2️⃣ Зайди на funpay.com (будь авторизован!)\n"
        "3️⃣ Открой Cookie-Editor → найди golden_key\n"
        "4️⃣ Скопируй значение и отправь сюда",
        reply_markup=back_menu(),
        parse_mode="HTML"
    )
    await state.set_state(SetupStates.funpay_key)

@dp.message(SetupStates.funpay_key)
async def process_funpay_key(message: types.Message, state: FSMContext):
    if message.text == "◀️ Назад":
        await state.clear()
        await message.answer("Отменено.", reply_markup=settings_menu())
        return

    golden_key = message.text.strip()
    db.set_setting('funpay_key', golden_key)

    global funpay_account
    try:
        funpay_account = create_account(golden_key)
        await message.answer(
            f"✅ <b>FunPay настроен!</b>\n\n"
            f"👤 Аккаунт: {funpay_account.username}\n"
            f"🆔 ID: {funpay_account.id}",
            reply_markup=settings_menu(),
            parse_mode="HTML"
        )
        logger.info(f"[SETUP] ✅ FunPay: {funpay_account.username}")
    except fp_exceptions.UnauthorizedError:
        await message.answer(
            "⚠️ Ключ сохранён, но авторизация не прошла.\n"
            "Проверь Golden Key (не выходи с FunPay в браузере!)",
            reply_markup=settings_menu()
        )
    except Exception as e:
        await message.answer(
            f"⚠️ Ключ сохранён, но подключение не удалось:\n{e}",
            reply_markup=settings_menu()
        )
        logger.error(f"[SETUP] ❌ FunPay error: {e}")

    await state.clear()

@dp.message(F.text == "📧 Настроить Gmail")
async def setup_gmail(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "📧 Введи свою <b>Gmail почту</b> (основную):\n\n"
        "Пример: lolzxcded@gmail.com",
        reply_markup=back_menu(),
        parse_mode="HTML"
    )
    await state.set_state(SetupStates.gmail_email)

@dp.message(SetupStates.gmail_email)
async def process_gmail_email(message: types.Message, state: FSMContext):
    if message.text == "◀️ Назад":
        await state.clear()
        await message.answer("Отменено.", reply_markup=settings_menu())
        return

    email = message.text.strip()
    if "@gmail.com" not in email:
        await message.answer("❌ Это не Gmail! Введи почту с @gmail.com")
        return

    await state.update_data(gmail_email=email)
    await message.answer(
        "✅ Почта сохранена!\n\n"
        "Теперь введи <b>App Password</b> (16-значный код):\n\n"
        "📋 Как получить:\n"
        "1️⃣ myaccount.google.com → Безопасность\n"
        "2️⃣ Включи Двухфакторную аутентификацию\n"
        "3️⃣ Поиск: 'Пароли приложений'\n"
        "4️⃣ Создай для 'Почта' → назови 'Roblox Bot'\n"
        "5️⃣ Скопируй 16-значный код"
    )
    await state.set_state(SetupStates.gmail_app_password)

@dp.message(SetupStates.gmail_app_password)
async def process_gmail_password(message: types.Message, state: FSMContext):
    if message.text == "◀️ Назад":
        await state.clear()
        await message.answer("Отменено.", reply_markup=settings_menu())
        return

    data = await state.get_data()
    email = data['gmail_email']
    password = message.text.strip().replace(" ", "")

    db.set_setting('gmail_email', email)
    db.set_setting('gmail_app_password', password)

    global email_checker
    try:
        email_checker = EmailChecker(email, password)
        ok, total = await asyncio.to_thread(email_checker.test_connection)

        if ok:
            base = email.split('@')[0]
            db.set_setting('gmail_base', base)
            await message.answer(
                f"✅ <b>Gmail настроен!</b>\n\n"
                f"📧 Почта: {email}\n"
                f"📨 Писем во входящих: {total}\n\n"
                f"💡 Формат для аккаунтов:\n"
                f"<code>{base}+roblox_vc_1@gmail.com</code>\n"
                f"<code>{base}+roblox_vc_2@gmail.com</code>\n"
                f"и т.д.",
                reply_markup=settings_menu(),
                parse_mode="HTML"
            )
            logger.info(f"[SETUP] ✅ Gmail: {email}")
        else:
            await message.answer(
                "⚠️ Данные сохранены, но IMAP не работает.\n"
                "Проверь App Password и что IMAP включён в Gmail.",
                reply_markup=settings_menu()
            )
    except Exception as e:
        await message.answer(
            f"⚠️ Ошибка при настройке Gmail: {e}",
            reply_markup=settings_menu()
        )
        logger.error(f"[SETUP] ❌ Gmail error: {e}")

    await state.clear()

@dp.message(F.text == "➕ Добавить аккаунт")
async def add_account_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    settings = db.get_all_settings()
    if not settings.get('gmail_email'):
        await message.answer("❌ Сначала настрой Gmail в ⚙️ Настройки!")
        return

    await message.answer(
        "➕ <b>Добавление аккаунта Roblox</b>\n\n"
        "Введи <b>логин</b> (ник в Roblox):",
        reply_markup=back_menu(),
        parse_mode="HTML"
    )
    await state.set_state(AddAccountStates.login)

@dp.message(AddAccountStates.login)
async def process_login(message: types.Message, state: FSMContext):
    if message.text == "◀️ Назад":
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_menu())
        return
    await state.update_data(login=message.text.strip())
    await message.answer("Введи <b>пароль</b> от аккаунта:", parse_mode="HTML")
    await state.set_state(AddAccountStates.password)

@dp.message(AddAccountStates.password)
async def process_password(message: types.Message, state: FSMContext):
    if message.text == "◀️ Назад":
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_menu())
        return

    data = await state.get_data()
    login = data['login']
    password = message.text.strip()

    settings = db.get_all_settings()
    base = settings.get('gmail_base', settings['gmail_email'].split('@')[0])
    stats = db.get_stats()
    total = sum(stats.values()) if stats else 0
    email = f"{base}+roblox_vc_{total + 1}@gmail.com"

    await state.update_data(password=password, email=email)
    await message.answer(
        f"📋 <b>Проверь данные:</b>\n\n"
        f"👤 Логин: <code>{login}</code>\n"
        f"🔑 Пароль: <code>{password}</code>\n"
        f"📧 Email: <code>{email}</code>\n\n"
        f"⚠️ Привяжи эту почту к аккаунту на Roblox, потом нажми <b>✅ Да, добавить</b>",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="✅ Да, добавить")],
                [KeyboardButton(text="❌ Нет, отмена")]
            ],
            resize_keyboard=True
        ),
        parse_mode="HTML"
    )
    await state.set_state(AddAccountStates.confirm)

@dp.message(AddAccountStates.confirm)
async def process_confirm(message: types.Message, state: FSMContext):
    if message.text == "❌ Нет, отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_menu())
        return

    data = await state.get_data()
    login = data['login']
    password = data['password']
    email = data['email']

    success = db.add_account(login, password, email)
    if success:
        await message.answer(
            f"✅ <b>Аккаунт добавлен!</b>\n\n"
            f"👤 {login}\n"
            f"📧 {email}",
            reply_markup=main_menu(),
            parse_mode="HTML"
        )
        logger.info(f"[ACCOUNT] ✅ Добавлен: {login}")
    else:
        await message.answer(
            f"❌ Аккаунт <code>{login}</code> уже существует!",
            reply_markup=main_menu(),
            parse_mode="HTML"
        )

    await state.clear()

@dp.message(F.text == "🗑️ Удалить аккаунт")
async def delete_account_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🗑️ <b>Удаление аккаунта</b>\n\n"
        "Введи <b>ID аккаунта</b> для удаления:\n"
        "Используй команду 📋 Все аккаунты чтобы посмотреть ID",
        reply_markup=back_menu(),
        parse_mode="HTML"
    )
    await state.set_state(DeleteAccountStates.account_id)

@dp.message(DeleteAccountStates.account_id)
async def process_delete_account_id(message: types.Message, state: FSMContext):
    if message.text == "◀️ Назад":
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_menu())
        return

    try:
        account_id = int(message.text.strip())
        account = db.get_account_by_id(account_id)

        if not account:
            await message.answer(
                f"❌ Аккаунт #{account_id} не найден!\n"
                f"Попробуй ещё раз или нажми ◀️ Назад",
                reply_markup=back_menu()
            )
            return

        id_, login, password, email, status = account

        await state.update_data(account_id=account_id, login=login, email=email)

        await message.answer(
            f"⚠️ <b>Подтверждение удаления</b>\n\n"
            f"📋 Аккаунт #{id_}:\n"
            f"👤 Логин: <code>{login}</code>\n"
            f"📧 Email: <code>{email}</code>\n"
            f"📍 Статус: {status}\n\n"
            f"⚠️ Это действие <b>НЕОБРАТИМО</b>!\n"
            f"Нажми <b>✅ Да, удалить</b> для подтверждения",
            reply_markup=confirm_delete_menu(),
            parse_mode="HTML"
        )
        await state.set_state(DeleteAccountStates.confirm_delete)

    except ValueError:
        await message.answer(
            "❌ Введи корректный ID (число)!\n"
            "Попробуй ещё раз или нажми ◀️ Назад",
            reply_markup=back_menu()
        )

@dp.message(DeleteAccountStates.confirm_delete)
async def process_delete_confirm(message: types.Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("❌ Удаление отменено.", reply_markup=main_menu())
        return

    if message.text != "✅ Да, удалить":
        await message.answer("⚠️ Нажми одну из кнопок ниже")
        return

    data = await state.get_data()
    account_id = data['account_id']
    login = data['login']

    success = db.delete_account(account_id)

    if success:
        await message.answer(
            f"✅ <b>Аккаунт #{account_id} удалён!</b>\n\n"
            f"👤 {login}\n"
            f"Аккаунт безвозвратно удалён из базы.",
            reply_markup=main_menu(),
            parse_mode="HTML"
        )
        logger.info(f"[DELETE] ✅ Удалён аккаунт #{account_id} ({login})")
    else:
        await message.answer(
            f"❌ Не удалось удалить аккаунт #{account_id}.\n"
            f"Возможно, он уже был удалён.",
            reply_markup=main_menu()
        )
        logger.error(f"[DELETE] ❌ Не удалось удалить аккаунт #{account_id}")

    await state.clear()

@dp.message(F.text == "📊 Статистика")
async def stats_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    stats = db.get_stats()
    text = "📊 <b>Статистика аккаунтов</b>\n\n"
    for status, count in stats.items():
        emoji = {"available": "🟢", "sold": "🟡", "transferred": "🔵"}.get(status, "⚪")
        text += f"{emoji} {status}: <b>{count}</b>\n"
    total = sum(stats.values()) if stats else 0
    text += f"\n📦 Всего: <b>{total}</b>"
    
    # Добавляем инфу о диалогах
    chat_states = db.get_all_chat_states()
    if chat_states:
        new_count = sum(1 for s in chat_states.values() if s['stage'] == 'new')
        delivered_count = sum(1 for s in chat_states.values() if s['stage'] == 'delivered')
        text += f"\n\n💬 Диалоги: {new_count} новых, {delivered_count} ожидают код"
    
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "📋 Все аккаунты")
async def all_accounts_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    accounts = db.get_all_accounts(limit=20)
    if not accounts:
        await message.answer("📭 Аккаунтов пока нет.")
        return

    text = "📋 <b>Последние аккаунты:</b>\n\n"
    for acc in accounts:
        id_, login, password, email, status = acc[:5]
        emoji = {"available": "🟢", "sold": "🟡", "transferred": "🔵"}.get(status, "⚪")
        text += f"{emoji} <b>#{id_}</b> | <code>{login}</code>\n"
        text += f"   📧 {email}\n"
        text += f"   📍 {status}\n\n"
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "🔍 Найти аккаунт")
async def find_account_cmd(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await message.answer(
        "🔍 Введи <b>ID</b> аккаунта для поиска:",
        reply_markup=back_menu(),
        parse_mode="HTML"
    )
    await state.set_state("find_account_wait_id")

@dp.message(lambda msg: msg.text and msg.text.isdigit())
async def find_by_id(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    current_state = await state.get_state()
    if current_state != "find_account_wait_id":
        return

    try:
        acc_id = int(message.text.strip())
        acc = db.get_account_by_id(acc_id)
        if acc:
            id_, login, password, email, status = acc[:5]
            emoji = {"available": "🟢", "sold": "🟡", "transferred": "🔵"}.get(status, "⚪")
            await message.answer(
                f"{emoji} <b>Аккаунт #{id_}</b>\n\n"
                f"👤 Логин: <code>{login}</code>\n"
                f"🔑 Пароль: <code>{password}</code>\n"
                f"📧 Email: <code>{email}</code>\n"
                f"📍 Статус: {status}",
                reply_markup=main_menu(),
                parse_mode="HTML"
            )
        else:
            await message.answer("❌ Аккаунт не найден.", reply_markup=main_menu())
    except Exception as e:
        logger.error(f"[FIND] Ошибка: {e}")
    finally:
        await state.clear()

@dp.message(F.text == "🧪 Тест подключений")
async def test_connections(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    global funpay_account
    settings = db.get_all_settings()
    results = []

    if settings.get('gmail_email') and settings.get('gmail_app_password'):
        try:
            checker = EmailChecker(settings['gmail_email'], settings['gmail_app_password'])
            ok, total = await asyncio.to_thread(checker.test_connection)
            if ok:
                results.append(f"✅ Gmail: OK ({total} писем)")
            else:
                results.append("❌ Gmail: Ошибка подключения")
        except Exception as e:
            results.append(f"❌ Gmail: {e}")
    else:
        results.append("⚠️ Gmail: Не настроен")

    if settings.get('funpay_key'):
        try:
            if funpay_account is None:
                funpay_account = await asyncio.to_thread(create_account, settings['funpay_key'])
            else:
                await asyncio.to_thread(funpay_account.get)
            results.append(f"✅ FunPay: OK ({funpay_account.username}, ID {funpay_account.id})")
        except Exception as e:
            results.append(f"❌ FunPay: {e}")
            logger.error(f"[TEST] ❌ FunPay error: {e}")
    else:
        results.append("⚠️ FunPay: Не настроен")

    # Проверяем FSM storage
    try:
        states_count = len(db.get_all_chat_states())
        results.append(f"💬 Сохранено диалогов: {states_count}")
    except Exception as e:
        results.append(f"⚠️ Chat states: {e}")

    await message.answer("🧪 <b>Результаты тестов:</b>\n\n" + "\n".join(results), parse_mode="HTML")

@dp.message(F.text == "📧 Проверить почту")
async def check_email_cmd(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    settings = db.get_all_settings()
    if not settings.get('gmail_email'):
        await message.answer("❌ Сначала настрой Gmail!")
        return

    try:
        checker = EmailChecker(settings['gmail_email'], settings['gmail_app_password'])
        ok, total = await asyncio.to_thread(checker.test_connection)
        if ok:
            await message.answer(f"📧 Всего писем: {total}\n\n🔍 Ищу письма от Roblox...")
            roblox_count = await asyncio.to_thread(_count_roblox_emails, checker)
            await message.answer(f"📨 Писем от Roblox: {roblox_count}")
        else:
            await message.answer("❌ Не удалось подключиться к Gmail.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        logger.error(f"[EMAIL] Error: {e}")

def _count_roblox_emails(checker):
    if not checker.connect():
        return 0
    try:
        checker.imap.select("inbox")
        _, msgs = checker.imap.search(None, 'FROM "noreply@roblox.com"')
        return len(msgs[0].split()) if msgs[0] else 0
    finally:
        checker.disconnect()

# ============================================================================
# FUNPAY BOT RUNNER
# ============================================================================
funpay_task = None
_funpay_runner_stop_event = asyncio.Event()

@dp.message(F.text == "▶️ Запустить FunPay")
async def start_funpay_bot(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    global funpay_task, funpay_account
    settings = db.get_all_settings()

    if not settings.get('funpay_key'):
        await message.answer("❌ Сначала настрой FunPay в ⚙️ Настройки!")
        return

    if funpay_account is None:
        try:
            funpay_account = await asyncio.to_thread(create_account, settings['funpay_key'])
        except Exception as e:
            await message.answer(f"❌ Не удалось подключиться к FunPay: {e}")
            return

    if funpay_task and not funpay_task.done():
        await message.answer("⚠️ FunPay бот уже запущен!")
        return

    _funpay_runner_stop_event.clear()
    funpay_task = asyncio.create_task(funpay_bot_loop(message.chat.id))
    await message.answer("▶️ <b>FunPay бот запущен!</b>\nСлежу за сообщениями...", parse_mode="HTML")
    logger.info("[RUNNER] ✅ FunPay бот запущен")

@dp.message(F.text == "⏹ Остановить FunPay")
async def stop_funpay_bot(message: types.Message):
    global funpay_task, funpay_account, pending_tasks

    if funpay_task and not funpay_task.done():
        _funpay_runner_stop_event.set()
        funpay_task.cancel()

        for chat_id, task in list(pending_tasks.items()):
            if not task.done():
                task.cancel()
        pending_tasks.clear()

        try:
            await asyncio.wait_for(funpay_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

        funpay_task = None
        if funpay_account:
            funpay_account.runner = None

        await message.answer("⏹ <b>FunPay бот остановлен.</b>", parse_mode="HTML")
        logger.info("[RUNNER] ✅ FunPay бот остановлен")
    else:
        await message.answer("⚠️ FunPay бот не был запущен.")

# ============================================================================
# FUNPAY EVENT LOOP
# ============================================================================
async def funpay_bot_loop(admin_chat_id):
    global funpay_account
    settings = db.get_all_settings()

    if funpay_account is None:
        funpay_account = await asyncio.to_thread(create_account, settings['funpay_key'])
        logger.info(f"[RUNNER] ✅ Аккаунт: {funpay_account.username}")

    fp = funpay_account
    
    # ВОССТАНАВЛИВАЕМ chat_states из БД при перезапуске!
    chat_states = db.get_all_chat_states()
    logger.info(f"[RUNNER] ♻️ Восстановлено {len(chat_states)} диалогов из БД")
    
    # Отправляем админу инфу о восстановлении
    if chat_states:
        await safe_notify_admin(
            admin_chat_id,
            f"♻️ <b>Бот перезапущен!</b>\n"
            f"💬 Восстановлено диалогов: {len(chat_states)}\n"
            f"Бот продолжит работу с сохранёнными состояниями."
        )

    loop = asyncio.get_running_loop()
    fail_count_ref = [0]

    def make_runner():
        fp.runner = None
        return Runner(fp)

    async def debounced_handle(msg):
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return

        chat_id = msg.chat_id
        msg_id = getattr(msg, 'id', 0)

        if chat_id not in processed_messages:
            processed_messages[chat_id] = deque(maxlen=MAX_PROCESSED_IDS)

        if msg_id in processed_messages[chat_id]:
            return
        if msg_id <= last_processed_msg_ids.get(chat_id, 0):
            return

        last_processed_msg_ids[chat_id] = msg_id
        processed_messages[chat_id].append(msg_id)

        logger.info(f"[RUNNER] ✅ Новое | Чат: {chat_id} | От: {msg.author} | Текст: {(msg.text or '')[:50]}")

        try:
            await handle_funpay_message(fp, ec, msg, chat_states, admin_chat_id)
        except Exception as e:
            logger.error(f"[PROCESS ERROR] ❌ {e}", exc_info=True)
        finally:
            pending_tasks.pop(chat_id, None)

    async def process_event(event):
        try:
            if event.type != fp_enums.EventTypes.NEW_MESSAGE:
                return

            msg = event.message
            if msg is None:
                logger.warning("[PROCESS] ⚠️ event.message = None")
                return

            if msg.author_id == fp.id:
                return

            chat_id = msg.chat_id

            old_task = pending_tasks.pop(chat_id, None)
            if old_task and not old_task.done():
                old_task.cancel()

            pending_tasks[chat_id] = asyncio.create_task(debounced_handle(msg))

        except Exception as e:
            logger.error(f"[PROCESS ERROR] ❌ {e}", exc_info=True)

    ec = email_checker or EmailChecker(settings['gmail_email'], settings['gmail_app_password'])

    runner = make_runner()

    def listen_events(runner_obj, fail_count_ref):
        while not _funpay_runner_stop_event.is_set():
            try:
                for event in runner_obj.listen(requests_delay=5, ignore_exceptions=False):
                    if _funpay_runner_stop_event.is_set():
                        break
                    asyncio.run_coroutine_threadsafe(process_event(event), loop)
                fail_count_ref[0] = 0
            except fp_exceptions.RequestFailedError as e:
                fail_count_ref[0] += 1
                resp = e.response
                body_text = resp.text if resp is not None else "<нет тела>"
                status = resp.status_code if resp is not None else "?"

                logger.warning(f"[RUNNER {status}] ⚠️ Попытка #{fail_count_ref[0]}: {body_text[:800]}")

                asyncio.run_coroutine_threadsafe(
                    safe_notify_admin(admin_chat_id, f"⚠️ Runner ошибка {status}\n<code>{body_text[:600]}</code>"),
                    loop
                )

                if fail_count_ref[0] >= 3:
                    logger.info("[RUNNER] 🔄 Обновляю сессию...")
                    asyncio.run_coroutine_threadsafe(
                        safe_notify_admin(admin_chat_id, "🔄 Обновляю сессию FunPay..."),
                        loop
                    )
                    time.sleep(5)
                    try:
                        fp.get(update_phpsessid=True)
                        runner_obj = make_runner()
                        asyncio.run_coroutine_threadsafe(
                            safe_notify_admin(admin_chat_id, f"✅ Сессия обновлена ({fp.username})"),
                            loop
                        )
                        fail_count_ref[0] = 0
                    except Exception as reconnect_err:
                        logger.error(f"[RUNNER] ❌ Reconnect error: {reconnect_err}")
                        asyncio.run_coroutine_threadsafe(
                            safe_notify_admin(admin_chat_id, f"❌ Ошибка обновления: {reconnect_err}"),
                            loop
                        )
                        time.sleep(30)
                else:
                    time.sleep(10)
            except Exception as e:
                fail_count_ref[0] += 1
                logger.error(f"[RUNNER ERROR] ❌ #{fail_count_ref[0]}: {e}", exc_info=True)
                if fail_count_ref[0] >= 3:
                    time.sleep(5)
                    try:
                        fp.get(update_phpsessid=True)
                        runner_obj = make_runner()
                        fail_count_ref[0] = 0
                    except Exception as e2:
                        logger.error(f"[RUNNER] ❌ Reconnect failed: {e2}")
                        time.sleep(30)
                else:
                    time.sleep(10)

    listener_task = loop.run_in_executor(None, listen_events, runner, fail_count_ref)

    try:
        await listener_task
    except asyncio.CancelledError:
        logger.info("[RUNNER] ✅ Listener отменён")
        raise

# ============================================================================
# FUNPAY MESSAGE HANDLERS
# ============================================================================
async def handle_funpay_message(fp, ec, msg, chat_states, admin_chat_id):
    try:
        if msg is None:
            return

        chat_id = msg.chat_id
        text = (msg.text or "").lower().strip()
        sender_id = msg.author_id
        sender_name = msg.author or "?"

        logger.info(f"[HANDLE] 📨 Чат={chat_id}, от={sender_name}, текст={text[:30]}")

        await safe_notify_admin(
            admin_chat_id,
            f"💬 <b>Новое сообщение от {sender_name}</b>\n"
            f"💬 Чат: {chat_id}\n"
            f"📝 Текст: {(msg.text or '')[:100]}"
        )

        # Загружаем state из БД (а не только из памяти!)
        state = chat_states.get(str(chat_id))
        if state is None:
            state = db.get_chat_state(str(chat_id))
            if state:
                chat_states[str(chat_id)] = state
                logger.info(f"[HANDLE] ♻️ Восстановлен state для чата {chat_id}: {state['stage']}")
        
        if state is None:
            state = {"stage": "new", "account_id": None}
            chat_states[str(chat_id)] = state
            db.set_chat_state(str(chat_id), "new")

        try:
            if state["stage"] == "new":
                account = db.get_account_by_funpay_chat(chat_id)

                if account:
                    # Восстанавливаем delivered state
                    new_state = {
                        "stage": "delivered", 
                        "account_id": account[0],
                        "buyer_id": str(sender_id),
                        "buyer_name": sender_name
                    }
                    chat_states[str(chat_id)] = new_state
                    db.set_chat_state(
                        str(chat_id), "delivered", 
                        account_id=account[0],
                        buyer_id=str(sender_id),
                        buyer_name=sender_name
                    )
                    logger.info(f"[HANDLE] ✅ Найден существующий аккаунт #{account[0]}")

                    acc_id, login, pwd, email, status = account
                    reminder = (
                        f"👋 Привет, {sender_name}!\n\n"
                        f"Данные твоего аккаунта уже были отправлены ранее:\n"
                        f"👤 Логин: `{login}`\n"
                        f"📧 Почта: `{email}`\n\n"
                        f"💡 Если тебе нужен код для смены почты, просто напиши слово **КОД**.\n"
                        f"Если ты не оплачивал этот заказ, пожалуйста, оплати его, и я выдам новый аккаунт."
                    )
                    await async_send_fp_message(fp, chat_id, reminder, admin_chat_id)
                else:
                    logger.info(f"[HANDLE] 🔍 Проверяю оплату для чата {chat_id}...")
                    is_paid, order_info = await check_order_payment(fp, chat_id, sender_id)

                    if is_paid:
                        logger.info(f"[HANDLE] 💰 Оплата подтверждена! Выдаю аккаунт...")
                        await deliver_account(fp, chat_id, sender_id, sender_name, chat_states, admin_chat_id, order_info)
                    else:
                        logger.info(f"[HANDLE] ⏳ Оплата не найдена. Отправляю напоминание.")
                        await send_payment_reminder(fp, chat_id, sender_name, order_info, admin_chat_id)

            elif state["stage"] == "delivered":
                if any(w in text for w in ["код", "code", "пришли", "дай", "отправь"]):
                    await send_verification_code(fp, ec, chat_id, state["account_id"], admin_chat_id, chat_states)
                elif any(w in text for w in ["сменил", "поменял", "готово", "done"]):
                    db.mark_account_transferred(state["account_id"])
                    
                    # Обновляем state в БД
                    db.set_chat_state(str(chat_id), "completed", account_id=state["account_id"])
                    chat_states[str(chat_id)] = {"stage": "completed", "account_id": state["account_id"]}
                    
                    await async_send_fp_message(
                        fp, chat_id,
                        "🎉 Отлично! Аккаунт перевязан на твою почту!\n\n"
                        "✅ Теперь аккаунт полностью твой. Спасибо за покупку! 🚀",
                        admin_chat_id
                    )
                    await safe_notify_admin(admin_chat_id, f"✅ Аккаунт #{state['account_id']} перевязан!")

            elif state["stage"] == "code_sent":
                if any(w in text for w in ["сменил", "поменял", "готово", "done"]):
                    db.mark_account_transferred(state["account_id"])
                    db.set_chat_state(str(chat_id), "completed", account_id=state["account_id"])
                    chat_states[str(chat_id)] = {"stage": "completed", "account_id": state["account_id"]}
                    
                    await async_send_fp_message(
                        fp, chat_id,
                        "🎉 Отлично! Аккаунт перевязан на твою почту!\n\n"
                        "✅ Теперь аккаунт полностью твой. Спасибо за покупку! 🚀",
                        admin_chat_id
                    )
                    await safe_notify_admin(admin_chat_id, f"✅ Аккаунт #{state['account_id']} перевязан!")

        except Exception as e:
            logger.error(f"[HANDLE] ❌ State error: {e}", exc_info=True)
            await async_send_fp_message(fp, chat_id, "⚠️ Произошла ошибка, повтори попытку позже", admin_chat_id)

    except Exception as e:
        logger.error(f"[HANDLE] ❌ КРИТИЧЕСКАЯ ОШИБКА: {e}", exc_info=True)


async def send_payment_reminder(fp, chat_id, buyer_name, order_info, admin_chat_id):
    if order_info and order_info.get('status'):
        status = order_info['status']
        reminder_text = (
            f"👋 Привет, {buyer_name}!\n\n"
            f"⏳ Я вижу, что заказ ещё не оплачен.\n"
            f"📊 Текущий статус: <b>{status}</b>\n\n"
            f"💡 Как только оплатите — я автоматически отправлю данные аккаунта.\n"
            f"Просто напишите что-нибудь после оплаты (например, \"Оплатил\").\n\n"
            f"❓ Если есть вопросы — пишите!"
        )
    else:
        reminder_text = (
            f"👋 Привет, {buyer_name}!\n\n"
            f"⏳ Я не вижу оплаченного заказа.\n\n"
            f"💡 Пожалуйста:\n"
            f"1️⃣ Оплатите заказ на FunPay\n"
            f"2️⃣ Напишите мне что-нибудь после оплаты (например, \"Оплатил\")\n"
            f"3️⃣ Я автоматически проверю оплату и отправлю аккаунт\n\n"
            f"❓ Если есть вопросы — пишите!"
        )

    success = await async_send_fp_message(fp, chat_id, reminder_text, admin_chat_id)
    if success:
        logger.info(f"[REMINDER] 📩 Отправлено напоминание {buyer_name}")


async def deliver_account(fp, chat_id, buyer_id, buyer_name, chat_states, admin_chat_id, order_info=None):
    try:
        account = db.get_available_account()
        if not account:
            await async_send_fp_message(
                fp, chat_id,
                "😔 Извини, сейчас нет свободных аккаунтов. Напиши позже или оформи возврат.",
                admin_chat_id
            )
            logger.warning(f"[DELIVER] ⚠️ Нет свободных аккаунтов")
            return

        account_id, roblox_login, roblox_pass, email, status = account
        order_id = order_info.get('id', 'unknown') if order_info else 'unknown'

        db.mark_account_sold(account_id, buyer_id, order_id, chat_id)
        db.create_order(order_id, buyer_id, buyer_name, account_id)

        # Сохраняем state в БД
        db.set_chat_state(
            str(chat_id), "delivered",
            account_id=account_id,
            buyer_id=str(buyer_id),
            buyer_name=buyer_name,
            order_id=order_id
        )
        chat_states[str(chat_id)] = {
            "stage": "delivered",
            "account_id": account_id,
            "buyer_id": str(buyer_id),
            "buyer_name": buyer_name,
            "order_id": order_id
        }

        message = (
            f"🎮 Спасибо за покупку!\n\n"
            f"📋 Данные аккаунта Roblox с Voice Chat:\n"
            f"👤 Логин: {roblox_login}\n"
            f"🔑 Пароль: {roblox_pass}\n"
            f"📧 Почта: {email}\n\n"
            f"⚠️ ВАЖНО: Смени почту на свою!\n\n"
            f"📖 Инструкция:\n"
            f"1️⃣ roblox.com → Настройки → Account Info\n"
            f"2️⃣ Email Address → Change Email\n"
            f"3️⃣ Введи СВОЮ почту\n"
            f"4️⃣ Send Verification Code\n"
            f"5️⃣ Напиши мне \"КОД\" — я пришлю код\n"
            f"6️⃣ Введи код на Roblox\n"
            f"7️⃣ Готово! ✅"
        )

        success = await async_send_fp_message(fp, chat_id, message, admin_chat_id)
        if success:
            logger.info(f"[DELIVER] ✅ #{account_id} → {buyer_name} (Заказ: {order_id})")

            await safe_notify_admin(
                admin_chat_id,
                f"✅ <b>Аккаунт выдан!</b>\n"
                f"👤 Покупатель: {buyer_name}\n"
                f"📦 Аккаунт: #{account_id} ({roblox_login})\n"
                f"🧾 Заказ: {order_id}"
            )

    except Exception as e:
        logger.error(f"[DELIVER] ❌ {e}", exc_info=True)
        await async_send_fp_message(fp, chat_id, "⚠️ Ошибка системы. Обратитесь к администратору.", admin_chat_id)


async def async_send_fp_message(fp, chat_id, text, admin_chat_id, max_retries=3):
    for attempt in range(max_retries):
        try:
            result = await asyncio.to_thread(fp.send_message, chat_id, text)
            if result is not None:
                logger.info(f"[SEND] ✅ Сообщение отправлено в чат {chat_id}")
                return True
            else:
                logger.warning(f"[SEND] ⚠️ fp.send_message вернул None для чата {chat_id}")
        except Exception as e:
            logger.error(f"[SEND] ❌ Попытка {attempt + 1}/{max_retries} для чата {chat_id}: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)

    try:
        await bot.send_message(admin_chat_id, f"❌ Не удалось отправить сообщение в FunPay чат {chat_id}")
    except Exception:
        pass
    return False


async def async_find_roblox_code(ec, target_email, timeout=60, check_interval=3):
    return await asyncio.to_thread(ec.find_roblox_code, target_email, timeout, check_interval)


async def send_verification_code(fp, ec, chat_id, account_id, admin_chat_id, chat_states):
    try:
        account = db.get_account_by_id(account_id)
        if not account:
            await async_send_fp_message(fp, chat_id, "❌ Ошибка: не найден аккаунт. Обратись к продавцу.", admin_chat_id)
            return

        target_email = account[3]
        await async_send_fp_message(fp, chat_id, f"🔍 Ищу код верификации для {target_email}... Подожди 10-30 секунд.", admin_chat_id)

        code = await async_find_roblox_code(ec, target_email, timeout=60, check_interval=3)

        if code:
            # Сохраняем state
            db.set_chat_state(str(chat_id), "code_sent", account_id=account_id)
            chat_states[str(chat_id)] = {"stage": "code_sent", "account_id": account_id}
            
            await async_send_fp_message(
                fp, chat_id,
                f"✅ Код найден!\n\n"
                f"🔢 Твой код верификации Roblox: {code}\n\n"
                f"Введи его на странице смены почты.\n\n"
                f"После успешной смены напиши \"СМЕНИЛ\" ✅",
                admin_chat_id
            )
            logger.info(f"[CODE] ✅ Код для #{account_id}")
        else:
            await async_send_fp_message(
                fp, chat_id,
                "❌ Код не найден. Возможные причины:\n"
                "1️⃣ Ты ещё не нажал \"Send Verification Code\"\n"
                "2️⃣ Письмо ещё не пришло (подожди 1-2 мин)\n"
                "3️⃣ Письмо в спаме\n\n"
                "Попробуй ещё раз — напиши \"КОД\"",
                admin_chat_id
            )

    except Exception as e:
        logger.error(f"[CODE] ❌ {e}", exc_info=True)
        await async_send_fp_message(fp, chat_id, f"⚠️ Ошибка при проверке почты: {e}", admin_chat_id)


# ============================================================================
# MAIN
# ============================================================================
async def main():
    logger.info("🤖 Запуск Telegram бота...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
