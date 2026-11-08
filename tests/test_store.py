"""Pass database: persistence, product queries, and the shareable report format."""

import numpy as np
import pytest

from propdop import adsb, associate, detect, geo, store, synth

RX = geo.Receiver(lat=40.88, lon=-73.52, elevation_msl_m=20.0, geoid_offset_m=-33.0, name="alpha")
C = 343.0
AIR_A = (90.0, 90.0, 2000.0, 1800.0, 90.0, 120.0)
AIR_B = (320.0, 130.0, 4000.0, 3600.0, 110.0, 140.0)


@pytest.fixture(scope="module")
def world():
    scene = [(t, dict(speed=v, cpa_distance=d, f0=f0, duration=dur))
             for t, v, d, _a, f0, dur in (AIR_A, AIR_B)]
    scene.append((210.0, dict(speed=28, cpa_distance=150, f0=120.0, duration=40.0)))
    samples, _ = synth.render_scene(scene, duration=430.0, snr_db=12.0, seed=4)
    tracks = [
        adsb.synthetic_track(RX, v, d, a, heading_deg=35.0 + 50 * i, t_cpa=t - d / C,
                             icao24=f"aaa00{i}", type_code=("C172", "BE20")[i])
        for i, (t, v, d, a, _f0, _dur) in enumerate((AIR_A, AIR_B))
    ]
    tracks.append(adsb.synthetic_track(RX, 95, 7500, 7000, t_cpa=150.0, icao24="quiet1",
                                       half_span_s=150.0))
    dets = detect.detect(samples, 8000, band=(50.0, 300.0), n_harmonics=4)
    geoms = adsb.audible_candidates(tracks, RX)
    return associate.join(dets, geoms)


@pytest.fixture
def populated(world):
    st = store.Store(":memory:")
    st.add_station(store.Station.from_receiver(RX, microphone="EM272", windscreen="90mm foam"))
    counts = st.record_join("alpha", world, blade_counts={"C172": 2, "BE20": 4})
    return st, counts


def test_records_matches_unexplained_and_unheard(populated):
    st, counts = populated
    assert counts["matched"] == 2
    assert counts["unheard"] == 1
    assert st.count("passes") == counts["matched"] + counts["unexplained"]
    assert st.count("unheard") == 1
    assert st.count("stations") == 1


def test_detection_rate_has_a_denominator(populated):
    """Negative detections are what make 'unexplained' interpretable."""
    st, _ = populated
    assert st.detection_rate("alpha") == pytest.approx(2 / 3, abs=0.01)


def test_station_metadata_round_trips(populated):
    st, _ = populated
    s = st.stations()[0]
    assert s["station_id"] == "alpha"
    # the geoid offset must survive: it is a tens-of-metres slant error if lost
    assert s["geoid_offset_m"] == pytest.approx(-33.0)
    assert s["microphone"] == "EM272"
    assert s["spl_calibrated"] == 0


def test_rpm_is_stored_only_where_blade_count_is_known(populated):
    st, _ = populated
    rows = st.conn.execute(
        "SELECT registration, type_code, blade_count, rpm, fit_f0_hz FROM passes"
        " WHERE icao24 IS NOT NULL"
    ).fetchall()
    assert rows
    for r in rows:
        if r["blade_count"]:
            assert r["rpm"] == pytest.approx(r["fit_f0_hz"] * 60.0 / r["blade_count"])
        else:
            assert r["rpm"] is None


def test_error_model_measures_against_truth(populated):
    st, _ = populated
    em = st.error_model("alpha")
    assert em["n"] == 2
    assert em["speed_frac"]["median_abs"] < 0.15
    assert em["slant_frac"]["median_abs"] < 0.30
    # the headline: the fit's own residual badly understates its real slant error
    assert em["slant_error_over_epsilon"] > 1.0


