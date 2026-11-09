"""Shared fixtures. No test uses the network: OpenSky is mocked."""

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_module():
    # The script name has a hyphen, so it is imported by path.
    spec = importlib.util.spec_from_file_location("get_traffic", ROOT / "get-traffic.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["get_traffic"] = module
    spec.loader.exec_module(module)
    return module


gt_module = _load_module()


@pytest.fixture
def gt():
    return gt_module


INI = """\
[general]
airports_file = airports.csv
log_file = data/traffic-log.csv
timezone = America/Mexico_City

[opensky]
base_url = https://opensky.test/api
token_url = https://auth.opensky.test/token
client_id =
client_secret =
timeout = 5
request_delay = 0
max_retries = 2

[check]
mode = approach
approach_radius_km = 50
max_altitude_m = 6000
require_descending = true

[graph]
title = Test
"""

# Uses the Spanish column aliases, like the project's airports.csv.
AIRPORTS = """\
nombre_aeropuerto,designacion_icao,ciudad_cercana,latitud,longitud,zona_horaria
Ciudad de México,MMMX,CDMX,19.4361,-99.0719,America/Mexico_City
Cancún,MMUN,Cancún,21.0365,-86.8771,America/Cancun
"""


@pytest.fixture
def project(tmp_path):
    """Temporary folder with an ini and an airports list; returns the ini path."""
    (tmp_path / "get-traffic.ini").write_text(INI, encoding="utf-8")
    (tmp_path / "airports.csv").write_text(AIRPORTS, encoding="utf-8")
    return tmp_path / "get-traffic.ini"


@pytest.fixture
def config(gt, project):
    return gt.load_config(project)


def state(lat, lon, alt=3000.0, on_ground=False, vrate=-5.0):
    """State vector in the /states/all format (17 fields)."""
    s = [None] * 17
    s[0], s[1] = "abc123", "TST100"
    s[5], s[6], s[7], s[8], s[11], s[13] = lon, lat, alt, on_ground, vrate, alt
    return s


def write(path: Path, text: str) -> Path:
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path
