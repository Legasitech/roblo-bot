import asyncio
from funpay_service import create_account
import config

async def test():
    try:
        acc = create_account(config.FUNPAY_EXTRA_COOKIES.split("golden_key=")[1].split(";")[0])
        print(f"✅ Аккаунт: {acc.username}")
        
        # Попробуй отправить тестовое сообщение
        # (ЭТО РЕАЛЬНОЕ СООБЩЕНИЕ! Отправляй только на тестовый чат!)
        result = acc.send_message(272992482, "🤖 Тест бота - игнорируй")
        print(f"Результат отправки: {result}")
        
    except Exception as e:
        print(f"❌ Ошибка: {e}")

asyncio.run(test())