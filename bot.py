"""Telegram-бот: вакансии Ausbildung из Jobsuche API Bundesagentur für Arbeit.

Фильтры настраиваются прямо в боте (/settings), вакансии показываются списком
с листанием, а по описанию каждой вакансии бот определяет требования к немецкому
и скрывает те, где нужен уровень выше твоего.

Запуск: python bot.py   (переменные окружения — см. .env.example)
"""

import asyncio
import html
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta

import httpx
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    TypeHandler,
    filters,
)

import api
import db
import matching
from config import (
    BOT_TOKEN,
    CHECK_INTERVAL_HOURS,
    DEFAULT_TERMS,
    ENRICH_BATCH,
    ENRICH_INTERVAL_MIN,
    OWNER_IDS,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ausbildung-bot")

LOCK = asyncio.Lock()       # одна операция с API за раз
PAGE_SIZE = 5
MAX_TERMS = 10
JOB_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"
esc = html.escape


def B(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def de_date(iso: str) -> str:
    """2027-08-01 -> 01.08.2027"""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return iso


# ================================================================= доступ

REQUEST_COOLDOWN = 12 * 3600   # не чаще одного запроса доступа от человека за 12 часов
DENY_SILENCE = 7 * 24 * 3600   # после отказа — неделю без новых запросов


def user_name(user) -> str:
    name = user.full_name or str(user.id)
    return f"{name} (@{user.username})" if user.username else name


def setup_access() -> None:
    """Назначает владельца при запуске.

    1) OWNER_ID из переменных окружения; 2) если владельца нет, а подписчик ровно один —
    он и становится владельцем; 3) иначе в лог пишется одноразовый код для /claim.
    """
    for uid in OWNER_IDS:
        db.add_user(uid, "", "owner")
    if db.owner_ids():
        db.set_meta("claim_code", "")
        return
    subs = db.private_subscribers()
    if len(subs) == 1:
        db.add_user(subs[0], "", "owner")
        db.set_meta("owner_greet", str(subs[0]))
        log.info("Владельцем назначен единственный подписчик %s", subs[0])
        return
    code = secrets.token_hex(4)
    db.set_meta("claim_code", code)
    log.warning("🔐 Владелец бота не назначен. Отправь боту: /claim %s  "
                "(или задай переменную OWNER_ID со своим Telegram ID)", code)


async def set_owner_commands(bot, owner_id: int) -> None:
    try:
        await bot.set_my_commands(USER_COMMANDS + OWNER_COMMANDS, scope=BotCommandScopeChat(owner_id))
    except TelegramError as err:
        log.warning("Не удалось задать меню владельцу %s: %s", owner_id, err)


async def request_access(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> None:
    """Отказ незнакомцу и запрос владельцу с кнопками «Пустить / Отклонить»."""
    requests = context.bot_data.setdefault("access_requests", {})
    now = time.time()
    send = now >= requests.get(user.id, 0)
    if update.callback_query:
        await update.callback_query.answer("🔒 Нет доступа", show_alert=True)
    elif update.message:
        status = ("Я отправил владельцу запрос на доступ." if send and db.owner_ids()
                  else "Попроси владельца бота добавить тебя.")
        await update.message.reply_text(
            "🔒 Это личный бот: пользоваться им могут только те, кого добавил владелец.\n\n"
            f"Твой Telegram ID: <code>{user.id}</code>\n{status}"
        )
    if not send:
        return
    requests[user.id] = now + REQUEST_COOLDOWN
    context.bot_data.setdefault("pending_names", {})[user.id] = user_name(user)
    kb = InlineKeyboardMarkup([[B("✅ Пустить", f"acc:ok:{user.id}"), B("❌ Отклонить", f"acc:no:{user.id}")]])
    for owner in db.owner_ids():
        try:
            await context.bot.send_message(
                owner, f"🔐 Запрос доступа к боту:\n<b>{esc(user_name(user))}</b>, ID <code>{user.id}</code>",
                reply_markup=kb,
            )
        except TelegramError as err:
            log.warning("Не удалось отправить запрос владельцу %s: %s", owner, err)


async def gatekeeper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пропускает дальше только пользователей из whitelist (группа -1, до всех хендлеров)."""
    user, chat = update.effective_user, update.effective_chat
    if user is None or (chat is not None and chat.type != "private"):
        raise ApplicationHandlerStop  # только личные чаты
    if db.is_allowed(user.id):
        db.set_user_name(user.id, user_name(user))
        return
    text = (update.message.text or "").strip() if update.message else ""

    # приглашение: /start inv_<токен>
    m = re.match(r"^/start\s+inv_([\w-]+)$", text)
    if m:
        inviter = db.use_invite(m[1], user.id)
        if inviter is not None:
            db.add_user(user.id, user_name(user), "user", inviter)
            log.info("По приглашению добавлен %s (%s)", user.id, user_name(user))
            try:
                await context.bot.send_message(inviter, f"✅ По твоей ссылке присоединился <b>{esc(user_name(user))}</b>.")
            except TelegramError:
                pass
            return  # дальше обычный /start
        await update.message.reply_text("Ссылка-приглашение недействительна или уже использована.")
        raise ApplicationHandlerStop

    # назначение владельца одноразовым кодом из логов: /claim <код>
    m = re.match(r"^/claim\s+(\w+)$", text)
    code = db.get_meta("claim_code") or ""
    if m and code and not db.owner_ids() and secrets.compare_digest(m[1], code):
        db.add_user(user.id, user_name(user), "owner")
        db.set_meta("claim_code", "")
        await set_owner_commands(context.bot, user.id)
        await update.message.reply_text("👑 Готово, теперь ты владелец бота. Нажми /start.")
        raise ApplicationHandlerStop

    await request_access(update, context, user)
    raise ApplicationHandlerStop


def users_view() -> tuple[str, InlineKeyboardMarkup]:
    users = db.list_users()
    lines = ["👥 <b>Доступ к боту</b>"]
    rows = []
    for u in users:
        name = u["name"] or f"ID {u['user_id']}"
        if u["role"] == "owner":
            lines.append(f"👑 {esc(name)} — владелец")
        else:
            lines.append(f"• {esc(name)} — <code>{u['user_id']}</code>, с {de_date(u['added_at'][:10])}")
            rows.append([B(f"❌ Убрать {name[:30]}", f"usr:del:{u['user_id']}")])
    lines.append("\nДобавить человека: ссылка-приглашение (одноразовая, на 24 часа) "
                 "или <code>/allow ID</code>.")
    rows.append([B("➕ Создать ссылку-приглашение", "usr:inv")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def make_invite(context: ContextTypes.DEFAULT_TYPE, owner_id: int) -> str:
    token = db.create_invite(owner_id)
    link = f"https://t.me/{context.bot.username}?start=inv_{token}"
    return (f"🔗 Ссылка-приглашение (одноразовая, действует 24 часа):\n{link}\n\n"
            "Отправь её человеку: он откроет бота, нажмёт «Start» и сразу получит доступ.")


async def on_access_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопки владельца: запросы доступа и список пользователей."""
    data, uid = query.data, query.from_user.id
    if not db.is_owner(uid):
        await query.answer("Только для владельца", show_alert=True)
        return
    await query.answer()
    if data.startswith("acc:"):
        _, action, target = data.split(":")
        target = int(target)
        name = context.bot_data.get("pending_names", {}).get(target, f"ID {target}")
        if action == "ok":
            db.add_user(target, name, "user", uid)
            context.bot_data.get("access_requests", {}).pop(target, None)
            try:
                await context.bot.send_message(target, "✅ Владелец открыл тебе доступ. Нажми /start.")
            except TelegramError:
                pass
            await edit(query, f"✅ Доступ открыт: <b>{esc(name)}</b>", None)
        else:
            context.bot_data.setdefault("access_requests", {})[target] = time.time() + DENY_SILENCE
            await edit(query, f"❌ Отклонено: <b>{esc(name)}</b>", None)
        return
    if data.startswith("usr:del:"):
        db.remove_user(int(data[8:]))
    elif data == "usr:inv":
        text = await make_invite(context, uid)
        return await edit(query, text, InlineKeyboardMarkup([[B("⬅️ К списку", "usr:list")]]))
    text, kb = users_view()
    await edit(query, text, kb)


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not db.is_owner(update.effective_user.id):
        await update.message.reply_text("Эта команда только для владельца.")
        return
    text, kb = users_view()
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not db.is_owner(update.effective_user.id):
        await update.message.reply_text("Эта команда только для владельца.")
        return
    await update.message.reply_text(await make_invite(context, update.effective_user.id))


async def cmd_allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not db.is_owner(update.effective_user.id):
        await update.message.reply_text("Эта команда только для владельца.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: <code>/allow 123456789</code> (Telegram ID человека).")
        return
    target = int(context.args[0])
    name = " ".join(context.args[1:]) or context.bot_data.get("pending_names", {}).get(target, "")
    db.add_user(target, name, "user", update.effective_user.id)
    await update.message.reply_text(f"✅ Добавил ID <code>{target}</code>. Пусть напишет боту /start.")


async def cmd_deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not db.is_owner(update.effective_user.id):
        await update.message.reply_text("Эта команда только для владельца.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: <code>/deny 123456789</code>.")
        return
    removed = db.remove_user(int(context.args[0]))
    await update.message.reply_text("Убрал из списка." if removed else "Такого пользователя нет (или это владелец).")


# =================================================== загрузка и уведомления


def all_terms() -> list[str]:
    """Профессии всех подписчиков — по ним идёт поиск в API."""
    terms: list[str] = []
    for chat in db.get_chats():
        terms += [t for t in chat["settings"]["terms"] if t not in terms]
    return terms or list(DEFAULT_TERMS)


async def fetch_and_store(http: httpx.AsyncClient, terms: list[str]) -> tuple[int, list[dict], list[str]]:
    """Загружает вакансии по профессиям. Возвращает (найдено, новые, ошибки)."""
    now = db.now_str()
    fetched = set(json.loads(db.get_meta("terms_fetched") or "[]"))
    empty_db = db.job_count() == 0
    found, new, errors = 0, [], []
    for term in terms:
        try:
            rows = await api.fetch_term(http, term)
        except (httpx.HTTPError, api.ApiError) as err:
            log.error("Запрос «%s» не удался: %s", term, err)
            errors.append(term)
            continue
        found += len(rows)
        # первая загрузка профессии — это не «новые» вакансии, а вся база по ней
        initial = empty_db or term not in fetched
        new += [dict(r, is_initial=initial) for r in db.save_jobs(rows, now, initial)]
        fetched.add(term)
    db.set_meta("terms_fetched", json.dumps(sorted(fetched), ensure_ascii=False))
    return found, new, errors


def jobs_needing_details(limit: int, only: set[str] | None = None) -> list[str]:
    """Вакансии без описания, которые подходят хоть одному подписчику по базовым фильтрам."""
    chats = db.get_chats()
    if not chats:
        return []
    retry_before = (datetime.now() - timedelta(days=1)).strftime(db.FMT)
    todo = [
        j for j in db.active_jobs()
        if (only is None or j["refnr"] in only)
        and (j["details_status"] is None
             or (j["details_status"] == "fail" and (j["details_at"] or "") < retry_before))
        and any(matching.prefilter(j, c["settings"]) for c in chats)
    ]
    todo.sort(key=lambda j: j["veroeffentlicht"] or "", reverse=True)
    todo.sort(key=lambda j: j["is_initial"])  # сначала новые
    return [j["refnr"] for j in todo[:limit]]


async def enrich(http: httpx.AsyncClient, refnrs: list[str]) -> int:
    """Загружает описания вакансий и разбирает требования. Возвращает число успешных."""
    pause = db.get_meta("details_pause_until") or ""
    if pause > db.now_str():
        return 0
    done, fails_in_row = 0, 0
    for refnr in refnrs:
        now = db.now_str()
        try:
            info = await api.fetch_details(http, refnr)
        except httpx.HTTPError as err:
            log.warning("Детали %s: %s", refnr, err)
            info = None
        if info is None:
            db.save_details(refnr, None, "fail", now)
            fails_in_row += 1
            if fails_in_row >= 15:
                until = (datetime.now() + timedelta(hours=6)).strftime(db.FMT)
                db.set_meta("details_pause_until", until)
                log.error("Описания вакансий недоступны (15 ошибок подряд), пауза до %s", until)
                break
        else:
            info.update(matching.parse_requirements(info["beschreibung"]))
            db.save_details(refnr, info, "ok", now)
            done += 1
            fails_in_row = 0
        await asyncio.sleep(0.3)
    return done


async def run_check(bot) -> str:
    """Проверка: загрузка вакансий, описания для новых, уведомления."""
    async with LOCK:
        terms = all_terms()
        async with api.client() as http:
            found, new, errors = await fetch_and_store(http, terms)
            if len(errors) < len(terms):
                db.set_meta("last_check", db.now_str())
            fresh = {r["refnr"] for r in new if not r["is_initial"]}
            if fresh:
                todo = jobs_needing_details(len(fresh), only=fresh)
                await enrich(http, todo)
                db.mark_details_failed(list(fresh), db.now_str())  # чтобы не ждать их вечно
    sent = await notify_all(bot)
    summary = f"Найдено в API: {found}, новых: {len(fresh)}, отправлено уведомлений: {sent}."
    if errors:
        summary += "\n⚠️ Не удалось запросить: " + ", ".join(errors)
    log.info(summary.replace("\n", " "))
    return summary


async def notify_all(bot) -> int:
    """Каждому подписчику — одно сообщение со списком новых подходящих вакансий."""
    jobs = db.active_jobs()
    total = 0
    for chat in db.get_chats():
        chat_id, s = chat["chat_id"], chat["settings"]
        if not s["notify"]:
            continue
        seen = db.notified_set(chat_id)
        candidates = [
            j for j in jobs
            if not j["is_initial"] and j["first_seen"] >= chat["notify_since"]
            and j["details_status"] is not None and j["refnr"] not in seen
        ]
        if not candidates:
            continue
        matched = matching.match_jobs(candidates, s)
        matched_ids = {j["refnr"] for j, _, _ in matched}
        # неподходящие тоже помечаем, чтобы не рассматривать их повторно
        db.mark_notified(chat_id, [j["refnr"] for j in candidates if j["refnr"] not in matched_ids], 0)
        if not matched:
            continue
        batch = int(time.time())
        db.mark_notified(chat_id, list(matched_ids), batch)
        text, kb = render_list(matched, s, f"n{batch}", f"🆕 Новых подходящих вакансий: {len(matched)}")
        try:
            await bot.send_message(chat_id, text, reply_markup=kb)
            total += len(matched)
        except Forbidden:
            db.remove_chat(chat_id)
        except TelegramError as err:
            log.warning("Не удалось отправить в %s: %s", chat_id, err)
    return total


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not db.get_chats():
        log.info("Подписчиков нет — жду /start")
        return
    try:
        await run_check(context.bot)
    except Exception:
        log.exception("Ошибка плановой проверки")


async def scheduled_enrich(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Фоном подтягивает описания уже известных вакансий."""
    if LOCK.locked():
        return
    try:
        async with LOCK:
            todo = jobs_needing_details(ENRICH_BATCH)
            if not todo:
                return
            async with api.client() as http:
                done = await enrich(http, todo)
            log.info("Описания вакансий: загружено %s из %s, отраслей в базе: %s",
                     done, len(todo), len(db.branch_counts()))
            if api.DETAIL_KEYS and not context.bot_data.get("keys_logged"):
                context.bot_data["keys_logged"] = True
                log.info("Поля в описаниях вакансий: %s", ", ".join(sorted(api.DETAIL_KEYS)))
        await notify_all(context.bot)
    except Exception:
        log.exception("Ошибка загрузки описаний")


# ================================================================= список


def format_item(n: int, job: dict, badges: list[str]) -> str:
    title = job["titel"] or job["beruf"] or "Ausbildung"
    lines = [f"<b>{n}. {esc(title)}</b>"]
    where = " ".join(x for x in (job["plz"], job["ort"]) if x)
    if job["region"] and job["region"].lower() not in (job["ort"] or "").lower():
        where += f" ({job['region']})"
    sub = [f"🏢 {esc(job['arbeitgeber'])}" if job["arbeitgeber"] else "", f"📍 {esc(where)}" if where.strip() else ""]
    lines.append(" · ".join(x for x in sub if x))
    info = [f"🗓 с {de_date(job['eintrittsdatum'])}" if job["eintrittsdatum"] else ""] + [esc(b) for b in badges]
    lines.append(" · ".join(x for x in info if x))
    links = [f'<a href="{esc(JOB_URL.format(refnr=job["refnr"]))}">Открыть</a>']
    if job["externe_url"]:
        links.append(f'<a href="{esc(job["externe_url"])}">сайт фирмы</a>')
    if job["veroeffentlicht"]:
        links.append(f"опубл. {de_date(job['veroeffentlicht'])}")
    lines.append(" · ".join(links))
    return "\n".join(line for line in lines if line)


SORTS = {"s": ("⭐ Лучшие", "Сначала самые подходящие"),
         "d": ("📏 Ближе", "Сначала ближайшие"),
         "n": ("🆕 Новые", "Сначала новые")}


def pg(kind: str, flt: str, sort: str, page: int) -> str:
    """callback_data страницы списка: вид, фильтр по городу, сортировка, страница."""
    return f"pg:{kind}:{flt}:{sort}:{page}"


def apply_view(matched: list, s: dict, flt: str, sort: str, ort_opts: list[str]):
    """Фильтр по городу и сортировка поверх подходящих вакансий.

    flt: '' — все; c<i> — в радиусе i-го города из настроек; co<i> — только в самом городе;
    o<i> — место из экрана «По городам». Возвращает (вакансии, подпись, город или None, flt).
    """
    city, label = None, ""
    m = re.match(r"^(co|c|o)(\d+)$", flt)
    if m and m[1] in ("c", "co") and int(m[2]) < len(s["cities"]):
        city, only = s["cities"][int(m[2])], m[1] == "co"
        matched = [x for x in matched if matching.in_city(x[0], city, only)]
        label = f"📍 только {city['name']}" if only else f"📍 {city['name']} и до {city['radius']} км"
        # расстояние показываем до выбранного города
        matched = [
            (job, score, [b for b in badges if not b.startswith("📏")]
             + ([f"📏 {d:.0f} км от {city['name']}"] if (d := matching.city_distance(job, city)) is not None else []))
            for job, score, badges in matched
        ]
    elif m and m[1] == "o" and int(m[2]) < len(ort_opts):
        ort = ort_opts[int(m[2])]
        matched = [x for x in matched if (x[0]["ort"] or "") == ort]
        label = f"🏙 {ort}"
    else:
        flt = ""

    if sort == "d":
        refs = [city] if city else s["cities"]
        if refs:
            def dist(x):
                ds = [d for c in refs if (d := matching.city_distance(x[0], c)) is not None]
                return min(ds) if ds else float("inf")
            matched = sorted(matched, key=dist)
    elif sort == "n":
        matched = sorted(matched, key=lambda x: (x[0]["veroeffentlicht"] or "", x[0]["first_seen"] or ""), reverse=True)
    return matched, label, city, flt


def render_list(matched: list, s: dict, kind: str, title: str, flt: str = "", sort: str = "s",
                page: int = 0, ort_opts: list[str] | None = None):
    """Страница списка вакансий: фильтр по городам, сортировка, листание.

    kind: a — все подходящие, n<batch> — пачка новых из уведомления.
    """
    if sort not in SORTS or (sort == "d" and not s["cities"]):
        sort = "s"
    matched, label, city, flt = apply_view(matched, s, flt, sort, ort_opts or [])
    check = lambda on: "✅ " if on else ""  # noqa: E731
    header = title + (f" · {esc(label)}" if label else "")

    rows = []
    if matched:
        pages = (len(matched) + PAGE_SIZE - 1) // PAGE_SIZE
        page = max(0, min(page, pages - 1))
        start = page * PAGE_SIZE
        chunk = matched[start:start + PAGE_SIZE]
        parts = [f"{header}\n<i>{SORTS[sort][1]} · {start + 1}–{start + len(chunk)} из {len(matched)}</i>"]
        parts += [format_item(start + i + 1, job, badges) for i, (job, _, badges) in enumerate(chunk)]
        nav = []
        if page > 0:
            nav.append(B("◀️", pg(kind, flt, sort, page - 1)))
        nav.append(B(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(B("▶️", pg(kind, flt, sort, page + 1)))
        rows.append(nav)
        if pages > 3:
            jump = [B("⏮ В начало", pg(kind, flt, sort, 0))] if page > 0 else []
            if page + 5 < pages:
                jump.append(B("⏩ +5 стр.", pg(kind, flt, sort, page + 5)))
            if jump:
                rows.append(jump)
    else:
        hint = ("В этом городе подходящих вакансий нет — выбери другой или увеличь радиус."
                if label else "Ничего не подходит. Попробуй ослабить фильтры в настройках.")
        parts = [f"{header}\n\n{hint}"]

    # города из настроек — быстрый фильтр
    if s["cities"]:
        chips = [B(check(not flt) + "Все", pg(kind, "", sort, 0))]
        for i, c in enumerate(s["cities"]):
            active = flt in (f"c{i}", f"co{i}")
            chips.append(B(check(active) + c["name"][:16], pg(kind, f"c{i}", sort, 0)))
        rows += [chips[k:k + 3] for k in range(0, len(chips), 3)]
    if city:
        i = s["cities"].index(city)
        rows.append([
            B(check(flt == f"co{i}") + f"Только {city['name'][:14]}", pg(kind, f"co{i}", sort, 0)),
            B(check(flt == f"c{i}") + f"До {city['radius']} км", pg(kind, f"c{i}", sort, 0)),
        ])
    rows.append([B(check(sort == key) + name, pg(kind, flt, key, 0))
                 for key, (name, _) in SORTS.items() if key != "d" or s["cities"]])
    rows.append([B("🗺 По городам", f"oc:{kind}"), B("⚙️ Настройки", "st:main")])
    return "\n\n".join(parts), InlineKeyboardMarkup(rows)


def base_matches(chat_id: int, kind: str, s: dict) -> tuple[list, str]:
    if kind.startswith("n") and kind[1:].isdigit():
        jobs = db.jobs_by_refnrs(db.batch_refnrs(chat_id, int(kind[1:])))
        return matching.match_jobs(jobs, s), "🆕 Новые вакансии"
    return matching.match_jobs(db.active_jobs(), s), "📋 Подходящие вакансии"


def list_for(chat_id: int, kind: str, page: int = 0, flt: str = "", sort: str = "s", context=None):
    s = db.get_settings(chat_id)
    matched, title = base_matches(chat_id, kind, s)
    ort_opts = context.chat_data.get("ort_opts", []) if context else []
    return render_list(matched, s, kind, title, flt, sort, page, ort_opts)


def cities_overview(chat_id: int, kind: str, context) -> tuple[str, InlineKeyboardMarkup]:
    """Экран «По городам»: сколько подходящих вакансий где, с кнопками перехода."""
    s = db.get_settings(chat_id)
    matched, _ = base_matches(chat_id, kind, s)
    lines = ["🗺 <b>Вакансии по городам</b>", f"Всего подходит: {len(matched)}"]
    rows = []
    if s["cities"]:
        lines.append("\n<b>Твои города:</b>")
        for i, c in enumerate(s["cities"]):
            only = sum(1 for job, _, _ in matched if matching.in_city(job, c, True))
            near = sum(1 for job, _, _ in matched if matching.in_city(job, c, False))
            lines.append(f"📍 {esc(c['name'])}: в самом городе {only}, до {c['radius']} км — {near}")
            rows.append([
                B(f"📍 {c['name'][:14]} ({only})", pg(kind, f"co{i}", "s", 0)),
                B(f"до {c['radius']} км ({near})", pg(kind, f"c{i}", "d", 0)),
            ])
    counts: dict[str, int] = {}
    for job, _, _ in matched:
        if job["ort"]:
            counts[job["ort"]] = counts.get(job["ort"], 0) + 1
    top = sorted(counts.items(), key=lambda x: -x[1])[:12]
    context.chat_data["ort_opts"] = [ort for ort, _ in top]
    if top:
        lines.append("\n<b>Где больше всего подходящих вакансий:</b>")
        buttons = [B(f"{ort[:20]} ({n})", pg(kind, f"o{i}", "s", 0)) for i, (ort, n) in enumerate(top)]
        rows += [buttons[k:k + 2] for k in range(0, len(buttons), 2)]
    rows.append([B("➕ Добавить свой город", "ct:addl")])
    rows.append([B("⬅️ К списку", pg(kind, "", "s", 0))])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# ============================================================== настройки


def _short(items: list[str], empty: str, limit: int = 4) -> str:
    if not items:
        return empty
    text = ", ".join(items[:limit])
    return text + (f" и ещё {len(items) - limit}" if len(items) > limit else "")


def settings_text(s: dict) -> str:
    cities = [f"{c['name']} ({c['radius']} км)" for c in s["cities"]]
    de = s["de_level"]
    if s["strict"]:
        de += ", строго"
    if s["hide_unknown"]:
        de += ", без уровня скрывать"
    return "\n".join([
        f"🔎 <b>Профессии:</b> {esc(_short(s['terms'], '— (ничего не ищу)'))}",
        f"🗺 <b>Земли:</b> {esc(_short(s['laender'], 'все'))}",
        f"🏙 <b>Города:</b> {esc(_short(cities, '—'))}",
        f"🏢 <b>Отрасли:</b> {esc(_short(s['branchen'], 'все', 2))}",
        f"🚫 <b>Исключения:</b> {esc(_short(s['exclude'], '—'))}",
        f"🇩🇪 <b>Мой немецкий:</b> {de}",
        f"🗓 <b>Начало:</b> {'с ' + de_date(s['min_start']) if s['min_start'] else 'любое'}",
        f"🎓 <b>Duales Studium:</b> {'показывать' if s['dual'] else 'скрывать'}",
        f"🏭 <b>Zeitarbeit и агентства:</b> {'скрывать' if s['no_zeitarbeit'] else 'показывать'}",
        f"🔔 <b>Уведомления:</b> {'вкл' if s['notify'] else 'выкл'}",
    ])


PROMPTS = {
    "term": ("terms", "Напиши профессию так, как её ищут на сайте Arbeitsagentur.\n"
                      "Например: <code>Mechatroniker</code> или <code>Kaufmann für Büromanagement</code>."),
    "city": ("cities", "Напиши город и, если нужно, радиус в км.\n"
                       "Например: <code>Cottbus 50</code> или <code>Leipzig</code> (по умолчанию 50 км)."),
    "excl": ("excl", "Напиши слова через запятую. Вакансии, где они есть в названии, профессии "
                     "или названии фирмы, будут скрыты.\nНапример: <code>Duales Studium, Zeitarbeit</code>."),
    "start": ("start", "С какой даты начало тебе подходит? Например: <code>2027</code>, "
                       "<code>08.2027</code> или <code>01.08.2027</code>. Сбросить: <code>-</code>."),
}


def menu(section: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE, s: dict | None = None):
    """Текст и кнопки раздела настроек."""
    s = s or db.get_settings(chat_id)
    back = [B("⬅️ Назад", "st:main")]
    check = lambda on: "✅ " if on else ""  # noqa: E731

    if section == "terms":
        rows = [[B(f"❌ {t}", f"tm:del:{i}")] for i, t in enumerate(s["terms"])]
        if len(s["terms"]) < MAX_TERMS:
            rows.append([B("➕ Добавить профессию", "tm:add")])
        text = ("🔎 <b>Профессии</b>\nБот ищет вакансии Ausbildung по этим названиям по всей Германии. "
                "Нажми на профессию, чтобы убрать её.")
        return text, InlineKeyboardMarkup(rows + [back])

    if section == "laender":
        names = matching.LAENDER
        rows = [
            [B(check(n in s["laender"]) + n, f"ld:{i}") for i, n in list(enumerate(names))[k:k + 2]]
            for k in range(0, len(names), 2)
        ]
        rows.append([B(check(not s["laender"]) + "Вся Германия", "ld:all")])
        text = ("🗺 <b>Федеральные земли</b>\nОтметь нужные. Если выбраны и земли, и города, "
                "подходят вакансии из любой выбранной земли или в радиусе любого города.")
        return text, InlineKeyboardMarkup(rows + [back])

    if section == "cities":
        rows = [[B(f"❌ {c['name']} ({c['radius']} км)", f"ct:del:{i}")] for i, c in enumerate(s["cities"])]
        rows.append([B("➕ Добавить город", "ct:add")])
        text = ("🏙 <b>Города</b>\nВакансии в радиусе от выбранных городов. Нажми на город, чтобы убрать его.\n\n"
                "Если выбраны ещё и земли, в список попадают и вакансии из этих земель. Чтобы смотреть "
                "только один город, нажми на его кнопку над списком вакансий или открой «🗺 По городам».")
        return text, InlineKeyboardMarkup(rows + [back])

    if section == "branchen":
        options = db.branch_counts()
        names = [n for n, _ in options]
        names += [n for n in s["branchen"] if n not in names]
        context.chat_data["br_opts"] = names
        counts = dict(options)
        rows = [[B(f"{check(n in s['branchen'])}{n[:40]} ({counts.get(n, 0)})", f"br:{i}")]
                for i, n in enumerate(names)]
        rows.append([B(check(not s["branchen"]) + "Все отрасли", "br:all")])
        text = ("🏢 <b>Отрасли работодателя</b>\nЧем занимается фирма, по данным Arbeitsagentur. "
                "В скобках — число активных вакансий.")
        if not options:
            text += "\n\n⏳ Данные об отраслях ещё собираются из описаний вакансий, загляни позже."
        else:
            text += "\nЕсли выбрать отрасли, вакансии с ещё не загруженным описанием скрываются."
        return text, InlineKeyboardMarkup(rows + [back])

    if section == "excl":
        rows = [[B(f"❌ {w}", f"ex:del:{i}")] for i, w in enumerate(s["exclude"])]
        rows.append([B("➕ Добавить слова", "ex:add")])
        text = "🚫 <b>Исключения</b>\nСкрываю вакансии с этими словами в названии, профессии или фирме."
        return text, InlineKeyboardMarkup(rows + [back])

    if section == "de":
        levels = [B(check(lv == s["de_level"]) + lv, f"de:{lv}") for lv in matching.LEVELS[1:]]
        rows = [
            levels,
            [B(check(s["strict"]) + "Скрывать «sehr gute / fließend»", "tg:strict")],
            [B(check(s["hide_unknown"]) + "Скрывать, если уровень не указан", "tg:unk")],
        ]
        text = ("🇩🇪 <b>Мой немецкий</b>\nБот читает описание каждой вакансии и скрывает те, где "
                "требуют уровень выше твоего (например, C1 или «Muttersprache»).\n\n"
                "• «sehr gute / fließende Deutschkenntnisse» — обычно это C1, но не всегда. "
                "По умолчанию такие вакансии показываются с пометкой ⚠️.\n"
                "• В большинстве объявлений уровень не указан, они помечены «не указан».")
        return text, InlineKeyboardMarkup(rows + [back])

    if section == "start":
        year = datetime.now().year
        rows = [
            [B(check(s["min_start"] == f"{y}-01-01") + f"с {y}", f"ms:{y}-01-01") for y in (year, year + 1)],
            [B(check(s["min_start"] == f"{year + 1}-08-01") + f"с 01.08.{year + 1}", f"ms:{year + 1}-08-01")],
            [B("✏️ Своя дата", "ms:custom"), B(check(not s["min_start"]) + "Любое", "ms:")],
        ]
        text = "🗓 <b>Начало Ausbildung</b>\nСкрываю вакансии, которые начинаются раньше выбранной даты."
        return text, InlineKeyboardMarkup(rows + [back])

    # главное меню
    count = len(matching.match_jobs(db.active_jobs(), s))
    rows = [
        [B("🔎 Профессии", "st:terms"), B("🗺 Земли", "st:laender")],
        [B("🏙 Города", "st:cities"), B("🏢 Отрасли", "st:branchen")],
        [B("🚫 Исключения", "st:excl"), B("🇩🇪 Немецкий", "st:de")],
        [B("🗓 Начало", "st:start"), B(f"🎓 Duales Studium: {'да' if s['dual'] else 'нет'}", "tg:dual")],
        [B(f"🏭 Zeitarbeit: {'скрыта' if s['no_zeitarbeit'] else 'да'}", "tg:zeit"),
         B(f"🔔 Уведомления: {'вкл' if s['notify'] else 'выкл'}", "tg:notify")],
        [B(f"📋 Показать подходящие ({count})", "ls")],
    ]
    text = f"⚙️ <b>Настройки поиска</b>\n\n{settings_text(s)}\n\nСейчас подходит: <b>{count}</b>"
    return text, InlineKeyboardMarkup(rows)


async def edit(query, text: str, kb) -> None:
    try:
        await query.edit_message_text(text, reply_markup=kb)
    except BadRequest as err:
        if "not modified" not in str(err).lower():
            raise


def _toggle(items: list, value) -> None:
    items.remove(value) if value in items else items.append(value)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    chat_id = query.message.chat.id
    if data.startswith(("acc:", "usr:")):
        return await on_access_callback(query, context)
    await query.answer()
    if data == "noop":
        return
    if data.startswith("pg:"):
        parts = data[3:].split(":")
        if len(parts) == 2:  # кнопки из сообщений до обновления
            kind, flt, sort, page = parts[0], "", "s", parts[1]
        else:
            kind, flt, sort, page = parts
        text, kb = list_for(chat_id, kind, int(page), flt, sort, context)
        return await edit(query, text, kb)
    if data == "ls":
        text, kb = list_for(chat_id, "a", context=context)
        return await edit(query, text, kb)
    if data.startswith("oc:"):
        text, kb = cities_overview(chat_id, data[3:], context)
        return await edit(query, text, kb)

    context.chat_data.pop("await", None)  # любая кнопка отменяет ожидание ввода
    context.chat_data.pop("after_city", None)
    s = db.get_settings(chat_id)
    section = "main"
    prompt = None

    if data.startswith("st:"):
        section = data[3:]
    elif data == "tg:dual":
        s["dual"] = not s["dual"]
    elif data == "tg:notify":
        s["notify"] = not s["notify"]
    elif data == "tg:zeit":
        s["no_zeitarbeit"] = not s["no_zeitarbeit"]
    elif data in ("tg:strict", "tg:unk"):
        key = "strict" if data == "tg:strict" else "hide_unknown"
        s[key] = not s[key]
        section = "de"
    elif data.startswith("de:"):
        s["de_level"], section = data[3:], "de"
    elif data == "ld:all":
        s["laender"], section = [], "laender"
    elif data.startswith("ld:"):
        _toggle(s["laender"], matching.LAENDER[int(data[3:])])
        section = "laender"
    elif data.startswith("tm:del:"):
        idx = int(data[7:])
        if idx < len(s["terms"]):
            s["terms"].pop(idx)
        section = "terms"
    elif data.startswith("ct:del:"):
        idx = int(data[7:])
        if idx < len(s["cities"]):
            s["cities"].pop(idx)
        section = "cities"
    elif data.startswith("ex:del:"):
        idx = int(data[7:])
        if idx < len(s["exclude"]):
            s["exclude"].pop(idx)
        section = "excl"
    elif data == "br:all":
        s["branchen"], section = [], "branchen"
    elif data.startswith("br:"):
        options = context.chat_data.get("br_opts") or []
        idx = int(data[3:])
        if idx < len(options):
            _toggle(s["branchen"], options[idx])
        section = "branchen"
    elif data == "ms:custom":
        prompt = "start"
    elif data.startswith("ms:"):
        s["min_start"], section = data[3:], "start"
    elif data in ("tm:add", "ct:add", "ex:add"):
        prompt = {"tm:add": "term", "ct:add": "city", "ex:add": "excl"}[data]
    elif data == "ct:addl":  # добавить город из экрана «По городам»
        prompt = "city"
        context.chat_data["after_city"] = "list"

    if prompt:
        context.chat_data["await"] = prompt
        back_section, text = PROMPTS[prompt]
        back = "oc:a" if context.chat_data.get("after_city") else f"st:{back_section}"
        kb = InlineKeyboardMarkup([[B("Отмена", back)]])
        return await edit(query, f"✏️ {text}", kb)

    db.save_settings(chat_id, s)
    text, kb = menu(section, chat_id, context, s)
    await edit(query, text, kb)


# ============================================================ ввод текста


def parse_start(text: str) -> str | None:
    """'2027', '08.2027', '01.08.2027', '2027-08-01', '-' -> ГГГГ-ММ-ДД или '' (сброс)."""
    text = text.strip()
    if text in ("-", "—", "сброс", "любое"):
        return ""
    for pattern, build in (
        (r"^(\d{4})$", lambda m: f"{m[1]}-01-01"),
        (r"^(\d{1,2})[./](\d{4})$", lambda m: f"{m[2]}-{int(m[1]):02d}-01"),
        (r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", lambda m: f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}"),
        (r"^(\d{4})-(\d{2})-(\d{2})$", lambda m: m[0]),
    ):
        m = re.match(pattern, text)
        if m:
            value = build(m)
            try:
                datetime.strptime(value, "%Y-%m-%d")
                return value
            except ValueError:
                return None
    return None


def parse_city(text: str) -> tuple[str, int]:
    m = re.match(r"^(.*?)[\s,]+(\d{1,3})\s*(km|км)?$", text.strip(), re.IGNORECASE)
    if m and m[1].strip():
        return m[1].strip(), max(1, min(300, int(m[2])))
    return text.strip(), 50


async def load_new_term(app: Application, chat_id: int, term: str) -> None:
    """Загружает вакансии по только что добавленной профессии."""
    try:
        async with LOCK:
            async with api.client() as http:
                found, _, errors = await fetch_and_store(http, [term])
        if errors:
            msg = f"⚠️ Не получилось загрузить вакансии по «{esc(term)}». Попробую при следующей проверке."
        else:
            msg = (f"Загрузил вакансии по «{esc(term)}»: {found}. Требования к немецкому "
                   "подтянутся в течение часа.")
        await app.bot.send_message(chat_id, msg, reply_markup=InlineKeyboardMarkup([[B("📋 Показать подходящие", "ls")]]))
    except Exception:
        log.exception("Ошибка загрузки профессии %s", term)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kind = context.chat_data.pop("await", None)
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    if not kind:
        await update.message.reply_text("Настройки поиска — /settings, список вакансий — /list.")
        return
    s = db.get_settings(chat_id)
    section = PROMPTS[kind][0]
    note = ""

    if kind == "term":
        term = text[:80]
        if term and term not in s["terms"]:
            s["terms"].append(term)
            fetched = set(json.loads(db.get_meta("terms_fetched") or "[]"))
            if term not in fetched:
                note = f"⏳ Загружаю вакансии по «{esc(term)}», пришлю сообщение, когда закончу.\n\n"
                context.application.create_task(load_new_term(context.application, chat_id, term))
    elif kind == "excl":
        for word in re.split(r"[,;\n]", text):
            word = word.strip()
            if word and word not in s["exclude"]:
                s["exclude"].append(word)
    elif kind == "city":
        name, radius = parse_city(text)
        place = None
        try:
            place = await api.geocode(name)
        except httpx.HTTPError as err:
            log.warning("Геокодер: %s", err)
        place = place or db.city_coords(name)
        if not place:
            context.chat_data["await"] = kind
            await update.message.reply_text(f"Не нашёл город «{esc(name)}». Попробуй написать иначе, например по-немецки.")
            return
        s["cities"].append({"name": place["name"], "lat": place["lat"], "lon": place["lon"], "radius": radius})
        if context.chat_data.pop("after_city", None):
            # пришли из списка вакансий — сразу показываем вакансии в этом городе
            db.save_settings(chat_id, s)
            text, kb = list_for(chat_id, "a", 0, f"c{len(s['cities']) - 1}", "d", context)
            await update.message.reply_text(text, reply_markup=kb)
            return
    elif kind == "start":
        value = parse_start(text)
        if value is None:
            context.chat_data["await"] = kind
            await update.message.reply_text("Не понял дату. Примеры: 2027, 08.2027, 01.08.2027 или «-» для сброса.")
            return
        s["min_start"] = value

    db.save_settings(chat_id, s)
    menu_text, kb = menu(section, chat_id, context, s)
    await update.message.reply_text(note + menu_text, reply_markup=kb)


# ================================================================ команды


HELP = (
    "/list — подходящие вакансии списком\n"
    "/settings — профессии, земли, города, отрасли, немецкий\n"
    "/check — проверить новые вакансии сейчас\n"
    "/stats — статистика\n"
    "/stop — отписаться"
)


async def first_check(app: Application, chat_id: int) -> None:
    try:
        await run_check(app.bot)
        count = len(matching.match_jobs(db.active_jobs(), db.get_settings(chat_id)))
        await app.bot.send_message(
            chat_id,
            f"Готово! Подходящих вакансий: <b>{count}</b>. Требования к немецкому подтягиваются "
            "в фоне, примерно за час.",
            reply_markup=InlineKeyboardMarkup([[B("📋 Показать", "ls"), B("⚙️ Настройки", "st:main")]]),
        )
    except Exception:
        log.exception("Ошибка первой проверки")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    db.add_chat(chat_id)
    db.set_user_name(update.effective_user.id, user_name(update.effective_user))
    s = db.get_settings(chat_id)
    text = (
        "Привет! Я ищу вакансии Ausbildung на сайте Bundesagentur für Arbeit, присылаю новые "
        "и отсеиваю те, где требуют немецкий выше твоего уровня.\n\n"
        f"{settings_text(s)}\n\n{HELP}"
    )
    kb = InlineKeyboardMarkup([[B("📋 Подходящие вакансии", "ls")], [B("⚙️ Настройки", "st:main")]])
    await update.message.reply_text(text, reply_markup=kb)
    if db.get_meta("last_check") is None:
        await update.message.reply_text("Запускаю первую проверку, это займёт около минуты…")
        context.application.create_task(first_check(context.application, chat_id))


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = list_for(update.effective_chat.id, "a", context=context)
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data.pop("await", None)
    if not db.is_subscribed(update.effective_chat.id):
        db.add_chat(update.effective_chat.id)
    text, kb = menu("main", update.effective_chat.id, context)
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Проверяю…")
    try:
        summary = await run_check(context.bot)
    except Exception as err:
        log.exception("Ошибка проверки")
        summary = f"Ошибка: {err}"
    await update.message.reply_text(esc(summary), reply_markup=InlineKeyboardMarkup([[B("📋 Показать подходящие", "ls")]]))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not db.get_meta("last_check"):
        await update.message.reply_text("Статистики пока нет — сначала /check.")
        return
    st = db.stats()
    s = db.get_settings(update.effective_chat.id)
    jobs = db.active_jobs()
    matched = matching.match_jobs(jobs, s)
    base = [j for j in jobs if matching.prefilter(j, s)]
    hidden_de = sum(1 for j in base if j["de_kind"] in ("exact", "hard")
                    and matching.LEVELS.index(j["de_level"]) > matching.LEVELS.index(s["de_level"]))
    lines = [
        "<b>Статистика</b>",
        f"Всего в базе: {st['total']}, активных: {st['active']}",
        f"Новых за 7 дней: {st['week']}",
        f"Описания загружены: {st['with_details']} из {st['active']}",
        f"Уровень немецкого указан в {st['with_level']} описаниях",
        "",
        f"Под твои фильтры: {len(base)}, из них скрыто из-за немецкого: {hidden_de}",
        f"Подходит: <b>{len(matched)}</b>",
    ]
    if st["lifetime"] is not None:
        lines.append(f"Закрытые вакансии висели в среднем {st['lifetime']:.0f} дн.")
    cities: dict[str, int] = {}
    firms: dict[str, int] = {}
    for job, _, _ in matched:
        if job["ort"]:
            cities[job["ort"]] = cities.get(job["ort"], 0) + 1
        if job["arbeitgeber"]:
            firms[job["arbeitgeber"]] = firms.get(job["arbeitgeber"], 0) + 1
    for title, data in (("Топ городов", cities), ("Топ работодателей", firms)):
        top = sorted(data.items(), key=lambda x: -x[1])[:5]
        if top:
            lines += ["", f"<b>{title} (подходящие):</b>"] + [f"• {esc(k)} — {v}" for k, v in top]
    lines += ["", f"Последняя проверка: {db.get_meta('last_check')}"]
    await update.message.reply_text("\n".join(lines))


OWNER_HELP = (
    "\n\n<b>Доступ (только для тебя как владельца):</b>\n"
    "/users — кто имеет доступ, убрать человека\n"
    "/invite — одноразовая ссылка-приглашение на 24 часа\n"
    "/allow ID — добавить по Telegram ID\n"
    "/deny ID — убрать по Telegram ID"
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    extra = OWNER_HELP if db.is_owner(update.effective_user.id) else ""
    await update.message.reply_text(HELP + extra)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db.remove_chat(update.effective_chat.id)
    await update.message.reply_text("Отписал. /start — подписаться снова.")


USER_COMMANDS = [
    BotCommand("list", "Подходящие вакансии"),
    BotCommand("settings", "Настройки поиска"),
    BotCommand("check", "Проверить сейчас"),
    BotCommand("stats", "Статистика"),
    BotCommand("help", "Команды"),
    BotCommand("stop", "Отписаться"),
]
OWNER_COMMANDS = [
    BotCommand("users", "Доступ: кто пользуется ботом"),
    BotCommand("invite", "Ссылка-приглашение"),
]


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(USER_COMMANDS)
    for owner in db.owner_ids():
        await set_owner_commands(app.bot, owner)
    greet = db.get_meta("owner_greet")
    if greet:
        db.set_meta("owner_greet", "")
        try:
            await app.bot.send_message(
                int(greet),
                "🔐 Бот закрыт: пользоваться им можешь только ты и те, кого ты добавишь. "
                "Ты — владелец.\n\n/users — список доступа\n/invite — пригласить человека по ссылке\n"
                "Незнакомцы увидят «нет доступа», а тебе придёт запрос с кнопками «Пустить / Отклонить».",
            )
        except TelegramError as err:
            log.warning("Не удалось поприветствовать владельца: %s", err)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Скопируй .env.example в .env и впиши токен.")
    db.init_db()
    setup_access()
    defaults = Defaults(parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True))
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .defaults(defaults)
        .connect_timeout(20)
        .read_timeout(20)
        .post_init(post_init)
        .build()
    )
    app.add_handler(TypeHandler(Update, gatekeeper), group=-1)  # whitelist до всех хендлеров
    for name, handler in (
        ("start", cmd_start), ("list", cmd_list), ("last", cmd_list), ("settings", cmd_settings),
        ("check", cmd_check), ("stats", cmd_stats), ("help", cmd_help), ("stop", cmd_stop),
        ("users", cmd_users), ("invite", cmd_invite), ("allow", cmd_allow), ("deny", cmd_deny),
    ):
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(scheduled_check, interval=CHECK_INTERVAL_HOURS * 3600, first=10)
    app.job_queue.run_repeating(scheduled_enrich, interval=ENRICH_INTERVAL_MIN * 60, first=90)
    log.info("Бот запущен. Профессии: %s", "; ".join(all_terms()))
    # -1: при старте бесконечно повторять подключение к Telegram, а не падать
    app.run_polling(bootstrap_retries=-1)


if __name__ == "__main__":
    main()
