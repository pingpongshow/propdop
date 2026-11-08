"""Atmospheric inversion: sound speed, wind and temperature from the passes."""

import numpy as np
import pytest

from propdop import atmos, fit, physics, synth


def test_c_eff_recovers_the_assumed_speed_when_the_fit_agrees():
    """If the acoustic speed matches ADS-B, the medium was exactly what we assumed."""
    for v in (45.0, 90.0, 130.0):
        assert atmos.c_eff_from_speed_bias(v, v, 343.0) == pytest.approx(343.0, rel=1e-9)


@pytest.mark.parametrize("c_true", [330.0, 343.0, 352.0])
@pytest.mark.parametrize("v_true", [60.0, 90.0, 130.0])
def test_c_eff_inversion_is_exact(c_true, v_true):
    """Round trip: a true (v, c) implies a fitted v under c=343; invert it back."""
    k = 2.0 * v_true * c_true / (c_true**2 - v_true**2)
    # the speed a fit assuming 343 would report for that fractional drop
    c0 = 343.0
    v_fit = c0 * (-1.0 + np.sqrt(1.0 + k * k)) / k
    assert atmos.c_eff_from_speed_bias(v_fit, v_true, c0) == pytest.approx(c_true, rel=1e-9)


def test_c_eff_from_a_real_fit_with_a_shifted_sound_speed():
    """End to end: render a pass through a medium at 352 m/s and recover it from the audio."""
    c_true = 352.0
    fo = synth.Flyover(seed=5, snr_db=20, speed=110.0, cpa_distance=3000.0, f0=95.0,
                       duration=150.0, c=c_true)
    samples, truth = synth.render(fo)
    r = fit.fit_comb(samples, fo.sample_rate, band=(50.0, 300.0), n_harmonics=4, c=343.0)
    c_eff = atmos.c_eff_from_speed_bias(r.speed, truth["speed"], 343.0)
    assert c_eff == pytest.approx(c_true, abs=4.0)


def test_sigma_depends_only_on_epsilon_and_speed():
    """Not on which harmonic was tracked -- f0 cancels (APPLICATIONS.md 6)."""
    a = atmos.sigma_c_eff(0.0019, 90.0)
    assert a == pytest.approx(1.01, abs=0.05)
    # fast aircraft carry ~3x the information of slow ones
    assert atmos.sigma_c_eff(0.0019, 130.0) < 0.6 * atmos.sigma_c_eff(0.0019, 60.0)


def test_temperature_conversion_divides_not_multiplies():
    c = float(physics.sound_speed(15.0))
    assert atmos.temperature_from_c(c) == pytest.approx(15.0, abs=0.01)
    s = atmos.Sounding(c0=c, wind_east=0.0, wind_north=0.0, n=20, sigma_c0=1.0)
    # 1 m/s of sound speed is ~1.7 C, not ~0.59 C
    assert s.sigma_temperature_c == pytest.approx(1.69, abs=0.05)


def make_samples(c0, w_e, w_n, n=40, sigma=0.4, seed=0, bearings=None, elevation=45.0,
                 altitude=2000.0):
    rng = np.random.default_rng(seed)
    bearings = np.linspace(0, 360, n, endpoint=False) if bearings is None else bearings
    out = []
    for b in bearings:
        ce = np.cos(np.radians(elevation))
        truth = c0 + ce * (w_e * np.sin(np.radians(b)) + w_n * np.cos(np.radians(b)))
        out.append(
            atmos.PathSample(
                c_eff=truth + rng.normal(scale=sigma),
                sigma=sigma,
                bearing_deg=float(b),
                elevation_deg=elevation,
                altitude_m=altitude,
            )
        )
    return out


def test_bulk_solve_recovers_wind_and_temperature():
    c0 = float(physics.sound_speed(12.0))
    s = atmos.solve_bulk(make_samples(c0, w_e=6.0, w_n=-4.0, n=60, sigma=0.4, seed=1))
    assert s is not None and s.n == 60
    assert s.c0 == pytest.approx(c0, abs=0.3)
    assert s.wind_east == pytest.approx(6.0, abs=0.6)
    assert s.wind_north == pytest.approx(-4.0, abs=0.6)
    assert s.wind_speed == pytest.approx(np.hypot(6.0, 4.0), rel=0.12)
    assert s.temperature_c == pytest.approx(12.0, abs=1.0)
    assert s.note == ""


