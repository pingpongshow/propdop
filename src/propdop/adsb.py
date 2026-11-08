"""ADS-B tracks, and the pass geometry the join needs from them.

Loads readsb/`tar1090`-shaped JSON, computes the closest-approach geometry against a surveyed
receiver, and -- because propeller blade count is *not* in any standard aircraft table --
infers it from the data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from scipy import interpolate, optimize

from . import geo, physics

# ICAO Doc 8643 type designators encode engine count and type (L1P, L2T, ...), not blade count.
# Engine classes with no fixed-wing propeller comb for this detector to find: turbofans are
# broadband, and a helicopter's main rotor sits below the band while it does not fly the
# straight constant-speed pass the model inverts.
#
# This governs the DENOMINATOR only -- "should this miss count against the instrument?" -- and
# deliberately NOT which tracks the join may consider. Those are different questions, and
# conflating them is how a jet the detector did fire on becomes an unexplained detection: with
# no candidate to match, the one product that must never be wrong reports a non-cooperative
# aircraft. Measured on AeroSonicDB, 2 of 474 turbofan clips fire at the deployed threshold.
# Bounds on the per-sample ADS-B position noise the trajectory spline is allowed to infer, and
# the fallback for a track too short to infer it from. 10 m is roughly what NACp delivers
# horizontally, and geometric altitude is quantised to 25 ft = 7.6 m. See `_smooth_enu`.
POSITION_SIGMA_M = 10.0
# The feed cannot be more precise than its own quantisation, so claiming less than this is
# claiming precision ADS-B does not have. Geometric altitude is reported in 25 ft steps, and a
# uniform quantiser of step q has standard deviation q/sqrt(12) = 2.2 m; horizontal CPR is
# finer, so the same floor is conservative there. It was 1.0 m, which on a level-flight track
# -- where the altitude column is the same 25 ft figure over and over -- let `s` collapse
# below even the old fixed value and brought back the FITPACK warning it was meant to end.
POSITION_SIGMA_FLOOR_M = 25.0 * 0.3048 / 12 ** 0.5
POSITION_SIGMA_CEIL_M = 200.0

NON_PROPELLER_CLASSES = frozenset({"J", "R"})
JET_CLASSES = NON_PROPELLER_CLASSES      # retained name, used by older callers

# Doc 8643's engine letter is not in the ADS-B feed. adsb.lol and readsb publish the type
# designator (`t`) and the emitter category, but no `desc`, so `engine_class` stayed None for
# every live track and `is_jet` was therefore always False. `audible_candidates(exclude_jets=
# True)` silently excluded nothing: on the deployed station 10 of 12 "should have been
# audible" rows were airliners, which the detector is a propeller comb filter and cannot hear.
#
# This table is the common traffic, not all of Doc 8643. Anything absent resolves to None and
# is treated as a possible propeller, which is the conservative direction: the cost is an
# unheard row that should not exist, not a missed detection.
_ENGINE_CLASS_BY_TYPE: dict[str, str] = {}


def _register(cls: str, codes: str) -> None:
    for code in codes.split():
        _ENGINE_CLASS_BY_TYPE[code] = cls


_register("J", """
    A19N A20N A21N A318 A319 A320 A321 A332 A333 A337 A338 A339 A342 A343 A345 A346
    A359 A35K A388 A3ST BCS1 BCS3 B37M B38M B39M B3XM B703 B712 B720 B721 B722 B731
    B732 B733 B734 B735 B736 B737 B738 B739 B741 B742 B743 B744 B748 B752 B753 B762
    B763 B764 B772 B773 B77L B77W B788 B789 B78X E135 E145 E170 E175 E190 E195 E290
    E295 E45X E50P E545 E550 E55P E75L E75S CRJ1 CRJ2 CRJ7 CRJ9 CRJX C25A C25B C25C
    C500 C510 C525 C550 C560 C56X C650 C680 C68A C700 C750 CL30 CL35 CL60 GLEX GL5T
    GLF4 GLF5 GLF6 G280 GALX H25B HDJT LJ31 LJ35 LJ40 LJ45 LJ55 LJ60 LJ70 LJ75 F2TH
    FA50 FA7X FA8X PRM1 BE40 SF50 MD11 MD81 MD82 MD83 MD87 MD88 MD90 DC10 B461 B462
    B463 RJ85 RJ1H F70 F100
