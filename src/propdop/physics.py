"""Acoustic propagation physics.

The emission-time ("retarded") formulation matters here: at 6 km slant range the sound of
closest approach arrives about 17 s after the aircraft was there, and the resulting curve is
asymmetric. Using the radial velocity at *receive* time instead is wrong by enough to move an
answer kilometres.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import ArrayLike, NDArray

T0_KELVIN = 273.15
P_REF_KPA = 101.325


def sound_speed(t_celsius: ArrayLike) -> NDArray[np.float64]:
    """Speed of sound in dry air, m/s. Humidity shifts this by <0.4%, below our noise floor."""
    return 331.3 * np.sqrt(1.0 + np.asarray(t_celsius, dtype=float) / T0_KELVIN)


def d_sound_speed_dt(t_celsius: float) -> float:
    """dc/dT in (m/s)/degC. ~0.59 at 15 C.

    This is the conversion that turns a sound-speed measurement into a temperature. Note it
    *divides*: sigma_T = sigma_c / (dc/dT).
    """
    t = float(t_celsius)
    return 331.3 / (2.0 * T0_KELVIN * np.sqrt(1.0 + t / T0_KELVIN))


def absorption_db_per_km(
    freq_hz: ArrayLike,
    t_celsius: float = 15.0,
    rel_humidity_pct: float = 70.0,
    pressure_kpa: float = P_REF_KPA,
) -> NDArray[np.float64]:
    """Atmospheric absorption, ISO 9613-1 / ANSI S1.26 pure-tone coefficient.

    Strongly frequency dependent: ~0.25 dB/km at 100 Hz against ~29 dB/km at 4.2 kHz. That
    gradient is why propeller tones carry for kilometres and jet fan tones do not.

    Spot checks against published octave-band tables agree within ~0.9-1.5x; conclusions here
    rest on the trend across three orders of magnitude, not on that factor.
    """
    f = np.asarray(freq_hz, dtype=float)
    t_k = t_celsius + T0_KELVIN
    t_ratio = t_k / 293.15
    p = pressure_kpa / P_REF_KPA

    # molar concentration of water vapour, %
    p_sat_ratio = 10.0 ** (-6.8346 * (273.16 / t_k) ** 1.261 + 4.6151)
    h = rel_humidity_pct * p_sat_ratio / p

    # oxygen and nitrogen relaxation frequencies
    f_ro = p * (24.0 + 4.04e4 * h * (0.02 + h) / (0.391 + h))
    f_rn = p * t_ratio**-0.5 * (9.0 + 280.0 * h * np.exp(-4.17 * (t_ratio ** (-1 / 3) - 1.0)))

    classical = 1.84e-11 * (1.0 / p) * np.sqrt(t_ratio)
    relaxation = t_ratio**-2.5 * (
        0.01275 * np.exp(-2239.1 / t_k) / (f_ro + f * f / f_ro)
        + 0.1068 * np.exp(-3352.0 / t_k) / (f_rn + f * f / f_rn)
    )
    return 8.686 * f * f * (classical + relaxation) * 1000.0


def blade_pass_hz(rpm: ArrayLike, n_blades: int) -> NDArray[np.float64]:
    """Propeller blade-pass frequency. Not carried by any public data feed."""
    return np.asarray(rpm, dtype=float) * n_blades / 60.0


def rpm_from_blade_pass(f0_hz: ArrayLike, n_blades: int) -> NDArray[np.float64]:
    """Inverse of `blade_pass_hz`. Fractional precision equals the fit's fractional residual."""
    return np.asarray(f0_hz, dtype=float) * 60.0 / n_blades


def firing_hz(rpm: ArrayLike, n_cylinders: int, strokes: int = 4) -> NDArray[np.float64]:
    """Engine firing frequency. For a 4-stroke each cylinder fires once per two revolutions.

    Independent of blade-pass, so departures from their fixed ratio indicate cylinder
    imbalance -- except for the common degenerate case of a 4-cylinder with a 2-blade prop on
    direct drive, where the two coincide exactly.
    """
    firings_per_rev = n_cylinders / (strokes / 2.0)
    return np.asarray(rpm, dtype=float) / 60.0 * firings_per_rev


# --------------------------------------------------------------------------------------
# Straight constant-velocity pass: closed form
# --------------------------------------------------------------------------------------
#
# Geometry: the aircraft passes at constant speed v, reaching closest approach (CPA) at slant
# distance d at emission time t0e. With s = t_e - t0e the emission time relative to CPA:
#
#     R(s) = sqrt(d^2 + v^2 s^2)
#     u    = t_r - t0e = s + R(s)/c
#
# Solving the quadratic in s and taking the root with u > s (causality):
#
#     s = [c^2 u - sqrt(c^2 v^2 u^2 + d^2 (c^2 - v^2))] / (c^2 - v^2)
#
# At s = 0 this gives u = d/c, i.e. the CPA is *heard* at t0e + d/c, later than it happened.


def emission_offset(
    t_heard_rel: ArrayLike, speed: float, cpa_distance: float, c: float = 343.0
) -> NDArray[np.float64]:
    """Emission time relative to CPA, given heard time relative to CPA emission.

    `t_heard_rel` is t_r - t0e (not t_r - t0r). Add `cpa_distance / c` to convert.
    """
    u = np.asarray(t_heard_rel, dtype=float)
    c2 = c * c
    v2 = speed * speed
    denom = c2 - v2
    if denom <= 0.0:
        raise ValueError("supersonic source: no subsonic retarded-time solution")
    disc = c2 * v2 * u * u + cpa_distance * cpa_distance * denom
    return (c2 * u - np.sqrt(disc)) / denom


