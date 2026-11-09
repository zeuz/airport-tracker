"""Traffic log and MRTG-style HTML generation."""

import re

import arrow
import pytest

TZ = "America/Mexico_City"


def row(gt, hour, icao, count, source="approach", tz=TZ):
    return gt._row(arrow.get(hour, tzinfo=tz), icao, count, source, arrow.get("2026-11-01T00:00:00+00:00"))


def test_read_log_missing_file(gt, tmp_path):
    assert gt.read_log(tmp_path / "no.csv") == []


def test_write_log_creates_dir_and_upserts(gt, tmp_path):
    path = tmp_path / "data" / "log.csv"
    gt.write_log(path, [row(gt, "2026-11-02T10:00", "MMMX", 5), row(gt, "2026-11-02T09:00", "MMMX", 3)])
    gt.write_log(path, [row(gt, "2026-11-02T10:00", "MMMX", 9)])  # same key: replaces
    gt.write_log(path, [row(gt, "2026-11-02T10:00", "MMMX", 4, source="arrivals")])  # another source

    rows = gt.read_log(path)
    assert [(r["_hour"].hour, r["count"], r["source"]) for r in rows] == [
        (9, 3, "approach"),
        (10, 9, "approach"),
        (10, 4, "arrivals"),
    ]
    assert path.read_text(encoding="utf-8").splitlines()[0] == "hour,icao,count,source,checked_at"
    assert not path.with_suffix(".csv.tmp").exists()


def test_write_log_same_instant_different_offset_is_same_key(gt, tmp_path):
    path = tmp_path / "log.csv"
    gt.write_log(path, [row(gt, "2026-11-02T10:00", "MMUN", 1, tz="America/Mexico_City")])
    gt.write_log(path, [row(gt, "2026-11-02T11:00", "MMUN", 2, tz="America/Cancun")])
    rows = gt.read_log(path)
    assert len(rows) == 1 and rows[0]["count"] == 2


def test_read_log_skips_bad_rows(gt, tmp_path):
    path = tmp_path / "log.csv"
    path.write_text(
        "hour,icao,count,source,checked_at\n"
        "2026-11-02T10:00-06:00,MMMX,5,approach,x\n"
        "basura,MMMX,5,approach,x\n"
        "2026-11-02T11:00-06:00,MMMX,no-num,approach,x\n",
        encoding="utf-8",
    )
    assert len(gt.read_log(path)) == 1


@pytest.mark.parametrize(
    "value, expected", [(0, 4), (1, 4), (3, 4), (4, 4), (5, 8), (7, 8), (9, 10), (37, 40), (62, 80), (120, 200)]
)
def test_nice_max(gt, value, expected):
    top = gt.nice_max(value)
    assert top == expected
    assert top >= value


def test_svg_bars_highlights_max_and_skips_empty(gt, config):
    hours = list(arrow.Arrow.range("hour", arrow.get("2026-11-02T00:00", tzinfo=TZ), limit=4))
    series = list(zip(hours, [3, None, 0, 7]))
    svg = gt.svg_bars(series, "HH", config["graph"])
    bars = re.findall(r'<rect [^>]*fill="(#[0-9a-f]+)"><title>', svg)
    assert bars == ["#00cc00", "#0000ff"]  # only 3 and 7 draw a bar; 7 is the max


def test_stats_table(gt):
    hours = list(arrow.Arrow.range("hour", arrow.get("2026-11-02T00:00", tzinfo=TZ), limit=4))
    html = gt.stats_table(list(zip(hours, [2, None, 4, None])), "hour")
    cells = re.findall(r"<td>([0-9.]+)</td>", html)
    assert cells == ["4", "3.0", "4", "6"]  # MAX, AVG, NOW, TOTAL
    assert "No data" in gt.stats_table([(hours[0], None)], "hour")


@pytest.fixture
def airports(gt, project):
    return gt.load_airports(project.parent / "airports.csv", TZ)


def sections(html):
    return re.findall(r"<h2>([^<]*)</h2>", html)


