"""End-to-end command line, with OpenSky mocked."""

import arrow
import pytest
from typer.testing import CliRunner

from conftest import state

runner = CliRunner()


def run(gt, *args):
    return runner.invoke(gt.app, list(args))


@pytest.fixture
def fake_opensky(gt, monkeypatch):
    """Mocks OpenSky: 2 aircraft approaching MMMX, 1 approaching MMUN, and 3 historical arrivals."""
    def states_around(self, lat, lon, radius_km):
        n = 2 if lat < 20 else 1
        return [state(lat + 0.01, lon) for _ in range(n)]

    def arrivals(self, icao, begin, end):
        ts = begin.shift(hours=9, minutes=15).int_timestamp
        return [{"lastSeen": ts}] * 3

    monkeypatch.setattr(gt.OpenSkyClient, "states_around", states_around)
    monkeypatch.setattr(gt.OpenSkyClient, "arrivals", arrivals)


def test_no_action_shows_help(gt, project):
    result = run(gt, "--config", str(project))
    assert result.exit_code == 1
    assert "--check-traffic" in result.output


def test_help(gt):
    result = run(gt, "--help")
    assert result.exit_code == 0
    for opt in ("--check-traffic", "--generate-graph", "--from-date", "--to-date", "--config"):
        assert opt in result.output


@pytest.mark.parametrize(
    "args, message",
    [
        (["--check-traffic", "--from-date", "2026-11-01"], "Invalid date"),
        (["--check-traffic", "--from-date", "05-11-2026", "--to-date", "01-11-2026"], "cannot be later"),
        (["--generate-graph", "r.html"], "empty"),
    ],
)
def test_user_errors(gt, project, args, message):
    result = run(gt, "--config", str(project), *args)
    assert result.exit_code == 1
    assert message in result.output


def test_missing_config(gt, tmp_path):
    result = run(gt, "--config", str(tmp_path / "x.ini"), "--check-traffic")
    assert result.exit_code == 1
    assert "Configuration file not found" in result.output


def test_invalid_timezone(gt, project):
    project.write_text(project.read_text().replace("America/Mexico_City", "Marte/Base"), encoding="utf-8")
    result = run(gt, "--config", str(project), "--check-traffic")
    assert result.exit_code == 1
    assert "Invalid time zone" in result.output


def test_check_approach_then_graph(gt, project, fake_opensky):
    out = project.parent / "report.html"
    result = run(gt, "--config", str(project), "--check-traffic", "--generate-graph", str(out))
    assert result.exit_code == 0, result.output
    assert "MMMX" in result.output and "Log updated" in result.output

    rows = gt.read_log(project.parent / "data" / "traffic-log.csv")
    assert {(r["icao"], r["count"], r["source"]) for r in rows} == {("MMMX", 2, "approach"), ("MMUN", 1, "approach")}
    html = out.read_text(encoding="utf-8")
    assert html.index("Total — all airports") < html.index("Ciudad de México")


def test_check_with_dates_uses_arrivals(gt, project, fake_opensky):
    result = run(gt, "--config", str(project), "--check-traffic", "--from-date", "02-11-2026")
    assert result.exit_code == 0, result.output
    assert "Historical arrivals" in result.output

    rows = gt.read_log(project.parent / "data" / "traffic-log.csv")
    mx = [r for r in rows if r["icao"] == "MMMX"]
    assert len(mx) == 24 and all(r["source"] == "arrivals" for r in mx)
    assert {r["_hour"].hour: r["count"] for r in mx}[9] == 3
    assert sum(r["count"] for r in mx) == 3


def test_check_arrivals_mode_without_dates_uses_yesterday(gt, project, fake_opensky):
    project.write_text(project.read_text().replace("mode = approach", "mode = arrivals"), encoding="utf-8")
    result = run(gt, "--config", str(project), "--check-traffic")
    assert result.exit_code == 0, result.output
    yesterday = arrow.now("America/Mexico_City").shift(days=-1).format("DD-MM-YYYY")
    assert yesterday in result.output


