# Expansion directions — the acoustic side

Researched 2026-11-07. Focused on what the **microphone** can yield beyond what's planned. Additional
radio streams (ACARS, VDL2, UAT/FIS-B) are deliberately out of scope here; §6 keeps only the one radio
item that the acoustic products depend on.

---

## The framing

The expensive parts of this build are the **site** (enclosure, power, network, mast, windscreen, surveyed
position) and the **clock**. Against those, a single microphone accumulates:

- a continuous timestamped acoustic record at a known position
- thousands of measurements of **known sources at known geometry**, courtesy of ADS-B
- and, as a by-product, everything else audible for years

Almost everything below is **re-analysis of data the current plan already collects.** Two headline items
are genuine gaps in the literature; both turn parts of the current *error budget* into products.

---

## 1. Turbulence from tonal scintillation — turning `ε` into a product

> Worked up in full, with a regime map and validation plan, in **[SCINTILLATION.md](SCINTILLATION.md)**.

The most interesting acoustic expansion, and the one most relevant to aviation.

The current plan treats the scatter in each Doppler fit as noise, lumped into `ε`
([DESIGN.md §2](DESIGN.md)). But a tone propagating through turbulent air acquires **log-amplitude
fluctuation, phase/frequency jitter, and reduced spatial coherence** — and those fluctuation statistics
encode the **strength of turbulence along the path**. The mean of the curve gives you wind and
temperature; the *variance* gives you turbulence.

### Why this is worth pursuing

