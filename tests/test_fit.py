"""Comb-fit checks against synthetic ground truth.

Tolerances here are what the implementation actually achieves on synthetic passes, recorded so
regressions show up. They are *not* claims about field performance: real passes manoeuvre,
change power, and sit under real wind noise.
"""

import numpy as np
import pytest

from propdop import fit, synth

BAND = (50.0, 300.0)


def run(**kw):
    fo = synth.Flyover(seed=kw.pop("seed", 7), **kw)
    samples, truth = synth.render(fo)
    result = fit.fit_comb(samples, fo.sample_rate, band=BAND, n_harmonics=4)
    return result, truth


CASES = [
    ("clean", {}),
    ("snr10", {"snr_db": 10}),
    ("snr0", {"snr_db": 0}),
    ("snr-6", {"snr_db": -6}),
    ("close600", {"cpa_distance": 600, "snr_db": 20}),
    ("far5km", {"cpa_distance": 5000, "snr_db": 20}),
    ("fast130", {"speed": 130, "snr_db": 20}),
    ("slow60", {"speed": 60, "snr_db": 20}),
    ("f0_140", {"f0": 140.0, "snr_db": 20}),
    ("f0_250", {"f0": 250.0, "snr_db": 20}),
    ("two_harmonics", {"harmonic_amplitudes": (1.0, 0.4), "snr_db": 20}),
]


@pytest.mark.parametrize("label,kw", CASES, ids=[c[0] for c in CASES])
def test_recovers_parameters(label, kw):
    r, t = run(**kw)
    assert r.converged
    assert r.f0 == pytest.approx(t["f0"], rel=0.02)
    assert r.speed == pytest.approx(t["speed"], rel=0.10)
    assert r.cpa_distance == pytest.approx(t["cpa_distance"], rel=0.25)
    assert r.t0_heard == pytest.approx(t["duration"] / 2.0, abs=3.0)


@pytest.mark.parametrize("label,kw", CASES, ids=[c[0] for c in CASES])
def test_epsilon_is_small_and_consistent_with_f0_error(label, kw):
    r, t = run(**kw)
    # epsilon is the master precision parameter; on synthetics it should stay well under 2%
    assert 0.0 < r.epsilon < 0.02
    # and it should be in the same league as the actual fractional f0 error
    f0_err = abs(r.f0 - t["f0"]) / t["f0"]
    assert f0_err < max(10.0 * r.epsilon, 0.005)


# A live station passes an absolute unix time as `t_offset`; the AeroSonicDB harness passes
# none at all. That one argument was the difference between 68.2% detection on the corpus and
# 13 consecutive unmatched passes on the deployed station, because `least_squares` runs with
# `x_scale=1.0` and its step tolerance scales with ||x|| -- which an absolute t0 dominates.
# The solver stopped after one step and still reported success.
#
# Measured on a retained near-miss clip, same audio and band: 66.5 m/s at 1791 m in local
# time against 25.7 m/s at 150 m in absolute time, with ADS-B truth at 66.3 m/s and 1835 m.
# So assert the invariant rather than the symptom: t_offset labels the time axis, so it must
# move t0_heard by exactly itself and leave every other quantity untouched.
LIVE_EPOCH = 1_794_114_900.0


@pytest.mark.parametrize(
    "label,kw", [c for c in CASES if c[0] in {"clean", "snr0", "far5km"}],
    ids=["clean", "snr0", "far5km"],
)
def test_fit_is_independent_of_the_time_origin(label, kw):
    fo = synth.Flyover(seed=kw.pop("seed", 7), **kw)
    samples, _ = synth.render(fo)
    local = fit.fit_comb(samples, fo.sample_rate, band=BAND, n_harmonics=4, t_offset=0.0)
    live = fit.fit_comb(
        samples, fo.sample_rate, band=BAND, n_harmonics=4, t_offset=LIVE_EPOCH
    )
    assert live.t0_heard == pytest.approx(local.t0_heard + LIVE_EPOCH, abs=1e-3)
    assert live.f0 == pytest.approx(local.f0, rel=1e-9)
    assert live.speed == pytest.approx(local.speed, rel=1e-9)
    assert live.cpa_distance == pytest.approx(local.cpa_distance, rel=1e-9)
    assert live.epsilon == pytest.approx(local.epsilon, rel=1e-9)
    # comb contrast is computed against the ridge, so it broke the same way and silently:
    # the station logged "contrast -0.19" for a pass that matches ADS-B to 2.4%.
    assert live.notes["comb_score"] == pytest.approx(local.notes["comb_score"], rel=1e-9)


