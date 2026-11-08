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
JET_CLASSES = frozenset({"J", "R"})


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
        return (self.engine_class or "").upper() in JET_CLASSES

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
    track: Track | None = None

    @property
    def transition_s(self) -> float:
        """d/v -- how long the sweep takes, and so whether it is measurable at all."""
        return self.slant_cpa_m / max(self.speed_mps, 1e-6)


def _smooth_enu(track: Track, receiver: geo.Receiver):
    """Cubic splines of the ENU trajectory.

    Smoothed deliberately: differentiating raw ADS-B positions puts kinks in the modelled curve
    that the audio does not have, which is a documented cause of wrong answers in the
    geolocation literature. A dropped position report should not become a feature.
    """
    enu = receiver.enu_of(track.lat, track.lon, track.alt_hae_m)
    t = track.times
    k = 3 if t.size >= 4 else max(1, t.size - 1)
    s = max(len(t) * 4.0, 1.0)  # metres^2 of allowed misfit per point
    return [interpolate.UnivariateSpline(t, enu[:, i], k=k, s=s) for i in range(3)]


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
        track=track,
    )


def audible_candidates(
    tracks: list[Track],
    receiver: geo.Receiver,
    max_slant_m: float | None = None,
    min_speed_mps: float = 30.0,
    exclude_jets: bool = True,
    c: float = 343.0,
) -> list[PassGeometry]:
    """Tracks that *should* have been audible -- the reverse half of the join.

    This is what makes an unexplained detection interpretable and what measures detection
    probability against range. Without it you cannot tell a non-transmitting aircraft from one
    you simply failed to hear.
    """
    max_slant_m = physics.AUDIBILITY_RADIUS_M if max_slant_m is None else max_slant_m
    out = []
    for tr in tracks:
        if exclude_jets and tr.is_jet:
            continue
        g = pass_geometry(tr, receiver, c=c)
        if g is None or g.slant_cpa_m > max_slant_m or g.speed_mps < min_speed_mps:
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

    tracks = []
    for icao, rec in by_icao.items():
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
                meta={"alt_datum": sorted(rec["datum"])},
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
