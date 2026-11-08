# Passive acoustic aircraft & atmosphere observatory — design

**Purpose.** Use microphones plus a local ADS-B receiver to measure, continuously and passively:

1. **Atmospheric soundings** — path-averaged temperature and wind aloft, in ~1–2 km layers from the
   surface to ~7 km, from the aircraft that happen to fly over.
2. **Per-aircraft metrics** — speed, altitude, heading, propeller RPM, and propeller/engine health, per
   tail number.
3. **Non-cooperative detections** — aircraft heard but absent from ADS-B.

**A single microphone is the baseline configuration and must stay fully supported.** Multiple spatially
separated nodes are an *optional* enhancement, not a requirement — nothing in the primary products
depends on them. §5 sets out exactly what the second and third nodes add and what single-mic deployments
give up (less than you'd think, because ADS-B already supplies the geometry).

Geolocation (finding an unknown recording site) is **deferred and not being built**. The physics and the
code are largely shared, so it stays reachable later; see [RESEARCH.md](RESEARCH.md).

---

## 1. Why this is the tractable direction

Worth stating plainly, because it's the reason to prefer it.

Geolocation fixes the aircraft and solves for the listener: 3 unknowns (lat, lon, clock) searched against
a continent of candidate aircraft, with no ground truth. It is badly ill-posed — which is exactly why the
blind field trials in RESEARCH.md §2 mostly failed.

This project fixes the listener and solves for the aircraft and the medium. Microphone positions are
surveyed once. ADS-B gives each aircraft's 3D track at ~1 Hz, continuously, for free. So every flyover
is an **over-determined fit of a handful of physical parameters against known truth** — and the truth
arrives with every pass, forever. Same forward model, inverted into a well-posed problem with a built-in
calibration source.

That also means the system validates itself from day one, instead of after a benchmark corpus is built.

## 2. What is measured, and how well

From [APPLICATIONS.md](APPLICATIONS.md), where these are derived:

| Product | Precision | Source |
|---|---|---|
| Aircraft **speed** | ~3 % steady pass, ~6 % median | pitch-drop magnitude |
| Aircraft **slant range** at CPA | **~17 % median, 34 % p75** — and the fit's own error bar is ~5× overconfident | curve steepness |
| Aircraft **altitude** (ADS-B aircraft) | ~10 m — **taken from ADS-B** | no acoustic measurement needed |
| Aircraft **altitude**, measured acoustically, 1 mic | ~23 % — poor, and only needed for non-ADS-B aircraft | slant/lateral degeneracy |
| Aircraft **altitude**, measured acoustically, ≥2 mics *(optional)* | tens of metres | TDOA + range, §5 |
| **Propeller RPM** | **exactly `ε`** — 0.2–0.8 %, i.e. ±5 to ±22 RPM at 2,700 | `f0 = N_blades·RPM/60`, so `σ_RPM/RPM = σ_f0/f0` |
| **Prop health** | detection, not a number | rotation-harmonic energy that isn't a BPF multiple |
| **Path-avg wind** | **~1 m/s per good fast pass**; direction from the 127° bearing sweep | `∂drop/∂c`, APPLICATIONS §6 |
| **Path-avg temperature** | **~1.7 °C per good fast pass** — needs averaging to be useful | same |
| **Layer wind / temperature**, 1–2 km | **~0.9 m/s / 1.5 °C per pass → ~0.3 m/s / 0.34 °C over 20 passes** | differencing passes by altitude |
| **Humidity** (speculative) | uncalibrated | differential harmonic absorption |

### The two numbers that drive the design

**Fit quality `ε` (fractional frequency precision) is the master parameter.** Per APPLICATIONS.md §6,
`σ_c = ε·(c²−v²)²/(2v(c²+v²))` — it depends *only* on `ε` and aircraft speed, not on which harmonic you
track. Demonstrated values span 0.19 % (dedicated outdoor recording) to 0.83 % (smartphone), and the
required pass count scales as `ε²`: going from smartphone-grade to good-recording-grade fits cuts it
**~20×**. This is why §6 spends its budget on windscreen and siting rather than on compute.

**Averaging is mandatory, which sets the cadence.** At ~1.5 °C per pass for a 1–2 km layer, a useful
sounding is an **hourly-to-daily aggregate**, not a per-pass snapshot. Roughly 20 passes per layer gets
temperature to ~0.34 °C and wind to ~0.3 m/s.

Honest limits: nothing above ~7 km (props only — jets are acoustically useless, RESEARCH.md §2.1); thin
layers are hopeless (a 3.0–3.5 km layer amplifies error 9.2×); **wind is a far stronger product than
temperature**, since the same `σ_c` is ~1 m/s of wind but ~1.7 °C of temperature; and systematics
(straight-ray propagation, no refraction, RPM wander, ground reflection) impose a floor whose location is
**unmeasured**, so the averaging numbers are a best case until validated against radiosondes.

## 3. Architecture

```
   ACOUSTIC NODES (1..N)                      ADS-B NODE
   Pi + mic + windscreen                      RTL-SDR + 1090 MHz antenna
   continuous capture -> ring buffer          readsb / tar1090
          |  timestamped clips                       |  aircraft tracks, ~1 Hz
          |  + decimated 24/7 archive                |
          +---------------------+--------------------+
                                |  LAN / WiFi
                      +---------v----------+
                      |    analysis host   |
                      |  (server, Docker)  |
                      +--------------------+
                       blind detection (continuous)
                       DSP: whiten, ridge, comb fit
                       inversion: atmosphere / aircraft / health
                       TimescaleDB + web UI (CPU-bound; no GPU needed)
```

### 3.1 One continuous pipeline, plus an ADS-B join

**Corrected twice, 2026-11-07.** There is no scheduler. Earlier versions of this section said
(a) the scheduler "is what makes this cheap", then (b) that it extends sensitivity for known
aircraft. Both were measured and both are false. The component that survives is not a trigger
at all but a **two-way join** between acoustic detections and ADS-B tracks.

**Measured: blind detection is not the limiting factor.** With clip length matched to `d/v`,
`propdop.detect` finds a 5 km pass at **-30 dB** broadband SNR with a template score of 40
against a null maximum of 8.9. Targeted extraction at the same conditions reaches `epsilon`
1.95% -- i.e. *the fit* is at its limit exactly where the detector is. Both paths degrade
together, so knowing when and in what band to look buys no range. The comb-matched template
integrates four harmonics over hundreds of frames, and that coherent gain is simply larger
than the multiple-comparisons penalty of searching (f0, t0, template).

So: **listen continuously, detect blindly, fit, store -- then join to ADS-B.** No clip
scheduling. The join is what the geolocation projects never needed (they analysed single known
clips and had no local receiver), which is where the idea came from and why it kept failing to
justify itself here.

**An ADS-B-triggered design cannot see non-transmitting aircraft at all**, and that is not an
edge case.
Under 14 CFR 91.225 ADS-B Out is required in Classes A/B/C and Class E above 10,000 ft, but
**not in Class G below 10,000 ft** — which is exactly where low, slow propeller traffic flies —
and aircraft never certificated with an engine-driven electrical system are exempt outright.
Published ADS-B noise-mapping work puts GA coverage nearer 30-50% for this reason. So a blind
path is mandatory, not optional.

**And triggering saves little compute.** A published matched-filter detector runs
60 minutes of audio in ~70 s on 32 threads, so a day of audio is well under an hour on this
server's 24 threads. Blind detection is affordable; that was never the argument.

What the **join** buys is structural, and four products cannot exist without it:

- **RPM, the strongest product, is impossible blind.** `f0 = N_blades * RPM / 60`. The detector
  measures `f0` to ~0.05% but cannot know the blade count, so RPM is undetermined by a factor
  of 2-3: a 90 Hz tone is 2,700 RPM on a two-blade prop or 1,800 on a three-blade. The ADS-B
  type code supplies it.
- **Prop health needs per-tail-number grouping** over months. Blind detections cannot be
  grouped by aircraft at all.
- **The atmospheric products need known geometry.** The inversion solves for `c_eff` *given*
  the path; blind it would be fitting medium and geometry together, which is degenerate. For
  scintillation it is worse: `sigma^2 ~ L^(11/6)`, so the detector's ~20% distance error
  becomes **39% error in `C_eff^2`** from geometry alone, swamping the 20-30% per-pass
  precision. ADS-B gives slant range to ~10 m and that term vanishes.
- **"Unexplained" is meaningless without the reverse join.** A detection with no ADS-B match
  could be a non-transmitting aircraft, a transmitting aircraft that was not heard, or a ground
  source. Only knowing which aircraft *should* have been audible separates those -- and that is
  also what measures detection probability against range and conditions. The join is what makes
  the blind detector's own product interpretable.
- **The empirical error model** (§9) needs paired acoustic-and-truth rows; the fit's own
  uncertainties are ~5x overconfident.

**Built** as `propdop.associate` (plus `propdop.adsb` and `propdop.geo`). Two findings came out
of implementing it:

**Blade count is not in any standard aircraft table.** ICAO Doc 8643 encodes engine count and
type (`L1P`, `L2T`), not propeller blades — so the type code alone does *not* give RPM, and a
90 Hz tone is 2,700 RPM on a two-blade prop or 1,800 on a three-blade. But blade count is
**identifiable from the data**: across repeat passes of one type, only the correct integer puts
the implied RPM inside a plausible cruise band consistently, and the true value also minimises
the spread because a cruise setting is repeatable. `adsb.infer_blade_count` recovers 2, 3 and 4
blades from 14 passes with 0.4% frequency noise, and returns `None` rather than guessing when
the data cannot separate.

**The clock self-calibrates, as designed.** ADS-B supplies the true heard-CPA time, so the
systematic part of the residual *is* the node's clock offset. A deliberately injected 37 s
offset is recovered to within 4 s from two passes, with no GPS and no assumption that NTP
worked.

A middle tier is worth checking for: a 1090 MHz receiver also hears Mode S and Mode A/C
replies from aircraft with transponders but no ADS-B Out. Those carry **presence and altitude
but no position**, and altitude alone is a real constraint when combined with an acoustic CPA
distance. How cleanly `readsb` surfaces altitude-only targets is unresolved — worth a look at
its `aircraft.json` before assuming either way.

### 3.1a Path B's hard problem is ground sources, not tones

Finding a tone is easy; deciding it is an aircraft is not. `fit.plausible_aircraft` implements
three independent checks, each physical rather than tuned, and they classify 10/10 correctly
on synthetic cars, trucks, locomotives and aircraft from 20 to 130 m/s:

| Check | Threshold | Why |
|---|---|---|
| **Fit quality** | comb score ≥ 5.0, `ε` ≤ 0.5% | A ground source forced into a flyby model fits badly: aircraft scored comb 5.4-6.7 and `ε` ≤ 0.3%, mis-fits scored 1.4-2.2 and 1.9-3.9% |
| **Resolvability** | `d/v` ≥ 5 s | The sweep takes about `d/v`. A locomotive at 50 m doing 20 m/s completes it in 2.5 s — two or three STFT frames — and came back fitted at **101 m/s** |
| **Speed** | ≥ 35.8 m/s (80 mph) | Road and rail run below ~35 m/s; aircraft above ~40 m/s |

The speed gate only works because the fit's lower speed bound reaches 20 m/s, letting a slow
source be described correctly and then rejected on its true speed. A template bank starting at
40 m/s forces slow sources into the aircraft range instead — which is how a published
implementation reported a locomotive as a 140 m/s flyover.

**Site geometry does most of the work, though.** Ground sources sit at fixed, known distances
and bearings, and they propagate badly: an elevated aircraft has clear line of sight while a
ground-to-ground path loses an extra 10-20 dB to ground effect and shielding over a few
hundred metres. So an aircraft at 3 km is routinely louder than a train at 500 m. In the test
set, everything beyond ~200 m (truck at 200 m, locomotive at 300 m) was fitted *correctly* and
rejected on speed alone; only sources inside ~150 m needed the quality and resolvability
checks. **A site with nothing within a couple of hundred metres barely has this problem** — so
survey the site's fixed sources once rather than re-classifying every event.

### 3.1b How the blind detector works (built)

`propdop.detect`. The method rests on a change of variables. In log-frequency,

```
log f(t) = log f0  -  log(1 + (v/c)*sigma/sqrt(1+sigma^2))
(t_r - t0e)/(d/v) = sigma + (v/c)*sqrt(1+sigma^2)
```

so the curve **shape depends only on `v/c`**; `f0` is a pure vertical translation and `d/v` a
pure horizontal scale. Two things follow, and the second is the useful one:

1. A modest bank over (speed, `d/v`) — 35 templates — covers every geometry, and **one 2-D FFT
   cross-correlation per template scores all (`f0`, `t0`) at once.**
2. **The harmonic comb becomes a fixed vertical pattern**, since harmonic `k` sits at
   `log f0 + log k` whatever `f0` is. So the template is comb-matched for free: a candidate has
   to fit the whole comb, not one line.

The log-frequency map is normalised per frequency row and the kernels to unit norm, so the
correlation output is a scale-free matched-filter score. Peaks are refined by the comb fit and
screened by `plausible_aircraft`, then de-duplicated **again on the refined parameters** —
template coordinates are too coarse to merge reliably, which left two peaks per event.

**Measured on a 400 s synthetic scene** (one 90 m/s pass at 2 km, one road vehicle at 150 m,
one 130 m/s pass at 4 km, 12 dB SNR, pink wind noise, ~4 minutes of nothing): 19 raw candidates
→ 7 after merging → **exactly the 2 true aircraft accepted, zero false positives**, with the
vehicle detected but correctly classified as ground. Blind parameter recovery on the far pass:
speed 129.9 against 130, distance 3999 against 4000, `f0` 110.0 against 110.

**One gate had to be removed.** Comb contrast is a per-frame measure and therefore *less
sensitive than the matched filter that found the event* — the 4 km aircraft scored 110 on the
detector while its per-frame contrast sat at zero. Gating on it rejected both true aircraft.
It is still reported as a diagnostic, but the screen is fit residual, resolvability and speed.

**And the detection threshold had to be calibrated against a null.** The first version used a
matched-filter threshold of 4.0, chosen by eye, and 240 s of pure wind noise duly produced a
"73.9 m/s aircraft at 422 m" that passed every downstream gate. Measured properly: over
6 x 200 s of pink noise the template score peaks at **8.9** (p99 7.8, median 5.0), while real
passes from 800 m to 6 km at 0-20 dB SNR score **68-92**. The threshold is now 20, near the
geometric centre of that gap. The lesson generalises to every threshold in this project --
derive it from a null distribution, which the system produces for free.

### 3.2 Storage

| Stream | Rate | Per node per day |
|---|---|---|
| Raw 48 kHz/16-bit mono (transient ring buffer only) | 96 kB/s | not retained |
| Triggered clips, ~50 passes × 120 s @ 48 kHz | — | **~0.6 GB** |
| Decimated 24/7 archive @ 4 kHz (for blind search + reanalysis) | 8 kB/s | **~0.7 GB** |
| ADS-B local tracks | — | ~10–50 MB total |

So ~1.3 GB/node/day, under 0.5 TB/year for a three-node array. Trivial on any modest pool. Keep clips indefinitely — reanalysis as the DSP improves is the whole point.

### 3.3 Node software and transport: the server *pulls*

What runs on each acoustic node is deliberately minimal — a Pi in a weatherproof box should do as little
as possible:

- **Capture daemon** → fixed-size ring buffer on local disk (10 minutes at 48 kHz is ~58 MB, so this is
  free). Timestamp **at the capture boundary** from the audio device's own frame counter, not from
  wall-clock at write time, and record the nominal-vs-actual sample rate so §4's drift term is fittable.
- **Decimator** → rolling 4 kHz archive, shipped to the server on a lazy schedule.
- **A small HTTP endpoint** exposing "give me samples between t1 and t2".
- **chrony** against the server (which is itself NTP-synced).

**The server pulls windows rather than the node pushing a stream.** The acoustic arrival is at most
~20 s after the ADS-B fix at 7 km, so a 10-minute ring buffer is ample slack for the associator to
request the window it wants around a detection. Pulling means no continuous network load, nodes that tolerate WiFi dropouts (the buffer
covers the gap), and no server-side ingest of audio nobody will ever look at. The 24/7 decimated archive
is the only thing that ships unconditionally, at 8 kB/s.

A node that loses contact keeps recording and backfills when it returns, which matters for anything
mounted outdoors on WiFi.

### 3.4 Container layout

One compose project, with all persistent state bind-mounted so the image owns nothing:

| Service | Role | Notes |
|---|---|---|
| `propdop-web` | FastAPI + UI: live passes, profiles, health trends, the radar | port **8475** |
| `propdop-sched` | Watches ADS-B, predicts acoustic windows, pulls clips from nodes | the core loop (§3.1) |
| `propdop-dsp` | Whiten, ridge-trace, comb fit, per-pass inversion | CPU-bound; the GPU is almost certainly unnecessary |
| `propdop-db` | **TimescaleDB** — passes, metrics, atmos samples, profiles | time-series shaped, so Timescale over plain Postgres |
| `propdop-adsb` | `readsb` + `tar1090` for the local receiver | also serves `tar1090` for your own use |
| *(reuse)* `Redis` | Job queue | **already running on 6379** — no new container |

Note this is a much smaller system than the geolocation stack: no 4 GB/day archive fetcher, no
continent-scale candidate search, no GPU worker. Historical adsb.lol archives are only needed
for backfill or reanalysis of dates predating a local receiver.

## 4. Clock synchronisation — only if you add nodes

**Single-node deployments can skip this section.** Doppler fitting tolerates a loose clock: the fit
absorbs an offset, and ordinary NTP (tens of ms) is already far better than needed, since sound travels
only ~3 m in 10 ms against ranges of 1–6 km. One mic needs no special timing at all.

Multi-node TDOA is different, and it's the only real engineering risk in the optional path. Bearing error
is

```
σ_θ = σ_τ · c / (B · sin θ)
```

Computed, at a 500 m baseline, broadside:

| Sync method | offset | bearing error | verdict |
|---|---|---|---|
| **NTP over WiFi** | 5–20 ms | 0.20–0.79° | **usable** |
| NTP over wired LAN, chrony | 0.2–1 ms | 0.008–0.04° | excellent |
| PTP / IEEE-1588 | <0.05 ms | ~0° | excellent |
| GPS PPS per node | <0.001 ms | ~0° | excellent |

**This is the key design result, and it vindicates separating the nodes widely.** A long baseline is
forgiving: ordinary WiFi NTP at a 500 m baseline already gives sub-degree bearings, which at 3 km range
is a ~50 m position error. A tight array would need PTP or GPS; a wide one does not.

Two caveats that follow:

- **Sample-clock drift must be corrected.** Independent sound cards run ±10–100 ppm, which is 3 ms over
  60 s at 50 ppm — larger than the NTP offset. Timestamp at the capture boundary and fit a per-clip drift
  term, don't assume nominal sample rate.
- **Don't cross-correlate waveforms at a wide baseline.** The two nodes see different ranges, so different
  Doppler — the tones aren't at the same frequency — and turbulence decorrelates the wavefront over
  hundreds of metres anyway. Instead **fit each node's Doppler curve independently** and compare the
  resulting CPA times and ranges. More robust, and it works at any baseline.

### 4.1 The array self-calibrates from ADS-B

Because ADS-B gives the true 3D track and the node positions are surveyed, the expected TDOA is
*predictable*. The residual between predicted and measured TDOA **is** the inter-node clock offset. So
aircraft of opportunity continuously calibrate the array:

| TDOA noise | 1 pass | 10 passes | 100 passes |
|---|---|---|---|
| 1 ms | 1.0 ms | 0.32 ms | 0.10 ms |
| 5 ms | 5.0 ms | 1.58 ms | 0.50 ms |
| 20 ms | 20 ms | 6.3 ms | 2.0 ms |

A day's traffic drives the clock offset to well under a millisecond regardless of starting sync. GPS PPS
(~$15–25/node) is cheap insurance that removes the problem entirely and gives you node positions too, but
it isn't required to start.

## 5. Node count — one is the baseline, more is optional

### A single microphone is sufficient for the primary products

This matters, so it's worth being precise about *why*. The weak axis of single-mic acoustics is altitude
(~23 %, APPLICATIONS.md §2) — but **for any aircraft in ADS-B, you already know the altitude to ~10 m.**
Geometry is an input, not an output. So with one mic you get, at full quality:

- **Atmospheric soundings** (product #1) — complete. The inversion needs known geometry plus a measured
  curve, and one mic supplies the curve. The 127° bearing sweep within a single pass is what separates
  wind from temperature, and that happens at one microphone.
- **Propeller RPM and health** (part of #2) — complete. These come from `f0` and the harmonic comb, which
  are properties of the sound, not of the array.
- **Speed and slant range** — complete, and directly checkable against ADS-B.

So a one-node deployment is a real instrument, not a degraded one. Design every stage to run on a single
node and treat extra nodes as additional independent observations of the same pass.

### What extra nodes add, if you want them

| Nodes | Adds |
|---|---|
| **1** (baseline) | everything above. Altitude taken from ADS-B rather than measured |
| **2** (optional) | TDOA → *measured* altitude to tens of metres, independent of ADS-B. **But two mics give a *cone*, not a bearing** — a left/right mirror about the baseline axis remains, and there's *no* bearing sensitivity along the axis (endfire). Also a second atmospheric path per pass |
| **3**, non-collinear (optional) | full unambiguous 3D track with **no ADS-B at all**, which is what makes product #3 (non-cooperative aircraft) quantitative rather than just a flag |

The honest summary: extra nodes buy **independence from ADS-B** and horizontal atmospheric gradients.
They do not unlock the main products. If product #3 matters to you, three nodes is the configuration that
delivers it; otherwise one node and a good windscreen is the better use of effort.

If you do expand: recommended baseline **300 m – 2 km**. Longer is better for bearing resolution *and*
for sync tolerance (§4), and normally worse for wavefront coherence — but since nodes are fitted
independently rather than cross-correlated, coherence stops being the limit. Nodes at different places
also sample **different paths to the same aircraft**, giving horizontal gradients a single path can't.

## 6. Hardware

**Wind noise is the binding constraint, not microphone quality.** The outdoor-acoustics literature is
blunt about this: for outdoor measurement, capsule self-noise is negligible next to wind-induced noise,
and field trials in RESEARCH.md §2 failed on exactly this (a "wind-saturated microphone" at Duke Farms).
So budget effort accordingly:

- **Spend on wind treatment and siting.** Large foam windscreen minimum, basket/blimp better. **Mount
  low** — wind slows near the ground, which is standard practice in infrasound work. Consider a
  ground-plane mount facing up.
- **Don't overspend on the capsule.** An I2S MEMS mic (INMP441, SPH0645) wired straight to a Pi needs no
  sound card and is well documented. **Check its low-frequency corner**, though — many MEMS parts roll off
  around 50–100 Hz, and a 2-blade prop at 2,400 RPM sits at 80 Hz. A webcam stream high-passed at ~125 Hz
  was enough to sink one of the field tests.
- **Upgrade path** if fundamentals come out weak: a low-noise electret capsule with flat response to
  ~20 Hz into a USB audio interface.
- **Calibrate absolute level** only if you want the noise-monitoring or humidity-absorption products;
  Doppler, RPM and health all work off *frequency*, which needs no amplitude calibration at all.

Sample at 48 kHz (standard, cheap) even though the band of interest is ~20 Hz–2 kHz; it leaves the
humidity channel at 1.6 kHz and jet tones available without a re-deploy.

ADS-B: RTL-SDR dongle plus a 1090 MHz antenna, `readsb`/`tar1090` in a container on the server. Mount the
antenna as high as practical — your low-altitude coverage *is* the instrument's sensitivity floor.

### 6.1 Compute budget, and whether a Raspberry Pi is enough

Measured on one core of an Apple M2 Max, on a 420 s window at the 8 kHz archive rate — the exact
shape the pipeline analyses:

| Stage | Cost |
|---|---|
| Decimate 1 s of 48 kHz audio (the only *continuous* cost) | 0.93 ms = **0.09 % of real time** |
| Whitened spectrogram, 420 s | 0.95 s |
| Log-frequency map | 0.01 s |
| Build the 35-template bank | 0.03 s |
| Blind search — 35 FFT correlations over a 284 × 839 map | **1.14 s** |
| Comb-fit refinement | **2.5–5.2 s per candidate** |
| Whole window, quiet sky (3 candidates) | **16.8 s** |
| Whole window, busy sky (11 candidates) | **27.6 s** |

Two things follow. **The blind search is nearly free** — under 2 % of the window's cost — so
listening continuously is not what costs anything; the comb fit is, and it scales with *traffic*
rather than with time. And **capture is free**: 0.09 % of real time means the capture thread is
never the problem, which is why the GIL contention of §3.4 was fixed by moving analysis to a
subprocess rather than by making it cheaper.

**The Pi 4 figures below were scaled, and then measured. The scaling was pessimistic by about
1.5×.** Geekbench 6 single-core ratios (M2 Max ≈ 2759; Pi 4 ≈ 340) predicted ×8–11; the board
actually runs this workload at **×6.4** against one i7-13700K core, consistently across both the
FFT-bound correlation and the Python-bound refinement. Recorded here rather than edited away,
because the lesson is that Geekbench over-predicts the penalty for numpy/scipy work of this shape.

Measured on a Raspberry Pi 4 Model B Rev 1.4 (4 GB, Debian 13, Python 3.13.5 aarch64,
numpy 2.5.4 / scipy 1.18.1), on the same 420 s window at the 8 kHz archive rate:

| Stage | i7-13700K, 1 core | Pi 4, 1 core | ratio |
|---|---|---|---|
| Template correlation (every window pays this) | 0.91 s | **5.83 s** | ×6.4 |
| Refinement, per candidate | 2.26 s | **14.35 s** | ×6.3 |
| Whole window, 5 candidates | 12.20 s | **77.58 s** | ×6.4 |
| Duty cycle at a 300 s period | 4.1 % | **25.9 % of one core** | |

| Pi 4 worst case | Cost | Outcome |
|---|---|---|
| `max_detections=8` | 121 s | inside the period, comfortable |
| `max_detections=24` (default) | 350 s | over the 300 s period, still inside the 420 s window: an overrun, but no coverage gap |

So **a Pi 4 runs this at about a quarter of one core of four**, and the only configuration change
it actually needs is `--max-detections 8`, which bounds the saturated case well inside the period.

**Giving it more cores does nothing by itself.** Measured on the same board, raising the BLAS/FFT
thread count from 1 to 4 changed the window from 77.58 s to 77.25 s — 0.4 %, i.e. nothing.
`scipy.signal.fftconvolve` uses single-threaded pocketfft, and the refinement is GIL-bound Python
(the Viterbi ridge, the per-bin median), so thread-level parallelism has nothing to grip. The only
way to spend the other three cores is explicit process-level fan-out across candidates, which
would cut the saturated case to roughly a third. That is a real option, but capping
`max_detections` reaches the same place for free, so it is not implemented.
Nothing in the package touches CUDA, so moving off a GPU machine costs no capability.

Deployment needs no root and no system packages: numpy, scipy and fastapi all have aarch64
wheels for cp313, and `soundfile`'s wheel bundles libsndfile, so a plain userspace venv installs
with `--only-binary=:all:` and no compiler. `python:3.13-slim` is multi-arch if a container is
wanted instead, but on a dedicated board a venv plus a `systemd --user` unit is lighter and
avoids the `/dev/snd` and `group_add` handling the compose file needs.

Memory is the tighter budget: 173 MB of standing ring buffers (115 MB live at 48 kHz + 58 MB
archive at 8 kHz, both float32) + ~101 MB parent interpreter + **486 MB peak in the analysis
child** + ~150 MB for the web container ≈ **900 MB peak**. Comfortable on 4 GB, tight on 2 GB.

So the processor is not the binding constraint. These are the real ones, in order:

1. **Analysis cadence, not throughput.** `analysis_period_s` is a *start-to-start* period. It was
   originally a wait *after* each run, making the true cadence `duration + period` — invisible at
   17 s per window, but at a Pi 4's measured 78 s (and 350 s saturated) it can push the cadence
   past the 420 s window and
   stretches of audio go unanalysed. An unanalysed stretch leaves **no row at all**, not even an
   `unheard` one, so it cannot be recovered or even noticed after the fact. The loop now subtracts
   the elapsed time and counts both overruns and coverage gaps, and the dashboard says so.
2. **USB bus contention.** An RTL-SDR at 2.4 Msps alongside a USB sound card on one host controller
   drops samples — a well-known Pi 4 problem, better but not absent on a Pi 5. Separate the buses,
   and budget roughly another 40 % of a core for `readsb` decoding. **Pulling ADS-B from a network
   feed instead removes both costs**, which is what makes a Pi 4 viable.
3. **Storage.** SQLite in WAL mode plus a 1 GB rolling corpus will wear out a microSD. Use a USB
   SSD, or NVMe on a Pi 5.
4. **Thermals.** A Pi 5 under sustained load wants the active cooler.

Clock accuracy is *not* on this list: `node.ClockModel` measures the sound card's true rate from
the capture timeline (the deployed card reads 3,687 ppm fast, fitted to a 1.2 ms residual over
180 s), and §4.1 recovers the absolute offset from ADS-B. A board with no RTC is fine.

A Pi used as a **remote microphone node only** — capture, decimate, ship — is trivially within
reach at 0.09 % of real time, with no analysis on the board at all. That is the NETWORK.md
topology, and it is the use a Pi is unambiguously suited to.

## 7. Per-pass pipeline

1. **Predict.** From the ADS-B track and each node position, solve the retarded time for the acoustic
   window at that node.
2. **Extract.** Cut the clip per node from the ring buffer; record the exact capture timestamps.
3. **Measure.** Whitened log-frequency STFT → ridge trace → **harmonic-comb fit** (shared with
   RESEARCH.md §4.2 item 1) → `f0(t)`, `v`, `d`, `t0r`, plus the fit residual. **Record `ε`** (residual
   as a fraction of `f0`) on every pass — it is the master precision parameter (§2) and its distribution
   over time is how you tell whether the installation is healthy.
4. **Aircraft products.** `RPM = f0 · 60 / N_blades` (blade count from the ADS-B type code);
   speed and range from the fit; altitude from TDOA if ≥2 nodes.
5. **Health.** Test for energy at shaft-rotation harmonics that are *not* BPF multiples — the patent
   signature in APPLICATIONS.md §3. Gate on steady-state flight.
6. **Atmosphere.** With geometry fixed by ADS-B, fit `c_eff` along each path. Decompose the isotropic part
   (temperature) from the `cos(θ−θ_w)` part (wind) across the pass's 127° bearing sweep. A single pass is
   **not** a usable sounding (§2) — it's one sample.
7. **Store** a pass record plus one atmospheric sample per node-path, carrying its `ε`, its geometry and
   its **empirical** weight (§9 — not the fit's own covariance).

Then, as a batch job: accumulate samples by altitude and bearing and run a **regularised layer inversion**
for the vertical profile (~1–2 km layers, 0–7 km), weighting by aircraft speed as well as `ε` since fast
aircraft carry ~3× the information. Report **wind first**; temperature only once the pass count in a layer
justifies it. Every published profile should carry its contributing pass count, because that is what sets
whether it means anything.

## 8. Data products

| Table | Contents |
|---|---|
| `passes` | time, icao24, registration, type, ADS-B geometry, per-node fit params + residuals |
| `aircraft_metrics` | derived speed, altitude, heading, RPM, advance ratio |
| `health_history` | per tail number over time — the point is *trend*, not absolute |
| `atmos_samples` | per node-path: `c_eff`, inferred T and wind, path geometry, weights |
| `atmos_profiles` | gridded layer solutions with uncertainties |
| `unexplained` | detections with no matching ADS-B track |

Time-series shaped, so TimescaleDB (Postgres extension) fits your existing `*-db` convention better than
plain Postgres.

## 9. Validation — free, continuous, and non-negotiable

Every product has an independent reference available at no cost, so build the comparison in from the start:

- **Aircraft speed, altitude, heading** → ADS-B itself. Direct, per pass.
- **Winds and temperature aloft** → NOAA GFS/HRRR, and Iowa Environmental Mesonet radiosondes (twice
  daily). The benchmark to beat is "agrees with HRRR in the boundary layer, where HRRR is weakest."
- **Surface wind** → nearest ASOS, via IEM.
- **RPM** → repeat passes of the same tail number should give consistent cruise RPM.
- **Health** → trend stability per tail number; a step change is the signal.

A single-pass agreement proves nothing; a distribution over hundreds of passes does. Plot residual versus
range, altitude, bearing and time of day — the systematics in §2 will show up as structure there.

### Derive the error bars empirically — do not trust the fit's own

Non-negotiable, because it's measured: on the Zenodo set the curve fit reports a median slant-range
uncertainty of **3.8 %** while actually being **17.2 %** off — **overconfident by ~5×** (APPLICATIONS.md
§2). Propagating the fit covariance into the atmospheric inversion would therefore understate the layer
uncertainties by roughly the same factor, and the inversion would mis-weight passes against each other.

So: fit an **empirical error model** against ADS-B truth — residual as a function of SNR, range, speed and
manoeuvring — and use *that* to weight the inversion. You have continuous ground truth for exactly this,
which is the central advantage of the whole design, so use it for the error model and not only for the
headline accuracy numbers.

This also means the averaging tables in §2 and APPLICATIONS.md §6 are **best cases assuming independent
errors**. If a systematic (an RPM-wander bias, a siting-dependent ground reflection, a diurnal
refraction effect) is common across passes, averaging will plateau. Watch for that plateau explicitly as
pass count grows; it is the single most likely way this project quietly produces confident nonsense.

## 10. Build order

0. **Check for Mode-S meteorological replies before anything else.** See
   [PRIOR-ART.md §1](PRIOR-ART.md). Aircraft already report wind and temperature aloft over the *same
   1090 MHz radio*, via Mode-S EHS (BDS 5,0/6,0) and MRAR (BDS 4,4) — operationally assimilated into NWP
   in Europe, at quality comparable to AMDAR, and better than acoustics can manage for temperature. But
   those registers are *replies to ground radar interrogation*, not spontaneous broadcasts, and whether US
   radars interrogate them at a useful rate is **unresolved**.

   Decode an evening's traffic with `pyModeS` and count BDS 4,4 / 5,0 / 6,0 replies. If they're there in
   volume, the atmospheric product is largely redundant and the project should refocus on what no radio
   can give — RPM, prop health, non-cooperative detection. If they're absent, that absence is the gap this
   project fills, and it becomes the central justification. One evening, and it decides what to build.
1. **ADS-B node, then a feasibility check before buying a microphone.** RTL-SDR + antenna +
   `readsb` container. It's independently useful, it starts accumulating local coverage immediately, and
   every later step depends on it.

   Then spend a week answering the question that decides whether the atmospheric product exists at all:
   **what is the pass density by altitude band at this site, weighted by aircraft speed?** §2 says a
   1–2 km layer needs ~20 good passes to be worth reporting, and fast aircraft carry ~3× the information
   of slow ones. This is computable from the ADS-B log alone with no microphone, and it tells you the
   achievable cadence (hourly? daily? not at all?) per layer. Four of ten published field sites failed on
   sparse traffic — find out which kind of site you have before spending money or effort on acoustics.
2. **One acoustic node**, with the windscreen taken seriously. Prove you can see a blade-pass tone and its
   comb from a known overflight, and **measure `ε`** — the fractional frequency precision of the fit. Per
   §2 this is the master parameter, with required pass counts scaling as `ε²`, so measure it early and
   treat improving it (windscreen, siting, capsule) as the main optimisation target.
3. **Continuous detection + the ADS-B join.** Not triggered capture. At this point you have the auto-labelled
   corpus, which is the asset everything else is built on.
4. **Comb fit → speed, range, RPM.** Validate speed and range against ADS-B over a few hundred passes.
   Nothing else should start until this residual distribution looks sane.
5. **Atmospheric inversion**, single node, path-averaged — **wind first**, since per §2 it's the far
   stronger product; temperature second. Then compare to HRRR and radiosondes.
6. **Prop health**, which needs the longest baseline of repeat passes to mean anything.
7. **Layer profile inversion** once sample density by altitude justifies it.
8. *(Optional)* **Second and third nodes** → TDOA, measured altitude, full 3D, and quantitative
   non-cooperative detection. Deliberately last: steps 1–7 are a complete instrument without it, and the
   clock-sync work in §4 is only worth doing once the single-node pipeline is proven.

Steps 1–4 are the foundation and are worth doing carefully; everything after is incremental. Nothing in
1–7 requires more than one microphone.

## 11. Scope note

This points microphones at aircraft. Aircraft positions are public and ADS-B is broadcast in the clear, so
this is a very different posture from locating a person's recording — but the array also hears the ground
around it, so keep retention policies pointed at the sky and be deliberate about what the 24/7 decimated
archive keeps.