def test_absolute_time_does_not_fake_convergence():
    """The failure reported success, which is what let it run unnoticed for a whole session."""
    fo = synth.Flyover(seed=7)
    samples, truth = synth.render(fo)
    r = fit.fit_comb(
        samples, fo.sample_rate, band=BAND, n_harmonics=4, t_offset=LIVE_EPOCH
    )
    assert r.converged
    assert r.speed == pytest.approx(truth["speed"], rel=0.10)
    assert r.cpa_distance == pytest.approx(truth["cpa_distance"], rel=0.25)


def test_close_passes_are_harder_than_distant_ones():
    """The sharp sweep of a close pass costs frequency resolution. Physical, not a bug."""
    near, _ = run(cpa_distance=400, snr_db=20)
    far, _ = run(cpa_distance=5000, snr_db=20)
    assert near.epsilon > far.epsilon


def test_rpm_and_its_uncertainty():
    r, t = run(snr_db=20)
    assert r.rpm(2) == pytest.approx(t["rpm"], rel=0.02)
    # RPM precision is exactly epsilon, by construction
    assert r.rpm_uncertainty(2) == pytest.approx(r.rpm(2) * r.epsilon)
    assert r.rpm_uncertainty(2) < 60.0  # ~2% of 2700 at worst


def test_amplitude_tracks_are_produced_for_each_harmonic():
    r, t = run(snr_db=20)
    assert r.n_harmonics >= 3
    for k in r.harmonics:
        assert r.amp_tracks[k].shape == r.track_times.shape
        assert r.freq_tracks[k].shape == r.track_times.shape
        assert r.log_amp_fluct[k].shape == r.track_times.shape
        assert np.all(r.amp_tracks[k] >= 0.0)
    # harmonic k should track near k*f0
    for k in r.harmonics:
        assert np.median(r.freq_tracks[k]) == pytest.approx(k * r.f0, rel=0.25)


def test_harmonic_product_resolves_the_octave_ambiguity():
    """The whole reason the fundamental is found by comb score rather than a single ridge."""
    fo = synth.Flyover(seed=7, snr_db=30)
    samples, truth = synth.render(fo)
    freqs, times, wmag = fit.whitened_spectrogram(
        samples, fo.sample_rate, window_s=1.0, band=(25.0, 1500.0)
    )
    cand, hps = fit.harmonic_product_spectrogram(freqs, wmag, BAND, 4)
    # at the loudest frame the comb score must peak on the fundamental, not a harmonic
    j = int(np.argmax(hps.max(axis=0)))
    peak = float(cand[int(np.argmax(hps[:, j]))])
    f_true = np.interp(
        times[j], np.arange(truth["f_heard"].size) / fo.sample_rate, truth["f_heard"]
    )
    assert peak == pytest.approx(float(f_true), rel=0.08)


def test_comb_score_prefers_the_true_parameters():
    fo = synth.Flyover(seed=5, snr_db=20)
    samples, truth = synth.render(fo)
    freqs, times, wmag = fit.whitened_spectrogram(
        samples, fo.sample_rate, window_s=1.0, band=(25.0, 1500.0)
    )
    rt = times
    rs = np.ones_like(times)
    true_p = (truth["f0"], truth["speed"], truth["cpa_distance"], fo.duration / 2.0)
    good = fit.comb_score(freqs, times, wmag, true_p, rt, rs, 4)
    for wrong in (
        (truth["f0"] * 1.15, *true_p[1:]),
        (truth["f0"] * 0.87, *true_p[1:]),
    ):
        assert good > fit.comb_score(freqs, times, wmag, wrong, rt, rs, 4)


# --------------------------------------------------------------------------------------
# Scintillation: the novel product, and the confound it must survive
# --------------------------------------------------------------------------------------


def test_no_turbulence_reports_nothing():
    """Bias correction must make the estimator refuse, not invent a number.

    Additive noise alone contributes 1/(2*SNR) to log-amplitude variance, and because higher
    harmonics are weaker that bias rises with frequency and imitates the turbulence scaling.
    """
    r, _ = run(c_eff_sq=0.0, snr_db=30)
    s = r.scintillation_scaling()
    assert s["n_harmonics"] == 0
    assert np.isnan(s["exponent"])


