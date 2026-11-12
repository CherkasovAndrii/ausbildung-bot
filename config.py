"""Настройки из переменных окружения (на Railway — вкладка Variables)."""

import os

from dotenv import load_dotenv

load_dotenv()


def _list(name: str, default: str = "") -> list[str]:
    """Значение вида 'a;b;c' -> ['a', 'b', 'c']."""
    return [x.strip() for x in os.getenv(name, default).split(";") if x.strip()]


BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Профессии по умолчанию для нового подписчика; дальше он меняет их в /settings
DEFAULT_TERMS = _list(
    "SEARCH_TERMS",
    "Fachinformatiker Systemintegration;"
    "Fachinformatiker Anwendungsentwicklung;"
    "Kaufmann für Digitalisierungsmanagement",
)

CHECK_INTERVAL_HOURS = float(os.getenv("CHECK_INTERVAL_HOURS", "6"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "20"))            # страниц по 100 на одну профессию
ENRICH_BATCH = int(os.getenv("ENRICH_BATCH", "300"))     # описаний вакансий за один заход
ENRICH_INTERVAL_MIN = float(os.getenv("ENRICH_INTERVAL_MIN", "10"))

# Вакансия считается активной, если API отдавал её за последние две проверки
ACTIVE_WINDOW_HOURS = CHECK_INTERVAL_HOURS * 2 + 1

# На Railway база лежит на подключённом Volume и переживает деплои
DB_PATH = os.getenv("DB_PATH") or os.path.join(
    os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ""), "jobs.db"
)
