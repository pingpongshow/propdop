"""Blind flyover detection -- Path B, the only route to aircraft that do not transmit.

ADS-B Out is not required in US Class G below 10,000 ft, which is where much low propeller
traffic flies, so this path is mandatory rather than a fallback (DESIGN.md 3.1).

The method exploits a change of variables. In log-frequency the heard curve is

    log f(t) = log f0  -  log(1 + (v/c) * sigma / sqrt(1 + sigma^2))

with sigma = v*s/d the dimensionless along-track position, and dimensionless heard time

    (t_r - t0e) / (d/v) = sigma + (v/c) * sqrt(1 + sigma^2)

So the *shape* depends only on `beta = v/c`. `f0` is a pure vertical translation, `d/v` a pure
horizontal scale, and `t0` a pure horizontal translation. Two consequences:

1. A modest bank of templates over (speed, d/v) covers every geometry, and a single 2-D FFT
   cross-correlation per template yields the score for all (f0, t0) at once.
2. **The harmonic comb becomes a fixed vertical pattern**, because harmonic k sits at
   log f0 + log k regardless of f0. So the template is comb-matched for free -- a wrong
   candidate has to fit the whole comb, not one line.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray
from scipy import ndimage, signal

from . import fit as fitmod
from . import physics


# Detection threshold, calibrated against a noise-only null rather than guessed.
#
# The log-frequency map is normalised per frequency row and each kernel to unit norm, so the
# correlation output is a scale-free matched-filter score -- effectively a z-score, inflated by
# the correlation between neighbouring cells and by taking a maximum over a large (f0, t0,
# template) search space.
#
# Measured over 6 x 200 s of pure pink wind noise: scores peak at 8.9, p99 = 7.8, median 5.0.
# Measured on real passes from 800 m to 6 km and SNR 0 to 20 dB: 68.3 to 91.9. So there is
# roughly a factor of 8 gap, and 20 sits near its geometric centre.
#
# The first version of this used 4.0, which is *below* the noise median -- it manufactured a
# "73.9 m/s aircraft at 422 m" out of 240 s of wind noise. Any threshold here must be set from
# a null, not chosen by eye.
#
# CORRECTED against real audio (VALIDATION.md). The null above is synthetic, and synthetic
# noise is too quiet: real aircraft-free background peaks at 17.8, not 8.9. The old value of
# 20 was therefore not "near the geometric centre of a factor of 8 gap" -- it sat 2.2 above
# the real ceiling, and its apparent margin was an artefact of calibrating against pink noise.
#
# 18.0 is set just above the measured field ceiling, which is the principle the original value
# only appeared to follow. Measured on AeroSonicDB: 68.9 % of propeller aircraft detected
# against 61.6 % at 20, with zero false positives in 274 verified aircraft-free windows.
#
# Lower values detect more -- 74.8 % at 15 -- but 15 is *below* the field ceiling, so roughly
# 1.5 % of clean windows then produce a candidate and `plausible_aircraft` becomes the only
# thing rejecting them. For a station whose headline product is "heard but absent from ADS-B",
# screening should be defence in depth, not the load-bearing member. A noisier site should
# measure its own ceiling and raise this.
MIN_TEMPLATE_SCORE = 18.0

# Two noise ceilings, because they differ by a factor of two and conflating them was an error.
#
# PINK_NOISE_CEILING is what 6 x 200 s of synthetic pink noise reaches. It is what
# MIN_TEMPLATE_SCORE was originally calibrated against, and it is optimistic.
#
# FIELD_NOISE_CEILING is what *real* background reaches: the maximum over 274 non-overlapping
# 51 s windows of airport environment audio, each verified aircraft-free frame by frame
# (VALIDATION.md). Real background peaks twice as high as synthetic noise, so a score of 14 --
# comfortably "tonal" against pink noise -- is unremarkable in the field.
PINK_NOISE_CEILING = 8.9
FIELD_NOISE_CEILING = 17.8

# Kept as the conservative floor for "nothing was there at all", and as the name the rest of
# the package already uses.
NOISE_CEILING_SCORE = PINK_NOISE_CEILING


@dataclass
class Detection:
    """One blind detection, after refinement by the comb fit."""

    t0_heard: float
    f0: float
    speed: float
    cpa_distance: float
    template_score: float
    is_aircraft: bool
    reason: str
    fit: fitmod.CombFit | None = None


def log_frequency_map(
    freqs: NDArray[np.float64],
    wmag: NDArray[np.float64],
    bins_per_octave: int = 48,
    band: tuple[float, float] = (40.0, 1600.0),
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Resample a whitened spectrogram onto a uniform log-frequency axis.

    This is the change of variables that makes the flyby shape f0-independent and the harmonic
    comb a fixed pattern.
    """
    lo = max(band[0], float(freqs[freqs > 0][0]))
    hi = min(band[1], float(freqs[-1]))
    n = int(np.ceil(np.log2(hi / lo) * bins_per_octave))
    log_f = lo * 2.0 ** (np.arange(n) / bins_per_octave)
    out = np.empty((n, wmag.shape[1]))
    for j in range(wmag.shape[1]):
        out[:, j] = np.interp(log_f, freqs, wmag[:, j])
    return log_f, out


