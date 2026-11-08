"""The ADS-B join -- a two-way association, not a scheduler.

Measured in DESIGN.md 3.1: triggering on ADS-B buys no sensitivity, because blind detection
already reaches -30 dB SNR at 5 km. What the join buys is structural:

* **identity**, without which RPM does not exist (f0 = N_blades * RPM / 60, and N is not in any
  standard table -- see `adsb.infer_blade_count`);
* **geometry** to ~10 m, without which the atmospheric products are degenerate and
  scintillation inherits a 39% error from the blind distance estimate via L^(11/6);
* **truth pairing**, without which the fit's 5x-overconfident error bars cannot be corrected;
* and the **reverse direction**, without which an unexplained detection cannot be told from an
  aircraft that was simply not heard.

Tolerances come from the measured accuracy of the blind path, not from guesses: time to a
second or two, speed to ~6%, slant distance to ~12-25%.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import adsb, detect

SIGMA_T_S = 3.0
SIGMA_SPEED_FRAC = 0.10
SIGMA_SLANT_FRAC = 0.25
MAX_CHI = 3.0
AMBIGUITY_RATIO = 1.5


@dataclass
class Match:
    """One acoustic detection associated with one ADS-B pass."""

    detection: detect.Detection
    geometry: adsb.PassGeometry
    chi: float
    runner_up_chi: float | None = None
    ambiguous: bool = False

    @property
    def dt(self) -> float:
        return self.detection.t0_heard - self.geometry.t_heard_cpa

    @property
    def speed_error_frac(self) -> float:
        return (self.detection.speed - self.geometry.speed_mps) / self.geometry.speed_mps

    @property
    def slant_error_frac(self) -> float:
        return (self.detection.cpa_distance - self.geometry.slant_cpa_m) / self.geometry.slant_cpa_m


@dataclass
class JoinResult:
    matches: list[Match] = field(default_factory=list)
    unexplained: list[detect.Detection] = field(default_factory=list)
    unheard: list[adsb.PassGeometry] = field(default_factory=list)
    clock_offset_s: float = 0.0

    @property
    def detection_rate(self) -> float:
        """Fraction of passes that should have been audible and were heard.

        The quantity that makes `unexplained` mean something: if this is 0.4, then four in ten
        silent passes are expected and an unexplained detection is weak evidence.

        Counts propeller aeroplanes only, on both halves. Jets and rotorcraft are legitimate
        join candidates but have no comb for this detector to find, so neither a hit nor a
        miss on one says anything about the instrument. `store.detection_rate` applies the
        same rule in SQL; they must not disagree.
        """
        heard = sum(1 for m in self.matches if m.geometry.expected_audible)
        missed = sum(1 for g in self.unheard if g.expected_audible)
        total = heard + missed
        return heard / total if total else float("nan")

    def summary(self) -> dict:
        return {
            "matched": len(self.matches),
            "unexplained": len(self.unexplained),
            "unheard": len(self.unheard),
            "detection_rate": self.detection_rate,
            "clock_offset_s": self.clock_offset_s,
            "ambiguous": sum(m.ambiguous for m in self.matches),
        }


def _chi(det: detect.Detection, geom: adsb.PassGeometry, clock_offset_s: float) -> float:
    dt = (det.t0_heard - clock_offset_s) - geom.t_heard_cpa
    dv = (det.speed - geom.speed_mps) / max(geom.speed_mps, 1e-6)
    dd = (det.cpa_distance - geom.slant_cpa_m) / max(geom.slant_cpa_m, 1e-6)
    return float(
        np.sqrt(
            (dt / SIGMA_T_S) ** 2
            + (dv / SIGMA_SPEED_FRAC) ** 2
            + (dd / SIGMA_SLANT_FRAC) ** 2
        )
    )


def estimate_clock_offset(
    detections: list[detect.Detection],
    geometries: list[adsb.PassGeometry],
    search_s: float = 120.0,
) -> float:
    """Offset between the node's audio clock and ADS-B time, from the passes themselves.

    The array self-calibrates: ADS-B gives the true heard-CPA time, so the systematic part of
    the residual *is* the clock offset. Uses the median of the best per-detection residual over
    a coarse scan, which tolerates detections that match nothing.
    """
    if not detections or not geometries:
        return 0.0
    best_offset, best_score = 0.0, np.inf
    for trial in np.arange(-search_s, search_s + 1.0, 1.0):
        resid = []
        for det in detections:
            c = [_chi(det, g, trial) for g in geometries]
            if c and min(c) < MAX_CHI * 2.0:
                resid.append(min(c))
        if len(resid) >= max(1, len(detections) // 3):
            score = float(np.median(resid))
            if score < best_score:
                best_offset, best_score = float(trial), score
    if not np.isfinite(best_score):
        return 0.0
    # refine on the residuals of the accepted matches
    fine = []
    for det in detections:
        pairs = [(_chi(det, g, best_offset), g) for g in geometries]
        chi, g = min(pairs, key=lambda p: p[0])
        if chi < MAX_CHI:
            fine.append(det.t0_heard - g.t_heard_cpa)
    return float(np.median(fine)) if fine else best_offset


def join(
    detections: list[detect.Detection],
    geometries: list[adsb.PassGeometry],
    clock_offset_s: float | None = None,
    aircraft_only: bool = True,
) -> JoinResult:
    """Associate detections with ADS-B passes, in both directions.

    Greedy on chi, best first, one-to-one. A detection whose best and second-best candidates
    are within `AMBIGUITY_RATIO` is still matched but flagged -- two aircraft on the same
    airway minutes apart genuinely cannot be told apart by geometry alone, and silently picking
    one would corrupt both the error model and any per-tail-number history.
    """
    dets = [d for d in detections if d.is_aircraft] if aircraft_only else list(detections)
    if clock_offset_s is None:
        clock_offset_s = estimate_clock_offset(dets, geometries)

    pairs = []
    for i, det in enumerate(dets):
        for j, geom in enumerate(geometries):
            chi = _chi(det, geom, clock_offset_s)
            if chi < MAX_CHI:
                pairs.append((chi, i, j))
    pairs.sort()

    used_d: set[int] = set()
    used_g: set[int] = set()
    result = JoinResult(clock_offset_s=clock_offset_s)
    for chi, i, j in pairs:
        if i in used_d or j in used_g:
            continue
        others = [c for c, ii, jj in pairs if ii == i and jj != j]
        runner = min(others) if others else None
        result.matches.append(
            Match(
                detection=dets[i],
                geometry=geometries[j],
                chi=chi,
                runner_up_chi=runner,
                ambiguous=runner is not None and runner < AMBIGUITY_RATIO * max(chi, 1e-6),
            )
        )
        used_d.add(i)
        used_g.add(j)

    result.unexplained = [d for k, d in enumerate(dets) if k not in used_d]
    result.unheard = [g for k, g in enumerate(geometries) if k not in used_g]
    result.matches.sort(key=lambda m: m.detection.t0_heard)
    return result


def error_model(result: JoinResult) -> dict:
    """Empirical error distribution of the acoustic measurements against ADS-B truth.

    This is the point of the join. The fit's own uncertainties are about 5x optimistic
    (APPLICATIONS.md 2), so the usable error model has to be measured from paired rows --
    which is exactly what a match is.
    """
    good = [m for m in result.matches if not m.ambiguous]
    if not good:
        return {"n": 0}
    dt = np.array([m.dt for m in good])
    dv = np.array([m.speed_error_frac for m in good])
    dd = np.array([m.slant_error_frac for m in good])

    def stats(x):
        return {
            "bias": float(np.mean(x)),
            "median_abs": float(np.median(np.abs(x))),
            "p90_abs": float(np.percentile(np.abs(x), 90)),
            "sd": float(np.std(x)),
        }

    return {
        "n": len(good),
        "time_s": stats(dt),
        "speed_frac": stats(dv),
        "slant_frac": stats(dd),
    }
