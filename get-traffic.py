#!/usr/bin/env python3
"""get-traffic.py - Per-airport arrivals log and MRTG-style graphs.

Actions:
  --check-traffic            Query OpenSky and record the hourly count in the log.
  --generate-graph FILE      Build an MRTG-style HTML report (hourly bars) from the log.
  --from-date / --to-date    Date range (dd-mm-YYYY) for both actions.
"""

from __future__ import annotations

import configparser
import csv
import html
import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Optional

import arrow
import requests
import typer

SCRIPT_DIR = Path(__file__).resolve().parent
DATE_FMT = "DD-MM-YYYY"
HOUR_FMT = "YYYY-MM-DDTHH:mmZZ"
STAMP_FMT = "YYYY-MM-DDTHH:mm:ssZZ"
LOG_FIELDS = ["hour", "icao", "count", "source", "checked_at"]


class TrafficError(Exception):
    """Configuration or data error shown to the user."""


# --------------------------------------------------------------------------- #
# Configuration and input data
# --------------------------------------------------------------------------- #
@dataclass
class Airport:
    icao: str
    iata: str
    name: str
    lat: float | None
    lon: float | None
    timezone: str
    extra: dict[str, str] = field(default_factory=dict)


# Column names accepted in the airports CSV (English, plus Spanish aliases).
COLUMN_ALIASES = {
    "icao": ("icao", "designacion_icao", "codigo_icao"),
    "iata": ("iata", "designacion_iata", "codigo_iata"),
    "name": ("name", "nombre_aeropuerto", "nombre"),
    "lat": ("lat", "latitude", "latitud"),
    "lon": ("lon", "longitude", "longitud"),
    "timezone": ("timezone", "zona_horaria"),
}


def resolve_path(base: Path, value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else base / p


def load_config(path: Path) -> configparser.ConfigParser:
    if not path.is_file():
        raise TrafficError(f"Configuration file not found: {path}")
    # Only ";" starts inline comments: "#" is used in values such as colors (bar_color = #94a3b8).
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";",))
    cfg.read(path, encoding="utf-8")
    for section in ("general", "opensky", "check", "graph"):
        if not cfg.has_section(section):
            cfg.add_section(section)
    return cfg


def load_airports(path: Path, default_tz: str) -> list[Airport]:
    if not path.is_file():
        raise TrafficError(f"Airports list not found: {path}")
    airports: list[Airport] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = [h.strip() for h in reader.fieldnames or []]
        lower = {h.lower(): h for h in headers}
        cols = {
            key: next((lower[a] for a in aliases if a in lower), None)
            for key, aliases in COLUMN_ALIASES.items()
        }
        if not cols["icao"]:
            raise TrafficError(f"{path}: missing the ICAO code column ('icao' or 'designacion_icao')")
        known = {c for c in cols.values() if c}
        reader.fieldnames = headers

        def get(row: dict, key: str) -> str:
            return (row.get(cols[key]) or "").strip() if cols[key] else ""

        for n, row in enumerate(reader, start=2):
            icao = get(row, "icao").upper()
            if not icao:
                continue
            try:
                lat = float(get(row, "lat")) if get(row, "lat") else None
                lon = float(get(row, "lon")) if get(row, "lon") else None
            except ValueError:
                raise TrafficError(f"{path}:{n}: invalid latitude/longitude for {icao}") from None
            airports.append(
                Airport(
                    icao=icao,
                    iata=get(row, "iata").upper(),
                    name=get(row, "name") or icao,
                    lat=lat,
                    lon=lon,
                    timezone=get(row, "timezone") or default_tz,
                    extra={
                        h: (row.get(h) or "").strip()
                        for h in headers
                        if h not in known and (row.get(h) or "").strip()
                    },
                )
            )
    if not airports:
        raise TrafficError(f"{path}: contains no airports")
    return airports


def parse_date(value: str, tz: str, end_of_day: bool = False) -> arrow.Arrow:
    try:
        d = arrow.get(value.strip(), DATE_FMT, tzinfo=tz)
    except (arrow.parser.ParserError, ValueError):
        raise TrafficError(f"Invalid date '{value}', use the dd-mm-YYYY format") from None
    return d.ceil("day") if end_of_day else d.floor("day")


