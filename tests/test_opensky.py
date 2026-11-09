"""OpenSky client (mocked) and counting functions."""

import arrow
import pytest

from conftest import state


class FakeResponse:
    def __init__(self, status_code=200, data=None, headers=None):
        self.status_code = status_code
        self._data = data
        self.headers = headers or {}

    def json(self):
        return self._data


@pytest.fixture
def client(gt, config, monkeypatch):
    monkeypatch.setattr(gt.time, "sleep", lambda s: None)
    return gt.OpenSkyClient(config["opensky"])


def fake_get(client, monkeypatch, responses):
    """Makes session.get return the responses in order and records the calls."""
    calls = []

    def _get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": params, "headers": headers})
        return responses[min(len(calls), len(responses)) - 1]

    monkeypatch.setattr(client.session, "get", _get)
    return calls


def test_get_ok(client, monkeypatch):
    calls = fake_get(client, monkeypatch, [FakeResponse(200, {"states": []})])
    assert client._get("/states/all", {"a": 1}) == {"states": []}
    assert calls[0]["url"] == "https://opensky.test/api/states/all"
    assert calls[0]["headers"] == {}  # no credentials, no token


def test_get_404_is_no_data(client, monkeypatch):
    fake_get(client, monkeypatch, [FakeResponse(404)])
    assert client._get("/flights/arrival", {}) is None


@pytest.mark.parametrize("status", [401, 403])
def test_get_auth_error(gt, client, monkeypatch, status):
    fake_get(client, monkeypatch, [FakeResponse(status)])
    with pytest.raises(gt.TrafficError, match="client_id"):
        client._get("/flights/arrival", {})


def test_get_retries_on_429_then_ok(client, monkeypatch):
    calls = fake_get(
        client,
        monkeypatch,
        [FakeResponse(429, headers={"X-Rate-Limit-Retry-After-Seconds": "3"}), FakeResponse(200, [1])],
    )
    assert client._get("/x", {}) == [1]
    assert len(calls) == 2


def test_get_gives_up_after_max_retries(gt, client, monkeypatch):
    calls = fake_get(client, monkeypatch, [FakeResponse(503)])
    with pytest.raises(gt.TrafficError, match="503"):
        client._get("/x", {})
    assert len(calls) == client.max_retries


def test_get_network_error(gt, client, monkeypatch):
    def boom(*a, **k):
        raise gt.requests.ConnectionError("no network")

    monkeypatch.setattr(client.session, "get", boom)
    with pytest.raises(gt.TrafficError, match="Network error"):
        client._get("/x", {})


def test_token_requested_once_and_cached(gt, config, monkeypatch):
    config["opensky"]["client_id"] = "id"
    config["opensky"]["client_secret"] = "secret"
    client = gt.OpenSkyClient(config["opensky"])
    posts = []

    def _post(url, data=None, timeout=None):
        posts.append(data)
        return FakeResponse(200, {"access_token": "tok", "expires_in": 1800})

    monkeypatch.setattr(client.session, "post", _post)
    calls = fake_get(client, monkeypatch, [FakeResponse(200, {})])
    client._get("/a", {})
    client._get("/b", {})
    assert len(posts) == 1
    assert posts[0]["grant_type"] == "client_credentials"
    assert calls[1]["headers"] == {"Authorization": "Bearer tok"}


def test_token_error(gt, config, monkeypatch):
    config["opensky"]["client_id"] = "id"
    config["opensky"]["client_secret"] = "wrong"
    client = gt.OpenSkyClient(config["opensky"])
    monkeypatch.setattr(client.session, "post", lambda *a, **k: FakeResponse(401))
    with pytest.raises(gt.TrafficError, match="token"):
        client._get("/a", {})


