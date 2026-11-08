# Acoustic geolocation from aircraft Doppler curves — state of the art and a build plan

> **Status: background. Geolocation is deferred.**
> The project's purpose is now a passive acoustic aircraft and atmosphere observatory — see
> [README.md](README.md) and [DESIGN.md](DESIGN.md). This document remains the survey of the
> geolocation technique: the physics (§1), the six open implementations and their honest field
> results (§2), why jets are acoustically useless (§2.1), and the data sources (§5). Those parts
> are still the factual basis for the observatory. The build plan in §4 and §7 is superseded.

Research note, 2026-11-07. Target: run this locally on an Apple M2 Max (12 cores, 32 GB, ~400 GB free),
with a Linux server available for the heavy searches.

---

## 1. The physics

A propeller or turboprop aircraft radiates a strong, steady narrowband tone at its **blade-pass
frequency** — shaft RPM × blade count — typically **60–400 Hz**, plus a harmonic comb. The tone carries
for kilometres because low frequencies suffer little atmospheric absorption. It is usually *inaudible*
under wind and traffic but stands out clearly in a per-bin-whitened spectrogram.

A source emitting `f0` from position `p(t_e)`, heard at `x`:

```
heard time    t_r = t_e + |p(t_e) − x| / c
heard pitch   f(t_r) = f0 · c / (c + v_r(t_e))        v_r = radial speed, + receding
```

The **emission-time (retarded-time) form matters.** At 6 km slant range the sound of closest approach
arrives ~17 s after the aircraft was actually there, and the curve is asymmetric. A naive
`f = f0·c/(c+v_r(t_r))` model is wrong by enough to move an answer kilometres.

For a straight constant-speed pass at speed `v` and closest-approach (CPA) slant distance `d`:

- **total pitch drop** `f0·c/(c−v) → f0·c/(c+v)` sets **v**
- **steepness at CPA** `≈ f0·v²/(c·d)` sets **d**
- **curve centre** sets the heard CPA time `t0r = t0e + d/c`

So one curve yields `(f0, v, d, t0r)`. Match that to one real ADS-B track and you get a **line of
candidate ground points beside the track** — each tied to a different recording clock offset. **One
flyover is not enough**: the solution is mirrored across the flight path, and position-along-track
trades off against the clock. **Two flyovers on different headings intersect to a point and fix the
clock.** Three or more are what make an answer provable.

### Why ADS-B closes the loop
adsb.lol publishes a global per-UTC-day archive of every transponder-equipped aircraft at ~1 Hz, free,
ODbL, back to 2023. So for any date you can enumerate every candidate aircraft and forward-model the
exact curve it would have produced at any candidate ground point.

---

## 2. Who has built this, and what they actually achieved

The source is **colsto**'s video *"I discovered a sound that leaks your exact location"* (early Nov 2026,
`youtube.com/watch?v=UytYrhYLOKI`, raw 1-hour reference audio at `youtu.be/4CxVxuMVkMc`). He released no
code — months of work, ~5,000 lines, 50 GB of data, Flightradar24 (paid) for flight data. Six open
reimplementations appeared within days. All use adsb.lol instead.