def straight_pass_doppler(
    t_heard_rel: ArrayLike,
    f0: float,
    speed: float,
    cpa_distance: float,
    c: float = 343.0,
) -> NDArray[np.float64]:
    """Heard frequency of a steady tone `f0` from a straight constant-velocity pass.

    Returns frequency at each heard time (relative to the CPA *emission* time). Asymptotes are
    f0*c/(c-v) long before and f0*c/(c+v) long after; the slope at the heard CPA is
    -f0*v^2/(c*d), which is what makes the curve's steepness a distance measurement.
    """
    s = emission_offset(t_heard_rel, speed, cpa_distance, c)
    r = np.sqrt(cpa_distance * cpa_distance + speed * speed * s * s)
    v_radial = speed * speed * s / r  # dR/dt_e, positive receding
    return f0 * c / (c + v_radial)


def cpa_slope_hz_per_s(f0: float, speed: float, cpa_distance: float, c: float = 343.0) -> float:
    """Frequency slope at the heard CPA, Hz/s (negative). Steepness gives distance."""
    return -f0 * speed * speed / (c * cpa_distance)


def doppler_asymptotes(f0: float, speed: float, c: float = 343.0) -> tuple[float, float]:
    """(approaching, receding) asymptotic frequencies. Their ratio gives speed."""
    return f0 * c / (c - speed), f0 * c / (c + speed)


# --------------------------------------------------------------------------------------
# Arbitrary trajectory: numerical retarded time
# --------------------------------------------------------------------------------------


def solve_emission_time(
    t_heard: float,
    position_at: callable,
    receiver: NDArray[np.float64],
    c: float = 343.0,
    bracket_s: float = 60.0,
    tol: float = 1e-9,
) -> float:
    """Emission time whose sound reaches `receiver` at `t_heard`, for any trajectory.

    `position_at(t)` returns the source position as a 3-vector in the same frame as
    `receiver`. g(t_e) = t_e + |p(t_e) - x|/c - t_heard is monotonically increasing for a
    subsonic source, so bisection is unconditionally safe -- worth the few extra iterations
    over Newton, which can leave the bracket on a manoeuvring track.
    """

    def g(t_e: float) -> float:
        d = np.asarray(position_at(t_e), dtype=float) - receiver
        return t_e + float(np.linalg.norm(d)) / c - t_heard

    lo = t_heard - bracket_s
    hi = t_heard
    g_lo = g(lo)
    if g_lo > 0.0:
        raise ValueError(
            f"bracket too short: sound arriving at {t_heard} was emitted before {lo}"
        )
    while hi - lo > tol:
        mid = 0.5 * (lo + hi)
        if g(mid) < 0.0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def trajectory_doppler(
    t_heard: ArrayLike,
    f0: float,
    position_at: callable,
    velocity_at: callable,
    receiver: ArrayLike,
    c: float = 343.0,
) -> NDArray[np.float64]:
    """Heard frequency from a real trajectory, using emission-time radial velocity.

    `velocity_at(t)` should come from reported ground speed/track/vertical rate where
    available, or from a *smoothed* trajectory -- differentiating raw ADS-B positions puts
    kinks in the modelled curve that the audio does not have, which is a documented cause of
    wrong answers.
    """
    x = np.asarray(receiver, dtype=float)
    out = np.empty(np.size(t_heard), dtype=float)
    for i, t_r in enumerate(np.atleast_1d(np.asarray(t_heard, dtype=float))):
        t_e = solve_emission_time(float(t_r), position_at, x, c)
        d = np.asarray(position_at(t_e), dtype=float) - x
        r = float(np.linalg.norm(d))
        v_radial = float(np.dot(np.asarray(velocity_at(t_e), dtype=float), d)) / r
        out[i] = f0 * c / (c + v_radial)
    return out.reshape(np.shape(t_heard)) if np.ndim(t_heard) else out[0]


# --------------------------------------------------------------------------------------
# Turbulence (scintillation) -- see SCINTILLATION.md
# --------------------------------------------------------------------------------------


def rytov_log_amplitude_variance(
    c_eff_sq: float, freq_hz: float, path_m: float, c: float = 343.0
) -> float:
    """Log-amplitude variance in the weak-scattering (Rytov) regime, Kolmogorov spectrum.

    sigma^2 = 0.50 * Ceff^2 * k^(7/6) * L^(11/6)

    Valid only while the result is well below ~0.3; beyond that the Rytov approximation
    saturates and this overestimates badly. Published flyover measurements reach ~1.
    """
    k = 2.0 * np.pi * freq_hz / c
    return 0.50 * c_eff_sq * k ** (7.0 / 6.0) * path_m ** (11.0 / 6.0)


def c_eff_sq_from_fluctuations(
    t_rms: float, v_rms: float, t_mean_k: float = 290.0, outer_scale_m: float = 100.0
) -> float:
    """Effective acoustic structure parameter from temperature and wind fluctuations.

    mu' = T'/(2 T0) + v'/c, so wind dominates: at T'=0.5 K and v'=1 m/s the wind term is ~3.4x
    the thermal one. This is closer to a momentum-turbulence measurement than a thermal one.
    """
    c = float(sound_speed(t_mean_k - T0_KELVIN))
    mu_sq = (t_rms / (2.0 * t_mean_k)) ** 2 + (v_rms / c) ** 2
    return mu_sq / outer_scale_m ** (2.0 / 3.0)


def fresnel_scale_m(freq_hz: float, path_m: float, c: float = 343.0) -> float:
    """sqrt(lambda * L) -- the decorrelation length of the scintillation pattern.

    Aircraft motion sweeps the path through the turbulence at roughly v/2, so independent
    samples accrue far faster than for a static source. That is a genuine advantage of using
    aircraft of opportunity, not merely a convenience.
    """
    return float(np.sqrt(c / freq_hz * path_m))
