# What to build — the decision

This supersedes the build orders in every other document. The rest are research; this is scope.

---

## The single insight that sets the scope

Every product in this project reduces to **one measurement**: a harmonic-comb fit to a flyover, producing
per-harmonic frequency *and amplitude* over time, plus its own residual `ε`.

| Product | What it needs from the fit |
|---|---|
| Propeller RPM | `f0`, and nothing else. Precision **is** `ε` |
| Prop/engine health | energy at shaft-rate harmonics that aren't BPF multiples |
| Wind and temperature aloft | the fitted curve's **mean** against ADS-B geometry |
| Turbulence (scintillation) | per-harmonic **log-amplitude fluctuations** |
| Noise emission and directivity | per-harmonic **absolute amplitude** vs angle |
| Non-cooperative detection | a comb with no ADS-B aircraft to explain it |

So this is not six products. It is **one instrument read six ways.** Build the comb fit properly and the
rest are analysis modules over a database table. Build it badly and nothing downstream works, because `ε`
caps every single one.

---

## 1. Build now — the spine

| Component | Scope |
|---|---|
| **ADS-B node** | RTL-SDR + 1090 MHz antenna, mounted high, `readsb` in a container |
| **One acoustic node** *(capture built: `propdop.node`; needs real hardware)* | Pi + mic, **large windscreen, mounted low**. 48 kHz capture → 10-minute ring buffer → HTTP "give me t1..t2" endpoint ([DESIGN.md §3.3](DESIGN.md)) |
| **Surface met sensor** | Temperature, humidity, pressure at the node. The physics needs it; ~$50 and it removes an assumed term |
| **ADS-B associator** *(built: `propdop.associate`)* | A two-way join, not a scheduler: match each detection to an ADS-B track, and record tracks with no detection. Supplies blade count (hence RPM), geometry (hence the atmospheric products) and truth for the error model. Measured: ADS-B triggering buys no sensitivity -- blind detection reaches -30 dB SNR at 5 km (DESIGN §3.1) |
| **The comb fit** *(built: `propdop.fit`)* | Whitened log-f STFT → ridge trace → harmonic comb fit. Emits **per-harmonic frequency and amplitude time series**, not just curve parameters, and records `ε` on every pass. The one piece worth over-engineering |
| **Blind detector** *(built: `propdop.detect`)* | Runs continuously over all audio — the primary and only detection path. ADS-B Out is not required in Class G below 10,000 ft, so this is also the only route to non-transmitting aircraft. Includes `plausible_aircraft` ground-source rejection (DESIGN §3.1a) |
| **Pass database** *(built: `propdop.store`)* | TimescaleDB: one row per pass per node, plus the derived tracks (§2) |
| **Validation harness** | Speed and slant range against ADS-B, as a distribution over hundreds of passes, plus an **empirical** error model — the fit's own error bars are ~5× overconfident ([APPLICATIONS.md §2](APPLICATIONS.md)) |
| **Container** *(built: `Dockerfile`, `docker-compose.yml`)* | Deployed on server with **all state under `./data`** — `db/`, `corpus/`, `archive/`, `config/`, `adsb/`. No GPU service, no archive fetcher. The image is disposable; data survives rebuilds |
| **Web UI** *(built: `propdop.web`)* | Live passes, spectrogram with fit overlaid, `ε` over time, validation plots, and each product's current value with its confidence (§3) |

**Container layout** is [DESIGN.md §3.4](DESIGN.md), minus the GPU worker — CPU-bound workload, the 5090
earns nothing. Reuse the existing Redis.

**Ship RPM first.** It's free once the comb fit works, no incumbent carries it, precision is exactly `ε`,
and repeat passes of the same tail number validate it. It proves the spine.

## 2. Retention — the derived tracks are the product, not the audio

You were right that keeping every clip was poorly justified. The actual data product is tiny:

