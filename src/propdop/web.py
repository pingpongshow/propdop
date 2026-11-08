"""Web UI: instrument health and product confidence, read off the pass database.

Per BUILD.md 3 there are no gates, so the UI's job is to make each product state its own
confidence and contributing-pass count rather than emit a bare number. Two things are therefore
shown prominently:

* **`epsilon` over time**, because it scales every product (required pass counts go as its
  square) and a rising trend means the windscreen, siting or microphone needs attention.
* **what is not yet measurable**, so "insufficient history" appears where a number would be
  dishonest -- prop health needs months of repeat passes before it means anything.

Plots are server-rendered inline SVG. No JavaScript, no CDN, no matplotlib: the page has to be
readable on a homelab with no outbound internet, and inline SVG is testable by parsing the
response.

A consequence of the retention policy worth noting: audio is kept only as a capped 1 GB corpus
(BUILD.md 2), so a pass detail view plots the *stored per-harmonic tracks*, not a spectrogram.
For most passes the clip is long gone, and the tracks are the permanent record.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import math
from pathlib import Path

import numpy as np

from . import __version__, detect, livespec, store

try:  # keep the core import-light; the web extra is optional
    from fastapi import FastAPI, HTTPException, Response
    from fastapi.responses import HTMLResponse, StreamingResponse
except ModuleNotFoundError as exc:  # pragma: no cover
    raise ModuleNotFoundError(
        "the web UI needs the 'web' extra: pip install '.[web]'"
    ) from exc


LIVE_SCRIPT = """<script>
// Live regions refresh on their own; the rest of the page only changes when a pass is
// analysed, so a whole-page reload would be wasteful and would scroll the reader away.
// Vanilla, no framework, no CDN -- the page has to work on a network with no outbound route.
(function () {
  var FIELDS = {radar: "radar", overhead: "overhead", capture: "capture",
                candidates: "candidates", spectrogram: "spectrogram",
                "radar-note": "radar_note"};
  // Delegated, because the spectrogram fragment is replaced on every poll.
  document.addEventListener("mousemove", function (e) {
    var hit = e.target;
    if (!hit || hit.id !== "spec-hit") return;
    var r = hit.getBoundingClientRect();
    var flo = parseFloat(hit.getAttribute("data-flo")),
        fhi = parseFloat(hit.getAttribute("data-fhi")),
        win = parseFloat(hit.getAttribute("data-win"));
    var hz = flo + (fhi - flo) * (1 - (e.clientY - r.top) / r.height);
    var ago = win * (1 - (e.clientX - r.left) / r.width);
    var out = document.getElementById("spec-cursor");
    if (out) out.textContent = "\u2014 cursor: " + hz.toFixed(0) + " Hz, " +
      ago.toFixed(0) + " s ago \u2192 " + (hz * 30).toFixed(0) + " RPM on 2 blades, " +
      (hz * 20).toFixed(0) + " on 3";
  });
  var dot = document.getElementById("state-dot");
  var label = document.getElementById("state-label");
  function tick() {
    fetch("/api/live", {cache: "no-store"}).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    }).then(function (d) {
      for (var id in FIELDS) {
        var el = document.getElementById(id);
        if (el && d[FIELDS[id]] != null) el.innerHTML = d[FIELDS[id]];
      }
      var n = document.getElementById("n-overhead");
      if (n) n.textContent = d.n_overhead;
      if (dot) dot.style.background = d.listening ? "var(--ok)" : "var(--bad)";
      if (label) label.textContent = d.state;
    }).catch(function () {
      if (dot) dot.style.background = "var(--warn)";
      if (label) label.textContent = "unreachable";
    });
  }
  setInterval(tick, 5000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) tick();   // catch up at once when the tab comes back
  });

  // -- the live spectrogram ---------------------------------------------------------------
  // Columns arrive over server-sent events and are drawn into an ImageData kept at the ring's
  // native size, which CSS then stretches to the panel. Drawing at native size means the
  // browser scales once, on the GPU, instead of this code resampling on every frame.
  (function () {
    var cv = document.getElementById("spec-canvas");
    if (!cv || typeof EventSource === "undefined") return;
    var ctx = cv.getContext("2d", {alpha: false});
    var img = null, pal = null, bins = 0, feed = document.getElementById("spec-feed");
    var es = new EventSource("/api/spectrogram/stream");

    es.addEventListener("meta", function (ev) {
      var m = JSON.parse(ev.data);
      bins = m.n_bins;
      cv.width = m.capacity;
      cv.height = bins;
      img = ctx.createImageData(m.capacity, bins);
      for (var i = 3; i < img.data.length; i += 4) img.data[i] = 255;   // opaque
      pal = m.palette;
    });

    es.addEventListener("cols", function (ev) {
      if (!img) return;
      var d = JSON.parse(ev.data);
      var raw = atob(d.b64), w = img.width, k = Math.min(d.n, w), px = img.data;
      // Scroll left by k columns, one row at a time. copyWithin on the row's slice is a
      // memmove, so this is a few hundred microseconds for a panel this size.
      if (k < w) {
        for (var y = 0; y < bins; y++) {
          var row = y * w * 4;
          px.copyWithin(row, row + k * 4, row + w * 4);
        }
      }
      // The newest k columns land at the right edge. Row 0 of an ImageData is the top of the
      // canvas, and high frequency belongs at the top, so the bin index runs backwards.
      var first = d.n - k;
      for (var c = 0; c < k; c++) {
        var src = (first + c) * bins, x = w - k + c;
        for (var b = 0; b < bins; b++) {
          var v = raw.charCodeAt(src + b) * 3, o = ((bins - 1 - b) * w + x) * 4;
          px[o] = pal[v]; px[o + 1] = pal[v + 1]; px[o + 2] = pal[v + 2];
        }
      }
      ctx.putImageData(img, 0, 0);
      if (feed) feed.textContent = "";
    });

    es.onerror = function () {
      // EventSource reconnects on its own; say so rather than leaving a frozen panel
      // looking like a working one.
      if (feed) feed.textContent = "\u2014 feed interrupted, reconnecting\u2026";
    };
  })();
})();
</script>
"""

CSS = """
:root { --bg:#fbfbfc; --fg:#15171a; --mut:#6b7280; --line:#e3e5e9; --acc:#2563eb;
        --warn:#b45309; --bad:#b91c1c; --ok:#047857; --card:#fff; --shadow:0 1px 2px rgba(0,0,0,.05);}
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f1115; --fg:#e7e9ee; --mut:#99a1b3; --line:#262a33; --acc:#60a5fa;
          --warn:#f0b357; --bad:#ff8d94; --ok:#4ade80; --card:#161920; --shadow:none;}
}
* { box-sizing:border-box }

/* The live spectrogram: a canvas behind the axes, not inside them. See `spectrogram_svg`
   for why this is an overlay rather than an SVG foreignObject. The SVG is what gives the
   wrapper its height, so the canvas's percentages resolve against exactly the box the axes
   are drawn around -- at any width, which is what makes the panel survive a phone. */
.specwrap { position:relative; width:100%; overflow:hidden }
.specwrap > svg { position:relative; z-index:1; display:block; width:100% }
.specwrap > canvas { position:absolute; z-index:0; display:block;
                     max-width:none; max-height:none }
