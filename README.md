# 📚 Reading Check Bot

Telegram-бот для проверки чтения. Ребёнок отправляет фото страниц книги,
записывает голосовой пересказ — бот проверяет понимание и уведомляет родителя.

## Как работает

```
Ребёнок                          Бот                        Родитель
   │                              │                              │
   ├─── /start ──────────────────>│                              │
   ├─── 📷 Фото страниц ─────────>│                              │
   ├─── /done ───────────────────>│                              │
   ├─── "5-10" (страницы) ───────>│                              │
   ├─── 🎙 Голосовое сообщение ──>│                              │
   │                              ├── Groq Whisper (расшифровка)─┤
   │                              ├── Claude Vision (текст книги)─┤
   │                              ├── Claude (проверка понимания)─┤
   │<── ✅/❌ Результат + оценка ──┤                              │
   │                              ├─── 📩 Отчёт ────────────────>│
```

## Получение API ключей

### 1. Telegram Bot Token
1. Открой [@BotFather](https://t.me/BotFather) в Telegram
2. Отправь `/newbot`
3. Следуй инструкциям, получи токен вида `7123456789:AAF...`

### 2. Твой Telegram Chat ID (PARENT_CHAT_ID)
1. Напиши [@userinfobot](https://t.me/userinfobot) — он пришлёт твой ID
2. Это число вида `123456789`

### 3. Anthropic API Key
1. Зайди на [console.anthropic.com](https://console.anthropic.com)
2. API Keys → Create Key
3. Выглядит как `sk-ant-api03-...`

### 4. Groq API Key (бесплатно)
1. Зайди на [console.groq.com](https://console.groq.com)
2. API Keys → Create API Key
3. Выглядит как `gsk_...`

---

## Деплой на Railway

### Шаг 1: Залей код на GitHub
```bash
git init
git add .
git commit -m "initial"
git remote add origin https://github.com/ТВО_ИМЯ/reading-bot.git
git push -u origin main
```

### Шаг 2: Создай проект на Railway
1. Зайди на [railway.app](https://railway.app) → New Project
2. Deploy from GitHub repo → выбери свой репозиторий
3. Railway автоматически обнаружит `Procfile`

### Шаг 3: Добавь переменные окружения
В Railway: Settings → Variables → Add Variable

| Переменная | Значение |
|---|---|
| `TELEGRAM_TOKEN` | Токен от BotFather |
| `ANTHROPIC_API_KEY` | Ключ Anthropic |
| `GROQ_API_KEY` | Ключ Groq |
| `PARENT_CHAT_ID` | Твой Telegram ID |

### Шаг 4: Deploy!
Railway задеплоит автоматически. Бот будет работать 24/7.

---

## Деплой на Render (альтернатива)

1. Зайди на [render.com](https://render.com) → New → Background Worker
2. Подключи GitHub репо
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `python bot.py`
5. Добавь те же переменные окружения в Environment

---

## Локальный запуск (для теста)

```bash
# 1. Создай виртуальное окружение
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 2. Установи зависимости
pip install -r requirements.txt

# 3. Создай .env файл
cp .env.example .env
# Отредактируй .env — вставь свои ключи

# 4. Запусти
python bot.py
```

---

## Как использовать боту

**Ребёнок делает:**
1. Пишет `/start`
2. Отправляет фото страниц книги (одно или несколько)
3. Пишет `/done`
4. Пишет диапазон страниц, например `5-10`
5. Записывает голосовое сообщение с пересказом

**Родитель получает:**
- Уведомление с оценкой и кратким анализом пересказа
- ✅ если ребёнок понял прочитанное
- ❌ если нужно перечитать

---

## Структура проекта

```
reading_bot/
├── bot.py           # Основной код бота
├── requirements.txt # Зависимости
├── Procfile         # Для Railway/Render
├── runtime.txt      # Версия Python
├── .env.example     # Шаблон переменных окружения
├── .gitignore
└── README.md
```