""")
_register("T", """
    AT43 AT44 AT45 AT46 AT72 AT73 AT75 AT76 ATP DH8A DH8B DH8C DH8D DHC6 DHC7 SF34
    SB20 SW2 SW3 SW4 JS31 JS32 JS41 B190 BE20 BE9L BE99 B350 C208 C212 C441 D228 D328
    E110 E120 F406 L410 PC12 PC6T PC21 TBM7 TBM8 TBM9 TBM0 AC90 AC95 P180 P46T PAY1
    PAY2 PAY3 PAY4 MU2 Y12 CN35 C295 AN26 AN32 C130 C30J LCH DC3T
""")
_register("P", """
    AA1 AA5 AC11 AC50 AEST BE23 BE24 BE33 BE35 BE36 BE50 BE55 BE58 BE60 BE76 BE95
    C150 C152 C162 C170 C172 C175 C177 C180 C182 C185 C188 C195 C206 C207 C210 C310
    C336 C337 C340 C402 C404 C411 C414 C421 CH7A CH70 COL4 DA20 DA36 DA40 DA42 DA50
    DA62 DR40 EVSS G115 GA7 GA8 GLAS GY80 J3 L4 M20P M20T M5 M7 NAVI P28A P28B P28R
    P28S P28T P32R P32T PA11 PA12 PA16 PA18 PA20 PA22 PA23 PA24 PA25 PA27 PA28
    PA30 PA31 PA32 PA34 PA38 PA39 PA44 PA46 PITT RV3 RV4 RV6 RV7 RV8 RV9 RV10 RV12
    RV14 S22T SR20 SR22 ST75 SUBA T206 T210 T303 T310 TOBA TRIN U206 V35 BL8 BT36
""")
# Rotorcraft, as "R" rather than by engine letter. Registering these as T/P was a real fault
# and not a cosmetic one: `NON_PROPELLER_CLASSES` and `store.detection_rate`'s
# `NOT IN ('J','R')` both existed, but NO type designator ever resolved to "R", so the whole
# rotorcraft exclusion was dead code. The deployed station put an AS350 at 1,173 m into the
# propeller denominator four times, and its score of 53.9 was the highest unheard row in the
# database -- a helicopter being measured as an aeroplane.
_register("R", """
    A109 A119 A139 A169 A189 AS32 AS50 AS55 AS65 B06 B06T B222 B230 B407 B412 B427
    B429 B430 B505 BK17 EC20 EC25 EC30 EC35 EC45 EC55 EC75 H500 H60 MD52 MD60 MD90H
    R66 S64 S76 S92 UH1 S61 S70 R22 R44 EN28 H269 S269 SCHW
