"""Web UI. Checks the pages render and, more importantly, that they report honestly:
products with too little data must say so rather than show a number."""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from propdop import adsb, associate, detect, geo, store, synth, web  # noqa: E402

RX = geo.Receiver(lat=40.88, lon=-73.52, elevation_msl_m=20.0, geoid_offset_m=-33.0, name="alpha")
C = 343.0


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    """A populated database on disk, since the app opens a connection per request."""
    path = tmp_path_factory.mktemp("web") / "passes.sqlite"
    air = [
        (90.0, 90.0, 2000.0, 1800.0, 90.0, 120.0),
        (320.0, 130.0, 4000.0, 3600.0, 110.0, 140.0),
    ]
    scene = [(t, dict(speed=v, cpa_distance=d, f0=f0, duration=dur))
             for t, v, d, _a, f0, dur in air]
    scene.append((210.0, dict(speed=28, cpa_distance=150, f0=120.0, duration=40.0)))
    samples, _ = synth.render_scene(scene, duration=430.0, snr_db=12.0, seed=4)
    tracks = [
        adsb.synthetic_track(RX, v, d, a, heading_deg=35.0 + 50 * i, t_cpa=t - d / C,
                             icao24=f"aaa00{i}", type_code=("C172", "BE20")[i])
        for i, (t, v, d, a, _f0, _dur) in enumerate(air)
    ]
    tracks.append(adsb.synthetic_track(RX, 95, 7500, 7000, t_cpa=150.0, icao24="quiet1",
                                       half_span_s=150.0))
    dets = detect.detect(samples, 8000, band=(50.0, 300.0), n_harmonics=4)
    result = associate.join(dets, adsb.audible_candidates(tracks, RX))

    db = store.Store(path)
    db.add_station(store.Station.from_receiver(RX, microphone="EM272"))
    db.record_join("alpha", result, blade_counts={"C172": 2, "BE20": 4})
    db.close()
    return path


@pytest.fixture(scope="module")
def client(db_path):
    return TestClient(web.create_app(db_path))