| What | Size | Policy |
|---|---|---|
| **Derived tracks** — per-harmonic frequency and log-amplitude at 2 Hz, 8 harmonics, plus fit parameters, residuals and `ε` | **~15 kB/pass** → 0.28 GB/year at 50 passes/day | **Keep forever.** This is the dataset. Scintillation, health trends and every re-analysis that isn't a DSP change read *this*, not audio |
| **Retest corpus** | **1 GB total**, split below | Curated, capped, see policy below |
| **Unexplained detections** | ±2 min around each, ~29 MB/day at 10/day | Keep — these are the non-cooperative product's evidence |
| **Rolling raw buffer** | 48 h at 8 kHz FLAC = **2.1 GB**, self-expiring | For debugging and for investigating fresh detections |
| Everything else | — | **Discarded after the fit runs** |

### The 1 GB retest corpus

Audio is only needed to re-run the *DSP* against a known input. That wants diversity and stability, not
volume. 8 kHz is sufficient for everything in scope — prop BPF is 80–90 Hz and the 20th harmonic is
1800 Hz, well inside a 4 kHz Nyquist, and the only things wanting more bandwidth (humidity-from-absorption,
jets) are cut.

- **250 MB — frozen golden set, 48 kHz FLAC, ~30 clips.** Chosen once and **never rotated**, so DSP
  versions stay comparable over years. Full bandwidth, so the resampling path itself is testable.
- **750 MB — rolling diversity set, 8 kHz FLAC, ~520 clips.** Bin by type class, slant range, `ε`, time of
  day and season; keep N per bin and evict the oldest within a bin when it overflows.
- **Anomalies take priority for eviction slots:** failed fits, outliers, disagreements with ADS-B, and
  unexplained detections. Those are the clips you actually want to re-run.

A frozen set plus a rolling set matters more than the total size: if the whole corpus rotates you can't
compare DSP versions, and comparability is the only reason to keep audio at all.

**Bioacoustics is cut**, and with it the permanent 24/7 archive. Continuous recording stays only to feed
the in-stream detector for non-ADS-B aircraft, and that detector keeps its *detections* rather than its
input.

## 3. All six products implemented now, each reporting its own confidence

No gates. On reflection most of what I'd called gates weren't decision points at all — they were
measurements the system produces anyway (`ε`, pass density, the diurnal scaling test). Gating on them was
false structure. Build all six modules; they read the same table and report continuously.

What replaces the gates is a **sufficiency and confidence field on every product**, which is better
discipline than a go/no-go anyway:

| Product | Reports | Honest expectation |
|---|---|---|
| **RPM** | value ± `ε`-derived error | Good immediately: ±5–22 RPM at 2,700 |
| **Wind aloft** | per-layer value, **contributing pass count**, error | Usable within days where traffic allows; ~6 passes per 1–2 km layer for 1 m/s |
| **Temperature aloft** | same | Will read wide initially — ~15 passes per layer for 1 °C. Report it, don't lead with it |
| **Turbulence** | `C²_eff` in the weak regime; saturation-boundary index where saturated; plus the f^(7/6) and L^(11/6) scaling diagnostics | 20–30 % per pass. The diurnal plot is the first thing to look at ([SCINTILLATION.md](SCINTILLATION.md)) |
| **Prop health** | per-tail-number baseline and deviation | **Reports nothing meaningful for months** — it needs repeat-pass history. Build it, expect silence, and make the UI say "insufficient history" rather than a number |
| **Non-cooperative** | detections with no ADS-B match | Works from day one as a flag. Quantitative 3D needs three nodes, which stays cut |

Two things the spine must expose for its own sake, since they're no longer gates:

- **`ε` over time, prominently.** It scales every product (pass counts go as `ε²`), so it's the instrument's
  health metric. A rising `ε` means the windscreen, the siting or the mic needs attention.
- **Pass density by altitude band**, computed from ADS-B alone. It sets the achievable cadence for the
  layer products, and it's the first thing to check if the atmospheric numbers look thin.