def test_generate_graph_total_first_and_sums(gt, config, airports, tmp_path):
    log = tmp_path / "log.csv"
    gt.write_log(
        log,
        [
            row(gt, "2026-11-02T10:00", "MMMX", 5),
            row(gt, "2026-11-02T11:00", "MMUN", 3, tz="America/Cancun"),  # = 10:00 in Mexico City
            row(gt, "2026-11-02T12:00", "MMMX", 2),
            row(gt, "2026-11-02T10:00", "MMMX", 50, source="arrivals"),  # not included in the total
        ],
    )
    out = tmp_path / "out" / "r.html"
    rng = gt.parse_range("02-11-2026", None, TZ)
    gt.generate_graph(config, airports, log, out, rng, TZ)
    html = out.read_text(encoding="utf-8")

    assert sections(html) == ["Total — all airports", "Ciudad de México (MMMX)", "Cancún (MMUN)"]
    total = html.split("<section")[1]
    assert "Sum of 2 airports" in total
    # Tables (MAX, AVG, NOW, TOTAL): hourly, this week (middle) and weekly (right).
    assert re.findall(r"<td>([0-9.]+)</td>", total) == [
        "8", "5.0", "2", "10",
        "10", "10.0", "10", "10",
        "10", "10.0", "10", "10",
    ]
    assert "Ciudad cercana: CDMX" in html
    assert "Actual arrivals" in html and "Weekly" not in html


def test_generate_graph_weekly_for_long_ranges(gt, config, airports, tmp_path):
    log = tmp_path / "log.csv"
    gt.write_log(log, [row(gt, f"2026-11-0{d}T10:00", "MMMX", d) for d in (1, 2, 3, 4)])
    out = tmp_path / "r.html"
    gt.generate_graph(config, airports, log, out, gt.parse_range("01-11-2026", "04-11-2026", TZ), TZ)
    html = out.read_text(encoding="utf-8")
    assert html.count("Weekly graph (1 day total)") == 2  # total + MMMX
    assert "Cancún" not in html  # no data, not drawn


def test_generate_graph_includes_icao_not_in_list(gt, config, airports, tmp_path):
    log = tmp_path / "log.csv"
    gt.write_log(log, [row(gt, "2026-11-02T10:00", "KSLN", 1)])
    out = tmp_path / "r.html"
    gt.generate_graph(config, airports, log, out, gt.parse_range("02-11-2026", None, TZ), TZ)
    assert sections(out.read_text(encoding="utf-8"))[-1] == "KSLN"


def test_generate_graph_errors(gt, config, airports, tmp_path):
    log = tmp_path / "log.csv"
    with pytest.raises(gt.TrafficError, match="empty"):
        gt.generate_graph(config, airports, log, tmp_path / "r.html", None, TZ)
    gt.write_log(log, [row(gt, "2026-11-02T10:00", "MMMX", 1)])
    with pytest.raises(gt.TrafficError, match="range"):
        gt.generate_graph(config, airports, log, tmp_path / "r.html", gt.parse_range("10-11-2026", None, TZ), TZ)


# --- [graph] info-html ---------------------------------------------------------


def test_load_info_html_fragment(gt, tmp_path):
    path = tmp_path / "info.html"
    path.write_text("﻿<h1>My report</h1><p>Project <b>X</b></p>\n", encoding="utf-8")
    assert gt.load_info_html(path) == "<h1>My report</h1><p>Project <b>X</b></p>"


def test_load_info_html_full_document_keeps_body_and_styles(gt, tmp_path):
    path = tmp_path / "info.html"
    path.write_text(
        "<!doctype html><html><head><title>t</title><style>.x{color:red}</style></head>"
        "<BODY class='a'>\n<h1 class='x'>Title</h1>\n</BODY></html>",
        encoding="utf-8",
    )
    result = gt.load_info_html(path)
    assert result == "<style>.x{color:red}</style>\n<h1 class='x'>Title</h1>"


def test_load_info_html_missing(gt, tmp_path):
    with pytest.raises(gt.TrafficError, match="info-html"):
        gt.load_info_html(tmp_path / "no.html")


