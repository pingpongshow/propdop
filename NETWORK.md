# Sharing reports between stations

What a network of PropDop stations measures that a single station structurally cannot.
Computed 2026-11-07, building on [APPLICATIONS.md](APPLICATIONS.md) and
[SCINTILLATION.md](SCINTILLATION.md).

---

## The one structural asset

A single station measures **path-averaged** quantities along one slant path per pass, and all
its paths converge on one point. No amount of data fixes that: you cannot recover a *field*
from rays that all meet at the receiver.

N stations give **N independent paths to the same aircraft at the same instant** — the same
source, the same engine setting, the same moment of atmosphere. Those rays cross. Everything
below follows from that.

---

## 1. Acoustic tomography: a 3-D field instead of path averages

The headline, and the natural extension of the single-station work.

Each (station, aircraft) pair contributes one path integral of `c_eff` along a known slant
path. Many such integrals crossing a volume invert for the **3-D temperature and wind field**.
This is exactly the method the UAV-tomography literature established
([PRIOR-ART.md §2](PRIOR-ART.md)) — Finn and Rogers reconstructed 2-D and 3-D fields from
travel times and Doppler between a drone and ground microphones — but over a **300 × 300 m area
at ~120 m altitude**.

A network using aircraft of opportunity would run the same inversion over **tens of kilometres
horizontally and 0–7 km vertically**. The altitude extension was already the contribution
([PRIOR-ART.md §2](PRIOR-ART.md)); adding crossing rays is what turns path averages into a
field, and it is the part no single station can ever reach.

## 2. Horizontal gradients, which one station cannot see at all

The simplest version of the above, and worth stating numerically. Two stations watching one
aircraft give two path averages; their difference over the station baseline is a horizontal
gradient.

With `σ_c = 0.4 m/s` per pass (the best demonstrated `ε` on a fast aircraft), the difference
carries `σ = 0.57 m/s`:

| Station baseline | 1 pass | 20 passes | 200 passes |
|---|---|---|---|
| 5 km | 11.3 | 2.5 | 0.80 m/s per 100 km |
| 10 km | 5.7 | 1.3 | 0.40 |
| **20 km** | **2.8** | **0.63** | 0.20 |
| 50 km | 1.1 | 0.25 | 0.08 |

Geostrophic wind gradients run 1–10 m/s per 100 km, so a 20 km baseline with a day's traffic is
genuinely useful — and consistent with everything else here needing averaging rather than
single passes.

## 3. MLAT closes the transponder-only gap

The most immediately practical win, and it needs no acoustics at all.

[DESIGN.md §3.1](DESIGN.md) flagged a middle tier: aircraft with a Mode S transponder but no
ADS-B Out broadcast **presence and altitude but no position**, so a single receiver cannot use
them. Three or more networked 1090 MHz receivers can **multilaterate** those replies — mature,
widely deployed technique (`mlat-client` feeds it routinely).

That converts a large slice of traffic from "non-cooperative, geometry unknown" into **fully
usable passes with known geometry**: RPM, atmospherics and the error model all become available
for them. Given that ADS-B Out is not required in Class G below 10,000 ft, this may recover a
substantial fraction of exactly the low, slow propeller traffic the instrument wants.

## 4. Regional 3-D tracking of non-transmitters

Each station reports a fitted slant distance; shared, those trilaterate. Using the measured
12–25% range error and rough GDOP:

| Aircraft range | Stations | Position error |
|---|---|---|
| 2 km | 3 / 5 / 10 | 300 / 173 / 106 m |
| 5 km | 3 / 5 / 10 | 1000 / 577 / 354 m |

Far worse than ADS-B's ~10 m — but for an aircraft that transmits nothing, and that MLAT also
cannot see because it carries no transponder, this is the only source there is. It turns
product #3 from a per-site flag into a regional capability.

## 5. Directivity measured at a single engine setting

[EXPANSION.md §2](EXPANSION.md) noted that one pass samples a *slice* of an aircraft's
directivity pattern, and that no open dataset pairs ADS-B tracks with measured GA directivity.

A network samples **N points on the directivity sphere simultaneously** — same RPM, same
configuration, same atmosphere, differing only in bearing and elevation angle. That is a much
cleaner measurement than accumulating single-station passes over months, where power setting
and conditions vary between samples. With calibrated microphones it would produce measured 3-D
directivity per type, which does not currently exist publicly.

## 6. Turbulence: a second, independent observable

Single-station scintillation gives `C²_eff` from log-amplitude *variance*
([SCINTILLATION.md](SCINTILLATION.md)).

Two stations watching the same tone give something else: the **transverse coherence** of their
amplitude fluctuations. Paths that share eddies fluctuate together; paths that do not,
decorrelate. The decorrelation distance measures the turbulence **outer scale / coherence
length** — the classic scintillometry observable, and strictly unavailable to one receiver.

