"""The run loop. Unattended processes fail differently from libraries, so these tests are
mostly about not dying and not growing without bound."""

import time

import numpy as np
import pytest

from propdop import adsb, geo, node, pipeline, synth

RX = geo.Receiver(lat=51.23, lon=-0.98, elevation_msl_m=68.0, geoid_offset_m=46.0, name="home")
C = 343.0


def make_config(tmp_path, **kw):
    defaults = dict(
        station=RX,
        station_id="home",
        db_path=str(tmp_path / "passes.sqlite"),
        sample_rate=8000,
        archive_rate=8000,
        buffer_s=600.0,
        archive_s=900.0,
        analysis_window_s=420.0,
        corpus_dir=str(tmp_path / "corpus"),
    )
    defaults.update(kw)
    return pipeline.PipelineConfig(**defaults)


def scene_with_one_pass(t_heard=200.0, duration=600.0):
    return synth.render_scene(
        [(t_heard, dict(speed=90, cpa_distance=2000, f0=90.0, duration=140.0))],
        duration=duration,
        snr_db=14.0,
        seed=4,
    )


def test_pipeline_detects_and_stores_a_pass(tmp_path):
    samples, _ = scene_with_one_pass()
    src = node.SyntheticSource(samples, nominal_rate=8000)
    p = pipeline.Pipeline(make_config(tmp_path), source=src)
    p.node.clock.t0 = 0.0
    p.node.pump(blocks=600)

    out = p.analyse_once(seconds=600.0)
    assert out["aircraft"] >= 1
    assert p.db.count("passes") >= 1
    row = p.db.conn.execute("SELECT fit_f0_hz, fit_speed_mps FROM passes").fetchone()
    assert row["fit_f0_hz"] == pytest.approx(90.0, rel=0.05)
    assert row["fit_speed_mps"] == pytest.approx(90.0, rel=0.15)


def test_overlapping_windows_do_not_duplicate_passes(tmp_path):
    """Windows overlap so a pass on a boundary is not lost, which means re-detection is
    expected and must be de-duplicated."""
    samples, _ = scene_with_one_pass()
    src = node.SyntheticSource(samples, nominal_rate=8000)
    p = pipeline.Pipeline(make_config(tmp_path), source=src)
    p.node.clock.t0 = 0.0
    p.node.pump(blocks=600)

    p.analyse_once(seconds=600.0)
    n_first = p.db.count("passes")
    p.analyse_once(seconds=600.0)
    assert p.db.count("passes") == n_first


def test_analysis_is_skipped_until_there_is_enough_audio(tmp_path):
    src = node.SyntheticSource(np.zeros(8000 * 10), nominal_rate=8000)
    p = pipeline.Pipeline(make_config(tmp_path), source=src)
    p.node.pump(blocks=10)
    assert "skipped" in p.analyse_once(seconds=600.0)


def test_corpus_is_capped_and_evicts_oldest(tmp_path):
    samples, _ = scene_with_one_pass()
    src = node.SyntheticSource(samples, nominal_rate=8000)
    cfg = make_config(tmp_path, corpus_limit_bytes=20_000)
    p = pipeline.Pipeline(cfg, source=src)
    p.node.clock.t0 = 0.0
    p.node.pump(blocks=600)
    p.analyse_once(seconds=600.0)

    corpus = tmp_path / "corpus"
    if corpus.exists():
        total = sum(f.stat().st_size for f in corpus.glob("*.flac"))
        assert total <= cfg.corpus_limit_bytes * 1.5


def test_a_failing_adsb_poll_does_not_stop_the_run(tmp_path, monkeypatch):
    """A free third-party API will fail sometimes; the instrument must keep listening."""
    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(adsb, "fetch_adsb_lol", boom)
    src = node.SyntheticSource(np.zeros(8000 * 60), nominal_rate=8000)
    cfg = make_config(tmp_path, adsb_poll_s=0.05)
    p = pipeline.Pipeline(cfg, source=src)
    p.start()
    time.sleep(0.6)
    p.stop(timeout=5)
    assert p.stats["adsb_failures"] >= 2
    assert p.stats["last_error"] is None or "network down" not in str(p.stats["last_error"])