def test_tracks_round_trip_through_the_blob(populated):
    st, _ = populated
    blob = st.conn.execute(
        "SELECT tracks FROM passes WHERE tracks IS NOT NULL LIMIT 1"
    ).fetchone()[0]
    data = store.unpack_tracks(blob)
    ks = data["meta"]["harmonics"]
    assert ks and all(f"df{k}" in data and f"la{k}" in data for k in ks)
    assert data["t"].size == data[f"df{ks[0]}"].size == data["meta"]["n"]
    assert set(data["meta"]["noise_bias"]) == {str(k) for k in ks}
    # fit parameters travel too, so the absolute tracks are reconstructable
    assert set(data["meta"]["fit"]) == {"f0", "speed", "cpa_distance", "t0_heard"}
    assert data["meta"]["dt"] == pytest.approx(0.2, abs=0.05)  # 5 Hz storage rate


def test_track_blob_is_about_fifteen_kilobytes(populated):
    """BUILD.md 2 budgets ~15 kB/pass for the permanent dataset. Hold the line."""
    st, _ = populated
    sizes = [
        len(r[0])
        for r in st.conn.execute("SELECT tracks FROM passes WHERE tracks IS NOT NULL")
    ]
    assert sizes
    assert max(sizes) < 20_000, f"track blobs grew to {max(sizes)} bytes"
    print(f"\n  track blob sizes: {sorted(sizes)} bytes")


def test_blade_count_inferred_from_stored_fundamentals():
    st = store.Store(":memory:")
    st.add_station(store.Station("beta", 0.0, 0.0, 0.0, 0.0))
    rng = np.random.default_rng(3)
    # fabricate f0 rows directly: this tests the query path, not the fit
    for f0 in 2450.0 * 2 / 60.0 * (1 + rng.normal(scale=0.004, size=12)):
        st.conn.execute(
            "INSERT INTO passes (station_id, t_heard_cpa, source, type_code, fit_f0_hz,"
            " fit_speed_mps, fit_slant_m, epsilon) VALUES ('beta', ?, 'blind', 'PA28', ?,"
            " 90, 2000, 0.002)",
            (float(rng.uniform(0, 1e6)), float(f0)),
        )
    st.conn.commit()
    assert st.infer_blade_counts(min_passes=8) == {"PA28": 2}


def test_epsilon_trend_is_available_as_instrument_health(populated):
    st, _ = populated
    trend = st.epsilon_trend(bucket_s=3600.0)
    assert trend and all(t["n"] >= 1 and t["mean_epsilon"] > 0 for t in trend)


def test_scintillation_by_hour_runs_off_stored_tracks(populated):
    st, _ = populated
    rows = st.scintillation_by_hour()
    assert rows
    assert all(set(r) == {"hour_utc", "n", "mean_log_amp_var"} for r in rows)


# -- sharing ---------------------------------------------------------------------------


def test_reports_export_and_merge_into_another_station(populated, tmp_path):
    """A second station must be able to merge the first's reports without key collisions."""
    st, _ = populated
    path = tmp_path / "alpha.ndjson"
    written = st.export_reports(path)
    assert written == st.count("passes") + st.count("unheard") + 1

    other = store.Store(":memory:")
    other.add_station(store.Station("bravo", 41.0, -73.0, 50.0, -33.0))
    counts = other.import_reports(path)
    assert counts["station"] == 1 and counts["pass"] == st.count("passes")
    assert counts["unheard"] == st.count("unheard")
    assert {s["station_id"] for s in other.stations()} == {"alpha", "bravo"}
    # and the merged rows are usable, not just present
    assert other.detection_rate("alpha") == pytest.approx(st.detection_rate("alpha"))


def test_import_is_idempotent(populated, tmp_path):
    st, _ = populated
    path = tmp_path / "a.ndjson"
    st.export_reports(path)
    other = store.Store(":memory:")
    other.import_reports(path)
    n_first = other.count("passes")
    other.import_reports(path)
    assert other.count("passes") == n_first


