"""The ADS-B join, end to end: synthetic audio plus matching synthetic tracks."""

import warnings

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


@pytest.mark.parametrize("jitter_m", [10.0, 25.0])
def test_adsb_truth_is_better_than_the_tolerance_it_is_compared_against(jitter_m):
    """The truth side of the join has to be better than the join's own tolerances.

    `SIGMA_SPEED_FRAC` is 10 %, so an ADS-B speed carrying a comparable error turns chi into a
    comparison between two noisy numbers and the error model into a measurement of the spline.
    The trajectory spline was allowed a hard-coded 4 m^2 of misfit per point -- a 2 m RMS that
    ADS-B does not deliver -- which cost 10-25 % on speed at realistic jitter and filled the
    station's log with FITPACK's "s too small".

    Asserted on the distribution over seeds, not per seed: the tail is set by where the noise
    happens to land around CPA and a single draw says little. Pinned against the join's
    tolerances rather than against whatever the implementation achieves, which is how
    `test_pass_geometry_survives_position_jitter` above came to record the defect instead of
    catching it -- it allows 15 % on a quantity the join needs to 10 %.
    """
    speed, slant = [], []
    for seed in range(20):
        tr = adsb.synthetic_track(RX, 70.0, 1500.0, 1200.0, t_cpa=300.0,
                                  jitter_m=jitter_m, seed=seed)
        g = adsb.pass_geometry(tr, RX)
        speed.append(abs(g.speed_mps - 70.0) / 70.0)
        slant.append(abs(g.slant_cpa_m - 1500.0) / 1500.0)

    assert np.median(speed) < 0.1 * associate.SIGMA_SPEED_FRAC
    assert np.percentile(speed, 90) < 0.5 * associate.SIGMA_SPEED_FRAC
    assert np.median(slant) < 0.1 * associate.SIGMA_SLANT_FRAC
    assert max(slant) < 0.5 * associate.SIGMA_SLANT_FRAC


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


def test_a_screened_out_detection_can_still_be_matched():
    """ADS-B is better evidence about identity than a self-assessed fit residual.

    The deployed station rejected a Cirrus SR22T as "poor fit (epsilon 3.13%)" and filed it as
    a miss. Refitted, it reproduces that aircraft's own track at chi 0.11 against a threshold
    of 3.0. Screening before the join discards exactly this case, and it discards it silently:
    the detection becomes a miss and the aircraft becomes unheard, so one event damages both
    products at once.
    """
    track = adsb.synthetic_track(
        RX, 66.3, 1835, 1700, t_cpa=200.0, icao24="scr001", type_code="S22T"
    )
    geoms = adsb.audible_candidates([track], RX)
    assert len(geoms) == 1
    g = geoms[0]

    marginal = detect.Detection(
        t0_heard=g.t_heard_cpa + 0.1,
        f0=82.6,
        speed=66.5,
        cpa_distance=1791.0,
        template_score=46.3,
        is_aircraft=False,
        reason="poor fit (epsilon 3.13%, contrast -0.08)",
    )

    screened_first = associate.join([marginal], geoms, aircraft_only=True)
    assert screened_first.matches == []
    assert screened_first.unheard == geoms        # and the aircraft is recorded as a miss

    joined_first = associate.join([marginal], geoms, aircraft_only=False)
    assert len(joined_first.matches) == 1
    assert joined_first.matches[0].geometry.icao24 == "scr001"
    assert joined_first.matches[0].chi < associate.MAX_CHI
    assert joined_first.unheard == []


def test_screening_still_gates_the_blind_product():
    """`unexplained` is the one place the screen is load-bearing: nothing else vouches for it."""
    ground = detect.Detection(
        t0_heard=500.0, f0=120.0, speed=48.0, cpa_distance=359.0,
        template_score=22.0, is_aircraft=False, reason="poor fit (epsilon 3.91%)",
    )
    result = associate.join([ground], [], aircraft_only=False)
    assert result.matches == []
    assert result.unexplained == [ground]
    # the worker applies the screen to exactly this list, and only to this list
    assert [d for d in result.unexplained if d.is_aircraft] == []


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