""")


def engine_class_for_type(type_code: str | None, category: str | None = None) -> str | None:
    """Doc 8643 engine letter for an ICAO type designator, or None if not known.

    `category` is the ADS-B emitter category and is only consulted as a fallback, because it
    encodes *size* rather than engine type: A1 (light) and A2 (small) contain both pistons and
    light jets, so only the large-transport categories are decisive.
    """
    code = (type_code or "").strip().upper()
    if code in _ENGINE_CLASS_BY_TYPE:
        return _ENGINE_CLASS_BY_TYPE[code]
    cat = (category or "").strip().upper()
    if cat == "A7":
        return "R"      # the one category that names an airframe kind rather than a size
    if cat in {"A3", "A4", "A5"}:
        return "J"      # large, high-vortex-large and heavy are transports: jets in practice
    return None


@dataclass
class Track:
    """One aircraft's trajectory over a window, in the ADS-B geometric altitude datum."""

    icao24: str
    times: NDArray[np.float64]
    lat: NDArray[np.float64]
    lon: NDArray[np.float64]
    alt_hae_m: NDArray[np.float64]
    registration: str | None = None
    type_code: str | None = None
    engine_class: str | None = None
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        order = np.argsort(self.times)
        self.times = np.asarray(self.times, dtype=float)[order]
        self.lat = np.asarray(self.lat, dtype=float)[order]
        self.lon = np.asarray(self.lon, dtype=float)[order]
        self.alt_hae_m = np.asarray(self.alt_hae_m, dtype=float)[order]

    @property
    def is_jet(self) -> bool:
        """No propeller comb to find: a turbofan or a rotorcraft. Still a join candidate."""
        return (self.engine_class or "").upper() in NON_PROPELLER_CLASSES

    def __len__(self) -> int:
        return self.times.size


@dataclass
class PassGeometry:
    """Closest-approach geometry of one track past one receiver.

    `t_heard_cpa` is the quantity to compare against an acoustic detection: the CPA as *heard*,
    which is the emission-time CPA plus the propagation delay. At 6 km that is about 17 s
    later, so matching on the emission time would be wrong by far more than the timing
    tolerance.
    """

    icao24: str
    t_cpa_emit: float
    t_heard_cpa: float
    slant_cpa_m: float
    speed_mps: float
    altitude_agl_m: float
    audible_window: tuple[float, float]
    bearing_deg: float = float("nan")
    elevation_deg: float = float("nan")
    engine_class: str | None = None
    track: Track | None = None
    # Whether the observed track actually brackets the closest approach, i.e. the aircraft was
    # seen receding as well as approaching. False means the minimum sits on an endpoint, so
    # this is not a CPA but "the closest it got before the track ran out" -- see
    # `pass_geometry`.
    cpa_bracketed: bool = True

    @property
    def expected_audible(self) -> bool:
        """Whether failing to hear this track says anything about the instrument.

        A turbofan has no comb and a rotorcraft does not fly the straight pass the model
        inverts, so a miss on either measures nothing and must stay out of the detection-rate
        denominator. It is still a legitimate join candidate -- see `NON_PROPELLER_CLASSES`.
        """
        return (self.engine_class or "").upper() not in NON_PROPELLER_CLASSES

    @property
    def transition_s(self) -> float:
        """d/v -- how long the sweep takes, and so whether it is measurable at all."""
        return self.slant_cpa_m / max(self.speed_mps, 1e-6)


def _position_sigma_m(x: NDArray[np.float64]) -> float:
    """Per-sample position noise along one axis, from the scatter of second differences.

    A trajectory is smooth, so its second difference is near zero and what remains is noise,
    with variance 6*sigma^2. Taken as a median absolute deviation rather than a standard
    deviation: a dropped report or a mode-C altitude step is an outlier, and must not be
    allowed to set the smoothing for the whole track.
    """
    if x.size < 5:
        return POSITION_SIGMA_M
    d2 = np.diff(x, n=2)
    scale = 0.6745 * np.sqrt(6.0)
    sigma = float(np.median(np.abs(d2 - np.median(d2)))) / scale

    # A median absolute deviation of zero does not mean the data is clean. It means more than
    # half the second differences are identical, which is what a *quantised* series looks
    # like: an aircraft in level flight reports the same 25 ft altitude figure for a minute,
    # and a held position report repeats exactly. The MAD then measures the flat stretches and
    # ignores the steps between them, so the spline is told to chase a staircase. The RMS sees
    # the steps, so fall back to it -- on genuinely noisy data the two agree, and it only
    # takes over where the MAD has stopped describing anything.
    if sigma < POSITION_SIGMA_FLOOR_M:
        sigma = float(np.sqrt(np.mean(d2 ** 2))) / np.sqrt(6.0)
    return sigma if np.isfinite(sigma) else POSITION_SIGMA_M


