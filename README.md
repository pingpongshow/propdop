# PropDop

**A passive acoustic observatory: a microphone pointed at the sky, plus ADS-B, used as a
measurement instrument.**

Aircraft propellers radiate a strong, steady tone — typically 60–400 Hz, the blade-pass
frequency — that Doppler-shifts as they pass. It is usually inaudible under wind and traffic
but unmistakable in a whitened spectrogram. ADS-B says exactly where each aircraft was, every
second, for free. Put those together at a surveyed microphone position and every flyover
becomes an over-determined physics measurement.

```
       listen continuously  ->  detect blindly  ->  fit the harmonic comb
                                                            |
                                   join to ADS-B  <----------+
                                          |
    RPM · prop health · wind · temperature · turbulence · non-cooperative aircraft
```

## What it measures

| Product | Precision | Notes |
|---|---|---|
| **Propeller RPM** | ≲1 %, i.e. ±5–22 RPM at 2,700 | No public data feed carries RPM |
| **Aircraft speed** | ~3 % on a steady pass | Checkable against ADS-B every pass |
| **Wind aloft** | ~1 m/s per good pass | The strong atmospheric product |
| **Temperature aloft** | ~1.7 °C per good pass | The weak one — needs averaging |
| **Turbulence** | `C²_eff` to 20–30 % per pass | From tonal scintillation; see caveats |
| **Prop/engine health** | anomaly detection, not diagnosis | Needs months of repeat passes |
| **Non-cooperative aircraft** | heard but absent from ADS-B | ADS-B Out is not required in US Class G below 10,000 ft |

Every product reports its own **confidence and contributing-pass count**. A product with too
little data says so rather than showing a number.

## Status — read this before believing anything

- **Everything above is validated on synthetic audio**, with known ground truth, by 180+ tests.
- It has been **deployed once**, on an indoor USB microphone with internet ADS-B. **No
  real-world detection has been confirmed yet.**
- Indoor is for proving the pipeline, not for producing soundings: walls attenuate, and mains
  hum at 60 Hz sits directly in the propeller band.
- The numbers above are what the implementation achieves on synthetics. Real passes manoeuvre,
  change power, and sit under real wind noise.

Several figures here were **wrong and were corrected** after being measured properly. The
corrections are recorded in the documents rather than quietly edited away.

## Quickstart

```bash
python3.13 -m venv .venv && ./.venv/bin/pip install -e '.[test,web]'
```

```bash
./.venv/bin/python -m pytest -q
```

Analyse a recording:

```bash
./.venv/bin/propdop detect recording.wav
```

Fit a single pass and read its RPM:

```bash
./.venv/bin/propdop fit pass.wav --blades 2
```

Run the whole thing — capture, detect, join, store — plus the web UI:

```bash
docker compose up -d --build
```

The dashboard is on port 8475. It needs a `config/station.json` with your surveyed position,
elevation and **geoid offset** — ADS-B altitude is height above the WGS84 ellipsoid while site
elevation is above mean sea level, and conflating them is a silent tens-of-metres error. See
`examples/station.json`.

## How it works

**Blind detection.** In log-frequency, a flyby's shape depends only on `v/c`; `f0` is a pure
vertical shift and `d/v` a pure horizontal scale. So a small template bank plus one 2-D FFT
correlation scores every `(f0, t0)` at once — and the harmonic comb becomes a *fixed vertical
pattern*, making the template comb-matched for free. It reaches −30 dB broadband SNR at 5 km.

**Rejecting the ground.** A locomotive at 50 m fits as a 101 m/s aircraft if you let it, so
detections are screened on fit quality, resolvability (`d/v ≥ 5 s` — a very close source
completes its whole sweep in two or three STFT frames) and speed (≥ 80 mph).

**The comb fit** yields per-harmonic frequency and amplitude tracks plus `ε`, the fractional
frequency residual — the master precision parameter, since required pass counts scale as `ε²`.

**The ADS-B join** is a two-way association, not a trigger. It supplies identity (hence blade
count, hence RPM), geometry to ~10 m (without which the atmospheric inversion is degenerate),
truth for an empirical error model, and — by recording passes that *should* have been audible
and were not — the denominator that makes "unexplained detection" mean anything.

**The atmospheric inversion** is closed-form: with speed known from ADS-B, the bias between
the acoustic speed and the radar speed *is* the sound-speed error. Temperature and wind then
separate by their angular signatures — isotropic versus `cos(bearing − wind bearing)`.

## The dashboard

No JavaScript framework, no CDN, no external assets — it works on a network with no outbound
route. The centrepiece is a polar display rather than a street map, because what governs
whether a pass is usable is its **range and bearing from the microphone**, not where the roads
are.

It shows range rings, live ADS-B traffic, and a **measured detection envelope**: per bearing
sector, the farthest pass actually heard. Hollow marks are passes that should have been
audible and were not, so a notch with hollow marks beyond it is a direction the site is deaf
in — a building, an embankment, a hillside. That is a measurement of shielding, and it only
exists because unheard passes get stored.

## Documents

| File | Contents |
|---|---|
| [BUILD.md](BUILD.md) | What is built, what is cut, and the retention policy |
| [DESIGN.md](DESIGN.md) | Architecture, detection paths, hardware, data model |
| [APPLICATIONS.md](APPLICATIONS.md) | The science, with derivations and honest precision |
| [PRIOR-ART.md](PRIOR-ART.md) | What already exists, and whether researchers would care |
| [SCINTILLATION.md](SCINTILLATION.md) | Turbulence from tonal scintillation, worked up |
| [NETWORK.md](NETWORK.md) | What sharing reports between stations would add |
| [EXPANSION.md](EXPANSION.md) | Acoustic directions beyond the current scope |
| [INFRASOUND.md](INFRASOUND.md) | Infrasound assessment, and why it is a separate instrument |
| [RESEARCH.md](RESEARCH.md) | Survey of the acoustic-geolocation work this grew out of |

## Origins, and what this deliberately is not

This grew out of the acoustic-geolocation technique demonstrated by **colsto** in late 2026 —
recovering *where* a recording was made from the Doppler curves of passing aircraft — and the
open reimplementations that followed. [RESEARCH.md](RESEARCH.md) surveys them honestly,
including the published blind trials that failed.

**PropDop inverts that problem and does not implement geolocation.** Fixing the microphone and
solving for the aircraft and the atmosphere is the well-posed direction: geometry becomes a
known input, ADS-B supplies continuous ground truth, and the instrument validates itself from
the first pass. Aircraft positions are public and broadcast in the clear; a person's recording
is not.

## Licence

**PolyForm Noncommercial License 1.0.0** — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Any noncommercial purpose is permitted, explicitly including research, teaching, personal
study, and use by educational, public-research, charitable and government organisations.
Commercial use is not granted by this licence. The author retains ownership and all rights not
expressly granted.
