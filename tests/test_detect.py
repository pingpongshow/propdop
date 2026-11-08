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