def _smooth_enu(track: Track, receiver: geo.Receiver):
    """Cubic splines of the ENU trajectory.

    Smoothed deliberately: differentiating raw ADS-B positions puts kinks in the modelled curve
    that the audio does not have, which is a documented cause of wrong answers in the
    geolocation literature. A dropped position report should not become a feature.

    `s` is the total allowed misfit, so per point it is the *variance of the position error*.
    It was a hard-coded 4 m^2 -- a 2 m RMS that ADS-B does not deliver. FITPACK cannot reach a
    misfit that small, which is why the station's log filled with "the maximal number of
    iterations maxit (set to 20 by the program) allowed for finding a smoothing spline with
    fp=s has been reached: s too small". The spline returned in that state interpolates the
    noise, which is the exact failure this function exists to prevent, and it put a 10-25 %
    error on the *truth* side of a join whose speed tolerance is `SIGMA_SPEED_FRAC` = 10 %.

    No single constant fixes it, because the noise is not a constant: it depends on the
    aircraft's equipment and on how far away it is. Smoothing less than the noise tracks the
    noise; smoothing more than it cuts the corner on a turn. So the noise is measured from the
    track itself, and `s` follows it. Median speed error over 16 seeds, against known truth:

    | position noise | s = 4n (deployed) | s = 100n | measured from the track |
    |---|---|---|---|
    | 0 m, turning | 0.11 % | 0.96 % | **0.13 %** |
    | 10 m, straight | 9.47 % | 0.06 % | **0.04 %** |
    | 25 m, straight | 19.67 % | 9.64 % | **0.11 %** |
    | 40 m, straight | 25.00 % | 25.83 % | **0.19 %** |
    | 40 m, turning | 23.82 % | 27.55 % | **1.37 %** |

    The estimator recovers the jitter it was given to within 2 % (5.1, 10.1, 25.3, 40.6 m for
    5, 10, 25, 40), so this is self-calibration rather than another tuned constant.
    """
    enu = receiver.enu_of(track.lat, track.lon, track.alt_hae_m)
    t = track.times
    k = 3 if t.size >= 4 else max(1, t.size - 1)
    # Per axis, not one figure for all three. East, north and up do not share an error
    # distribution: altitude arrives in 25 ft steps while position does not, so on a real
    # track the altitude estimate is near zero while the horizontal ones are 10-100 m. Taking
    # the median across the three applied one axis's noise to another, in both directions --
    # measured on live traffic, one track had 0.05 m east and 9.2 m north, another 50 m east
    # and 1.1 m up. Each axis gets its own spline anyway, so each gets its own `s`.
    out = []
    for i in range(3):
        sigma = float(np.clip(_position_sigma_m(enu[:, i]),
                              POSITION_SIGMA_FLOOR_M, POSITION_SIGMA_CEIL_M))
        out.append(interpolate.UnivariateSpline(t, enu[:, i], k=k,
                                                s=max(len(t) * sigma**2, 1.0)))
    return out


