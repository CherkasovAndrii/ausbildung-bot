"""Telegram-бот: присылает новые вакансии Ausbildung из Jobsuche API
Bundesagentur für Arbeit и копит историю в SQLite.

Запуск:  python bot.py   (настройки — в файле .env, см. .env.example)
"""

import asyncio
import html
import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta

import httpx
from dotenv import load_dotenv
from telegram import LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, Defaults

load_dotenv()

# ---------------------------------------------------------------- настройки


def _list(name: str, default: str = "") -> list[str]:
    """Значение из .env вида 'a;b;c' -> ['a', 'b', 'c']."""
    return [x.strip() for x in os.getenv(name, default).split(";") if x.strip()]


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SEARCH_TERMS = _list(
    "SEARCH_TERMS",
    "Fachinformatiker Systemintegration;"
    "Fachinformatiker Anwendungsentwicklung;"
    "Kaufmann für Digitalisierungsmanagement",
)
WO = os.getenv("WO", "").strip()                    # пусто = вся Германия
UMKREIS = int(os.getenv("UMKREIS", "50"))           # км вокруг WO
EXCLUDE_WORDS = [w.lower() for w in _list("EXCLUDE_WORDS")]
MIN_START = os.getenv("MIN_START", "").strip()      # напр. 2027-01-01
CHECK_INTERVAL_HOURS = float(os.getenv("CHECK_INTERVAL_HOURS", "6"))
FIRST_RUN_DAYS = int(os.getenv("FIRST_RUN_DAYS", "3"))
MAX_NOTIFY = int(os.getenv("MAX_NOTIFY", "60"))     # максимум вакансий за одну рассылку
MAX_PAGES = int(os.getenv("MAX_PAGES", "20"))       # страниц по 100 на один запрос
# На Railway база кладётся на подключённый Volume, чтобы переживать деплои
DB_PATH = os.getenv("DB_PATH") or os.path.join(
    os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ""), "jobs.db"
)

API_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
# Эндпоинты поиска в порядке предпочтения; бот запоминает первый рабочий.
# API_URL в .env позволяет задать конкретный адрес вручную.
API_URLS = [os.getenv("API_URL")] if os.getenv("API_URL") else [
    f"{API_BASE}/pc/v4/app/jobs",
    f"{API_BASE}/pc/v6/jobs",
    f"{API_BASE}/pc/v4/jobs",
]
API_HEADERS = {"X-API-Key": "jobboerse-jobsuche", "User-Agent": "ausbildung-bot/1.0"}
ANGEBOTSART_AUSBILDUNG = 4                          # Ausbildung / Duales Studium
PAGE_SIZE = 100
JOB_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ausbildung-bot")

CHECK_LOCK = asyncio.Lock()

# ------------------------------------------------------------------- база


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                refnr          TEXT PRIMARY KEY,
                titel          TEXT,
                beruf          TEXT,
                arbeitgeber    TEXT,
                plz            TEXT,
                ort            TEXT,
                region         TEXT,
                eintrittsdatum TEXT,
                veroeffentlicht TEXT,
                externe_url    TEXT,
                suchbegriff    TEXT,
                first_seen     TEXT NOT NULL,
                last_seen      TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chats (
                chat_id  INTEGER PRIMARY KEY,
                added_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )


def get_meta(key: str) -> str | None:
    with db() as con:
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with db() as con:
        con.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_chats() -> list[int]:
    with db() as con:
        return [r["chat_id"] for r in con.execute("SELECT chat_id FROM chats")]


def save_jobs(rows: list[dict], now: str) -> list[dict]:
    """Обновляет last_seen у известных вакансий, добавляет новые. Возвращает новые."""
    new = []
    with db() as con:
        for row in rows:
            cur = con.execute(
                "UPDATE jobs SET last_seen=? WHERE refnr=?", (now, row["refnr"])
            )
            if cur.rowcount == 0:
                con.execute(
                    "INSERT INTO jobs VALUES "
                    "(:refnr, :titel, :beruf, :arbeitgeber, :plz, :ort, :region, "
                    ":eintrittsdatum, :veroeffentlicht, :externe_url, :suchbegriff, "
                    ":now, :now)",
                    {**row, "now": now},
                )
                new.append(row)
    return new


# -------------------------------------------------------------------- API


