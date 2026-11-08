"""The run loop: the thing that makes it an instrument rather than a library.

Everything else in this package is a component with a test. This is what connects them and
keeps running for months:

    capture -> ring buffer -> rolling archive
                                   |
    adsb.lol poll -> tracks        +-> blind detect -> comb fit -> join -> store -> corpus

Three properties matter more than features, because this runs unattended:

* **Nothing unbounded.** The audio rings are fixed size, ADS-B tracks are pruned, and the
  corpus is capped. A long-running process that accumulates anything eventually dies.
* **Nothing fatal.** One bad analysis window, one dropped ADS-B poll or one unplugged
  microphone must not end the run. Failures are counted and reported, not raised.
* **Overlapping windows.** Analysis windows overlap so a pass straddling a boundary is not
  lost, which means detections must be de-duplicated against what is already stored.
"""

from __future__ import annotations

import concurrent.futures as _futures
import multiprocessing as _mp
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import adsb, associate, detect, geo, node, store


def _analysis_worker(payload: dict):
    """Detect, fit and join, in a separate process.

    This must not run in a thread beside capture. Detection and fitting are CPU-bound *Python*
    (the Viterbi ridge, the per-bin median, the template loop), so they hold the GIL and the
    capture thread only gets a share of the interpreter. In deployment that showed up as
    90 seconds of audio captured in 186 seconds of wall time -- and since ALSA's buffer is far
    shorter than an analysis, the shortfall is dropped samples, which silently corrupts the
    timeline rather than raising anything.

    A subprocess has its own GIL, so capture keeps running at real time regardless.
    """
    from . import adsb as _adsb
    from . import associate as _associate
    from . import detect as _detect
    from . import geo as _geo

    audio = payload["audio"]
    rx = _geo.Receiver(**payload["receiver"])
    diag: dict = {}
    dets = _detect.detect(
        audio,
        payload["rate"],
        band=tuple(payload["band"]),
        n_harmonics=payload["n_harmonics"],
        t_offset=payload["t_offset"],
        max_detections=payload["max_detections"],
        diagnostics=diag,
    )
    aircraft = [d for d in dets if d.is_aircraft]
    tracks = _adsb.tracks_from_readsb(payload["snapshots"])
    geoms = _adsb.audible_candidates(tracks, rx)
    result = _associate.join(aircraft, geoms)
    return {
        "result": result,
        "n_detections": len(dets),
        "n_aircraft": len(aircraft),
        "n_tracks": len(tracks),
        "n_candidates": len(geoms),
        # Carried back so the parent can score its own (window-clamped) unheard list.
        # Every candidate with its screening verdict, so a stored negative can say *why* a
        # scoring candidate was rejected rather than only that one existed.
        "candidates": [
            {"t0": float(d.t0_heard), "score": float(d.template_score),
             "is_aircraft": bool(d.is_aircraft), "reason": str(d.reason)[:120],
             "f0": float(d.f0), "speed": float(d.speed), "cpa_m": float(d.cpa_distance)}
            for d in dets
        ],
        "score_times": diag.get("times"),
        "score_best": diag.get("best_score"),
        "peak_score": diag.get("peak_score"),
    }


