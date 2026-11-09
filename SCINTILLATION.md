# Turbulence from tonal scintillation — detailed work-up

The idea: the Doppler curve's **mean** gives wind and temperature; its **fluctuations** give turbulence.
Everything the current plan discards as `ε` contains a second measurement.

Expands [EXPANSION.md §1](EXPANSION.md). All numbers computed here, 2026-11-07.

---

## 1. The physics

A tone propagating through turbulent air acquires log-amplitude and phase fluctuations. Define the
log-amplitude perturbation `χ = ln(A/⟨A⟩)`. In the weak-scattering (Rytov) regime with a Kolmogorov
spectrum:

```
σ²_χ  ≈  0.50 · C²_eff · k^(7/6) · L^(11/6)          k = 2πf/c
```

Ostashev & Wilson's result that **log-amplitude and phase variances are nearly equal for plane and
spherical waves** means you don't need to be precious about the source model.

For *acoustics* the refractive-index fluctuation combines temperature and wind, which is why this measures
something mechanically meaningful:

```
μ′ = T′/(2T₀) + v′/c           C²_eff ≈ ⟨μ′²⟩ / L₀^(2/3)
```

Wind fluctuations dominate: at `T′ = 0.5 K` and `v′ = 1 m/s`, the wind term is ~3.4× the temperature term.
So this is closer to a **momentum**-turbulence measurement than a thermal one — which is the aviation-relevant
quantity.

Resulting structure parameters:

| Condition | `T′` | `v′` | `C²_eff` (m^−2/3) |
|---|---|---|---|
| Night, stable | 0.05 K | 0.2 m/s | 1.6 × 10⁻⁸ |
| Morning / evening | 0.2 K | 0.5 m/s | 1.0 × 10⁻⁷ |
| Daytime convective | 0.5 K | 1.0 m/s | 4.3 × 10⁻⁷ |
| Strong convection | 1.0 K | 2.0 m/s | 1.7 × 10⁻⁶ |

## 2. Where it works — the regime map

`σ²_χ` computed across frequency, range and condition. **W** = weak scattering, quantitatively invertible.
**S** = saturated, where the Rytov expression no longer holds (taking 0.3 as the boundary).

| Condition | f | 0.5 km | 1 km | 2 km | 4 km | 6 km |
|---|---|---|---|---|---|---|
| Night, stable | 80 Hz | 0.00 W | 0.00 W | 0.01 W | 0.05 W | 0.11 W |
| | 160 Hz | 0.00 W | 0.01 W | 0.03 W | 0.11 W | 0.24 W |
| | 320 Hz | 0.01 W | 0.02 W | 0.07 W | 0.25 W | 0.54 **S** |
| Morning / evening | 80 Hz | 0.01 W | 0.03 W | 0.09 W | 0.33 **S** | 0.69 **S** |
| | 160 Hz | 0.02 W | 0.06 W | 0.21 W | 0.73 **S** | 1.54 **S** |
| Daytime convective | 80 Hz | 0.03 W | 0.11 W | 0.38 **S** | 1.35 **S** | 2.83 **S** |
| | 160 Hz | 0.07 W | 0.24 W | 0.85 **S** | 3.02 **S** | 6.35 **S** |
| Strong convection | 80 Hz | 0.12 W | 0.42 **S** | 1.51 **S** | 5.38 **S** | 11.3 **S** |

**Two things fall out of this table.**

**First, it is independently confirmed.** The 2024 *Acta Acustica* paper *"Auralization of atmospheric
turbulence-induced amplitude fluctuations in aircraft flyover sound based on a semi-empirical model"*
reports that measured flyover log-amplitude variance "shows signs of saturation but reaches values around
1, corresponding to level fluctuations of more than 8 dB." That lands squarely in the daytime-convective
row at 2–4 km. The physics above reproduces published observations, which is reassuring before building
anything.

**Second, the usable regime is specific:** close passes, low harmonics, stable air. Nights and
mornings are quantitatively invertible out to several km; **daytime convection saturates beyond ~1–2 km.**
That is awkward, because afternoon convective turbulence is precisely what's most interesting.

## 3. Saturation is a product, not only a problem

Don't fight it. Build two tiers:

**Tier 1 — quantitative `C²_eff`** in the weak regime (W cells). Direct Rytov inversion.

**Tier 2 — a turbulence index from the saturation boundary.** The W/S boundary *moves* with `C²_eff`. For a
fixed tone, the range at which saturation sets in is a monotonic function of turbulence strength, and
you get many ranges for free from ADS-B geometry. So "the shortest range at which scintillation saturates
today" is a usable turbulence proxy even where the variance itself is uninformative.

