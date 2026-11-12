"""Фильтры вакансий, разбор требований к немецкому и оценка «насколько подходит»."""

import math
import re
from datetime import date, timedelta

LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]

LAENDER = [
    "Baden-Württemberg", "Bayern", "Berlin", "Brandenburg", "Bremen", "Hamburg",
    "Hessen", "Mecklenburg-Vorpommern", "Niedersachsen", "Nordrhein-Westfalen",
    "Rheinland-Pfalz", "Saarland", "Sachsen", "Sachsen-Anhalt", "Schleswig-Holstein",
    "Thüringen",
]

# ------------------------------------------------- требования к немецкому

_LEVEL_RE = re.compile(r"(?<![a-z0-9])([abc])\s?[-–]?\s?([12])(?![a-z0-9])")
_DE_RE = re.compile(r"deutsch(?!land)")
_DE_WORD_RE = re.compile(r"deutsch(?!land)[\w-]*")
_OTHER_LANG_RE = re.compile(
    r"englisch|english|französisch|franzoesisch|spanisch|polnisch|russisch|türkisch|"
    r"tuerkisch|italienisch|arabisch|ukrainisch|niederländisch|chinesisch"
)
# названия компаний со словом «Deutsche», которые не про язык
_COMPANY_RE = re.compile(
    r"deutsche[nr]? (bahn|telekom|post|bank|bundesbahn|rentenversicherung|bundesbank|"
    r"lufthansa|börse|vermögensberatung|bildung|angestellten)"
)
_HARD = [
    (re.compile(r"muttersprach"), "C2"),
    (re.compile(r"verhandlungssicher"), "C1"),
]
_SOFT_RE = re.compile(
    r"fließend|fliessend|sehr gute?[nrs]?|ausgezeichnete?[nrs]?|exzellente?[nrs]?|"
    r"perfekte?[nrs]?|einwandfreie?[nrs]?|hervorragende?[nrs]?|sehr sicher"
)
_OK_RE = re.compile(r"(?<![a-zäöüß])(gute?[nrs]?|solide[nrs]?|grundkenntnisse|ausreichende?[nrs]?)(?![a-zäöüß])")
_POSITIVE = [
    (re.compile(r"sprachkurs|deutschkurs|sprachförder|sprachfoerder|sprachunterricht"), "Sprachkurs"),
    (re.compile(r"geflüchtet|gefluechtet|flüchtling|fluechtling"), "для беженцев"),
    (re.compile(
        r"internationale[nr]? (bewerb|azubi|talent|fachkr)|aus dem ausland|ausländische|"
        r"migrationshintergrund|alle nationalitäten|jeder herkunft"
    ), "international"),
]


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).lower()
    return _COMPANY_RE.sub("firma", text)


def _near_german(text: str, start: int, end: int, window: int) -> bool:
    """Относится ли фраза на позиции start..end к немецкому, а не к другому языку."""
    lo, hi = max(0, start - window), min(len(text), end + window)
    seg, pos = text[lo:hi], start - lo
    de = [m.start() for m in _DE_RE.finditer(seg)]
    if not de:
        return False
    other = [m.start() for m in _OTHER_LANG_RE.finditer(seg)]
    if not other:
        return True
    return min(abs(p - pos) for p in de) <= min(abs(p - pos) for p in other)


def _level_is_german(text: str, start: int, end: int) -> bool:
    """К какому языку относится уровень (B2, C1…) на позиции start..end.

    Берём язык, упомянутый ближе всего перед уровнем («Deutschkenntnisse auf B2»,
    «Englisch (B2) und Deutsch» -> английский); если перед ним языка нет —
    первый язык сразу после («B2-Deutschkenntnisse»).
    """
    before = text[max(0, start - 70):start]
    mentions = [(m.start(), "de") for m in _DE_RE.finditer(before)]
    mentions += [(m.start(), "other") for m in _OTHER_LANG_RE.finditer(before)]
    if mentions:
        return max(mentions)[1] == "de"
    after = text[end:end + 30]
    mentions = [(m.start(), "de") for m in _DE_RE.finditer(after)]
    mentions += [(m.start(), "other") for m in _OTHER_LANG_RE.finditer(after)]
    return bool(mentions) and min(mentions)[1] == "de"


def _hint(text: str, start: int, end: int) -> str:
    """Короткая цитата: ключевая фраза + ближайшее слово на «deutsch»."""
    lo, hi = max(0, start - 45), min(len(text), end + 45)
    words = [(m.start() + lo, m.end() + lo) for m in _DE_WORD_RE.finditer(text[lo:hi])]
    if not words:
        return text[start:end]
    ws, we = min(words, key=lambda w: abs(w[0] - start))
    phrase = text[min(start, ws):max(end, we)].strip(" ,.;:()")
    return phrase if len(phrase) <= 45 else phrase[:44] + "…"