@pytest.mark.parametrize("c_eff_sq", [1.6e-8, 1.0e-7])
def test_recovers_the_f_to_the_7_6_scaling(c_eff_sq):
    """The turbulence signature: log-amplitude variance rising as f^(7/6) across the comb.

    Source and geometry are identical across harmonics of one tone, so a frequency dependence
    can only be propagation.

    MEASURED, 12 seeds: single-pass exponent mean 1.33, sd 0.20, range 0.95-1.62 against a
    prediction of 1.17. So a single pass cannot pin the exponent -- consistent with everything
    else here needing averaging -- and there is a residual systematic bias of about +0.15 that
    pooling does *not* remove. Candidate causes: the injected correlation time goes as
    f^(-1/2), so the polynomial detrend and the 4 Hz low-pass do not treat harmonics
    identically. Tolerances below encode that honestly rather than hiding it.
    """
    exps = []
    for seed in range(8):
        r, _ = run(c_eff_sq=c_eff_sq, snr_db=30, seed=seed)
        s = r.scintillation_scaling()
        if s["n_harmonics"] >= 3 and np.isfinite(s["exponent"]):
            exps.append(s["exponent"])
    assert len(exps) >= 6
    # every single pass lands in the turbulence neighbourhood, nowhere near engine wander
    assert all(0.6 < e < 2.0 for e in exps)
    # and the pooled estimate is close, allowing for the known +0.15 bias
    assert float(np.mean(exps)) == pytest.approx(7 / 6, abs=0.3)
    assert float(np.std(exps)) < 0.4


def test_stronger_turbulence_gives_larger_variance():
    weak, _ = run(c_eff_sq=1.6e-8, snr_db=30)
    strong, _ = run(c_eff_sq=1.0e-7, snr_db=30)
    assert (
        strong.log_amp_variance()[1] > 2.0 * weak.log_amp_variance()[1]
    )


def test_rpm_wander_is_not_mistaken_for_turbulence():
    """The confound that would otherwise fake a turbulence measurement.

    Engine wander moves every harmonic by the same fraction and leaves amplitude alone, so its
    amplitude-variance exponent lands nowhere near 7/6.
    """
    exps = []
    for seed in range(6):
        r, _ = run(rpm_wander_frac=0.010, snr_db=30, c_eff_sq=0.0, seed=seed)
        s = r.scintillation_scaling()
        if s["n_harmonics"] >= 2 and np.isfinite(s["exponent"]):
            exps.append(s["exponent"])
    assert exps, "wander case produced no measurable harmonics at all, which is also a pass"
    # turbulence sits at 1.0-1.6; wander lands far above, so the exponent separates them
    assert float(np.mean(exps)) > 2.2


def test_frequency_coherence_flags_engine_wander():
    """Supporting, not primary, evidence -- see the caveat in SCINTILLATION.md.

    Turbulent phase perturbation also scales with harmonic number, so it too produces
    partially correlated fractional frequency error. Wander sits high and reliably; turbulence
    is merely usually lower.
    """
    wander, _ = run(rpm_wander_frac=0.003, snr_db=30)
    assert wander.frequency_coherence() > 0.85


def test_fresnel_scale_sets_the_scintillation_timescale():
    """Sanity: the correlation time is ~1-2 s, which is why the detrend must be modelled
    rather than high-passed at 0.1 Hz."""
    from propdop import physics

    r_f = physics.fresnel_scale_m(90.0, 2000.0)
    tau = r_f / (90.0 / 2.0)
    assert 0.5 < tau < 4.0


# --------------------------------------------------------------------------------------
# Blind path: rejecting ground sources
# --------------------------------------------------------------------------------------