def test_check_unknown_mode(gt, project):
    project.write_text(project.read_text().replace("mode = approach", "mode = other"), encoding="utf-8")
    result = run(gt, "--config", str(project), "--check-traffic")
    assert result.exit_code == 1
    assert "Unknown mode" in result.output


def test_partial_failure_keeps_going(gt, project, monkeypatch):
    def states_around(self, lat, lon, radius_km):
        if lat < 20:
            raise gt.TrafficError("OpenSky returned HTTP 503")
        return [state(lat, lon)]

    monkeypatch.setattr(gt.OpenSkyClient, "states_around", states_around)
    result = run(gt, "--config", str(project), "--check-traffic")
    assert result.exit_code == 0
    assert "ERROR" in result.output and "503" in result.output
    rows = gt.read_log(project.parent / "data" / "traffic-log.csv")
    assert [r["icao"] for r in rows] == ["MMUN"]


def test_all_airports_fail_exit_1(gt, project, monkeypatch):
    def states_around(self, lat, lon, radius_km):
        raise gt.TrafficError("no network")

    monkeypatch.setattr(gt.OpenSkyClient, "states_around", states_around)
    result = run(gt, "--config", str(project), "--check-traffic")
    assert result.exit_code == 1
    assert "Could not query any airport" in result.output
    assert not (project.parent / "data" / "traffic-log.csv").exists()


def set_info_html(project, value):
    text = project.read_text(encoding="utf-8").replace("[graph]\n", f"[graph]\ninfo-html = {value}\n")
    project.write_text(text, encoding="utf-8")


def test_info_html_from_ini(gt, project, fake_opensky):
    (project.parent / "info.html").write_text("<h1>Missile log</h1>", encoding="utf-8")
    set_info_html(project, "info.html")  # relative to the .ini folder
    out = project.parent / "r.html"
    result = run(gt, "--config", str(project), "--check-traffic", "--generate-graph", str(out))
    assert result.exit_code == 0, result.output
    assert "<h1>Missile log</h1>" in out.read_text(encoding="utf-8")


def test_info_html_missing_fails_before_querying(gt, project, monkeypatch):
    def states_around(*a, **k):
        raise AssertionError("should not have queried OpenSky")

    monkeypatch.setattr(gt.OpenSkyClient, "states_around", states_around)
    set_info_html(project, "missing.html")
    result = run(gt, "--config", str(project), "--check-traffic", "--generate-graph", "r.html")
    assert result.exit_code == 1
    assert "info-html" in result.output


def test_info_html_ignored_when_only_checking(gt, project, fake_opensky):
    set_info_html(project, "missing.html")
    result = run(gt, "--config", str(project), "--check-traffic")
    assert result.exit_code == 0, result.output


def set_graph_option(project, key, value):
    text = project.read_text(encoding="utf-8").replace("[graph]\n", f"[graph]\n{key} = {value}\n")
    project.write_text(text, encoding="utf-8")


def test_css_html_from_ini(gt, project, fake_opensky):
    (project.parent / "styles.css").write_text("<style>:root { --bg-color: #0f172a; }</style>", encoding="utf-8")
    set_graph_option(project, "css-html", "styles.css")
    out = project.parent / "r.html"
    result = run(gt, "--config", str(project), "--check-traffic", "--generate-graph", str(out))
    assert result.exit_code == 0, result.output
    assert ":root { --bg-color: #0f172a; }" in out.read_text(encoding="utf-8")


def test_css_html_missing_only_warns(gt, project, fake_opensky):
    set_graph_option(project, "css-html", "missing.css")
    out = project.parent / "r.html"
    result = run(gt, "--config", str(project), "--check-traffic", "--generate-graph", str(out))
    assert result.exit_code == 0, result.output
    assert "Warning" in result.output and out.exists()


def test_project_styles_css_is_valid(gt):
    """Project styles.css: no <style> tags and defines the variables the report uses."""
    css = gt.load_css(gt.SCRIPT_DIR / "styles.css")
    assert css and "<style" not in css
    for var in ("--bg-color", "--card-bg", "--text-main", "--text-muted", "--border-color", "--accent", "--row-alt"):
        assert var in css
    assert css.count("{") == css.count("}")
