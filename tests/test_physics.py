"""Physics checks. The closed-form straight-pass solution is verified against an independent
numerical retarded-time solve, so a sign or algebra error in either would show up."""

import numpy as np
import pytest

from propdop import physics


def test_sound_speed_and_temperature_conversion():
    assert physics.sound_speed(20.0) == pytest.approx(343.2, abs=0.5)
    assert physics.sound_speed(0.0) == pytest.approx(331.3, abs=0.1)

    k = physics.d_sound_speed_dt(15.0)
    assert k == pytest.approx(0.590, abs=0.005)
    # a 1 C rise must raise c by about k
    delta = physics.sound_speed(16.0) - physics.sound_speed(15.0)
    assert float(delta) == pytest.approx(k, rel=1e-3)
    # the conversion divides: 1 m/s of sound speed is ~1.7 C, not ~0.59 C
    assert 1.0 / k == pytest.approx(1.69, abs=0.02)


def test_absorption_rises_steeply_with_frequency():
    a = {f: float(physics.absorption_db_per_km(f)) for f in (63, 100, 400, 1600, 4200)}
    assert all(a[lo] < a[hi] for lo, hi in zip([63, 100, 400, 1600], [100, 400, 1600, 4200]))
    # the whole argument for props over jets: a prop fundamental survives 10 km, a fan tone does not
    assert a[100] * 10 < 5.0
    assert a[4200] * 10 > 100.0


def test_emission_offset_zero_at_heard_cpa():
    d, v, c = 2000.0, 90.0, 343.0
    # the CPA is heard at t0e + d/c, and that heard time corresponds to emission at the CPA
    s = physics.emission_offset(d / c, v, d, c)
    assert float(s) == pytest.approx(0.0, abs=1e-9)


def test_emission_offset_rejects_supersonic():
    with pytest.raises(ValueError):
        physics.emission_offset(0.0, 400.0, 1000.0, 343.0)


def test_closed_form_matches_numerical_retarded_time():
    """Independent check: solve t_e numerically for a straight track and compare."""
    d, v, c = 3000.0, 110.0, 343.0
    receiver = np.array([0.0, 0.0, 0.0])

    def pos(t_e):
        # passes along +x at height d, CPA at t_e = 0
        return np.array([v * t_e, 0.0, d])

    for t_rel_emission in (-40.0, -5.0, d / c, 20.0, 50.0):
        s_closed = float(physics.emission_offset(t_rel_emission, v, d, c))
        t_e_num = physics.solve_emission_time(
            t_rel_emission, pos, receiver, c=c, bracket_s=200.0
        )
        assert t_e_num == pytest.approx(s_closed, abs=1e-6)


def test_doppler_asymptotes_and_cpa_slope():
    f0, v, d, c = 90.0, 90.0, 2000.0, 343.0
    hi, lo = physics.doppler_asymptotes(f0, v, c)
    assert hi == pytest.approx(f0 * c / (c - v))
    assert lo == pytest.approx(f0 * c / (c + v))

    # far before and far after, the curve must approach those asymptotes
    f_early = float(physics.straight_pass_doppler(-4000.0, f0, v, d, c))
    f_late = float(physics.straight_pass_doppler(4000.0, f0, v, d, c))
    assert f_early == pytest.approx(hi, rel=2e-3)
    assert f_late == pytest.approx(lo, rel=2e-3)

    # numerical slope at the heard CPA must match -f0 v^2 / (c d)
    t0r = d / c
    h = 0.01
    num = (
        float(physics.straight_pass_doppler(t0r + h, f0, v, d, c))
        - float(physics.straight_pass_doppler(t0r - h, f0, v, d, c))
    ) / (2 * h)
    assert num == pytest.approx(physics.cpa_slope_hz_per_s(f0, v, d, c), rel=1e-3)


def test_curve_is_asymmetric_in_heard_time():
    """The retarded-time formulation makes the curve asymmetric; a naive model would not."""
    f0, v, d, c = 90.0, 120.0, 6000.0, 343.0
    t0r = d / c
    before = float(physics.straight_pass_doppler(t0r - 20.0, f0, v, d, c))
    after = float(physics.straight_pass_doppler(t0r + 20.0, f0, v, d, c))
    assert abs((before - f0) - (f0 - after)) > 0.5


def test_trajectory_doppler_matches_straight_pass():
    f0, v, d, c = 100.0, 100.0, 2500.0, 343.0
    receiver = np.array([0.0, 0.0, 0.0])

    def pos(t):
        return np.array([v * t, 0.0, d])

    def vel(t):
        return np.array([v, 0.0, 0.0])

    for t_rel in (-30.0, 0.0, d / c, 30.0):
        a = float(physics.trajectory_doppler(t_rel, f0, pos, vel, receiver, c))
        b = float(physics.straight_pass_doppler(t_rel, f0, v, d, c))
        assert a == pytest.approx(b, rel=1e-6)


def test_blade_pass_and_rpm_roundtrip():
    assert float(physics.blade_pass_hz(2700, 2)) == pytest.approx(90.0)
    assert float(physics.blade_pass_hz(2400, 2)) == pytest.approx(80.0)
    assert float(physics.rpm_from_blade_pass(90.0, 2)) == pytest.approx(2700.0)


def test_firing_frequency_degeneracy():
    """A 4-cylinder with a 2-blade prop on direct drive is degenerate -- documented, not a bug."""
    rpm = 2400.0
    assert float(physics.firing_hz(rpm, 4)) == pytest.approx(float(physics.blade_pass_hz(rpm, 2)))
    # six cylinders, or three blades, separate cleanly
    assert float(physics.firing_hz(rpm, 6)) != pytest.approx(float(physics.blade_pass_hz(rpm, 2)))
    assert float(physics.firing_hz(rpm, 4)) != pytest.approx(float(physics.blade_pass_hz(rpm, 3)))


def test_rytov_scaling_exponents():
    """The two scaling laws that make scintillation testable: f^(7/6) and L^(11/6)."""
    c2, f, L = 4.3e-7, 80.0, 2000.0
    base = physics.rytov_log_amplitude_variance(c2, f, L)
    assert physics.rytov_log_amplitude_variance(c2, 4 * f, L) / base == pytest.approx(
        4 ** (7 / 6), rel=1e-6
    )
    assert physics.rytov_log_amplitude_variance(c2, f, 2 * L) / base == pytest.approx(
        2 ** (11 / 6), rel=1e-6
    )


def test_c_eff_dominated_by_wind():
    """At realistic fluctuation levels the wind term dominates, so this is closer to a
    momentum-turbulence measurement than a thermal one."""
    wind_only = physics.c_eff_sq_from_fluctuations(0.0, 1.0)
    temp_only = physics.c_eff_sq_from_fluctuations(0.5, 0.0)
    assert wind_only > 10 * temp_only


def test_daytime_convection_saturates_at_a_few_km():
    """Regime check: the weak-scattering approximation must be flagged as invalid where the
    literature's measured flyover values reach ~1."""
    c2 = physics.c_eff_sq_from_fluctuations(0.5, 1.0)  # daytime convective
    assert physics.rytov_log_amplitude_variance(c2, 80.0, 1000.0) < 0.3
    assert physics.rytov_log_amplitude_variance(c2, 80.0, 4000.0) > 1.0
