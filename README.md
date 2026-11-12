# Ausbildung-Bot

Telegram-бот, который присылает новые вакансии Ausbildung из Jobsuche API
Bundesagentur für Arbeit и сохраняет историю в SQLite для анализа.

## Установка

Нужен Python 3.10 или новее.

1. Создай бота: напиши [@BotFather](https://t.me/BotFather) команду `/newbot` и скопируй токен.
2. Положи `bot.py`, `requirements.txt` и `.env.example` в одну папку.
3. Скопируй `.env.example` в `.env` и впиши токен в `BOT_TOKEN=`.
4. Установи зависимости и запусти:

```
pip install -r requirements.txt
python bot.py
```

5. Открой своего бота в Telegram и отправь `/start`.

## Команды

| Команда | Что делает |
|---|---|
| `/start` | подписка; при первом запуске сразу идёт проверка |
| `/check` | проверить новые вакансии сейчас |
| `/last 15` | последние вакансии из базы (по умолчанию 10) |
| `/stats` | статистика: активные, новые за неделю, топ городов и фирм |
| `/stop` | отписаться |

## Как это работает

- Каждые `CHECK_INTERVAL_HOURS` часов бот запрашивает API по каждому запросу из
  `SEARCH_TERMS` с `angebotsart=4` (Ausbildung / Duales Studium).
- Вакансии сохраняются в `jobs.db`. Новой считается та, чьего `refnr` ещё нет в базе.
- У каждой вакансии хранятся `first_seen` и `last_seen`, поэтому со временем
  видно, сколько объявления висят и какие закрылись.
- При первом запуске в базу попадает всё, а в чат уходят только вакансии
  за последние `FIRST_RUN_DAYS` дней.

Бот работает, пока запущен скрипт. Для круглосуточной работы нужен компьютер
или сервер, который не выключается (VPS, Raspberry Pi).

## Анализ истории

База — обычный SQLite, её можно открыть в DB Browser for SQLite или pandas:

```python
import sqlite3, pandas as pd
df = pd.read_sql("SELECT * FROM jobs", sqlite3.connect("jobs.db"))
print(df.groupby("region").size().sort_values(ascending=False).head(10))
```