body { margin:0; padding:0; background:var(--bg); color:var(--fg);
       font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,sans-serif;
       -webkit-font-smoothing:antialiased }
header { border-bottom:1px solid var(--line); background:var(--card) }
header .inner, main { max-width:1080px; margin:0 auto; padding:0 20px }
header .inner { display:flex; align-items:baseline; gap:16px; padding-top:16px; padding-bottom:16px;
                flex-wrap:wrap }
h1 { font-size:19px; margin:0; letter-spacing:-.01em }
h1 .dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:8px;
          vertical-align:middle }
nav { margin-left:auto; display:flex; gap:14px; font-size:13px }
nav a { color:var(--mut); text-decoration:none } nav a:hover { color:var(--acc) }
main { padding-top:24px; padding-bottom:56px }
h2 { font-size:12px; text-transform:uppercase; letter-spacing:.07em; color:var(--mut);
     margin:32px 0 12px; font-weight:600 }
h2:first-of-type { margin-top:8px }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(132px,1fr)); gap:10px }
.cols { display:grid; grid-template-columns:minmax(300px,440px) 1fr; gap:24px; align-items:start }
@media (max-width:820px){ .cols{ grid-template-columns:1fr } }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:13px 14px;
        box-shadow:var(--shadow) }
.card .k { color:var(--mut); font-size:11px; text-transform:uppercase; letter-spacing:.05em }
.card .v { font-size:21px; font-variant-numeric:tabular-nums; margin-top:3px; letter-spacing:-.01em }
table { width:100%; border-collapse:collapse; font-size:13px; font-variant-numeric:tabular-nums }
th,td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line) }
tr:last-child td { border-bottom:none }
th { color:var(--mut); font-weight:600; font-size:11px; text-transform:uppercase;
     letter-spacing:.05em }
tbody tr:hover { background:color-mix(in srgb, var(--acc) 5%, transparent) }
a { color:var(--acc) }
.ok{color:var(--ok)} .warn{color:var(--warn)} .bad{color:var(--bad)} .mut{color:var(--mut)}
.pill { display:inline-block; padding:1px 8px; border-radius:999px; font-size:11px;
        font-weight:600; border:1px solid currentColor }
figure { margin:0 0 10px } figcaption { color:var(--mut); font-size:11px; margin-top:3px }
.note { color:var(--mut); font-size:12.5px; margin:8px 0 0; max-width:62ch }
code { background:var(--bg); border:1px solid var(--line); padding:1px 5px; border-radius:4px;
       font-size:12px }
.legend { display:flex; gap:14px; flex-wrap:wrap; font-size:11.5px; color:var(--mut);
          margin-top:6px }
.legend i { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:5px;
            vertical-align:baseline }
