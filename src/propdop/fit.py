"""The harmonic comb fit -- the one measurement everything else reads.

Two stages, because they want different time-frequency tradeoffs:

1. A whitened STFT, a Viterbi ridge trace and a four-parameter straight-pass fit. Frequency
   resolution here is capped by the sweep rate: a window of 1/sqrt(slope) minimises the sum of
   transform resolution and intra-window smear, which is around 1 Hz at typical geometries.

2. Phase tracking. Heterodyne each harmonic against the stage-1 curve and low-pass. This
   escapes the STFT resolution limit entirely and yields what the products actually need --
   per-harmonic complex amplitude at high time resolution, and a frequency residual whose RMS
   is `epsilon`.

`epsilon` is the fractional frequency residual. It is the master precision parameter: RPM
precision equals it, and the pass counts needed for the atmospheric products scale as its
square.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray
from scipy import optimize, signal

from . import physics


@dataclass
class CombFit:
    """Result of fitting one flyover. This is the row that goes in the database."""

    f0: float
    speed: float
    cpa_distance: float
    t0_heard: float
    epsilon: float
    epsilon_ridge: float
    n_harmonics: int
    harmonics: tuple[int, ...]
    track_times: NDArray[np.float64]
    freq_tracks: dict[int, NDArray[np.float64]]
    amp_tracks: dict[int, NDArray[np.float64]]
    log_amp_fluct: dict[int, NDArray[np.float64]]
    ridge_times: NDArray[np.float64]
    ridge_freqs: NDArray[np.float64]
    model_freqs: NDArray[np.float64]
    noise_bias: dict[int, float] = field(default_factory=dict)
    converged: bool = True
    notes: dict = field(default_factory=dict)

    def rpm(self, n_blades: int) -> float:
        return float(physics.rpm_from_blade_pass(self.f0, n_blades))

    def rpm_uncertainty(self, n_blades: int) -> float:
        """RPM precision is exactly epsilon -- the fractional error carries straight through."""
        return self.rpm(n_blades) * self.epsilon

    def log_amp_variance(self, correct_noise: bool = True) -> dict[int, float]:
        """Per-harmonic log-amplitude variance: the scintillation observable.

        Bias-corrected by default. Additive noise contributes 1/(2*SNR) to this variance, and
        because higher harmonics are weaker that bias *grows with frequency* -- it mimics the
        turbulence scaling and, uncorrected, dominates it on quiet passes.
        """
        out = {}
        for k, v in self.log_amp_fluct.items():
            raw = float(np.var(v[_trim(v.size)]))
            out[k] = raw - self.noise_bias.get(k, 0.0) if correct_noise else raw
        return out

    def scintillation_scaling(self, min_snr_margin: float = 1.5) -> dict:
        """Fit a power law to bias-corrected log-amplitude variance against frequency.

        Turbulence predicts an exponent of 7/6 (and L^(11/6) across geometries). This is the
        within-pass, assumption-free half of the discriminator: harmonics of the same tone
        share source and geometry exactly, so anything varying with frequency is propagation.

        Harmonics whose raw variance does not clear their noise bias by `min_snr_margin` are
        excluded -- on those the measurement is of SNR, not of the atmosphere.
        """
        raw = self.log_amp_variance(correct_noise=False)
        corrected = self.log_amp_variance(correct_noise=True)
        ks = sorted(
            k
            for k in corrected
            if corrected[k] > 0.0 and raw[k] > min_snr_margin * self.noise_bias.get(k, 0.0)
        )
        base = {
            "predicted": 7 / 6,
            "n_harmonics": len(ks),
            "variances": {k: corrected[k] for k in ks},
            "noise_bias": dict(self.noise_bias),
        }
        if len(ks) < 2:
            return {"exponent": float("nan"), **base}
        slope, intercept = np.polyfit(
            np.log([k * self.f0 for k in ks]), np.log([corrected[k] for k in ks]), 1
        )
        return {"exponent": float(slope), "intercept": float(intercept), **base}

    def frequency_coherence(self) -> float:
        """Correlation of fractional frequency error between the first two harmonics.

        The other half of the discriminator, and the sharper one. Propeller RPM wander moves
        every harmonic by the same *fraction*, so this goes to ~1; turbulence decorrelates
        across harmonics, so it stays near 0. On synthetic passes: 0.99 for 0.3% RPM wander
        against 0.06 for injected turbulence.
        """
        ks = sorted(self.freq_tracks)
        if len(ks) < 2:
            return float("nan")
        a, b = ks[0], ks[1]
        tr = _trim(self.track_times.size)
        model = np.interp(self.track_times, self.ridge_times, self.model_freqs)[tr]
        d1 = self.freq_tracks[a][tr] / a / model - 1.0
        d2 = self.freq_tracks[b][tr] / b / model - 1.0
        # Residual fit misfit is common-mode across harmonics, exactly like RPM wander, so it
        # inflates this correlation towards 1 regardless of cause. It is smooth over the whole
        # pass, so removing a low-order polynomial leaves the engine/turbulence distinction.
        tn = np.linspace(-1.0, 1.0, d1.size)
        d1 = d1 - np.polyval(np.polyfit(tn, d1, 3), tn)
        d2 = d2 - np.polyval(np.polyfit(tn, d2, 3), tn)
        if np.std(d1) == 0.0 or np.std(d2) == 0.0:
            return float("nan")
        return float(np.corrcoef(d1, d2)[0, 1])


# --------------------------------------------------------------------------------------
# Stage 1: whitened spectrogram and ridge
# --------------------------------------------------------------------------------------


def whitened_spectrogram(
    samples: NDArray[np.float64],
    sample_rate: int,
    window_s: float = 1.0,
    overlap: float = 0.75,
    band: tuple[float, float] = (40.0, 2000.0),
    median_s: float = 30.0,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """STFT magnitude with each frequency bin divided by its running median.

    Whitening is what makes an inaudible tone visible: it removes the noise floor's shape and
    any steady tone (mains hum, a fan), leaving only things that change.
    """
    nperseg = int(round(window_s * sample_rate))
    noverlap = int(round(nperseg * overlap))
    freqs, times, zxx = signal.stft(
        samples,
        fs=sample_rate,
        nperseg=nperseg,
        noverlap=noverlap,
        window="hann",
        boundary=None,
        padded=False,
    )
    mag = np.abs(zxx)

    lo, hi = band
    keep = (freqs >= lo) & (freqs <= hi)
    freqs, mag = freqs[keep], mag[keep]

    # running median per bin; a window longer than any single pass's dwell keeps the sweeping
    # tone from whitening itself away
    frame_dt = times[1] - times[0] if times.size > 1 else window_s
    half = max(1, int(round(median_s / frame_dt / 2)))
    pad = np.pad(mag, ((0, 0), (half, half)), mode="edge")
    med = np.empty_like(mag)
    for i in range(mag.shape[1]):
        med[:, i] = np.median(pad[:, i : i + 2 * half + 1], axis=1)
    return freqs, times, mag / (med + 1e-12)


def harmonic_product_spectrogram(
    freqs: NDArray[np.float64],
    wmag: NDArray[np.float64],
    f0_band: tuple[float, float],
    n_harmonics: int = 4,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Score every candidate fundamental by the energy of its whole harmonic family.

    Tracing a single ridge does not work. A higher harmonic sweeps proportionally faster in
    Hz, dwells less per frequency bin, and therefore gets more contrast from per-bin whitening
    -- on synthetic passes the ridge follows harmonic 2 and then jumps to 3 and 4 as their
    relative brightness changes, which leaves the stage-1 fit meaningless.

    Summing log energy over k*f for k = 1..K instead makes the true fundamental the unique
    maximum: only it has energy at *every* multiple. This is comb scoring applied at the
    bootstrap rather than as a post-hoc check, and it removes the octave ambiguity at source.

    Returns (candidate frequencies, score map) with the same time axis as `wmag`.
    """
    df = float(freqs[1] - freqs[0])
    f_lo, f_hi = f0_band
    cand_idx = np.nonzero((freqs >= f_lo) & (freqs <= f_hi))[0]
    if cand_idx.size == 0:
        raise ValueError("no candidate fundamentals inside the band")
    cand = freqs[cand_idx]

    logw = np.log1p(wmag)
    score = np.zeros((cand.size, wmag.shape[1]))
    counts = np.zeros(cand.size)
    for k in range(1, n_harmonics + 1):
        tgt = np.round((k * cand - freqs[0]) / df).astype(int)
        ok = (tgt >= 0) & (tgt < freqs.size)
        if not np.any(ok):
            continue
        # take the max over +/-1 bin so a line between bins is not missed
        rows = np.clip(tgt[ok][:, None] + np.array([-1, 0, 1]), 0, freqs.size - 1)
        score[ok] += logw[rows].max(axis=1)
        counts[ok] += 1.0
    return cand, score / np.maximum(counts, 1.0)[:, None]


