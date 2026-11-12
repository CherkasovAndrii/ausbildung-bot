"""Работа с Jobsuche API Bundesagentur für Arbeit и геокодером для городов."""

import asyncio
import base64
import json
import logging

import httpx

from config import MAX_PAGES

log = logging.getLogger("ausbildung-bot")

API_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
HEADERS = {"X-API-Key": "jobboerse-jobsuche", "User-Agent": "ausbildung-bot/2.0"}

# Эндпоинты в порядке предпочтения; первый рабочий переезжает в начало списка.
SEARCH_URLS = [
    f"{API_BASE}/pc/v6/jobs",
    f"{API_BASE}/pc/v4/app/jobs",
    f"{API_BASE}/pc/v4/jobs",
]
DETAIL_URLS = [
    f"{API_BASE}/pc/v4/jobdetails/{{code}}",
    f"{API_BASE}/pc/v3/jobdetails/{{code}}",
    f"{API_BASE}/pc/v6/jobdetails/{{code}}",
]
ANGEBOTSART_AUSBILDUNG = 4  # Ausbildung / Duales Studium
PAGE_SIZE = 100

_logged_samples: set[str] = set()
DETAIL_KEYS: set[str] = set()  # все поля, встреченные в описаниях (для диагностики)


class ApiError(Exception):
    """Все известные эндпоинты ответили 403/404."""


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=HEADERS, timeout=30)


async def _get(http: httpx.AsyncClient, urls: list[str], params=None, code=None):
    """GET по первому рабочему адресу из urls.

    403/404 -> пробует следующий адрес; сетевые ошибки и 5xx повторяет до трёх раз.
    Возвращает JSON или None, если все адреса ответили 403/404.
    """
    network_error: Exception | None = None
    for url in list(urls):
        full = url.format(code=code) if code else url
        for attempt in range(3):
            try:
                resp = await http.get(full, params=params)
                if resp.status_code in (403, 404):
                    log.debug("%s -> %s", full, resp.status_code)
                    break  # к следующему адресу
                resp.raise_for_status()
                data = resp.json()
                if url != urls[0]:
                    urls.remove(url)
                    urls.insert(0, url)
                    log.info("Рабочий эндпоинт: %s", url)
                return data
            except (httpx.TransportError, httpx.HTTPStatusError, ValueError) as err:
                network_error = err
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
    if network_error:
        raise network_error
    return None


# ------------------------------------------------------------ разбор полей


def _first(item: dict, *keys: str):
    """Первое непустое поле из списка возможных названий (v4 и v6 называют их по-разному)."""
    for key in keys:
        if item.get(key):
            return item[key]
    return None


def _text(value) -> str:
    """Строка из значения, которое бывает str, dict или list."""
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = _first(value, "bezeichnung", "name", "titel", "kurzBezeichnung") or ""
    return str(value or "").strip()


def _datum(value) -> str:
    """Дата ГГГГ-ММ-ДД из строки или из периода вида {"von": "..."} (v6)."""
    if isinstance(value, dict):
        value = value.get("von") or ""
    return str(value or "")[:10]


def region_name(value: str) -> str:
    """THUERINGEN / BADEN_WUERTTEMBERG (v6) -> Thüringen / Baden-Württemberg."""
    if not value.isupper():
        return value
    name = value.replace("_", "-").title()
    return name.replace("Wuerttemberg", "Württemberg").replace("Thueringen", "Thüringen")


def _sample(kind: str, data: dict, limit: int = 2500) -> None:
    """Один раз за запуск пишет в лог образец ответа — чтобы видеть формат API."""
    if kind not in _logged_samples:
        _logged_samples.add(kind)
        log.info("Образец %s: %s", kind, json.dumps(data, ensure_ascii=False)[:limit])