"""

def jsonable(obj):
    """Replace non-finite floats with None so API responses are valid JSON.

    `detection_rate` and friends legitimately return NaN for "not computable yet" -- on an
    empty database there is nothing to divide. JSON has no NaN, so the encoder raises and a
    fresh install gets a 500 from its own status endpoint. `null` is the honest encoding of an
    undefined quantity, so convert rather than hiding the condition behind a zero.
    """
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (float, np.floating)):
        f = float(obj)
        return f if math.isfinite(f) else None
    if isinstance(obj, (np.integer,)):
        return int(obj)
    return obj


def _esc(x) -> str:
    return html.escape("" if x is None else str(x))


def _fmt(x, digits=2, dash="—") -> str:
    if x is None:
        return dash
    try:
        f = float(x)
    except (TypeError, ValueError):
        return _esc(x)
    return dash if not math.isfinite(f) else f"{f:,.{digits}f}"


_SPECTROGRAM_RAMP = (
    # One hue, monotonic in lightness: a spectrogram encodes magnitude, which is a sequential
    # job, so a rainbow (jet, viridis) is the wrong tool -- it invents category boundaries
    # where the data has none and is not monotonic in perceived lightness. These are control
    # points of a single blue ramp on its own dark plot surface, interpolated to 256 steps.
    (0.00, (0x0b, 0x10, 0x1a)),
    (0.35, (0x15, 0x3e, 0x75)),
    (0.65, (0x25, 0x63, 0xeb)),
    (0.85, (0x7d, 0xaa, 0xf7)),
    (1.00, (0xe8, 0xf1, 0xfe)),
)


def _spectrogram_palette() -> bytes:
    """256 RGB triples for an indexed-colour PNG."""
    stops = _SPECTROGRAM_RAMP
    out = bytearray()
    for i in range(256):
        t = i / 255.0
        for k in range(len(stops) - 1):
            t0, c0 = stops[k]
            t1, c1 = stops[k + 1]
            if t <= t1 or k == len(stops) - 2:
                f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
                f = min(max(f, 0.0), 1.0)
                out += bytes(int(round(a + (b - a) * f)) for a, b in zip(c0, c1))
                break
    return bytes(out)


def _png_indexed(idx: "np.ndarray", palette: bytes) -> bytes:
    """Encode a 2-D uint8 array as an indexed-colour PNG, using only the standard library.

    No Pillow, no matplotlib: the dashboard's whole premise is that it works on a network with
    no outbound route and pulls nothing at runtime, and adding an imaging dependency to render
    one heatmap would be out of proportion. Colour type 3 at 8 bits is one byte per pixel plus
    a 768-byte palette, which compresses far smaller than the equivalent SVG rect grid -- a
    380x119 spectrogram would otherwise be 45,000 <rect> elements.
    """
    import struct
    import zlib

    arr = np.ascontiguousarray(idx, dtype=np.uint8)
    h, w = arr.shape

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 3, 0, 0, 0)
    # Each scanline is prefixed with its filter type; 0 (None) is right for palette indices,
    # where byte deltas carry no meaning.
    raw = b"".join(b"\x00" + arr[r].tobytes() for r in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"PLTE", palette)
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def _score(x) -> float:
    """NULL (a row stored before scoring existed) reads as 'not analysed', not as zero."""
    return float("nan") if x is None else float(x)


def _clock(ts, fmt: str = "%m-%d %H:%M:%S") -> str:
    """Wall-clock time, which is how the operator reports hearing an aircraft.

    The zone is **always labelled**, because this is the one number on the dashboard that an
    operator checks against their own memory and it is read off whatever zone the host happens
    to be in. A Raspberry Pi ships as UTC, so a station whose operator is some hours off it
    shows one time for a pass they heard at another, and the two records cannot be reconciled
    by eye. Nothing here
    converts: inventing a zone the host has not been told about would be worse. The fix is
    `timedatectl set-timezone`, and until then the label says which clock this is.
    """
    try:
        t = _dt.datetime.fromtimestamp(float(ts)).astimezone()
    except (TypeError, ValueError, OSError):
        return "—"
    return f"{t.strftime(fmt)} {t.strftime('%Z') or t.strftime('%z')}"


def line_svg(
    xs, ys, width=460, height=130, label="", xlabel="", ylabel="", logy=False
) -> str:
    """Minimal inline line plot. Returns a placeholder rather than failing on empty input."""
    xs = [float(v) for v in xs]
    ys = [float(v) for v in ys]
    good = [(x, y) for x, y in zip(xs, ys) if math.isfinite(x) and math.isfinite(y)]
    if len(good) < 2:
        return (
            f'<figure><svg width="{width}" height="{height}" role="img">'
            f'<text x="8" y="{height // 2}" fill="var(--mut)" font-size="12">'
            f"not enough data yet</text></svg>"
            f"<figcaption>{_esc(label)}</figcaption></figure>"
        )
    xs, ys = zip(*good)
    if logy:
        ys = [math.log10(max(y, 1e-12)) for y in ys]
    pad_l, pad_b, pad_t, pad_r = 44, 22, 8, 8
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 == x0:
        x1 = x0 + 1
    if y1 == y0:
        y0, y1 = y0 - 1, y1 + 1
    pw, ph = width - pad_l - pad_r, height - pad_b - pad_t

    def px(x):
        return pad_l + (x - x0) / (x1 - x0) * pw

    def py(y):
        return pad_t + ph - (y - y0) / (y1 - y0) * ph

    pts = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in zip(xs, ys))
    fmt = (lambda v: f"1e{v:.0f}") if logy else (lambda v: f"{v:.3g}")
    ticks = "".join(
        f'<text x="{pad_l - 6}" y="{py(v) + 4:.1f}" text-anchor="end" fill="var(--mut)"'
        f' font-size="10">{fmt(v)}</text>'
        f'<line x1="{pad_l}" y1="{py(v):.1f}" x2="{width - pad_r}" y2="{py(v):.1f}"'
        f' stroke="var(--line)" stroke-width="1"/>'
        for v in (y0, (y0 + y1) / 2, y1)
    )
    dots = "".join(
        f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="2" fill="var(--acc)"/>'
        for x, y in zip(xs, ys)
    ) if len(xs) <= 60 else ""
    return (
        f'<figure><svg width="{width}" height="{height}" role="img"'
        f' aria-label="{_esc(label)}">{ticks}'
        f'<polyline fill="none" stroke="var(--acc)" stroke-width="1.6" points="{pts}"/>'
        f"{dots}</svg>"
        f"<figcaption>{_esc(label)}"
        + (f" &middot; x: {_esc(xlabel)}" if xlabel else "")
        + (f" &middot; y: {_esc(ylabel)}" if ylabel else "")
        + "</figcaption></figure>"
    )




def radar_svg(
    envelope: list[dict] | None = None,
    overhead: list[dict] | None = None,
    recent: list[dict] | None = None,
    max_km: float = 12.0,
    size: int = 440,
) -> str:
    """Polar display: range rings, live aircraft, and the measured detection envelope.

    A polar plot rather than a street map, deliberately. What governs whether a pass is usable
    is its **range and bearing from this microphone**, not where the roads are -- and the one
    thing worth showing that no map tile can is the *measured* hearing range by direction.

    The shaded lobe is exactly that: per bearing sector, the farthest pass actually heard.
    Hollow marks are passes ADS-B says should have been audible and were not, so a notch in
    the lobe with hollow marks beyond it is a direction this site is deaf in -- a building, an
    embankment, a hillside. It is a measurement, and it only exists because unheard passes are
    stored.
    """
    envelope = envelope or []
    overhead = overhead or []
    recent = recent or []
    cx = cy = size / 2
    r_max = size / 2 - 34

    def pt(bearing_deg, range_m):
        """Compass bearing, north up, clockwise."""
        r = min(float(range_m) / 1000.0, max_km) / max_km * r_max
        a = np.radians(float(bearing_deg) - 90.0)
        return cx + r * np.cos(a), cy + r * np.sin(a)

    parts = [
        f'<svg viewBox="0 0 {size} {size}" width="100%" style="max-width:{size}px" role="img"'
        f' aria-label="detection envelope and live aircraft">'
    ]

    # range rings
    rings = [max_km * f for f in (0.25, 0.5, 0.75, 1.0)]
    for km in rings:
        r = km / max_km * r_max
        parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r:.1f}" fill="none" stroke="var(--line)"'
            f' stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{cx + 3}" y="{cy - r + 11:.1f}" fill="var(--mut)" font-size="9">'
            f"{km:g} km</text>"
        )
    for ang, lab in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
        x, y = pt(ang, max_km * 1000)
        parts.append(
            f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="var(--line)"'
            f' stroke-width="1"/>'
        )
        lx, ly = pt(ang, max_km * 1000 * 1.1)
        parts.append(
            f'<text x="{lx:.1f}" y="{ly + 4:.1f}" text-anchor="middle" fill="var(--mut)"'
            f' font-size="11" font-weight="600">{lab}</text>'
        )

    # measured detection envelope
    heard_pts = [(e["bearing_deg"], e["max_heard_m"]) for e in envelope if e.get("max_heard_m")]
    if len(heard_pts) >= 3:
        poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in (pt(b, m) for b, m in heard_pts))
        parts.append(
            f'<polygon points="{poly}" fill="var(--acc)" fill-opacity="0.16"'
            f' stroke="var(--acc)" stroke-width="1.5"/>'
        )

    # passes that should have been audible and were not
    for e in envelope:
        if e.get("min_missed_m"):
            x, y = pt(e["bearing_deg"], e["min_missed_m"])
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="none" stroke="var(--warn)"'
                f' stroke-width="1.2"/>'
            )

    # passes actually heard
    for rec in recent:
        if rec.get("adsb_bearing_deg") is None or rec.get("adsb_slant_m") is None:
            continue
        x, y = pt(rec["adsb_bearing_deg"], rec["adsb_slant_m"])
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="var(--ok)"/>')

    # live traffic
    for a in overhead:
        x, y = pt(a["bearing_deg"], a["slant_m"])
        label = a.get("type_code") or a.get("registration") or ""
        low = (a.get("agl_m") or 1e9) < 3000
        colour = "var(--acc)" if low else "var(--mut)"
        parts.append(
            f'<g><circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{colour}"'
            f' fill-opacity="0.85"/><title>{_esc(label)} '
            f'{_fmt(a["slant_m"] / 1000, 1)} km, {_fmt(a.get("agl_m"), 0)} m AGL</title></g>'
        )
        if label and low:
            parts.append(
                f'<text x="{x + 6:.1f}" y="{y + 3:.1f}" fill="var(--fg)" font-size="9">'
                f"{_esc(label)}</text>"
            )

    parts.append(
        f'<circle cx="{cx}" cy="{cy}" r="3.5" fill="var(--fg)"/>'
        f'<text x="{cx}" y="{cy + 16}" text-anchor="middle" fill="var(--mut)" font-size="9">'
        f"station</text></svg>"
    )
    return "".join(parts)


def _product_rows(db: store.Store) -> list[dict]:
    """Each product with its confidence and contributing-pass count.

    This is what replaced the gates in BUILD.md 3: a product with too little data says so
    instead of showing a number.
    """
    n_passes = db.count("passes")
    em = db.error_model()
    blades = db.infer_blade_counts()
    rpm_rows = db.conn.execute("SELECT COUNT(*) FROM passes WHERE rpm IS NOT NULL").fetchone()[0]
    scint = db.conn.execute(
        "SELECT COUNT(*) FROM passes WHERE scint_exponent IS NOT NULL"
    ).fetchone()[0]
    regs = db.conn.execute(
        "SELECT registration, COUNT(*) c FROM passes WHERE registration IS NOT NULL"
        " GROUP BY registration ORDER BY c DESC LIMIT 1"
    ).fetchone()
    best_reg_n = regs["c"] if regs else 0

    def row(name, n, need, value, note, zero_is_fine=False):
        """Confidence from the contributing-pass count.

        `zero_is_fine` matters: for non-cooperative detections, *zero* is a perfectly good
        answer rather than missing data. Flagging it red would train the reader to ignore the
        column.
        """
        if n == 0 and not zero_is_fine:
            conf = ("none", "bad")
        elif n < need:
            conf = (f"insufficient ({n}/{need})", "warn")
        else:
            conf = ("usable", "ok")
        return {"product": name, "n": n, "value": value, "confidence": conf, "note": note}

    atmos_samples = db.atmospheric_samples()
    n_atmos = len(atmos_samples)
    sounding = db.sounding()
    n_expected = em.get("n", 0) + db.count("unheard")
    n_unexplained = db.conn.execute(
        "SELECT COUNT(*) FROM passes WHERE icao24 IS NULL"
    ).fetchone()[0]

    return [
        row(
            "Propeller RPM",
            rpm_rows,
            20,
            f"{rpm_rows} pass(es) with RPM"
            + (f"; blade count inferred for {len(blades)} type(s)" if blades else ""),
            "precision equals epsilon; needs blade count, which no standard table carries",
        ),
        row(
            "Speed / slant range",
            em.get("n", 0),
            20,
            f"speed {_fmt(100 * em.get('speed_frac', {}).get('median_abs', float('nan')), 1)}%"
            if em.get("n")
            else "—",
            "measured against ADS-B truth, not the fit's own error bars",
        ),
        row(
            "Turbulence (scintillation)",
            scint,
            20,
            "see diurnal plot",
            "the f^(7/6) exponent needs averaging; a single pass cannot pin it",
        ),
        row(
            "Wind aloft",
            n_atmos,
            20,
            (
                f"{_fmt(sounding.wind_speed, 1)} m/s from "
                f"{_fmt(sounding.wind_bearing_deg, 0)}&deg; "
                f"&plusmn;{_fmt(sounding.sigma_wind, 1)}"
            )
            if sounding
            else "—",
            "the strong atmospheric product: ~1 m/s per good pass",
        ),
        row(
            "Temperature aloft",
            n_atmos,
            20,
            (
                f"{_fmt(sounding.temperature_c, 1)} &deg;C "
                f"&plusmn;{_fmt(sounding.sigma_temperature_c, 1)}"
            )
            if sounding
            else "—",
            "the weak one: the same sigma_c is ~1 m/s of wind but ~1.7 C of temperature",
        ),
        row(
            "Prop health",
            best_reg_n,
            30,
            "—",
            "per-tail-number trend; reports nothing meaningful for months by design",
        ),
        row(
            "Non-cooperative detections",
            n_expected,
            10,
            f"{n_unexplained} unexplained &middot; detection rate "
            f"{_fmt(100 * db.detection_rate(), 0)}%",
            "zero is a good answer; only interpretable because unheard passes are stored too",
            zero_is_fine=True,
        ),
    ]


def _live_status(db_path: str) -> dict | None:
    """Read the capture pipeline's heartbeat file, if it is running."""
    import json as _json
    import os as _os
    import time as _time

    try:
        p = _os.path.join(_os.path.dirname(str(db_path)), "pipeline_status.json")
        d = _json.loads(open(p).read())
        d["age_s"] = _time.time() - d.get("status_written", 0)
        d["listening"] = d["age_s"] < 180
        return d
    except Exception:
        return None