def test_arrivals_split_in_one_day_chunks(client, monkeypatch):
    calls = []

    def _get(endpoint, params):
        calls.append(params)
        return [{"lastSeen": params["begin"] + 60}] if len(calls) != 2 else None

    monkeypatch.setattr(client, "_get", _get)
    begin = arrow.get("2026-11-01T00:00:00+00:00")
    flights = client.arrivals("MMMX", begin, begin.shift(days=3))
    assert len(calls) == 3
    assert all(c["end"] - c["begin"] == 86400 for c in calls)
    assert len(flights) == 2  # the 404 chunk (None) adds no flights


def test_states_around_builds_bbox(client, monkeypatch):
    seen = {}

    def _get(endpoint, params):
        seen.update(params)
        return {"states": None}

    monkeypatch.setattr(client, "_get", _get)
    assert client.states_around(20.0, -100.0, 111.32) == []
    assert seen["lamin"] == pytest.approx(19.0)
    assert seen["lamax"] == pytest.approx(21.0)
    assert seen["lomax"] - seen["lomin"] > 2.0  # longitude widens with latitude


def test_haversine(gt):
    assert gt.haversine_km(0, 0, 0, 0) == 0
    assert gt.haversine_km(0, 0, 1, 0) == pytest.approx(111.19, abs=0.1)


class FakeClient:
    def __init__(self, states=None, flights=None):
        self.states = states or []
        self.flights = flights or []

    def states_around(self, lat, lon, radius_km):
        return self.states

    def arrivals(self, icao, begin, end):
        return self.flights


def test_count_approach_filters(gt, config):
    ap = gt.Airport("MMMX", "", "MX", 19.4361, -99.0719, "America/Mexico_City")
    states = [
        state(19.44, -99.07),                    # counts
        state(19.50, -99.10, vrate=-1.0),        # counts
        state(19.44, -99.07, on_ground=True),    # on the ground
        state(19.44, -99.07, alt=9000),          # too high
        state(19.44, -99.07, vrate=2.0),         # climbing
        state(19.44, -99.07, vrate=None),        # no vertical rate
        state(20.40, -99.07),                    # outside the radius (≈107 km > 50)
        state(None, None),                       # no position
    ]
    assert gt.count_approach(FakeClient(states), ap, config["check"]) == 2


def test_count_approach_without_descending_requirement(gt, config):
    config["check"]["require_descending"] = "false"
    ap = gt.Airport("MMMX", "", "MX", 19.4361, -99.0719, "UTC")
    states = [state(19.44, -99.07, vrate=2.0), state(19.44, -99.07, vrate=None)]
    assert gt.count_approach(FakeClient(states), ap, config["check"]) == 2


def test_count_approach_requires_coordinates(gt, config):
    ap = gt.Airport("KXXX", "", "X", None, None, "UTC")
    with pytest.raises(gt.TrafficError, match="lat/lon"):
        gt.count_approach(FakeClient(), ap, config["check"])


def test_count_arrivals_groups_by_local_hour(gt):
    tz = "America/Mexico_City"  # UTC-6
    ap = gt.Airport("MMMX", "", "MX", None, None, tz)
    start = arrow.get("2026-11-02", tzinfo=tz)
    end = start.ceil("day")
    t = lambda s: arrow.get(s).int_timestamp  # noqa: E731
    flights = [
        {"lastSeen": t("2026-11-02T14:10:00+00:00")},   # 08:10 local
        {"lastSeen": t("2026-11-02T14:50:00+00:00")},   # 08:50 local
        {"firstSeen": t("2026-11-02T20:00:00+00:00")},  # 14:00 local (no lastSeen)
        {"lastSeen": t("2026-11-04T00:00:00+00:00")},   # outside the range
        {},                                             # no timestamps
    ]
    counts = gt.count_arrivals(FakeClient(flights=flights), ap, start, end)
    assert len(counts) == 24
    by_hour = {h.hour: c for h, c in counts.items()}
    assert by_hour[8] == 2 and by_hour[14] == 1
    assert sum(counts.values()) == 3
