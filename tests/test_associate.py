"""The ADS-B join, end to end: synthetic audio plus matching synthetic tracks."""

import numpy as np
import pytest

from propdop import adsb, associate, detect, geo, synth

C = 343.0
RX = geo.Receiver(lat=40.88, lon=-73.52, elevation_msl_m=20.0, geoid_offset_m=-33.0, name="test")

# (heard-CPA time, speed, slant CPA, altitude AGL, f0, duration)
AIR_A = (90.0, 90.0, 2000.0, 1800.0, 90.0, 120.0)
AIR_B = (320.0, 130.0, 4000.0, 3600.0, 110.0, 140.0)
VEHICLE = (210.0, 28.0, 150.0, 120.0)


def test_receiver_altitude_datum_is_explicit():
    """The geoid offset must be applied, not silently assumed zero."""
    assert RX.altitude_hae_m == pytest.approx(20.0 - 33.0)
    naive = geo.Receiver(lat=RX.lat, lon=RX.lon, elevation_msl_m=20.0)
    assert naive.altitude_hae_m == pytest.approx(20.0)
    # 33 m of datum error is a 33 m slant error straight up, well above ADS-B's own precision
    assert abs(RX.altitude_hae_m - naive.altitude_hae_m) > 30.0


def test_geodetic_round_trip():
    lat, lon, h = 40.88, -73.52, 1234.5
    ecef = geo.geodetic_to_ecef(lat, lon, h)
    la, lo, hh = geo.ecef_to_geodetic(ecef)
    assert float(la[0]) == pytest.approx(lat, abs=1e-9)
    assert float(lo[0]) == pytest.approx(lon, abs=1e-9)
    assert float(hh[0]) == pytest.approx(h, abs=1e-6)


@pytest.mark.parametrize("speed,slant,agl", [(90, 2000, 1800), (130, 4000, 3600), (45, 800, 600)])
def test_pass_geometry_recovers_what_was_asked_for(speed, slant, agl):
    tr = adsb.synthetic_track(RX, speed, slant, agl, heading_deg=35.0, t_cpa=300.0)
    g = adsb.pass_geometry(tr, RX)
    assert g.speed_mps == pytest.approx(speed, rel=0.01)
    assert g.slant_cpa_m == pytest.approx(slant, rel=0.01)
    # the heard CPA is later than the emission CPA by exactly the propagation delay
    assert g.t_heard_cpa - g.t_cpa_emit == pytest.approx(slant / C, rel=0.02)


def test_pass_geometry_survives_position_jitter():
    """Trajectory smoothing: a dropped or noisy report must not become a feature."""
    clean = adsb.pass_geometry(adsb.synthetic_track(RX, 90, 2000, 1800, t_cpa=300.0), RX)
    noisy = adsb.pass_geometry(
        adsb.synthetic_track(RX, 90, 2000, 1800, t_cpa=300.0, jitter_m=40.0, seed=2), RX
    )
    assert noisy.slant_cpa_m == pytest.approx(clean.slant_cpa_m, rel=0.05)
    assert noisy.speed_mps == pytest.approx(clean.speed_mps, rel=0.15)


def test_distant_traffic_is_not_a_candidate_but_a_jet_overhead_still_is():
    """The two questions the candidate list used to answer with one filter.

    Range is about whether the aircraft could be heard at all, so it removes a track from
    consideration entirely. Engine type is about whether a *miss* means anything, which is a
    question for the denominator -- and answering it here left a detection the detector really
    did make on a turbofan with no track to match, so it was published as a non-cooperative
    aircraft instead.
    """
    jet = adsb.synthetic_track(
        RX, 240, 3000, 2800, t_cpa=100.0, icao24="jet001", engine_class="J"
    )
    heli = adsb.synthetic_track(
        RX, 50, 1500, 1300, t_cpa=150.0, icao24="heli01", engine_class="R"
    )
    far = adsb.synthetic_track(RX, 90, 12000, 11000, t_cpa=200.0, icao24="far001")
    near = adsb.synthetic_track(RX, 90, 2000, 1800, t_cpa=300.0, icao24="near01")
    cands = adsb.audible_candidates([jet, heli, far, near], RX)

    # out of range is out of the join entirely; engine type never is
    assert [g.icao24 for g in cands] == ["jet001", "heli01", "near01"]
    # but only the propeller aeroplane may be counted as a miss
    assert [g.icao24 for g in cands if g.expected_audible] == ["near01"]


def test_a_matched_jet_is_in_neither_half_of_the_detection_rate():
    """Excluding jets from the denominator while counting them in the numerator would score
    the instrument on exactly the traffic it cannot measure."""
    import propdop.store as store_mod

    db = store_mod.Store(":memory:")
    jet = adsb.synthetic_track(
        RX, 240, 3000, 2800, t_cpa=100.0, icao24="jet001", engine_class="J"
    )
    prop = adsb.synthetic_track(
        RX, 90, 2000, 1800, t_cpa=300.0, icao24="prop01", engine_class="P"
    )
    geoms = adsb.audible_candidates([jet, prop], RX)
    for g in geoms:
        db.add_unheard("s", g)
    assert db.detection_rate() == 0.0          # one propeller missed, jet not counted
    assert db.query_one("SELECT COUNT(*) FROM unheard")[0] == 2   # both still recorded