def test_import_rejects_an_unknown_format(tmp_path):
    bad = tmp_path / "bad.ndjson"
    bad.write_text('{"type": "header", "format": "something-else/9"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported report format"):
        store.Store(":memory:").import_reports(bad)


def test_store_is_usable_from_multiple_threads():
    """The pipeline reads counts from its capture thread and writes passes from its analysis
    thread. SQLite's default `check_same_thread=True` raises on that -- and in deployment it
    presented not as an error anyone saw, but as a capture loop that silently restarted every
    five seconds and recorded nothing for the first run.
    """
    import threading

    db = store.Store(":memory:")
    db.add_station(store.Station("t", 0.0, 0.0, 0.0, 0.0))
    errors: list = []

    def worker():
        try:
            for _ in range(60):
                db.count("passes")
                db.detection_rate()
                db.stations()
                db.error_model()
                db.infer_blade_counts()
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors


def test_schema_migrates_a_database_from_an_older_build(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` does nothing to an existing table, so a new column is
    invisible to a deployed database. That presented as a 500 on the dashboard the first time
    the schema grew under a running instance.
    """
    import sqlite3

    path = tmp_path / "old.sqlite"
    con = sqlite3.connect(path)
    con.executescript(
        """CREATE TABLE passes (pass_id INTEGER PRIMARY KEY, station_id TEXT,
           t_heard_cpa REAL, source TEXT, fit_f0_hz REAL, fit_speed_mps REAL,
           fit_slant_m REAL, epsilon REAL, icao24 TEXT, registration TEXT, type_code TEXT,
           match_ambiguous INTEGER DEFAULT 0, adsb_slant_m REAL, adsb_speed_mps REAL,
           adsb_agl_m REAL, adsb_t_heard_cpa REAL, tracks BLOB, rpm REAL,
           blade_count INTEGER, converged INTEGER DEFAULT 1);
           CREATE TABLE unheard (unheard_id INTEGER PRIMARY KEY, station_id TEXT,
           t_heard_cpa REAL, icao24 TEXT, type_code TEXT, adsb_slant_m REAL,
           adsb_speed_mps REAL, adsb_agl_m REAL);
           CREATE TABLE stations (station_id TEXT PRIMARY KEY, lat REAL, lon REAL,
           elevation_msl_m REAL, geoid_offset_m REAL, microphone TEXT, windscreen TEXT,
           spl_calibrated INTEGER DEFAULT 0, ground_sources TEXT, notes TEXT);
           CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"""
    )
    con.commit()
    con.close()

    db = store.Store(path)
    for table, col in (("unheard", "adsb_bearing_deg"), ("passes", "adsb_bearing_deg"),
                       ("passes", "adsb_elevation_deg"),
                       ("unheard", "best_template_score")):
        cols = {r[1] for r in db.conn.execute(f"PRAGMA table_info({table})")}
        assert col in cols, f"{table}.{col} was not migrated"
    # and the queries that need the new columns now work
    assert len(db.detection_envelope(8)) == 8
    assert db.recent_positions() == []
    assert db.recent_unheard() == []
    db.close()


def test_an_unheard_pass_stores_the_score_it_reached(world):
    """A stored negative without a score is a complaint; with one it is a measurement."""
    st = store.Store(":memory:")
    st.add_station(store.Station.from_receiver(RX))
    g = world.unheard[0]
    st.record_join("alpha", world, blade_counts={"C172": 2},
                   unheard_scores={(g.icao24, round(g.t_heard_cpa, 1)): 6.25})
    rows = st.recent_unheard()
    assert len(rows) == 1
    assert rows[0]["icao24"] == g.icao24
    assert rows[0]["best_template_score"] == pytest.approx(6.25)
    assert detect.score_verdict(rows[0]["best_template_score"]) == "nothing tonal"
    st.close()


def test_a_missing_score_is_stored_as_null_not_as_zero(world):
    """NaN round-trips through SQLite as a float, and zero would read as 'nothing tonal'."""
    st = store.Store(":memory:")
    st.add_station(store.Station.from_receiver(RX))
    g = world.unheard[0]
    st.record_join("alpha", world, unheard_scores={
        (g.icao24, round(g.t_heard_cpa, 1)): float("nan")})
    assert st.recent_unheard()[0]["best_template_score"] is None
    # and a join recorded with no scores at all stays usable
    st2 = store.Store(":memory:")
    st2.add_station(store.Station.from_receiver(RX))
    st2.record_join("alpha", world)
    assert st2.recent_unheard()[0]["best_template_score"] is None
    st.close()
    st2.close()
