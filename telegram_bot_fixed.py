"""
🤖 Roblox Seller Bot — Панель управления через Telegram
(ИСПРАВЛЕННАЯ ВЕРСИЯ v2: фикс критических багов + улучшения)
"""
import asyncio
import logging
import time
import re
from collections import deque
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
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

bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
db = Database()

# Глобальное состояние
funpay_account = None
email_checker = None
last_processed_msg_ids = {}
processed_messages = {}  # chat_id -> deque с автоочисткой
pending_tasks = {}       # chat_id -> asyncio.Task

# Настройка дебаунса (в секундах)
DEBOUNCE_SECONDS = 1.5
MAX_PROCESSED_IDS = 200  # Автоочистка старых ID
NOTIFY_COOLDOWN = 30     # Кулдаун уведомлений админу (сек)

# Кулдаун уведомлений
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
    """Отправка уведомления админу с кулдауном, чтобы не спамить"""
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
# ПРОВЕРКА ОПЛАТЫ ЧЕРЕЗ API FUNPAY
# ============================================================================
async def check_order_payment(fp, chat_id, buyer_id):
    """
    Проверяет оплату через FunPay API.
    Ищет заказы покупателя и проверяет статус.
    """
    try:
        # Пробуем получить заказы через API
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

        # Fallback: проверяем через HTML если API не доступен
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

        for row in orders_table.find_all('tr')[1:]:  # skip header
            cells = row.find_all('td')
            if len(cells) < 3:
                continue

            # Ищем по buyer_id в ссылке на профиль или имени
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
async def cmd_start(message: types.Message):
    user_id = message.from_user.id

    if not is_admin(user_id):
        await message.answer("❌ У тебя нет доступа к боту.")
        return

    init_services()
    settings = db.get_all_settings()

    status_lines = []
    status_lines.append("✅ FunPay настроен" if settings.get('funpay_key') else "❌ FunPay НЕ настроен")
    status_lines.append("✅ Gmail настроен" if settings.get('gmail_email') else "❌ Gmail НЕ настроен")

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
    """Ищет аккаунт по ID только если пользователь в состоянии ожидания ID"""
    if not is_admin(message.from_user.id):
        return

    current_state = await state.get_state()
    if current_state != "find_account_wait_id":
        return  # Игнорируем цифры в других состояниях

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

    # Инициализируем FunPay если ещё не готов
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

        # Отменяем все pending задачи
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
    chat_states = {}

    loop = asyncio.get_running_loop()
    fail_count_ref = [0]

    def make_runner():
        fp.runner = None
        return Runner(fp)

    async def debounced_handle(msg):
        """Дебаунс с автоочисткой старых ID"""
        try:
            await asyncio.sleep(DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return

        chat_id = msg.chat_id
        msg_id = getattr(msg, 'id', 0)

        # Используем deque вместо set для автоочистки
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
            # Чистим задачу из pending
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
                return  # своё же сообщение

            chat_id = msg.chat_id

            # Дебаунс: отменяем старую задачу, создаём новую
            old_task = pending_tasks.pop(chat_id, None)
            if old_task and not old_task.done():
                old_task.cancel()

            pending_tasks[chat_id] = asyncio.create_task(debounced_handle(msg))

        except Exception as e:
            logger.error(f"[PROCESS ERROR] ❌ {e}", exc_info=True)

    # Используем глобальный email_checker или создаём новый
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

        # Уведомление админу с кулдауном
        await safe_notify_admin(
            admin_chat_id,
            f"💬 <b>Новое сообщение от {sender_name}</b>\n"
            f"💬 Чат: {chat_id}\n"
            f"📝 Текст: {(msg.text or '')[:100]}"
        )

        state = chat_states.get(chat_id, {"stage": "new", "account_id": None})

        try:
            if state["stage"] == "new":
                account = db.get_account_by_funpay_chat(chat_id)

                if account:
                    state = {"stage": "delivered", "account_id": account[0]}
                    chat_states[chat_id] = state
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
                    await async_send_fp_message(
                        fp, chat_id,
                        "🎉 Отлично! Аккаунт перевязан на твою почту!\n\n"
                        "✅ Теперь аккаунт полностью твой. Спасибо за покупку! 🚀",
                        admin_chat_id
                    )
                    chat_states[chat_id] = {"stage": "completed", "account_id": state["account_id"]}
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
            chat_states[chat_id] = {"stage": "delivered", "account_id": account_id}
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
    """Отправка сообщения в FunPay с retry и exponential backoff"""
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
                await asyncio.sleep(2 ** attempt)  # exponential backoff: 1, 2, 4 сек

    # Все попытки исчерпаны
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
            await async_send_fp_message(
                fp, chat_id,
                f"✅ Код найден!\n\n"
                f"🔢 Твой код верификации Roblox: {code}\n\n"
                f"Введи его на странице смены почты.\n\n"
                f"После успешной смены напиши \"СМЕНИЛ\" ✅",
                admin_chat_id
            )
            chat_states[chat_id] = {"stage": "code_sent", "account_id": account_id}
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