That matters because it breaks a degeneracy. Variance alone constrains `C²_eff` given an
assumed outer scale (`c_eff_sq_from_fluctuations` takes `outer_scale_m = 100` as a *guess*).
Coherence measures the outer scale directly, so the two together determine both.

## 7. Health cross-validation replaces the missing ground truth

[PRIOR-ART.md §5](PRIOR-ART.md) rated prop health "highest novelty, weakest evidence", because
nobody tells you a propeller was damaged and you cannot establish a true-positive rate.

A network supplies the next best thing: **N independent observers of one pass.** A
rotation-harmonic signature seen by every station is in the engine; one seen by a single station
is propagation, local noise, or that station's microphone. That converts an unvalidatable
product into a self-consistent one — not proof of damage, but a defensible anomaly detector.

## 8. A real error budget

Each station's empirical error model can be compared against the others on *shared* passes,
which decomposes the error into **per-station systematic, network-common, and random** terms.
A station whose residuals are anomalous is miscalibrated — and that is only detectable by
comparison. Single-station work cannot separate "my site is odd" from "the method is
imprecise."

Also: propagation and ducting climatology becomes regional. An aircraft heard anomalously far
at one station and not others is a ducting event, localised.

---

## 9. The elegant part: ADS-B is the network's clock

No inter-station synchronisation protocol is needed, for two independent reasons.

**Wide baselines are forgiving.** Bearing error is `σ_τ·c/(B·sinθ)`, so:

| Baseline | 1 ms | 20 ms | 100 ms | 1 s |
|---|---|---|---|---|
| 0.5 km | 0.04° | 0.79° | 3.9° | 39° |
| 5 km | 0.004° | 0.08° | 0.39° | 3.9° |
| **20 km** | 0.001° | 0.02° | 0.10° | **0.98°** |

At 20 km, a **full second** of clock error costs under a degree. Plain NTP is already overkill;
the tight-array sync worries of [DESIGN.md §4](DESIGN.md) simply do not apply at network scale.

**And every station self-calibrates to the same reference anyway.** `associate` already
recovers each node's clock offset from ADS-B heard-CPA times — verified recovering a 37 s
offset to within 4 s from two passes. Since every station calibrates against the *same* ADS-B
timebase, **they land on a common clock for free**, with no protocol, no GPS and no trust
between stations.

## 10. What has to be shared

The derived tracks are the product; audio never moves.

| Shared once, per station | Shared per pass (~15 kB) |
|---|---|
| surveyed position, **geoid offset**, elevation | `icao24`, heard-CPA time, ADS-B geometry |
| microphone type, calibration, response | fitted `f0`, speed, slant distance, `t0` |
| windscreen and siting, **ground-source survey** | `ε`, comb contrast, per-harmonic amplitude tracks |
| inferred blade counts per type | per-harmonic noise bias, surface met |

**Negative detections must be shared too, and are as valuable as positive ones.** Detection
probability, propagation conditions and the whole non-cooperative product depend on knowing who
*didn't* hear something. A network that only publishes hits cannot compute its own reliability.

Cost is negligible:

| Network | Volume |
|---|---|
| 3 stations × 150 passes/day | 6.8 MB/day — 2.5 GB/yr |
| 10 stations × 200 passes/day | 30 MB/day — 11 GB/yr |
| 50 stations × 200 passes/day | 150 MB/day — 55 GB/yr |

## 11. Honest limits

- **Ray coverage follows airways.** Aircraft do not fly where a tomographer would want them, so
  crossing-ray density is highly non-uniform and resolution will be anisotropic and patchy.
  Mapping the actual ray coverage from ADS-B alone, before building any inversion, is the
  cheap first step — the same logic as the pass-density check in [BUILD.md](BUILD.md).
- **Still capped at ~7 km**, because props remain the only usable source.
- **Systematics may be network-common.** Stations in one region share weather, so a
  refraction or diurnal bias will not average down across stations any more than across passes.
  Comparison detects *differential* error, not common error.
- **Everything still needs averaging.** A network multiplies paths; it does not improve the
  ~1 m/s and ~1.7 °C per-pass precision.
- **Site heterogeneity is the practical obstacle.** Unequal microphones, windscreens and ground
  impedance will dominate the differential signal unless calibration is taken seriously. The
  shared-once table above is not paperwork; it is the measurement.

## 12. Why this is the version researchers would care about

[PRIOR-ART.md §4](PRIOR-ART.md) found the boundary-layer observation gap is real but that COST
Action PROBE diagnoses it as persisting **"less from missing instruments than from missing
coordination"** — what is absent is harmonisation. A single clever instrument does not answer
that critique.

**An open node design plus a published report schema does.** Ten deployed stations sharing a
documented format are worth far more, scientifically, than one very good site — and the
tomographic products in §1, §2 and §6 are the ones that do not exist at all today in this
altitude band. If any part of this project is worth writing up, it is this.

So: build the single station first and prove it ([BUILD.md](BUILD.md)), but **design the report
schema as though it will be shared**, because retrofitting that is far harder than getting it
right while there is one station to change.