def _spectrogram_snapshot(db_path: str) -> dict | None:
    """Load the snapshot the capture process publishes beside the heartbeat."""
    import os as _os

    try:
        path = _os.path.join(_os.path.dirname(str(db_path)), "spectrogram.npz")
        with np.load(path) as z:
            img = np.asarray(z["img"], dtype=np.uint8)
            freqs = np.asarray(z["freqs"], dtype=float)
            return {
                "img": img,
                "f_lo": float(freqs[0]),
                "f_hi": float(freqs[-1]),
                "t_end": float(z["t_end"]),
                "window_s": float(z["window_s"]),
                "db_lo": float(z["db_lo"]),
                "db_hi": float(z["db_hi"]),
                "age_s": max(0.0, __import__("time").time() - float(z["t_end"])),
            }
    except Exception:
        return None


def _sse(event: str, data: str) -> str:
    """One server-sent event. Every line of the payload needs its own `data:` prefix."""
    body = "".join(f"data: {line}\n" for line in data.split("\n"))
    return f"event: {event}\n{body}\n"


def _live_meta(db_path: str) -> dict | None:
    """The live ring's geometry, or None when the capture process publishes no columns."""
    reader = livespec.open_reader(db_path)
    if reader is None:
        return None
    try:
        if reader.seq <= 0:
            return None
        return {
            "f_lo": reader.f_lo, "f_hi": reader.f_hi,
            "window_s": reader.capacity * reader.hop_s,
            "hop_s": reader.hop_s, "transform_s": reader.window_s,
            "db_lo": reader.db_lo, "db_hi": reader.db_hi,
        }
    finally:
        reader.close()