def test_wind_bearing_uses_the_meteorological_convention():
    """Reported as the direction the wind blows *from*."""
    s = atmos.solve_bulk(make_samples(343.0, w_e=0.0, w_n=10.0, n=40, sigma=0.05, seed=2))
    assert s.wind_bearing_deg == pytest.approx(180.0, abs=5.0)  # blowing north, from the south
    s = atmos.solve_bulk(make_samples(343.0, w_e=10.0, w_n=0.0, n=40, sigma=0.05, seed=2))
    assert s.wind_bearing_deg == pytest.approx(270.0, abs=5.0)  # blowing east, from the west


def test_clustered_bearings_are_flagged_not_silently_wrong():
    """With every pass on one bearing, wind along it is indistinguishable from temperature."""
    s = atmos.solve_bulk(
        make_samples(343.0, 5.0, 0.0, bearings=np.linspace(88, 92, 30), sigma=0.4, seed=3)
    )
    assert s is not None
    assert "ill-conditioned" in s.note
    assert s.condition > atmos.MAX_CONDITION


def test_too_few_passes_returns_none_rather_than_a_number():
    assert atmos.solve_bulk(make_samples(343.0, 3.0, 1.0, n=3)) is None


def test_averaging_tightens_the_estimate_as_expected():
    """Precision should improve roughly as sqrt(N) -- the reason soundings are aggregates."""
    few = atmos.solve_bulk(make_samples(343.0, 5.0, 2.0, n=12, sigma=0.4, seed=7))
    many = atmos.solve_bulk(make_samples(343.0, 5.0, 2.0, n=120, sigma=0.4, seed=7))
    assert many.sigma_c0 < 0.5 * few.sigma_c0


# -- layers ----------------------------------------------------------------------------


def test_layer_weights_sum_to_one_and_respect_the_ceiling():
    edges = np.array([0.0, 1000.0, 2000.0, 4000.0])
    w = atmos.layer_weights(1500.0, edges)
    assert w.sum() == pytest.approx(1.0)
    assert w[0] == pytest.approx(1000 / 1500)
    assert w[1] == pytest.approx(500 / 1500)
    assert w[2] == 0.0


def test_error_amplification_matches_the_documented_values():
    assert atmos.error_amplification(0.0, 1000.0) == pytest.approx(1.0)
    assert atmos.error_amplification(1000.0, 2000.0) == pytest.approx(2.24, abs=0.01)
    assert atmos.error_amplification(3000.0, 3500.0) == pytest.approx(9.22, abs=0.01)


def test_profile_recovers_a_sound_speed_gradient():
    """A lapse rate: sound speed falling with height. Passes at many altitudes constrain it."""
    edges = np.array([0.0, 1000.0, 2000.0, 4000.0])
    layer_c = np.array([345.0, 341.0, 337.0])
    rng = np.random.default_rng(11)
    samples = []
    for _ in range(400):
        h = float(rng.uniform(300.0, 4000.0))
        w = atmos.layer_weights(h, edges)
        truth = float(w @ layer_c)
        b = float(rng.uniform(0, 360))
        samples.append(
            atmos.PathSample(
                c_eff=truth + rng.normal(scale=0.4),
                sigma=0.4,
                bearing_deg=b,
                elevation_deg=60.0,
                altitude_m=h,
            )
        )
    prof = atmos.solve_profile(samples, edges, regularisation=0.3)
    assert prof.n_total == 400
    assert all(layer is not None for layer in prof.layers)
    got = np.array([layer.c0 for layer in prof.layers])
    # the gradient must come out with the right sign and roughly the right size
    assert got[0] > got[1] > got[2]
    assert np.allclose(got, layer_c, atol=1.5)


def test_profile_reports_none_for_layers_no_pass_reaches():
    edges = np.array([0.0, 1000.0, 2000.0, 8000.0])
    samples = make_samples(343.0, 2.0, 1.0, n=30, altitude=1500.0)
    prof = atmos.solve_profile(samples, edges)
    assert prof.layers[-1] is None  # nothing flew above 2 km
    assert prof.counts[-1] == 0
    assert prof.layers[0] is not None


def test_profile_layers_carry_their_pass_counts():
    edges = np.array([0.0, 1500.0, 3000.0])
    prof = atmos.solve_profile(make_samples(343.0, 3.0, 0.0, n=40, altitude=2500.0), edges)
    for layer in prof.layers:
        assert layer is None or layer.n > 0
        if layer is not None and layer.n < 20:
            assert "expect wide bars" in layer.note


def test_empty_input_is_handled():
    prof = atmos.solve_profile([], [0.0, 1000.0, 2000.0])
    assert prof.n_total == 0 and all(layer is None for layer in prof.layers)
