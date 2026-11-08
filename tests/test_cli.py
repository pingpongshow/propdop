"""CLI surface -- what the container actually runs."""

import json

import pytest
import soundfile as sf

from propdop import cli, synth


@pytest.fixture(scope="module")
def one_pass(tmp_path_factory):
    d = tmp_path_factory.mktemp("audio")
    fo = synth.Flyover(seed=7, snr_db=15, speed=90, cpa_distance=2000, f0=90.0, duration=120.0)
    x, truth = synth.render(fo)
    path = d / "one_pass.wav"
    sf.write(path, x, fo.sample_rate)
    return path, truth


def test_fit_reports_parameters_and_rpm(one_pass, capsys):
    path, truth = one_pass
    assert cli.main(["fit", str(path), "--blades", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["f0_hz"] == pytest.approx(truth["f0"], rel=0.02)
    assert out["speed_mps"] == pytest.approx(truth["speed"], rel=0.10)
    assert out["aircraft"] is True
    # RPM only appears when blade count is supplied, because f0 alone cannot give it
    assert out["rpm"] == pytest.approx(truth["rpm"], rel=0.02)
    assert out["rpm_uncertainty"] < 60.0


def test_fit_omits_rpm_without_blade_count(one_pass, capsys):
    path, _ = one_pass
    assert cli.main(["fit", str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "rpm" not in out


def test_detect_finds_aircraft_and_rejects_the_vehicle(tmp_path, capsys):
    scene = [
        (90.0, dict(speed=90, cpa_distance=2000, f0=90.0, duration=120.0)),
        (210.0, dict(speed=28, cpa_distance=150, f0=120.0, duration=40.0)),
    ]
    x, _ = synth.render_scene(scene, duration=300.0, snr_db=12.0, seed=4)
    path = tmp_path / "scene.wav"
    sf.write(path, x, 8000)

    assert cli.main(["detect", str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["n_aircraft"] == 1
    slow = [d for d in out["detections"] if abs(d["t0_heard"] - 210.0) < 15.0]
    assert slow and not any(d["aircraft"] for d in slow)


def test_ingest_persists_and_status_reports(tmp_path, one_pass, capsys):
    path, _ = one_pass
    station = tmp_path / "station.json"
    station.write_text(
        json.dumps(
            {
                "station_id": "alpha",
                "lat": 40.8764,
                "lon": -73.5258,
                "elevation_msl_m": 20.0,
                "geoid_offset_m": -33.0,
            }
        )
    )
    db = tmp_path / "passes.sqlite"
    assert cli.main(["--db", str(db), "ingest", str(path), "--station", str(station)]) == 0
    ing = json.loads(capsys.readouterr().out)
    assert ing["station"] == "alpha"
    # no ADS-B given, so the pass is stored as blind and unexplained
    assert ing["total_passes"] >= 1

    assert cli.main(["--db", str(db), "status"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["passes"] >= 1
    assert st["stations"] == ["alpha"]


def test_station_config_requires_the_geoid_offset(tmp_path, one_pass):
    """Defaulting it would be a silent tens-of-metres slant error, so it must be fatal."""
    path, _ = one_pass
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"lat": 40.0, "lon": -73.0, "elevation_msl_m": 10.0}))
    with pytest.raises(SystemExit, match="geoid_offset_m"):
        cli.main(["--db", str(tmp_path / "x.db"), "ingest", str(path), "--station", str(bad)])


def test_export_and_import_round_trip(tmp_path, one_pass, capsys):
    path, _ = one_pass
    db_a, db_b = tmp_path / "a.sqlite", tmp_path / "b.sqlite"
    station = tmp_path / "s.json"
    station.write_text(
        json.dumps(
            {
                "station_id": "alpha",
                "lat": 40.0,
                "lon": -73.0,
                "elevation_msl_m": 10.0,
                "geoid_offset_m": -33.0,
            }
        )
    )
    cli.main(["--db", str(db_a), "ingest", str(path), "--station", str(station)])
    capsys.readouterr()

    reports = tmp_path / "reports.ndjson"
    assert cli.main(["--db", str(db_a), "export", str(reports)]) == 0
    assert json.loads(capsys.readouterr().out)["records"] >= 2

    assert cli.main(["--db", str(db_b), "import", str(reports)]) == 0
    counts = json.loads(capsys.readouterr().out)
    assert counts["station"] == 1 and counts["pass"] >= 1