| Project | Lang | Strongest honest result | Mac-friendly? |
|---|---|---|---|
| [mjshiggins/dopplertrack](https://github.com/mjshiggins/dopplertrack) | pure Python (numpy/scipy/pandas) | **2 recordings located CONUS-wide with no region prior**: Oyster Bay 32 m (rank 1 of 9,669 flight pairs), Florida eagle cam 627 m (rank 1 with extra events). Ships a *statistical verdict*. | **Yes** — no native/CUDA deps |
| [Hexay/dopplerprint](https://github.com/Hexay/dopplerprint) | Rust + Python wheels | **Livermore benchmark: right aircraft 34/40, median error 207 m**, 27/40 within 500 m. Oyster Bay 79 m. | **Yes** — prebuilt wheels, no ffmpeg needed |
| [digitalis-p/doppler-locate](https://github.com/digitalis-p/doppler-locate) | Python + CUDA/OpenMP | Oyster Bay rank 1 of 1,060 pairs, **67 m**, heat map peak 13 m. Best atmospheric modelling (GFS winds aloft). GTK desktop app. | Linux-only — **run on the 5090 server**, see §3 |
| [Dicklesworthstone/franken_doppler_finder](https://github.com/Dicklesworthstone/franken_doppler_finder) | Rust | Reference site high-confidence 263–270 m, radius ~380 m. Most rigorous **gating/refusal** design (5 gates, high/med/low, exit code 1 for "no location"). Dev'd on Apple silicon. | **Yes** |
| [Jackychen8/prop-geolocate](https://github.com/Jackychen8/prop-geolocate) | Python | **Simulation only** — 440-trial Monte Carlo. Nominal median 12 m; realistic mix median 37 m, 85 % <100 m. No field result yet. | Yes |
| [Bodila51/colsto-leak-and-mask](https://github.com/Bodila51/colsto-leak-and-mask) | Python | **The valuable negative.** 7 pre-registered blind webcam runs, SHA-256-sealed truth: **0 located.** | Yes |

### The sober read

The headlines ("100 m from any video") are not what the open evidence supports.

**What is solid:**
- The physics and the forward model are right, and reproducible. Multiple independent teams converge on
  the same aircraft pair and the same ~70 m point for colsto's recording.
- Given a **known** microphone position, identifying *which* aircraft you're hearing is reliable
  (Midway: 143 kt vs 139 kt measured, CPA within 0.1 s, 19× margin over runner-up among 35 aircraft).
- Robustness to codecs is genuinely good: MP3 32k, phone AAC chain, spectral noise suppression and
  Opus 16k VoIP all stay at 11–13 m median in simulation. Harmonics up to 1.5 kHz survive; losing the
  fundamental doesn't matter.

**What is not:**
- **Blind location mostly fails.** colsto-leak-and-mask's 7 sealed blind webcam runs: zero located.
  One looked like a 749 m hit until a control run with audio shifted 30 min landed 1,014 m away with a
  *higher* score — i.e. the answer came from where aircraft fly, not from the sound.
- **Two events are never conclusive.** dopplertrack's own null-distribution test: Oyster Bay's 6.2 %
  margin over the runner-up gives p = 0.23 — a wrong winner leads by that much one time in four.
  Both reference recordings only became "located" once a 3rd–5th flyover was added.
- **Everything hinges on event measurement quality, not search.** doppler-locate reaches Oyster Bay at
  67 m with hand-measured events; run fully automatic end-to-end by a third party, its own detector put
  event 2's tone at 140.7 Hz instead of ~134 Hz and rank 1 moved **137 km** to Connecticut.
- **The straight-pass seed fit is badly biased on real passes.** A faint Oyster Bay curve: fit says
  41 m/s at 750 m; truth was 70 m/s at **8.2 km** — a factor of ten in distance, enough that default
  gates discard the true aircraft before scoring.
- **Site dependence is severe.** Of 10 field recordings franken tried, 4 rural sites gave no location at
  all (0–3 non-jet passes in reach) and 2 busy sites failed on audio (one webcam high-pass filtered at
  ~125 Hz, removing the fundamentals; one wind-saturated mic). Low-altitude GA often isn't in adsb.lol
  at all — no nearby feeders, and seaplanes/vintage aircraft often carry no transponder.
- **Jets barely work** except low and close to a runway — see §2.1.
- **Archived streams drift.** An Atlanta railcam stream had silently lost 107 s mid-file, which put
  events on either side of the gap on different clocks and actively buried the true answer.

### Calibration of expectation

| You have | Realistic outcome |
|---|---|
| ≥3 clean prop flyovers, known date, known ±minutes clock, region narrowed to a state | tens to a few hundred metres, with a defensible confidence grade |
| Exactly 2 flyovers, no region prior | may well find it, cannot prove it; expect ambiguity verdict |
| 1 flyover + known time | ~1–3 km band, plus a mirror solution |
| Random webcam / phone clip, <60 s, suburban | likely nothing. 30 s clips: 65 % within 100 m in *simulation*, far worse in field |
| Heavily noise-suppressed, high-passed, or silent audio track | nothing |

### 2.1 Why jets are the wrong target — and what to do with them anyway

Three independent reasons, and they compound:

**1. The sound is mostly broadband.** Jet mixing noise is a roar, not a tone. Doppler tracking needs a
narrowband ridge to follow. A propeller's blade-pass tone is one of the most strongly tonal sources in
the outdoor environment; a jet's dominant output is the opposite.

**2. The tones that do exist are far too high to survive the trip.** The one well-documented jet case —
a 737 MAX 8 departing over the Midway webcam — shows a fan-tone stack at **1.4 / 2.8 / 4.2 / 5.6 / 7 kHz**.
Atmospheric absorption is brutally frequency-dependent (ISO 9613-1, 15 °C, 70 % RH):

| Tone | dB/km | loss over 5 km | loss over 10 km |
|---|---|---|---|
| 63 Hz | 0.10 | **0.5 dB** | 1.0 dB |
| 100 Hz (prop fundamental) | 0.25 | **1.3 dB** | 2.5 dB |
| 200 Hz | 0.82 | 4.1 dB | 8.2 dB |
| 400 Hz (prop harmonic) | 1.93 | 9.7 dB | 19.3 dB |
| 1.4 kHz (jet fan) | 5.65 | **28 dB** | 57 dB |
| 2.8 kHz | 14.5 | **73 dB** | 145 dB |
| 4.2 kHz | 29.0 | **145 dB** | 290 dB |
| 7 kHz | 74.1 | **370 dB** | 741 dB |

A 100 Hz prop tone arrives from 10 km down 2.5 dB. The same geometry costs a 4.2 kHz fan tone 290 dB —
it does not arrive at all. This is *before* spherical spreading, which hits both equally. That 737 was
detected at **13 m horizontally and ~130 m up**; at 5 km it would have been silent.

*(Computed from the ISO 9613-1 / ANSI S1.26 formula. Spot-checked against commonly cited octave-band
tables, it agrees within ~0.9–1.5×. The argument rests on the frequency **trend**, which spans three
orders of magnitude across this table, so a 1.5× factor anywhere does not touch the conclusion.)*

**3. The geometry is wrong too.** Jets fly high and fast. High means far (faint, and the CPA geometry
flattens, so `d` is poorly constrained). Fast means the sweep through CPA is brief. And fan tones track
engine power, so a departing jet changing throttle violates the constant-`f0` assumption the whole fit
rests on.

**The implementations agree.** 4 of 5 exclude jets: dopplertrack's `fetch` "drops known jets", franken
drops them at ingest with no option to re-enable, dopplerprint keeps but flags them "since their tones
are rarely visible", and colsto's original used `exclude_engines = ["J", "R"]`. Worse, most prop-tuned
pipelines **physically cannot see** a jet fan tone: franken decodes at a 4 kHz sample rate with a
35–1100 Hz detection band, so a 1.4–7 kHz stack is above Nyquist. dopplertrack whitens 40–450 Hz.
Jets aren't excluded by a flag there — they're outside the instrument.

And the one blind test with jets overhead failed exactly as predicted: the Midway runs landed 21–28 km
off, because "the loudest thing is broadband roar, and the clean tonal slide the method needs is short
and weak."

**The cost of including them is real.** doppler-locate's jets-included run: 2.9× the flight pairs (68 %
of them involving a jet), 12.5× the candidates, 4× the search time, 2.8× the memory. The answer didn't
change — truth still ranked 1st at 67 m, best jet pair 8th. So it's pure cost.

**But there's one thing jets are genuinely good for: negative evidence.** If a candidate location would
have had a jet pass close overhead and the recording shows no trace of it, that's evidence *against* the
candidate — and jets are loud and broadband enough that their absence is informative even when their
tones are useless. franken notes that because it drops jets at ingest, they "can neither explain an event
nor count as an unheard close pass," which forfeits this for free.

**So: keep jets in the ADS-B index, exclude them from event matching.** The 12.5× candidate explosion
comes entirely from letting jets *explain* events. Carrying them as veto-only candidates costs almost
nothing — a slightly larger flight table — and strengthens the unheard-pass veto (§4.2 item 6) that kills
busy-airport false positives like the Miami 749 m case. I haven't seen any implementation do this.

---

## 3. Local hardware fit

Your M2 Max: **12 cores, 32 GB, 397 GB free.** Present: `ffmpeg 9.0.1`, `cargo/rustc 1.93`, `git`,
`gh 2.87`, `python3 3.14.7`, Homebrew. Missing: `uv`, `yt-dlp` (one brew command).

Cost drivers:

| Item | Cost |
|---|---|
| adsb.lol one UTC day, `prod` | ~3.8 GB download, cannot be fetched partially (unindexed tar). **About 37 s on a gigabit link** |
| adsb.lol one UTC day, `mlatonly` | ~1.1 GB (extra aircraft, worth adding) |
| dopplertrack day index (CONUS, props only) | 6.2 GB on disk, ~3 min to build; streams the 4 GB without storing it |
| doppler-locate ingest | ~1.2 GB parquet/day, needs ~26 GB RAM for CONUS search |
| CONUS-wide dopplertrack search, 16 threads | join 10 s → lens 71 s → **scoring ~16 min** |
| 60 min of audio, flyover detection | 70 s on 32 cores / a few min on 12 |

**Verdict: run everything on the server.** The Mac loses on all three counts that matter — 32 GB of RAM
is tight for a CONUS search that needs ~26 GB with nothing else running, 400 GB can't hold a meaningful
ADS-B archive, and it has no CUDA. The server's RAM headroom buys the things that actually matter for
this project: day indexes (6.2 GB each) staying in page cache across runs, multi-day search windows, the
harmonic-comb scorer's extra work per candidate, and **several benchmark cases scored in parallel** —
which is the difference between a 40-case regression run taking an afternoon or a week. A containerised always-on host with ample RAM and disk is the right target. doppler-locate becomes the production scorer: published 4090 numbers are a **~80 s CONUS
search on GPU vs ~126 s CPU** and **40–120 s to score 85 k candidates vs ~350 s** on numba, and the
scorer is memory-bandwidth bound (see §4.2 item 2) so the 5090's ~1.8× bandwidth should beat that.

The deployment target is a containerised always-on host; the architecture and its
constraints are in **[DESIGN.md](DESIGN.md)**.

---

## 4. What I'd build — staged plan

### Stage 0 — reproduce the known answer (half a day)

Don't write anything yet. Establish a working baseline and a ground truth you trust.

```bash
brew install uv yt-dlp
```

```bash
pip install dopplerprint && dopplerprint serve --open
```

dopplerprint is the fastest smoke test: prebuilt macOS wheel, native audio decode, browser UI. Then the
research platform:

```bash
git clone https://github.com/mjshiggins/dopplertrack && cd dopplertrack && uv sync && uv run pytest
```

Then reproduce Oyster Bay end to end — the one case everyone agrees on. Pull colsto's raw audio
(`youtu.be/4CxVxuMVkMc`), run `spectrogram` → `detect` → `fit` → `fetch 2026-09-08` → `search`, and
confirm you land ~32 m from the published point. **If you can't reproduce this, nothing downstream means
anything.**

### Stage 1 — get a real validation corpus (1–2 days)

This is the step every project skimped on, and it's the highest-leverage thing you can do.

**Pathak 2026, Zenodo record 23424278** — *"Aeroplane data full analysis and raw files"*, CC-BY 4.0,
~130 MB. 40 smartphone recordings of aircraft passing over Livermore, CA in May 2026, each with:
WAV audio, extracted frequency–time CSV with UTC timestamps, the aircraft's ADS-B track as KML, and a
per-case analysis PDF. Grouped by whether speed and height were constant or varying.

That is 40 known-answer cases with real audio, real tracks and real clocks — plus 10 known-position
public webcams from franken's field tests and colsto-leak-and-mask's sealed webcam set. Build a harness
that scores any pipeline against all of it and reports **error distribution and false-positive rate**,
not single best cases. Steal colsto-leak-and-mask's protocol: seal truth as a SHA-256 commit *before*
solving, and always run a **shifted-audio control** (same audio, timestamp moved 30 min) — if the
control scores as well as the real run, your answer came from traffic density, not from sound.

### Stage 2 — the improvements worth making, ranked

These come from the failure analyses, and they're mostly unclaimed.

1. **Score the harmonic comb, not a single curve.** Real recordings carry 3–4 harmonics of the same
   propeller at fixed integer ratios. Every implementation scores one curve. A wrong candidate only has
   to fit one line; it would have to fit the whole comb. dopplertrack names this as its top roadmap item
   and nobody has done it. Likely the single biggest margin improvement available.

2. **Fix radial velocity from ADS-B.** Today it comes from differentiating reported positions, so a
   dropped or glitched position report puts a **kink** in the modelled curve that the audio doesn't have.
   This is exactly why the Atlanta railcam's true answer lost to a Scottsdale pair with a smoother
   track. Instead: smooth trajectories (spline/Kalman in ECEF) before differentiating, *or* take velocity
   from the reported ground speed + track + vertical rate fields directly, and treat a track's
   report-gap density as a per-flight reliability weight.

3. **Drop the straight-pass fit from the gating path.** It's the weak link — 10× distance error on
   non-straight passes. Use it only to seed, open the gates wide (±8 % f0, ±8 s offset, 0.4–2.5× in
   distance is still too tight), and let the trajectory-modelled curve do all discrimination. Better:
   skip the parametric fit entirely and match the whitened ridge against forward-modelled curves.

4. **Per-event clock offsets with a hierarchical prior.** One shared clock breaks on any archived stream
   with a dropout. Give each event its own offset with a tight prior around a shared value, and let a
   large per-event deviation be evidence of a stream gap rather than a reason to reject the truth.

5. **Calibrate the confidence properly.** dopplertrack's exponential-tail null is the right idea — the
   thousands of wrong flight pairs you scored *are* your null distribution, produced for free. But its
   p < 0.1 "located" threshold produced 2 false positives in 18 no-right-answer searches. Fit the null
   on a large held-out set and report a calibrated posterior probability, not a margin. Adopt franken's
   discipline of **refusing to answer** (exit code 1) when gates fail — a tool that returns a winner for
   audio recorded 150 km from the search box is worse than useless.

6. **Negative evidence / unheard-pass veto.** If a candidate point would have heard a close tonal
   aircraft that left no detection, that's strong evidence against it. franken does this; it's cheap and
   powerful, and it's what kills "busy airport" false positives like the Miami 749 m case.

7. **Replace the detector with a learned one.** Detection, not search, is where end-to-end runs die
   (140.7 Hz vs 134 Hz → 137 km error). Matched-filter banks over (speed × CPA distance) templates work
   but under-resolve f0. With the Livermore corpus plus synthetic rendering from real tracks you have
   enough labelled data to train a small CRNN ridge-tracker, and to put **calibrated uncertainty on f0**
   — which is what the gates actually need.

8. **Add the `mlatonly` archive.** ~1.1 GB/day extra, catches multilaterated aircraft that aren't in the
   `prod` ADS-B dump. More candidate aircraft at low altitude, which is exactly where the method is
   starved at rural sites.

9. **Report a posterior, not a point.** Grid-max plus heat map understates the real structure: the
   posterior is genuinely **bimodal** for a single pass (the mirror pair). Emit a proper likelihood
   surface and name the mirror explicitly instead of picking the brighter lobe and hoping.

Not worth it yet: atmospheric ray refraction (a straight-ray height-averaged sound speed with horizontal
wind from Open-Meteo or GFS is already well inside other error terms), and GPU work before the harmonic
comb and the detector are fixed.

### Stage 3 — the defensive side

The interesting output isn't a locator, it's a **measurement of exposure**. With the Stage 1 harness you
can answer questions nobody has: what fraction of outdoor recordings in a given area contain two
locatable flyovers? How long must a clip be? What does it take to remove the leak?
colsto-leak-and-mask's "safe copy" (narrow notch filtering of plane tones + mains hum, metadata strip)
is a start but is unbenchmarked — you'd have the rig to actually test whether it survives a determined
re-run.

---

## 5. Data sources (all free)

| Need | Source | Licence |
|---|---|---|
| Historical ADS-B | [adsb.lol `globe_history_2026`](https://github.com/adsblol/globe_history_2026) GitHub releases, ~3.8 GB/day `prod` + 1.1 GB `mlatonly`; published ~03:30 UTC next day; archive starts 2023 | ODbL 1.0 |
| Aircraft type → engine class | [tar1090-db](https://github.com/wiedehopf/tar1090-db) `icao_aircraft_types2.js` (ICAO Doc 8643, e.g. `L1P`) | — |
| Terrain | AWS Terrain Tiles (Terrarium), USGS 3DEP, or Copernicus GLO-30 | public domain / Copernicus |
| Geoid (GNSS alt → MSL) | NOAA NGS GEOID API, or EGM96 grid from cdn.proj.org | public domain |
| Winds / temperature aloft | Open-Meteo pressure levels, or NOAA GFS 0.25° on AWS; IEM radiosondes | free |
| Validation corpus | [Zenodo 23424278](https://zenodo.org/records/23424278) — 40 Livermore passages, audio + ADS-B + truth | CC-BY 4.0 |

Observer height must be in the ADS-B geometric datum (WGS84 ellipsoid) — elevation above sea level
*plus* geoid height. **50 m wrong ≈ 30 m position error**, so get the geoid right.

---

## 6. Use

Every project in this space ships the same note, and it's the right one: run it on your own recordings,
on material published for the purpose (colsto's challenge), or on public webcams whose owners publish
their location. The point of building it is to measure and remove the exposure, not to locate people who
didn't consent.

---

## 7. Recommended next action

Server-hosted, containerised, with a web UI. The build order is in [BUILD.md](BUILD.md).

The Mac's only remaining role is as a browser. If you want a 10-minute sanity check on the physics before
committing to any of it, `pip install dopplerprint && dopplerprint serve --open` runs on macOS from a
prebuilt wheel with no ffmpeg and no CUDA — but it's a smoke test, not part of the system.