GROUND_AND_AIR = [
    ("car 25 @ 60 m", {"speed": 25, "cpa_distance": 60, "f0": 120.0}, False),
    ("car 30 @ 120 m", {"speed": 30, "cpa_distance": 120, "f0": 120.0}, False),
    ("truck 28 @ 200 m", {"speed": 28, "cpa_distance": 200, "f0": 90.0}, False),
    ("loco 20 @ 50 m", {"speed": 20, "cpa_distance": 50, "f0": 200.0}, False),
    ("loco 20 @ 300 m", {"speed": 20, "cpa_distance": 300, "f0": 200.0}, False),
    ("slow air 45 @ 800 m", {"speed": 45, "cpa_distance": 800, "f0": 90.0}, True),
    ("GA 60 @ 1.5 km", {"speed": 60, "cpa_distance": 1500, "f0": 90.0}, True),
    ("GA 90 @ 2 km", {"speed": 90, "cpa_distance": 2000, "f0": 90.0}, True),
    ("turboprop 130 @ 4 km", {"speed": 130, "cpa_distance": 4000, "f0": 90.0}, True),
    ("low GA 50 @ 400 m", {"speed": 50, "cpa_distance": 400, "f0": 90.0}, True),
]


@pytest.mark.parametrize(
    "label,kw,is_aircraft", GROUND_AND_AIR, ids=[c[0] for c in GROUND_AND_AIR]
)
def test_rejects_ground_sources(label, kw, is_aircraft):
    """The blind path's real job: telling a locomotive from a light aircraft.

    A speed gate alone is not enough -- a source at 50 m doing 20 m/s completes its sweep in
    2.5 s and gets fitted at 101 m/s, which is the documented locomotive false positive. The
    fit quality and resolvability checks are what catch those.
    """
    fo = synth.Flyover(seed=3, snr_db=20, duration=90.0, **kw)
    samples, _ = synth.render(fo)
    r = fit.fit_comb(samples, fo.sample_rate, band=(60.0, 300.0), n_harmonics=3)
    verdict, reason = fit.plausible_aircraft(r)
    assert verdict == is_aircraft, f"{label}: {reason}"


def test_max_epsilon_stays_below_the_misfit_ground_source():
    """The epsilon gate is load-bearing for ground rejection, not only for precision.

    A car at 30 m/s and 120 m misfits as "48 m/s at 359 m", which clears both the speed gate
    and the resolvability gate, so epsilon is the only check standing between it and a
    reported flyover. Loosening the gate to 0.045 to recover piston aircraft broke exactly
    this, and no sweep over an aircraft dataset could have revealed it -- AeroSonicDB has no
    labelled ground vehicles.

    Asserted as a relationship rather than a number, so a future recalibration has to confront
    the constraint instead of silently stepping over it.
    """
    fo = synth.Flyover(seed=3, snr_db=20, duration=90.0,
                       speed=30, cpa_distance=120, f0=120.0)
    samples, _ = synth.render(fo)
    r = fit.fit_comb(samples, fo.sample_rate, band=(60.0, 300.0), n_harmonics=3)

    assert r.speed > fit.MIN_AIRCRAFT_SPEED, "must not be rescued by the speed gate"
    assert r.cpa_distance / r.speed > fit.MIN_TRANSITION_S, "nor by resolvability"
    assert fit.MAX_EPSILON < r.epsilon, (
        f"MAX_EPSILON {fit.MAX_EPSILON} must stay below this misfit's "
        f"epsilon {r.epsilon:.4f}, or a car is reported as an aircraft"
    )


def _good_fit():
    fo = synth.Flyover(seed=1, snr_db=20, duration=90.0,
                       speed=90, cpa_distance=2000, f0=90.0)
    samples, _ = synth.render(fo)
    r = fit.fit_comb(samples, fo.sample_rate, band=(60.0, 300.0), n_harmonics=3)
    assert fit.plausible_aircraft(r)[0], "fixture must be accepted before it is perturbed"
    return r


def test_rejects_a_fit_pinned_at_the_speed_bound():
    """A parameter sitting on its bound is where the search stopped, not a measurement.

    The deployed station produced exactly this: "211.8 m/s at 16.4 km", unmatched to any ADS-B
    track, which would have been published as a non-cooperative aircraft.
    """
    import dataclasses

    r = _good_fit()
    pinned = dataclasses.replace(r, speed=fit.SPEED_BOUND_HI)
    ok, why = fit.plausible_aircraft(pinned)
    assert not ok and "pinned" in why, why