Also run the **Mode-S register check** (`pyModeS`, BDS 4,4 / 5,0 / 6,0) once as a one-off script. Not a
gate — it changes nothing about what gets built — but if those registers are plentiful here, the
atmospheric products are duplicating something better and the UI should say so
([PRIOR-ART.md §1](PRIOR-ART.md)).

## 4. Deliberately cut

Not "later" — out of scope, with the reason.

| Cut | Why |
|---|---|
| **Bioacoustics** | Your call. Removes the permanent 24/7 archive with it |
| **Geolocation** | Ill-posed; most published blind trials failed. Code stays reachable, build nothing ([RESEARCH.md](RESEARCH.md)) |
| **adsb.lol archive fetcher** | A local receiver makes it unnecessary. Keep a manual backfill script for dates before the receiver existed; drop the nightly mirror, index cache and LRU logic |
| **GPU worker** | CPU-bound. The 5090 earns nothing here |
| **Infrasound** | Separate instrument sharing only the mast; wind-noise structures are 18–70 m across, and propeller tones sit far above the band ([INFRASOUND.md](INFRASOUND.md)) |
| **Multi-node / TDOA** | Buys independence from ADS-B, unnecessary while aircraft transmit. Two mics give a *cone*, not a bearing; three close it. Revisit only if non-cooperative detection becomes the goal |
| **Humidity from absorption** | Needs absolute calibration, only works on close passes, and absorption is non-monotonic in humidity at some frequencies |
| **Soil moisture from ground impedance** | Speculative — but see §5, the enabling hardware is worth buying anyway |
| **Thunder, meteors, bats, vehicles** | Each needs its own sensor or its own science |

## 5. Two hardware additions worth pre-committing to

**A second mic at a different height on the same mast.** Same ADC, so no sync problem and no second site.
Ground-reflection interference is a systematic the plan otherwise just hopes is small, and it specifically
contaminates the scintillation estimator — a moving source sweeps the comb-notch pattern in a way that
mimics turbulence. Two heights let you measure and correct it. The soil-moisture inversion is a free
option on top.

**A calibrated measurement microphone.** The only product needing absolute level, and the clearest
"nobody has this data" gap: measured GA noise directivity tied to ADS-B doesn't exist publicly, and
current noise mapping is modelled with ~4.5 dBA error. Straightforward rather than speculative. Without
it, the directivity product reports *relative* directivity only — still useful, since the angular shape
is what's missing from the literature.

## 6. Order — dependencies, not permissions

1. **ADS-B node.** Everything reads from it. Run the Mode-S register check while it beds in.
2. **Acoustic node**, windscreen and low siting taken seriously. This is where `ε` is won.
3. **Scheduler + clip extraction + pass database.**
4. **The comb fit**, emitting per-harmonic frequency and amplitude tracks, and `ε`.
5. **Validation harness** against ADS-B, including the empirical error model.
6. **RPM** — first product, free, validates the spine.
7. **Retention policy** (§2) wired in: derived tracks permanent, corpus capped and curated, rolling buffer
   expiring.
8. **The remaining five modules**, in whatever order appeals — they're independent analyses over the same
   table. Turbulence is the most interesting and needs no hardware; health is the longest-latency, so
   start it early even though it stays quiet.
9. **Second mic** and **calibrated mic**, which unlock the ground-reflection correction and absolute
   directivity respectively.

## 7. What "done" looks like for v1

A box on a mast and a container stack that, for every propeller aircraft overhead:

- identifies it from ADS-B,
- measures its propeller RPM to ±5–22 RPM,
- records speed and slant range with a *measured*, not assumed, error distribution,
- stores per-harmonic frequency and amplitude tracks permanently at ~15 kB per pass,
- reports wind, temperature, turbulence and prop-health deviation each with an explicit confidence and
  contributing-pass count,
- flags anything it hears that ADS-B cannot explain,
- and keeps a capped, curated 1 GB audio corpus — half of it frozen — so the DSP can be improved and
  re-benchmarked without hoarding.

One microphone, one dongle, and a real instrument.