def parse_requirements(text: str) -> dict:
    """Ищет в описании требования к немецкому и плюсы для иностранцев.

    de_kind: exact — указан уровень (B2, C1…); hard — «Muttersprache» (C2),
    «verhandlungssicher» (C1); soft — «sehr gute / fließende Deutschkenntnisse»;
    ok — «gute Deutschkenntnisse»; '' — не указано.
    """
    t = _clean(text)
    res = {"de_level": "", "de_kind": "", "de_hint": "", "positive": ""}

    found = []
    for m in _LEVEL_RE.finditer(t):
        if _level_is_german(t, m.start(), m.end()):
            found.append((LEVELS.index(f"{m.group(1)}{m.group(2)}".upper()), m))
    if found:
        idx, m = min(found, key=lambda f: f[0])  # «mindestens B2, idealerweise C1» -> B2
        res.update(de_level=LEVELS[idx], de_kind="exact", de_hint=_hint(t, m.start(), m.end()))
    else:
        for rx, level in _HARD:
            m = next((m for m in rx.finditer(t) if _near_german(t, m.start(), m.end(), 40)), None)
            if m:
                res.update(de_level=level, de_kind="hard", de_hint=_hint(t, m.start(), m.end()))
                break
    if not res["de_kind"]:
        for rx, kind, level in ((_SOFT_RE, "soft", "C1"), (_OK_RE, "ok", "B2")):
            m = next((m for m in rx.finditer(t) if _near_german(t, m.start(), m.end(), 30)), None)
            if m:
                res.update(de_level=level, de_kind=kind, de_hint=_hint(t, m.start(), m.end()))
                break

    res["positive"] = ", ".join(label for rx, label in _POSITIVE if rx.search(t))
    return res


# --------------------------------------------------------------- фильтры


def distance_km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def is_dual(job: dict) -> bool:
    text = f"{job.get('titel') or ''} {job.get('beruf') or ''}".lower()
    return "DUAL" in (job.get("art") or "").upper() or "duales studium" in text or "dual studieren" in text


def nearest_city(job: dict, cities: list[dict]) -> tuple[dict, float] | None:
    if job.get("lat") is None or job.get("lon") is None or not cities:
        return None
    return min(
        ((c, distance_km(job["lat"], job["lon"], c["lat"], c["lon"])) for c in cities),
        key=lambda x: x[1],
    )


def prefilter(job: dict, s: dict) -> bool:
    """Фильтры, для которых не нужно описание вакансии."""
    if not s["terms"] or not any(f"|{t}|" in (job.get("begriffe") or "") for t in s["terms"]):
        return False
    if not s["dual"] and is_dual(job):
        return False
    start = job.get("eintrittsdatum") or ""
    if s["min_start"] and start and start < s["min_start"]:
        return False
    if s["exclude"]:
        text = f"{job.get('titel') or ''} {job.get('beruf') or ''} {job.get('arbeitgeber') or ''}".lower()
        if any(word.lower() in text for word in s["exclude"]):
            return False
    if s["laender"] or s["cities"]:
        in_land = (job.get("region") or "") in s["laender"]
        near = nearest_city(job, s["cities"])
        in_city = near is not None and near[1] <= near[0]["radius"]
        if not (in_land or in_city):
            return False
    return True


def evaluate(job: dict, s: dict) -> tuple[int, list[str]] | None:
    """Оценка по требованиям. None — не подходит; иначе (баллы, значки)."""
    if s["branchen"] and (job.get("branchengruppe") or "") not in s["branchen"]:
        return None
    if s.get("no_zeitarbeit") and job.get("zeitarbeit"):
        return None
    score, badges = 0, []
    user = LEVELS.index(s["de_level"])
    kind, level = job.get("de_kind") or "", job.get("de_level") or ""
    hint = job.get("de_hint") or ""
    pending = job.get("details_status") is None

    if kind in ("exact", "hard"):
        if LEVELS.index(level) > user:
            return None
        badges.append(f"🇩🇪 {level} ✅")
        score += 3 if kind == "exact" else 2
    elif kind == "soft":
        if user >= LEVELS.index("C1"):
            badges.append(f"🇩🇪 «{hint}» ✅")
            score += 1
        elif s["strict"]:
            return None
        else:
            badges.append(f"🇩🇪 «{hint}» ⚠️")
            score -= 2
    elif kind == "ok":
        ok = user >= LEVELS.index("B2")
        badges.append(f"🇩🇪 «{hint}» {'✅' if ok else '⚠️'}")
        score += 1 if ok else -1
    elif pending:
        badges.append("🇩🇪 ⏳")
    else:
        if s["hide_unknown"]:
            return None
        badges.append("🇩🇪 не указан")

    if job.get("positive") or job.get("fluechtlinge"):
        badges.append("🤝 " + (job.get("positive") or "для беженцев"))
        score += 1
    published = job.get("veroeffentlicht") or ""
    if published and published >= (date.today() - timedelta(days=7)).isoformat():
        score += 1
    near = nearest_city(job, s["cities"])
    if near:
        badges.append(f"📏 {near[1]:.0f} км от {near[0]['name']}")
    return score, badges


def match_jobs(jobs: list[dict], s: dict) -> list[tuple[dict, int, list[str]]]:
    """Подходящие вакансии: сначала по баллам, потом по дате публикации."""
    result = []
    for job in jobs:
        if not prefilter(job, s):
            continue
        rated = evaluate(job, s)
        if rated:
            result.append((job, rated[0], rated[1]))
    result.sort(key=lambda r: r[0].get("veroeffentlicht") or "", reverse=True)
    result.sort(key=lambda r: r[1], reverse=True)
    return result