def parse_range(
    from_date: str | None, to_date: str | None, tz: str
) -> tuple[arrow.Arrow, arrow.Arrow] | None:
    if not from_date and not to_date:
        return None
    start = parse_date(from_date or to_date, tz)
    end = parse_date(to_date or from_date, tz, end_of_day=True)
    if start > end:
        raise TrafficError("--from-date cannot be later than --to-date")
    return start, end


# --------------------------------------------------------------------------- #
# OpenSky client
# --------------------------------------------------------------------------- #
class OpenSkyClient:
    def __init__(self, cfg: configparser.SectionProxy):
        self.base_url = cfg.get("base_url", "https://opensky-network.org/api").rstrip("/")
        self.token_url = cfg.get("token_url", "")
        self.client_id = cfg.get("client_id", "").strip()
        self.client_secret = cfg.get("client_secret", "").strip()
        self.timeout = cfg.getfloat("timeout", 30)
        self.delay = cfg.getfloat("request_delay", 1.0)
        self.max_retries = cfg.getint("max_retries", 3)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "get-traffic/1.0"
        self._token: str | None = None
        self._token_exp = 0.0
        self._last_request = 0.0

    def _auth_header(self) -> dict[str, str]:
        if not (self.client_id and self.client_secret and self.token_url):
            return {}
        if not self._token or time.time() > self._token_exp - 30:
            r = self.session.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                timeout=self.timeout,
            )
            if r.status_code != 200:
                raise TrafficError(f"Could not get an OpenSky token (HTTP {r.status_code})")
            data = r.json()
            self._token = data["access_token"]
            self._token_exp = time.time() + float(data.get("expires_in", 1800))
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, endpoint: str, params: dict) -> object | None:
        """GET with a delay between requests and retries. Returns None on 404."""
        url = f"{self.base_url}{endpoint}"
        for attempt in range(1, self.max_retries + 1):
            wait = self.delay - (time.time() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.time()
            try:
                r = self.session.get(
                    url, params=params, headers=self._auth_header(), timeout=self.timeout
                )
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise TrafficError(f"Network error talking to OpenSky: {exc}") from None
                time.sleep(2 * attempt)
                continue
            if r.status_code == 404:
                return None
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == self.max_retries:
                    raise TrafficError(f"OpenSky returned HTTP {r.status_code} on {endpoint}")
                retry = r.headers.get("X-Rate-Limit-Retry-After-Seconds") or r.headers.get(
                    "Retry-After"
                )
                time.sleep(min(float(retry), 60) if retry and retry.isdigit() else 5 * attempt)
                continue
            if r.status_code in (401, 403):
                raise TrafficError(
                    f"OpenSky rejected the request (HTTP {r.status_code}); "
                    "set client_id/client_secret in get-traffic.ini"
                )
            if r.status_code != 200:
                raise TrafficError(f"OpenSky returned HTTP {r.status_code} on {endpoint}")
            return r.json()
        return None

    def arrivals(self, icao: str, begin: arrow.Arrow, end: arrow.Arrow) -> list[dict]:
        """Historical arrivals, queried in 1-day chunks (API limit)."""
        flights: list[dict] = []
        cursor = begin
        while cursor < end:
            chunk_end = min(cursor.shift(days=1), end)
            data = self._get(
                "/flights/arrival",
                {"airport": icao, "begin": int(cursor.timestamp()), "end": int(chunk_end.timestamp())},
            )
            if data:
                flights.extend(data)
            cursor = chunk_end
        return flights

    def states_around(self, lat: float, lon: float, radius_km: float) -> list[list]:
        dlat = radius_km / 111.32
        dlon = radius_km / (111.32 * max(math.cos(math.radians(lat)), 0.01))
        data = self._get(
            "/states/all",
            {"lamin": lat - dlat, "lamax": lat + dlat, "lomin": lon - dlon, "lomax": lon + dlon},
        )
        return (data or {}).get("states") or []


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


# --------------------------------------------------------------------------- #
# Traffic log
# --------------------------------------------------------------------------- #
def read_log(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        rows = []
        for row in csv.DictReader(fh):
            try:
                row["count"] = int(row["count"])
                row["_hour"] = arrow.get(row["hour"])
            except (KeyError, ValueError, arrow.parser.ParserError):
                continue
            rows.append(row)
        return rows


def write_log(path: Path, new_rows: list[dict]) -> None:
    """Append rows to the log; a new measurement replaces the one with the same key."""
    path.parent.mkdir(parents=True, exist_ok=True)
    merged: dict[tuple, dict] = {}
    for row in read_log(path) + new_rows:
        key = (arrow.get(row["hour"]).int_timestamp, row["icao"], row["source"])
        merged[key] = {k: row[k] for k in LOG_FIELDS}
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LOG_FIELDS)
        writer.writeheader()
        for key in sorted(merged):
            writer.writerow(merged[key])
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# --check-traffic
# --------------------------------------------------------------------------- #
def count_approach(client: OpenSkyClient, ap: Airport, cfg: configparser.SectionProxy) -> int:
    if ap.lat is None or ap.lon is None:
        raise TrafficError("'approach' mode requires lat/lon in the CSV")
    radius = cfg.getfloat("approach_radius_km", 150)
    max_alt = cfg.getfloat("max_altitude_m", 6000)
    descending = cfg.getboolean("require_descending", True)
    total = 0
    for s in client.states_around(ap.lat, ap.lon, radius):
        lon, lat, baro, on_ground, vrate = s[5], s[6], s[7], s[8], s[11]
        alt = baro if baro is not None else s[13]
        if on_ground or lat is None or lon is None:
            continue
        if alt is not None and alt > max_alt:
            continue
        if descending and (vrate is None or vrate >= 0):
            continue
        if haversine_km(ap.lat, ap.lon, lat, lon) <= radius:
            total += 1
    return total


def count_arrivals(
    client: OpenSkyClient, ap: Airport, start: arrow.Arrow, end: arrow.Arrow
) -> dict[arrow.Arrow, int]:
    start, end = start.to(ap.timezone), end.to(ap.timezone)
    counts: dict[arrow.Arrow, int] = {h: 0 for h in arrow.Arrow.range("hour", start.floor("hour"), end)}
    for f in client.arrivals(ap.icao, start, end):
        ts = f.get("lastSeen") or f.get("firstSeen")
        if not ts:
            continue
        hour = arrow.get(ts).to(ap.timezone).floor("hour")
        if hour in counts:
            counts[hour] += 1
    return counts


def check_traffic(cfg, airports, log_path: Path, date_range, default_tz: str) -> bool:
    client = OpenSkyClient(cfg["opensky"])
    mode = cfg["check"].get("mode", "approach").strip().lower()
    if date_range is not None:
        mode = "arrivals"
    elif mode == "arrivals":
        yesterday = arrow.now(default_tz).shift(days=-1)
        date_range = (yesterday.floor("day"), yesterday.ceil("day"))
    elif mode != "approach":
        raise TrafficError(f"Unknown mode in [check] mode: '{mode}' (approach|arrivals)")

    now = arrow.now(default_tz)
    rows: list[dict] = []
    ok = 0
    if mode == "arrivals":
        typer.echo(
            f"Historical arrivals {date_range[0].format(DATE_FMT)} → {date_range[1].format(DATE_FMT)}"
        )
    else:
        typer.echo(f"Inbound flights (approach) at {now.format('HH:mm')}")
    typer.echo(f"{'ICAO':<6} {'Airport':<28} {'Flights':>7}  Detail")
    for ap in airports:
        try:
            if mode == "approach":
                hour = arrow.now(ap.timezone).floor("hour")
                n = count_approach(client, ap, cfg["check"])
                rows.append(_row(hour, ap.icao, n, "approach", now))
                detail = f"hour {hour.format('HH:00')}"
            else:
                counts = count_arrivals(client, ap, *date_range)
                rows.extend(_row(h, ap.icao, c, "arrivals", now) for h, c in counts.items())
                n = sum(counts.values())
                peak = max(counts.items(), key=lambda kv: kv[1], default=None)
                detail = f"peak {peak[1]} at {peak[0].format('DD-MM HH:00')}" if peak and peak[1] else ""
            ok += 1
            typer.echo(f"{ap.icao:<6} {ap.name[:28]:<28} {n:>7}  {detail}")
        except TrafficError as exc:
            typer.secho(f"{ap.icao:<6} {ap.name[:28]:<28} {'ERROR':>7}  {exc}", fg="red", err=True)
    if rows:
        write_log(log_path, rows)
        typer.echo(f"Log updated: {log_path} ({len(rows)} records)")
    return ok > 0


def _row(hour: arrow.Arrow, icao: str, count: int, source: str, checked: arrow.Arrow) -> dict:
    return {
        "hour": hour.format(HOUR_FMT),
        "icao": icao,
        "count": count,
        "source": source,
        "checked_at": checked.format(STAMP_FMT),
    }


# --------------------------------------------------------------------------- #
# --generate-graph (MRTG-style HTML)
# --------------------------------------------------------------------------- #
def nice_max(value: int) -> int:
    """Y-axis top: 4 divisions of a "round" step (1, 2, 2.5, 5 × 10^n)."""
    raw_step = max(value, 1) / 4
    exp = 10 ** math.floor(math.log10(raw_step)) if raw_step >= 1 else 1
    step = next(m * exp for m in (1, 2, 2.5, 5, 10) if m * exp >= raw_step)
    return int(step * 4)


def svg_bars(
    series: list[tuple[arrow.Arrow, int | None]],
    label_fmt: str,
    gcfg,
    width: int | None = None,
    unit: str | None = None,
    tip_fmt: str = "DD-MM-YYYY HH:mm",
    labels: list[str] | None = None,
) -> str:
    """Bar graph. With `labels`, each bar gets that fixed label centered below it."""
    width = width or gcfg.getint("width", 600)
    unit = unit or ("hour" if "HH" in label_fmt else "day")
    height = gcfg.getint("height", 150)
    color = gcfg.get("bar_color", "#00cc00")
    max_color = gcfg.get("max_bar_color", "#0000ff")
    ml, mr, mt, mb = 44, 12, 10, 28
    pw, ph = width, height
    vals = [v for _, v in series if v is not None]
    vmax = max(vals, default=0)
    top = nice_max(vmax)
    n = max(len(series), 1)
    bw = pw / n
    out = [
        f'<svg class="mrtg" xmlns="http://www.w3.org/2000/svg" width="{ml + pw + mr}" height="{mt + ph + mb}" '
        f'viewBox="0 0 {ml + pw + mr} {mt + ph + mb}" font-family="monospace" font-size="10">',
        f'<rect class="bg" x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="#ffffff" stroke="#000"/>',
    ]
    for i in range(5):  # horizontal grid and Y axis
        y = mt + ph - ph * i / 4
        out.append(
            f'<line class="grid" x1="{ml}" y1="{y:.1f}" x2="{ml + pw}" y2="{y:.1f}" stroke="#ccc" stroke-dasharray="2,2"/>'
        )
        val = top * i / 4
        txt = f"{val:g}"
        out.append(f'<text x="{ml - 4}" y="{y + 3:.1f}" text-anchor="end">{txt}</text>')
    step = max(1, math.ceil(n / 12))
    for i, (t, v) in enumerate(series):
        x = ml + i * bw
        if labels is not None:
            out.append(
                f'<text x="{x + bw / 2:.1f}" y="{mt + ph + 12}" text-anchor="middle">{html.escape(labels[i])}</text>'
            )
        elif i % step == 0:
            out.append(f'<line x1="{x:.1f}" y1="{mt}" x2="{x:.1f}" y2="{mt + ph}" stroke="#e00" stroke-opacity="0.25"/>')
            out.append(f'<text x="{x:.1f}" y="{mt + ph + 12}" text-anchor="middle">{t.format(label_fmt)}</text>')
        if v:
            h = ph * v / top
            fill = max_color if v == vmax else color
            out.append(
                f'<rect x="{x + bw * 0.1:.1f}" y="{mt + ph - h:.1f}" width="{max(bw * 0.8, 0.8):.1f}" '
                f'height="{h:.1f}" fill="{fill}"><title>{t.format(tip_fmt)}: {v}</title></rect>'
            )
    out.append(f'<text class="muted" x="{ml}" y="{mt + ph + 25}" fill="#555">flights / {unit}</text>')
    out.append("</svg>")
    return "".join(out)


def stats_table(series: list[tuple[arrow.Arrow, int | None]], unit: str) -> str:
    vals = [v for _, v in series if v is not None]
    if not vals:
        return "<p class='nodata'>No data in range.</p>"
    current = next((v for _, v in reversed(series) if v is not None), 0)
    avg = sum(vals) / len(vals)
    return (
        "<table class='stats'><tr><th></th><th>MAX</th><th>AVG</th><th>NOW</th><th>TOTAL</th></tr>"
        f"<tr><td class='lbl'>{unit}</td><td>{max(vals)}</td><td>{avg:.1f}</td>"
        f"<td>{current}</td><td>{sum(vals)}</td></tr></table>"
    )


def load_info_html(path: Path) -> str:
    """Read the report header HTML ([graph] info-html).

    Accepts a fragment or a full document. For a full document, it takes the <body>
    content and keeps its <style> blocks. The content is inserted as is, unescaped.
    """
    if not path.is_file():
        raise TrafficError(f"[graph] info-html file not found: {path}")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise TrafficError(f"Could not read {path}: {exc}") from None
    body = re.search(r"<body[^>]*>(.*?)</body\s*>", text, re.IGNORECASE | re.DOTALL)
    if not body:
        return text.strip()
    styles = re.findall(r"<style[^>]*>.*?</style\s*>", text[: body.start()], re.IGNORECASE | re.DOTALL)
    return "\n".join(styles + [body.group(1).strip()])


def load_css(path: Path) -> str | None:
    """Read the [graph] css-html stylesheet to embed it in the report.

    Strips <style> tags if the file has them. If the file is missing, warns and returns
    None: the report is still generated, with its own styles.
    """
    if not path.is_file():
        typer.secho(f"Warning: [graph] css-html not found: {path}; styles skipped", fg="yellow", err=True)
        return None
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise TrafficError(f"Could not read {path}: {exc}") from None
    text = re.sub(r"</?style[^>]*>", "", text, flags=re.IGNORECASE)
    # Keeps the content from closing the report's <style> block early.
    return text.strip().replace("</", "<\\/") or None


def generate_graph(
    cfg,
    airports,
    log_path: Path,
    output: Path,
    date_range,
    default_tz: str,
    info_html: str | None = None,
    css: str | None = None,
) -> None:
    rows = read_log(log_path)
    if not rows:
        raise TrafficError(f"The log {log_path} is empty or missing; run --check-traffic")
    if date_range is None:
        end = arrow.now(default_tz).ceil("hour")
        start = end.shift(hours=-24).shift(microseconds=1)
    else:
        start, end = date_range
    gcfg = cfg["graph"]
    title = gcfg.get("title", "Arrivals traffic")
    names = {a.icao: a for a in airports}

    # data: records inside the range (hourly graph).
    # history: everything recorded up to the end of the range (weekly graph).
    data: dict[tuple[str, str], dict[int, int]] = defaultdict(dict)
    history: dict[tuple[str, str], dict[int, int]] = defaultdict(dict)
    for r in rows:
        if r["_hour"] <= end:
            history[(r["icao"], r["source"])][r["_hour"].int_timestamp] = r["count"]
            if start <= r["_hour"]:
                data[(r["icao"], r["source"])][r["_hour"].int_timestamp] = r["count"]

    order = [a.icao for a in airports] + sorted({k[0] for k in data} - set(names))
    multi_day = (end - start).total_seconds() > 2 * 86400
    n_weeks = max(gcfg.getint("weeks", 8), 1)
    week_width = gcfg.getint("week_width", 320)
    this_week_width = gcfg.getint("this_week_width", 210)
    # Full SVG widths (plot + 44 px left margin + 12 px right margin). They set the share of
    # the row each column gets, so the three graphs always fit side by side.
    col_hour, col_this_week, col_week = (w + 56 for w in (gcfg.getint("width", 600), this_week_width, week_width))

    def this_week_series(values: dict[int, int], tz: str) -> list[tuple[arrow.Arrow, int | None]]:
        """Daily totals for the week (Sunday to Saturday) that contains the end of the range."""
        day = end.to(tz).floor("day")
        sunday = day.shift(days=-((day.weekday() + 1) % 7))  # weekday(): Monday=0 ... Sunday=6
        days = [sunday.shift(days=i) for i in range(7)]
        sums: dict[int, int] = {}
        for ts, count in values.items():
            d = arrow.get(ts).to(tz).floor("day").int_timestamp
            sums[d] = sums.get(d, 0) + count
        return [(d, sums.get(d.int_timestamp)) for d in days]

    def weekly_series(values: dict[int, int], tz: str) -> list[tuple[arrow.Arrow, int | None]]:
        """Total per week (Monday to Sunday) for the last n_weeks weeks up to the end of the range."""
        last = end.to(tz).floor("week")
        weeks = [last.shift(weeks=-i) for i in reversed(range(n_weeks))]
        sums: dict[int, int] = {}
        for ts, count in values.items():
            wk = arrow.get(ts).to(tz).floor("week").int_timestamp
            sums[wk] = sums.get(wk, 0) + count
        return [(w, sums.get(w.int_timestamp)) for w in weeks]

    def graph_blocks(values: dict[int, int], all_values: dict[int, int], tz: str, label: str) -> list[str]:
        """Row with the hourly graph, this week's graph (Sun-Sat) and the weekly one.

        For long ranges, a daily graph is added below the row.
        """
        hours = arrow.Arrow.range("hour", start.to(tz).floor("hour"), end.to(tz))
        series = [(h, values.get(h.int_timestamp)) for h in hours]
        tseries = this_week_series(all_values, tz)
        wseries = weekly_series(all_values, tz)
        blocks = [
            "<div class='graph-row'>"
            "<div class='graph-col'>"
            f"<h3>{label} — Daily graph (1 hour average)</h3>"
            f"<div class='graph'>{svg_bars(series, 'HH' if not multi_day else 'DD/MM HH', gcfg)}</div>"
            f"{stats_table(series, 'hour')}"
            "</div>"
            "<div class='graph-col this-week'>"
            f"<h3>This week ({tseries[0][0].format('DD/MM')} – {tseries[-1][0].format('DD/MM')})</h3>"
            "<div class='graph'>"
            + svg_bars(tseries, "dd", gcfg, width=this_week_width, unit="day",
                       tip_fmt="dddd DD-MM-YYYY", labels=list("SMTWTFS"))
            + "</div>"
            f"{stats_table(tseries, 'day')}"
            "</div>"
            "<div class='graph-col week'>"
            f"<h3>Last {n_weeks} weeks (weekly total)</h3>"
            f"<div class='graph'>{svg_bars(wseries, 'DD/MM', gcfg, width=week_width, unit='week', tip_fmt='[Week of] DD-MM-YYYY')}</div>"
            f"{stats_table(wseries, 'week')}"
            "</div>"
            "</div>"
        ]
        if multi_day:
            daily: dict[arrow.Arrow, int | None] = {}
            for h, v in series:
                d = h.floor("day")
                if v is not None:
                    daily[d] = (daily.get(d) or 0) + v
                else:
                    daily.setdefault(d, None)
            dseries = list(daily.items())
            blocks.append(
                f"<h3>{label} — Weekly graph (1 day total)</h3>"
                f"<div class='graph'>{svg_bars(dseries, 'DD/MM', gcfg)}</div>"
                f"{stats_table(dseries, 'day')}"
            )
        return blocks

    sections: list[str] = []

    # First graph: total inbound flights (approach) summed over all airports.
    def sum_approach(source_data: dict) -> tuple[dict[int, int], set[str]]:
        totals: dict[int, int] = defaultdict(int)
        icaos = set()
        for (icao, source), values in source_data.items():
            if source == "approach":
                icaos.add(icao)
                for ts, count in values.items():
                    totals[ts] += count
        return totals, icaos

    totals, contributing = sum_approach(data)
    if totals:
        all_totals, _ = sum_approach(history)
        sections.append(
            "<section class='total'><h2>Total — all airports</h2>"
            f"<p class='tz'>Sum of {len(contributing)} airports · "
            f"Time zone: {html.escape(default_tz)}</p>"
            + "".join(graph_blocks(totals, all_totals, default_tz, "Inbound flights (approach)"))
            + "</section>"
        )

    for icao in order:
        ap = names.get(icao)
        tz = ap.timezone if ap else default_tz
        name = f"{ap.name} ({icao}{'/' + ap.iata if ap.iata else ''})" if ap else icao
        blocks = []
        for source, label in (("arrivals", "Actual arrivals"), ("approach", "Inbound flights (approach)")):
            values = data.get((icao, source))
            if values:
                blocks.extend(graph_blocks(values, history[(icao, source)], tz, label))
        if blocks:
            sections.append(
                f"<section><h2>{html.escape(name)}</h2><p class='tz'>"
                + "".join(
                    f"{html.escape(k.replace('_', ' ').capitalize())}: {html.escape(v)} · "
                    for k, v in (ap.extra.items() if ap else [])
                )
                + f"Time zone: {html.escape(tz)}</p>"
                + "".join(blocks)
                + "</section>"
            )
    if not sections:
        raise TrafficError("No log records for the requested range")

    generated = arrow.now(default_tz)
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
/* Built-in styles. They use the [graph] css-html variables when defined, otherwise the fallbacks. */
body {{ background:var(--bg-color, #fff); color:var(--text-main, #000);
  font-family: Arial, Helvetica, sans-serif; margin:16px; }}
.report, .container.report {{ width:100%; max-width:1440px; }}
.report > h1 {{ font-size:1.4em; border-bottom:2px solid var(--border-color, #000); padding-bottom:4px; margin-bottom:8px; }}
.report section h2 {{ font-size:1.15em; color:var(--text-main, #000); background:var(--card-bg, #eee);
  padding:4px 8px; border-left:6px solid #00cc00; margin:0 0 4px; }}
.report section h3 {{ font-size:0.95em; margin:14px 0 4px; }}
.report section.total h2 {{ border-left-color:var(--accent, #0000ff); background:var(--row-alt, #e6e6ff); }}
.report .total {{ border-bottom:2px solid var(--border-color, #000); padding-bottom:16px; }}
.report section {{ margin-bottom:28px; }}
.report .graph {{ overflow-x:auto; }}
/* The three graphs always share one row: each column takes a share proportional to its
   graph's width, and the SVGs scale down to fit when the window is narrower. */
.report .graph-row {{ display:flex; flex-wrap:nowrap; gap:24px; align-items:flex-start; }}
.report .graph-col {{ flex:{col_hour} 1 0; min-width:0; }}
.report .graph-col.this-week {{ flex:{col_this_week} 1 0; }}
.report .graph-col.week {{ flex:{col_week} 1 0; }}
.report .graph-row .graph {{ overflow:visible; }}
.report .graph-row svg.mrtg {{ display:block; width:100%; height:auto; }}
.report .graph-row .stats {{ display:block; max-width:100%; overflow-x:auto; }}
.report table.stats {{ width:auto; border-collapse:collapse; font-size:0.72em; margin-top:4px; }}
.report .stats th, .report .stats td {{ border:1px solid var(--border-color, #999); padding:1px 6px;
  text-align:right; line-height:1.3; }}
.report .stats th {{ font-size:0.9em; letter-spacing:0.03em; }}
.report .stats .lbl {{ text-align:left; color:#00aa00; font-weight:bold; }}
.report .meta, .report .tz {{ font-size:0.85em; color:var(--text-muted, #444); margin:4px 0 12px; }}
.report .nodata {{ color:#a00; }}
.report footer {{ font-size:0.75em; color:var(--text-muted, #666); border-top:1px solid var(--border-color, #ccc);
  padding-top:6px; margin-top:16px; }}
.report .info-html {{ border-bottom:2px solid var(--border-color, #000); padding-bottom:12px; margin-bottom:8px; }}
.mrtg .bg {{ fill:var(--card-bg, #fff); stroke:var(--border-color, #000); }}
.mrtg .grid {{ stroke:var(--border-color, #ccc); }}
.mrtg text {{ fill:var(--text-main, #000); }}
.mrtg .muted {{ fill:var(--text-muted, #555); }}
</style>{f'''
<style>
/* [graph] css-html */
{css}
</style>''' if css else ''}</head><body>
<div class="container report">
{f'<div class="info-html">{info_html}</div>' if info_html else f'<h1>{html.escape(title)}</h1>'}
<p class="meta">Range: {start.format('DD-MM-YYYY HH:mm')} → {end.format('DD-MM-YYYY HH:mm')} ({html.escape(default_tz)})<br>
Generated: {generated.format('DD-MM-YYYY HH:mm:ss')} · Source: OpenSky Network</p>
{''.join(sections)}
<footer>get-traffic.py · MRTG-style graphs</footer>
</div>
</body></html>
"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(doc, encoding="utf-8")
    typer.echo(f"Report generated: {output}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
app = typer.Typer(add_completion=False, help=__doc__, rich_markup_mode=None)


@app.command()
def main(
    ctx: typer.Context,
    check_traffic_opt: Annotated[
        bool, typer.Option("--check-traffic", help="Query OpenSky and update the log.")
    ] = False,
    generate_graph_opt: Annotated[
        Optional[Path],
        typer.Option("--generate-graph", metavar="FILE", help="Generate the MRTG-style HTML report."),
    ] = None,
    from_date: Annotated[
        Optional[str], typer.Option("--from-date", metavar="dd-mm-YYYY", help="Start date.")
    ] = None,
    to_date: Annotated[
        Optional[str], typer.Option("--to-date", metavar="dd-mm-YYYY", help="End date (inclusive).")
    ] = None,
    config: Annotated[
        Path, typer.Option("--config", help="Configuration file.")
    ] = SCRIPT_DIR / "get-traffic.ini",
) -> None:
    """Per-airport arrivals log (OpenSky) and MRTG-style bar graphs."""
    if not check_traffic_opt and generate_graph_opt is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(1)
    try:
        cfg = load_config(config)
        base = config.resolve().parent
        default_tz = cfg["general"].get("timezone", "UTC").strip() or "UTC"
        try:
            arrow.now(default_tz)
        except Exception:
            raise TrafficError(f"Invalid time zone in [general] timezone: '{default_tz}'") from None
        airports = load_airports(
            resolve_path(base, cfg["general"].get("airports_file", "airports.csv")), default_tz
        )
        log_path = resolve_path(base, cfg["general"].get("log_file", "data/traffic-log.csv"))
        date_range = parse_range(from_date, to_date, default_tz)

        # Validated before querying OpenSky so it fails early if the file is missing.
        info_html = css = None
        info_file = cfg["graph"].get("info-html", "").strip()
        css_file = cfg["graph"].get("css-html", "").strip()
        if generate_graph_opt is not None:
            if info_file:
                info_html = load_info_html(resolve_path(base, info_file))
            if css_file:
                css = load_css(resolve_path(base, css_file))

        if check_traffic_opt:
            if not check_traffic(cfg, airports, log_path, date_range, default_tz):
                raise TrafficError("Could not query any airport")
        if generate_graph_opt is not None:
            generate_graph(cfg, airports, log_path, generate_graph_opt, date_range, default_tz, info_html, css)
    except TrafficError as exc:
        typer.secho(f"Error: {exc}", fg="red", err=True)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
