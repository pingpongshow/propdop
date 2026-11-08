# Inverting the problem: what else the sound tells you

Companion to [RESEARCH.md](RESEARCH.md) and [DESIGN.md](DESIGN.md).

Geolocation solves for the **listener** given known aircraft. Flip it: fix the listener, and the same
Doppler curve solves for the **aircraft**. Couple that with a local ADS-B receiver and you get continuous
ground truth, which turns a one-off analysis tool into an instrument.

---

## 1. What the physics hands you directly

From one fitted flyover curve:

| Quantity | Comes from | Notes |
|---|---|---|
| `f0` rest frequency | the asymptotes of the sweep | **Not in ADS-B.** → RPM, see §3 |
| `v` speed | total pitch drop, `f0·c/(c−v) → f0·c/(c+v)` | ADS-B has this, so it's a *check* |
| `d` CPA slant distance | steepness at CPA, `≈ f0·v²/(c·d)` | ADS-B has position, so again a check |
| `t0r` heard CPA time | curve centre | ADS-B has this → gives **clock/latency**, §9 |
| harmonic comb structure | ratios of simultaneous ridges | **Not in ADS-B.** → blade count, engine count, health, §3–5 |
| residual after fitting a *known* track | everything the model got wrong | → **wind and temperature aloft**, §6 |

The two rows that aren't in ADS-B are where all the value is. Everything ADS-B already broadcasts is
useful only as validation — which is exactly what makes a local receiver so powerful.

## 2. How well does single-microphone inversion actually work?