def trace_ridge(
    freqs: NDArray[np.float64],
    times: NDArray[np.float64],
    wmag: NDArray[np.float64],
    band: tuple[float, float] | None = None,
    max_slope_hz_per_s: float = 25.0,
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    """Viterbi ridge through the whitened spectrogram.

    A frame-wise argmax jumps between sources; constraining the frame-to-frame frequency step
    to a physical slope keeps the path on one aircraft. Returns (times, freqs, strength).
    """
    if band is not None:
        keep = (freqs >= band[0]) & (freqs <= band[1])
        freqs, wmag = freqs[keep], wmag[keep]
    n_f, n_t = wmag.shape
    if n_f == 0 or n_t == 0:
        raise ValueError("empty spectrogram band")

    df = freqs[1] - freqs[0]
    dt = times[1] - times[0] if times.size > 1 else 1.0
    max_step = max(1, int(round(max_slope_hz_per_s * dt / df)))

    score = np.log(wmag + 1e-12)
    best = score[:, 0].copy()
    back = np.zeros((n_f, n_t), dtype=np.int32)
    for j in range(1, n_t):
        # max over a sliding window of allowed predecessors
        padded = np.full(n_f + 2 * max_step, -np.inf)
        padded[max_step : max_step + n_f] = best
        windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * max_step + 1)
        idx = np.argmax(windows, axis=1)
        best = windows[np.arange(n_f), idx] + score[:, j]
        back[:, j] = np.arange(n_f) + idx - max_step

    path = np.empty(n_t, dtype=np.int32)
    path[-1] = int(np.argmax(best))
    for j in range(n_t - 1, 0, -1):
        path[j - 1] = back[path[j], j]
    return times, freqs[path], wmag[path, np.arange(n_t)]


