import copy

import pytest

from db import DEFAULT_SETTINGS
from matching import evaluate, match_jobs, parse_requirements, prefilter


@pytest.mark.parametrize(
    "text, level, kind",
    [
        ("Du verfügst über Deutschkenntnisse auf B2-Niveau.", "B2", "exact"),
        ("Deutsch C1 erforderlich", "C1", "exact"),
        ("Deutschkenntnisse mindestens B2, idealerweise C1", "B2", "exact"),
        ("Deutsch auf Muttersprachniveau", "C2", "hard"),
        ("Deutsch als Muttersprache", "C2", "hard"),
        ("verhandlungssichere Deutschkenntnisse", "C1", "hard"),
        ("Sehr gute Deutschkenntnisse in Wort und Schrift", "C1", "soft"),
        ("Du sprichst fließend Deutsch", "C1", "soft"),
        ("Gute Deutsch- und Englischkenntnisse", "B2", "ok"),
        ("Englischkenntnisse (B2) und gute Deutschkenntnisse", "B2", "ok"),
        ("Englisch C1 wünschenswert", "", ""),
        ("Wir sind ein B2B-Unternehmen mit Standorten in ganz Deutschland.", "", ""),
        ("Ausbildung bei der Deutschen Bahn, Gebäude B1", "", ""),
        ("Spaß an Technik und Teamfähigkeit", "", ""),
    ],
)
def test_german_level(text, level, kind):
    res = parse_requirements(text)
    assert (res["de_level"], res["de_kind"]) == (level, kind)


def test_html_and_positive_signals():
    res = parse_requirements("<p>Wir bieten einen <b>Deutschkurs</b> und freuen uns über internationale Bewerber.</p>")
    assert "Sprachkurs" in res["positive"]
    assert "international" in res["positive"]


def test_hint_is_short_quote():
    res = parse_requirements("Voraussetzungen: sehr gute Deutschkenntnisse und Teamgeist")
    assert res["de_hint"] == "sehr gute deutschkenntnisse"


def settings(**kw):
    s = copy.deepcopy(DEFAULT_SETTINGS)
    s.update(terms=["Fachinformatiker Systemintegration"], **kw)
    return s


def job(**kw):
    j = {
        "refnr": "1", "titel": "Ausbildung FISI", "beruf": "Fachinformatiker/in", "arbeitgeber": "ACME",
        "region": "Brandenburg", "ort": "Cottbus", "lat": 51.76, "lon": 14.33, "eintrittsdatum": "2027-08-01",
        "veroeffentlicht": "2026-11-01", "begriffe": "|Fachinformatiker Systemintegration|", "art": "AUSBILDUNG",
        "details_status": "ok", "de_kind": "", "de_level": "", "de_hint": "", "positive": "", "fluechtlinge": 0,
        "branchengruppe": "IT, Computer, Telekommunikation",
    }
    j.update(kw)
    return j


def test_prefilter_location_and_terms():
    assert prefilter(job(), settings())
    assert not prefilter(job(begriffe="|Mechatroniker|"), settings())
    assert prefilter(job(), settings(laender=["Brandenburg"]))
    assert not prefilter(job(), settings(laender=["Bayern"]))
    berlin = {"name": "Berlin", "lat": 52.52, "lon": 13.40, "radius": 50}
    assert not prefilter(job(), settings(cities=[berlin]))           # Cottbus ~ 110 км
    assert prefilter(job(), settings(cities=[dict(berlin, radius=150)]))
    assert prefilter(job(), settings(laender=["Brandenburg"], cities=[berlin]))  # земля ИЛИ город


def test_prefilter_exclude_start_dual():
    assert not prefilter(job(arbeitgeber="Zeitarbeit GmbH"), settings(exclude=["zeitarbeit"]))
    assert not prefilter(job(eintrittsdatum="2026-09-01"), settings(min_start="2027-01-01"))
    assert not prefilter(job(art="DUALES_STUDIUM"), settings(dual=False))


def test_evaluate_german_levels():
    assert evaluate(job(de_kind="exact", de_level="C1"), settings()) is None   # нужен C1, у меня B2
    assert evaluate(job(de_kind="hard", de_level="C2"), settings()) is None
    assert evaluate(job(de_kind="exact", de_level="B2"), settings())[0] >= 3
    assert evaluate(job(de_kind="exact", de_level="C1"), settings(de_level="C1")) is not None
    soft = job(de_kind="soft", de_level="C1", de_hint="sehr gute deutschkenntnisse")
    assert evaluate(soft, settings()) is not None
    assert evaluate(soft, settings(strict=True)) is None
    assert evaluate(job(), settings(hide_unknown=True)) is None
    assert evaluate(job(details_status=None), settings(hide_unknown=True)) is not None  # ещё грузится


def test_branch_filter_and_ranking():
    assert evaluate(job(), settings(branchen=["Handel"])) is None
    jobs = [
        job(refnr="a"),
        job(refnr="b", de_kind="exact", de_level="B1"),
        job(refnr="c", de_kind="soft", de_level="C1", de_hint="fließend deutsch"),
        job(refnr="d", de_kind="exact", de_level="C1"),
    ]
    assert [j["refnr"] for j, _, _ in match_jobs(jobs, settings())] == ["b", "a", "c"]


@pytest.mark.parametrize(
    "text, level",
    [
        ("B2-Deutschkenntnisse sind Voraussetzung", "B2"),
        ("Englisch C1. Deutsch B2.", "B2"),
        ("Gute Englisch- und Deutschkenntnisse (B1)", "B1"),
        ("Deutsch (Niveau C 1)", "C1"),
    ],
)
def test_level_attached_to_right_language(text, level):
    assert parse_requirements(text)["de_level"] == level
