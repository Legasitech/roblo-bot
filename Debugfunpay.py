"""
Быстрая диагностика подключения к FunPay БЕЗ запуска телеграм-бота.
Запусти: python debug_funpay.py
Покажет ровно то, что происходит на каждом шаге — get() и первый runner-запрос.
"""
import config
from funpay_service import create_account
from FunPayAPI import Runner
from FunPayAPI.common import exceptions as fp_exceptions

print("=" * 70)
print("ШАГ 0: Проверка config.py")
print("=" * 70)

settings_ok = True

if "placeholder" in config.FUNPAY_EXTRA_COOKIES:
    print("❌ FUNPAY_EXTRA_COOKIES всё ещё содержит 'placeholder' —")
    print("   ты не заполнил его реальными cookies из Network tab!")
    settings_ok = False
else:
    print(f"✅ FUNPAY_EXTRA_COOKIES заполнен ({len(config.FUNPAY_EXTRA_COOKIES)} символов)")
    # покажем что там, но замаскируем значения кроме имён
    names = [p.strip().split("=")[0] for p in config.FUNPAY_EXTRA_COOKIES.split(";") if p.strip()]
    print(f"   Cookies в строке: {names}")

print(f"   User-Agent: {config.FUNPAY_USER_AGENT}")

if not settings_ok:
    print("\n⛔ Сначала заполни config.py, потом запускай этот скрипт снова.")
    exit(1)

golden_key = None
try:
    from database import Database
    db = Database()
    golden_key = db.get_setting("funpay_key")
except Exception:
    pass

if not golden_key:
    golden_key = input("\nВведи golden_key вручную: ").strip()

print("\n" + "=" * 70)
print("ШАГ 1: Account.get() — логин и получение csrf_token")
print("=" * 70)

try:
    acc = create_account(golden_key)
    print(f"✅ Успешно! Аккаунт: {acc.username} (ID {acc.id})")
    print(f"   PHPSESSID: {acc.phpsessid}")
    print(f"   csrf_token: {acc.csrf_token}")
except fp_exceptions.UnauthorizedError:
    print("❌ UnauthorizedError — golden_key недействителен или ты вышел из аккаунта в браузере.")
    exit(1)
except Exception as e:
    print(f"❌ Ошибка: {e}")
    exit(1)

print("\n" + "=" * 70)
print("ШАГ 2: Первый запрос к runner/ (тот самый, что падает в боте)")
print("=" * 70)

runner = Runner(acc)
try:
    gen = runner.listen(requests_delay=5, ignore_exceptions=False)
    event = next(gen)
    print(f"✅ Успех! Получено событие: {event.type}")
except fp_exceptions.RequestFailedError as e:
    resp = e.response
    print(f"❌ RequestFailedError: статус {resp.status_code if resp else '?'}")
    print(f"   Тело ответа: {resp.text if resp else '<нет>'}")
    print(f"   Cookie, который реально был отправлен:")
    # достаём заголовки последнего запроса из requests, если возможно
    if resp is not None and resp.request is not None:
        print(f"   {resp.request.headers.get('Cookie', '<не найден>')}")
except Exception as e:
    print(f"❌ Неожиданная ошибка: {e}")
    import traceback
    traceback.print_exc()

print("\n" + "=" * 70)
print("Готово. Если ШАГ 2 упал — скопируй ВЕСЬ вывод (включая строку Cookie) и пришли.")
print("=" * 70)