The *Acta Acustica* paper already supplies a **semi-empirical saturation correction anchored on the
weak/strong boundary**, so there's a published forward model to invert rather than one to invent. It also
notes Kolmogorov can stand in for von Kármán for log-amplitude variance, which simplifies things.

## 4. Precision per pass — and why a moving source helps

The scintillation pattern decorrelates over the **Fresnel scale** `√(λL)`. Critically, the aircraft's
motion sweeps the propagation path through the turbulence field — the path midpoint moves at roughly `v/2`
— so independent samples accrue much faster than for a fixed source.

| f | L | Fresnel scale | `v` | decorrelation | samples per 90 s pass | error on `σ²_χ` |
|---|---|---|---|---|---|---|
| 80 Hz | 1 km | 65 m | 90 m/s | 1.5 s | 62 | **18 %** |
| 80 Hz | 3 km | 113 m | 90 m/s | 2.5 s | 36 | 24 % |
| 160 Hz | 1 km | 46 m | 130 m/s | 0.7 s | 126 | **13 %** |
| 320 Hz | 3 km | 57 m | 90 m/s | 1.3 s | 71 | 17 % |

So **`C²_eff` to roughly 20–30 % from a single pass** — a few dB, which is respectable for a turbulence
measurement. A static source decorrelating only at the wind speed (~5 m/s) would give ~5× fewer samples.

**Using aircraft of opportunity is an advantage here, not merely a convenience.** That's a sharper
argument than the one for the mean-quantity products, where the moving source is mostly just free.

## 5. The estimator

1. **Extract the complex amplitude** of each comb harmonic along the fitted Doppler curve — a tracking
   filter following the known frequency track, not a fixed-bin STFT.
2. **Remove the geometric trend** from `ln A`: spherical spreading, absorption, and source directivity all
   vary smoothly through the pass. Turbulence lives at the Fresnel timescale (~1–3 s); the geometric trend
   lives at the pass timescale (~60–120 s). A high-pass at ~0.1 Hz separates them cleanly — a 1.5-decade
   gap, which is comfortable.
3. **`σ²_χ` = variance of the residual**, per harmonic.
4. **Invert** via Rytov in the weak regime, or the semi-empirical correction when saturated, using the
   ADS-B path geometry for `L`.
5. **Weight and accumulate** by altitude band and path, exactly as the mean-quantity inversion does — same
   machinery, same layer scheme ([DESIGN.md §7](DESIGN.md)).

## 6. Separating turbulence from propeller RPM wander — the key test

This is the make-or-break confound, and the harmonic comb solves it **with no extra hardware and no
geometric assumptions**:

| | Turbulence | RPM wander |
|---|---|---|
| Amplitude variance vs frequency | **∝ f^(7/6)** | ~no effect to first order |
| Frequency jitter across harmonics | partially decorrelated | **perfectly correlated** in `Δf/f` |
| Absolute frequency jitter | — | ∝ f¹ |
| Correlates with path length `L` | **yes, ∝ L^(11/6)** | no |

So, **within a single pass, at a single instant**, compare harmonics of the *same* tone. Source and
geometry are identical by construction, so anything that varies across harmonics is propagation:

- From 80 → 320 Hz (×4), turbulence predicts log-amplitude variance **×5.04**.
- Engine wander predicts coherent fractional frequency shifts and essentially no amplitude scaling.

That's a within-pass, assumption-free discriminator. It also means a pass with at least two clean
harmonics is worth far more than one with a single strong tone.

## 7. The validation plan — free, and with enormous dynamic range

**Test 1: diurnal cycle.** At a fixed geometry (2 km, 80 Hz) the table predicts 0.01 → 0.09 → 0.38 → 1.5
from stable night to strong convection. **That is a ~150× swing at fixed geometry.** You cannot miss it if
it's real; equally, its absence is a decisive negative. This is the first thing to plot and it needs no
hardware beyond what the project already specifies.

**Test 2: range scaling.** `L^(11/6)` predicts ×3.56 from 1→2 km and ×12.7 from 1→4 km. ADS-B hands you a
wide spread of ranges every day, so this is a free regression test.

**Test 3: frequency scaling** across the comb, as §6.

**Test 4: independent reference.** Compare against a sonic anemometer at the surface (cheap, and gives
real `v′` statistics), against HRRR's turbulence/TKE fields, and — if you receive your own radiosondes
([EXPANSION.md §8](EXPANSION.md)) — against profile-derived shear and stability.