def _bare_detection(t0, speed, cpa, f0=90.0):
    return detect.Detection(t0_heard=t0, f0=f0, speed=speed, cpa_distance=cpa,
                            template_score=30.0, is_aircraft=True, reason="ok")


def test_one_detection_cannot_invent_a_clock_offset():
    """A single residual plus a free offset is an exactly degenerate fit.

    The scan slides the offset until that one detection's dt is zero, which removes the time
    term from `_chi` and lets geometry alone decide the association. On the deployed station
    this matched an aircraft 96 s away at chi 1.16, with the estimator reporting -96.4 s.
    """
    g = adsb.pass_geometry(adsb.synthetic_track(RX, 90.0, 2000.0, 1800.0, t_cpa=400.0), RX)
    det = _bare_detection(g.t_heard_cpa + 96.0, g.speed_mps, g.slant_cpa_m)

    assert associate.estimate_clock_offset([det], [g]) == 0.0
    assert associate.join([det], [g]).matches == []
    # ...and the detection is still reported, as an unexplained one rather than a bad match
    assert len(associate.join([det], [g]).unexplained) == 1


def test_a_real_offset_is_still_recovered_once_enough_passes_agree():
    """The guard must not cost the station its self-calibration, which is the point of it."""
    offset = 40.0
    geoms, dets = [], []
    for i, (v, d, agl) in enumerate([(90, 2000, 1800), (70, 1200, 900), (110, 3000, 2700),
                                     (80, 1600, 1400)]):
        g = adsb.pass_geometry(
            adsb.synthetic_track(RX, v, d, agl, t_cpa=200.0 + 600.0 * i,
                                 icao24="ac%04d" % i), RX)
        geoms.append(g)
        dets.append(_bare_detection(g.t_heard_cpa + offset, g.speed_mps, g.slant_cpa_m))

    assert associate.estimate_clock_offset(dets, geoms) == pytest.approx(offset, abs=1.5)
    assert len(associate.join(dets, geoms).matches) == 4


def test_a_cpa_the_track_never_reached_is_not_a_pass():
    """A minimum on the endpoint of an open track is not a closest approach.

    The aircraft was still inbound when the snapshots ran out, so the "CPA" is wherever the
    observation stopped -- always too far and too early. The station filed a Bonanza unheard
    at a predicted 3017 m and then heard the real 828 m pass 36 s later, putting one aeroplane
    in both halves of its own detection rate.
    """
    full = adsb.synthetic_track(RX, 70.0, 900.0, 700.0, t_cpa=300.0, half_span_s=120.0,
                                icao24="cc0003")
    # the same aircraft as the window saw it: snapshots stop 40 s before it gets there
    inbound = adsb.Track(
        icao24=full.icao24, times=full.times[full.times <= 260.0],
        lat=full.lat[full.times <= 260.0], lon=full.lon[full.times <= 260.0],
        alt_hae_m=full.alt_hae_m[full.times <= 260.0], type_code=full.type_code,
        registration=full.registration, engine_class=full.engine_class,
    )

    g_full = adsb.pass_geometry(full, RX)
    g_part = adsb.pass_geometry(inbound, RX)
    assert g_full.cpa_bracketed is True
    assert g_part.cpa_bracketed is False
    # ...and it is wrong in the direction that matters: further away, and earlier
    assert g_part.slant_cpa_m > g_full.slant_cpa_m * 1.3
    assert g_part.t_heard_cpa < g_full.t_heard_cpa

    # so it is offered to neither half of the join
    assert [g.icao24 for g in adsb.audible_candidates([full], RX)] == ["cc0003"]
    assert adsb.audible_candidates([inbound], RX) == []


