"""SQLite-хранилище: вакансии, подписчики с их настройками, отправленные уведомления."""

import copy
import json
import sqlite3
from datetime import datetime, timedelta

from config import ACTIVE_WINDOW_HOURS, DB_PATH, DEFAULT_TERMS

FMT = "%Y-%m-%d %H:%M:%S"
SCHEMA_VERSION = "3"

DEFAULT_SETTINGS = {
    "terms": [],          # профессии (заполняется DEFAULT_TERMS)
    "laender": [],        # федеральные земли; пусто = все
    "cities": [],         # [{"name", "lat", "lon", "radius"}]
    "branchen": [],       # отрасли работодателя; пусто = все
    "exclude": [],        # слова-исключения в названии, профессии, фирме
    "min_start": "",      # начало не раньше ГГГГ-ММ-ДД
    "de_level": "B2",     # мой уровень немецкого
    "strict": False,      # скрывать вакансии с «sehr gute Deutschkenntnisse»
    "hide_unknown": False,  # скрывать, если уровень немецкого не указан
    "dual": True,         # показывать Duales Studium
    "notify": True,       # присылать уведомления о новых
}

JOB_COLUMNS = [
    ("refnr", "TEXT PRIMARY KEY"),
    ("titel", "TEXT DEFAULT ''"),
    ("beruf", "TEXT DEFAULT ''"),
    ("arbeitgeber", "TEXT DEFAULT ''"),
    ("plz", "TEXT DEFAULT ''"),
    ("ort", "TEXT DEFAULT ''"),
    ("region", "TEXT DEFAULT ''"),
    ("eintrittsdatum", "TEXT DEFAULT ''"),
    ("veroeffentlicht", "TEXT DEFAULT ''"),
    ("externe_url", "TEXT DEFAULT ''"),
    ("suchbegriff", "TEXT DEFAULT ''"),
    ("first_seen", "TEXT NOT NULL DEFAULT ''"),
    ("last_seen", "TEXT NOT NULL DEFAULT ''"),
    ("begriffe", "TEXT DEFAULT ''"),        # все профессии, по которым нашлась: |A|B|
    ("lat", "REAL"),
    ("lon", "REAL"),
    ("art", "TEXT DEFAULT ''"),
    ("is_initial", "INTEGER DEFAULT 0"),    # пришла при первой загрузке профессии — не «новая»
    ("details_status", "TEXT"),             # NULL — не загружали, ok, fail
    ("details_at", "TEXT DEFAULT ''"),
    ("beschreibung", "TEXT DEFAULT ''"),
    ("branchengruppe", "TEXT DEFAULT ''"),
    ("branche", "TEXT DEFAULT ''"),
    ("fluechtlinge", "INTEGER DEFAULT 0"),
    ("de_level", "TEXT DEFAULT ''"),        # требуемый уровень немецкого
    ("de_kind", "TEXT DEFAULT ''"),         # exact / hard / soft / ok / ''
    ("de_hint", "TEXT DEFAULT ''"),         # фраза из описания
    ("positive", "TEXT DEFAULT ''"),        # Sprachkurs, für Geflüchtete, international
]
CHAT_COLUMNS = [
    ("chat_id", "INTEGER PRIMARY KEY"),
    ("added_at", "TEXT NOT NULL DEFAULT ''"),
    ("settings", "TEXT DEFAULT ''"),
    ("notify_since", "TEXT DEFAULT ''"),
]
LIST_FIELDS = ", ".join(c for c, _ in JOB_COLUMNS if c != "beschreibung")


def now_str() -> str:
    return datetime.now().strftime(FMT)


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def _create(con, table: str, columns: list[tuple[str, str]]) -> None:
    con.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(f'{c} {t}' for c, t in columns)})")
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    for col, decl in columns:
        if col not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db() -> None:
    with db() as con:
        _create(con, "jobs", JOB_COLUMNS)
        _create(con, "chats", CHAT_COLUMNS)
        con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute(
            "CREATE TABLE IF NOT EXISTS notified (chat_id INTEGER, refnr TEXT, batch INTEGER, "
            "PRIMARY KEY (chat_id, refnr))"
        )
    schema = get_meta("schema")
    with db() as con:
        if schema in (None, "1"):
            # вакансии из версии до поддержки формата v6 были без дат и мест
            con.execute("DELETE FROM jobs")
            con.execute("DELETE FROM meta WHERE key='last_check'")
        elif schema == "2":
            # уже загруженное не считаем «новым», чтобы не прислать тысячи уведомлений
            con.execute(
                "UPDATE jobs SET is_initial=1, begriffe='|' || suchbegriff || '|' WHERE begriffe=''"
            )
        con.execute("UPDATE chats SET notify_since=? WHERE notify_since=''", (now_str(),))
    set_meta("schema", SCHEMA_VERSION)