def spectrogram_svg(meta: dict | None, cache_bust: str = "",
                    live: dict | None = None) -> str:
    """Frame the heatmap with axes and a magnitude scale.

    Only the axes, the colour bar and the hover readout are SVG; drawing the cells as rects
    would mean tens of thousands of elements for one panel. The heatmap itself is either a
    `<canvas>` fed column by column over server-sent events, or -- when no live ring is being
    published -- the raster at /spectrogram.png, which the capture process rewrites once a
    heartbeat.

    The axes are identical either way, so the panel does not change shape when the capture
    process restarts and the live feed drops back to the still.
    """
    W, H = 720, 268
    L, R, T, B = 48, 86, 10, 30
    pw, ph = W - L - R, H - T - B
    if live is None and meta is None:
        return ('<p class="note">No spectrogram yet. The capture process publishes one every '
                "heartbeat once it has a couple of minutes of audio.</p>")

    source = live or meta
    f_lo, f_hi = source["f_lo"], source["f_hi"]
    win = source["window_s"]
    # One hue, so the bar is just the ramp sampled as gradient stops.
    stops = "".join(
        f'<stop offset="{t * 100:.0f}%" stop-color="rgb({c[0]},{c[1]},{c[2]})"/>'
        for t, c in _SPECTROGRAM_RAMP
    )

    def fy(hz):      # frequency bins are linear in Hz, and low frequency sits at the bottom
        return T + ph * (1.0 - (hz - f_lo) / max(f_hi - f_lo, 1e-9))

    yticks = [h for h in (25, 50, 100, 150, 200, 300, 400) if f_lo <= h <= f_hi]
    ylab = "".join(
        f'<line x1="{L - 4}" y1="{fy(h):.1f}" x2="{L}" y2="{fy(h):.1f}" class="ax"/>'
        f'<text x="{L - 7}" y="{fy(h) + 3.5:.1f}" class="tk" text-anchor="end">{h}</text>'
        for h in yticks
    )
    freshness = (
        f'Live: a new column every {live["hop_s"]:.1f} s, each one a '
        f'{live["transform_s"]:.1f} s transform. <span id="spec-feed"></span>'
        if live else
        f'Updated {meta["age_s"]:.0f} s ago.'
    )
    xstep = 15 if win <= 90 else 30
    xs = [t for t in range(0, int(win) + 1, xstep)]
    xlab = "".join(
        f'<line x1="{L + pw * (1 - t / win):.1f}" y1="{T + ph}" '
        f'x2="{L + pw * (1 - t / win):.1f}" y2="{T + ph + 4}" class="ax"/>'
        f'<text x="{L + pw * (1 - t / win):.1f}" y="{T + ph + 16}" class="tk" '
        f'text-anchor="middle">{"now" if t == 0 else f"-{t}"}</text>'
        for t in xs
    )
    # The propeller band, so the eye knows where to look.
    band_y0, band_y1 = fy(300.0), fy(50.0)
    # A canvas cannot be an SVG child. `foreignObject` looks like the answer -- it would put
    # one in the same coordinate system as the axes -- but its layout and clipping are not
    # dependable: on iOS the percentage sizing resolved against the viewport instead of the
    # box, so the canvas rendered at several times its size, covered the axis labels and
    # overflowed the card. The portable arrangement is a positioned overlay, with the plot
    # rectangle expressed as percentages of the viewBox so the two stay in step if the layout
    # constants move. The SVG keeps its aspect ratio and fills the wrapper, so these resolve
    # to the same rectangle the axes are drawn around, at any width.
    heat = (
        f'<image x="{L}" y="{T}" width="{pw}" height="{ph}" preserveAspectRatio="none"'
        f' href="/spectrogram.png{cache_bust}"/>'
        if not live else ""
    )
    plot_box = (f"left:{100.0 * L / W:.4f}%;top:{100.0 * T / H:.4f}%;"
                f"width:{100.0 * pw / W:.4f}%;height:{100.0 * ph / H:.4f}%")
    canvas = (f'<canvas id="spec-canvas" style="{plot_box}"></canvas>' if live else "")
    return f"""<div class="specwrap">{canvas}<svg viewBox="0 0 {W} {H}" width="100%" role="img" id="spec-svg"
     aria-label="live spectrogram of the last {win:.0f} seconds, {f_lo:.0f} to {f_hi:.0f} Hz">
  <defs><linearGradient id="specramp" x1="0" y1="1" x2="0" y2="0">{stops}</linearGradient></defs>
  <style>
    #spec-svg .ax {{ stroke: var(--line); stroke-width: 1; }}
    #spec-svg .tk {{ fill: var(--mut); font: 10px ui-monospace, monospace; }}
    #spec-svg .cap {{ fill: var(--mut); font: 10px ui-sans-serif, system-ui; }}
    #spec-svg .bandmark {{ stroke: var(--warn); stroke-width: 1; stroke-dasharray: 3 3;
                           fill: none; opacity: .55; }}
  </style>
  {heat}
  <rect x="{L}" y="{T}" width="{pw}" height="{ph}" fill="none" stroke="var(--line)"/>
  <rect x="{L}" y="{band_y0:.1f}" width="{pw}" height="{band_y1 - band_y0:.1f}" class="bandmark"/>
  <text x="{L + 5}" y="{band_y0 - 4:.1f}" class="cap" fill="var(--warn)">50-300 Hz propeller band</text>
  {ylab}{xlab}
  <text x="12" y="{T + ph / 2:.1f}" class="cap" text-anchor="middle"
        transform="rotate(-90 12 {T + ph / 2:.1f})">frequency (Hz)</text>
  <text x="{L + pw / 2:.1f}" y="{H - 2}" class="cap" text-anchor="middle">seconds ago</text>
  <rect x="{W - R + 16}" y="{T}" width="12" height="{ph}" fill="url(#specramp)"
        stroke="var(--line)"/>
  <text x="{W - R + 33}" y="{T + 8}" class="tk">+{source["db_hi"]:.0f}</text>
  <text x="{W - R + 33}" y="{T + ph}" class="tk">{source["db_lo"]:.0f}</text>
  <text x="{W - R + 33}" y="{T + ph + 14}" class="cap">dB over median</text>
  <rect id="spec-hit" x="{L}" y="{T}" width="{pw}" height="{ph}" fill="transparent"
        data-flo="{f_lo}" data-fhi="{f_hi}" data-win="{win}"/>
</svg></div>
<p class="note" id="spec-readout">Brighter is louder than this minute's median at that
frequency, so steady wind and mains hum sit near zero and a passing tone stands out as a
sweeping curve. {freshness} <span id="spec-cursor" class="mono"></span></p>"""