def _snapshots_holding_each_fix(hold, jitter_m=8.0, seed=0, n=60, poll=4.0):
    """Polling a feed that re-reports a fix until the aircraft makes a new one.

    `t` is `now - seen_pos`, so every repeat of one fix arrives with the identical timestamp
    -- which is what makes them recognisable as the same measurement.
    """
    tr = adsb.synthetic_track(RX, 70.0, 1500.0, 1200.0, t_cpa=300.0, jitter_m=jitter_m,
                              seed=seed, half_span_s=120.0)
    snaps = []
    for k in range(n):
        now = 180.0 + k * poll
        fix_t = 180.0 + (k // hold) * hold * poll
        j = int(np.argmin(np.abs(tr.times - fix_t)))
        snaps.append({"now": now, "aircraft": [{
            "hex": "aa0001", "t": "C172", "r": "N1",
            "lat": float(tr.lat[j]), "lon": float(tr.lon[j]),
            "alt_geom": float(tr.alt_hae_m[j] / 0.3048),
            "seen_pos": now - fix_t}]})
    return snaps


@pytest.mark.parametrize("hold", [1, 2, 3, 5, 8])
def test_a_repeated_position_report_is_one_measurement_not_several(hold):
    """Kept, repeats are a staircase, and the speed is read off a derivative.

    A fix still being broadcast when the next poll lands arrives again unchanged. Counting it
    twice makes the trajectory a sequence of flat stretches joined by steps; the smoothing
    spline then either chases it or is told to ignore it, and the speed comes out wrong by
    tens of per cent -- measured at -33% and +332% before these were dropped.
    """
    tracks = adsb.tracks_from_readsb(_snapshots_holding_each_fix(hold))
    assert len(tracks) == 1
    track = tracks[0]
    assert len(track) == -(-60 // hold), "repeats of one fix were kept"
    g = adsb.pass_geometry(track, RX)
    assert abs(g.speed_mps - 70.0) / 70.0 < 0.02


def test_each_axis_is_smoothed_to_its_own_noise():
    """Altitude and position do not share an error distribution.

    Geometric altitude arrives in 25 ft steps, so on a level-flight track its scatter is near
    zero while the horizontal scatter is tens of metres. One figure for all three applied one
    axis's noise to another -- on live traffic, 0.05 m east against 9.2 m north on one track,
    50 m east against 1.1 m up on another.
    """
    rng = np.random.default_rng(0)
    t = np.arange(0.0, 240.0)
    east = 1200.0 + rng.normal(0, 40.0, t.size)          # noisy
    north = 70.0 * (t - 120.0)                            # clean
    up = np.round(np.full(t.size, 900.0) / 7.62) * 7.62   # quantised to 25 ft, so flat

    sig = [adsb._position_sigma_m(x) for x in (east, north, up)]
    assert sig[0] > 20.0, "the noisy axis has to be recognised as noisy"
    assert sig[1] < sig[0] / 4.0, "the clean axis must not inherit the noisy one's scatter"
    assert sig[2] <= adsb.POSITION_SIGMA_FLOOR_M * 1.5, "a flat quantised axis is not noisy"


def test_the_smoothing_floor_is_the_feeds_own_quantisation():
    """Below the quantisation step there is nothing to resolve, and asking FITPACK for a
    misfit it cannot reach is what produced "s too small" in the station's journal."""
    assert adsb.POSITION_SIGMA_FLOOR_M == pytest.approx(25.0 * 0.3048 / 12 ** 0.5, rel=1e-9)
    # a level-flight altitude column is the same figure over and over; its MAD is zero, and
    # the estimator must not report a precision finer than the step that produced it
    flat = np.full(60, 900.0)
    assert adsb._position_sigma_m(flat) <= adsb.POSITION_SIGMA_FLOOR_M * 1.5


def test_a_quantised_track_does_not_make_fitpack_complain():
    """The warning this whole estimator exists to remove, reproduced from the shape of real
    data rather than from Gaussian noise -- which is why the first fix did not end it."""
    for hold in (1, 2, 3, 5, 8):
        tracks = adsb.tracks_from_readsb(_snapshots_holding_each_fix(hold))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            adsb.pass_geometry(tracks[0], RX)
        spline = [w for w in caught if "spline" in str(w.message)
                  or "s too small" in str(w.message).replace("\n", " ")]
        assert spline == [], f"hold={hold}: {[str(w.message) for w in spline]}"