# --------------------------------------------------------------------------------------
# Stage 1b: the four-parameter straight-pass fit
# --------------------------------------------------------------------------------------


def seed_parameters(
    t: NDArray[np.float64], f: NDArray[np.float64], c: float = 343.0
) -> tuple[float, float, float, float]:
    """Analytic seed for (f0, speed, cpa_distance, t0_heard) from a traced ridge.

    Deliberately crude but unbiased in the right directions: the speed comes from the ratio of
    the asymptotes, the distance from the steepest slope. A straight-line fit is badly biased
    on a manoeuvring pass, so this only seeds -- it never decides anything.
    """
    n = max(3, len(f) // 10)
    f_hi = float(np.median(np.sort(f)[-n:]))
    f_lo = float(np.median(np.sort(f)[:n]))
    f0 = float(np.sqrt(f_hi * f_lo))  # geometric mean cancels the Doppler factor to 1st order

    ratio = (f_hi - f_lo) / (f_hi + f_lo)
    speed = float(np.clip(c * ratio, 20.0, 250.0))

    # Steepest descent locates the heard CPA and sets the distance scale. The ridge must be
    # smoothed first: at 1 Hz bins and 0.25 s frames the raw gradient quantises to +/-4 Hz/s,
    # which swamps the ~3.5 Hz/s true slope of a close pass and seeds an absurdly small
    # distance. That in turn lets the optimiser settle in the fast-and-very-close degenerate
    # minimum instead of the real one.
    if len(t) > 9:
        span = max(3, min(len(t) // 8, 101))
        if span % 2 == 0:
            span += 1
        f_smooth = signal.savgol_filter(f, span, 2)
        grad = np.gradient(f_smooth, t)
        i = int(np.argmin(grad))
        t0 = float(t[i])
        slope = float(grad[i])
    else:
        t0, slope = float(np.mean(t)), -1.0
    slope = min(slope, -1e-3)
    cpa = float(np.clip(-f0 * speed * speed / (c * slope), 50.0, 20000.0))
    return f0, speed, cpa, t0


def _model(
    t: NDArray[np.float64], f0: float, speed: float, cpa: float, t0: float, c: float
) -> NDArray[np.float64]:
    # the closed form takes heard time relative to CPA *emission*, hence the +cpa/c
    return physics.straight_pass_doppler(t - t0 + cpa / c, f0, speed, cpa, c)


def fit_straight_pass(
    t: NDArray[np.float64],
    f: NDArray[np.float64],
    weights: NDArray[np.float64] | None = None,
    c: float = 343.0,
) -> tuple[tuple[float, float, float, float], bool]:
    """Robust least squares for (f0, speed, cpa_distance, t0_heard).

    Multi-started over the distance seed. A fast very-close pass and a slower further one can
    produce similar curves, so a single start can settle in the wrong basin -- the degenerate
    solution shows up as the distance pinned against its lower bound.

    The f0 bound is deliberately tight: the geometric mean of the two asymptotes cancels the
    Doppler factor to first order, so the seed is good to a few percent and there is no reason
    to let the optimiser wander an octave away and trade f0 against speed.
    """
    f0, speed, cpa, t0 = seed_parameters(t, f, c)
    w = np.ones_like(f) if weights is None else np.sqrt(np.clip(weights, 0.0, None))

    def resid(p):
        return (_model(t, p[0], p[1], p[2], p[3], c) - f) * w

    lo = [f0 * 0.75, 20.0, 50.0, t.min() - 30.0]
    hi = [f0 * 1.33, 250.0, 20000.0, t.max() + 30.0]
    t0c = float(np.clip(t0, lo[3], hi[3]))

    best = None
    for scale in (1.0, 0.3, 3.0):
        p0 = [
            float(np.clip(f0, lo[0], hi[0])),
            float(np.clip(speed, lo[1], hi[1])),
            float(np.clip(cpa * scale, lo[2], hi[2])),
            t0c,
        ]
        try:
            res = optimize.least_squares(
                resid, p0, bounds=(lo, hi), loss="soft_l1", f_scale=np.std(f) * 0.1 + 1e-6
            )
        except Exception:
            continue
        if best is None or res.cost < best.cost:
            best = res

    if best is None:
        return (f0, speed, cpa, t0), False
    return (
        float(best.x[0]),
        float(best.x[1]),
        float(best.x[2]),
        float(best.x[3]),
    ), bool(best.success)


# --------------------------------------------------------------------------------------
# Stage 1c: which harmonic did the ridge actually follow?
# --------------------------------------------------------------------------------------
#
# The ridge does NOT reliably land on the fundamental. A higher harmonic sweeps proportionally
# faster in Hz, so it dwells less in each frequency bin, so per-bin whitening gives it more
# contrast -- on synthetic passes the 2nd harmonic is the brightest line in the band most of
# the time even when it is 5 dB weaker at the source.
#
# The saving grace is that speed, CPA distance and CPA time are harmonic-invariant: only f0
# scales. So the only thing to resolve is the integer the ridge was tracking, and that is the
# classic subharmonic search -- take the lowest line that still carries real energy.


def score_line(
    freqs: NDArray[np.float64],
    times: NDArray[np.float64],
    wmag: NDArray[np.float64],
    f_curve: NDArray[np.float64],
    weights: NDArray[np.float64] | None = None,
) -> float:
    """Mean log whitened energy sampled along a frequency curve.

    Weighted towards frames where signal is present, because a sweeping tone is invisible in
    its asymptotic tails and averaging those in buries the signal.
    """
    in_grid = (f_curve >= freqs[0]) & (f_curve <= freqs[-1])
    if not np.any(in_grid):
        return 0.0
    idx = np.clip(np.searchsorted(freqs, f_curve), 0, freqs.size - 1)
    vals = np.log1p(wmag[idx, np.arange(times.size)])
    w = np.ones_like(vals) if weights is None else np.clip(weights, 0.0, None)
    w = w * in_grid
    if w.sum() <= 0:
        return 0.0
    return float(np.sum(vals * w) / np.sum(w))


def resolve_harmonic_number(
    freqs: NDArray[np.float64],
    times: NDArray[np.float64],
    wmag: NDArray[np.float64],
    params: tuple[float, float, float, float],
    ridge_times: NDArray[np.float64],
    ridge_strength: NDArray[np.float64],
    c: float = 343.0,
    max_m: int = 4,
    margin: float = 1.25,
) -> int:
    """Which harmonic the traced ridge was following.

    For each candidate m, the implied fundamental is f0_ridge/m. Accept the largest m (lowest
    fundamental) whose own line carries clearly more energy than a deliberately detuned
    control at the same geometry -- the control is what rules out scoring noise.
    """
    f0_r, v, d, t0 = params
    w = np.clip(ridge_strength, 0.0, None)
    best_m = 1
    for m in range(max_m, 0, -1):
        f0_try = f0_r / m
        if f0_try < freqs[0] * 1.05:
            continue
        line = _model(ridge_times, f0_try, v, d, t0, c)
        signal_score = score_line(freqs, times, wmag, line, w)
        # detuned controls: a real line beats a nearby wrong one
        ctrl = max(
            score_line(freqs, times, wmag, line * 1.18, w),
            score_line(freqs, times, wmag, line * 0.85, w),
        )
        if signal_score > margin * max(ctrl, 1e-9):
            best_m = m
            break
    return best_m


def comb_score(
    freqs: NDArray[np.float64],
    times: NDArray[np.float64],
    wmag: NDArray[np.float64],
    params: tuple[float, float, float, float],
    ridge_times: NDArray[np.float64],
    ridge_strength: NDArray[np.float64],
    n_harmonics: int = 4,
    c: float = 343.0,
) -> float:
    """Energy along the whole harmonic family *relative to detuned controls*.

    A wrong candidate has to fit a single line; it would have to fit the comb. This is the
    metric the research literature flags as the main unexploited improvement.

    Reported as a contrast, not an absolute: comb energy minus the energy of deliberately
    detuned lines at the same geometry. An absolute version conflates "is this a comb" with
    "how loud is it", so a fixed threshold on it rejects quiet-but-real passes -- which it did,
    on every aircraft in a mixed scene where a close road vehicle set the loudness scale.
    """
    f0, v, d, t0 = params
    base = _model(ridge_times, f0, v, d, t0, c)
    w = np.clip(ridge_strength, 0.0, None)
    signal_total = 0.0
    control_total = 0.0
    used = 0
    for k in range(1, n_harmonics + 1):
        line = k * base
        if line.max() > freqs[-1]:
            continue
        signal_total += score_line(freqs, times, wmag, line, w)
        # 1.18 and 0.85 are not integer-harmonic ratios, so they cannot land on the comb
        control_total += 0.5 * (
            score_line(freqs, times, wmag, line * 1.18, w)
            + score_line(freqs, times, wmag, line * 0.85, w)
        )
        used += 1
    if used == 0:
        return 0.0
    return (signal_total - control_total) / used


# --------------------------------------------------------------------------------------
# Stage 2: phase tracking
# --------------------------------------------------------------------------------------


def track_harmonics(
    samples: NDArray[np.float64],
    sample_rate: int,
    f_model: NDArray[np.float64],
    harmonics: tuple[int, ...],
    track_rate: float = 20.0,
    lowpass_hz: float = 4.0,
    log_envelope: dict[int, NDArray[np.float64]] | None = None,
    detrend_poly: int = 3,
) -> tuple[NDArray, dict, dict, dict, dict]:
    """Heterodyne each harmonic against the model curve, low-pass, and decimate.

    This escapes the STFT resolution limit: the complex amplitude comes out at the track rate,
    not the frame rate. Harmonic spacing is f0 (tens of Hz), so a 4 Hz low-pass isolates each
    harmonic comfortably while still passing scintillation, which lives at the Fresnel
    timescale of ~1-3 s.

    The geometric trend is removed by *modelling* it, not by high-passing. An earlier version
    high-passed ln A at 0.1 Hz on the assumption that turbulence and the geometric envelope
    were a decade apart in timescale. They are not: the scintillation correlation time is the
    Fresnel scale crossed at ~v/2, which is 1-2 s, i.e. 0.08-0.16 Hz -- right on that corner.
    Worse, the Fresnel scale goes as 1/sqrt(f), so the corner ate more variance from low
    harmonics than high ones and biased the measured f^(7/6) exponent up to ~1.7.

    With `log_envelope` supplied from the fitted geometry (spreading plus absorption), only a
    low-order polynomial is removed afterwards, for unmodelled directivity.
    """
    n = samples.size
    dt = 1.0 / sample_rate
    # reference phase of the fundamental, by integrating the model frequency
    phase1 = 2.0 * np.pi * np.cumsum(f_model) * dt

    sos = signal.butter(4, lowpass_hz, btype="low", fs=sample_rate, output="sos")
    step = max(1, int(round(sample_rate / track_rate)))
    times = (np.arange(0, n, step)) * dt

    freq_tracks, amp_tracks, fluct, noise_bias = {}, {}, {}, {}
    for k in harmonics:
        # Control track midway between harmonics k and k+1: same sweep, same filter, no tone.
        # Its mean power estimates the in-band noise, which is what biases the log-amplitude
        # variance. For z = A + n, ln|z| ~ ln A + Re(n)/A, so additive noise contributes
        # sigma^2/(2 A^2) = 1/(2 SNR) to the variance -- and since higher harmonics are
        # weaker, that bias *rises with frequency* and mimics the f^(7/6) turbulence scaling.
        # Subtracting it is the difference between measuring turbulence and measuring SNR.
        zc = samples * np.exp(-1j * (k + 0.5) * phase1)
        zcf = signal.sosfiltfilt(sos, zc.real) + 1j * signal.sosfiltfilt(sos, zc.imag)
        noise_power = float(np.mean(np.abs(zcf[::step]) ** 2))

        z = samples * np.exp(-1j * k * phase1)
        # filtfilt on real and imaginary parts keeps the phase zero-shifted
        zf = signal.sosfiltfilt(sos, z.real) + 1j * signal.sosfiltfilt(sos, z.imag)
        zd = zf[::step]

        # x = A sin(phi) heterodyned and low-passed leaves A/2
        amp = 2.0 * np.abs(zd)
        amp_tracks[k] = amp

        # residual frequency from the phase derivative, divided by k to refer it to the
        # fundamental's frame
        ph = np.unwrap(np.angle(zd))
        dphi = np.gradient(ph, times) if times.size > 2 else np.zeros_like(ph)
        freq_tracks[k] = k * np.interp(times, np.arange(n) * dt, f_model) + dphi / (2.0 * np.pi)

        # detrend ln A to isolate the fluctuation from the geometric envelope
        with np.errstate(divide="ignore"):
            log_amp = np.log(np.clip(amp, 1e-12, None))
        resid = log_amp
        if log_envelope is not None and k in log_envelope:
            resid = resid - log_envelope[k][::step][: resid.size]
        if times.size > detrend_poly + 2 and detrend_poly > 0:
            tn = (times - times.mean()) / (np.ptp(times) or 1.0)
            resid = resid - np.polyval(np.polyfit(tn, resid, detrend_poly), tn)
        else:
            resid = resid - np.mean(resid)
        fluct[k] = resid

        mean_amp_sq = float(np.mean(np.abs(zd) ** 2))
        noise_bias[k] = (
            float(noise_power / (2.0 * mean_amp_sq)) if mean_amp_sq > 0.0 else float("inf")
        )

    return times, freq_tracks, amp_tracks, fluct, noise_bias


def _trim(n: int, frac: float = 0.125) -> slice:
    """Drop the edges, where filtfilt transients and the weakest signal both live."""
    k = max(1, int(n * frac))
    return slice(k, -k)


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------


def fit_comb(
    samples: NDArray[np.float64],
    sample_rate: int,
    band: tuple[float, float] = (40.0, 400.0),
    n_harmonics: int = 4,
    c: float = 343.0,
    window_s: float | None = None,
    track_rate: float = 20.0,
    t_offset: float = 0.0,
) -> CombFit:
    """Fit one flyover clip. `band` should bracket the expected fundamental.

    `window_s` defaults to 1 s; the theoretical optimum is 1/sqrt(|slope at CPA|), which is
    close to 1 s at typical light-aircraft geometries. After the first fit the slope is known,
    so the stage-1 trace is repeated once at the matched window.
    """
    samples = np.asarray(samples, dtype=float)
    n = samples.size
    dt = 1.0 / sample_rate
    t_signal = np.arange(n) * dt + t_offset

    spec_band = (band[0] * 0.5, min(band[1] * (n_harmonics + 1), sample_rate / 2.0 * 0.95))

    def trace_fundamental(win_s: float):
        """Whiten, score every candidate fundamental by its whole comb, then trace that."""
        freqs, times, wmag = whitened_spectrogram(
            samples, sample_rate, window_s=win_s, band=spec_band
        )
        cand, hps = harmonic_product_spectrogram(freqs, wmag, band, n_harmonics)
        rt, rf, rs = trace_ridge(cand, times, hps, max_slope_hz_per_s=15.0)
        return (freqs, times, wmag), (rt, rf, rs)

    def fit_strong(rt, rf, rs):
        """Fit only the frames that actually contain the pass.

        A flyover occupies part of the clip; in its asymptotic tails the tone is below the
        noise and the ridge is tracking nothing. Including those frames lets a majority of
        garbage set the parameters, which is how the fit ends up pinned against its bounds.
        """
        thresh = max(np.percentile(rs, 55.0), np.median(rs) * 0.5)
        keep = rs >= thresh
        if keep.sum() < 12:
            keep = rs >= np.percentile(rs, 25.0)
        p, ok = fit_straight_pass(rt[keep] + t_offset, rf[keep], weights=rs[keep], c=c)
        return p, ok, keep

    win = window_s or 1.0
    (freqs, times, wmag), (rt, rf, rs) = trace_fundamental(win)
    params, ok, keep = fit_strong(rt, rf, rs)
    f0, speed, cpa, t0 = params

    # re-trace at the window matched to the fitted slope: 1/sqrt(slope) minimises the sum of
    # transform resolution and intra-window smear
    if window_s is None and ok:
        slope = abs(physics.cpa_slope_hz_per_s(f0, speed, cpa, c))
        matched = float(np.clip(1.0 / np.sqrt(max(slope, 1e-3)), 0.25, 4.0))
        if abs(matched - win) > 0.2 * win:
            win = matched
            (freqs, times, wmag), (rt, rf, rs) = trace_fundamental(win)
            p2, ok2, keep2 = fit_strong(rt, rf, rs)
            if ok2:
                params, ok, keep = p2, ok2, keep2
                f0, speed, cpa, t0 = params

    ridge_times, ridge_freqs, ridge_strength = rt + t_offset, rf, rs
    model_at_ridge = _model(ridge_times, f0, speed, cpa, t0, c)
    eps_ridge = float(
        np.sqrt(np.mean(((ridge_freqs[keep] - model_at_ridge[keep]) / f0) ** 2))
    )
    quality = comb_score(
        freqs, times, wmag, params, rt, rs * keep, n_harmonics, c
    )

    # stage 2 needs the model at full sample rate
    f_model = _model(t_signal, f0, speed, cpa, t0, c)
    harmonics = tuple(range(1, n_harmonics + 1))
    nyq = sample_rate / 2.0
    harmonics = tuple(k for k in harmonics if k * f0 * 1.3 < nyq)

    # Modelled log-amplitude envelope per harmonic: spherical spreading plus absorption, both
    # known once the geometry is fitted. Subtracting this instead of high-passing is what keeps
    # the detrending out of the scintillation band.
    s_emit = physics.emission_offset(t_signal - t0 + cpa / c, speed, cpa, c)
    slant = np.sqrt(cpa * cpa + (speed * s_emit) ** 2)
    log_env = {}
    for k in harmonics:
        alpha = float(physics.absorption_db_per_km(k * f0))
        log_env[k] = np.log(cpa / slant) - np.log(10.0) * alpha * (slant / 1000.0) / 20.0

    times, freq_tracks, amp_tracks, fluct, noise_bias = track_harmonics(
        samples, sample_rate, f_model, harmonics, track_rate=track_rate, log_envelope=log_env
    )

    # epsilon from the fundamental's phase-tracked frequency against the model. Trim the edges,
    # where filtfilt transients and the weakest signal both live.
    f1 = freq_tracks[harmonics[0]] / harmonics[0]
    f1_model = np.interp(times + t_offset, t_signal, f_model)
    trim = _trim(times.size)
    eps = float(np.sqrt(np.mean(((f1[trim] - f1_model[trim]) / f0) ** 2)))

    return CombFit(
        f0=f0,
        speed=speed,
        cpa_distance=cpa,
        t0_heard=t0,
        epsilon=eps,
        epsilon_ridge=eps_ridge,
        n_harmonics=len(harmonics),
        harmonics=harmonics,
        track_times=times + t_offset,
        freq_tracks=freq_tracks,
        amp_tracks=amp_tracks,
        log_amp_fluct=fluct,
        ridge_times=ridge_times,
        ridge_freqs=ridge_freqs,
        model_freqs=model_at_ridge,
        noise_bias=noise_bias,
        converged=ok,
        notes={
            "ridge_strength_median": float(np.median(ridge_strength)),
            "frames_used": int(keep.sum()),
            "comb_score": float(quality),
            "stft_window_s": float(win),
        },
    )


# --------------------------------------------------------------------------------------
# Is this an aircraft at all?
# --------------------------------------------------------------------------------------
#
# The ADS-B-triggered path cannot see aircraft that do not transmit, and in US Class G below
# 10,000 ft ADS-B Out is not required at all -- which is exactly where low, slow propeller
# traffic flies. So a blind path is mandatory, and its hard problem is rejecting ground
# sources rather than finding tones.
#
# Three independent checks, each physical rather than tuned. Measured on synthetic sources,
# they classify 10/10 correctly across cars, trucks, locomotives and aircraft from 20 to
# 130 m/s:
#
#   1. Fit quality.   A ground source forced into a flyby model fits badly, which shows up as
#                     a large fractional residual. Comb contrast is *reported* but deliberately
#                     not gated on: it is a per-frame measure and so less sensitive than the
#                     matched-filter detector that found the event. A real aircraft at 4 km was
#                     detected with a matched-filter score of 110 while its per-frame comb
#                     contrast sat at zero, so gating on contrast discards true positives.
#   2. Resolvability. The transition takes about d/v seconds. A source at 50 m doing 20 m/s
#                     completes its whole sweep in 2.5 s -- two or three STFT frames -- and
#                     cannot be measured at all. Such fits came back at 101 m/s from a 20 m/s
#                     locomotive, reproducing a documented false positive in the literature.
#   3. Speed.         Road and rail traffic runs below ~35 m/s; aircraft above ~40 m/s.
#
# Check 3 only works because the fit's speed bound reaches down to 20 m/s, so a slow source
# can be described correctly and then rejected on its true speed. A template bank starting at
# 40 m/s (as one published implementation uses) forces slow sources into the aircraft range
# instead, which is how a locomotive gets reported as a 140 m/s flyover.


MIN_AIRCRAFT_SPEED = 35.8  # m/s, 80 mph
MIN_TRANSITION_S = 5.0
MIN_COMB_CONTRAST = 0.35
MAX_EPSILON = 0.02


def plausible_aircraft(
    result: CombFit,
    min_speed: float = MIN_AIRCRAFT_SPEED,
    min_transition_s: float = MIN_TRANSITION_S,
    min_comb_contrast: float = MIN_COMB_CONTRAST,
    max_epsilon: float = MAX_EPSILON,
) -> tuple[bool, str]:
    """Whether a blind detection is plausibly an aircraft. Returns (verdict, reason).

    For the ADS-B-triggered path this is only a sanity check; for the blind path it is the
    product. See the note above for why each threshold is what it is.
    """
    comb = float(result.notes.get("comb_score", 0.0))
    if not result.converged:
        return False, "fit did not converge"
    if result.epsilon > max_epsilon:
        return False, (
            f"poor fit (epsilon {result.epsilon * 100:.2f}%, contrast {comb:.2f})"
        )
    transition = result.cpa_distance / max(result.speed, 1e-6)
    if transition < min_transition_s:
        return False, f"transition unresolvable ({transition:.1f} s)"
    if result.speed < min_speed:
        return False, f"too slow for an aircraft ({result.speed:.0f} m/s)"
    return True, f"aircraft: {result.speed:.0f} m/s at {result.cpa_distance:.0f} m"