If Tests 1–3 all hold with the predicted exponents, the measurement is real. If `σ²_χ` is large but
*flat* in both frequency and range, you're measuring your own instrument or the engine — which is
itself worth knowing, since it would mean `ε` is improvable rather than atmospheric.

## 8. Confounds, honestly

- **Saturation** at the most interesting times (§2–3). The hardest limitation.
- **RPM wander** — addressed by §6, but it must actually be *done*, not assumed away.
- **Wind noise and SNR**, which produce amplitude fluctuation that is *not* turbulence along the path.
  Mitigation: wind noise is broadband and low-frequency-weighted while scintillation is multiplicative on
  the tone, so estimate the noise floor beside the tone and subtract its contribution. This is probably the
  second-biggest practical risk.
- **Ground reflection interference** — a moving source sweeps the comb-notch pattern, producing amplitude
  modulation that mimics scintillation. Two mics at different heights ([EXPANSION.md §5.1](EXPANSION.md))
  is the clean fix; otherwise model it from geometry.
- **Aircraft manoeuvring and power changes** — changes both amplitude and `f0`. Gate on steady flight, as
  the health product does.
- **Theory limits**: the literature is explicit that weak-scattering models are "limited to relatively
  short propagation distances and weak turbulence." Expect semi-empirical work at the ranges here.
- **Path-integration.** Like the mean products, you get a path average, not a profile, so the layer
  inversion and its error amplification apply ([APPLICATIONS.md §6](APPLICATIONS.md)).

## 9. Prior art, and the gap

- **Forward modelling is published and validated** — the *Acta Acustica* 2024 auralisation paper plus
  companion work, which synthesise realistic flyover sound *from* turbulence, including a saturation
  correction.
- **Theory is mature** — Ostashev & Wilson, *Acoustics in Moving Inhomogeneous Media* (2nd ed., 2015) is
  the reference; the Tatarskii lineage is well documented. A JASA paper on Martian sound propagation even
  uses the Rytov/Markov log-amplitude variance for a **ground source and elevated receiver** — our
  geometry inverted.
- **The inverse problem appears unexplored for tonal aircraft noise.** My searches found no work measuring
  the acoustic turbulence structure parameter from the scintillation of aircraft tones.
- **Related prior art to check before claiming novelty:** US patent 10,389,940 *"Passive method to measure
  strength of turbulence"* — I have not read it, and anyone pursuing this seriously should.

So the honest framing: **the forward model exists and is validated; the contribution is inverting it, with
aircraft of opportunity, at km ranges.** That is a much safer position than inventing physics — and it's
the same relationship PropDop's main product has to the UAV tomography literature
([PRIOR-ART.md §2](PRIOR-ART.md)).

## 10. Why this may be the project's best idea

- **Zero marginal hardware.** It re-analyses data the plan already collects.
- **It converts the error budget into a product.** `ε` currently caps every other measurement; here it
  *is* the signal.
- **It measures something the incumbents don't.** Mode-S and AMDAR give wind and temperature; they
  characterise turbulence poorly. Turbulence is the aviation-relevant quantity (EDR is the operational
  metric), and an independent path-integrated measure would be genuinely different.
- **The moving source is a real advantage** (§4), not just a convenience.
- **Validation is free and unambiguous** (§7) — a 150× diurnal swing either appears or it doesn't.
- **The discriminator is clean** (§6) — within-pass, assumption-free.

## 10a. What implementing it actually showed (2026-11-07)

The estimator is built (`propdop.fit`) and tested against synthetic passes with injected
turbulence. Three claims above turned out to be wrong, and one held up well.

**Wrong: "a high-pass at ~0.1 Hz separates them cleanly — a 1.5-decade gap."** It does not.
The scintillation correlation time is the Fresnel scale crossed at ~v/2, which §4 puts at
1–2.5 s — i.e. **0.08–0.16 Hz, sitting directly on that corner.** Worse, the Fresnel scale
goes as `1/√f`, so a fixed high-pass removes *more* variance from low harmonics than high
ones, which biased the measured exponent up to **~1.7**. The fix is to detrend by the
**modelled** envelope — spherical spreading plus absorption, both known once the geometry is
fitted — and then remove only a low-order polynomial for unmodelled directivity. That brought
the exponent from ~1.7 to ~1.2.

**Wrong: additive noise is a secondary risk.** It is a first-order bias with the *same
signature as the signal*. For `z = A + n`, `ln|z| ≈ ln A + Re(n)/A`, so noise contributes
`σ²/(2A²) = 1/(2·SNR)` to the log-amplitude variance — and since higher harmonics are weaker,
**that bias rises with frequency and imitates f^(7/6)**. Uncorrected, a pass with *no*
turbulence at all returned an exponent of 2.55. The fix is a control track heterodyned midway
between harmonics (same sweep, same filter, no tone) to measure the in-band noise, then
subtract. With it, the zero-turbulence case correctly reports **nothing measurable** rather
than inventing a number.