def _curve_log_offset(
    tau: NDArray[np.float64], beta: float, n_iter: int = 60
) -> NDArray[np.float64]:
    """Log-frequency offset of the flyby curve at dimensionless heard time `tau`.

    `tau = (t_r - t0e) / (d/v)`. Inverts tau(sigma) = sigma + beta*sqrt(1+sigma^2) by
    bisection, which is monotone and so unconditionally safe.
    """
    lo = np.full_like(tau, -1e4)
    hi = np.full_like(tau, 1e4)
    for _ in range(n_iter):
        mid = 0.5 * (lo + hi)
        f = mid + beta * np.sqrt(1.0 + mid * mid) - tau
        neg = f < 0.0
        lo = np.where(neg, mid, lo)
        hi = np.where(neg, hi, mid)
    sigma = 0.5 * (lo + hi)
    v_radial_over_c = beta * sigma / np.sqrt(1.0 + sigma * sigma)
    return -np.log2(1.0 + v_radial_over_c)


def build_templates(
    frame_dt: float,
    bins_per_octave: int = 48,
    speeds: tuple[float, ...] = (30.0, 45.0, 60.0, 80.0, 100.0, 130.0, 170.0),
    scales_s: tuple[float, ...] = (3.0, 6.0, 12.0, 24.0, 48.0),
    n_harmonics: int = 4,
    span: float = 2.5,
    max_span_s: float = 150.0,
    c: float = 343.0,
) -> list[dict]:
    """Template bank over (speed, d/v). Each is a small 2-D kernel in (log-f, time)."""
    bank = []
    for v in speeds:
        beta = v / c
        for scale in scales_s:
            half_s = min(span * scale, max_span_s / 2.0)
            n_t = max(5, int(round(2 * half_s / frame_dt)))
            t = np.linspace(-half_s, half_s, n_t)
            tau = t / scale
            # offset relative to the curve's value at tau = 0
            off = _curve_log_offset(tau, beta) - _curve_log_offset(np.array([0.0]), beta)[0]
            rows = off * bins_per_octave

            pad = int(np.ceil(bins_per_octave * np.log2(n_harmonics + 1))) + 3
            lift = int(np.ceil(abs(rows).max())) + 2
            n_f = 2 * lift + pad + 1
            ker = np.zeros((n_f, n_t))
            for k in range(1, n_harmonics + 1):
                centre = lift + bins_per_octave * np.log2(k)
                idx = np.round(centre + rows).astype(int)
                ok = (idx >= 0) & (idx < n_f)
                # weight harmonics down: higher ones are weaker and more absorbed
                ker[idx[ok], np.arange(n_t)[ok]] += 1.0 / k
            ker = ndimage.gaussian_filter(ker, sigma=(1.0, 0.6))
            ker -= ker.mean()
            norm = np.linalg.norm(ker)
            if norm <= 0:
                continue
            bank.append(
                {
                    "kernel": ker / norm,
                    "speed": v,
                    "cpa_distance": v * scale,
                    "scale_s": scale,
                    "row_of_f0": lift,
                    "n_t": n_t,
                }
            )
    return bank