async def get_json(client: httpx.AsyncClient, params: dict) -> dict:
    """GET к API поиска. Если эндпоинт отвечает 403/404, пробует следующий.
    Сетевые ошибки и 5xx повторяет до трёх раз."""
    last_error: Exception | None = None
    for url in list(API_URLS):
        for attempt in range(3):
            try:
                resp = await client.get(url, params=params)
                if resp.status_code in (403, 404):
                    log.warning("%s → %s: %s", url, resp.status_code, resp.text[:200])
                    last_error = httpx.HTTPStatusError(
                        f"{resp.status_code} от {url}", request=resp.request, response=resp
                    )
                    break  # к следующему эндпоинту
                resp.raise_for_status()
                data = resp.json()
                if url != API_URLS[0]:
                    API_URLS.remove(url)
                    API_URLS.insert(0, url)
                    log.info("Рабочий эндпоинт: %s", url)
                if not any(k in data for k in ("stellenangebote", "ergebnisliste")):
                    log.warning("Неожиданный ответ API, ключи: %s", list(data)[:20])
                return data
            except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as err:
                last_error = err
                if attempt == 2:
                    break
                await asyncio.sleep(2 * (attempt + 1))
    raise last_error or RuntimeError("API недоступен")


_SAMPLE_LOGGED = False


def _first(item: dict, *keys: str):
    """Первое непустое поле из списка возможных названий."""
    for key in keys:
        if item.get(key):
            return item[key]
    return None


def _text(value) -> str:
    """Строка из значения, которое в разных версиях API бывает str, dict или list."""
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = _first(value, "bezeichnung", "name", "titel", "kurzBezeichnung") or ""
    return str(value or "").strip()


def normalize(item: dict, term: str) -> dict | None:
    """Вакансия из ответа API (v4 или v6) -> строка для таблицы jobs."""
    global _SAMPLE_LOGGED
    if not _SAMPLE_LOGGED:
        _SAMPLE_LOGGED = True
        log.info("Образец вакансии из API: %s", json.dumps(item, ensure_ascii=False)[:800])
    refnr = _text(_first(item, "refnr", "referenznummer"))
    if not refnr:
        return None
    ort = _first(item, "arbeitsort", "arbeitsorte") or {}
    if isinstance(ort, list):  # в v6 места работы списком
        ort = ort[0] if ort else {}
    if not isinstance(ort, dict):
        ort = {"ort": str(ort)}
    datum = _text(_first(
        item, "aktuelleVeroeffentlichungsdatum", "ersteVeroeffentlichungsdatum",
        "veroeffentlichungsdatum", "modifikationsTimestamp",
    ))
    return {
        "refnr": refnr,
        "titel": _text(_first(item, "titel", "stellenangebotsTitel", "stellenbezeichnung")),
        "beruf": _text(_first(item, "beruf", "hauptberuf", "berufsbezeichnung")),
        "arbeitgeber": _text(_first(item, "arbeitgeber", "arbeitgeberName", "firma")),
        "plz": str(ort["plz"]).zfill(5) if ort.get("plz") else "",
        "ort": _text(ort.get("ort")),
        "region": _text(ort.get("region")),
        "eintrittsdatum": _text(item.get("eintrittsdatum"))[:10],
        "veroeffentlicht": datum[:10],
        "externe_url": _text(_first(item, "externeUrl", "externeURL")),
        "suchbegriff": term,
    }


async def fetch_term(client: httpx.AsyncClient, term: str) -> list[dict]:
    """Все вакансии Ausbildung по одному поисковому запросу (с пагинацией)."""
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        params = {
            "angebotsart": ANGEBOTSART_AUSBILDUNG,
            "was": term,
            "page": page,
            "size": PAGE_SIZE,
        }
        if WO:
            params["wo"] = WO
            params["umkreis"] = UMKREIS
        data = await get_json(client, params)
        items = data.get("stellenangebote") or data.get("ergebnisliste") or []
        rows += [r for r in (normalize(i, term) for i in items) if r]
        total = int(data.get("maxErgebnisse") or 0)
        if len(items) < PAGE_SIZE or (total and page * PAGE_SIZE >= total):
            break
        await asyncio.sleep(0.5)  # не долбим API
    return rows


# ------------------------------------------------------- фильтры и формат


def wanted(row: dict) -> bool:
    text = f"{row['titel']} {row['beruf']}".lower()
    if any(word in text for word in EXCLUDE_WORDS):
        return False
    if MIN_START and row["eintrittsdatum"] and row["eintrittsdatum"] < MIN_START:
        return False
    return True


def de_date(iso: str) -> str:
    """2027-08-01 -> 01.08.2027"""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return iso


