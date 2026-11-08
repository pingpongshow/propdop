"""Blind detector checks -- Path B, which must work with no ADS-B hint at all."""

import numpy as np
import pytest

from propdop import detect, fit, synth


def test_log_frequency_axis_is_uniform_in_log():
    freqs = np.linspace(0.0, 2000.0, 1001)
    wmag = np.ones((freqs.size, 20))
    log_f, lmap = detect.log_frequency_map(freqs, wmag, bins_per_octave=48, band=(50.0, 1600.0))
    ratios = log_f[1:] / log_f[:-1]
    assert np.allclose(ratios, ratios[0], rtol=1e-9)
    assert ratios[0] == pytest.approx(2.0 ** (1 / 48))
    assert lmap.shape == (log_f.size, 20)


def test_flyby_shape_is_independent_of_f0_on_a_log_axis():
    """The change of variables the whole detector rests on.

    In log-frequency the curve shape depends only on v/c; f0 is a pure vertical translation
    and d/v a pure horizontal scale. If this were false, the template bank would need a third
    dimension over f0 and the harmonic comb would not be a fixed pattern.
    """
    from propdop import physics

    c, v, t0 = 343.0, 90.0, 0.0
    for d in (800.0, 4000.0):
        t = np.linspace(-3.0 * d / v, 3.0 * d / v, 400)
        shapes = []
        for f0 in (70.0, 140.0, 280.0):
            f = physics.straight_pass_doppler(t + d / c, f0, v, d, c)
            shapes.append(np.log2(f) - np.log2(f0))
        for s in shapes[1:]:
            assert np.allclose(s, shapes[0], atol=1e-12)
        # and the horizontal scale really is d/v: the same beta gives the same curve in tau
        tau = t / (d / v)
        off = detect._curve_log_offset(tau + (v / c), v / c)
        off -= off[np.argmin(np.abs(tau))]
        assert np.corrcoef(off, shapes[0])[0, 1] > 0.999


def test_template_bank_is_built_and_normalised():
    bank = detect.build_templates(frame_dt=0.5, bins_per_octave=48, n_harmonics=4)
    assert len(bank) >= 20
    for tpl in bank:
        ker = tpl["kernel"]
        assert ker.ndim == 2 and ker.size > 0
        assert np.linalg.norm(ker) == pytest.approx(1.0, abs=1e-9)
        assert abs(ker.mean()) < 1e-9  # zero-mean, so flat regions score zero
        assert tpl["cpa_distance"] == pytest.approx(tpl["speed"] * tpl["scale_s"])


SCENE = [
    (90.0, {"speed": 90, "cpa_distance": 2000, "f0": 90.0, "duration": 120.0}),
    (210.0, {"speed": 28, "cpa_distance": 150, "f0": 120.0, "duration": 40.0}),
    (320.0, {"speed": 130, "cpa_distance": 4000, "f0": 110.0, "duration": 140.0}),
]


@pytest.fixture(scope="module")
def scene_detections():
    samples, truths = synth.render_scene(SCENE, duration=400.0, snr_db=12.0, seed=4)
    dets = detect.detect(samples, 8000, band=(50.0, 300.0), n_harmonics=4)
    return dets, truths


def test_finds_both_aircraft_and_no_phantoms(scene_detections):
    """Two real aircraft, one road vehicle, and ~4 minutes of nothing."""
    dets, _ = scene_detections
    aircraft = [d for d in dets if d.is_aircraft]
    assert len(aircraft) == 2, [(round(d.t0_heard, 1), d.reason) for d in dets]
    times = sorted(d.t0_heard for d in aircraft)
    assert times[0] == pytest.approx(90.0, abs=6.0)
    assert times[1] == pytest.approx(320.0, abs=6.0)


def test_rejects_the_road_vehicle(scene_detections):
    """It is detected -- it has a genuine Doppler comb -- but classified as ground."""
    dets, _ = scene_detections
    near = [d for d in dets if abs(d.t0_heard - 210.0) < 15.0]
    assert near, "the vehicle should still be detected, just not called an aircraft"
    assert not any(d.is_aircraft for d in near)
    assert any("too slow" in d.reason for d in near)


def test_recovers_aircraft_parameters_blind(scene_detections):
    dets, _ = scene_detections
    aircraft = sorted((d for d in dets if d.is_aircraft), key=lambda d: d.t0_heard)
    fast = aircraft[-1]
    assert fast.speed == pytest.approx(130.0, rel=0.12)
    assert fast.cpa_distance == pytest.approx(4000.0, rel=0.25)
    assert fast.f0 == pytest.approx(110.0, rel=0.05)


