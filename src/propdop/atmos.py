"""Wind and temperature aloft from the passes.

The observable is the **effective sound speed along the path**, `c_eff = c(T) + w . r_hat`.
Recovering it needs no extra measurement: the comb fit already solves for speed under an
assumed sound speed, so the disagreement between that and the ADS-B truth *is* the sound-speed
error, in closed form (`c_eff_from_speed_bias`).

Separating temperature from wind then exploits their different angular signatures --
temperature is isotropic, wind projects as `cos(elevation) * cos(bearing - wind bearing)`. A
single pass sweeps ~127 degrees of bearing, which is why one station can do this at all; many
passes on many bearings is what makes it well conditioned.

Honest expectations, from APPLICATIONS.md 6 and unchanged by this implementation:

* per pass, `sigma_c` is ~1 m/s at the best demonstrated fit quality on a fast aircraft;
* that is ~1 m/s of wind (useful) or ~1.7 C of temperature (marginal), so **wind is the real
  product and temperature is the weak one**;
* a 1-2 km layer amplifies the error 2.2x, so useful soundings are hourly-to-daily aggregates
  of ~20 passes, never single passes.

Every result therefore carries its contributing-pass count, and the solvers refuse rather than
extrapolate when a layer is empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from . import physics

C_ASSUMED = 343.0


# --------------------------------------------------------------------------------------
# The observable
# --------------------------------------------------------------------------------------


def c_eff_from_speed_bias(
    speed_fitted: float, speed_true: float, c_assumed: float = C_ASSUMED
) -> float:
    """Effective sound speed along the path, in closed form.

    The fit matches the observed *fractional* pitch drop, which for a straight pass is

        K = 2 v c / (c^2 - v^2)

    and depends only on speed and sound speed. The fit reports the `v` that reproduces K under
    an assumed `c`; ADS-B supplies the true `v`. Solving the same relation the other way,

        K c^2 - 2 v_true c - K v_true^2 = 0   =>   c = v_true (1 + sqrt(1 + K^2)) / K

    So no additional measurement is needed -- the sound speed was always encoded in the
    disagreement between the acoustic speed and the radar speed.
    """
    if speed_fitted <= 0.0 or speed_true <= 0.0:
        raise ValueError("speeds must be positive")
    if speed_fitted >= c_assumed:
        raise ValueError("fitted speed is supersonic under the assumed sound speed")
    k = 2.0 * speed_fitted * c_assumed / (c_assumed**2 - speed_fitted**2)
    return float(speed_true * (1.0 + np.sqrt(1.0 + k * k)) / k)


def sigma_c_eff(epsilon: float, speed: float, c: float = C_ASSUMED) -> float:
    """Per-pass uncertainty in `c_eff`.

    `sigma_c = eps * (c^2 - v^2)^2 / (2 v (c^2 + v^2))` -- depends only on the *fractional*
    frequency precision and the aircraft speed, not on which harmonic was tracked. Fast
    aircraft carry roughly three times the information of slow ones, so this is also the
    weighting the solvers use.
    """
    v = max(float(speed), 1e-6)
    return float(epsilon * (c * c - v * v) ** 2 / (2.0 * v * (c * c + v * v)))


def temperature_from_c(c_eff: float) -> float:
    """Celsius implied by a sound speed, inverting `physics.sound_speed`."""
    return float(273.15 * ((c_eff / 331.3) ** 2 - 1.0))


@dataclass
class PathSample:
    """One pass reduced to what the inversion needs."""

    c_eff: float
    sigma: float
    bearing_deg: float
    elevation_deg: float
    altitude_m: float
    t: float = 0.0
    icao24: str | None = None

    @property
    def weight(self) -> float:
        return 1.0 / max(self.sigma, 1e-9) ** 2


def path_geometry(enu_aircraft_at_cpa: NDArray[np.float64]) -> tuple[float, float]:
    """(bearing, elevation) in degrees of the path from receiver to aircraft at CPA."""
    e, n, u = (float(v) for v in np.asarray(enu_aircraft_at_cpa, dtype=float))
    bearing = float(np.degrees(np.arctan2(e, n)) % 360.0)
    horiz = float(np.hypot(e, n))
    elevation = float(np.degrees(np.arctan2(u, max(horiz, 1e-9))))
    return bearing, elevation


def sample_from_match(match, epsilon: float, c_assumed: float = C_ASSUMED) -> PathSample:
    """Build a `PathSample` from an `associate.Match`.

    Uses the ADS-B geometry for speed, bearing and elevation -- which is the whole reason the
    atmospheric products need the join: solved blind, the medium and the geometry would be
    fitted together and the problem is degenerate.
    """
    g = match.geometry
    c_eff = c_eff_from_speed_bias(match.detection.speed, g.speed_mps, c_assumed)
    elevation = float(g.elevation_deg)
    if not np.isfinite(elevation):
        elevation = float(
            np.degrees(
                np.arcsin(np.clip(g.altitude_agl_m / max(g.slant_cpa_m, 1e-9), -1.0, 1.0))
            )
        )
    bearing = float(g.bearing_deg)
    if not np.isfinite(bearing):
        raise ValueError("pass geometry has no bearing; cannot separate wind from temperature")
    return PathSample(
        c_eff=c_eff,
        sigma=sigma_c_eff(epsilon, g.speed_mps, c_assumed),
        bearing_deg=bearing,
        elevation_deg=elevation,
        altitude_m=g.altitude_agl_m,
        t=g.t_heard_cpa,
        icao24=g.icao24,
    )


# --------------------------------------------------------------------------------------
# Bulk solve: one path-averaged layer
# --------------------------------------------------------------------------------------


@dataclass
class Sounding:
    """A solved layer. Always carries its pass count, because a number without one is a lie."""

    c0: float
    wind_east: float
    wind_north: float
    n: int
    sigma_c0: float = float("nan")
    sigma_wind: float = float("nan")
    altitude_lo: float = 0.0
    altitude_hi: float = float("inf")
    condition: float = float("nan")
    note: str = ""

    @property
    def temperature_c(self) -> float:
        return temperature_from_c(self.c0)

    @property
    def sigma_temperature_c(self) -> float:
        """Uncertainty in temperature: sigma_c DIVIDED by dc/dT, which is ~1.7x, not 0.59x."""
        return float(self.sigma_c0 / physics.d_sound_speed_dt(self.temperature_c))

    @property
    def wind_speed(self) -> float:
        return float(np.hypot(self.wind_east, self.wind_north))

    @property
    def wind_bearing_deg(self) -> float:
        """Meteorological convention: the direction the wind blows *from*."""
        return float((np.degrees(np.arctan2(self.wind_east, self.wind_north)) + 180.0) % 360.0)

    def as_dict(self) -> dict:
        return {
            "altitude_lo_m": self.altitude_lo,
            "altitude_hi_m": self.altitude_hi,
            "n_passes": self.n,
            "c0_mps": self.c0,
            "sigma_c0_mps": self.sigma_c0,
            "temperature_c": self.temperature_c,
            "sigma_temperature_c": self.sigma_temperature_c,
            "wind_speed_mps": self.wind_speed,
            "wind_from_deg": self.wind_bearing_deg,
            "sigma_wind_mps": self.sigma_wind,
            "condition_number": self.condition,
            "note": self.note,
        }


def _design(samples: list[PathSample]) -> tuple[NDArray, NDArray, NDArray]:
    """Rows of [1, cos(elev) sin(bearing), cos(elev) cos(bearing)] -- isotropic plus wind."""
    a = np.empty((len(samples), 3))
    y = np.empty(len(samples))
    w = np.empty(len(samples))
    for i, s in enumerate(samples):
        ce = np.cos(np.radians(s.elevation_deg))
        br = np.radians(s.bearing_deg)
        a[i] = (1.0, ce * np.sin(br), ce * np.cos(br))
        y[i] = s.c_eff
        w[i] = s.weight
    return a, y, w


MIN_PASSES = 6
MAX_CONDITION = 50.0


def solve_bulk(
    samples: list[PathSample], min_passes: int = MIN_PASSES
) -> Sounding | None:
    """Sound speed and horizontal wind from a set of passes.

    Returns None rather than a number when there are too few passes. Flags, rather than hides,
    a poorly conditioned bearing distribution: if every pass came from one direction the wind
    component along that bearing is indistinguishable from a temperature change, and the
    solution will happily report a confident wrong answer.
    """
    if len(samples) < min_passes:
        return None
    a, y, w = _design(samples)
    sw = np.sqrt(w)
    aw, yw = a * sw[:, None], y * sw
    try:
        coef, *_ = np.linalg.lstsq(aw, yw, rcond=None)
        cond = float(np.linalg.cond(aw))
        cov = np.linalg.inv(aw.T @ aw)
    except np.linalg.LinAlgError:
        return None

    resid = yw - aw @ coef
    dof = max(len(samples) - 3, 1)
    scale = float(resid @ resid / dof)
    sig = np.sqrt(np.clip(np.diag(cov) * max(scale, 1.0), 0.0, None))

    note = ""
    if cond > MAX_CONDITION:
        note = (
            f"ill-conditioned (cond {cond:.0f}): bearings too clustered to separate wind "
            "from temperature"
        )
    return Sounding(
        c0=float(coef[0]),
        wind_east=float(coef[1]),
        wind_north=float(coef[2]),
        n=len(samples),
        sigma_c0=float(sig[0]),
        sigma_wind=float(np.hypot(sig[1], sig[2])),
        altitude_lo=float(min(s.altitude_m for s in samples)),
        altitude_hi=float(max(s.altitude_m for s in samples)),
        condition=cond,
        note=note,
    )


# --------------------------------------------------------------------------------------
# Layer inversion
# --------------------------------------------------------------------------------------


def layer_weights(altitude_m: float, edges: NDArray[np.float64]) -> NDArray[np.float64]:
    """Fraction of a path from the ground to `altitude_m` that lies in each layer.

    A near-overhead path samples height roughly uniformly, so the weights are just the overlap
    of [0, h] with each layer, normalised. This is what makes the inversion a *differencing*
    problem, and why thin layers amplify error so badly.
    """
    h = max(float(altitude_m), 1e-6)
    lo = np.clip(edges[:-1], 0.0, h)
    hi = np.clip(edges[1:], 0.0, h)
    overlap = np.clip(hi - lo, 0.0, None)
    total = overlap.sum()
    return overlap / total if total > 0 else overlap


@dataclass
class Profile:
    edges: NDArray[np.float64]
    layers: list[Sounding | None]
    regularisation: float
    n_total: int
    counts: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "edges_m": [float(e) for e in self.edges],
            "regularisation": self.regularisation,
            "n_total": self.n_total,
            "layers": [s.as_dict() if s else None for s in self.layers],
        }


def solve_profile(
    samples: list[PathSample],
    edges: NDArray[np.float64] | list[float],
    regularisation: float = 1.0,
    min_per_layer: int = 3,
) -> Profile:
    """Vertical profile of sound speed and wind, Tikhonov-smoothed.

    Each pass measures a path *average*, so recovering per-layer values means differencing
    overlapping averages -- an ill-conditioned problem whose error amplification is
    `sqrt(h1^2 + h2^2) / (h2 - h1)`: 2.2x for a 1-2 km layer and 9.2x for 3.0-3.5 km. The
    first-difference penalty is what keeps that from exploding; it buys conditioning with
    vertical resolution, which is the honest trade rather than a free lunch.

    Layers that no pass reaches are returned as None, not extrapolated.
    """
    edges = np.asarray(edges, dtype=float)
    n_layers = edges.size - 1
    if n_layers < 1:
        raise ValueError("need at least two layer edges")
    if not samples:
        return Profile(edges, [None] * n_layers, regularisation, 0, [0] * n_layers)

    n_unknown = 3 * n_layers  # c0, w_east, w_north per layer
    rows, ys, ws = [], [], []
    counts = np.zeros(n_layers, dtype=int)
    for s in samples:
        lw = layer_weights(s.altitude_m, edges)
        if lw.sum() <= 0:
            continue
        counts += (lw > 1e-6).astype(int)
        ce = np.cos(np.radians(s.elevation_deg))
        br = np.radians(s.bearing_deg)
        row = np.zeros(n_unknown)
        row[0:n_layers] = lw
        row[n_layers : 2 * n_layers] = lw * ce * np.sin(br)
        row[2 * n_layers : 3 * n_layers] = lw * ce * np.cos(br)
        rows.append(row)
        ys.append(s.c_eff)
        ws.append(s.weight)

    if not rows:
        return Profile(edges, [None] * n_layers, regularisation, 0, [0] * n_layers)

    a = np.array(rows)
    y = np.array(ys)
    sw = np.sqrt(np.array(ws))
    aw, yw = a * sw[:, None], y * sw

    # first-difference smoothness penalty, applied within each of the three fields
    pen = []
    for block in range(3):
        for j in range(n_layers - 1):
            r = np.zeros(n_unknown)
            r[block * n_layers + j] = -1.0
            r[block * n_layers + j + 1] = 1.0
            pen.append(r)
    if pen:
        scale = regularisation * float(np.sqrt((aw * aw).sum() / max(aw.size, 1)))
        aw = np.vstack([aw, np.array(pen) * scale])
        yw = np.concatenate([yw, np.zeros(len(pen))])

    coef, *_ = np.linalg.lstsq(aw, yw, rcond=None)
    try:
        cov = np.linalg.inv(aw.T @ aw)
        sig = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    except np.linalg.LinAlgError:
        sig = np.full(n_unknown, np.nan)

    layers: list[Sounding | None] = []
    for j in range(n_layers):
        if counts[j] < min_per_layer:
            layers.append(None)
            continue
        layers.append(
            Sounding(
                c0=float(coef[j]),
                wind_east=float(coef[n_layers + j]),
                wind_north=float(coef[2 * n_layers + j]),
                n=int(counts[j]),
                sigma_c0=float(sig[j]),
                sigma_wind=float(np.hypot(sig[n_layers + j], sig[2 * n_layers + j])),
                altitude_lo=float(edges[j]),
                altitude_hi=float(edges[j + 1]),
                note="" if counts[j] >= 20 else f"only {counts[j]} passes; expect wide bars",
            )
        )
    return Profile(edges, layers, regularisation, len(rows), [int(c) for c in counts])


def error_amplification(lo: float, hi: float) -> float:
    """`sqrt(lo^2 + hi^2) / (hi - lo)` -- why thin layers are hopeless. 1.0 for a surface layer."""
    if lo <= 0.0:
        return 1.0
    if hi <= lo:
        raise ValueError("hi must exceed lo")
    return float(np.hypot(lo, hi) / (hi - lo))