def format_job(row: dict) -> str:
    esc = html.escape
    title = row["titel"] or row["beruf"] or "Ausbildung"
    lines = [f"<b>{esc(title)}</b>"]
    if row["beruf"] and row["beruf"] != title:
        lines.append(f"<i>{esc(row['beruf'])}</i>")
    if row["arbeitgeber"]:
        lines.append(f"🏢 {esc(row['arbeitgeber'])}")
    place = " ".join(x for x in (row["plz"], row["ort"]) if x)
    if row["region"] and row["region"] != row["ort"]:
        place += f" ({row['region']})"
    info = []
    if place.strip():
        info.append(f"📍 {esc(place.strip())}")
    if row["eintrittsdatum"]:
        info.append(f"🗓 начало {de_date(row['eintrittsdatum'])}")
    if info:
        lines.append(" · ".join(info))
    links = [f'<a href="{esc(JOB_URL.format(refnr=row["refnr"]))}">Открыть вакансию</a>']
    if row["externe_url"]:
        links.append(f'<a href="{esc(row["externe_url"])}">сайт работодателя</a>')
    published = f" · опубл. {de_date(row['veroeffentlicht'])}" if row["veroeffentlicht"] else ""
    lines.append(" | ".join(links) + published)
    return "\n".join(lines)


def chunk_messages(header: str, rows: list[dict], limit: int = 3900) -> list[str]:
    """Склеивает вакансии в сообщения не длиннее лимита Telegram (4096)."""
    messages, current = [], header
    for row in rows:
        block = format_job(row)
        if len(current) + len(block) + 2 > limit:
            messages.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        messages.append(current)
    return messages


async def broadcast(bot, messages: list[str], chats: list[int] | None = None) -> None:
    for chat_id in chats if chats is not None else get_chats():
        for text in messages:
            try:
                await bot.send_message(chat_id, text)
                await asyncio.sleep(0.5)
            except Forbidden:  # пользователь заблокировал бота
                with db() as con:
                    con.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
                break
            except TelegramError as err:
                log.warning("Не удалось отправить в %s: %s", chat_id, err)


# --------------------------------------------------------------- проверка


async def run_check(bot) -> str:
    """Опрашивает API, сохраняет историю, рассылает новое. Возвращает итог строкой."""
    async with CHECK_LOCK:
        with db() as con:  # первый запуск = в базе ещё нет ни одной вакансии
            first_run = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        found, new, errors = 0, [], []

        async with httpx.AsyncClient(headers=API_HEADERS, timeout=30) as client:
            for term in SEARCH_TERMS:
                try:
                    rows = await fetch_term(client, term)
                except (httpx.HTTPError, ValueError) as err:
                    log.error("Запрос «%s» не удался: %s", term, err)
                    errors.append(term)
                    continue
                found += len(rows)
                new += save_jobs(rows, now)

        if len(errors) < len(SEARCH_TERMS):
            set_meta("last_check", now)

        to_send = [r for r in new if wanted(r)]
        if first_run:
            # при первом запуске в базу попадает всё, а в чат — только свежее
            cutoff = (date.today() - timedelta(days=FIRST_RUN_DAYS)).isoformat()
            to_send = [r for r in to_send if r["veroeffentlicht"] >= cutoff]
        to_send.sort(key=lambda r: r["veroeffentlicht"], reverse=True)
        extra = max(0, len(to_send) - MAX_NOTIFY)
        to_send = to_send[:MAX_NOTIFY]

        if to_send:
            if first_run:
                header = (
                    f"👋 Первая проверка: в базе {len(new)} вакансий. "
                    f"Свежие за {FIRST_RUN_DAYS} дн.: {len(to_send) + extra}"
                )
            else:
                header = f"🆕 Новых вакансий: {len(to_send) + extra}"
            messages = chunk_messages(header, to_send)
            if extra:
                messages.append(
                    f"…и ещё {extra}. Чтобы приходило меньше, сузь фильтры в .env "
                    "(WO, EXCLUDE_WORDS, MIN_START)."
                )
            await broadcast(bot, messages)

        summary = f"Найдено в API: {found}, новых в базе: {len(new)}, отправлено: {len(to_send)}."
        if errors:
            summary += "\n⚠️ Не удалось запросить: " + ", ".join(errors)
        log.info(summary.replace("\n", " "))
        return summary


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not get_chats():
        log.info("Подписчиков нет — жду /start")
        return
    try:
        await run_check(context.bot)
    except Exception:  # фоновая задача не должна падать
        log.exception("Ошибка плановой проверки")


# ---------------------------------------------------------------- команды