- **The observable is already established** in the forward direction. Aircraft flyover auralisation
  research models exactly this: a 2025 *Acta Acustica* study ("Atmospheric turbulence affects noise
  annoyance from aircraft flyovers") estimates log-amplitude fluctuation at 200 ms update intervals, and
  there's companion work on "Fluctuations by atmospheric turbulence in aircraft flyover auralisation."
  So the physics is modelled, validated and published — just pointed the other way.
- **Nobody appears to invert it.** My search found no work measuring the acoustic turbulence structure
  parameter from *tonal* scintillation of aircraft noise. The auralisation literature treats turbulence as
  an input to synthesise realistic sound; using the measured fluctuations to infer the turbulence is the
  unexplored direction.
- **It fills a gap the incumbents don't.** Radiosondes and Mode-S/AMDAR give wind and temperature but
  characterise turbulence poorly. Turbulence is operationally important in aviation (EDR is the standard
  metric, derived from aircraft accelerometers) and scientifically important in boundary-layer work. An
  independent, continuous, path-integrated turbulence measurement would be a genuinely different
  observable.

### It has a free validation story

This is what makes it attractive despite the risk — two strong, testable predictions that need **no extra
hardware**:

1. **A diurnal cycle.** Convective turbulence peaks in the afternoon and collapses overnight. Log-amplitude
   variance should track that, strongly.
2. **Path-length scaling.** Variance should grow with propagation distance in a predictable way, and you
   get a wide spread of path lengths for free from ADS-B geometry.

If both hold, you are measuring turbulence. If neither does, you're measuring your own instrument — which
is itself worth knowing, because it would mean `ε` is instrumental rather than atmospheric and therefore
improvable.

### The honest risk

The theory is incomplete at the ranges that matter. The literature states plainly that the best available
model for amplitude fluctuations is **"limited to relatively short propagation distances and weak
turbulence"**, with semi-empirical models used beyond that. At 1–6 km through a convective boundary layer
you are outside the clean regime. So expect to work semi-empirically, calibrating against something else
rather than inverting from first principles.

Also, decomposing `ε` requires separating three contributions: instrument/SNR noise, **propeller RPM
wander** (the source genuinely is not perfectly steady), and turbulence. RPM wander is the awkward one —
though it should be uncorrelated with path length, which is the handle for separating it.

## 2. Measured noise emission and directivity per aircraft type

Lower risk than §1, immediately useful, and also an unoccupied gap.

With a **calibrated** microphone, geometry from ADS-B and the type/registration from the type code, each
pass measures the aircraft's actual radiated level. Two products:

**Source levels per type.** Reference values exist to check against — EASA maintains a measured
certified-noise database, and FAA AC 36-1H tabulates certificated levels for propeller-driven small
aeroplanes. So you can validate, and then detect aircraft that deviate (modified exhausts, different
props, degraded mufflers).

**Directivity — the more novel part.** During a single pass the aircraft sweeps through a wide range of
angles relative to the microphone, so one flyover samples a *slice* of the source's directivity pattern.
Propeller noise is strongly directional (in-plane versus axial radiation differ a lot), and NASA's
flyover-noise work explicitly flags "tilt of the propeller axis relative to the flight path" as an error
source in predictions. Accumulate many passes at many geometries and you can reconstruct the full 3D
directivity per type.

**Why it matters:** current ADS-B-based noise mapping is *modelled*, not measured. A Purdue study pairing
ADS-B with the EUROCONTROL ANP database reported **4.5 dBA average error** at a non-towered GA airport,
and the commercial Flight Path Noise service estimates from a reference level per type. My search found
**no open dataset pairing ADS-B tracks with measured directivity or propeller sound power for GA
aircraft.** Measured per-type data at realistic geometries would be a direct improvement to those models,
and GA/non-towered airports are exactly where noise data is thinnest.

Requirement: an actual calibrated measurement microphone plus a calibrator. Everything else in the
project works off *frequency* and needs no amplitude calibration — this product is the reason to spend on
one.

## 3. A richer engine diagnostic: firing frequency versus blade-pass

[APPLICATIONS.md §3](APPLICATIONS.md) uses the propeller blade-pass tone. The **engine** has its own
independent tone, and separating the two is a better diagnostic than either alone.

For a 4-stroke engine, each cylinder fires once per two revolutions, so

```
firing frequency = (RPM/60) · N_cylinders/2
blade-pass        = (RPM/60) · N_blades
```

Two independent tones in a fixed ratio for a direct-drive engine. A departure from that ratio, or
sidebands around it, indicates cylinder imbalance — a dropped or weak cylinder, ignition trouble.
Half-order components (at `RPM/120` and odd multiples) are the classic signature of one cylinder
misbehaving in a 4-stroke.

**An honest degeneracy:** for a **4-cylinder with a 2-blade prop, direct drive** — a Lycoming O-320 in an
RV, a C172 — both land on the same frequency (80 Hz at 2,400 RPM) and cannot be separated. That
configuration is extremely common. The cases that *do* separate:

| Configuration | Firing | BPF (2,400 RPM) | Separable? |
|---|---|---|---|
| 4-cyl, 2-blade, direct | 80 Hz | 80 Hz | **No — degenerate** |
| 6-cyl, 2-blade, direct | 120 Hz | 80 Hz | Yes, 3:2 |
| 4-cyl, 3-blade, direct | 80 Hz | 120 Hz | Yes, 2:3 |
| Geared turboprop | gas generator | prop ÷ gear ratio | Yes, cleanly |

Even in the degenerate case the *half-order* imbalance components at 20 and 40 Hz remain diagnostic, and
they sit where absorption is lowest (0.054 dB/km at 45 Hz — PRIOR-ART §5). Worth checking against
wind-noise floor, which is worst in exactly that band.

## 4. Other aircraft-acoustic products from the same corpus

- **Propeller advance ratio and performance mapping.** `J = V/(nD)` — RPM from acoustics, true airspeed
  from ADS-B, diameter from type. Over many passes you build an empirical operating-point map per type.
  Directly relevant if you're flying an RV.
- **Helicopters.** Main-rotor blade-pass is very low (~10–20 Hz), rotor RPM is nearly constant (so the
  constant-`f0` assumption holds far better than for a manoeuvring light aircraft), and low frequencies
  propagate furthest. Plus tail-rotor tones and blade-slap as distinctive features. A good, under-served
  target.
- **ADS-B-guided source separation.** When two aircraft overlap, you *know* how many there are and what
  curve each should follow. That makes a supervised source-separation dataset with free labels — rare and
  valuable for machine-listening work, and it directly improves the detector.
- **A self-supervised acoustic embedding** trained on the auto-labelled corpus: type ID, few-shot
  recognition of unusual types, and anomaly detection that doesn't depend on hand-specified signatures.

## 5. Acoustic inversions for the ground and the propagation path

### 5.1 Ground reflection → ground impedance → soil moisture

**Two microphones at different heights on the same mast** — same ADC, so no timing problem and no second
site. The ground reflection interferes with the direct path, putting comb-filter notches in the received
spectrum whose frequencies depend on geometry *and* on the ground's acoustic impedance. Impedance in turn
depends on porosity and water content.

Two payoffs. First, practical: ground effect is a systematic the current plan merely hopes is small, and
two heights let you **measure and correct** it. Second, speculative: with geometry known exactly from
ADS-B, inverting the notch pattern gives ground impedance, trackable over time as a **soil-moisture
proxy**. I found nobody doing this with aircraft of opportunity. Cheap to try, since the correction is
worth having regardless.

### 5.2 Propagation climatology and ducting

At fixed geometry over months, measured **excess attenuation** versus time of day and season is a direct
record of refraction and ducting. Anomalously loud distant aircraft is the signature of a temperature
inversion trapping sound — self-confirming, and it cross-checks the §1 and atmospheric products. Feeds
back as a correction term, and is of standalone interest to outdoor-noise modelling.

## 6. Non-aircraft sources — the same microphone, the same archive

### 6.1 Bioacoustics and phenology (best value-to-effort here)

The 24/7 48 kHz archive **already contains** birds, frogs and insects. **BirdNET** (Cornell) runs on a Pi;
`BirdNET-Pi` and **BirdWeather** are established platforms with active communities.

Marginal cost is near zero, and the scientific value is real and compounding: multi-year **phenology** —
first calls of spring, migration timing, breeding-season shift — is scarce data that only a permanent
station can produce, and it reaches a completely different research community. The only requirement is a
good microphone and windscreen, which the main project needs anyway.

Insect and amphibian chorus timing are recognised climate indicators on the same recording. **Bats** need
a separate ultrasonic channel (192–384 kHz), so that's a real hardware addition rather than free.

### 6.2 Thunder, and storm mapping without a radio

Thunder ranging is the classic acoustic measurement, and your calibrated propagation model makes it
better than flash-to-bang arithmetic. The flash timing needs an optical sensor — **a photodiode or an
all-sky camera, not a radio receiver** — and with multiple nodes thunder arrival times map the lightning
channel geometry, not just its range.

### 6.3 Sonic booms

Measurable, and uniquely **attributable**: ADS-B tells you which aircraft, so you get boom overpressure
against known geometry and Mach. Rare, but the kind of thing nobody has a casual dataset of.

### 6.4 Ground vehicles, rail and industry — the false positive as a product

The locomotive that wrecked a geolocation run (RESEARCH.md §2) is a measurable source in its own right:
Doppler gives speed, the spectrum gives classification. Traffic counting, rail activity, and local noise
monitoring all fall out, and the better you model these sources the better you reject them in the aircraft
pipeline. Dual purpose.

### 6.5 Infrasound (<20 Hz)

> Assessed in full in **[INFRASOUND.md](INFRASOUND.md)**. Short version: it is the only path above the 7 km propeller ceiling, and stratospheric sounding from microbaroms is real science — but it is a separate instrument sharing only the mast, and the wind-noise infrastructure it needs is physically large.

A different sensor — differential pressure, not a microphone (`Raspberry Shake BOOM` is the known
hardware) — but the same mast, power, network and clock, and the same wind-noise engineering. Opens
thunder and severe-storm infrasound (including the tornado literature), bolides, mining blasts, avalanches
and gravity waves. Judge it as a separate instrument that happens to share your site.

## 7. Acoustic hardware worth considering

| Addition | Buys | Notes |
|---|---|---|
| **Second mic, different height, same mast** | Ground-effect correction, and §5.1 | Same ADC — no sync problem. Cheapest real upgrade |
| **Calibrated measurement mic + calibrator** | Everything in §2 | The only product needing absolute level |
| **Compact array (~0.5 m, 4 mics)** | Coarse direction-of-arrival; **rejects ground sources**; separates simultaneous aircraft | Complements the km-baseline TDOA idea rather than replacing it |
| **Ultrasonic channel** | Bats | Separate mic and sample rate |
| **Better windscreen / lower siting** | Lowers `ε`, which per DESIGN §2 scales every product | Do this before anything else on this list |

## 8. The one radio item that still matters

Keeping this despite the acoustic focus, because the acoustic products depend on it: **receive your own
radiosondes.** `projecthorus/radiosonde_auto_rx` with one RTL-SDR decodes local launches and uploads to
SondeHub, giving a **co-located vertical profile** of temperature, wind and humidity — the exact quantity
the acoustic inversion is trying to infer, measured in situ over your own site, typically twice daily.

That converts "validate against a model" into "validate against an in-situ profile," which is what makes
§1 and the main atmospheric product defensible. US caveat: some US sites still launch on 1680 MHz, at the
edge of RTL-SDR coverage — check your nearest site before buying.

## 9. What I'd prioritise

1. **A better windscreen and lower siting.** `ε` multiplies into every single product. Nothing else on
   this list pays as broadly.
2. **§6.1 bioacoustics** — free, immediate, compounding, and it starts accumulating phenology value from
   day one, which no amount of later effort can backfill.
3. **§1 turbulence from scintillation** — the headline research idea, needs no new hardware, and the
   diurnal-cycle test tells you cheaply whether it's real.
4. **§5.1 second mic on the mast** — cheap, fixes a known systematic, and opens a novel inversion.
5. **§2 calibrated mic** if the noise-emission product appeals; it's the clearest "nobody has this data"
   gap, and it's straightforward rather than speculative.
6. **§8 radiosonde reception** alongside, because it's what makes any atmospheric claim publishable.

Then stop. §6.5 infrasound, cameras and arrays are each separate instruments with their own failure
modes, and the project earns them by getting one measurement validated first — it has not yet validated
any.