@dataclass
class PipelineConfig:
    station: geo.Receiver
    db_path: str
    station_id: str = "node"
    # Hardware provenance from the station config. `geo.Receiver` carries only geometry, so
    # without this the microphone, windscreen and calibration state never reach the database
    # and every exported report describes its station as having unknown hardware.
    station_meta: dict = field(default_factory=dict)
    alsa_device: str = "plughw:0,0"
    sample_rate: int = 48000
    archive_rate: int = 8000
    buffer_s: float = 600.0
    archive_s: float = 1800.0
    band: tuple[float, float] = (50.0, 300.0)
    n_harmonics: int = 4
    # Caps the worst case. Refinement costs 2.5-5.2 s per candidate, so a saturated window is
    # what pushes a slow board past its analysis period (DESIGN.md 6.1); the default is the
    # detector's own, and small boards want it lower.
    max_detections: int = 24
    adsb_poll_s: float = 15.0
    adsb_dist_nm: int = 40
    adsb_round_deg: float = 0.1
    adsb_keep_s: float = 3600.0
    analysis_period_s: float = 300.0
    analysis_window_s: float = 420.0
    corpus_dir: str | None = None
    corpus_limit_bytes: int = 750_000_000
    # A rejected candidate that scored above the threshold is the most informative thing the
    # station produces and the hardest to reproduce: the archive ring is 30 minutes, so by the
    # time anyone reads the row the audio is gone. Kept in its own directory under its own cap
    # so diagnostics can never crowd out the pass corpus.
    near_miss_fraction: float = 0.2
    dedup_window_s: float = 20.0
    status_path: str | None = None
    spectrogram_path: str | None = None
    spectrogram_window_s: float = 60.0
    spectrogram_band: tuple[float, float] = (20.0, 400.0)
    heartbeat_s: float = 30.0
    first_analysis_s: float = 120.0
    analyse_in_process: bool = False
    analysis_timeout_s: float = 600.0
    adsb_max_backoff: float = 16.0