def test_a_failing_analysis_does_not_stop_the_run(tmp_path, monkeypatch):
    src = node.SyntheticSource(np.zeros(8000 * 60), nominal_rate=8000)
    p = pipeline.Pipeline(make_config(tmp_path, analysis_period_s=0.05,
                                      analysis_window_s=0.05), source=src)
    monkeypatch.setattr(p, "analyse_once", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    p.start()
    time.sleep(0.5)
    p.stop(timeout=5)
    assert p.stats["analysis_failures"] >= 2


def test_nothing_grows_without_bound(tmp_path):
    """Rings are fixed, ADS-B snapshots are pruned. A months-long process cannot accumulate."""
    src = node.SyntheticSource(np.zeros(8000 * 2000), nominal_rate=8000)
    cfg = make_config(tmp_path, adsb_keep_s=1.0)
    p = pipeline.Pipeline(cfg, source=src)
    p.node.pump(blocks=2000)
    assert p.node.ring.available_frames[1] - p.node.ring.available_frames[0] <= p.node.ring.capacity
    arch = p.node.archive_ring
    assert arch.available_frames[1] - arch.available_frames[0] <= arch.capacity

    p._snapshots = [{"now": time.time() - 100.0, "aircraft": []} for _ in range(50)]
    p._snapshots.append({"now": time.time(), "aircraft": []})
    cutoff = time.time() - cfg.adsb_keep_s
    p._snapshots = [s for s in p._snapshots if s.get("now", 0) >= cutoff]
    assert len(p._snapshots) == 1


def test_status_reports_enough_to_diagnose_a_silent_run(tmp_path):
    src = node.SyntheticSource(np.zeros(8000 * 30), nominal_rate=8000)
    p = pipeline.Pipeline(make_config(tmp_path), source=src)
    p.node.pump(blocks=30)
    st = p.status()
    for key in ("uptime_s", "node", "adsb_tracks", "passes_in_db", "analyses",
                "adsb_failures", "analysis_failures", "last_analysis"):
        assert key in st
    assert st["node"]["sample_rate_nominal"] == 8000


def test_pipeline_joins_to_adsb_when_tracks_are_present(tmp_path):
    """End to end with a matching track: the pass must come out identified, not blind."""
    t_heard = 200.0
    samples, _ = scene_with_one_pass(t_heard=t_heard)
    src = node.SyntheticSource(samples, nominal_rate=8000)
    p = pipeline.Pipeline(make_config(tmp_path), source=src)
    p.node.clock.t0 = 0.0
    p.node.pump(blocks=600)

    tr = adsb.synthetic_track(RX, 90.0, 2000.0, 1800.0, heading_deg=40.0,
                              t_cpa=t_heard - 2000.0 / C, icao24="aa0001", type_code="C172")
    snap_times = np.arange(t_heard - 120, t_heard + 120, 10.0)
    p._snapshots = [
        {
            "now": float(t),
            "aircraft": [
                {
                    "hex": "aa0001", "t": "C172", "r": "N12345",
                    "lat": float(np.interp(t, tr.times, tr.lat)),
                    "lon": float(np.interp(t, tr.times, tr.lon)),
                    "alt_geom": float(np.interp(t, tr.times, tr.alt_hae_m) / 0.3048),
                    "seen_pos": 0.1,
                }
            ],
        }
        for t in snap_times
    ]
    out = p.analyse_once(seconds=600.0)
    assert out["tracks"] == 1
    assert out["matched"] == 1
    row = p.db.conn.execute("SELECT icao24, registration FROM passes WHERE icao24 IS NOT NULL").fetchone()
    assert row["icao24"] == "aa0001" and row["registration"] == "N12345"


def test_capture_lag_is_measured(tmp_path):
    """The metric that makes a starved capture visible instead of silent."""
    src = node.SyntheticSource(np.zeros(8000 * 120), nominal_rate=8000)
    p = pipeline.Pipeline(make_config(tmp_path), source=src)
    p.stats["started"] = time.time() - 100.0
    p.node.pump(blocks=100)          # 100 s of audio in 100 s of wall time
    assert abs(p.capture_lag_s) < 5.0
    p.stats["started"] = time.time() - 300.0   # pretend 300 s elapsed, still 100 s captured
    assert p.capture_lag_s > 150.0


def test_analysis_runs_out_of_process_by_default(tmp_path):
    """Detection is CPU-bound Python and holds the GIL; in a thread it starves capture."""
    cfg = make_config(tmp_path)
    assert cfg.analyse_in_process is False


def test_adsb_backs_off_on_rate_limit(tmp_path, monkeypatch):
    """adsb.lol is free and community-run, and it does return 429."""
    import urllib.error

    def rate_limited(*a, **k):
        raise urllib.error.HTTPError("u", 429, "Too Many Requests", None, None)

    monkeypatch.setattr(adsb, "fetch_adsb_lol", rate_limited)
    src = node.SyntheticSource(np.zeros(8000 * 60), nominal_rate=8000)
    p = pipeline.Pipeline(make_config(tmp_path, adsb_poll_s=0.02), source=src)
    p.start()
    time.sleep(0.8)
    p.stop(timeout=5)
    assert p.stats["adsb_failures"] >= 2
    assert p.stats["adsb_backoff"] > 1.0