def test_speed_floor_sits_between_the_truck_and_the_confirmed_aircraft():
    """Assert the relationship, not the number, so a recalibration cannot silently break it.

    Only one synthetic ground source depends on this gate at all -- a truck at 28 m/s and
    200 m, which fits cleanly at ~26 m/s. Everything else is caught on fit quality,
    resolvability or a solver bound. The upper constraint is real traffic: the station's first
    ADS-B-confirmed pass fitted at 32.1 m/s against an ADS-B truth of 32.7, and the old 35.8
    floor would have thrown it away.
    """
    fo = synth.Flyover(seed=3, snr_db=20, duration=90.0,
                       speed=28, cpa_distance=200, f0=90.0)
    samples, _ = synth.render(fo)
    truck = fit.fit_comb(samples, fo.sample_rate, band=(60.0, 300.0), n_harmonics=3)

    assert truck.speed < fit.MIN_AIRCRAFT_SPEED, (
        f"the floor must reject the truck, which fits at {truck.speed:.1f} m/s"
    )
    assert fit.MIN_AIRCRAFT_SPEED < 32.1, (
        "the floor must admit the station's ADS-B-confirmed pass, fitted at 32.1 m/s"
    )
    ok, why = fit.plausible_aircraft(truck)
    assert not ok and "too slow" in why, why


def test_every_solver_bound_is_reported_as_a_failed_fit():
    """A wrong reason is worse than no reason: it sends the next person to the wrong threshold.

    The station showed "too slow for an aircraft (20 m/s)" and "transition unresolvable" for
    fits that had stopped on the speed floor and the 50 m distance floor. Both read as facts
    about the source; both were facts about the optimiser. Every bound now says so, and says
    which one, because the pinning check runs before the gates that phrase themselves as
    statements about the aircraft.
    """
    import dataclasses

    r = _good_fit()
    for field, value, name in (
        ("speed", fit.SPEED_BOUND_LO, "lower speed bound"),
        ("speed", fit.SPEED_BOUND_HI, "upper speed bound"),
        ("cpa_distance", fit.DIST_BOUND_LO, "distance floor"),
        ("cpa_distance", fit.DIST_BOUND_HI, "distance ceiling"),
    ):
        ok, why = fit.plausible_aircraft(dataclasses.replace(r, **{field: value}))
        assert not ok, f"{name} should be rejected"
        assert name in why, f"expected {name!r} in {why!r}"


def test_the_solver_box_and_the_gate_cannot_drift_apart():
    """The gate is only meaningful if it names the same numbers the optimiser was given."""
    import inspect

    src = inspect.getsource(fit.fit_straight_pass)
    for const in ("SPEED_BOUND_LO", "SPEED_BOUND_HI", "DIST_BOUND_LO", "DIST_BOUND_HI"):
        assert const in src, f"{const} should set the solver's box, not a duplicated literal"


def test_rejects_a_detection_beyond_the_audibility_radius():
    """The instrument must not report hearing what it refuses to expect hearing."""
    import dataclasses

    from propdop import physics

    r = _good_fit()
    far = dataclasses.replace(r, cpa_distance=physics.AUDIBILITY_RADIUS_M * 2.0)
    ok, why = fit.plausible_aircraft(far)
    assert not ok and "audibility radius" in why, why


def test_both_halves_of_the_join_use_one_audibility_radius():
    """`audible_candidates` and `plausible_aircraft` must not disagree about what is audible.

    They did: the ADS-B half excluded anything past 8 km from "should have been heard" while
    the acoustic half accepted a detection at 16.4 km and reported it as unexplained. One
    shared constant is what stops that returning.
    """
    import dataclasses

    from propdop import adsb, geo, physics

    rx = geo.Receiver(lat=51.23, lon=-0.98, elevation_msl_m=68.0, geoid_offset_m=46.0)
    radius = physics.AUDIBILITY_RADIUS_M

    inside = adsb.synthetic_track(rx, 90.0, radius * 0.5, 1500.0, t_cpa=0.0, icao24="near01")
    outside = adsb.synthetic_track(rx, 90.0, radius * 2.0, 1500.0, t_cpa=0.0, icao24="far001")
    names = {g.icao24 for g in adsb.audible_candidates([inside, outside], rx)}
    assert names == {"near01"}, names

    r = _good_fit()
    assert fit.plausible_aircraft(dataclasses.replace(r, cpa_distance=radius * 0.5))[0]
    assert not fit.plausible_aircraft(dataclasses.replace(r, cpa_distance=radius * 2.0))[0]
