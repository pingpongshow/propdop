# Prior art, and would researchers care?

Researched 2026-11-07. Short answers:

- **Yes, substantially.** The acoustic method is published, validated science — done with UAVs, not
  aircraft of opportunity. And the *atmospheric* product has a mature operational incumbent that needs no
  microphone at all.
- **Possibly useful to researchers, as a methods contribution**, not as an operational data source. But
  there is one test to run first that could redirect the whole project, in §1.

---

## 1. The incumbent that matters: Mode-S derived winds and temperature

**This is the finding that most affects the project.** Aircraft already report winds and temperature aloft
over the *same 1090 MHz radio* your ADS-B receiver listens to — no microphone, no Doppler, no acoustics.

- **Mode-S EHS** registers BDS 5,0 and 6,0 give true airspeed/heading and ground speed/track; differencing
  them yields the **wind vector**, and temperature follows from Mach and TAS.
- **Mode-S MRAR** (BDS 4,4) is a *directly measured* wind and temperature report.
- This is **operational, not experimental.** EUMETNET runs
  [EMADDC](https://amt.copernicus.org/articles/18/3341/2025/) on it at volume; the Met Office assimilates
  Mode-S EHS winds and EMADDC temperatures into the UKV; KNMI and the ALADIN consortium have published
  NWP impact studies. MRAR quality is reported as *comparable to AMDAR*.
- And it's been done on hobby hardware: KNMI published **"The use of a commercial ADS-B receiver to derive
  upper air wind and temperature observations from Mode-S EHS information in The Netherlands"** in 2013,
  plus *"A Network of Mode-S Receivers for Routine Acquisition of Aircraft-Derived Meteorological Data"*
  (J. Atmos. Oceanic Technol., 2016).

Accuracy comparison that matters:

| Source | Wind | Temperature |
|---|---|---|
| Radiosonde | ~0.5–1 m/s | ~0.2–0.5 °C |
| AMDAR / Mode-S MRAR | ~1–2 m/s | ~0.5–1 °C |
| Mode-S EHS derived | similar to AMDAR once bias-corrected | noisier; usable when aggregated |
| **PropDop acoustic (per pass)** | **~1 m/s** | **~1.7 °C** |

So acoustics is roughly *competitive on wind* and *clearly worse on temperature* than something you can
get by decoding a different field in the same radio stream.

### The one test to run before building anything

**Mode-S EHS data are interrogation-dependent.** BDS registers are *replies* to ground secondary
surveillance radar interrogation — they aren't broadcast spontaneously like ADS-B. Europe mandates
Mode-S EHS, so nearly all aircraft there are interrogated every 4–20 s and a passive receiver harvests the
replies. **I could not establish from the literature whether US radars interrogate BDS 4,4 / 5,0 / 6,0 at
any useful rate**, and US surveillance is built around 1090ES ADS-B rather than the European EHS mandate.

So: **point your RTL-SDR at the sky for an evening and count BDS 4,4 / 5,0 / 6,0 replies.** `pyModeS`
(Junzi Sun) decodes all of these and is the standard tool.

- **If you see them in useful volume:** you have winds and temperature aloft directly, better than
  acoustics, for the cost of a dongle. PropDop's atmospheric product becomes redundant — and its honest
  purpose narrows to the acoustic-only products (RPM, prop health, non-cooperative detection), which no
  radio can provide.
- **If you don't** — which I'd weakly expect in US airspace — that absence *is* the gap PropDop fills, and
  it becomes the project's central justification. It also makes the project much more interesting to
  researchers, because the US boundary layer would be a genuine Mode-S blind spot.

Either way this is an evening's work and it determines what the project is for. It belongs before the
microphone purchase.

## 2. The acoustic method is published — with UAVs

The Doppler-to-sound-speed inversion that [APPLICATIONS.md §6](APPLICATIONS.md) derives is **not novel**.
A group at the University of South Australia (Finn, Rogers and colleagues) built it out across roughly
2013–2019:

| Work | Venue |
|---|---|
| Three-Dimensional UAV-Based Atmospheric Tomography | J. Atmos. Oceanic Technol. 30(2), 2013 |
| The feasibility of unmanned aerial vehicle-based acoustic atmospheric tomography | JASA 138(2), 2015 |
| Improving UAV-Based Acoustic Atmospheric Tomography | J. Atmos. Oceanic Technol. 33(4), 2016 |
| Acoustic atmospheric tomography using multiple UAVs | IET Radar, Sonar & Nav., 2016 |
| Preliminary evaluation of temperature and wind profiles from UAV-based acoustic tomography | ISPRS Archives, 2019 |

Their method, in their own framing: compare the **projected versus observed Doppler shift of the engine's
narrowband tones** measured by ground microphones, convert to **effective sound speed**, then invert
tomographically for 2D/3D temperature and wind fields. That is precisely PropDop's physics. They validated
against a ZephIR lidar, over roughly **120 m of altitude across a 300 × 300 m area**.

Adjacent published work:

- **Passive acoustic measurements of wind velocity and sound speed in air** (JASA 135(2), 2014) — ambient
  noise cross-correlation between microphone pairs, no cooperative source at all.
- **Infrasound**: stratospheric temperature and wind inferred from microbarom cross-correlation; the
  CTBTO/IMS "atmospheric specification" literature is a mature version of this idea at 0–60 km.
- **Radio analogue, same "aircraft of opportunity" concept**: *Remote sensing of atmospheric refractivity
  using ADS-B interferometry (ADSBi)*, arXiv 2608.21108 (2026), and *Ionospheric tomography using ADS-B
  signals* (Radio Science).

### So what *is* novel here

Narrower than I previously implied. Searching specifically for commercial/GA aircraft noise + ADS-B +
atmospheric sounding turned up **nothing**. The novel combination is:

**Uncontrolled aircraft of opportunity, positioned by ADS-B, at kilometre altitudes.**

That's a real contribution to the Finn/Rogers line, and the altitude range is the headline: **~7 km versus
their ~120 m, a factor of ~50.** Plus free, continuous, unattended operation.

The trade is equally real, and explains why nobody has done it: a UAV gives you control of the flight
geometry (you can fly an optimal tomographic pattern), a stable commanded RPM, an onboard reference
microphone, and repeatability. Aircraft of opportunity give you none of those — which is exactly why `ε`
and pass density ([DESIGN.md §2](DESIGN.md)) dominate the error budget here and not in their work.

## 3. The auto-labelling idea already exists as a dataset

I earlier called ADS-B-triggered auto-labelled audio collection the project's big unlock. It's published:

**AeroSonicDB (YPAD-0523)** — arXiv [2311.06368](https://arxiv.org/abs/2311.06368), Nov 2023. Explicitly
"exploiting ADS-B radio transmissions to passively collect and label audio samples." Contents: **625
aircraft recordings** (18–60 s each, 8.87 h total), **301 unique aircraft**, **14 non-acoustic labels per
sample**, plus 3.52 h of ambient background and 6 h of annotated urban soundscape.

Two implications:

- The mechanism is not novel. Correct my earlier claim.
- **But it's a free, ready-made corpus** that is far better than the 40-case Zenodo set for detector work.
  Caveat: it was built for *classification*, so it may lack the timing precision and tonal SNR that
  Doppler fitting needs — evaluate it, don't assume it. If its clips do support a comb fit, it's an
  immediate `ε` measurement without deploying anything.

Also in this space: *Acoustic system for Low Flying Aircraft detection* (Stevens Institute), *Aircraft
tracking by means of the Acoustical Doppler Effect* (Aerospace Science and Technology, 2012), patent
US20210225182A1 (acoustic detection and avoidance combining ADS-B with ground microphone arrays), the
propeller-health patent family in APPLICATIONS.md §3, and a substantial commercial counter-UAS acoustic
sector.

**No open-source project combining microphones with ADS-B for monitoring turned up.** The code space is
genuinely empty even though the research space isn't.

## 4. Would aviation-weather and forecasting researchers care?

### The gap is real and documented

- **COST Action PROBE** (CA18235, *Profiling the Atmospheric Boundary Layer at European Scale*): the ABL
  observational gap "slows progress in NWP, air quality forecasting and climate assessment."
- **"Confronting the boundary layer data gap"** (Atmos. Meas. Tech. 13, 2020).
- **BAMS 101(7), 2020**: current systems "fail to provide a satisfactory amount of spatially and
  temporally resolved observations of temperature and humidity in the planetary boundary layer" — and,
  usefully for a single-site instrument, **"given the sparse current observation network, even single
  additional stations can improve regional, high-resolution NWP."**
- A 2026 PBL remote-sensing review notes persistent gaps over oceans, remote land and polar regions.

### But read PROBE's own diagnosis carefully

PROBE's framing is that the gap persists **"less from missing instruments than from missing
coordination"** — what's absent is "harmonization of data and procedures." That argues *against* a novel
bespoke instrument being what the field wants, and *for* standardisation of what already exists. Worth
knowing before pitching this as filling a need.

### Where researchers would plausibly be interested

1. **If the US Mode-S blind spot is real (§1)** — a passive, ~$100, unattended wind profiler reaching
   ~7 km in airspace where Mode-S meteorological harvesting doesn't work would be a genuinely interesting
   proposition.
2. **Path-integrated observables are complementary.** Radiosondes and AMDAR/Mode-S give point/in-situ
   measurements; acoustics gives line integrals along slant paths. That's the natural input to
   tomography, and it's a different error structure — which is what data assimilation people actually
   care about.
3. **Extending UAV acoustic tomography to altitude** is a clean methods contribution to an existing,
   active literature with established inversion machinery.
4. **Wind, not temperature.** Lead with the wind product; the temperature figures won't impress anyone
   next to AMDAR.

### Where they wouldn't

- Temperature at ~1.7 °C/pass is worse than every incumbent.
- Cadence is hostage to local traffic, and aggregating over hours erodes the one advantage (time
  resolution) you'd be claiming.
- Coarse 1–2 km layers aren't a profile.
- Systematics are unmeasured, so nothing is assimilable until validated against a co-located profiler.

### Honest positioning

This is a **methods/instrument paper**, not an operational data source — target JTECH, AMT or JASA, the
same venues as the UAV tomography work, framed as extending it to uncontrolled sources at altitude. The
credible path is to validate against a co-located lidar or radiosonde site and publish the comparison.
Approaching the Finn/Rogers group (or whoever now carries that line) as a collaboration would be sensible:
they have the inversion machinery and the validation experience, and the altitude extension is the thing
they can't easily get.

## 5. Honest ranking of the four products

"Atmosphere is weaker than engine health" is directionally right **on novelty**, but it bundles a strong
product with a weak one on each side. Separated out:

| Product | Incumbent? | Per-pass quality | Validatable? | Verdict |
|---|---|---|---|---|
| **Propeller RPM** | **None** — no radio field carries RPM | Precision is **exactly `ε`**: ±5 to ±22 RPM at 2,700 | Yes — repeat-pass consistency per tail number | **Strongest overall** |
| **Prop/engine health** | **None** — tomography literature doesn't touch condition monitoring | Detection, not a number | **Barely** — see below | Highest novelty, weakest evidence |
| **Wind aloft** | **Yes, if locally harvestable** — Mode-S EHS/MRAR (§1) | ~1 m/s, competitive with AMDAR | Yes — HRRR, radiosondes, ASOS, continuously and free | Moderate; hinges on step 0 |
| **Temperature aloft** | Same, and better than acoustics | ~1.7 °C, worse than every incumbent | Yes, same as wind | **Weakest** |

### The asymmetry that complicates the ranking: ground truth

This cuts the other way, and it matters for anything intended as research.

**The atmospheric products have free, continuous, independent validation** — HRRR, radiosondes, ASOS. You
can quantify your error against a reference every single day, which is what makes a methods paper
possible at all.

**The health product has essentially no ground truth.** Nobody tells you a given propeller was damaged.
Maintenance records aren't accessible, and genuinely damaged props are rare in the overflying population.
What you *can* validate is weaker:

- **The null hypothesis** — healthy props should show *no* rotation-harmonic energy. If most aircraft
  show it, the detector is broken. This is a real and useful test.
- **Trend stability** per tail number, where a step change is the signal.

But you cannot show a true-positive rate without a known-damaged aircraft. So "health" is better described
as **anomaly detection against a per-aircraft baseline** than as diagnosis — and that is a much harder
thing to publish or to act on.

### One physical point in health's favour

The damage signature sits at shaft-rate harmonics that aren't blade-pass multiples — for a 2-blade prop at
2,700 RPM, **45 Hz and 135 Hz**, i.e. *below* the BPF. Absorption there is the lowest in the whole band:

| Tone | dB/km | loss over 3 km | over 6 km |
|---|---|---|---|
| **45 Hz** (damage signature) | 0.054 | **0.16 dB** | 0.33 dB |
| 90 Hz (BPF fundamental) | 0.207 | 0.62 dB | 1.24 dB |
| **135 Hz** (damage signature) | 0.431 | 1.29 dB | 2.58 dB |
| 360 Hz (BPF 4th harmonic) | 1.736 | 5.21 dB | 10.4 dB |

So the damage signature **propagates better than the tones used for Doppler fitting** — it is not
range-limited by absorption.

The counterpoint, which is why this needs measuring rather than assuming: **wind noise is
low-frequency-dominated and rises steeply toward 45 Hz**, so the signature lands in the worst part of the
noise spectrum even though it suffers the least absorption. Whether it is detectable at 1–6 km is an
empirical question about `ε` and your windscreen, not one the physics settles.

### A possibly stronger research angle that isn't weather

**Propeller condition monitoring at fleet scale.** Nobody appears to hold a dataset of thousands of
propeller acoustic signatures, auto-labelled by tail number via ADS-B, tracked over months. The health
signature is well-posed (rotation-rate harmonics that aren't blade-pass multiples — APPLICATIONS.md §3),
RPM precision is **exactly `ε`** so ±5 to ±22 RPM at 2,700, and there's a clear application in
airworthiness and fleet health. Here the incumbent problem reverses: **no radio field carries RPM**, and
the UAV tomography literature doesn't touch condition monitoring. If the Mode-S test in §1 comes back
positive and the atmospheric case weakens, this is where the project's novelty actually lives.