class Pipeline:
    """Owns the threads and the state. `analyse_once` is synchronous so it can be tested."""

    def __init__(self, config: PipelineConfig, source: node.CaptureSource | None = None):
        self.cfg = config
        self.source = source or node.AlsaSource(
            device=config.alsa_device, nominal_rate=config.sample_rate
        )
        self.node = node.Node(
            source=self.source,
            station_id=config.station_id,
            buffer_s=config.buffer_s,
            archive_rate=config.archive_rate,
            archive_s=config.archive_s,
            start_time=time.time(),
        )
        self.db = store.Store(config.db_path)
        # Geometry comes from the Receiver, which is authoritative; a config that also
        # carries lat/lon must not be able to override it through this back door.
        _geometry = {"station_id", "lat", "lon", "elevation_msl_m", "geoid_offset_m"}
        meta = {
            k: v for k, v in (config.station_meta or {}).items()
            if k in store.Station.__dataclass_fields__ and k not in _geometry
        }
        self.db.add_station(
            store.Station.from_receiver(
                config.station, station_id=config.station_id, **meta
            )
        )
        self._snapshots: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._executor: _futures.ProcessPoolExecutor | None = None
        self.stats = {
            "started": time.time(),
            "blocks_captured": 0,
            "adsb_polls": 0,
            "adsb_failures": 0,
            "analyses": 0,
            "analysis_failures": 0,
            "passes_stored": 0,
            "unheard_stored": 0,
            "last_analysis": None,
            "last_peak_score": None,
            "analysis_duration_s": None,
            "analysis_overruns": 0,
            "coverage_gaps": 0,
            "last_error": None,
            "adsb_backoff": 1.0,
        }

    # -- threads -------------------------------------------------------------------------

    def start(self) -> "Pipeline":
        for target in (self._capture_loop, self._adsb_loop, self._analysis_loop,
                       self._status_loop):
            t = threading.Thread(target=self._guard(target), daemon=True, name=target.__name__)
            t.start()
            self._threads.append(t)
        return self

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout)
        if hasattr(self.source, "stop"):
            self.source.stop()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def _pool(self) -> "_futures.ProcessPoolExecutor":
        """A **spawn**-context pool, not the platform default.

        On Linux the default start method is fork, and forking a process whose other threads
        hold locks gives the child a copy of those locks in whatever state they were in. The
        child can then deadlock before it runs anything, and the parent sits on
        `future.result(timeout=600)` for ten minutes.

        That is exactly what happened in deployment: capture kept running, but the analysis
        thread wedged on the second analysis and the status thread stalled with it. `spawn`
        starts a clean interpreter and inherits no lock state.
        """
        if self._executor is None:
            self._executor = _futures.ProcessPoolExecutor(
                max_workers=1, mp_context=_mp.get_context("spawn")
            )
        return self._executor

    def _guard(self, fn):
        def wrapped():
            while not self._stop.is_set():
                try:
                    fn()
                    return
                except Exception:
                    # A thread that dies silently takes a product with it and nobody notices
                    # until the dashboard has been empty for a week.
                    self.stats["last_error"] = traceback.format_exc(limit=4)
                    self._stop.wait(5.0)

        return wrapped

    def _capture_loop(self) -> None:
        """Nothing but capture. Any other work here competes with the audio clock."""
        while not self._stop.is_set():
            self.node.pump(blocks=1, wall_clock=lambda frame: time.time())
            self.stats["blocks_captured"] += 1

    def _status_loop(self) -> None:
        while not self._stop.is_set():
            self._write_status()
            self._write_spectrogram()
            self._stop.wait(self.cfg.heartbeat_s)

    def _write_spectrogram(self) -> None:
        """Publish a small spectrogram of the recent past, for the dashboard to draw.

        The web UI is a separate process and cannot read the capture rings: ALSA capture is
        exclusive, which is the same reason analysis had to move out of process. So this
        crosses the boundary exactly as the status does -- a small file written here and read
        there -- rather than by having the UI open the microphone, which would fail, or by
        storing audio in the database, which would be wasteful.

        Deliberately not `fit.whitened_spectrogram`: that one's per-bin median over a 420 s
        window is the expensive part of an analysis, and this runs every heartbeat. A plain
        STFT plus a per-frequency median subtraction over a 60 s window costs milliseconds and
        is what a human needs to see a tone standing out of coloured noise.
        """
        if not self.cfg.spectrogram_path:
            return
        try:
            rate = int(self.node.archive_ring.sample_rate)
            audio = self.node.archived(seconds=self.cfg.spectrogram_window_s)
            nperseg = rate                       # 1 s frames -> 1 Hz resolution
            if audio.size < nperseg * 2:
                return
            hop = nperseg // 2
            n_frames = 1 + (audio.size - nperseg) // hop
            window = np.hanning(nperseg)
            lo, hi = self.cfg.spectrogram_band
            freqs = np.fft.rfftfreq(nperseg, 1.0 / rate)
            keep = (freqs >= lo) & (freqs <= hi)

            mag = np.empty((int(keep.sum()), n_frames), dtype=np.float32)
            for j in range(n_frames):
                seg = audio[j * hop : j * hop + nperseg] * window
                mag[:, j] = np.abs(np.fft.rfft(seg))[keep]

            db = 20.0 * np.log10(mag + 1e-12)
            # Per-frequency median subtraction: wind noise is strongly coloured and mains hum
            # is constant, so both sit in the median and a transient tone stands out of it.
            db -= np.median(db, axis=1, keepdims=True)

            lo_db, hi_db = -6.0, 24.0
            img = np.clip((db - lo_db) / (hi_db - lo_db), 0.0, 1.0)
            img = (img * 255.0).astype(np.uint8)

            hi_frame = self.node.archive_ring.available_frames[1]
            t_end = self.node.archive_time_of(hi_frame)
            path = Path(self.cfg.spectrogram_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp.npz")
            np.savez(
                tmp, img=img, freqs=freqs[keep].astype(np.float32),
                t_end=np.float64(t_end), window_s=np.float64(n_frames * hop / rate),
                db_lo=np.float32(lo_db), db_hi=np.float32(hi_db),
            )
            tmp.replace(path)     # atomic, so a reader never sees a half-written array
        except Exception:
            self.stats["last_error"] = traceback.format_exc(limit=3)

    @property
    def capture_lag_s(self) -> float:
        """Wall time elapsed minus audio actually captured.

        The metric that makes a starved or overrunning capture visible. It should hover near
        zero; a steadily growing value means samples are being dropped and every timestamp
        downstream is wrong.
        """
        audio_s = self.node.ring.frames_written / float(self.node.source.nominal_rate)
        return (time.time() - self.stats["started"]) - audio_s

    def _write_status(self) -> None:
        """Heartbeat plus state, as a file.

        A long-running capture process needs to prove it is alive in a way a container
        healthcheck can read, and an HTTP probe is wrong here -- the run loop serves no HTTP.
        A file whose mtime stops advancing is an unambiguous signal that capture has wedged,
        and the same file lets the UI show whether the instrument is actually listening rather
        than inferring it from an empty database.
        """
        if not self.cfg.status_path:
            return
        try:
            import json as _json

            path = Path(self.cfg.status_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(_json.dumps(self.status(), default=float, indent=2))
            tmp.replace(path)  # atomic, so a reader never sees a half-written file
        except Exception:
            self.stats["last_error"] = traceback.format_exc(limit=3)

    def _adsb_loop(self) -> None:
        """Poll the live feed, backing off when the service asks us to.

        adsb.lol is free and community-run, and it does return 429. Retrying at the same
        cadence through a rate limit is both rude and useless, so back off exponentially and
        recover once it clears.
        """
        backoff = 1.0
        while not self._stop.is_set():
            try:
                snap = adsb.fetch_adsb_lol(
                    self.cfg.station.lat,
                    self.cfg.station.lon,
                    dist_nm=self.cfg.adsb_dist_nm,
                    round_deg=self.cfg.adsb_round_deg,
                )
                with self._lock:
                    self._snapshots.append(snap)
                    cutoff = time.time() - self.cfg.adsb_keep_s
                    self._snapshots = [s for s in self._snapshots if s.get("now", 0) >= cutoff]
                self.stats["adsb_polls"] += 1
                backoff = 1.0
            except Exception as exc:
                self.stats["adsb_failures"] += 1
                if "429" in str(exc) or "Too Many Requests" in str(exc):
                    backoff = min(backoff * 2.0, self.cfg.adsb_max_backoff)
                    self.stats["adsb_backoff"] = backoff
            self._stop.wait(self.cfg.adsb_poll_s * backoff)

    def _analysis_loop(self) -> None:
        # Fill a short window first, not a full one. Waiting the whole analysis window before
        # the first run means nothing appears for seven minutes after any restart, which makes
        # the instrument look dead exactly when someone is watching it start.
        self._stop.wait(min(self.cfg.first_analysis_s, self.cfg.analysis_window_s))
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.analyse_once()
            except Exception:
                self.stats["analysis_failures"] += 1
                self.stats["last_error"] = traceback.format_exc(limit=4)
            self._stop.wait(self._note_analysis_cost(time.monotonic() - started))

    def _note_analysis_cost(self, elapsed: float) -> float:
        """Record what an analysis cost, and say how long to wait before the next one.

        `analysis_period_s` is a *start-to-start* period, so the wait is what is left of it.
        Waiting the whole period after each run instead makes the real cadence
        `duration + period`, which on a slow machine silently exceeds `analysis_window_s` --
        and audio no window covers is never analysed and never reported, because an unanalysed
        stretch produces no rows at all, not even `unheard` ones. On the deployment machine one
        window costs ~17 s against a 300 s period, so the difference never showed; measured on
        a Raspberry Pi 4 the same window is 136-304 s, where it loses passes outright.

        Coverage therefore holds while the cadence stays inside the window. Both thresholds are
        counted because they say different things: an overrun means the headroom is gone, a
        coverage gap means data was lost.
        """
        self.stats["analysis_duration_s"] = elapsed
        if elapsed > self.cfg.analysis_period_s:
            self.stats["analysis_overruns"] += 1
        if elapsed > self.cfg.analysis_window_s:
            self.stats["coverage_gaps"] += 1
        return max(0.0, self.cfg.analysis_period_s - elapsed)

    # -- the work ------------------------------------------------------------------------

    def current_tracks(self) -> list[adsb.Track]:
        with self._lock:
            snaps = list(self._snapshots)
        return adsb.tracks_from_readsb(snaps)

    def analyse_once(self, seconds: float | None = None) -> dict:
        """Detect, fit, join and store over the most recent window. Returns a summary."""
        window = seconds or self.cfg.analysis_window_s
        audio = self.node.archived(seconds=window)
        rate = int(self.node.archive_ring.sample_rate)
        if audio.size < rate * 60:
            return {"skipped": "not enough audio yet", "samples": int(audio.size)}

        lo, hi = self.node.archive_ring.available_frames
        first = max(lo, hi - audio.size)
        t_offset = self.node.archive_time_of(first)

        with self._lock:
            snapshots = list(self._snapshots)
        payload = {
            "audio": audio,
            "rate": rate,
            "band": list(self.cfg.band),
            "n_harmonics": self.cfg.n_harmonics,
            "max_detections": self.cfg.max_detections,
            "t_offset": t_offset,
            "snapshots": snapshots,
            "receiver": {
                "lat": self.cfg.station.lat,
                "lon": self.cfg.station.lon,
                "elevation_msl_m": self.cfg.station.elevation_msl_m,
                "geoid_offset_m": self.cfg.station.geoid_offset_m,
                "name": self.cfg.station.name,
            },
        }
        if self.cfg.analyse_in_process:
            out = _analysis_worker(payload)
        else:
            out = self._pool().submit(_analysis_worker, payload).result(
                timeout=self.cfg.analysis_timeout_s
            )
        result = out["result"]
        result.matches = [
            m for m in result.matches if not self._already_stored(m.detection.t0_heard)
        ]
        result.unexplained = [
            d for d in result.unexplained if not self._already_stored(d.t0_heard)
        ]
        aircraft = [m.detection for m in result.matches] + list(result.unexplained)

        # Only report passes whose expected arrival is inside this window as "unheard";
        # anything outside it simply has not been analysed yet.
        result.unheard = [
            g for g in result.unheard if t_offset <= g.t_heard_cpa <= t_offset + window
        ]

        # Score every negative where ADS-B said to look, so "not detected" carries a number.
        diag = {"times": out.get("score_times"), "best_score": out.get("score_best")}
        unheard_scores = {
            (g.icao24, round(g.t_heard_cpa, 1)): detect.best_score_near(diag, g.t_heard_cpa)
            for g in result.unheard
        }
        rejected = [c for c in (out.get("candidates") or []) if not c["is_aircraft"]]
        unheard_reasons = {}
        for g in result.unheard:
            near = [c for c in rejected
                    if abs(c["t0"] - g.t_heard_cpa) <= detect.SCORE_WINDOW_S]
            if near:
                unheard_reasons[(g.icao24, round(g.t_heard_cpa, 1))] = max(
                    near, key=lambda c: c["score"]
                )["reason"]

        counts = self.db.record_join(
            self.cfg.station_id,
            result,
            blade_counts=self.db.infer_blade_counts(),
            unheard_scores=unheard_scores,
            unheard_reasons=unheard_reasons,
        )
        for d in aircraft:
            self._save_corpus_clip(d, audio, rate, t_offset)

        # Keep the audio behind every "scored, screened out" negative. Without it the stored
        # reason is a sentence with nothing to check it against, and the deployed station
        # produced exactly that: a Cirrus at 4.25 km scoring 27.8, rejected, unreproducible.
        from types import SimpleNamespace

        for c in sorted(rejected, key=lambda c: -c["score"])[:3]:
            if c["score"] < detect.MIN_TEMPLATE_SCORE:
                break
            self._save_corpus_clip(
                SimpleNamespace(t0_heard=c["t0"], f0=c["f0"], speed=c["speed"],
                                cpa_distance=c["cpa_m"]),
                audio, rate, t_offset, subdir="near_miss",
                limit=int(self.cfg.corpus_limit_bytes * self.cfg.near_miss_fraction),
            )

        self.stats["analyses"] += 1
        self.stats["passes_stored"] += counts["matched"] + counts["unexplained"]
        self.stats["unheard_stored"] += counts["unheard"]
        self.stats["last_analysis"] = time.time()
        self.stats["last_peak_score"] = out.get("peak_score")
        self._write_status()
        return {
            "window_start": t_offset,
            "window_s": window,
            "peak_score": out.get("peak_score"),
            "detections": out["n_detections"],
            "aircraft": out["n_aircraft"],
            "tracks": out["n_tracks"],
            "audible_candidates": out["n_candidates"],
            **counts,
        }

    def _already_stored(self, t_heard: float) -> bool:
        row = self.db.conn.execute(
            "SELECT 1 FROM passes WHERE station_id = ? AND ABS(t_heard_cpa - ?) < ? LIMIT 1",
            (self.cfg.station_id, t_heard, self.cfg.dedup_window_s),
        ).fetchone()
        return row is not None

    def _save_corpus_clip(self, det, audio, rate, t_offset, subdir=None, limit=None) -> None:
        """Keep the audio for a detected pass, within the capped corpus.

        Per BUILD.md 2 the corpus exists to re-run the DSP against known input, not to hoard.
        Oldest-first eviction over the cap; a frozen golden set would be carved out of this by
        hand once there is real material worth freezing.
        """
        if not self.cfg.corpus_dir:
            return
        try:
            import soundfile as sf

            d = Path(self.cfg.corpus_dir) / subdir if subdir else Path(self.cfg.corpus_dir)
            d.mkdir(parents=True, exist_ok=True)
            limit = self.cfg.corpus_limit_bytes if limit is None else limit
            half = float(np.clip(2.0 * det.cpa_distance / max(det.speed, 1.0), 20.0, 90.0))
            i0 = int(max(0, (det.t0_heard - t_offset - half) * rate))
            i1 = int(min(audio.size, (det.t0_heard - t_offset + half) * rate))
            if i1 - i0 < rate * 5:
                return
            stem = "nearmiss" if subdir else "pass"
            path = d / f"{stem}_{int(det.t0_heard)}_{int(det.f0)}hz.flac"
            if not path.exists():
                sf.write(path, audio[i0:i1], rate, format="FLAC")
            files = sorted(d.glob("*.flac"), key=lambda p: p.stat().st_mtime)
            total = sum(p.stat().st_size for p in files)
            while total > limit and files:
                victim = files.pop(0)
                total -= victim.stat().st_size
                victim.unlink(missing_ok=True)
        except Exception:
            self.stats["last_error"] = traceback.format_exc(limit=3)

    # -- reporting -----------------------------------------------------------------------

    def overhead(self, max_slant_m: float = 25000.0) -> list[dict]:
        """What ADS-B says is overhead right now, as bearing and range from the station.

        Published in the status file so the UI can draw it without needing its own feed or a
        second API key's worth of traffic to a free community service.
        """
        out = []
        for tr in self.current_tracks():
            g = adsb.pass_geometry(tr, self.cfg.station)
            if g is None or g.slant_cpa_m > max_slant_m:
                continue
            out.append(
                {
                    "icao24": tr.icao24,
                    "registration": tr.registration,
                    "type_code": tr.type_code,
                    "bearing_deg": g.bearing_deg,
                    "slant_m": g.slant_cpa_m,
                    "agl_m": g.altitude_agl_m,
                    "speed_mps": g.speed_mps,
                    "t_heard_cpa": g.t_heard_cpa,
                }
            )
        return sorted(out, key=lambda a: a["slant_m"])[:40]

    def status(self) -> dict:
        with self._lock:
            n_snaps = len(self._snapshots)
        try:
            overhead = self.overhead()
        except Exception:
            overhead = []
        return {
            "overhead": overhead,
            "capture_lag_s": self.capture_lag_s,
            "status_written": time.time(),
            "uptime_s": time.time() - self.stats["started"],
            "node": self.node.status(),
            "adsb_snapshots": n_snaps,
            "adsb_tracks": len(self.current_tracks()),
            "passes_in_db": self.db.count("passes"),
            "unheard_in_db": self.db.count("unheard"),
            "analysis_period_s": self.cfg.analysis_period_s,
            "analysis_window_s": self.cfg.analysis_window_s,
            **{k: v for k, v in self.stats.items() if k != "started"},
        }


def run(config: PipelineConfig, forever: bool = True) -> Pipeline:
    p = Pipeline(config).start()
    if forever:
        try:
            while True:
                time.sleep(60.0)
        except KeyboardInterrupt:
            p.stop()
    return p