def pass_geometry(
    track: Track,
    receiver: geo.Receiver,
    c: float = 343.0,
    audible_fraction: float = 3.0,
) -> PassGeometry | None:
    """Closest approach of `track` past `receiver`, with the heard CPA time."""
    if len(track) < 4:
        return None
    sx, sy, sz = _smooth_enu(track, receiver)

    def slant(t):
        return float(np.sqrt(sx(t) ** 2 + sy(t) ** 2 + sz(t) ** 2))

    t0, t1 = float(track.times[0]), float(track.times[-1])
    grid = np.linspace(t0, t1, max(32, int(t1 - t0)))
    vals = np.array([slant(t) for t in grid])
    i = int(np.argmin(vals))
    lo = grid[max(0, i - 1)]
    hi = grid[min(grid.size - 1, i + 1)]
    if hi > lo:
        res = optimize.minimize_scalar(slant, bounds=(lo, hi), method="bounded")
        t_cpa = float(res.x)
    else:
        t_cpa = float(grid[i])

    # A minimum that lands on an endpoint is not a closest approach: the aircraft was still
    # approaching (or already receding) when the track ran out, and the "CPA" is wherever the
    # observation happened to stop. The station filed a Bonanza as unheard at a predicted
    # 3017 m, then heard it 36 s later when the next window saw the real 828 m pass -- one
    # aircraft, in both halves of the detection rate. Reported rather than rejected here,
    # because the forward join and the reverse one want different things from it.
    bracketed = 0 < i < grid.size - 1

    d = slant(t_cpa)
    vel = np.array([sx.derivative()(t_cpa), sy.derivative()(t_cpa), sz.derivative()(t_cpa)])
    speed = float(np.linalg.norm(vel))
    agl = float(sz(t_cpa))

    east, north = float(sx(t_cpa)), float(sy(t_cpa))
    bearing = float(np.degrees(np.arctan2(east, north)) % 360.0)
    elevation = float(np.degrees(np.arctan2(agl, max(np.hypot(east, north), 1e-9))))

    half = audible_fraction * d / max(speed, 1e-6)
    return PassGeometry(
        icao24=track.icao24,
        t_cpa_emit=t_cpa,
        t_heard_cpa=t_cpa + d / c,
        slant_cpa_m=d,
        speed_mps=speed,
        altitude_agl_m=agl,
        audible_window=(t_cpa + d / c - half, t_cpa + d / c + half),
        bearing_deg=bearing,
        elevation_deg=elevation,
        engine_class=track.engine_class,
        track=track,
        cpa_bracketed=bracketed,
    )


def audible_candidates(
    tracks: list[Track],
    receiver: geo.Receiver,
    max_slant_m: float | None = None,
    min_speed_mps: float = 30.0,
    exclude_jets: bool = False,
    c: float = 343.0,
) -> list[PassGeometry]:
    """Every aircraft inside the audibility radius -- the reverse half of the join.

    This is what makes an unexplained detection interpretable and what measures detection
    probability against range. Without it you cannot tell a non-transmitting aircraft from one
    you simply failed to hear.

    `exclude_jets` now defaults to False, which is a deliberate reversal. It conflated two
    questions that pull in opposite directions:

    * *What could this detection be?* Everything overhead, jets included. A detection the
      detector made on a turbofan must be able to find its own track, or it is published as a
      non-cooperative aircraft -- the one claim this instrument cannot afford to get wrong.
    * *Does this miss count against us?* Propeller aeroplanes only. Use
      `PassGeometry.expected_audible`, which `store.detection_rate` applies to both halves of
      the ratio.

    Filtering here answered the first question with the second one's answer.
    """
    max_slant_m = physics.AUDIBILITY_RADIUS_M if max_slant_m is None else max_slant_m
    out = []
    for tr in tracks:
        if exclude_jets and tr.is_jet:
            continue
        g = pass_geometry(tr, receiver, c=c)
        if g is None or g.slant_cpa_m > max_slant_m or g.speed_mps < min_speed_mps:
            continue
        # An unbracketed CPA is an extrapolation, and wrong in a specific direction: too far
        # and too early, because the aircraft is still inbound. It is no use to either half of
        # the join -- a match made against it is a match to the wrong geometry, and a miss
        # recorded against it is a miss on a pass that has not happened yet. Consecutive
        # analysis windows overlap by design, so the next one sees the completed pass; this is
        # what that overlap is for.
        if not g.cpa_bracketed:
            continue
        out.append(g)
    return sorted(out, key=lambda g: g.t_heard_cpa)


# --------------------------------------------------------------------------------------
# Loading readsb / tar1090 output
# --------------------------------------------------------------------------------------