def settings_text() -> str:
    lines = ["<b>Что ищу:</b>"] + [f"• {html.escape(t)}" for t in SEARCH_TERMS]
    lines.append(f"<b>Где:</b> {html.escape(WO) + f' (+{UMKREIS} км)' if WO else 'вся Германия'}")
    if EXCLUDE_WORDS:
        lines.append("<b>Исключаю:</b> " + html.escape(", ".join(EXCLUDE_WORDS)))
    if MIN_START:
        lines.append(f"<b>Начало не раньше:</b> {de_date(MIN_START)}")
    lines.append(f"<b>Проверка:</b> каждые {CHECK_INTERVAL_HOURS:g} ч")
    return "\n".join(lines)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with db() as con:
        con.execute(
            "INSERT OR IGNORE INTO chats VALUES (?, ?)",
            (update.effective_chat.id, datetime.now().isoformat(timespec="seconds")),
        )
    await update.message.reply_text(
        "Подписка включена — буду присылать новые вакансии Ausbildung.\n\n"
        + settings_text()
        + "\n\n/check — проверить сейчас\n/last — последние вакансии из базы\n"
        "/stats — статистика\n/stop — отписаться"
    )
    if get_meta("last_check") is None:
        await update.message.reply_text("Запускаю первую проверку, это займёт около минуты…")
        context.application.create_task(scheduled_check(context))


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with db() as con:
        con.execute("DELETE FROM chats WHERE chat_id=?", (update.effective_chat.id,))
    await update.message.reply_text("Отписал. /start — подписаться снова.")


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Проверяю…")
    try:
        summary = await run_check(context.bot)
    except Exception as err:
        log.exception("Ошибка проверки")
        summary = f"Ошибка: {err}"
    await update.message.reply_text(html.escape(summary))


async def cmd_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        count = min(30, max(1, int(context.args[0])))
    except (IndexError, ValueError):
        count = 10
    with db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM jobs ORDER BY veroeffentlicht DESC, first_seen DESC LIMIT 300"
        )]
    rows = [r for r in rows if wanted(r)][:count]
    if not rows:
        await update.message.reply_text("В базе пока пусто. Попробуй /check.")
        return
    await broadcast(
        context.bot,
        chunk_messages(f"Последние {len(rows)} вакансий в базе:", rows),
        chats=[update.effective_chat.id],
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    last_check = get_meta("last_check")
    if not last_check:
        await update.message.reply_text("Статистики пока нет — сначала /check.")
        return
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as con:
        total = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        active = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE last_seen >= ?", (last_check,)
        ).fetchone()[0]
        week = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE first_seen >= ?", (week_ago,)
        ).fetchone()[0]
        lifetime = con.execute(
            "SELECT AVG(julianday(last_seen) - julianday(COALESCE(NULLIF(veroeffentlicht, ''), first_seen))) "
            "FROM jobs WHERE last_seen < ?", (last_check,)
        ).fetchone()[0]
        by_term = con.execute(
            "SELECT suchbegriff, COUNT(*) n FROM jobs WHERE last_seen >= ? "
            "GROUP BY suchbegriff ORDER BY n DESC", (last_check,)
        ).fetchall()
        cities = con.execute(
            "SELECT ort, COUNT(*) n FROM jobs WHERE last_seen >= ? AND ort != '' "
            "GROUP BY ort ORDER BY n DESC LIMIT 5", (last_check,)
        ).fetchall()
        employers = con.execute(
            "SELECT arbeitgeber, COUNT(*) n FROM jobs WHERE last_seen >= ? AND arbeitgeber != '' "
            "GROUP BY arbeitgeber ORDER BY n DESC LIMIT 5", (last_check,)
        ).fetchall()

    esc = html.escape
    lines = [
        "<b>Статистика</b>",
        f"Всего в базе: {total}",
        f"Активных сейчас: {active}",
        f"Новых за 7 дней: {week}",
    ]
    if lifetime is not None:
        lines.append(f"Закрытые вакансии висели в среднем {lifetime:.0f} дн.")
    if by_term:
        lines += ["", "<b>Активные по запросам:</b>"]
        lines += [f"• {esc(r['suchbegriff'])} — {r['n']}" for r in by_term]
    if cities:
        lines += ["", "<b>Топ городов:</b>"]
        lines += [f"• {esc(r['ort'])} — {r['n']}" for r in cities]
    if employers:
        lines += ["", "<b>Топ работодателей:</b>"]
        lines += [f"• {esc(r['arbeitgeber'])} — {r['n']}" for r in employers]
    lines += ["", f"Последняя проверка: {last_check}"]
    await update.message.reply_text("\n".join(lines))


# ------------------------------------------------------------------ запуск


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Скопируй .env.example в .env и впиши токен.")
    init_db()
    defaults = Defaults(
        parse_mode=ParseMode.HTML,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .defaults(defaults)
        .connect_timeout(20)
        .read_timeout(20)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("last", cmd_last))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.job_queue.run_repeating(
        scheduled_check, interval=CHECK_INTERVAL_HOURS * 3600, first=10
    )
    log.info("Бот запущен. Запросы: %s", "; ".join(SEARCH_TERMS))
    # -1: при старте бесконечно повторять подключение к Telegram, а не падать
    app.run_polling(bootstrap_retries=-1)


if __name__ == "__main__":
    main()