def detect(
    samples: NDArray[np.float64],
    sample_rate: int,
    band: tuple[float, float] = (50.0, 300.0),
    n_harmonics: int = 4,
    window_s: float = 1.0,
    bins_per_octave: int = 48,
    min_score: float = MIN_TEMPLATE_SCORE,
    max_detections: int = 24,
    refine: bool = True,
    c: float = 343.0,
    t_offset: float = 0.0,
    diagnostics: dict | None = None,
) -> list[Detection]:
    """Find flyovers with no prior knowledge of when or whether an aircraft passed.

    `band` brackets plausible fundamentals. Detections are refined with the comb fit and then
    screened by `fit.plausible_aircraft`, which is what rejects road and rail sources.

    If `diagnostics` is given it is filled with the best score reached at every analysed time,
    whether or not it cleared `min_score`. Without that, a pass ADS-B says should have been
    audible and was not produces no number at all, and there is no way to tell a microphone
    that could not hear it from a detector that nearly did.
    """
    samples = np.asarray(samples, dtype=float)
    spec_hi = min(band[1] * (n_harmonics + 1), sample_rate / 2.0 * 0.95)
    freqs, times, wmag = fitmod.whitened_spectrogram(
        samples, sample_rate, window_s=window_s, overlap=0.5, band=(band[0] * 0.5, spec_hi)
    )
    frame_dt = float(times[1] - times[0]) if times.size > 1 else window_s

    log_f, lmap = log_frequency_map(
        freqs, wmag, bins_per_octave=bins_per_octave, band=(band[0] * 0.5, spec_hi)
    )
    # Normalise per frequency row. The whitened map's statistics still vary with frequency, and
    # without this the correlation is dominated by whichever band happens to be liveliest.
    lmap = np.log1p(lmap)
    lmap = (lmap - lmap.mean(axis=1, keepdims=True)) / (
        lmap.std(axis=1, keepdims=True) + 1e-9
    )

    bank = build_templates(
        frame_dt, bins_per_octave=bins_per_octave, n_harmonics=n_harmonics, c=c
    )
    f0_lo = np.searchsorted(log_f, band[0])
    f0_hi = np.searchsorted(log_f, band[1])

    # Best score at each frame over every template and every f0 row, recorded regardless of
    # the threshold. `min_score` governs what is *reported*; this governs what can be *said*
    # about a window where nothing was reported.
    best_by_frame = np.full(times.size, -np.inf)

    peaks: list[tuple[float, float, float, float, float]] = []
    for tpl in bank:
        ker = tpl["kernel"]
        if ker.shape[0] >= lmap.shape[0] or ker.shape[1] >= lmap.shape[1]:
            continue
        corr = signal.fftconvolve(lmap, ker[::-1, ::-1], mode="valid")
        # row r of `corr` places the template's f0 row at lmap row r + row_of_f0
        rows = np.arange(corr.shape[0]) + tpl["row_of_f0"]
        keep = (rows >= f0_lo) & (rows < f0_hi)
        if not np.any(keep):
            continue
        sub = corr[keep]
        rows = rows[keep]
        # Column j of `corr` centres the kernel on frame j + width//2. Kernel widths differ
        # per template, so map back to frame indices before taking the running maximum.
        cols = np.arange(sub.shape[1]) + ker.shape[1] // 2
        inside = cols < times.size
        if np.any(inside):
            c_in = cols[inside]
            best_by_frame[c_in] = np.maximum(best_by_frame[c_in], sub.max(axis=0)[inside])
        # local maxima only, so one strong pass does not produce a ridge of detections
        loc = ndimage.maximum_filter(sub, size=(9, max(3, int(round(5.0 / frame_dt)))))
        hits = np.nonzero((sub == loc) & (sub > min_score))
        for i, j in zip(*hits):
            t_centre = times[j + ker.shape[1] // 2]
            peaks.append(
                (
                    float(sub[i, j]),
                    float(log_f[rows[i]]),
                    float(t_centre + t_offset),
                    tpl["speed"],
                    tpl["cpa_distance"],
                )
            )

    peaks.sort(key=lambda p: -p[0])

    # de-duplicate: same event found by neighbouring templates, and octave aliases
    chosen: list[tuple[float, float, float, float, float]] = []
    for p in peaks:
        score, f0, t0, v, d = p
        clash = False
        for q in chosen:
            if abs(t0 - q[2]) < 0.6 * max(q[4] / q[3], 8.0):
                ratio = f0 / q[1]
                if 0.5 <= ratio <= 2.0:
                    clash = True
                    break
        if not clash:
            chosen.append(p)
        if len(chosen) >= max_detections:
            break

    out: list[Detection] = []
    for score, f0, t0, v, d in chosen:
        det = Detection(
            t0_heard=t0,
            f0=f0,
            speed=v,
            cpa_distance=d,
            template_score=score,
            is_aircraft=False,
            reason="not refined",
        )
        if refine:
            half = float(np.clip(2.0 * d / v, 20.0, 90.0))
            i0 = int(max(0, (t0 - t_offset - half) * sample_rate))
            i1 = int(min(samples.size, (t0 - t_offset + half) * sample_rate))
            if i1 - i0 > sample_rate * 10:
                try:
                    r = fitmod.fit_comb(
                        samples[i0:i1],
                        sample_rate,
                        band=(max(band[0], f0 * 0.6), min(band[1], f0 * 1.7)),
                        n_harmonics=n_harmonics,
                        c=c,
                        t_offset=i0 / sample_rate + t_offset,
                    )
                    det.fit = r
                    det.f0, det.speed = r.f0, r.speed
                    det.cpa_distance, det.t0_heard = r.cpa_distance, r.t0_heard
                    det.is_aircraft, det.reason = fitmod.plausible_aircraft(r)
                except Exception as exc:  # a failed refinement is a rejection, not a crash
                    det.reason = f"refinement failed: {type(exc).__name__}"
        out.append(det)

    # Second de-duplication, now on *refined* parameters. The first pass works on template
    # coordinates, which are coarse enough that two peaks from one event can survive it; after
    # refinement the times and frequencies are accurate and the merge is reliable.
    out.sort(key=lambda x: -x.template_score)
    merged: list[Detection] = []
    for det in out:
        dup = False
        for keep in merged:
            # One d/v, not 0.6 of it. A flyby's Doppler transition spans about d/v either side
            # of CPA, so a second lock on the same pass -- typically on the receding tail,
            # where the tone has swept well below f0 -- lands anywhere in that span. At 0.6 a
            # duplicate of a 90 m/s pass at 2 km escaped by 0.7 s and was reported as a
            # second aircraft at 34 m/s.
            #
            # That duplicate was invisible for as long as MIN_AIRCRAFT_SPEED sat at 35.8,
            # which rejected it as "too slow" -- a gate masking a bug in a different stage.
            # Lowering the floor to 30.0 surfaced it, which is the argument for having both
            # the synthetic scene test and the corpus: neither alone would have shown this.
            window = 1.0 * max(keep.cpa_distance / max(keep.speed, 1.0), 10.0)
            if abs(det.t0_heard - keep.t0_heard) < window:
                ratio = det.f0 / keep.f0 if keep.f0 > 0 else 0.0
                if 0.45 <= ratio <= 2.2:
                    dup = True
                    break
        if not dup:
            merged.append(det)

    if diagnostics is not None:
        finite = np.isfinite(best_by_frame)
        diagnostics["times"] = times + t_offset
        diagnostics["best_score"] = np.where(finite, best_by_frame, np.nan)
        diagnostics["peak_score"] = (
            float(best_by_frame[finite].max()) if np.any(finite) else float("nan")
        )
        diagnostics["min_score"] = float(min_score)

    merged.sort(key=lambda x: x.t0_heard)
    return merged


# A pass is heard over roughly d/v seconds, and the acoustic CPA time carries both fit error
# and an unknown station clock offset early in a run, so the search for "did we score anything
# where ADS-B said to look" has to be a window rather than a sample.
# Fallback only, for a score asked for without a geometry. Prefer the pass's own
# `audible_window`: a fixed tolerance contradicts the instrument's own physics. A slow
# aircraft at 3.8 km is audible for +/-248 s by `adsb.pass_geometry`, and scoring it over
# +/-25 s read the quiet 50 seconds around CPA and filed "nothing tonal" while a comb scoring
# 37.8 sat 128 s away -- for an aircraft the operator could hear perfectly well.
SCORE_WINDOW_S = 25.0


def best_score_near(
    diagnostics: dict,
    t: float,
    tolerance_s: float = SCORE_WINDOW_S,
    window: tuple[float, float] | None = None,
) -> float:
    """Peak template score while a pass was audible; NaN if that time was not analysed.

    `window` is the pass's own `audible_window` and is what callers should pass: it is derived
    from `d/v`, so it stretches for a slow or distant aircraft exactly when a fixed tolerance
    would fall short. `tolerance_s` remains for callers with a bare timestamp.

    This answers "was anything tonal while this aircraft was overhead", not "was this aircraft
    heard". Only the join can answer the second, because only it checks that the tone's
    geometry matches the track's.
    """
    times = np.asarray(diagnostics.get("times") if diagnostics else (), dtype=float)
    best = np.asarray(diagnostics.get("best_score") if diagnostics else (), dtype=float)
    if times.size == 0 or best.size != times.size:
        return float("nan")
    if window is not None:
        sel = (times >= window[0]) & (times <= window[1]) & np.isfinite(best)
    else:
        sel = (np.abs(times - t) <= tolerance_s) & np.isfinite(best)
    return float(best[sel].max()) if np.any(sel) else float("nan")


def score_verdict(score: float, min_score: float = MIN_TEMPLATE_SCORE) -> str:
    """Plain words for a template score, so a negative result reads without the context.

    Four bands rather than three, because real background reaches twice as high as the
    synthetic noise the threshold was set against: a score between the two ceilings is not
    evidence of a tone, and calling it "below threshold" implied it nearly was one.
    """
    if not np.isfinite(score):
        return "not analysed"
    if score < PINK_NOISE_CEILING:
        return "nothing tonal"
    if score < FIELD_NOISE_CEILING:
        return "within background"
    if score < min_score:
        return "below threshold"
    return "scored, screened out"


def expected_f0_band(n_blades: int, rpm_range: tuple[float, float] = (1800.0, 2900.0)):
    """Plausible fundamental band for a known propeller -- what Path A supplies and Path B
    must search blindly. Included here to make the cost of not knowing it explicit."""
    lo = float(physics.blade_pass_hz(rpm_range[0], n_blades))
    hi = float(physics.blade_pass_hz(rpm_range[1], n_blades))
    return lo * 0.85, hi * 1.15