def tracks_from_readsb(
    snapshots: list[dict],
    geoid_offset_m: float = 0.0,
    prefer_geometric_alt: bool = True,
) -> list[Track]:
    """Build tracks from a series of readsb `aircraft.json` snapshots.

    Each snapshot is one decoded `{"now": ..., "aircraft": [...]}`. Geometric altitude
    (`alt_geom`, feet above the ellipsoid) is preferred; barometric `alt_baro` is pressure
    altitude and needs the local QNH, so when it is all that is on offer the track is flagged
    rather than silently mixed into the same datum.
    """
    by_icao: dict[str, dict[str, list]] = {}
    for snap in snapshots:
        now = float(snap.get("now", 0.0))
        if now > 1e11:  # millisecond timestamps, as adsb.lol serves them
            now /= 1000.0
        for ac in snap.get("aircraft", []):
            icao = ac.get("hex")
            lat, lon = ac.get("lat"), ac.get("lon")
            if icao is None or lat is None or lon is None:
                continue
            alt_ft, datum = ac.get("alt_geom"), "geom"
            if alt_ft is None or not prefer_geometric_alt:
                alt_ft, datum = ac.get("alt_baro"), "baro"
            if alt_ft is None or isinstance(alt_ft, str):  # "ground"
                continue
            t = now - float(ac.get("seen_pos", 0.0))
            rec = by_icao.setdefault(
                icao,
                {
                    "t": [],
                    "lat": [],
                    "lon": [],
                    "alt": [],
                    "datum": set(),
                    "reg": None,
                    "type": None,
                    "cat": None,
                },
            )
            rec["t"].append(t)
            rec["lat"].append(float(lat))
            rec["lon"].append(float(lon))
            # readsb reports feet; alt_baro is pressure altitude, not a height above anything
            rec["alt"].append(float(alt_ft) * 0.3048 + (geoid_offset_m if datum == "baro" else 0.0))
            rec["datum"].add(datum)
            rec["reg"] = rec["reg"] or ac.get("r")
            rec["type"] = rec["type"] or ac.get("t")
            rec["cat"] = rec["cat"] or ac.get("category")

    tracks = []
    for icao, rec in by_icao.items():
        # One position report, not one per snapshot that happened to see it. `t` is already
        # the time the position was *fixed* (`now - seen_pos`), so a report still being
        # broadcast when the next poll lands arrives again with the identical timestamp. Kept,
        # those repeats are a staircase: flat stretches joined by steps, which a smoothing
        # spline either chases or is told to ignore, and the speed read off its derivative is
        # then wrong by tens of per cent. They are not extra evidence -- they are the same
        # measurement, counted again.
        seen: set[float] = set()
        keep = []
        for i, t in enumerate(rec["t"]):
            key = round(float(t), 2)
            if key in seen:
                continue
            seen.add(key)
            keep.append(i)
        if len(keep) < len(rec["t"]):
            for field in ("t", "lat", "lon", "alt"):
                rec[field] = [rec[field][i] for i in keep]
        if len(rec["t"]) < 4:
            continue
        tracks.append(
            Track(
                icao24=icao,
                times=np.array(rec["t"]),
                lat=np.array(rec["lat"]),
                lon=np.array(rec["lon"]),
                alt_hae_m=np.array(rec["alt"]),
                registration=rec["reg"],
                type_code=rec["type"],
                engine_class=engine_class_for_type(rec["type"], rec["cat"]),
                meta={"alt_datum": sorted(rec["datum"]), "category": rec["cat"]},
            )
        )
    return tracks