def test_quiet_recording_yields_no_aircraft():
    """Wind noise alone must not manufacture flyovers."""
    rng = np.random.default_rng(0)
    noise = synth._pink_noise(8000 * 240, -1.5, rng)
    dets = detect.detect(noise, 8000, band=(50.0, 300.0), n_harmonics=4)
    assert not [d for d in dets if d.is_aircraft], [d.reason for d in dets]


def test_expected_band_narrows_the_search():
    """What Path A gets for free from the ADS-B type code and Path B has to search blindly."""
    lo, hi = detect.expected_f0_band(2)
    assert lo < 60.0 and hi > 95.0
    assert (hi - lo) < 70.0
    lo3, hi3 = detect.expected_f0_band(3)
    assert lo3 > lo and hi3 > hi


def test_detection_threshold_sits_between_null_and_signal():
    """The threshold must come from a null, not from eye.

    An earlier default of 4.0 was *below* the noise median and manufactured a "73.9 m/s
    aircraft" from 240 s of wind noise. This pins the separation so it cannot silently rot.
    """
    rng = np.random.default_rng(1)
    noise = synth._pink_noise(8000 * 200, -1.5, rng)
    null = [
        d.template_score
        for d in detect.detect(
            noise, 8000, band=(50.0, 300.0), n_harmonics=4, min_score=0.0, refine=False
        )
    ]
    assert null, "expected the correlator to produce candidates on noise"
    worst_null = max(null)

    fo = synth.Flyover(seed=3, snr_db=0, speed=90, cpa_distance=2000, f0=90.0, duration=150.0)
    samples, _ = synth.render(fo)
    best_signal = max(
        d.template_score
        for d in detect.detect(
            samples, 8000, band=(50.0, 300.0), n_harmonics=4, min_score=0.0, refine=False
        )
    )

    assert worst_null < detect.MIN_TEMPLATE_SCORE < best_signal
    assert best_signal > 3.0 * worst_null  # a real gap, not a hairline


def test_score_verdict_separates_silence_from_background_from_a_near_miss():
    """Four bands, because the two noise ceilings differ by a factor of two.

    Real background reaches FIELD_NOISE_CEILING, roughly twice the synthetic pink-noise
    figure the threshold was calibrated against (VALIDATION.md). A score between them is not
    evidence of a tone, and the old three-band version called it "below threshold", which
    implied it nearly was one.
    """
    assert detect.PINK_NOISE_CEILING < detect.FIELD_NOISE_CEILING
    assert detect.score_verdict(float("nan")) == "not analysed"
    assert detect.score_verdict(detect.PINK_NOISE_CEILING - 1.0) == "nothing tonal"

    between = 0.5 * (detect.PINK_NOISE_CEILING + detect.FIELD_NOISE_CEILING)
    assert detect.score_verdict(between) == "within background"

    near = 0.5 * (detect.FIELD_NOISE_CEILING + detect.MIN_TEMPLATE_SCORE)
    if detect.FIELD_NOISE_CEILING < detect.MIN_TEMPLATE_SCORE:
        assert detect.score_verdict(near) == "below threshold"
    assert detect.score_verdict(detect.MIN_TEMPLATE_SCORE + 1.0) == "scored, screened out"


def test_field_noise_ceiling_is_above_the_synthetic_one():
    """Guards the lesson, not just the value.

    MIN_TEMPLATE_SCORE was set from pink noise peaking at 8.9; measurement on 274 verified
    aircraft-free windows of real airport audio put the ceiling at 17.8. Any future
    recalibration that drops the field figure back to the synthetic one is a regression.
    """
    assert detect.FIELD_NOISE_CEILING >= 15.0
    assert detect.NOISE_CEILING_SCORE == detect.PINK_NOISE_CEILING


def test_diagnostics_are_optional_and_cost_nothing_when_unused():
    """`detect` is called without diagnostics from the CLI, so the default path must hold."""
    rng = np.random.default_rng(6)
    noise = synth._pink_noise(8000 * 90, -1.5, rng)
    assert detect.detect(noise, 8000, band=(50.0, 300.0), n_harmonics=4) == []