**Wrong: frequency coherence is "the sharper one."** It is the weaker one. Turbulent *phase*
perturbation also scales with harmonic number (`φ_k = k·∫μ dl`), so turbulence produces
partially correlated fractional frequency error too — measured 0.18–0.85 across cases against
0.93–1.00 for engine wander. Separation exists but overlaps. **The amplitude exponent is the
primary discriminator**; coherence is supporting evidence only. A further trap: residual fit
misfit is common-mode across harmonics exactly like wander, and inflates coherence towards 1
regardless of cause, so a low-order polynomial has to come off first.

**Held up: the exponent discriminates.** Measured over 12 synthetic passes:

| Injected | Exponent, single pass | Spread | Pooled |
|---|---|---|---|
| Turbulence (`C²_eff` = 1.6e-8 and 1.0e-7) | mean **1.33** | sd 0.20, range 0.95–1.62 | 1.32 |
| RPM wander, 1 %, no turbulence | **3.1–3.9** | — | — |
| Nothing | refuses (0 harmonics clear the noise margin) | — | — |

So turbulence and engine wander separate cleanly, and the estimator declines when there is no
signal. Two caveats recorded honestly: **a single pass cannot pin the exponent** (sd 0.20),
consistent with everything else here needing averaging; and there is a **residual +0.15 bias**
that pooling does not remove, most likely because the injected correlation time goes as
`f^(-1/2)` so the polynomial detrend and the 4 Hz low-pass do not treat harmonics identically.
Closing that is the obvious next refinement.

## 11. Implementation order

1. **Log-amplitude extraction and trend removal** on the existing comb fit. Pure software on clips you'd
   already have.
2. **Plot `σ²_χ` against time of day** at matched geometry. Test 1 — the whole idea stands or falls here,
   and it costs nothing.
3. **Regress against `L` and `f`** (Tests 2 and 3), which also measures how much of `ε` is engine wander.
4. **Noise-floor subtraction** for SNR-driven fluctuation.
5. **Rytov inversion** in the weak regime; report `C²_eff` with the per-pass 20–30 % error.
6. **Saturation-boundary index** for the saturated regime, using the published semi-empirical correction.
7. Only then consider the layer inversion and comparison against HRRR TKE.

Steps 1–3 are a weekend of analysis on data the project collects anyway, and they answer whether the rest
is worth doing.

## What the sixth review found: two defects between the measurement and the plot

The within-pass analysis was right; the dashboard query that aggregates it was not. Both
defects were found because the plot disagreed with an expectation that turned out to be
correct — convective turbulence should rise into the late afternoon, and the plot was flat.

**The noise-bias subtraction ran unguarded.** `CombFit.scintillation_scaling` excludes any
harmonic whose raw log-amplitude variance does not clear its own in-band noise bias by 1.5x,
because below that the measurement is of SNR rather than of the atmosphere. The hourly
aggregation subtracted the bias unconditionally. Measured on 80 stored passes, **11 came out
negative** — a variance cannot be — with the worst at **-82.7** against a real daytime signal
spanning 0.4 to 1.3. A single impossible value set the y-range, so the signal occupied about
3% of the plot height and read as a flat line. The fix applies the existing guard, which
excludes 17 of 80 passes, and reports per-hour how many were excluded so that "no usable
passes this hour" cannot impersonate "no passes this hour".

**The axis was UTC while the rest of the dashboard is local.** The cycle under test is driven
by the sun over the station, so west of Greenwich the convective afternoon landed in the small hours of the
plot and could not be read against the expectation it exists to test.

With both fixed, one day of passes (n=80, 17 excluded) gives:

| local hour | 05 | 08 | 09 | 10 | 12 | 13 | 14 | 15 | 16 | 17 | 20 | 21 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| mean var | 0.25 | 1.22 | 0.51 | 0.81 | 0.97 | 0.96 | 0.97 | 0.71 | **1.32** | 1.07 | 0.94 | 0.72 |

The late-afternoon maximum is where it is expected. It is **not yet a diurnal detection**: the
swing is about 5x where SCINTILLATION.md 7 predicts ~150x at matched geometry, this is a
single day, several hours rest on one or two passes, and the geometries are not matched — the
contributing slant ranges from 258 m to 16 km, which dominates the variance. The honest claim
is that the plot is now readable and is not inconsistent with the prediction.