def test_health_is_available_for_the_container(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_dashboard_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "<!doctype html>" in body.lower()
    assert "PropDop" in body
    # epsilon is the headline metric and must be on the page
    assert "epsilon" in body.lower()
    assert "detection rate" in body.lower()


def test_dashboard_states_what_is_not_yet_measurable(client):
    """BUILD.md 3 replaced gates with per-product confidence. The UI must honour that."""
    body = client.get("/").text
    assert "insufficient" in body.lower()  # products short of their pass counts
    assert "Prop health" in body
    assert "reports nothing meaningful for months by design" in body
    # the atmospheric products exist now, and must still state their own confidence
    assert "Wind aloft" in body and "Temperature aloft" in body


def test_dashboard_reports_the_measured_error_not_the_fit_covariance(client):
    body = client.get("/").text
    assert "Measured error against ADS-B" in body
    assert "paired rows" in body


def test_api_status_exposes_the_products(client):
    d = client.get("/api/status").json()
    assert d["passes"] >= 2
    assert d["unheard"] == 1
    assert 0.0 < d["detection_rate"] < 1.0
    assert d["error_model"]["n"] >= 1
    assert {s["station_id"] for s in d["stations"]} == {"alpha"}


def test_passes_list_and_detail(client):
    rows = client.get("/api/passes").json()["passes"]
    assert rows
    pid = rows[0]["pass_id"]
    assert client.get("/passes").status_code == 200

    detail = client.get(f"/api/pass/{pid}").json()
    assert detail["pass_id"] == pid
    assert detail["tracks"]["harmonics"]
    assert detail["tracks"]["dt"] == pytest.approx(0.2, abs=0.05)

    page = client.get(f"/pass/{pid}")
    assert page.status_code == 200
    assert "<svg" in page.text
    # the retention consequence must be stated, not hidden
    assert "corpus" in page.text


def test_missing_pass_is_a_404(client):
    assert client.get("/api/pass/999999").status_code == 404
    assert client.get("/pass/999999").status_code == 404


def test_plot_helper_degrades_rather_than_crashing():
    assert "not enough data yet" in web.line_svg([], [], label="x")
    assert "not enough data yet" in web.line_svg([1.0], [2.0], label="x")
    svg = web.line_svg([0, 1, 2], [1, 4, 2], label="ok")
    assert "<polyline" in svg and "ok" in svg


def test_plot_helper_survives_non_finite_values():
    svg = web.line_svg([0, 1, 2, 3], [1.0, float("nan"), float("inf"), 2.0], label="x")
    assert "<svg" in svg


def test_empty_database_still_renders(tmp_path):
    c = TestClient(web.create_app(tmp_path / "empty.sqlite"))
    assert c.get("/health").status_code == 200
    r = c.get("/")
    assert r.status_code == 200
    assert "not enough data yet" in r.text
    assert c.get("/passes").status_code == 200
    status = c.get("/api/status")
    assert status.status_code == 200, status.text
    d = status.json()
    assert d["passes"] == 0
    # NaN is not JSON; "not computable yet" must encode as null, not crash or read as zero
    assert d["detection_rate"] is None


def test_jsonable_converts_non_finite_floats():
    out = web.jsonable({"a": float("nan"), "b": [1.0, float("inf")], "c": 2.5, "d": "x"})
    assert out == {"a": None, "b": [1.0, None], "c": 2.5, "d": "x"}


def test_radar_renders_the_measured_detection_envelope():
    """The one thing a street map cannot show: hearing range by direction, measured."""
    envelope = [
        {"bearing_deg": b, "max_heard_m": 4000.0, "min_missed_m": 6000.0 if b > 180 else None,
         "n_heard": 3, "n_missed": 1}
        for b in range(11, 360, 22)
    ]
    svg = web.radar_svg(
        envelope=envelope,
        overhead=[{"bearing_deg": 40, "slant_m": 3200, "agl_m": 1800, "type_code": "C172",
                   "registration": "N1", "speed_mps": 60}],
        recent=[{"adsb_bearing_deg": 30, "adsb_slant_m": 2800}],
    )
    assert "<polygon" in svg          # the hearing envelope
    assert "C172" in svg              # live traffic labelled when low
    assert ">N<" in svg and ">E<" in svg  # compass
    assert "km<" in svg               # range rings


def test_radar_handles_an_empty_station():
    svg = web.radar_svg()
    assert "<svg" in svg and "<polygon" not in svg


def test_dashboard_shows_the_radar_and_capture_state(client):
    body = client.get("/").text
    assert "Local airspace" in body
    assert "hearing envelope" in body
    assert "not running" in body or "listening" in body
    assert "Overhead now" in body


def test_detection_envelope_and_recent_positions_query(db_path):
    db = store.Store(db_path)
    env = db.detection_envelope(16)
    assert len(env) == 16
    assert all(set(e) >= {"bearing_deg", "max_heard_m", "min_missed_m"} for e in env)
    assert isinstance(db.recent_positions(), list)
    db.close()


def test_live_endpoint_returns_refreshable_fragments(client):
    """The radar updates itself; a whole-page reload would scroll the reader away from
    whatever they were looking at."""
    d = client.get("/api/live").json()
    assert set(d) >= {"radar", "overhead", "capture", "radar_note", "state", "n_overhead"}
    assert d["radar"].startswith("<svg")
    assert d["state"] in {"listening", "stalled", "not running"}


def test_dashboard_carries_the_poller_and_its_targets(client):
    body = client.get("/").text
    assert '/api/live' in body
    for anchor in ('id="radar"', 'id="overhead"', 'id="capture"', 'id="state-dot"'):
        assert anchor in body, anchor
    # no framework, no external assets: the page must work with no outbound route
    import re

    assert not re.search(r'<script[^>]+src=', body)
    assert not re.search(r'<link[^>]+href="https?://', body)
    assert "https://" not in body.split("<script>")[0]


def test_dashboard_still_works_without_javascript(client):
    """A meta refresh remains as the no-JS fallback."""
    body = client.get("/").text
    assert 'http-equiv="refresh"' in body


def test_the_dashboard_shows_which_candidates_were_considered(client):
    """The panel that distinguishes a deaf site from a missed detection."""
    body = client.get("/").text
    assert "Audible candidates considered" in body
    assert 'id="candidates"' in body
    # the fixture's quiet high-altitude track is expected-but-unheard, so it must appear
    assert "verdict" in body


def test_the_candidates_panel_refreshes_with_the_other_live_regions(client):
    d = client.get("/api/live").json()
    assert "candidates" in d
    assert "verdict" in d["candidates"] or "No expected-but-unheard" in d["candidates"]
