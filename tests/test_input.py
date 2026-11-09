"""Configuration, airports list and dates."""

import pytest

from conftest import write


def test_load_config_missing(gt, tmp_path):
    with pytest.raises(gt.TrafficError, match="Configuration file not found"):
        gt.load_config(tmp_path / "missing.ini")


def test_load_config_adds_missing_sections(gt, tmp_path):
    cfg = gt.load_config(write(tmp_path / "x.ini", "[general]\ntimezone = UTC\n"))
    for section in ("general", "opensky", "check", "graph"):
        assert cfg.has_section(section)


def test_load_airports_spanish_columns(gt, project):
    airports = gt.load_airports(project.parent / "airports.csv", "UTC")
    assert [a.icao for a in airports] == ["MMMX", "MMUN"]
    mx = airports[0]
    assert mx.name == "Ciudad de México"
    assert (mx.lat, mx.lon) == (19.4361, -99.0719)
    assert mx.timezone == "America/Mexico_City"
    assert mx.extra == {"ciudad_cercana": "CDMX"}


def test_load_airports_english_columns_and_default_tz(gt, tmp_path):
    path = write(
        tmp_path / "a.csv",
        """\
        icao,iata,name,lat,lon
        mmgl,gdl,Guadalajara,20.52,-103.31
        """,
    )
    (ap,) = gt.load_airports(path, "America/Mexico_City")
    assert (ap.icao, ap.iata, ap.name) == ("MMGL", "GDL", "Guadalajara")
    assert ap.timezone == "America/Mexico_City"
    assert ap.extra == {}


def test_load_airports_with_bom_and_blank_rows(gt, tmp_path):
    path = tmp_path / "a.csv"
    path.write_text("﻿designacion_icao,nombre_aeropuerto\nKSLN,Salina\n,\n", encoding="utf-8")
    airports = gt.load_airports(path, "UTC")
    assert [a.icao for a in airports] == ["KSLN"]
    assert airports[0].lat is None


@pytest.mark.parametrize(
    "content, message",
    [
        ("name,city\nX,Y\n", "ICAO"),
        ("icao,lat,lon\nKSLN,abc,1\n", "latitude/longitude"),
        ("icao\n", "contains no airports"),
    ],
)
def test_load_airports_errors(gt, tmp_path, content, message):
    path = write(tmp_path / "a.csv", content)
    with pytest.raises(gt.TrafficError, match=message):
        gt.load_airports(path, "UTC")


def test_load_airports_missing_file(gt, tmp_path):
    with pytest.raises(gt.TrafficError, match="Airports list not found"):
        gt.load_airports(tmp_path / "none.csv", "UTC")


def test_parse_date(gt):
    start = gt.parse_date("05-11-2026", "America/Mexico_City")
    end = gt.parse_date("05-11-2026", "America/Mexico_City", end_of_day=True)
    assert start.format("YYYY-MM-DD HH:mm") == "2026-11-05 00:00"
    assert end.format("YYYY-MM-DD HH:mm:ss") == "2026-11-05 23:59:59"
    assert str(start.tzinfo) == "America/Mexico_City"


@pytest.mark.parametrize("value", ["2026-11-05", "32-11-2026", "today", ""])
def test_parse_date_invalid(gt, value):
    with pytest.raises(gt.TrafficError, match="dd-mm-YYYY"):
        gt.parse_date(value, "UTC")


def test_parse_range(gt):
    assert gt.parse_range(None, None, "UTC") is None
    start, end = gt.parse_range("01-11-2026", "03-11-2026", "UTC")
    assert (start.day, end.day, end.hour) == (1, 3, 23)


def test_parse_range_single_date_is_that_day(gt):
    for args in (("02-11-2026", None), (None, "02-11-2026")):
        start, end = gt.parse_range(*args, "UTC")
        assert (start.day, end.day) == (2, 2)


def test_parse_range_inverted(gt):
    with pytest.raises(gt.TrafficError, match="cannot be later"):
        gt.parse_range("05-11-2026", "01-11-2026", "UTC")


def test_config_hex_colors_are_not_comments(gt, tmp_path):
    cfg = gt.load_config(
        write(tmp_path / "x.ini", "[graph]\nbar_color = #94a3b8\nmax_bar_color = #0000ff  ; comment\n")
    )
    assert cfg["graph"]["bar_color"] == "#94a3b8"
    assert cfg["graph"]["max_bar_color"] == "#0000ff"