def test_diagnostics_score_a_window_where_nothing_was_reported():
    """A negative is only interpretable with the score behind it.

    `min_score` governs what gets reported; without the diagnostics there is no number at all
    for a pass ADS-B says should have been audible, and no way to tell a microphone that never
    had the signal from a detector that nearly had it.
    """
    rng = np.random.default_rng(5)
    noise = rng.standard_normal(120 * 8000) * 0.1
    diag = {}
    dets = detect.detect(noise, 8000, band=(50.0, 300.0), n_harmonics=4, diagnostics=diag)

    assert dets == [], "pure noise must not produce detections"
    assert diag["times"].size == diag["best_score"].size > 0
    assert diag["times"].shape == diag["best_score"].shape
    assert diag["min_score"] == detect.MIN_TEMPLATE_SCORE
    assert np.isnan(detect.best_score_near({}, 0.0))   # never called without diagnostics
    # Scored anyway, and the peak sits in the measured noise range rather than at zero.
    assert 0.0 < diag["peak_score"] < detect.MIN_TEMPLATE_SCORE
    assert detect.score_verdict(diag["peak_score"]) == "nothing tonal"

    # A time inside the window has a score; one outside it is honestly unknown, not zero.
    mid = float(diag["times"][diag["times"].size // 2])
    assert np.isfinite(detect.best_score_near(diag, mid))
    assert not np.isfinite(detect.best_score_near(diag, mid + 10_000.0))
    assert detect.score_verdict(float("nan")) == "not analysed"


def test_diagnostics_peak_where_a_real_pass_is():
    """The peak has to land on the pass, or the number means nothing."""
    audio, truths = synth.render_scene(
        [(100.0, dict(f0=113.0, speed=72.0, cpa_distance=900.0, duration=90.0))],
        duration=220.0, sample_rate=8000, snr_db=0.0, seed=3,
    )
    diag = {}
    dets = detect.detect(audio, 8000, band=(50.0, 300.0), n_harmonics=4, diagnostics=diag)
    assert any(d.is_aircraft for d in dets)

    # `t_centre` is the scene-level time of the pass; `t0_heard` in the truth dict is
    # relative to the clip the flyover was rendered from, and is 0.0 here.
    on_pass = detect.best_score_near(diag, truths[0]["t_centre"])
    off_pass = detect.best_score_near(diag, 15.0, tolerance_s=10.0)
    assert on_pass > detect.MIN_TEMPLATE_SCORE
    assert on_pass > off_pass
    assert detect.score_verdict(on_pass) == "scored, screened out"

def test_a_pass_is_scored_over_its_own_timescale():
    """A fixed tolerance contradicts the instrument's own physics, and said so on the dashboard.

    The pipeline scores each pass over +/-d/v, its Doppler transition. For a slow aircraft at
    3.8 km that is +/-83 s, against the +/-25 s this used to assume -- which read the quiet
    seconds around CPA and filed "nothing tonal" for an aircraft the operator heard plainly.

    Note the window is NOT `audible_window` (3*d/v): at real geometries that runs 440-660 s,
    wider than the 420 s analysis, so it would report the loudest thing anywhere and say
    nothing about this aircraft.
    """
    t_cpa = 1000.0
    times = np.arange(700.0, 1300.0, 2.0)
    best = np.full(times.size, 8.7)
    best[np.argmin(np.abs(times - (t_cpa + 60.0)))] = 37.8    # the real arrival
    diag = {"times": times, "best_score": best}

    assert detect.best_score_near(diag, t_cpa) == pytest.approx(8.7)
    assert detect.score_verdict(detect.best_score_near(diag, t_cpa)) == "nothing tonal"

    transition = (t_cpa - 83.0, t_cpa + 83.0)          # d/v for 3.8 km at 45.7 m/s
    assert detect.best_score_near(diag, t_cpa, window=transition) == pytest.approx(37.8)
    assert detect.best_score_near(diag, t_cpa, window=transition) > detect.MIN_TEMPLATE_SCORE


def test_scoring_window_still_reports_unanalysed_time_as_nan():
    """A window outside the analysed span must stay NaN, not collapse to a low score: 'not
    analysed' and 'analysed and quiet' are different claims about the instrument."""
    diag = {"times": np.arange(0.0, 100.0, 2.0), "best_score": np.full(50, 12.0)}
    assert np.isnan(detect.best_score_near(diag, 5000.0, window=(4900.0, 5100.0)))
    assert detect.score_verdict(float("nan")) == "not analysed"
