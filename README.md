# 🤖 Roblox Seller Bot — Telegram Control Panel

Автоматическая продажа аккаунтов Roblox с Voice Chat через FunPay.

## 🚀 Быстрый старт

### 1. Получи Telegram Bot Token
1. Напиши [@BotFather](https://t.me/BotFather)
2. Команда `/newbot`
3. Скопируй токен

### 2. Узнай свой Telegram ID
1. Напиши [@userinfobot](https://t.me/userinfobot)
2. Скопируй ID

### 3. Установи зависимости
```bash
pip install -r requirements.txt
```

### 4. Заполни config.py
```python
TELEGRAM_BOT_TOKEN = "123456:ABC-DEF..."  # Токен от @BotFather
ADMIN_IDS = [123456789]  # Твой Telegram ID
```

### 4.1. ⚠️ ВАЖНО — заполни FUNPAY_USER_AGENT и FUNPAY_EXTRA_COOKIES

Если Runner падает с ошибкой `"Необходимая cookie отсутствует или устарела"`,
это значит, что FunPay требует больше данных, чем просто `golden_key`.

1. Открой funpay.com в браузере (залогинен), нажми **F12** → вкладка **Network**
2. Обнови страницу, кликни на любой запрос к `funpay.com`
3. **Headers → Request Headers → Cookie:** → скопируй значение **целиком**
4. Вставь в `config.py`:
   ```python
   FUNPAY_EXTRA_COOKIES = "golden_key=...; PHPSESSID=...; locale=ru; cy=RUB; ..."
   ```
5. Там же в Request Headers найди `User-Agent:` → скопируй и вставь в:
   ```python
   FUNPAY_USER_AGENT = "Mozilla/5.0 ..."
   ```

`golden_key` и `PHPSESSID` внутри строки автоматически обновляются кодом —
остальное (locale, cy, cookie_prefs и т.д.) используется как есть.

### 5. Запусти бота
```bash
python telegram_bot.py
```

### 6. Напиши боту `/start`

## 📱 Команды в Telegram

| Кнопка | Действие |
|--------|----------|
| ⚙️ **Настройки** | Настроить FunPay и Gmail |
| ➕ **Добавить аккаунт** | Добавить один аккаунт Roblox |
| 📋 **Все аккаунты** | Список всех аккаунтов |
| 📊 **Статистика** | Сколько свободно/продано |
| 🔍 **Найти аккаунт** | Поиск по ID |
| ▶️ **Запустить FunPay** | Авто-выдача 24/7 |
| ⏹ **Остановить FunPay** | Остановить авто-выдачу |
| 📧 **Проверить почту** | Проверить Gmail IMAP |
| 🧪 **Тест подключений** | Проверить FunPay + Gmail |

## 🔄 Как работает продажа

```
[Покупатель на FunPay] → пишет сообщение
         ↓
[Бот] → видит сообщение → выдаёт данные аккаунта
         ↓
[Покупатель] → меняет почту → пишет "КОД"
         ↓
[Бот] → лезет в Gmail → находит код → отправляет
         ↓
[Покупатель] → пишет "СМЕНИЛ" → аккаунт перевязан
```

## 📧 Gmail + плюсы

Твоя почта: `lolzxcded@gmail.com`

Автоматически создаются:
- `lolzxcded+roblox_vc_1@gmail.com`
- `lolzxcded+roblox_vc_2@gmail.com`
- и т.д.

Все письма падают в твой основной Gmail.

## ⚠️ Важно

- Удали старый `accounts.db` перед первым запуском!
- FunPay может забанить за бота — используй задержки
- Golden Key протухает если выйти с FunPay
- Рекомендуется VPS за 200-500₽/мес (Hetzner, Timeweb)