def test_generate_graph_uses_info_html_instead_of_title(gt, config, airports, tmp_path):
    log = tmp_path / "log.csv"
    gt.write_log(log, [row(gt, "2026-11-02T10:00", "MMMX", 1)])
    out = tmp_path / "r.html"
    rng = gt.parse_range("02-11-2026", None, TZ)

    gt.generate_graph(config, airports, log, out, rng, TZ)
    assert "<h1>Test</h1>" in out.read_text(encoding="utf-8")

    gt.generate_graph(config, airports, log, out, rng, TZ, "<h1>Header & co.</h1>")
    html = out.read_text(encoding="utf-8")
    assert '<div class="info-html"><h1>Header & co.</h1></div>' in html  # unescaped
    assert "<h1>Test</h1>" not in html
    assert html.index("info-html\">") < html.index("Total — all airports")


# --- [graph] css-html ----------------------------------------------------------


def test_load_css_strips_style_tags(gt, tmp_path):
    path = tmp_path / "s.css"
    path.write_text("﻿  <style>\n  :root { --bg-color: #000; }\n  </style>\n", encoding="utf-8")
    assert gt.load_css(path) == ":root { --bg-color: #000; }"


def test_load_css_missing_warns_and_returns_none(gt, tmp_path, capsys):
    assert gt.load_css(tmp_path / "no.css") is None
    assert "css-html" in capsys.readouterr().err


def test_load_css_empty_is_none(gt, tmp_path):
    path = tmp_path / "s.css"
    path.write_text("<style></style>\n", encoding="utf-8")
    assert gt.load_css(path) is None


def test_generate_graph_embeds_css_after_own_styles(gt, config, airports, tmp_path):
    log = tmp_path / "log.csv"
    gt.write_log(log, [row(gt, "2026-11-02T10:00", "MMMX", 1)])
    out = tmp_path / "r.html"
    rng = gt.parse_range("02-11-2026", None, TZ)

    gt.generate_graph(config, airports, log, out, rng, TZ)
    html = out.read_text(encoding="utf-8")
    assert "/* [graph] css-html */" not in html
    assert '<div class="container report">' in html  # single container (copes with body {display:flex})

    gt.generate_graph(config, airports, log, out, rng, TZ, css="body { color: red; }")
    html = out.read_text(encoding="utf-8")
    assert html.index(".mrtg .bg") < html.index("body { color: red; }") < html.index("</head>")


def test_svg_uses_theme_classes(gt, config):
    hours = list(arrow.Arrow.range("hour", arrow.get("2026-11-02T00:00", tzinfo=TZ), limit=2))
    svg = gt.svg_bars(list(zip(hours, [1, 2])), "HH", config["graph"])
    assert '<svg class="mrtg"' in svg and 'class="bg"' in svg and 'class="grid"' in svg


def test_svg_bars_use_colors_from_ini(gt, config):
    config["graph"]["bar_color"] = "#94a3b8"
    config["graph"]["max_bar_color"] = "#0000ff"
    hours = list(arrow.Arrow.range("hour", arrow.get("2026-11-02T00:00", tzinfo=TZ), limit=2))
    svg = gt.svg_bars(list(zip(hours, [1, 2])), "HH", config["graph"])
    assert re.findall(r'<rect [^>]*fill="([^"]*)"><title>', svg) == ["#94a3b8", "#0000ff"]


# --- Weekly graph (to the right of each hourly graph) ---------------------------


def week_cells(section_html):
    week = section_html.split("<div class='graph-col week'>")[1].split("</table>")[0]
    return re.findall(r"<td>([0-9.]+)</td>", week)


def test_weekly_graph_next_to_each_hourly_graph(gt, config, airports, tmp_path):
    log = tmp_path / "log.csv"
    gt.write_log(log, [row(gt, "2026-11-02T10:00", "MMMX", 1), row(gt, "2026-11-02T10:00", "MMUN", 1)])
    out = tmp_path / "r.html"
    gt.generate_graph(config, airports, log, out, gt.parse_range("02-11-2026", None, TZ), TZ)
    html = out.read_text(encoding="utf-8")
    # total + 2 airports: each row has the hourly graph and the weekly one to its right
    assert html.count("<div class='graph-row'><div class='graph-col'>") == 3
    assert html.count("<div class='graph-col week'><h3>Last 8 weeks (weekly total)</h3>") == 3
    assert "flights / week" in html