def load_readsb_dir(path: str | Path, **kw) -> list[Track]:
    """Load every `*.json` snapshot in a directory, oldest first."""
    files = sorted(Path(path).glob("*.json"))
    snaps = []
    for f in files:
        try:
            snaps.append(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return tracks_from_readsb(snaps, **kw)


# --------------------------------------------------------------------------------------
# Blade count, which no standard table provides
# --------------------------------------------------------------------------------------


def infer_blade_count(
    f0_values: NDArray[np.float64],
    rpm_range: tuple[float, float] = (2100.0, 2750.0),
    candidates: tuple[int, ...] = (2, 3, 4, 5, 6),
) -> tuple[int | None, dict[int, float]]:
    """Infer propeller blade count for one aircraft type from a set of measured `f0`.

    This closes a real data gap. `f0 = N * RPM / 60` gives RPM only if N is known, and **N is
    not in ICAO Doc 8643**, which encodes engine count and type but not blades. Without N, a
    90 Hz tone is 2,700 RPM on two blades or 1,800 on three.

    But N is identifiable from the data. Across repeat passes of one type, only the correct N
    puts the implied RPM inside a plausible cruise band *consistently*. The score is the
    fraction of observations landing in band, tie-broken by how tightly they cluster -- a
    cruise setting should be repeatable, so the true N also minimises spread.

    Returns (best N or None, per-candidate score). None means the observations do not
    distinguish, which is the honest answer for a handful of passes.
    """
    f0 = np.asarray(f0_values, dtype=float)
    f0 = f0[np.isfinite(f0) & (f0 > 0)]
    scores: dict[int, float] = {}
    if f0.size == 0:
        return None, scores

    lo, hi = rpm_range
    for n in candidates:
        rpm = f0 * 60.0 / n
        in_band = (rpm >= lo) & (rpm <= hi)
        frac = float(np.mean(in_band))
        if frac == 0.0:
            scores[n] = 0.0
            continue
        spread = float(np.std(rpm[in_band]) / np.mean(rpm[in_band])) if in_band.sum() > 1 else 1.0
        scores[n] = frac * (1.0 / (1.0 + 10.0 * spread))

    best = max(scores, key=scores.get)
    runner = sorted(scores.values(), reverse=True)
    if scores[best] <= 0.0:
        return None, scores
    if len(runner) > 1 and runner[1] > 0.0 and scores[best] < 1.5 * runner[1]:
        return None, scores  # not separated; say so rather than guess
    return best, scores


def rpm_from_f0(f0: float, n_blades: int) -> float:
    return float(physics.rpm_from_blade_pass(f0, n_blades))


def synthetic_track(
    receiver: geo.Receiver,
    speed_mps: float,
    slant_cpa_m: float,
    altitude_agl_m: float,
    heading_deg: float = 0.0,
    t_cpa: float = 0.0,
    half_span_s: float = 120.0,
    rate_hz: float = 1.0,
    icao24: str = "abc123",
    type_code: str = "C172",
    engine_class: str = "P",
    jitter_m: float = 0.0,
    seed: int = 0,
) -> Track:
    """A straight constant-velocity track with a specified CPA past `receiver`.

    Lets a synthetic ADS-B track be built to match a synthetic audio clip, so the join can be
    tested end to end against known truth. `jitter_m` adds position noise, which is what makes
    trajectory smoothing worth having.
    """
    if altitude_agl_m > slant_cpa_m:
        raise ValueError("altitude cannot exceed the slant CPA distance")
    lateral = float(np.sqrt(slant_cpa_m**2 - altitude_agl_m**2))
    t = np.arange(-half_span_s, half_span_s + 1e-9, 1.0 / rate_hz) + t_cpa
    along = speed_mps * (t - t_cpa)

    hdg = np.radians(heading_deg)
    # unit vector along the flight path, and the perpendicular offset in the horizontal plane
    fwd = np.array([np.sin(hdg), np.cos(hdg), 0.0])
    side = np.array([np.cos(hdg), -np.sin(hdg), 0.0])
    enu = (
        along[:, None] * fwd[None, :]
        + lateral * side[None, :]
        + np.array([0.0, 0.0, altitude_agl_m])[None, :]
    )
    if jitter_m > 0.0:
        enu = enu + np.random.default_rng(seed).normal(scale=jitter_m, size=enu.shape)

    lat, lon, h = geo.enu_to_geodetic(enu, receiver.lat, receiver.lon, receiver.ecef)
    return Track(
        icao24=icao24,
        times=t,
        lat=lat,
        lon=lon,
        alt_hae_m=h,
        type_code=type_code,
        engine_class=engine_class,
        registration=f"N{icao24[:5].upper()}",
    )


# --------------------------------------------------------------------------------------
# Live ADS-B from the internet, when there is no local receiver
# --------------------------------------------------------------------------------------

ADSB_LOL_URL = "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist}"


def fetch_adsb_lol(
    lat: float,
    lon: float,
    dist_nm: int = 50,
    round_deg: float = 0.1,
    timeout: float = 20.0,
    url_template: str = ADSB_LOL_URL,
) -> dict:
    """One live snapshot from adsb.lol, shaped like a readsb `aircraft.json`.

    Free and keyless, and a workable stand-in for a local receiver -- with two caveats worth
    stating. Coverage comes from volunteer feeders, so low-altitude traffic near a quiet site
    may be missing exactly where a local antenna would hear it; and nothing here sees aircraft
    that broadcast no ADS-B at all, which in US Class G below 10,000 ft is not a small group.

    **The query centre is rounded to `round_deg` (~11 km at 0.1 deg) before it leaves the
    machine.** The search radius is tens of kilometres anyway, so rounding costs no traffic,
    and it avoids handing a third party the exact coordinates of a microphone -- which is the
    very thing this project exists to show is sensitive.
    """
    import urllib.request

    qlat = round(lat / round_deg) * round_deg if round_deg > 0 else lat
    qlon = round(lon / round_deg) * round_deg if round_deg > 0 else lon
    url = url_template.format(lat=f"{qlat:.4f}", lon=f"{qlon:.4f}", dist=int(dist_nm))
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    return normalise_adsb_lol(payload)


def normalise_adsb_lol(payload: dict) -> dict:
    """adsb.lol v2 uses `ac`; readsb uses `aircraft`. Otherwise the fields line up.

    **Except the units of `now`.** adsb.lol returns it in *milliseconds* while `seen_pos` on
    each aircraft stays in *seconds* -- so taking `now` at face value stretches every track's
    time span by 1000x, divides every speed by 1000, and puts the timestamps in a different
    epoch entirely from the audio.

    The failure is silent and total: a 90 m/s aircraft reports 0.09 m/s, every track is then
    dropped by the >=30 m/s audibility filter, and the join produces nothing at all while every
    component reports success. Normalise here rather than downstream, so there is exactly one
    place where the convention is decided.
    """
    now = float(payload.get("now", 0.0))
    if now > 1e11:  # seconds since 1970 will not reach 1e11 until the year 5138
        now /= 1000.0
    return {
        "now": now,
        "aircraft": payload.get("ac") or payload.get("aircraft") or [],
    }


def collect_live_tracks(
    lat: float,
    lon: float,
    duration_s: float = 600.0,
    interval_s: float = 15.0,
    dist_nm: int = 50,
    sleep=None,
    fetch=None,
    **kw,
) -> list[Track]:
    """Poll a live feed for a while and assemble tracks.

    `interval_s` of 15 s is a deliberate compromise: often enough that a 100 m/s aircraft moves
    1.5 km between samples, which the trajectory spline handles comfortably, and gentle on a
    free community service. Shorter intervals buy little, because the CPA geometry is fitted
    from a smoothed path rather than read off one sample.
    """
    import time as _time

    sleep = sleep or _time.sleep
    fetch = fetch or (lambda: fetch_adsb_lol(lat, lon, dist_nm=dist_nm, **kw))
    snaps = []
    deadline = (_time.monotonic() if sleep is _time.sleep else 0.0) + duration_s
    n = max(1, int(round(duration_s / max(interval_s, 1e-6))))
    for i in range(n):
        try:
            snaps.append(fetch())
        except Exception:  # a dropped poll must not end the collection
            pass
        if i < n - 1:
            sleep(interval_s)
    return tracks_from_readsb(snaps)