Not a guess — computed from the companion results table of
[Zenodo 23424278](https://zenodo.org/records/23424278) (*"Aircraft speed and height from Doppler-shifted
sound and flight-track data"*, Pathak 2026), 40 smartphone recordings at Livermore CA scored against
their own ADS-B tracks. Method I is a simpler endpoint/band estimate; Method II is the full curve fit.

| | median \|err\| | p75 | worst |
|---|---|---|---|
| **Speed**, Method I | 17.9 % | 26.6 % | 47.3 % |
| **Speed**, Method II | **5.9 %** | 14.5 % | 34.3 % |
| **Height**, Method I | 32.1 % | 44.9 % | 75.4 % |
| **Height**, Method II | **22.5 %** | 42.3 % | 157 % |
| **Slant range** at CPA, Method II | **17.2 %** | 34.0 % | 94.4 % |
| curve-fit RMSE, Method II | 2.78 Hz | 4.26 Hz | 16.6 Hz |

**The fits are badly overconfident.** Method II reports a median slant-range uncertainty of **3.8 %**
while actually being **17.2 %** off — a factor of ~5. Only 13 of 40 land within 10 %. This matters well
beyond this table: any weighted inversion that trusts per-pass error bars will be overconfident by the
same factor, so [DESIGN.md](DESIGN.md) calls for empirical reweighting against ADS-B truth rather than
propagating the fit covariance.

Broken out by how steady the pass was (Method II speed / height):

| Category | n | speed | height |
|---|---|---|---|
| speed const, height const | 8 | **2.7 %** | **11.6 %** |
| speed const, height varying | 20 | 5.5 % | 36.6 % |
| speed varying, height const | 4 | 7.0 % | 16.2 % |
| speed varying, height varying | 8 | 9.4 % | 18.1 % |

Context: aircraft were a median 1,226 m above the recorder at 1,722 m slant range doing 293 km/h, and
the fitted rest frequencies spanned **139–896 Hz** (median 337 Hz — these track a *harmonic*, not
necessarily the fundamental).

**Read this carefully, because it sets what's worth building:**

- **Speed is genuinely recoverable.** ~3 % on a steady pass is ±8 km/h on a 290 km/h aircraft, from a
  phone microphone. That's a real measurement.
- **Height is not, from one microphone.** 22 % median, 37 % when the aircraft is climbing or descending.
  The reason is structural: a single mic measures *slant* distance at CPA, and can't separate altitude
  from lateral offset. Don't build anything that depends on acoustic altitude alone — §8 is the fix.
- **The fit method matters more than the data.** Same recordings, 17.9 % → 5.9 % just from fitting the
  full curve instead of its endpoints. This is the same lesson as RESEARCH.md §4.2 item 3: the parametric
  shortcut is always the weak link.

---

## 3. Propeller RPM and engine health — the strongest standalone use

`f0 = N_blades × RPM / 60`. ADS-B broadcasts position, velocity, altitude and vertical rate. **It does
not broadcast RPM.** So acoustic `f0` plus the blade count (which follows from the ADS-B type code) gives
you a quantity no public data feed carries.

A concrete example in your own idiom: an RV-6 with an O-320 and a fixed-pitch two-blade prop at 2,700 RPM
puts BPF at 2700 × 2 / 60 = **90 Hz**; at a 2,400 RPM cruise, 80 Hz. Squarely in the band that propagates
best.

**The health signature is specific and well-established.** The propeller-health patent family
([US 10,858,122](https://patents.google.com/patent/US10858122), EP 3296195) rests on a clean asymmetry:

- a **healthy** propeller radiates essentially *only* BPF and its harmonics
- a **damaged or mismatched blade** breaks the blade-to-blade symmetry, which adds tones at harmonics of
  the **shaft rotation rate** that are not BPF multiples

For that 2-blade prop at 2,700 RPM: shaft rate 45 Hz, BPF 90 Hz. Healthy → energy at 90, 180, 270 Hz.
Damaged → energy appears at **45 Hz and 135 Hz**. That's a narrowband, physically-motivated test, not a
machine-learning hunch, and the patent gates alerts on steady-state flight to avoid false alarms during
power changes. (Note it *is* patented — fine for personal and research use, relevant if you ever wanted
to commercialise.)

Other things that fall out of `f0` and the comb:

- **Fixed-pitch vs constant-speed prop**, and whether the governor is working: a constant-speed prop holds
  RPM while airspeed changes; a fixed-pitch prop's RPM tracks airspeed. Two flyovers at different speeds
  distinguish them.
- **Power setting profiling** — takeoff vs climb vs cruise vs descent RPM, per tail number.
- **Prop overspeed events.**
- **Twin-engine synchronisation.** Two engines never run at exactly the same RPM, so they beat at the
  difference frequency. The beat is directly measurable, and a large one means a badly unsynchronised
  twin. It's also a free single-vs-twin classifier feature.

Honest caveat: for *your own* aircraft, a direct tachometer and engine monitor beats acoustics every time.
Acoustics earns its place for aircraft you don't own and can't instrument — or as a ground-based run-up
check, which is close to what the patent describes.

## 4. Detecting aircraft that aren't transmitting — the defensive mirror

The highest-value standalone capability, and it comes almost free.

If you detect a clean prop Doppler curve and **no ADS-B aircraft explains it**, you've found an aircraft
that wasn't transmitting, or wasn't received. This already happens as a byproduct in the existing tools:
doppler-locate's detector found "three more clear flyby curves with no ADS-B aircraft" in the Oyster Bay
hour, and at a seaplane fly-in it found **30 curves where adsb.lol had almost nothing** (seaplanes often
carry no transponder).

Uses: airfield situational awareness, ADS-B coverage-gap mapping, verifying that your own receiver's
coverage matches reality, and detecting genuinely non-cooperative traffic. This is the exact inverse of
the geolocation tool — it's a *defensive* instrument built from the same parts, and it's the one I'd be
most interested in running continuously.

Caveat: separating "not transmitting" from "transmitting but not received" requires knowing your own
receiver's coverage volume well, which is itself something you'd have to measure.

## 5. Acoustic type classification

The harmonic comb is a fingerprint: blade count sets which shaft harmonics appear, engine count sets the
beat structure, and piston-direct-drive versus turboprop-with-reduction-gearbox gives different
ratios between firing frequency and BPF.

Normally the blocker is labelled training data. With a local receiver, **labelling is automatic** — see §7.

## 6. Passive atmospheric sounding — what it actually tells you

### The observable

Sound propagating through moving air travels at the **effective sound speed along the path**:

```
c_eff = c(T) + w · r̂          r̂ = unit vector from aircraft to listener
c(T)  = 331.3 · √(1 + T/273.15)      dc/dT ≈ 0.59 (m/s)/°C at 15 °C
```

So one quantity is measured, and **1 m/s of `c_eff` ≡ 1 m/s of along-path wind ≡ ~1.7 °C of temperature.**
Fit a curve from a *known* aircraft at a *known* position, and whatever's left over is the medium.

This is already demonstrated, incidentally: doppler-locate fitted wind from the Oyster Bay audio alone and
got **14.4 m/s from 316°** against NOAA GFS's **15.1 m/s from 322°** — 0.7 m/s and 6°. Its fit residual
fell from **0.55 Hz** (still air, c = 343) to **0.21 Hz** once wind was included, which says the data is
genuinely, strongly sensitive to the medium. Every implementation treats that as a nuisance parameter to
absorb. It's a measurement.

### How precisely?

> **Corrected 2026-11-07.** An earlier version of this section was wrong twice: it converted `c` to
> temperature by *multiplying* by `dc/dT` instead of dividing (2.87× too optimistic), and it applied an
> absolute 0.2 Hz fit residual — measured on a 113 Hz tone — to a 337 Hz tone. The numbers below are the
> corrected ones, and they are materially worse. Conclusions changed; see the end of this section.

The total pitch drop is `Δf = f0·c·2v/(c²−v²)`, so

```
∂Δf/∂c = −f0·2v·(c²+v²)/(c²−v²)²
```

**Both the sensitivity and the fit error scale with `f0`, so `f0` cancels.** Writing the fit error as a
fixed fraction `ε` of the tone frequency (`σ_f = ε·f0`):

```
σ_c = ε · (c²−v²)² / (2v·(c²+v²))
```

So precision depends only on **fractional frequency precision `ε`** and on **aircraft speed `v`** — not on
which harmonic you track. Chasing a higher harmonic buys nothing, because its absolute error grows in
proportion.

Two values of `ε` are actually demonstrated in the literature:

| Source | Residual | on `f0` | → `ε` |
|---|---|---|---|
| Oyster Bay, dedicated hour-long outdoor recording | 0.21 Hz | ~113 Hz | **0.19 %** |
| Zenodo median, smartphone | 2.78 Hz | ~337 Hz | **0.83 %** |

Per pass, with `σ_T = σ_c / 0.59`:

| `ε` | `v` = 60 m/s | 90 m/s | 130 m/s |
|---|---|---|---|
| **0.19 %** (best demonstrated) | 1.70 m/s · 2.9 °C | 1.01 m/s · **1.7 °C** | 0.55 m/s · 0.9 °C |
| 0.40 % (good) | 3.58 m/s · 6.1 °C | 2.12 m/s · 3.6 °C | 1.16 m/s · 2.0 °C |
| 0.83 % (smartphone) | 7.42 m/s · 12.6 °C | 4.40 m/s · 7.5 °C | 2.41 m/s · 4.1 °C |

**Cross-check:** doppler-locate recovered wind to 0.7 m/s from one Oyster Bay pass. The formula at
ε = 0.19 % and the PC-12's actual 137 m/s gives **0.50 m/s**. Consistent, which is reassuring for the
whole derivation.

Three conclusions, all different from what the earlier version said:

1. **Wind is the viable product; temperature is marginal per pass.** The same `σ_c` is ~1 m/s of wind
   (genuinely useful) or ~1.7 °C of temperature (not). That is almost certainly why doppler-locate fitted
   *wind* and never attempted temperature.
2. **Aircraft speed matters enormously** — a factor of ~3 between a 60 m/s trainer and a 130 m/s
   turboprop. Weight fast aircraft heavily; they carry most of the information.
3. **Averaging is required, not optional.** Averaging is also no longer "irrelevant" as previously
   claimed — at these per-pass numbers it's the only route to a useful product:

| Target | ε = 0.19 % | ε = 0.40 % | ε = 0.83 % |
|---|---|---|---|
| wind to 1.0 m/s | 2 / 6 | 5 / 23 | 20 / 98 |
| wind to 0.5 m/s | 5 / 21 | 18 / 91 | 78 / 389 |
| temperature to 1.0 °C | 3 / 15 | 13 / 65 | 56 / 279 |
| temperature to 0.5 °C | 12 / 59 | 52 / 259 | 223 / 1116 |

*(passes needed for a path-average / for a 1–2 km layer, which costs 2.24× — see below)*

So **`ε` is the single most important engineering parameter in the whole project**, and it is set by
signal-to-noise: microphone, windscreen and siting. Going from smartphone-grade to Oyster-Bay-grade fits
cuts the required pass count by ~20×. That is the quantitative justification for §6 of
[DESIGN.md](DESIGN.md) spending its effort on wind treatment rather than on compute.

Systematics (straight-ray propagation, no refraction, RPM wander, ground reflection) will still impose a
floor somewhere, and where it sits is unmeasured — so treat the averaging table as a best case and
validate against radiosondes before believing any of it.

### Can wind and temperature be separated? Yes.

This is the question that decides whether it's useful, because both move `c_eff`. They separate by
**angular signature**: temperature is isotropic, wind is `|w|·cos(θ − θ_w)`.

And the geometry cooperates — within a *single* pass the line-of-sight bearing sweeps about **127°**
(for a trackable arc of ±2 CPA distances, independent of range). That's why doppler-locate recovered wind
speed *and* direction from one flyover. Passes on several bearings pin all three parameters
(`c₀`, `|w|`, `θ_w`) properly.

### Altitude resolution: coarse, and that's the real limit

One pass measures the **path average** of `c_eff` from the aircraft down to you — roughly uniform in
height for a near-overhead pass. A profile requires differencing passes at different altitudes. Since a
pass at height `h` measures `m(h) = (1/h)∫₀ʰ c dz`, the layer mean is `(h₂m₂ − h₁m₁)/(h₂ − h₁)`, which
amplifies the error by `√(h₁² + h₂²)/(h₂ − h₁)`:

| Layer | Amplification | σ_c per layer | as temperature | with 20 passes |
|---|---|---|---|---|
| 0–1 km | 1.0× | 0.40 m/s | 0.68 °C | 0.15 °C |
| 1–2 km | 2.2× | 0.89 m/s | 1.51 °C | 0.34 °C |
| **1.0–1.5 km** | **3.6×** | 1.44 m/s | 2.44 °C | 0.55 °C |
| 2–4 km | 2.2× | 0.89 m/s | 1.51 °C | 0.34 °C |
| **3.0–3.5 km** | **9.2×** | 3.69 m/s | 6.25 °C | 1.40 °C |
| 4–7 km | 2.7× | 1.07 m/s | 1.82 °C | 0.41 °C |

*(σ_c = 0.40 m/s per pass assumed — roughly the best-demonstrated `ε` on a fast aircraft)*

**Thin layers are hopeless; 1–2 km layers are usable**, and only with a couple of dozen passes each. The
ceiling is about **7 km**, because props are the only acoustically useful aircraft (RESEARCH.md §2.1 —
jets are useless here, which costs the 9–12 km band entirely).

This is the constraint that sets the product's **cadence**: a 1–2 km layer needs ~20 passes to reach a
third of a degree, so useful soundings are **hourly-to-daily aggregates**, not per-pass snapshots. In
sparse traffic it may be daily at best. Pass density by altitude band is therefore the thing to measure
first, before committing to the inversion — and it's measurable from ADS-B alone, before any microphone
is deployed.

### A second, independent channel: humidity from absorption

Sound speed barely responds to humidity, but *absorption* responds strongly, and frequency-selectively.
Over a 3 km path, 30 % vs 90 % RH:

| Tone | α @30 % RH | α @60 % RH | α @90 % RH | spread over 3 km |
|---|---|---|---|---|
| 200 Hz | 0.99 dB/km | 0.88 | 0.71 | 0.9 dB |
| 400 Hz | 1.80 | **1.91** | 1.90 | 0.3 dB |
| 800 Hz | 3.93 | 3.33 | 3.59 | 1.0 dB |
| **1600 Hz** | **11.92** | 6.95 | **6.36** | **16.7 dB** |

So the harmonic comb's *differential* decay carries humidity. The trick is that absolute level is
unusable (unknown source power), but **ratios between harmonics, tracked as the range changes through the
pass**, cancel the unknown source level — leaving differential absorption.

Two cautions, which is why this stays the most speculative item here:

- **Absorption is not monotonic in humidity.** Look at 400 Hz: 30 % → 1.80, 60 % → 1.91, 90 % → 1.90.
  The oxygen relaxation frequency moves with humidity, so α peaks at intermediate RH at some frequencies.
  A single-frequency inversion is therefore **ambiguous**; you need several harmonics spanning enough
  range (the 1600 Hz behaviour is monotonic and strong) to break it.
- It needs the harmonics to survive to useful range at all, and 1600 Hz is already losing 20–36 dB over
  3 km. So this works for close passes, not distant ones — the opposite of the Doppler products.

Treat it as a stretch goal to revisit once the comb fit is solid, not something to design around.

### So what is it good for?

Honest niche: **passive, continuous, single-point, surface to ~7 km, free, at the cadence your local
traffic allows.** That band is precisely where numerical weather models are weakest — the boundary layer
and lower troposphere. Compare the alternatives: radiosondes are twice daily at ~70 US sites (sparse in
time *and* space); SODAR and wind profilers are active, commercial and expensive.

Given the corrected precision above, the realistic framing is a **wind profiler first** — ~1 m/s per good
pass, aggregating to ~0.3 m/s per 1–2 km layer over ~20 passes — with temperature as a weaker secondary
product. Phenomena that are plausibly in reach and that models handle badly: **low-level jets**,
sea-breeze fronts, and local HRRR boundary-layer wind validation. Temperature **inversions** are
interesting but sit right where the method is weakest (thin layers, low altitude, often calm air and
therefore slow local traffic) — though they have a self-confirming acoustic signature, since they trap
sound and make distant aircraft anomalously loud. For a pilot, the wind product is directly useful for
soaring and thermal forecasting.

It will not beat a radiosonde on accuracy or vertical resolution, and per-pass soundings are not a
product. What it adds is **cadence at one point**, using aircraft of opportunity, for the price of a
microphone.

**The premise is testable before buying anything.** The whole product hinges on pass density by altitude
band at your site, and that is measurable from ADS-B alone — see [DESIGN.md §10](DESIGN.md), step 1.

### And it feeds back into the main project

Circular benefit worth stating plainly: doppler-locate's residual went 0.55 → 0.21 Hz by adding wind, so
the atmosphere is a large share of the geolocation error budget. If a local sounder measures winds aloft
*better than GFS at your site*, geolocation accuracy improves directly. The sounding isn't only a spin-off
— it's an input.

## 7. Community noise monitoring with tail numbers attached

Airport noise disputes are normally argued with *models*. A mic plus an ADS-B receiver gives you
measurements bound to specific aircraft: which operator, what type, what altitude, measured dB, at what
time. Automatically, continuously, with no cooperation from anyone. That's a civic application with real
teeth, and it's mostly a data-plumbing exercise once the detector works.

## 8. Multi-microphone: full passive tracking *(optional)*

Height is the weak axis (§2) because one mic can't separate altitude from lateral offset. Networked mics
spread across a property address it: time-difference-of-arrival constrains direction, and with range gives
a 3D track **independent of ADS-B**.

Two important corrections to the naive version:

- **Two mics give a *cone*, not a bearing.** A two-element array measures only the angle from its baseline
  axis, so a left/right mirror remains, and there is *no* bearing sensitivity along the axis. **Three
  non-collinear** mics are what close the geometry.
- **This is optional, and it isn't the bottleneck.** For any aircraft in ADS-B you already know the
  altitude to ~10 m, so acoustic altitude only matters for aircraft *not* transmitting. Single-mic
  deployments lose nothing on the atmospheric and RPM/health products.

Clock sync is the engineering risk, but a wide baseline is surprisingly forgiving — see
[DESIGN.md §4](DESIGN.md) for the computed requirements and the trick of self-calibrating the array from
ADS-B itself.

## 9. Measuring broadcast and stream latency

Niche but exact. Compare acoustic CPA time against ADS-B CPA time at a known camera position and the
difference is the stream's delay. colsto-leak-and-mask did precisely this, reading a **28 s** delay off
a Midway webcam. Useful for anyone who needs to know a live stream's true latency.

---

## 10. What the local ADS-B receiver specifically unlocks

Everything above gets better with one, but two things are *only* possible with it:

**An auto-labelling dataset machine.** Point a mic at the sky, run a receiver, and every flyover is
automatically labelled with type, registration, altitude, speed, heading, vertical rate, slant range and
engine class. No hand-annotation. Within weeks you'd have thousands of labelled real-world flyovers, with
your own microphone's characteristics baked in — the corpus a learned detector with calibrated `f0`
uncertainty needs.

> **Correction:** I previously called this mechanism novel. It isn't — **AeroSonicDB (YPAD-0523)**,
> arXiv 2311.06368 (2023), is a published dataset built by exactly this method: 625 ADS-B-labelled
> aircraft recordings, 301 unique aircraft, 14 non-acoustic labels each. See
> [PRIOR-ART.md §3](PRIOR-ART.md). The upside is that it's a free ready-made corpus, far better than the
> 40-case Zenodo set — though it was built for *classification*, so whether its clips have the timing
> precision and tonal SNR that Doppler fitting needs is worth checking before relying on it.

**Permanent ground truth for the geolocation tool.** The benchmark problem from RESEARCH.md Stage 1 stops
being a fixed 40-case corpus and becomes continuous regression testing against known answers. And
critically, **it solves the coverage problem too** — 4 of 10 published field sites failed
for sparse low-altitude ADS-B, and a receiver at your own site is the densest possible coverage exactly
where you need it.

---

## 11. Build order

These applications are now the project's purpose rather than spin-offs, so the build order lives in one
place: **[DESIGN.md §10](DESIGN.md)**. In brief — ADS-B node, then one acoustic node, then
ADS-B-triggered clip capture, then the harmonic-comb fit validated against ADS-B, then the atmospheric
inversion; prop health late (it needs many repeat passes to mean anything), and extra microphones last
and optional.

A note on scope: §4, §7 and §8 are *surveillance* capabilities pointed at your own airspace. Aircraft
positions are public data and ADS-B is broadcast in the clear, so this is a different ethical posture from
locating a person's recording — but it's worth being deliberate that they stay pointed at aircraft.