def test_weekly_graph_sums_whole_history_by_week(gt, config, airports, tmp_path):
    log = tmp_path / "log.csv"
    gt.write_log(
        log,
        [
            row(gt, "2026-10-20T08:00", "MMMX", 4),   # week of Mon 19-10 (outside the hourly range)
            row(gt, "2026-10-26T08:00", "MMMX", 1),   # week of Mon 26-10
            row(gt, "2026-11-01T23:00", "MMMX", 2),   # Sunday: still the week of 26-10
            row(gt, "2026-11-02T00:00", "MMMX", 7),   # Monday: week of 02-11
            row(gt, "2026-11-09T10:00", "MMMX", 50),  # after the end of the range: not counted
        ],
    )
    out = tmp_path / "r.html"
    gt.generate_graph(config, airports, log, out, gt.parse_range("02-11-2026", None, TZ), TZ)
    mx = out.read_text(encoding="utf-8").split("<h2>Ciudad de México (MMMX)</h2>")[1]

    assert week_cells(mx) == ["7", "4.7", "7", "14"]  # weeks with data: 4, 3, 7 (average 4.67)
    week_svg = mx.split("<div class='graph-col week'>")[1].split("</svg>")[0]
    bars = re.findall(r"<title>Week of ([0-9-]+): (\d+)</title>", week_svg)
    assert bars == [("19-10-2026", "4"), ("26-10-2026", "3"), ("02-11-2026", "7")]
    labels = re.findall(r'text-anchor="middle">([0-9/]+)</text>', week_svg)
    assert len(labels) == 8 and labels[0] == "14/09" and labels[-1] == "02/11"  # 8 Mondays


def test_weekly_graph_respects_weeks_option(gt, config, airports, tmp_path):
    config["graph"]["weeks"] = "3"
    log = tmp_path / "log.csv"
    gt.write_log(log, [row(gt, "2026-11-02T10:00", "MMMX", 1)])
    out = tmp_path / "r.html"
    gt.generate_graph(config, airports, log, out, gt.parse_range("02-11-2026", None, TZ), TZ)
    html = out.read_text(encoding="utf-8")
    assert "Last 3 weeks (weekly total)" in html
    week_svg = html.split("<div class='graph-col week'>")[1].split("</svg>")[0]
    assert len(re.findall(r'text-anchor="middle">', week_svg)) == 3


# --- This week's graph (Sunday to Saturday, between the hourly and weekly graphs) --


def col(section_html, name):
    return section_html.split(f"<div class='graph-col {name}'>")[1].split("</table>")[0]


def test_this_week_graph_sits_between_hourly_and_weekly(gt, config, airports, tmp_path):
    log = tmp_path / "log.csv"
    gt.write_log(log, [row(gt, "2026-11-02T10:00", "MMMX", 1), row(gt, "2026-11-02T10:00", "MMUN", 1)])
    out = tmp_path / "r.html"
    gt.generate_graph(config, airports, log, out, gt.parse_range("02-11-2026", None, TZ), TZ)
    html = out.read_text(encoding="utf-8")
    assert html.count("<div class='graph-col this-week'>") == 3  # total + 2 airports
    for section in html.split("<section")[1:]:
        assert section.index("Daily graph (1 hour average)") < section.index("graph-col this-week") \
            < section.index("graph-col week'")


