"""Synthetic flyovers with known ground truth.

The only way to know what the fit's residual `epsilon` means is to feed it a signal whose
parameters you already know. These generators also inject the two confounds that matter --
amplitude scintillation and propeller RPM wander -- so the discriminator between them can be
tested rather than assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from . import physics


@dataclass
class Flyover:
    """Ground truth for a synthetic pass. Times are relative to the *heard* CPA."""

    f0: float = 90.0
    speed: float = 90.0
    cpa_distance: float = 2000.0
    harmonic_amplitudes: tuple[float, ...] = (1.0, 0.55, 0.3, 0.16)
    duration: float = 120.0
    sample_rate: int = 8000
    c: float = 343.0
    t_celsius: float = 15.0
    rel_humidity_pct: float = 70.0
    snr_db: float = 10.0
    wind_noise_slope: float = -1.5
    c_eff_sq: float = 0.0
    rpm_wander_frac: float = 0.0
    seed: int = 0
    extras: dict = field(default_factory=dict)

    @property
    def n_harmonics(self) -> int:
        return len(self.harmonic_amplitudes)

    @property
    def cpa_slope(self) -> float:
        return physics.cpa_slope_hz_per_s(self.f0, self.speed, self.cpa_distance, self.c)

    @property
    def rpm(self) -> float:
        """Assumes a 2-blade propeller, the common GA case."""
        return float(physics.rpm_from_blade_pass(self.f0, 2))


def _pink_noise(n: int, slope: float, rng: np.random.Generator) -> NDArray[np.float64]:
    """Noise with a 1/f^|slope| spectrum. Wind noise is strongly low-frequency weighted, which
    is why it lands on exactly the band the propeller fundamental occupies."""
    spec = rng.normal(size=n // 2 + 1) + 1j * rng.normal(size=n // 2 + 1)
    f = np.arange(spec.size, dtype=float)
    f[0] = 1.0
    spec *= f ** (slope / 2.0)
    spec[0] = 0.0
    x = np.fft.irfft(spec, n=n)
    return x / (np.std(x) + 1e-30)


def _ou_process(
    n: int, dt: float, tau: float, rng: np.random.Generator
) -> NDArray[np.float64]:
    """Unit-variance Ornstein-Uhlenbeck process, correlation time `tau`.

    Stands in for both scintillation (correlation time = Fresnel crossing) and RPM wander
    (correlation time of seconds to tens of seconds).
    """
    a = np.exp(-dt / tau)
    noise = rng.normal(scale=np.sqrt(1.0 - a * a), size=n)
    x = np.empty(n)
    x[0] = rng.normal()
    for i in range(1, n):
        x[i] = a * x[i - 1] + noise[i]
    return x


def render(fo: Flyover) -> tuple[NDArray[np.float64], dict]:
    """Render a flyover to mono audio. Returns (samples, truth).

    The harmonic comb is phase-coherent by construction: harmonic n has instantaneous
    frequency n*f_heard(t), so its phase is exactly n times the fundamental's. That is what the
    comb fit exploits, and what lets the comb separate turbulence from engine wander.
    """
    rng = np.random.default_rng(fo.seed)
    n = int(round(fo.duration * fo.sample_rate))
    dt = 1.0 / fo.sample_rate
    t = np.arange(n) * dt - fo.duration / 2.0  # relative to heard CPA

    # heard time relative to CPA *emission* -- the closed form's independent variable
    t_rel_emission = t + fo.cpa_distance / fo.c

    f_heard = physics.straight_pass_doppler(
        t_rel_emission, fo.f0, fo.speed, fo.cpa_distance, fo.c
    )

    # RPM wander scales every harmonic by the same fraction, which is exactly what makes it
    # distinguishable from turbulence.
    if fo.rpm_wander_frac > 0.0:
        wander = 1.0 + fo.rpm_wander_frac * _ou_process(n, dt, 8.0, rng)
        f_heard = f_heard * wander
    else:
        wander = np.ones(n)

    # phase of the fundamental by integrating instantaneous frequency
    phase1 = 2.0 * np.pi * np.cumsum(f_heard) * dt

    # geometry through the pass, for spreading and absorption
    s = physics.emission_offset(t_rel_emission, fo.speed, fo.cpa_distance, fo.c)
    slant = np.sqrt(fo.cpa_distance**2 + (fo.speed * s) ** 2)

    signal = np.zeros(n)
    truth_tracks = {}
    for k, a_k in enumerate(fo.harmonic_amplitudes, start=1):
        f_k = k * f_heard
        # spherical spreading plus frequency-dependent absorption
        alpha = physics.absorption_db_per_km(
            float(np.median(f_k)), fo.t_celsius, fo.rel_humidity_pct
        )
        gain = (fo.cpa_distance / slant) * 10.0 ** (-alpha * (slant / 1000.0) / 20.0)

        log_amp = np.zeros(n)
        if fo.c_eff_sq > 0.0:
            # scintillation variance scales as f^(7/6) and L^(11/6); correlation time is the
            # Fresnel scale crossed at half the aircraft speed
            var = physics.rytov_log_amplitude_variance(
                fo.c_eff_sq, float(np.median(f_k)), float(np.median(slant)), fo.c
            )
            r_f = physics.fresnel_scale_m(float(np.median(f_k)), float(np.median(slant)), fo.c)
            tau = max(r_f / (fo.speed / 2.0), 4.0 * dt)
            log_amp = np.sqrt(var) * _ou_process(n, dt, tau, rng)

        amp = a_k * gain * np.exp(log_amp)
        signal += amp * np.sin(k * phase1)
        truth_tracks[k] = {"freq": f_k, "amp": amp, "log_amp_fluct": log_amp}

    # wind-weighted noise at the requested broadband SNR
    sig_rms = float(np.sqrt(np.mean(signal**2)))
    noise = _pink_noise(n, fo.wind_noise_slope, rng)
    noise *= sig_rms / (10.0 ** (fo.snr_db / 20.0))
    samples = signal + noise

    peak = float(np.max(np.abs(samples)))
    if peak > 0.0:
        samples = samples / peak * 0.9

    truth = {
        "f0": fo.f0,
        "speed": fo.speed,
        "cpa_distance": fo.cpa_distance,
        "t0_heard": 0.0,
        "rpm": fo.rpm,
        "cpa_slope": fo.cpa_slope,
        "n_harmonics": fo.n_harmonics,
        "sample_rate": fo.sample_rate,
        "duration": fo.duration,
        "c": fo.c,
        "f_heard": f_heard,
        "slant": slant,
        "tracks": truth_tracks,
        "rpm_wander": wander,
        "c_eff_sq": fo.c_eff_sq,
    }
    return samples, truth


def render_scene(
    events: list[tuple[float, dict]],
    duration: float,
    sample_rate: int = 8000,
    snr_db: float = 10.0,
    wind_noise_slope: float = -1.5,
    seed: int = 0,
) -> tuple[NDArray[np.float64], list[dict]]:
    """A long recording containing several passes at known times, plus stretches of nothing.

    This is what the blind detector has to work on: it is given no hint that anything happened.
    Each event is (centre time, Flyover kwargs); the sources are rendered clean and scene-level
    noise is added once, so the stated SNR refers to the whole record.
    """
    n = int(round(duration * sample_rate))
    buf = np.zeros(n)
    truths = []
    active = np.zeros(n, dtype=bool)

    for i, (t_centre, kw) in enumerate(events):
        fo = Flyover(sample_rate=sample_rate, snr_db=80.0, seed=seed + 100 + i, **kw)
        clip, truth = render(fo)
        start = int(round((t_centre - fo.duration / 2.0) * sample_rate))
        a, b = max(0, start), min(n, start + clip.size)
        if b <= a:
            continue
        buf[a:b] += clip[a - start : b - start]
        active[a:b] = True
        truth["t_centre"] = t_centre
        truth.update({k: v for k, v in kw.items() if k in ("speed", "cpa_distance", "f0")})
        truths.append(truth)

    rng = np.random.default_rng(seed)
    sig_rms = float(np.sqrt(np.mean(buf[active] ** 2))) if active.any() else 1.0
    noise = _pink_noise(n, wind_noise_slope, rng) * sig_rms / (10.0 ** (snr_db / 20.0))
    out = buf + noise
    peak = float(np.max(np.abs(out)))
    if peak > 0:
        out = out / peak * 0.9
    return out, truths
