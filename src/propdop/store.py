"""The pass database: one measurement, read six ways.

Every product is a query over one table rather than a separate pipeline, so this is what turns
the comb fit into an instrument. SQLite is used directly -- stdlib, no server, and the schema is
plain enough to move to TimescaleDB unchanged when the deployment wants hypertables.

Two design commitments come from NETWORK.md 10, both of which are far harder to retrofit across
deployed stations than to get right now:

1. **The report schema is shareable.** Station metadata is separated from per-pass rows, every
   row carries the station it came from, and `export_reports` emits a versioned, newline-
   delimited format that merges without conflict.
2. **Negative detections are stored.** Passes that ADS-B says should have been audible and were
   not are recorded in `unheard`. Without them a station cannot compute its own detection rate,
   and "unexplained detection" has no denominator.
"""

from __future__ import annotations

import base64
import io
import json
import sqlite3
import threading
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from . import adsb, associate, detect, fit

SCHEMA_VERSION = 1
REPORT_FORMAT = "propdop-report/1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Shared once per station. The calibration fields are not paperwork: per NETWORK.md 11, site
-- heterogeneity dominates the differential signal a network depends on.
CREATE TABLE IF NOT EXISTS stations (
    station_id        TEXT PRIMARY KEY,
    lat               REAL NOT NULL,
    lon               REAL NOT NULL,
    elevation_msl_m   REAL NOT NULL,
    geoid_offset_m    REAL NOT NULL,
    microphone        TEXT,
    windscreen        TEXT,
    spl_calibrated    INTEGER NOT NULL DEFAULT 0,
    ground_sources    TEXT,
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS passes (
    pass_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id        TEXT NOT NULL REFERENCES stations(station_id),
    t_heard_cpa       REAL NOT NULL,
    source            TEXT NOT NULL,              -- 'blind' or 'matched'

    -- identity, from the ADS-B join
    icao24            TEXT,
    registration      TEXT,
    type_code         TEXT,
    engine_class      TEXT,

    -- ADS-B truth geometry
    adsb_speed_mps    REAL,
    adsb_slant_m      REAL,
    adsb_agl_m        REAL,
    adsb_t_heard_cpa  REAL,
    adsb_bearing_deg  REAL,
    adsb_elevation_deg REAL,
    match_chi         REAL,
    match_ambiguous   INTEGER NOT NULL DEFAULT 0,

    -- the acoustic measurement
    fit_f0_hz         REAL NOT NULL,
    fit_speed_mps     REAL NOT NULL,
    fit_slant_m       REAL NOT NULL,
    epsilon           REAL NOT NULL,
    epsilon_ridge     REAL,
    comb_contrast     REAL,
    template_score    REAL,
    n_harmonics       INTEGER,
    frames_used       INTEGER,
    stft_window_s     REAL,
    converged         INTEGER NOT NULL DEFAULT 1,

    -- derived products
    blade_count       INTEGER,
    blade_source      TEXT,
    rpm               REAL,
    scint_exponent    REAL,
    freq_coherence    REAL,

    -- context
    surf_temp_c       REAL,
    surf_rh_pct       REAL,
    surf_pressure_kpa REAL,
    clock_offset_s    REAL,
    notes             TEXT,

    -- per-harmonic tracks, the permanent dataset at ~15 kB/pass
    tracks            BLOB,
    UNIQUE (station_id, t_heard_cpa, fit_f0_hz)
);


-- Negative detections. As valuable as the positives: NETWORK.md 10.
CREATE TABLE IF NOT EXISTS unheard (
    unheard_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id        TEXT NOT NULL REFERENCES stations(station_id),
    t_heard_cpa       REAL NOT NULL,
    icao24            TEXT,
    type_code         TEXT,
    adsb_speed_mps    REAL,
    adsb_slant_m      REAL,
    adsb_agl_m        REAL,
    adsb_bearing_deg  REAL,
    -- Best blind-detector score anywhere near the expected arrival, stored even though it
    -- failed to clear the threshold. A negative without this number is uninterpretable.
    best_template_score REAL,
    -- Why the best scoring candidate near this arrival was not accepted. Without it,
    -- "scored, screened out" is a verdict with no diagnosis attached.
    reject_reason     TEXT,
    engine_class      TEXT,
    UNIQUE (station_id, t_heard_cpa, icao24)
);

"""

# Indexes are applied *after* migration, not inside _SCHEMA. An index on a column that
# an older database is missing fails before ADD COLUMN ever runs, so the ordering matters.
_INDEXES = """
CREATE INDEX IF NOT EXISTS passes_time ON passes (t_heard_cpa);
CREATE INDEX IF NOT EXISTS passes_reg  ON passes (registration);
CREATE INDEX IF NOT EXISTS passes_type ON passes (type_code);
CREATE INDEX IF NOT EXISTS unheard_time ON unheard (t_heard_cpa);
"""


@dataclass
class Station:
    station_id: str
    lat: float
    lon: float
    elevation_msl_m: float
    geoid_offset_m: float
    microphone: str | None = None
    windscreen: str | None = None
    spl_calibrated: bool = False
    ground_sources: str | None = None
    notes: str | None = None

    @classmethod
    def from_receiver(cls, rx, **kw) -> "Station":
        return cls(
            station_id=kw.pop("station_id", rx.name),
            lat=rx.lat,
            lon=rx.lon,
            elevation_msl_m=rx.elevation_msl_m,
            geoid_offset_m=rx.geoid_offset_m,
            **kw,
        )


def pack_tracks(result: fit.CombFit, store_rate_hz: float = 5.0) -> bytes:
    """Per-harmonic tracks as a compressed blob, within the ~15 kB/pass budget.

    Three choices get it there, and the first one is a correction to BUILD.md 2:

    * **5 Hz, not 2 Hz.** The scintillation decorrelation time is the Fresnel scale crossed at
      ~v/2, which is 1-2.5 s (SCINTILLATION.md 4). Sampling the fluctuation at 2 Hz sits right
      on its Nyquist limit and would alias; 5 Hz is comfortable and still cheap.
    * **Residuals, not absolutes.** Frequency is stored as the departure from k * the fitted
      model and log-amplitude with its mean removed. Both are small and zero-centred, which
      both compresses far better and is what every downstream product actually reads.
    * **float16, and no time axis.** The grid is uniform, so t0 and dt are scalars. Residual
      precision at float16 is ~0.001 Hz and ~0.001 in log-amplitude, far finer than the
      measurement. Storing 20 Hz float32 absolutes instead gave 164 kB/pass.

    The fit parameters travel in the metadata, so the absolute tracks are reconstructable.
    """
    step = max(1, int(round((1.0 / store_rate_hz) / _track_dt(result))))
    t = result.track_times[::step]
    arrays: dict[str, np.ndarray] = {}
    for k in result.harmonics:
        model = np.interp(t, result.ridge_times, result.model_freqs) * k
        df = result.freq_tracks[k][::step] - model
        la = np.log(np.clip(result.amp_tracks[k][::step], 1e-12, None))
        arrays[f"df{k}"] = df.astype(np.float16)
        arrays[f"la{k}"] = (la - la.mean()).astype(np.float16)
        arrays[f"lm{k}"] = np.float32(la.mean())
    buf = io.BytesIO()
    np.savez(buf, **arrays)
    meta = json.dumps(
        {
            "harmonics": list(result.harmonics),
            "noise_bias": {str(k): v for k, v in result.noise_bias.items()},
            "t0": float(t[0]) if t.size else 0.0,
            "dt": float(t[1] - t[0]) if t.size > 1 else 0.0,
            "n": int(t.size),
            "fit": {
                "f0": result.f0,
                "speed": result.speed,
                "cpa_distance": result.cpa_distance,
                "t0_heard": result.t0_heard,
            },
        }
    ).encode()
    body = buf.getvalue()
    return zlib.compress(len(meta).to_bytes(4, "big") + meta + body, 9)


def _track_dt(result: fit.CombFit) -> float:
    t = result.track_times
    return float(t[1] - t[0]) if t.size > 1 else 0.05


def unpack_tracks(blob: bytes) -> dict:
    """Inverse of `pack_tracks`. Rebuilds the time axis and the absolute log-amplitude."""
    raw = zlib.decompress(blob)
    n = int.from_bytes(raw[:4], "big")
    meta = json.loads(raw[4 : 4 + n])
    with np.load(io.BytesIO(raw[4 + n :])) as z:
        arrays = {k: z[k] for k in z.files}
    out: dict = {"meta": meta}
    out["t"] = meta["t0"] + meta["dt"] * np.arange(meta["n"], dtype=float)
    for k in meta["harmonics"]:
        out[f"df{k}"] = arrays[f"df{k}"].astype(np.float64)
        out[f"la{k}"] = arrays[f"la{k}"].astype(np.float64)
        out[f"lm{k}"] = float(arrays[f"lm{k}"])
    return out


class Store:
    """The pass database. Open on a path, or ':memory:' for tests."""

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        # `check_same_thread=False` plus a lock, because the pipeline reads counts from its
        # capture thread while writing passes from its analysis thread. The default
        # (True) raises ProgrammingError on any cross-thread use -- which in deployment
        # presented as a silently restarting capture loop that recorded nothing, not as an
        # error anyone saw.
        self.conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
        self._lock = threading.RLock()
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            # The run loop writes while the web UI reads, from separate containers sharing the
            # volume. WAL allows concurrent readers alongside one writer; the default rollback
            # journal would lock them against each other.
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.executescript(_INDEXES)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        with self._lock:
            self.conn.commit()

    def _migrate(self) -> None:
        """Add columns that a database created by an older build is missing.

        `CREATE TABLE IF NOT EXISTS` does nothing to an existing table, so a new column in
        `_SCHEMA` is invisible to a deployed database -- which presented as a 500 on the
        dashboard the first time the schema grew under a running instance. SQLite's
        `ADD COLUMN` is cheap and non-destructive, so reconcile on every open.
        """
        wanted = {
            "passes": {
                "adsb_bearing_deg": "REAL",
                "adsb_elevation_deg": "REAL",
                "scint_exponent": "REAL",
                "freq_coherence": "REAL",
            },
            "unheard": {"adsb_bearing_deg": "REAL", "best_template_score": "REAL",
                        "reject_reason": "TEXT", "engine_class": "TEXT"},
        }
        for table, cols in wanted.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols.items():
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        self.conn.commit()

    def execute(self, sql: str, args: tuple = ()):
        """Write path. Fetching must use `query_one`/`query_all` instead.

        Holding the lock only across `execute` is not enough: a cursor from a shared
        connection can be stepped by another thread between `execute` and `fetchone`, which
        returns another query's rows rather than raising. Reads therefore fetch inside the
        lock, as one unit.
        """
        with self._lock:
            return self.conn.execute(sql, args)

    def query_all(self, sql: str, args: tuple = ()) -> list:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def query_one(self, sql: str, args: tuple = ()):
        with self._lock:
            return self.conn.execute(sql, args).fetchone()

    def close(self):
        with self._lock:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        with self._lock:
            self.conn.commit()
        self.close()

    # -- writes -------------------------------------------------------------------------

    def add_station(self, station: Station) -> None:
        d = asdict(station)
        d["spl_calibrated"] = int(d["spl_calibrated"])
        cols = ", ".join(d)
        self.execute(
            f"INSERT OR REPLACE INTO stations ({cols}) VALUES ({', '.join('?' * len(d))})",
            tuple(d.values()),
        )
        with self._lock:
            self.conn.commit()

    def add_pass(
        self,
        station_id: str,
        detection: detect.Detection,
        match: associate.Match | None = None,
        blade_count: int | None = None,
        blade_source: str | None = None,
        surface: dict | None = None,
        clock_offset_s: float = 0.0,
        notes: str | None = None,
    ) -> int:
        r = detection.fit
        if r is None:
            raise ValueError("detection has no fit; nothing to store")
        scint = r.scintillation_scaling()
        g = match.geometry if match else None
        row = {
            "station_id": station_id,
            "t_heard_cpa": detection.t0_heard,
            "source": "matched" if match else "blind",
            "icao24": g.icao24 if g else None,
            "registration": (g.track.registration if g and g.track else None),
            "type_code": (g.track.type_code if g and g.track else None),
            "engine_class": (g.track.engine_class if g and g.track else None),
            "adsb_speed_mps": g.speed_mps if g else None,
            "adsb_slant_m": g.slant_cpa_m if g else None,
            "adsb_agl_m": g.altitude_agl_m if g else None,
            "adsb_t_heard_cpa": g.t_heard_cpa if g else None,
            "adsb_bearing_deg": g.bearing_deg if g else None,
            "adsb_elevation_deg": g.elevation_deg if g else None,
            "match_chi": match.chi if match else None,
            "match_ambiguous": int(bool(match and match.ambiguous)),
            "fit_f0_hz": r.f0,
            "fit_speed_mps": r.speed,
            "fit_slant_m": r.cpa_distance,
            "epsilon": r.epsilon,
            "epsilon_ridge": r.epsilon_ridge,
            "comb_contrast": r.notes.get("comb_score"),
            "template_score": detection.template_score,
            "n_harmonics": r.n_harmonics,
            "frames_used": r.notes.get("frames_used"),
            "stft_window_s": r.notes.get("stft_window_s"),
            "converged": int(r.converged),
            "blade_count": blade_count,
            "blade_source": blade_source,
            "rpm": (adsb.rpm_from_f0(r.f0, blade_count) if blade_count else None),
            "scint_exponent": (
                None if not np.isfinite(scint.get("exponent", np.nan)) else scint["exponent"]
            ),
            "freq_coherence": (
                None if not np.isfinite(r.frequency_coherence()) else r.frequency_coherence()
            ),
            "surf_temp_c": (surface or {}).get("temp_c"),
            "surf_rh_pct": (surface or {}).get("rh_pct"),
            "surf_pressure_kpa": (surface or {}).get("pressure_kpa"),
            "clock_offset_s": clock_offset_s,
            "notes": notes,
            "tracks": pack_tracks(r),
        }
        cols = ", ".join(row)
        cur = self.execute(
            f"INSERT OR REPLACE INTO passes ({cols}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()),
        )
        with self._lock:
            self.conn.commit()
        return int(cur.lastrowid)

    def add_unheard(
        self, station_id: str, geom: adsb.PassGeometry, best_score: float | None = None,
        reject_reason: str | None = None,
    ) -> None:
        if best_score is not None and not np.isfinite(best_score):
            best_score = None       # NaN round-trips through SQLite as a float, not as NULL
        self.execute(
            "INSERT OR REPLACE INTO unheard (station_id, t_heard_cpa, icao24, type_code,"
            " adsb_speed_mps, adsb_slant_m, adsb_agl_m, adsb_bearing_deg,"
            " best_template_score, reject_reason, engine_class)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                station_id,
                geom.t_heard_cpa,
                geom.icao24,
                geom.track.type_code if geom.track else None,
                geom.speed_mps,
                geom.slant_cpa_m,
                geom.altitude_agl_m,
                geom.bearing_deg,
                best_score,
                reject_reason,
                geom.track.engine_class if geom.track else None,
            ),
        )
        with self._lock:
            self.conn.commit()

    def record_join(
        self,
        station_id: str,
        result: associate.JoinResult,
        blade_counts: dict[str, int] | None = None,
        surface: dict | None = None,
        unheard_scores: dict | None = None,
        unheard_reasons: dict | None = None,
    ) -> dict:
        """Persist a whole join: matches, unexplained detections and unheard passes.

        `unheard_scores` maps `(icao24, round(t_heard_cpa, 1))` to the best detector score
        near that arrival, and `unheard_reasons` to why the best candidate there was rejected,
        so each negative is stored with the evidence behind it.
        """
        blade_counts = blade_counts or {}
        n_matched = n_unexplained = 0
        for m in result.matches:
            tc = m.geometry.track.type_code if m.geometry.track else None
            n = blade_counts.get(tc)
            self.add_pass(
                station_id,
                m.detection,
                match=m,
                blade_count=n,
                blade_source="inferred" if n else None,
                surface=surface,
                clock_offset_s=result.clock_offset_s,
            )
            n_matched += 1
        for det in result.unexplained:
            if det.fit is not None:
                self.add_pass(
                    station_id, det, surface=surface, clock_offset_s=result.clock_offset_s
                )
                n_unexplained += 1
        for g in result.unheard:
            key = (g.icao24, round(g.t_heard_cpa, 1))
            self.add_unheard(
                station_id, g, (unheard_scores or {}).get(key),
                (unheard_reasons or {}).get(key),
            )
        return {
            "matched": n_matched,
            "unexplained": n_unexplained,
            "unheard": len(result.unheard),
        }

    # -- product queries ----------------------------------------------------------------

    def detection_rate(self, station_id: str | None = None) -> float:
        """Heard / (heard + should-have-been-heard). The denominator for 'unexplained'.

        Jets are excluded from the denominator. This instrument is a harmonic-comb matched
        filter for propellers and a turbofan has no comb to find, so counting airliners as
        missed detections measures nothing. The deployed station reported 14.3 % while 10 of
        its 12 misses were airliners; the real propeller denominator was 2.

        Rows recorded before `engine_class` was populated have it NULL, and those are kept:
        unknown is treated as a possible propeller, which is the conservative direction.
        """
        where, args = ("WHERE station_id = ?", (station_id,)) if station_id else ("", ())
        heard = self.query_one(
            f"SELECT COUNT(*) FROM passes {where} {'AND' if where else 'WHERE'} icao24 IS NOT NULL",
            args,
        )[0]
        not_jet = "(engine_class IS NULL OR engine_class NOT IN ('J', 'R'))"
        missed = self.query_one(
            f"SELECT COUNT(*) FROM unheard {where} {'AND' if where else 'WHERE'} {not_jet}",
            args,
        )[0]
        total = heard + missed
        return heard / total if total else float("nan")

    def error_model(self, station_id: str | None = None) -> dict:
        """Measured error against ADS-B truth -- the fit's own bars run ~5x optimistic."""
        where = "WHERE icao24 IS NOT NULL AND match_ambiguous = 0"
        args: tuple = ()
        if station_id:
            where += " AND station_id = ?"
            args = (station_id,)
        rows = self.query_all(
            "SELECT fit_speed_mps, adsb_speed_mps, fit_slant_m, adsb_slant_m,"
            f" t_heard_cpa, adsb_t_heard_cpa, epsilon FROM passes {where}",
            args,
        )
        if not rows:
            return {"n": 0}
        dv = np.array([(r[0] - r[1]) / r[1] for r in rows])
        dd = np.array([(r[2] - r[3]) / r[3] for r in rows])
        dt = np.array([r[4] - r[5] for r in rows])
        eps = np.array([r[6] for r in rows])

        def st(x):
            return {
                "bias": float(np.mean(x)),
                "median_abs": float(np.median(np.abs(x))),
                "p90_abs": float(np.percentile(np.abs(x), 90)),
            }

        return {
            "n": len(rows),
            "speed_frac": st(dv),
            "slant_frac": st(dd),
            "time_s": st(dt),
            "epsilon_median": float(np.median(eps)),
            # the headline check: does the fit's own residual predict its real error?
            "slant_error_over_epsilon": float(np.median(np.abs(dd)) / max(np.median(eps), 1e-9)),
        }

    def rpm_history(self, registration: str) -> list[dict]:
        rows = self.query_all(
            "SELECT t_heard_cpa, rpm, fit_f0_hz, blade_count, epsilon FROM passes"
            " WHERE registration = ? AND rpm IS NOT NULL ORDER BY t_heard_cpa",
            (registration,),
        )
        return [dict(r) for r in rows]

    def f0_by_type(self, type_code: str) -> np.ndarray:
        """Fundamentals for one type -- the input to blade-count inference."""
        rows = self.query_all(
            "SELECT fit_f0_hz FROM passes WHERE type_code = ? AND converged = 1", (type_code,)
        )
        return np.array([r[0] for r in rows], dtype=float)

    def infer_blade_counts(self, min_passes: int = 8) -> dict[str, int]:
        """Blade count per type, which no standard table carries (adsb.infer_blade_count)."""
        types = [
            r[0]
            for r in self.query_all(
                "SELECT type_code FROM passes WHERE type_code IS NOT NULL"
                " GROUP BY type_code HAVING COUNT(*) >= ?",
                (min_passes,),
            )
        ]
        out = {}
        for t in types:
            n, _ = adsb.infer_blade_count(self.f0_by_type(t))
            if n is not None:
                out[t] = n
        return out

    def epsilon_trend(self, bucket_s: float = 86400.0) -> list[dict]:
        """`epsilon` over time -- the instrument's health metric, since it scales every product."""
        rows = self.query_all(
            "SELECT CAST(t_heard_cpa / ? AS INTEGER) AS bucket, COUNT(*) AS n,"
            " AVG(epsilon) AS mean_eps FROM passes GROUP BY bucket ORDER BY bucket",
            (bucket_s,),
        )
        return [
            {"bucket_start": r["bucket"] * bucket_s, "n": r["n"], "mean_epsilon": r["mean_eps"]}
            for r in rows
        ]

    def scintillation_by_hour(self) -> list[dict]:
        """Log-amplitude variance by hour of day: the free diurnal test for turbulence.

        SCINTILLATION.md 7 predicts a ~150x swing at matched geometry. This is the query that
        either shows it or kills the idea.
        """
        rows = self.query_all(
            "SELECT t_heard_cpa, fit_slant_m, tracks FROM passes WHERE tracks IS NOT NULL"
        )
        by_hour: dict[int, list[float]] = {}
        for r in rows:
            data = unpack_tracks(r["tracks"])
            ks = data["meta"]["harmonics"]
            if not ks:
                continue
            x = data[f"la{ks[0]}"]
            n = max(1, x.size // 8)
            var = float(np.var(x[n:-n])) - float(data["meta"]["noise_bias"].get(str(ks[0]), 0.0))
            hour = int((r["t_heard_cpa"] // 3600) % 24)
            by_hour.setdefault(hour, []).append(var)
        return [
            {"hour_utc": h, "n": len(v), "mean_log_amp_var": float(np.mean(v))}
            for h, v in sorted(by_hour.items())
        ]

    def detection_envelope(self, n_sectors: int = 16) -> list[dict]:
        """Measured hearing range by direction -- the instrument's own detection envelope.

        For each bearing sector, the farthest pass actually heard and the nearest pass that
        ADS-B says should have been audible but was not. The gap between them is where the
        site is deaf, and it is a *measurement* of shielding rather than a guess: a road
        embankment, a building or a hillside shows up as a notch.

        This only means anything because unheard passes are stored (NETWORK.md 10).
        """
        width = 360.0 / n_sectors
        out = []
        for i in range(n_sectors):
            lo = i * width
            hi = lo + width
            heard = self.query_one(
                "SELECT MAX(adsb_slant_m), COUNT(*) FROM passes"
                " WHERE adsb_bearing_deg >= ? AND adsb_bearing_deg < ?"
                " AND adsb_slant_m IS NOT NULL",
                (lo, hi),
            )
            missed = self.query_one(
                "SELECT MIN(adsb_slant_m), COUNT(*) FROM unheard"
                " WHERE adsb_bearing_deg >= ? AND adsb_bearing_deg < ?"
                " AND adsb_slant_m IS NOT NULL",
                (lo, hi),
            )
            out.append(
                {
                    "bearing_deg": lo + width / 2.0,
                    "sector_width_deg": width,
                    "max_heard_m": heard[0],
                    "n_heard": heard[1] or 0,
                    "min_missed_m": missed[0],
                    "n_missed": missed[1] or 0,
                }
            )
        return out

    def recent_positions(self, limit: int = 60) -> list[dict]:
        """Recent passes as bearing/range, for the radar display."""
        rows = self.query_all(
            "SELECT t_heard_cpa, adsb_bearing_deg, adsb_slant_m, adsb_agl_m, type_code,"
            " registration, fit_slant_m FROM passes"
            " WHERE adsb_bearing_deg IS NOT NULL ORDER BY t_heard_cpa DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def recent_unheard(self, limit: int = 12) -> list[dict]:
        """Recent passes ADS-B said should have been audible, with the score they reached.

        The score is the whole point of the row. A Bonanza at 1.4 km that scored 6 says the
        microphone never had the signal; one that scored 18 says the detector nearly had it,
        and the threshold -- not the siting -- is what to look at.
        """
        rows = self.query_all(
            "SELECT t_heard_cpa, icao24, type_code, adsb_speed_mps, adsb_slant_m,"
            " adsb_agl_m, adsb_bearing_deg, best_template_score, reject_reason,"
            " engine_class FROM unheard"
            " ORDER BY t_heard_cpa DESC LIMIT ?",
            (int(limit),),
        )
        return [dict(r) for r in rows]

    def atmospheric_samples(self, station_id: str | None = None) -> list:
        """Matched passes reduced to path samples for the atmospheric inversion.

        Only matched passes qualify: the inversion solves for the medium *given* the geometry,
        so without ADS-B truth the medium and the geometry would be fitted together and the
        problem is degenerate.
        """
        from . import atmos

        where = (
            "WHERE icao24 IS NOT NULL AND match_ambiguous = 0"
            " AND adsb_bearing_deg IS NOT NULL AND adsb_speed_mps > 0"
        )
        args: tuple = ()
        if station_id:
            where += " AND station_id = ?"
            args = (station_id,)
        out = []
        for r in self.query_all(
            "SELECT fit_speed_mps, adsb_speed_mps, adsb_bearing_deg, adsb_elevation_deg,"
            f" adsb_agl_m, epsilon, t_heard_cpa, icao24 FROM passes {where}",
            args,
        ):
            try:
                c_eff = atmos.c_eff_from_speed_bias(r[0], r[1])
            except ValueError:
                continue
            out.append(
                atmos.PathSample(
                    c_eff=c_eff,
                    sigma=atmos.sigma_c_eff(r[5], r[1]),
                    bearing_deg=r[2],
                    elevation_deg=r[3] if r[3] is not None else 45.0,
                    altitude_m=r[4] or 0.0,
                    t=r[6],
                    icao24=r[7],
                )
            )
        return out

    def sounding(self, station_id: str | None = None):
        """Bulk wind and temperature. None when there are too few passes."""
        from . import atmos

        return atmos.solve_bulk(self.atmospheric_samples(station_id))

    def profile(self, edges=(0.0, 1000.0, 2000.0, 4000.0, 7000.0), station_id=None):
        from . import atmos

        return atmos.solve_profile(self.atmospheric_samples(station_id), edges)

    # -- sharing ------------------------------------------------------------------------

    def export_reports(self, path: str | Path, station_id: str | None = None) -> int:
        """Newline-delimited JSON, versioned, mergeable without conflict.

        Deliberately not a database dump: stations must be able to merge each other's reports
        without agreeing on primary keys. Float arrays go base64 so the file stays one
        self-describing text stream (NETWORK.md 10).
        """
        where, args = (("WHERE station_id = ?", (station_id,)) if station_id else ("", ()))
        out = Path(path)
        n = 0
        with out.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "header", "format": REPORT_FORMAT}) + "\n")
            for r in self.query_all(f"SELECT * FROM stations {where}", args):
                fh.write(json.dumps({"type": "station", **dict(r)}) + "\n")
                n += 1
            for r in self.query_all(f"SELECT * FROM passes {where}", args):
                d = dict(r)
                d.pop("pass_id", None)
                if d.get("tracks") is not None:
                    d["tracks"] = base64.b64encode(d["tracks"]).decode()
                fh.write(json.dumps({"type": "pass", **d}) + "\n")
                n += 1
            for r in self.query_all(f"SELECT * FROM unheard {where}", args):
                d = dict(r)
                d.pop("unheard_id", None)
                fh.write(json.dumps({"type": "unheard", **d}) + "\n")
                n += 1
        return n

    def import_reports(self, path: str | Path) -> dict:
        """Merge another station's reports. Idempotent: re-importing changes nothing."""
        counts = {"station": 0, "pass": 0, "unheard": 0, "skipped": 0}
        with Path(path).open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                kind = rec.pop("type", None)
                if kind == "header":
                    if rec.get("format") != REPORT_FORMAT:
                        raise ValueError(f"unsupported report format: {rec.get('format')}")
                    continue
                if kind == "pass" and rec.get("tracks") is not None:
                    rec["tracks"] = base64.b64decode(rec["tracks"])
                if kind not in ("station", "pass", "unheard"):
                    counts["skipped"] += 1
                    continue
                table = {"station": "stations", "pass": "passes", "unheard": "unheard"}[kind]
                cols = ", ".join(rec)
                self.execute(
                    f"INSERT OR REPLACE INTO {table} ({cols})"
                    f" VALUES ({', '.join('?' * len(rec))})",
                    tuple(rec.values()),
                )
                counts[kind] += 1
        with self._lock:
            self.conn.commit()
        return counts

    def stations(self) -> list[dict]:
        return [dict(r) for r in self.query_all("SELECT * FROM stations")]

    def count(self, table: str = "passes") -> int:
        if table not in ("passes", "unheard", "stations"):
            raise ValueError(table)
        return int(self.query_one(f"SELECT COUNT(*) FROM {table}")[0])