# --------------------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------------------


def build_world(clock_offset_s: float = 0.0):
    """Audio scene plus the ADS-B tracks that produced it, sharing one truth."""
    scene = []
    for t_heard, v, d, _agl, f0, dur in (AIR_A, AIR_B):
        scene.append((t_heard, dict(speed=v, cpa_distance=d, f0=f0, duration=dur)))
    t_v, v_v, d_v, _ = VEHICLE
    scene.append((t_v, dict(speed=v_v, cpa_distance=d_v, f0=120.0, duration=40.0)))
    samples, _ = synth.render_scene(scene, duration=430.0, snr_db=12.0, seed=4)

    tracks = []
    for i, (t_heard, v, d, agl, _f0, _dur) in enumerate((AIR_A, AIR_B)):
        tracks.append(
            adsb.synthetic_track(
                RX,
                v,
                d,
                agl,
                heading_deg=35.0 + 50.0 * i,
                t_cpa=t_heard - d / C - clock_offset_s,
                icao24=f"aaa00{i}",
                type_code="C172" if i == 0 else "BE20",
            )
        )
    # a real pass that the detector will not hear: too far for its tone to carry
    tracks.append(
        adsb.synthetic_track(
            RX, 95, 7500, 7000, t_cpa=150.0, icao24="quiet1", half_span_s=150.0
        )
    )
    return samples, tracks


@pytest.fixture(scope="module")
def joined():
    samples, tracks = build_world()
    dets = detect.detect(samples, 8000, band=(50.0, 300.0), n_harmonics=4)
    geoms = adsb.audible_candidates(tracks, RX)
    return associate.join(dets, geoms), dets, geoms


def test_both_aircraft_are_matched_to_the_right_tracks(joined):
    result, _, _ = joined
    assert len(result.matches) == 2
    matched = {m.geometry.icao24 for m in result.matches}
    assert matched == {"aaa000", "aaa001"}
    for m in result.matches:
        assert m.chi < associate.MAX_CHI
        assert abs(m.dt) < 5.0


def test_the_unheard_pass_is_reported_not_silently_dropped(joined):
    """The reverse direction, which is what makes an unexplained detection interpretable."""
    result, _, _ = joined
    assert [g.icao24 for g in result.unheard] == ["quiet1"]
    # with one of three expected passes missed, the detection rate is a real number
    assert 0.0 < result.detection_rate < 1.0
    assert result.detection_rate == pytest.approx(2 / 3, abs=0.01)


def test_the_vehicle_does_not_become_an_unexplained_aircraft(joined):
    """It is rejected upstream as ground, so it must not reach the unexplained list."""
    result, dets, _ = joined
    assert not any(abs(d.t0_heard - VEHICLE[0]) < 15.0 for d in result.unexplained)
    near = [d for d in dets if abs(d.t0_heard - VEHICLE[0]) < 15.0]
    assert near and not any(d.is_aircraft for d in near)


def test_error_model_is_measured_from_paired_rows(joined):
    """The whole point of the join: the fit's own error bars are ~5x optimistic."""
    result, _, _ = joined
    em = associate.error_model(result)
    assert em["n"] == 2
    for key in ("time_s", "speed_frac", "slant_frac"):
        assert set(em[key]) == {"bias", "median_abs", "p90_abs", "sd"}
    assert em["time_s"]["median_abs"] < 5.0
    assert em["speed_frac"]["median_abs"] < 0.15
    assert em["slant_frac"]["median_abs"] < 0.30


def test_clock_offset_is_recovered_from_the_passes_themselves():
    """Self-calibration: ADS-B gives the true heard-CPA time, so the systematic residual
    *is* the node's clock offset. No GPS needed to discover it."""
    offset = 37.0
    samples, tracks = build_world(clock_offset_s=offset)
    dets = detect.detect(samples, 8000, band=(50.0, 300.0), n_harmonics=4)
    geoms = adsb.audible_candidates(tracks, RX)
    result = associate.join(dets, geoms)
    assert result.clock_offset_s == pytest.approx(offset, abs=4.0)
    assert len(result.matches) == 2


# --------------------------------------------------------------------------------------
# Blade count, which no standard table carries
# --------------------------------------------------------------------------------------


def test_blade_count_is_identifiable_from_repeat_passes():
    """RPM does not exist without blade count, and ICAO Doc 8643 does not provide it.

    But only the correct N puts the implied RPM in a plausible cruise band consistently, so N
    is recoverable from repeat passes of one type.
    """
    rng = np.random.default_rng(0)
    for n_blades, rpm in ((2, 2450.0), (3, 2300.0), (4, 2200.0)):
        f0 = rpm * n_blades / 60.0 * (1.0 + rng.normal(scale=0.004, size=14))
        best, scores = adsb.infer_blade_count(f0)
        assert best == n_blades, scores
        assert adsb.rpm_from_f0(float(np.mean(f0)), best) == pytest.approx(rpm, rel=0.02)


def test_blade_count_refuses_when_the_data_cannot_separate():
    best, _ = adsb.infer_blade_count([80.0, 120.0])
    assert best is None
    best, _ = adsb.infer_blade_count([])
    assert best is None