def create_app(db_path: str | Path = ":memory:") -> "FastAPI":
    app = FastAPI(title="PropDop", version=__version__, docs_url="/api/docs")
    db_path = str(db_path)

    def get_db() -> store.Store:
        # SQLite connections are not shareable across threads; open per request. The database
        # is tiny (~0.3 GB/year), so this costs nothing.
        return store.Store(db_path)

    @app.get("/health")
    def health():
        return {"status": "ok", "version": __version__}

    @app.get("/api/status")
    def api_status():
        db = get_db()
        try:
            return jsonable({
                "version": __version__,
                "passes": db.count("passes"),
                "unheard": db.count("unheard"),
                "stations": db.stations(),
                "detection_rate": db.detection_rate(),
                "error_model": db.error_model(),
                "blade_counts": db.infer_blade_counts(),
                "epsilon_trend": db.epsilon_trend(),
                "scintillation_by_hour": db.scintillation_by_hour(),
                "pipeline": _live_status(db_path),
                "sounding": db.sounding().as_dict() if db.sounding() else None,
                "profile": db.profile().as_dict(),
            })
        finally:
            db.close()

    @app.get("/api/passes")
    def api_passes(limit: int = 100, offset: int = 0):
        db = get_db()
        try:
            rows = db.conn.execute(
                "SELECT pass_id, station_id, t_heard_cpa, icao24, registration, type_code,"
                " fit_f0_hz, fit_speed_mps, fit_slant_m, epsilon, rpm, blade_count,"
                " adsb_speed_mps, adsb_slant_m, match_ambiguous, template_score"
                " FROM passes ORDER BY t_heard_cpa DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return jsonable({"passes": [dict(r) for r in rows], "total": db.count("passes")})
        finally:
            db.close()

    @app.get("/api/pass/{pass_id}")
    def api_pass(pass_id: int):
        db = get_db()
        try:
            row = db.conn.execute(
                "SELECT * FROM passes WHERE pass_id = ?", (pass_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="no such pass")
            d = dict(row)
            blob = d.pop("tracks", None)
            if blob:
                t = store.unpack_tracks(blob)
                d["tracks"] = {
                    "harmonics": t["meta"]["harmonics"],
                    "dt": t["meta"]["dt"],
                    "n": t["meta"]["n"],
                    "noise_bias": t["meta"]["noise_bias"],
                }
            return jsonable(d)
        finally:
            db.close()

    def _chrome(title, body, live):
        if live is None:
            dot, label = "var(--bad)", "not running"
        elif live.get("listening"):
            dot, label = "var(--ok)", "listening"
        else:
            dot, label = "var(--bad)", "stalled"
        return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="300">  <!-- no-JS fallback; JS refreshes live regions every 5 s -->
<title>{_esc(title)}</title><style>{CSS}</style></head><body>
<header><div class="inner">
<h1><span class="dot" id="state-dot" style="background:{dot}"></span>PropDop</h1>
<span class="mut" style="font-size:12.5px"><span id="state-label">{_esc(label)}</span>
&middot; v{_esc(__version__)}</span>
<nav><a href="/">dashboard</a><a href="/passes">passes</a><a href="/api/status">api</a></nav>
</div></header><main>{body}</main></body></html>"""

    def _live_fragments(db, initial: bool = False) -> dict:
        """The parts of the dashboard that change minute to minute."""
        live = _live_status(db_path)
        overhead = (live or {}).get("overhead") or []
        envelope = db.detection_envelope()
        recent = db.recent_positions()

        if overhead:
            over = (
                "<table><tr><th>type</th><th>reg</th><th>bearing</th><th>range</th>"
                "<th>AGL</th><th>speed</th></tr>"
                + "".join(
                    f"<tr><td>{_esc(a.get('type_code') or '—')}</td>"
                    f"<td>{_esc(a.get('registration') or '—')}</td>"
                    f"<td>{_fmt(a['bearing_deg'], 0)}&deg;</td>"
                    f"<td>{_fmt(a['slant_m'] / 1000, 1)} km</td>"
                    f"<td>{_fmt(a.get('agl_m'), 0)} m</td>"
                    f"<td>{_fmt(a.get('speed_mps'), 0)} m/s</td></tr>"
                    for a in overhead[:12]
                )
                + "</table>"
            )
        else:
            over = '<p class="note">Nothing within range right now.</p>'

        if live is None:
            cap = ('<p class="bad">The capture pipeline is not running. Start it with '
                   "<code>docker compose up -d propdop</code>.</p>")
            state = "not running"
        else:
            nd = live.get("node", {})
            lag = live.get("capture_lag_s")
            cap = (
                "<table><tr><th>uptime</th><th>blocks</th><th>analyses</th><th>ADS-B</th>"
                "<th>failures</th><th>drift</th><th>capture lag</th>"
                "<th>analysis cost</th></tr>"
                f"<tr><td>{_fmt(live.get('uptime_s', 0) / 3600, 1)} h</td>"
                f"<td>{_fmt(live.get('blocks_captured'), 0)}</td>"
                f"<td>{_fmt(live.get('analyses'), 0)}</td>"
                f"<td>{_fmt(live.get('adsb_polls'), 0)} polls,"
                f" {_fmt(live.get('adsb_tracks'), 0)} tracks</td>"
                f"<td>{_fmt(live.get('adsb_failures'), 0)}/"
                f"{_fmt(live.get('analysis_failures'), 0)}</td>"
                f"<td>{_fmt(nd.get('drift_ppm'), 0)} ppm</td>"
                f"<td>{_fmt(lag, 1)} s</td>"
                f"<td>{_fmt(live.get('analysis_duration_s'), 0)} s of "
                f"{_fmt(live.get('analysis_period_s'), 0)} s</td></tr></table>"
            )
            if lag is not None and lag > 15.0:
                cap += ('<p class="bad">Capture is falling behind real time by '
                        f"{_fmt(lag, 0)} s. Samples are being dropped and every timestamp "
                        "downstream is wrong.</p>")
            # Two different complaints: one says the headroom is gone, the other says audio
            # went unanalysed -- and an unanalysed stretch leaves no trace anywhere else,
            # not even an unheard row, so it has to be said here.
            if live.get("coverage_gaps"):
                cap += ('<p class="bad">Analysis has taken longer than the '
                        f"{_fmt(live.get('analysis_window_s'), 0)} s window "
                        f"{_fmt(live.get('coverage_gaps'), 0)} time(s): stretches of audio "
                        "were never analysed at all, and leave no row behind. Lower "
                        "<code>analysis_period_s</code> or <code>max_detections</code>, or "
                        "move to a faster machine.</p>")
            elif live.get("analysis_overruns"):
                cap += ('<p class="note">Analysis has overrun its '
                        f"{_fmt(live.get('analysis_period_s'), 0)} s period "
                        f"{_fmt(live.get('analysis_overruns'), 0)} time(s). Coverage still "
                        "holds while it stays under the "
                        f"{_fmt(live.get('analysis_window_s'), 0)} s window, but there is no "
                        "headroom left.</p>")
            state = "listening" if live.get("listening") else "stalled"

        cand_rows = db.recent_unheard(limit=10)
        if cand_rows:
            cand = (
                "<table><tr><th>expected</th><th>type</th><th>eng</th><th>range</th>"
                "<th>bearing</th><th>score</th><th>verdict</th><th>why rejected</th></tr>"
                + "".join(
                    f"<tr><td>{_clock(r['t_heard_cpa'], '%H:%M:%S')}</td>"
                    f"<td>{_esc(r.get('type_code') or '—')}</td>"
                    f"<td>{_esc({'J': 'jet', 'T': 'turbo', 'P': 'piston', 'R': 'rotor'}.get(r.get('engine_class') or '', '—'))}</td>"
                    f"<td>{_fmt((r.get('adsb_slant_m') or 0) / 1000, 1)} km</td>"
                    f"<td>{_fmt(r.get('adsb_bearing_deg'), 0)}&deg;</td>"
                    f"<td>{_fmt(r.get('best_template_score'), 1)}</td>"
                    f"<td class=\"mut\">"
                    f"{_esc(detect.score_verdict(_score(r.get('best_template_score'))))}"
                    f"</td><td class=\"mut\">{_esc(r.get('reject_reason') or '—')}</td></tr>"
                    for r in cand_rows
                )
                + "</table>"
                + '<p class="note">Reported above '
                + f"{detect.MIN_TEMPLATE_SCORE:.0f}. Synthetic pink noise peaks at "
                + f"{detect.PINK_NOISE_CEILING:.1f}, but <em>real</em> background reaches "
                + f"{detect.FIELD_NOISE_CEILING:.1f} (measured over 274 verified "
                + "aircraft-free windows), so a score between the two is not evidence of a "
                + "tone. Below the lower figure the tone never reached the microphone at all, "
                + "and the siting is the limit rather than the threshold. "
                + "Blank scores are rows stored before scoring was recorded.</p>"
            )
        else:
            cand = ('<p class="note">No expected-but-unheard passes recorded yet. Either '
                    "nothing has come within range, or everything that did was heard.</p>")

        heard_sectors = sum(1 for e in envelope if e.get("max_heard_m"))
        best = max((e["max_heard_m"] or 0) for e in envelope) if envelope else 0
        note = (
            f"Shaded lobe: farthest pass actually heard, per bearing sector "
            f"({heard_sectors}/{len(envelope)} sectors have data, best "
            f"{_fmt(best / 1000, 1)} km). Hollow marks are passes ADS-B says should have been "
            f"audible and were not &mdash; a notch with hollow marks beyond it is a direction "
            f"this site is deaf in."
            if heard_sectors
            else "No heard passes yet, so there is no measured envelope. Live traffic is "
            "shown; the lobe fills in as passes are detected."
        )
        spec = _spectrogram_snapshot(db_path)
        # When the panel is live, the canvas owns it and must not be replaced on a poll:
        # swapping the fragment would destroy the element and with it the EventSource feeding
        # it. `None` is the existing signal for "leave this region alone". The first render
        # still has to produce the canvas, which is what `initial` is for.
        live = _live_meta(db_path)
        bust = f"?t={int(spec['t_end'])}" if spec else ""
        return {
            "radar": radar_svg(envelope, overhead, recent),
            # The cache-buster is what makes the <image> refetch; without it the browser
            # serves the first PNG forever and the panel looks frozen.
            "spectrogram": (
                spectrogram_svg(spec, bust, live) if initial
                else None if live
                else spectrogram_svg(spec, bust)
            ),
            "overhead": over,
            "candidates": cand,
            "capture": cap,
            "radar_note": note,
            "state": state,
            "n_overhead": len(overhead),
            "listening": bool(live and live.get("listening")),
        }

    @app.get("/spectrogram.png")
    def spectrogram_png():
        """The heatmap itself. Served as a raster, encoded with the standard library only."""
        meta = _spectrogram_snapshot(db_path)
        if meta is None:
            raise HTTPException(status_code=404, detail="no spectrogram published yet")
        # Row 0 of a PNG is the top row, and low frequency belongs at the bottom.
        png = _png_indexed(meta["img"][::-1], _spectrogram_palette())
        return Response(
            content=png,
            media_type="image/png",
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/api/spectrogram/stream")
    async def spectrogram_stream(since: int | None = None):
        """Push STFT columns to the browser as the capture process publishes them.

        Server-sent events rather than a WebSocket: the traffic is one-way, SSE is a plain
        HTTP response that reconnects by itself, and it needs nothing in the page beyond
        `EventSource` -- which matters, because the dashboard has to work on a network with
        no outbound route and so cannot fetch a client library.

        The first event carries the geometry and the palette, so the browser can size its
        canvas and colour the bytes without a second request; after that each event is a run
        of new columns. A client that falls behind is handed the freshest full ring rather
        than stale slots -- see `livespec.Reader.read_since`.
        """
        import asyncio as _asyncio
        import base64 as _base64

        reader = livespec.open_reader(db_path)
        if reader is None:
            raise HTTPException(status_code=503, detail="no live spectrogram is being published")

        async def events():
            nonlocal reader
            cursor = since
            try:
                yield _sse("meta", json.dumps({
                    "n_bins": reader.n_bins, "f_lo": reader.f_lo, "f_hi": reader.f_hi,
                    "hop_s": reader.hop_s, "window_s": reader.window_s,
                    "capacity": reader.capacity,
                    "palette": list(_spectrogram_palette()),
                }))
                idle = 0.0
                while True:
                    if reader.stale():       # the capture process restarted and recreated it
                        reader.close()
                        fresh = livespec.open_reader(db_path)
                        if fresh is None:
                            return
                        reader, cursor = fresh, None
                    cursor, cols = reader.read_since(cursor)
                    if cols.size:
                        idle = 0.0
                        yield _sse("cols", json.dumps({
                            "seq": cursor,
                            "n": int(cols.shape[0]),
                            "b64": _base64.b64encode(cols.tobytes()).decode("ascii"),
                        }))
                    else:
                        idle += reader.hop_s
                        if idle >= 15.0:
                            idle = 0.0
                            yield ": keepalive\n\n"   # stops an idle proxy closing us
                    await _asyncio.sleep(max(reader.hop_s * 0.5, 0.05))
            finally:
                reader.close()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/live")
    def api_live():
        """Fragments for the live regions, polled by the dashboard."""
        db = get_db()
        try:
            return jsonable(_live_fragments(db))
        finally:
            db.close()

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        db = get_db()
        try:
            live = _live_status(db_path)
            n = db.count("passes")
            em = db.error_model()
            trend = db.epsilon_trend()
            hours = db.scintillation_by_hour()
            eps_med = em.get("epsilon_median")

            frag = _live_fragments(db, initial=True)
            cards = "".join(
                f'<div class="card"><div class="k">{_esc(k)}</div>'
                f'<div class="v"{idattr}>{v}</div></div>'
                for k, v, idattr in [
                    ("passes", _fmt(n, 0), ""),
                    ("unheard", _fmt(db.count("unheard"), 0), ""),
                    ("detection rate", f"{_fmt(100 * db.detection_rate(), 0)}%", ""),
                    ("median epsilon", f"{_fmt(100 * eps_med, 3)}%" if eps_med else "&mdash;", ""),
                    ("overhead now", _fmt(frag["n_overhead"], 0), ' id="n-overhead"'),
                ]
            )

            prod = "".join(
                f"<tr><td>{_esc(p['product'])}</td>"
                f"<td><span class=\"pill {p['confidence'][1]}\">{_esc(p['confidence'][0])}"
                f"</span></td><td>{p['value']}</td>"
                f"<td class=\"mut\">{_esc(p['note'])}</td></tr>"
                for p in _product_rows(db)
            )

            err = '<p class="note">No matched passes yet, so no measured error model.</p>'
            if em.get("n"):
                err = (
                    "<table><tr><th>quantity</th><th>bias</th><th>median abs</th>"
                    "<th>p90 abs</th></tr>"
                    + "".join(
                        f"<tr><td>{name}</td><td>{_fmt(st['bias'], 3)}</td>"
                        f"<td>{_fmt(st['median_abs'], 3)}</td>"
                        f"<td>{_fmt(st['p90_abs'], 3)}</td></tr>"
                        for name, st in (("time (s)", em["time_s"]),
                                         ("speed (frac)", em["speed_frac"]),
                                         ("slant (frac)", em["slant_frac"]))
                    )
                    + "</table><p class=\"note\">Measured slant error is "
                    f"{_fmt(em['slant_error_over_epsilon'], 1)}x the fit's own residual, which "
                    "is why the error model is derived from paired rows rather than propagated "
                    "from the fit covariance.</p>"
                )

            sounding = db.sounding()
            if sounding is None:
                snd = ('<p class="note">Not enough matched passes yet. The inversion solves for '
                       "the medium <em>given</em> the geometry, so only ADS-B-matched passes "
                       "qualify.</p>")
            else:
                prof = db.profile()
                rows_h = "".join(
                    (f"<tr><td>{_fmt(l.altitude_lo, 0)}&ndash;{_fmt(l.altitude_hi, 0)} m</td>"
                     f"<td>{l.n}</td><td>{_fmt(l.wind_speed, 1)} m/s from "
                     f"{_fmt(l.wind_bearing_deg, 0)}&deg;</td>"
                     f"<td>{_fmt(l.temperature_c, 1)} &deg;C &plusmn;"
                     f"{_fmt(l.sigma_temperature_c, 1)}</td></tr>")
                    if l else '<tr><td colspan="4" class="mut">no passes reached this layer</td></tr>'
                    for l in prof.layers
                )
                snd = (
                    f'<p>Bulk, {sounding.n} passes: <strong>{_fmt(sounding.wind_speed, 1)} m/s '
                    f'from {_fmt(sounding.wind_bearing_deg, 0)}&deg;</strong>, '
                    f'{_fmt(sounding.temperature_c, 1)} &deg;C &plusmn;'
                    f'{_fmt(sounding.sigma_temperature_c, 1)}</p>'
                    + (f'<p class="warn">{_esc(sounding.note)}</p>' if sounding.note else "")
                    + "<table><tr><th>layer</th><th>n</th><th>wind</th><th>temperature</th></tr>"
                    + rows_h + "</table>"
                )

            body = f"""
<div class="grid">{cards}</div>
<div class="cols" style="margin-top:22px">
  <div>
    <h2>Local airspace</h2>
    <div id="radar">{frag["radar"]}</div>
    <div class="legend">
      <span><i style="background:var(--acc)"></i>live traffic</span>
      <span><i style="background:var(--ok)"></i>heard</span>
      <span><i style="border:1px solid var(--warn)"></i>expected, not heard</span>
      <span><i style="background:var(--acc);opacity:.35"></i>hearing envelope</span>
    </div>
    <p class="note" id="radar-note">{frag["radar_note"]}</p>
    <h2>Audible candidates considered</h2>
    <div id="candidates">{frag["candidates"]}</div>
  </div>
  <div>
    <h2>Capture</h2>
    <div id="capture">{frag["capture"]}</div>
    <h2>Overhead now</h2>
    <div id="overhead">{frag["overhead"]}</div>
  </div>
</div>
<h2>Live spectrogram</h2>
<div id="spectrogram">{frag["spectrogram"]}</div>
<h2>Instrument health</h2>
{line_svg([t["bucket_start"] / 86400.0 for t in trend],
          [100 * t["mean_epsilon"] for t in trend],
          label="epsilon over time", xlabel="days", ylabel="% of f0")}
<p class="note">Required pass counts scale as epsilon squared, so this is the single most
important number here: smartphone-grade to good-recording-grade fits is a ~20x difference.</p>
<h2>Products</h2>
<table><tr><th>product</th><th>confidence</th><th>value</th><th>note</th></tr>{prod}</table>
<h2>Sounding</h2>
{snd}
<h2>Measured error against ADS-B</h2>
{err}
<h2>Turbulence</h2>
{line_svg([h["hour_utc"] for h in hours], [h["mean_log_amp_var"] for h in hours],
          label="log-amplitude variance by hour UTC", xlabel="hour", ylabel="variance")}
<p class="note">A real turbulence signal should show a large diurnal swing and scale as
f^(7/6) across harmonics. Absence is a decisive negative, not a null result.</p>
{LIVE_SCRIPT}"""
            return _chrome("PropDop", body, live)
        finally:
            db.close()

    @app.get("/passes", response_class=HTMLResponse)
    def passes_page(limit: int = 100):
        db = get_db()
        try:
            rows = db.query_all(
                "SELECT pass_id, t_heard_cpa, registration, type_code, fit_f0_hz,"
                " fit_speed_mps, fit_slant_m, adsb_speed_mps, epsilon, rpm, match_ambiguous"
                " FROM passes ORDER BY t_heard_cpa DESC LIMIT ?",
                (limit,),
            )
            body_rows = "".join(
                f'<tr><td><a href="/pass/{r["pass_id"]}">{r["pass_id"]}</a></td>'
                f'<td>{_clock(r["t_heard_cpa"])}</td>'
                f'<td>{_esc(r["registration"] or "—")}</td>'
                f'<td>{_esc(r["type_code"] or "—")}</td>'
                f'<td>{_fmt(r["fit_f0_hz"], 2)}</td>'
                f'<td>{_fmt(r["fit_speed_mps"], 1)}</td>'
                f'<td>{_fmt(r["adsb_speed_mps"], 1)}</td>'
                f'<td>{_fmt(r["fit_slant_m"], 0)}</td>'
                f'<td>{_fmt(100 * r["epsilon"], 3)}%</td>'
                f'<td>{_fmt(r["rpm"], 0)}</td>'
                f'<td class="warn">{"ambiguous" if r["match_ambiguous"] else ""}</td></tr>'
                for r in rows
            )
            body = (
                "<h2>Passes</h2><table><tr><th>id</th><th>heard CPA</th><th>reg</th>"
                "<th>type</th><th>f0 Hz</th><th>v fit</th><th>v ADS-B</th><th>slant m</th>"
                "<th>epsilon</th><th>rpm</th><th></th></tr>"
                + (body_rows or '<tr><td colspan="11" class="mut">none yet</td></tr>')
                + "</table>"
            )
            return _chrome("PropDop \u2014 passes", body, _live_status(db_path))
        finally:
            db.close()

    @app.get("/pass/{pass_id}", response_class=HTMLResponse)
    def pass_page(pass_id: int):
        db = get_db()
        try:
            row = db.query_one("SELECT * FROM passes WHERE pass_id = ?", (pass_id,))
            if row is None:
                raise HTTPException(status_code=404, detail="no such pass")
            d = dict(row)
            plots = '<p class="note">No tracks stored for this pass.</p>'
            if d.get("tracks"):
                t = store.unpack_tracks(d["tracks"])
                figs = []
                for k in t["meta"]["harmonics"][:4]:
                    figs.append(line_svg(t["t"], t[f"df{k}"],
                                         label=f"harmonic {k}: frequency residual", xlabel="s",
                                         ylabel="Hz"))
                    figs.append(line_svg(
                        t["t"], t[f"la{k}"],
                        label=f"harmonic {k}: log-amplitude fluctuation (noise bias "
                              f"{_fmt(t['meta']['noise_bias'].get(str(k)), 4)})",
                        xlabel="s", ylabel="ln A"))
                plots = "".join(figs)

            cards = "".join(
                f'<div class="card"><div class="k">{_esc(k)}</div><div class="v">{v}</div></div>'
                for k, v in [
                    ("f0 Hz", _fmt(d["fit_f0_hz"], 2)),
                    ("speed m/s", _fmt(d["fit_speed_mps"], 1)),
                    ("slant m", _fmt(d["fit_slant_m"], 0)),
                    ("epsilon", f"{_fmt(100 * d['epsilon'], 3)}%"),
                    ("rpm", _fmt(d["rpm"], 0)),
                    ("ADS-B v", _fmt(d["adsb_speed_mps"], 1)),
                    ("ADS-B slant", _fmt(d["adsb_slant_m"], 0)),
                    ("match chi", _fmt(d["match_chi"], 2)),
                ]
            )
            # A caveat stored on the row and shown nowhere is not a caveat. These carry the
            # two cases that most need saying out loud: a pass whose geometry predates the
            # local-time-frame fix, and one kept on its ADS-B match against its own screening.
            caveat = (
                f'<p class="note"><strong>Caveat:</strong> {_esc(d["notes"])}</p>'
                if d["notes"] else ""
            )
            body = f"""<h2>Pass {pass_id} &middot; {_esc(d["registration"] or "unidentified")}
&middot; {_esc(d["type_code"] or "unknown type")} &middot; {_esc(d["source"])}</h2>
{caveat}
<div class="grid">{cards}</div>
<h2>Stored tracks</h2>
<p class="note">Audio is kept only as a capped 1 GB corpus, so these per-harmonic tracks are
the permanent record &mdash; about 15 kB per pass. A spectrogram view is only possible for
passes still in the corpus.</p>
{plots}"""
            return _chrome(f"PropDop \u2014 pass {pass_id}", body, _live_status(db_path))
        finally:
            db.close()

    return app


def serve(db_path: str | Path, host: str = "0.0.0.0", port: int = 8475) -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run(create_app(db_path), host=host, port=port, log_level="info")