# ------------------------------------------------------------------- meta


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


# ------------------------------------------------------- чаты и настройки


def _load_settings(raw: str) -> dict:
    s = copy.deepcopy(DEFAULT_SETTINGS)
    stored = json.loads(raw) if raw else {}
    s.update(stored)
    if "terms" not in stored:
        s["terms"] = list(DEFAULT_TERMS)
    return s


def add_chat(chat_id: int) -> bool:
    """Подписывает чат. True, если он новый."""
    now = now_str()
    s = _load_settings("")
    with db() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO chats (chat_id, added_at, settings, notify_since) VALUES (?, ?, ?, ?)",
            (chat_id, now, json.dumps(s, ensure_ascii=False), now),
        )
    return cur.rowcount > 0


def remove_chat(chat_id: int) -> None:
    with db() as con:
        con.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))


def get_chats() -> list[dict]:
    with db() as con:
        rows = con.execute("SELECT chat_id, settings, notify_since FROM chats").fetchall()
    return [
        {"chat_id": r["chat_id"], "settings": _load_settings(r["settings"]), "notify_since": r["notify_since"]}
        for r in rows
    ]


def is_subscribed(chat_id: int) -> bool:
    with db() as con:
        return con.execute("SELECT 1 FROM chats WHERE chat_id=?", (chat_id,)).fetchone() is not None