def normalize(item: dict, term: str) -> dict | None:
    """Вакансия из результатов поиска (v4 или v6) -> строка для таблицы jobs."""
    _sample("вакансии", item)
    refnr = _text(_first(item, "refnr", "referenznummer"))
    if not refnr:
        return None
    loc = _first(item, "arbeitsort", "arbeitsorte", "stellenlokationen") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    if not isinstance(loc, dict):
        loc = {"ort": str(loc)}
    coords = loc.get("koordinaten") or {}
    lat = loc.get("breite") or coords.get("lat")
    lon = loc.get("laenge") or coords.get("lon")
    addr = loc["adresse"] if isinstance(loc.get("adresse"), dict) else loc
    return {
        "refnr": refnr,
        "titel": _text(_first(item, "titel", "stellenangebotsTitel", "stellenbezeichnung")),
        "beruf": _text(_first(item, "beruf", "hauptberuf", "berufsbezeichnung")),
        "arbeitgeber": _text(_first(item, "arbeitgeber", "arbeitgeberName", "firma", "unternehmen")),
        "plz": str(addr["plz"]).zfill(5) if addr.get("plz") else "",
        "ort": _text(addr.get("ort")),
        "region": region_name(_text(addr.get("region"))),
        "eintrittsdatum": _datum(_first(item, "eintrittsdatum", "eintrittszeitraum")),
        "veroeffentlicht": _datum(_first(
            item, "aktuelleVeroeffentlichungsdatum", "veroeffentlichungszeitraum",
            "datumErsteVeroeffentlichung", "ersteVeroeffentlichungsdatum",
        )),
        "externe_url": _text(_first(item, "externeUrl", "externeURL")),
        "lat": float(lat) if lat else None,
        "lon": float(lon) if lon else None,
        "art": _text(_first(item, "ausbildungsart", "stellenangebotsart")),
        "suchbegriff": term,
    }


# ------------------------------------------------------------------ поиск


async def fetch_term(http: httpx.AsyncClient, term: str) -> list[dict]:
    """Все вакансии Ausbildung по одной профессии по всей Германии."""
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        params = {"angebotsart": ANGEBOTSART_AUSBILDUNG, "was": term, "page": page, "size": PAGE_SIZE}
        data = await _get(http, SEARCH_URLS, params=params)
        if data is None:
            raise ApiError("все эндпоинты поиска ответили 403/404")
        items = data.get("stellenangebote") or data.get("ergebnisliste") or []
        if data.get("facetten"):
            _sample("фасетов поиска", {"facetten": data["facetten"]}, 4000)
        rows += [r for r in (normalize(i, term) for i in items) if r]
        total = int(data.get("maxErgebnisse") or 0)
        if len(items) < PAGE_SIZE or (total and page * PAGE_SIZE >= total):
            break
        await asyncio.sleep(0.5)
    return rows


# ------------------------------------------------------------------ детали


async def fetch_details(http: httpx.AsyncClient, refnr: str) -> dict | None:
    """Описание вакансии и отрасль. None, если детали недоступны."""
    code = base64.b64encode(refnr.encode()).decode()
    data = await _get(http, DETAIL_URLS, code=code)
    if not data:
        return None
    _sample("деталей", {k: (str(v)[:200] if isinstance(v, str) else v) for k, v in data.items()}, 3000)
    text = _first(
        data, "stellenangebotsBeschreibung", "stellenbeschreibung", "beschreibung",
        "stellenangebotsbeschreibung",
    )
    DETAIL_KEYS.update(data)
    return {
        "beschreibung": str(text or ""),
        "branchengruppe": _text(_first(data, "branchengruppe", "branchenGruppe", "wirtschaftszweig")),
        "branche": _text(_first(data, "branche", "branchenbezeichnung")),
        "fluechtlinge": 1 if data.get("fuerFluechtlingeGeeignet") else 0,
        "zeitarbeit": 1 if data.get("istArbeitnehmerUeberlassung") or data.get("istPrivateArbeitsvermittlung") else 0,
    }


# --------------------------------------------------------------- геокодер


async def geocode(query: str) -> dict | None:
    """Координаты города в Германии через OpenStreetMap Nominatim."""
    headers = {"User-Agent": "ausbildung-bot/2.0 (Telegram bot, github.com/CherkasovAndrii/ausbildung-bot)"}
    params = {"q": query, "countrycodes": "de", "format": "jsonv2", "limit": 1, "accept-language": "de"}
    async with httpx.AsyncClient(headers=headers, timeout=15) as http:
        resp = await http.get("https://nominatim.openstreetmap.org/search", params=params)
        resp.raise_for_status()
        data = resp.json()
    if not data:
        return None
    hit = data[0]
    return {"name": hit.get("name") or query, "lat": float(hit["lat"]), "lon": float(hit["lon"])}
