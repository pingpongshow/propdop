"""Command line entry point -- what the container runs.

Under Docker everything lives under the bind-mounted `/data`, matching the
convention the other applications on that host use. Paths are overridable so the same image
runs on a laptop.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

from . import __version__, adsb, associate, detect, fit, geo, store

# Relative by default so a fresh clone works without configuration; the container and the
# systemd unit both set PROPDOP_DATA explicitly, so this default is only for local use.
DATA_ROOT = Path(os.environ.get("PROPDOP_DATA", "./data"))


def data_path(*parts: str) -> Path:
    p = DATA_ROOT.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_audio(path: str) -> tuple[np.ndarray, int]:
    import soundfile as sf

    x, rate = sf.read(path, always_2d=True)
    return x.mean(axis=1), int(rate)


def _station_from_config(path: Path | None) -> geo.Receiver | None:
    """Station config. The geoid offset is required, not defaulted: ADS-B altitude is height
    above the ellipsoid while site elevation is above mean sea level, and conflating them is a
    silent tens-of-metres slant error."""
    if path is None or not path.exists():
        return None
    cfg = json.loads(path.read_text())
    missing = {"lat", "lon", "elevation_msl_m", "geoid_offset_m"} - set(cfg)
    if missing:
        raise SystemExit(f"station config missing required keys: {sorted(missing)}")
    return geo.Receiver(
        lat=cfg["lat"],
        lon=cfg["lon"],
        elevation_msl_m=cfg["elevation_msl_m"],
        geoid_offset_m=cfg["geoid_offset_m"],
        name=cfg.get("station_id", "node"),
    )


def cmd_detect(args) -> int:
    samples, rate = _load_audio(args.audio)
    dets = detect.detect(
        samples, rate, band=(args.f_min, args.f_max), n_harmonics=args.harmonics
    )
    rows = []
    for d in dets:
        rows.append(
            {
                "t0_heard": round(d.t0_heard, 2),
                "f0_hz": round(d.f0, 2),
                "speed_mps": round(d.speed, 1),
                "slant_m": round(d.cpa_distance, 0),
                "template_score": round(d.template_score, 1),
                "epsilon_pct": round(d.fit.epsilon * 100, 3) if d.fit else None,
                "aircraft": d.is_aircraft,
                "reason": d.reason,
            }
        )
    print(json.dumps({"detections": rows, "n_aircraft": sum(r["aircraft"] for r in rows)}, indent=2))
    return 0


def cmd_fit(args) -> int:
    samples, rate = _load_audio(args.audio)
    r = fit.fit_comb(samples, rate, band=(args.f_min, args.f_max), n_harmonics=args.harmonics)
    ok, why = fit.plausible_aircraft(r)
    out = {
        "f0_hz": round(r.f0, 3),
        "speed_mps": round(r.speed, 2),
        "slant_m": round(r.cpa_distance, 1),
        "t0_heard_s": round(r.t0_heard, 2),
        "epsilon_pct": round(r.epsilon * 100, 4),
        "comb_contrast": round(r.notes.get("comb_score", float("nan")), 3),
        "harmonics": list(r.harmonics),
        "aircraft": ok,
        "reason": why,
        "scintillation": r.scintillation_scaling(),
        "frequency_coherence": r.frequency_coherence(),
    }
    if args.blades:
        out["rpm"] = round(r.rpm(args.blades), 1)
        out["rpm_uncertainty"] = round(r.rpm_uncertainty(args.blades), 1)
    print(json.dumps(out, indent=2, default=float))
    return 0


def cmd_ingest(args) -> int:
    """Analyse a recording and persist the passes, joining to ADS-B when tracks are given."""
    samples, rate = _load_audio(args.audio)
    rx = _station_from_config(Path(args.station) if args.station else None)
    db = store.Store(args.db or data_path("db", "passes.sqlite"))
    station_id = rx.name if rx else "unknown"
    if rx is not None:
        db.add_station(store.Station.from_receiver(rx))

    dets = detect.detect(samples, rate, band=(args.f_min, args.f_max), t_offset=args.t_offset)
    geoms: list = []
    if args.adsb and rx is not None:
        geoms = adsb.audible_candidates(adsb.load_readsb_dir(args.adsb), rx)

    result = associate.join(dets, geoms)
    counts = db.record_join(station_id, result, blade_counts=db.infer_blade_counts())
    print(
        json.dumps(
            {
                "station": station_id,
                **counts,
                "clock_offset_s": round(result.clock_offset_s, 2),
                "detection_rate": result.detection_rate,
                "total_passes": db.count("passes"),
            },
            indent=2,
            default=float,
        )
    )
    return 0


def cmd_status(args) -> int:
    db = store.Store(args.db or data_path("db", "passes.sqlite"))
    out = {
        "version": __version__,
        "data_root": str(DATA_ROOT),
        "passes": db.count("passes"),
        "unheard": db.count("unheard"),
        "stations": [s["station_id"] for s in db.stations()],
        "detection_rate": db.detection_rate(),
        "error_model": db.error_model(),
        "blade_counts": db.infer_blade_counts(),
        "epsilon_trend": db.epsilon_trend()[-7:],
    }
    print(json.dumps(out, indent=2, default=float))
    return 0


def cmd_run(args) -> int:
    """The long-running pipeline: capture, detect, join, store. What the container runs."""
    from . import pipeline

    rx = _station_from_config(Path(args.station) if args.station else data_path("config", "station.json"))
    if rx is None:
        raise SystemExit(
            "no station config. Write config/station.json with lat, lon, elevation_msl_m and"
            " geoid_offset_m -- the geoid offset is required, not optional."
        )
    cfg = pipeline.PipelineConfig(
        station=rx,
        station_id=rx.name,
        db_path=args.db or str(data_path("db", "passes.sqlite")),
        alsa_device=args.device,
        sample_rate=args.rate,
        band=(args.f_min, args.f_max),
        n_harmonics=args.harmonics,
        analysis_period_s=args.period,
        analysis_window_s=args.window,
        max_detections=args.max_detections,
        corpus_dir=args.corpus or str(data_path("corpus", ".keep").parent),
        status_path=str(data_path("db", "pipeline_status.json")),
        spectrogram_path=str(data_path("db", "spectrogram.npz")),
    )
    print(
        json.dumps(
            {
                "starting": True,
                "station": rx.name,
                "device": cfg.alsa_device,
                "rate": cfg.sample_rate,
                "db": cfg.db_path,
                "analysis_every_s": cfg.analysis_period_s,
                "max_detections": cfg.max_detections,
                "adsb": "adsb.lol live (query centre rounded)",
            },
            indent=2,
        ),
        flush=True,
    )
    pipeline.run(cfg)
    return 0


def cmd_sounding(args) -> int:
    db = store.Store(args.db or data_path("db", "passes.sqlite"))
    s = db.sounding()
    edges = [float(x) for x in args.edges.split(",")]
    out = {
        "n_samples": len(db.atmospheric_samples()),
        "bulk": s.as_dict() if s else None,
        "profile": db.profile(edges).as_dict(),
    }
    if s is None:
        out["note"] = (
            "too few matched passes; the inversion needs ADS-B geometry, so blind passes"
            " do not qualify"
        )
    print(json.dumps(out, indent=2, default=float))
    return 0


def cmd_serve(args) -> int:
    from . import web

    web.serve(args.db or data_path("db", "passes.sqlite"), host=args.host, port=args.port)
    return 0


def cmd_export(args) -> int:
    db = store.Store(args.db or data_path("db", "passes.sqlite"))
    n = db.export_reports(args.out, station_id=args.station_id)
    print(json.dumps({"records": n, "path": str(args.out)}, indent=2))
    return 0


def cmd_import(args) -> int:
    db = store.Store(args.db or data_path("db", "passes.sqlite"))
    counts = db.import_reports(args.path)
    print(json.dumps(counts, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="propdop", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--db", default=None, help="pass database path")
    sub = p.add_subparsers(dest="command", required=True)

    def band(sp):
        sp.add_argument("--f-min", type=float, default=50.0)
        sp.add_argument("--f-max", type=float, default=300.0)
        sp.add_argument("--harmonics", type=int, default=4)

    sp = sub.add_parser("detect", help="find flyovers blind")
    sp.add_argument("audio")
    band(sp)
    sp.set_defaults(func=cmd_detect)

    sp = sub.add_parser("fit", help="fit one clip containing a single pass")
    sp.add_argument("audio")
    sp.add_argument("--blades", type=int, default=None, help="blade count, for RPM")
    band(sp)
    sp.set_defaults(func=cmd_fit)

    sp = sub.add_parser("ingest", help="detect, join to ADS-B and persist")
    sp.add_argument("audio")
    sp.add_argument("--station", help="station config JSON")
    sp.add_argument("--adsb", help="directory of readsb aircraft.json snapshots")
    sp.add_argument("--t-offset", type=float, default=0.0, help="UTC time of the first sample")
    band(sp)
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("status", help="database summary and instrument health")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("run", help="the continuous pipeline (capture, detect, join, store)")
    sp.add_argument("--station", help="station config JSON (default: <data>/config/station.json)")
    sp.add_argument("--device", default="plughw:0,0", help="ALSA capture device")
    sp.add_argument("--rate", type=int, default=48000)
    sp.add_argument("--period", type=float, default=300.0, help="seconds between analyses")
    sp.add_argument("--window", type=float, default=420.0, help="analysis window seconds")
    sp.add_argument(
        "--max-detections", type=int, default=24,
        help="candidates refined per window. Refinement costs ~14 s each on a Pi 4, so this"
             " is the knob that bounds a slow board's worst case (DESIGN.md 6.1).",
    )
    sp.add_argument("--corpus", default=None)
    band(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("sounding", help="wind and temperature aloft")
    sp.add_argument("--edges", default="0,1000,2000,4000,7000", help="layer edges in metres")
    sp.set_defaults(func=cmd_sounding)

    sp = sub.add_parser("serve", help="run the web UI")
    sp.add_argument("--host", default="0.0.0.0")
    sp.add_argument("--port", type=int, default=8475)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("export", help="write shareable reports")
    sp.add_argument("out")
    sp.add_argument("--station-id", default=None)
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("import", help="merge another station's reports")
    sp.add_argument("path")
    sp.set_defaults(func=cmd_import)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