def test_this_week_graph_sunday_to_saturday(gt, config, airports, tmp_path):
    log = tmp_path / "log.csv"
    gt.write_log(
        log,
        [
            row(gt, "2026-10-31T10:00", "MMMX", 9),   # Saturday of the previous week: not counted
            row(gt, "2026-11-01T08:00", "MMMX", 2),   # Sunday 01-11: first day of the week
            row(gt, "2026-11-01T20:00", "MMMX", 3),   # same Sunday: adds up to 5
            row(gt, "2026-11-04T10:00", "MMMX", 4),   # Wednesday
            row(gt, "2026-11-06T23:00", "MMMX", 1),   # Friday = end of the range
            row(gt, "2026-11-07T10:00", "MMMX", 50),  # Saturday, after the end of the range: not counted
        ],
    )
    out = tmp_path / "r.html"
    gt.generate_graph(config, airports, log, out, gt.parse_range("06-11-2026", None, TZ), TZ)
    mx = out.read_text(encoding="utf-8").split("<h2>Ciudad de México (MMMX)</h2>")[1]
    week = col(mx, "this-week")

    assert "<h3>This week (01/11 – 07/11)</h3>" in week
    assert re.findall(r'text-anchor="middle">([A-Z])</text>', week) == list("SMTWTFS")
    bars = re.findall(r"<title>(\w+ [0-9-]+): (\d+)</title>", week)
    assert bars == [("Sunday 01-11-2026", "5"), ("Wednesday 04-11-2026", "4"), ("Friday 06-11-2026", "1")]
    assert re.findall(r"<td>([0-9.]+)</td>", week) == ["5", "3.3", "1", "10"]  # MAX, AVG, NOW, TOTAL
    assert "flights / day" in week


def test_this_week_starts_on_sunday_when_end_is_sunday(gt, config, airports, tmp_path):
    log = tmp_path / "log.csv"
    gt.write_log(log, [row(gt, "2026-11-08T10:00", "MMMX", 1)])
    out = tmp_path / "r.html"
    gt.generate_graph(config, airports, log, out, gt.parse_range("08-11-2026", None, TZ), TZ)
    assert "This week (08/11 – 14/11)" in out.read_text(encoding="utf-8")


def test_this_week_width_option(gt, config, airports, tmp_path):
    config["graph"]["this_week_width"] = "140"
    log = tmp_path / "log.csv"
    gt.write_log(log, [row(gt, "2026-11-02T10:00", "MMMX", 1)])
    out = tmp_path / "r.html"
    gt.generate_graph(config, airports, log, out, gt.parse_range("02-11-2026", None, TZ), TZ)
    week = out.read_text(encoding="utf-8").split("<div class='graph-col this-week'>")[1]
    assert '<svg class="mrtg" xmlns="http://www.w3.org/2000/svg" width="196"' in week  # 44 + 140 + 12


def test_three_graphs_share_one_row(gt, config, airports, tmp_path):
    config["graph"]["width"] = "600"
    config["graph"]["this_week_width"] = "210"
    config["graph"]["week_width"] = "320"
    log = tmp_path / "log.csv"
    gt.write_log(log, [row(gt, "2026-11-02T10:00", "MMMX", 1)])
    out = tmp_path / "r.html"
    gt.generate_graph(config, airports, log, out, gt.parse_range("02-11-2026", None, TZ), TZ)
    html = out.read_text(encoding="utf-8")
    assert "flex-wrap:nowrap" in html and "flex-wrap:wrap" not in html
    # Each column's share is its SVG width (plot + 56 px of margins).
    assert ".report .graph-col {{ flex:656 1 0; min-width:0; }}".replace("{{", "{").replace("}}", "}") in html
    assert ".report .graph-col.this-week { flex:266 1 0; }" in html
    assert ".report .graph-col.week { flex:376 1 0; }" in html
    assert ".report .graph-row svg.mrtg { display:block; width:100%; height:auto; }" in html
    # The three columns are in the same row element.
    row_html = html.split("<div class='graph-row'>")[1]
    assert row_html.index("graph-col'>") < row_html.index("graph-col this-week") < row_html.index("graph-col week")


def test_stats_table_headers_and_label(gt):
    hours = list(arrow.Arrow.range("hour", arrow.get("2026-11-02T00:00", tzinfo=TZ), limit=2))
    for unit in ("hour", "day", "week"):
        table = gt.stats_table(list(zip(hours, [1, 2])), unit)
        assert "<th></th><th>MAX</th><th>AVG</th><th>NOW</th><th>TOTAL</th>" in table
        assert f"<td class='lbl'>{unit}</td>" in table
        assert "Flights" not in table