def get_settings(chat_id: int) -> dict:
    with db() as con:
        row = con.execute("SELECT settings FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
    return _load_settings(row["settings"] if row else "")


def save_settings(chat_id: int, settings: dict) -> None:
    with db() as con:
        con.execute(
            "UPDATE chats SET settings=? WHERE chat_id=?",
            (json.dumps(settings, ensure_ascii=False), chat_id),
        )


# --------------------------------------------------------------- вакансии


def job_count() -> int:
    with db() as con:
        return con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def save_jobs(rows: list[dict], now: str, initial: bool) -> list[dict]:
    """Добавляет новые вакансии, у известных обновляет данные и last_seen. Возвращает новые."""
    new = []
    with db() as con:
        for row in rows:
            params = {**row, "now": now, "tok": f"|{row['suchbegriff']}|",
                      "initial": 1 if initial else 0}
            cur = con.execute(
                "UPDATE jobs SET titel=:titel, beruf=:beruf, arbeitgeber=:arbeitgeber, plz=:plz, "
                "ort=:ort, region=:region, eintrittsdatum=:eintrittsdatum, "
                "veroeffentlicht=:veroeffentlicht, externe_url=:externe_url, lat=:lat, lon=:lon, "
                "art=:art, last_seen=:now, begriffe = CASE "
                "  WHEN instr(begriffe, :tok) > 0 THEN begriffe "
                "  WHEN begriffe = '' THEN :tok "
                "  ELSE begriffe || :suchbegriff || '|' END "
                "WHERE refnr=:refnr",
                params,
            )
            if cur.rowcount == 0:
                con.execute(
                    "INSERT INTO jobs (refnr, titel, beruf, arbeitgeber, plz, ort, region, "
                    "eintrittsdatum, veroeffentlicht, externe_url, suchbegriff, begriffe, lat, lon, "
                    "art, is_initial, first_seen, last_seen) VALUES (:refnr, :titel, :beruf, "
                    ":arbeitgeber, :plz, :ort, :region, :eintrittsdatum, :veroeffentlicht, "
                    ":externe_url, :suchbegriff, :tok, :lat, :lon, :art, :initial, :now, :now)",
                    params,
                )
                new.append(row)
    return new


def _active_cutoff(con) -> str:
    latest = con.execute("SELECT MAX(last_seen) FROM jobs").fetchone()[0]
    if not latest:
        return ""
    return (datetime.strptime(latest, FMT) - timedelta(hours=ACTIVE_WINDOW_HOURS)).strftime(FMT)


def active_jobs() -> list[dict]:
    """Вакансии, которые API отдавал в последних проверках (без текста описания)."""
    with db() as con:
        cutoff = _active_cutoff(con)
        rows = con.execute(f"SELECT {LIST_FIELDS} FROM jobs WHERE last_seen >= ?", (cutoff,))
        return [dict(r) for r in rows]


def jobs_by_refnrs(refnrs: list[str]) -> list[dict]:
    if not refnrs:
        return []
    with db() as con:
        marks = ",".join("?" * len(refnrs))
        rows = con.execute(f"SELECT {LIST_FIELDS} FROM jobs WHERE refnr IN ({marks})", refnrs)
        return [dict(r) for r in rows]


def save_details(refnr: str, info: dict | None, status: str, now: str) -> None:
    with db() as con:
        if info is None:
            con.execute(
                "UPDATE jobs SET details_status=?, details_at=? WHERE refnr=?", (status, now, refnr)
            )
            return
        con.execute(
            "UPDATE jobs SET details_status=:status, details_at=:now, beschreibung=:beschreibung, "
            "branchengruppe=:branchengruppe, branche=:branche, fluechtlinge=:fluechtlinge, "
            "de_level=:de_level, de_kind=:de_kind, de_hint=:de_hint, positive=:positive "
            "WHERE refnr=:refnr",
            {**info, "status": status, "now": now, "refnr": refnr},
        )


def mark_details_failed(refnrs: list[str], now: str) -> None:
    with db() as con:
        con.executemany(
            "UPDATE jobs SET details_status='fail', details_at=? WHERE refnr=? AND details_status IS NULL",
            [(now, r) for r in refnrs],
        )


def branch_counts(limit: int = 24) -> list[tuple[str, int]]:
    with db() as con:
        cutoff = _active_cutoff(con)
        rows = con.execute(
            "SELECT branchengruppe, COUNT(*) n FROM jobs WHERE last_seen >= ? AND branchengruppe != '' "
            "GROUP BY branchengruppe ORDER BY n DESC LIMIT ?",
            (cutoff, limit),
        )
        return [(r[0], r[1]) for r in rows]


def city_coords(name: str) -> dict | None:
    """Запасной геокодер: средние координаты вакансий в городе с таким названием."""
    with db() as con:
        row = con.execute(
            "SELECT AVG(lat), AVG(lon), COUNT(*) FROM jobs WHERE lat IS NOT NULL AND lower(ort) LIKE ?",
            (name.lower() + "%",),
        ).fetchone()
    if not row or not row[2]:
        return None
    return {"name": name, "lat": row[0], "lon": row[1]}


# ------------------------------------------------------------ уведомления


def notified_set(chat_id: int) -> set[str]:
    with db() as con:
        return {r[0] for r in con.execute("SELECT refnr FROM notified WHERE chat_id=?", (chat_id,))}


def mark_notified(chat_id: int, refnrs: list[str], batch: int) -> None:
    with db() as con:
        con.executemany(
            "INSERT OR IGNORE INTO notified (chat_id, refnr, batch) VALUES (?, ?, ?)",
            [(chat_id, r, batch) for r in refnrs],
        )


def batch_refnrs(chat_id: int, batch: int) -> list[str]:
    with db() as con:
        rows = con.execute("SELECT refnr FROM notified WHERE chat_id=? AND batch=?", (chat_id, batch))
        return [r[0] for r in rows]


# ------------------------------------------------------------- статистика


def stats() -> dict:
    with db() as con:
        cutoff = _active_cutoff(con)
        week_ago = (datetime.now() - timedelta(days=7)).strftime(FMT)
        one = lambda sql, *p: con.execute(sql, p).fetchone()[0]  # noqa: E731
        return {
            "total": one("SELECT COUNT(*) FROM jobs"),
            "active": one("SELECT COUNT(*) FROM jobs WHERE last_seen >= ?", cutoff),
            "week": one("SELECT COUNT(*) FROM jobs WHERE first_seen >= ? AND is_initial=0", week_ago),
            "with_details": one(
                "SELECT COUNT(*) FROM jobs WHERE last_seen >= ? AND details_status='ok'", cutoff
            ),
            "with_level": one(
                "SELECT COUNT(*) FROM jobs WHERE last_seen >= ? AND details_status='ok' AND de_kind != ''",
                cutoff,
            ),
            "lifetime": one(
                "SELECT AVG(julianday(last_seen) - julianday(COALESCE(NULLIF(veroeffentlicht, ''), "
                "first_seen))) FROM jobs WHERE last_seen < ?",
                cutoff,
            ),
        }
