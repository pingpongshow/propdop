# Validation against real recordings

Everything else in this repository is validated on audio the project generates itself.
`synth.py` renders passes with the same retarded-time model `fit.py` inverts, so the test suite
proves the inversion is self-consistent — **not** that the model resembles an aeroplane. If the
model is missing something real, the renderer and the fitter are wrong together and every test
still passes.

This document records the first run against audio the project did not make.

## The dataset

**AeroSonicDB (YPAD-0523)** — Downward & Nordby 2023, [arXiv:2311.06368](https://arxiv.org/abs/2311.06368),
[Zenodo record 8371595](https://zenodo.org/records/8371595) v1.1.1, CC BY-NC 4.0. Labelled
recordings of low-flying aircraft at Adelaide Airport, plus six continuous one-hour environment
recordings carrying a per-five-second aircraft/no-aircraft label grid.

The dataset is **not redistributed here** and is not in this repository.

### Two traps in the controls, both of which would have given a wrong answer

1. **The curated no-aircraft clips are the wrong length.** They are 10 s, while the propeller
   clips run 18–60 s (median 51 s). Scores from different-length windows are not comparable: a
   longer window gives the template bank more chances to peak, and `detect` skips templates
   wider than the window outright. Measuring false positives on 10 s clips reports a rate that
   is **too low**.
2. **The hour-long environment recordings are not aircraft-free.** 762 of their 4,320 labelled
   frames contain an aircraft — unsurprising next to a runway. Slicing them naively into
   negatives would count genuine detections of unlabelled aeroplanes as false positives,
   reporting a rate that is **too high**.

So the primary control is 274 non-overlapping **51 s windows, each verified aircraft-free frame
by frame** (233 minutes). Same length as the positives, and a false positive there cannot be a
real aeroplane. The 10 s clips are reported alongside as the weaker measurement.

A third trap was hit and corrected mid-run: `detect` only *returns* candidates above its
`min_score`, so a threshold sweep performed on its output with the deployed `min_score=20` is
an artefact of a list already cut at 20. The harness now collects from 4.0 and the scorer
applies thresholds afterwards.

## Results

2,135 clips scored (8 kHz, 50–300 Hz, four harmonics, `max_detections=8`), zero errors. The
measurement was run twice: once at the original constants, then again after they were
recalibrated against it.

| group | n | before `20 / ε 0.02` | after `18 / ε 0.03` + gates |
|---|---|---|---|
| propeller | 151 | 61.6 % | **68.2 %** |
| — turboprop | 114 | 72.8 % | **80.7 %** |
| — piston | 37 | 27.0 % | 29.7 % |
| no-aircraft, 51 s label-verified | 274 | **0** | **0** |
| no-aircraft, 10 s curated | 1,236 | 0 | 0 |
| turbofan fired on | 474 | 0 | 2 |

The two physical-plausibility gates below cost 0.7 points of detection (68.9 → 68.2 %) and
remove a class of result that was not merely wrong but unpublishable.

The two turbofans are the cost of the lower threshold: jet peak scores reach 20.8, so at 18 a
couple fit a comb to broadband noise. They are not false positives in the "unexplained
detection" sense — they join to their own ADS-B track — but any RPM reported for them is
meaningless. ADS-B engine type is the natural downstream filter.

Jets firing on nothing is correct rather than a miss: a turbofan is broadband, and this
detector is a harmonic-comb matched filter for propellers.

### The threshold was calibrated against the wrong noise

`MIN_TEMPLATE_SCORE = 20` was set from pure pink noise, which peaked at **8.9**. Real
background peaks at **17.8** (label-verified windows) and **20.8** (jet clips).

**Real noise reaches twice as high as the synthetic noise the threshold was set against.** The
value 20 happens to be about right, but the margin above the noise ceiling is 2.2 points, not
the ~11 points the pink-noise calibration implied.

| threshold | TPR | FPR | TPR (no screen) | FPR (no screen) |
|---|---|---|---|---|
| 8.9 | 74.8 % | 0.0 % | 96.0 % | 22.3 % |
| 12 | 72.2 % | 0.0 % | 89.4 % | 5.5 % |
| 15 | **69.5 %** | **0.0 %** | 83.4 % | 1.5 % |
| 20 *(deployed)* | 61.6 % | 0.0 % | 68.9 % | 0.0 % |
| 30 | 32.5 % | 0.0 % | 35.8 % | 0.0 % |

**The threshold was changed to 18**, set just above the measured field ceiling — which is the
principle the original value only appeared to follow. Lower values detect more (74.2 % at 15),
but 15 is *below* the ceiling, so around 1.5 % of clean windows produce a candidate and
`plausible_aircraft` becomes the only thing rejecting them. For a station whose headline
product is "heard but absent from ADS-B", screening should be defence in depth rather than the
load-bearing member. Below 12 that load grows fast (5.5 %, then 22.3 %).

Note 0/274 bounds the true false-positive rate only to about 1.1 % at 95 % confidence. A
noisier site should measure its own ceiling and raise the threshold accordingly.

### Piston aircraft are the real weakness, and it is not fixable by tuning

This is the most consequential finding for a backyard station, where light piston singles and
twins are most of the traffic — not regional turboprops.

**A hypothesis recorded because it was wrong.** The rejection reasons were dominated by
`poor fit (epsilon 2.5–4.5 %)`, so the obvious read was that the epsilon gate was throwing real
piston aircraft away. Loosening it moved pistons by 2.7 points (27.0 → 29.7 %) while moving
turboprops by 8.8 (72.8 → 81.6 %). The gate was indeed too tight — but for turboprops.

What the measurement actually shows:

| engine | peak score p50 | p90 | misses that never reached the threshold at all |
|---|---|---|---|
| Turboprop | 28.3 | 36.3 | 16 / 21 (76 %) |
| Piston | **15.3** | 31.0 | **25 / 26 (96 %)** |

**Piston aircraft are signal-limited, not gate-limited.** Their median peak score of 15.3 sits
*below* the 17.8 field-noise ceiling: the comb is not above background in the first place. No
threshold or gate can recover them without also accepting background, so this bounds what the
instrument can do on light aircraft rather than describing something to tune. Altitude does not
explain it either (piston median 2,950 ft against turboprop 3,275 ft).

Among the minority of misses that *did* clear the threshold, the remaining rejection is
`transition unresolvable` (5 turboprop, 1 piston) — passes too close and fast to resolve.

### Epsilon is worse than the synthetics suggested

| | synthetic | real (n=139) |
|---|---|---|
| epsilon | 0.014 – 1.4 % | p10 0.72 %, **median 1.22 %**, p90 1.79 % |

Real `ε` sits at the top of the synthetic range. Required pass counts scale as `ε²`, so against
the 0.5 % synthetic median this is **5.9× as many passes** for any stated precision. Every
pass-count figure in APPLICATIONS.md derived from synthetic `ε` should be read as optimistic by
roughly that factor.

### What held up well

**The comb fit measures real propellers.** Fitted `f0` clusters tightly and by type, and the
implied RPM matches published figures:

| type | n | median f0 | range | blades | implied RPM | published |
|---|---|---|---|---|---|---|
| DH8C | 18 | 61.0 Hz | 59–62 | 4 | 915 | 900–1,200 |
| SF34 | 21 | 91.4 Hz | 83–95 | 4 | 1,371 | **1,384** |
| DH8D | 6 | 86.3 Hz | 83–90 | 6 | 863 | 850–1,020 |
| BE20 | 11 | 128.1 Hz | 115–136 | 4 | 1,922 | 1,900–2,000 |

The Saab 340 figure is the strongest single result here: **91.4 Hz measured against 92.3 Hz
theoretical, a 1 % error, over 21 independent passes.** Dash 8-300 holds 59–62 Hz across 18
different aircraft.

**No octave errors.** Zero suspected 2× locks across 83 detections. The harmonic product
spectrum bootstrap (DESIGN.md 3.1b) works on real audio, which is the failure it was added for.

**Ground-source screening was never exercised.** No false positive fired, so
`plausible_aircraft` rejecting real road and rail sources remains untested on real audio; the
dataset has no labelled ground vehicles.

Three rows report 2,512–2,640 RPM for `BE20` with propeller model `HC-B3TN-3`. Both BE20
subgroups fit ~128 Hz, which is correct for four blades at ~1,920 RPM and impossible for three
on a PT6 limited to 2,000 RPM, so this is very likely a dataset metadata error rather than a
fit error.

## Reproducing

The harness and scorer are deliberately separate programs: the harness records raw per-clip
outcomes and decides nothing, the scorer reads only that JSON. The easiest way to make a
validation like this lie is to adjust the measurement until the detector looks good, so the
threshold sweep is reported at nine values rather than at whichever one wins.

They are not committed, because they depend on a dataset layout this project does not control.
`scripts/` in a future revision is the right home if this becomes routine.


## What changed as a result

| constant | was | now | why |
|---|---|---|---|
| `detect.MIN_TEMPLATE_SCORE` | 20.0 | **18.0** | 20 came from pink noise; set just above the measured field ceiling of 17.8 |
| `fit.MAX_EPSILON` | 0.02 | **0.03** | 0.02 came from synthetic `ε`; 0.03 is set just under the 3.91 % of a misfitting car |
| `detect.FIELD_NOISE_CEILING` | *(did not exist)* | **17.8** | real background reaches twice the synthetic figure |
| `score_verdict` | 3 bands | **4 bands** | a score between the two ceilings is not evidence of a tone |

`plausible_aircraft` also now resolves its thresholds at call time rather than binding them as
default arguments, so a site can recalibrate once on the module and have `detect` pick it up.

**A regression the dataset could not have caught.** Loosening `MAX_EPSILON` to 0.045 — which the
aircraft sweep said was free — broke ground-source rejection: a car at 30 m/s and 120 m misfits
as "48 m/s at 359 m", clearing both the speed and resolvability gates, leaving `ε` as the only
check. AeroSonicDB has no labelled ground vehicles, so only the synthetic suite saw it. The two
test sets are complementary, and neither alone is sufficient.

## What the live station found that the dataset could not

Within hours of running with the recalibrated constants, the deployed station produced its
first two blind detections. One of them was this:

> **A note on times.** Every station timestamp quoted in this section and the next is **UTC**,
> which is what a Raspberry Pi renders out of the box and what the database returned at the
> time. That host has since been set to its operator's own zone, so re-running these queries
> today returns local time instead. The stored values are unix epoch and unchanged; only the
> rendering moved. The dashboard now labels the zone for exactly this reason.


    04:06:02   f0 89.09 Hz   211.8 m/s   slant 16,423 m   unmatched to ADS-B

It would have been published as a **non-cooperative aircraft** — the headline product. It is
not one. 211.8 m/s is 412 knots, and 16.4 km is twice the range this instrument itself
considers audible. Two faults, both invisible to a dataset of labelled clips:

1. **The fit was pinned at its own upper speed bound.** `fit_comb` bounds speed at 250 m/s, and
   a parameter resting on a bound is where the search stopped, not a measurement. The same
   failure shows in AeroSonicDB as accepted "propeller" detections at exactly 250.0 m/s.
   `plausible_aircraft` now rejects fits within 2 % of the bound.
2. **The two halves of the join disagreed about audibility.** `audible_candidates` excluded
   anything past 8 km from "should have been heard", while `plausible_aircraft` happily
   accepted a detection at 16.4 km and called it unexplained — the instrument contradicting
   itself. `physics.AUDIBILITY_RADIUS_M` is now shared by both, with a test asserting they
   agree.

No speed cap was added instead: a Piaggio Avanti cruises at 207 m/s, so any cap tight enough to
catch a 211 m/s misfit would reject real turboprops.

This is the argument for running both test sets. The synthetic suite caught the ground-source
regression that AeroSonicDB could not see; AeroSonicDB caught the threshold and epsilon
miscalibration the synthetics could not; and the live station caught an incoherence neither
corpus would ever contain, because only a real deployment produces an unmatched detection with
nothing to check it against.

## The fault that made every number on this page inapplicable to the station

Three hours later the station had thirteen detections, **every one of them unmatched**, and a
detection rate of 0.0 %. Four candidates were stored as `poor fit (epsilon 3.05–3.67 %)`.

The near-miss audio retention added in the previous round is what made this diagnosable: the
clips behind two of those rejections were on disk. Re-running the detector on them did not
reproduce the rejection. It produced `ε` of **0.20 %** and **0.63 %** — against 3.13 % and
3.67 % recorded live, on the same audio.

The first hypothesis was an octave slip. `detect` derives the fit band from the template-stage
`f0` as `(f0·0.6, f0·1.7)`, so a sub-octave lock would put the true fundamental outside the
band the fit is allowed to search, and one clip's filename said 81 Hz where the re-run found
165 Hz. **That was real and it was not the cause.** Refitting at the sub-octave band still gave
geometry that matched ADS-B; the octave does not decide this.

The cause is that **`fit_comb`'s answer depended on `t_offset`**. Same audio, same band:

| `t_offset` | f0 | speed | cpa | ε |
|---|---|---|---|---|
| 0 … 1e6 | 162.6 Hz | 69.5 m/s | 1,941 m | **0.50 %** |
| 1e8 | 162.6 Hz | 69.5 m/s | 1,936 m | 0.50 % |
| 1.794e9 *(live)* | 172.0 Hz | **23.4 m/s** | **197 m** | 1.24 % |

`t_offset` labels the time axis. It must shift `t0_heard` and change nothing else.

`fit_straight_pass` calls `least_squares` with the default `x_scale=1.0`, so the step test is
`‖dx‖ < xtol·(xtol + ‖x‖)` over the whole parameter vector `[f0, speed, cpa, t0]`. With an
absolute unix time as `t0`, `‖x‖ ≈ 1.79e9` and that tolerance becomes **≈18** — the solver
stops after one or two steps. It then returns `status=3`, which is success, so every caller
saw `converged=True` on a fit that never moved. `comb_score` was worse: it was handed
clip-local ridge times together with an absolute `t0`, so every `t − t0` it formed was out by
1.79e9, and the contrast it reported — including the `-0.19` logged beside one of these
rejections — was noise.

### What it cost, measured against truth

The Cirrus SR22T at 05:15 UTC, stored as *"poor fit (epsilon 3.13 %)"*:

| | speed err | slant err | Δt | chi |
|---|---|---|---|---|
| as flown live | −61.3 % | −91.8 % | −7.1 s | 7.53 → discarded |
| local time frame | **+0.3 %** | **−2.4 %** | **+0.1 s** | **0.11 → joins** |

Against `MAX_CHI = 3.0`. The acoustic fit reproduces the ADS-B speed to three parts in a
thousand and the slant range to 2.4 %, and the instrument threw it away.

### Why 2,135 clips could not see it

`run_aerosonicdb.py` calls `detect.detect(...)` without `t_offset`, so it defaults to 0.0. The
live pipeline passes `node.archive_time_of(first)` — an absolute unix time. **68.2 % detection
on the corpus and thirteen consecutive misses on the station are both true, and one argument
separates them.** No corpus of clips can produce an absolute timestamp, so no corpus run could
have caught this.

The harness now takes `--t-offset`, and the whole corpus was re-scored at both values. That
turns a two-clip observation into a 2,135-clip regression test:

| | `t_offset = 0` | `t_offset = 1.794e9` |
|---|---|---|
| propeller detected | 103 / 151 | 103 / 151 |
| turbofans fired on | 2 / 474 | 2 / 474 |
| false positives, 51 s label-verified | 0 / 274 | 0 / 274 |
| false positives, 10 s curated | 0 / 1,236 | 0 / 1,236 |
| **verdict flips** | | **0** |

2,134 of 2,135 clips are bit-identical; one jet clip differs by a single candidate near the
collect floor, which changes nothing at the deployed threshold. The headline numbers on this
page are therefore unchanged by the fix, which is what makes them comparable — and they now
describe the deployed station as well as the corpus, which they did not before.

### Epsilon is not accuracy, and that is the more general lesson

That chi-0.11 pass carries **ε = 2.97 %** — three hundredths of a point below the gate that
rejects at 3.0 %. Shortening the fit window drives ε *down* while the geometry gets *worse*:

| window | ε | speed err vs ADS-B | slant err vs ADS-B |
|---|---|---|---|
| 40 s | 0.50 % | +4.8 % | +5.8 % |
| 30 s | 0.19 % | −22.7 % | −45.3 % |
| 20 s | **0.14 %** | **−29.4 %** | **−53.3 %** |

A truncated sweep gives the model less to explain, so it explains it beautifully and wrongly.
ε measures how well the model fits what it was shown, not whether the answer is right. Since
`atmos.sample_from_match` sets `sigma` from ε and weights by `1/sigma²`, the inversion gives
the **highest weight to the most truncated fits**. `MAX_EPSILON` is unchanged at 0.03 — it was
calibrated on this corpus, in the healthy regime, and still separates the misfitting car — but
it should not be the last word on a detection that ADS-B can identify, which is why the join
now runs first.

### Two more faults the same review turned up

**No ICAO type designator ever resolved to `"R"`.** `NON_PROPELLER_CLASSES` and
`store.detection_rate`'s `NOT IN ('J','R')` both existed, but all 45 rotorcraft types were
registered by Doc 8643 engine letter as `T` or `P`, so the rotorcraft exclusion was dead code
in every path. The station put an AS350 at 1,173 m into the propeller denominator four times;
its score of 53.9 is the highest unheard row in the database. Emitter category **A7** is now a
fallback, and `P46T` no longer appears in two blocks with the later one winning.

### Confirmed live: the first ADS-B match the station has ever made

Ten minutes after the fix was deployed a low, slow aircraft broadcasting no type code passed
at 953 m. (Identifiers are withheld throughout: an ICAO address plus a timestamp locates the
receiver, and this instrument's whole premise is that aircraft positions are public while the
microphone's is not.)

| | acoustic fit | ADS-B | error |
|---|---|---|---|
| speed | 32.1 m/s | 32.7 m/s | **−1.8 %** |
| slant range | 853 m | 953 m | **−10.5 %** |
| chi | | | **0.45** (threshold 3.0) |

Thirteen passes before the fix matched nothing at all. This one is stored `source=matched`,
and the detection rate reads **9.1 %** rather than 0.0 % or the meaningless 14.3 %.

**It was screened out and kept anyway.** Its note reads `screened out, kept on ADS-B: poor fit
(epsilon 3.07%)` — three hundredths of a percent over the gate. Under the previous order of
operations this pass would have been discarded on that number and filed twice as a failure:
once as an unexplained detection, once as an aircraft that should have been heard and was not.
Joining first is what kept it, and the row that proves it exists only because the disagreement
is now recorded instead of resolved silently.

That is a second independent case of `ε` failing as a proxy for accuracy: 3.07 % on a fit
accurate to 1.8 % in speed. The first was 2.97 % on one accurate to 0.3 %.

### `MIN_AIRCRAFT_SPEED` is too high, and now there is truth behind that

DESIGN.md 3.1a justifies the 35.8 m/s (80 mph) floor as "road and rail run below ~35 m/s;
aircraft above ~40 m/s". The aircraft half of that is **false at this site**: the confirmed
pass was doing **32.7 m/s by ADS-B**, and the operator heard it plainly. A second candidate at the same
arrival was rejected with `too slow for an aircraft (23 m/s)`.

Light aircraft in the circuit fly at 60–70 kt (31–36 m/s), so a station near an airfield sees
them routinely — and they are exactly the traffic the blind path exists for, since ADS-B Out
is not required in Class G below 10,000 ft. The floor and real road traffic now genuinely
overlap, so **no threshold separates them**, which is the same shape of problem as the epsilon
gate and the misfitting car.

The gate has not been changed. Lowering it trades directly against the one thing this project
has protected most carefully — zero false positives in 274 label-verified windows — and the
ground-source corpus that would measure that trade does not exist. What the join-first order
already buys is that a slow aircraft **with** an ADS-B track is now kept regardless of the
floor, which is precisely what happened here. The floor still governs the blind product, where
nothing else can vouch for the detection.

### "Nothing tonal" was a statement about 50 seconds, not about the pass

The operator reported a plainly audible propeller aircraft. The dashboard recorded that pass
as **`nothing tonal`, score 8.7**.

`adsb.pass_geometry` already computes how long each pass is audible, as `3 · d/v`. For this
aircraft — 3.8 km out at 45.7 m/s — that is **±248 s**. `best_score_near` ignored it and used
a hard-coded **±25 s**, so it read the quiet fifty seconds either side of CPA. The comb that
actually belonged to that aircraft scored **37.8**, 128 s away, inside the window the same
object was already carrying.

Every slow or distant pass was being scored over a window far narrower than its own
audibility, which systematically inflated the `unheard` list and made the detection rate read
low.

The fix is **not** simply to use `audible_window`. That is `3 · d/v`, which at this station's
real geometries runs **440–660 s** — wider than the 420 s analysis window, so the best score
inside it would be the loudest thing in the entire analysis and would say nothing about the
aircraft in question. Replacing a window that is too narrow with one that is too wide just
inverts the error.

The attributable scale is the Doppler transition itself, about `d/v`: a tone belonging to a
pass has its own CPA within roughly that of the aircraft's. Each pass is now scored over
`±max(d/v, 25 s)`, the floor keeping a fast close pass from getting a uselessly short span.
For the aircraft above that is ±83 s rather than ±25.

What that column means is worth stating precisely, because it is easy to over-read: it answers
*was anything tonal while this aircraft was overhead*, not *was this aircraft heard*. Only the
join answers the second, because only the join checks that the tone's geometry matches the
track's.

### The speed floor was measured, and the first value tried was wrong

`MIN_AIRCRAFT_SPEED` was 35.8 m/s (80 mph), justified in DESIGN.md as "road and rail run below
~35 m/s; aircraft above ~40 m/s". The station's first ADS-B-confirmed pass was doing **32.7
m/s**, so the aircraft half of that is false at a site near an airfield.

**25.0 m/s was tried first and the synthetic suite rejected it in seconds**: a truck at 28 m/s
and 200 m came back as `aircraft: 26 m/s at 165 m`. Measuring what each source actually fits
at is what produced a defensible value:

| synthetic source | fits at | rejected by |
|---|---|---|
| car 25 @ 60 m | 60.8 m/s | transition unresolvable (1.5 s) |
| car 30 @ 120 m | 48.1 m/s | `ε` 3.91 % |
| **truck 28 @ 200 m** | **26.1 m/s** | **the speed floor, and nothing else** |
| loco 20 @ 50 m | 101.5 m/s | pinned at the distance floor |
| loco 20 @ 300 m | 20.0 m/s | pinned at the lower speed bound |
| *station's confirmed pass* | *32.1 m/s* | *(accepted; ADS-B 32.7)* |

Four of the five ground sources are rejected whatever this gate says — two of them by the
bound check added in the same pass. **Exactly one case depends on it**, which puts the usable
range at `(26.1, 32.1)`.

**30.0 m/s (58 kt)** sits in that gap, biased towards the ground-source end: 15 % margin above
the truck against 6.5 % below the aeroplane, because a false "non-cooperative aircraft" is the
error this instrument cannot afford. The 6.5 % is still ~3.6σ on the 1.8 % speed error
measured against ADS-B.

No value separates these populations cleanly — highway traffic runs 29–36 m/s and light
aircraft approach at 28–33 — so this is a floor for the blind path alone. Where ADS-B exists,
the join runs first and confirms a slow aircraft on its whole trajectory rather than one
number, which is how the 32.7 m/s pass was kept.

The suite caught the first attempt because AeroSonicDB has no labelled ground vehicles. That
is the second time the two corpora have covered for each other on this exact gate.

On the corpus the change is invisible, which is the point:

| | propeller | turbofan fired on | FP, 51 s verified | FP, 10 s curated |
|---|---|---|---|---|
| floor 35.8 m/s | 103 / 151 (68.2 %) | 2 / 474 | 0 / 274 | 0 / 1,236 |
| **floor 30.0 m/s** | **103 / 151 (68.2 %)** | **2 / 474** | **0 / 274** | **0 / 1,236** |

Not one clip of 2,135 changes verdict. AeroSonicDB is airport traffic, so nothing in it sits
in the 30–36 m/s band at all — the gate was costing nothing *here* while rejecting a confirmed
aircraft *there*. A corpus can only tell you about the traffic it contains.

**And lowering it exposed a bug in a different stage.** With the floor at 30.0 the CLI scene
test began reporting two aircraft where there is one. The extra detection was not the vehicle
— that is correctly rejected at 27.9 m/s — but a **second lock on the real aircraft's receding
tail**, fitted at 34 m/s and 690 m against a truth of 90 m/s and 2 km, with `ε` of 0.14 %.

It escaped de-duplication by 0.7 s. The merge window was `0.6 · d/v`, and a flyby's Doppler
transition spans about `d/v` either side of CPA, so a duplicate can land anywhere in that
range; the window is now the full `d/v`.

For as long as `MIN_AIRCRAFT_SPEED` was 35.8 this duplicate was silently discarded as "too
slow", so **a threshold in one stage was hiding a defect in another**, and the apparent cost
of lowering it was partly the cost of removing that cover. It is also a fourth case of `ε`
being no guide to accuracy: 0.14 % on a fit wrong by a factor of two in speed and three in
range.

### Rejection reasons that were statements about the optimiser, not the aircraft

Several candidates came back sitting exactly on a solver bound — speed at 20.0, distance at
50 m or 20,000 m — and were rejected with `too slow for an aircraft (20 m/s)` and
`transition unresolvable`. Both read as measurements of the source. Neither was one. The
bound check now runs before every gate that phrases itself that way, and names which bound,
because a wrong reason is worse than no reason: it sends the next person to recalibrate a
threshold that was never involved. This review began by taking four such reasons at face
value.

**Jets were excluded from the wrong half of the join.** `audible_candidates(exclude_jets=True)`
removed them from the candidate list, so a turbofan the detector *did* fire on — 2 of 474 here
— had no track to match and became an unexplained detection. This page claimed above that such
clips "join to their own ADS-B track"; in the deployed code they could not. Engine type now
decides only whether a miss counts, never whether a match may be made.
## What the fourth review found

The fourth review (2026-11-08, mid-morning local time) went looking for passes. It found five defects, four
of them landing on the one number that is supposed to summarise the instrument — the detection
rate — and pushing it in both directions at once. It read 17.4 %. Neither the direction nor the
size of the error was guessable from the figure.

Three of the five were found by reading the station's own stored columns rather than its code:
`clock_offset_s`, `comb_contrast`, and two rows whose `reject_reason` was a Python exception.

### A closest approach the aircraft had not reached yet

`pass_geometry` finds the slant minimum over the observed track. When the aircraft is still
inbound at the end of that track, the minimum lands on the final sample, and what comes back
is not a closest approach: it is *the closest it got before the snapshots ran out*. The error
has a fixed sign — too far, and too early — because the aircraft was still closing.

The station did this to a Beech Bonanza. One window predicted a CPA of 3,017 m, heard nothing
there, and filed it as unheard. The next window saw the completed pass at **828 m**,
detected it, and matched it at χ 0.43. One aeroplane, 36 s apart on the ADS-B clock, entered
both halves of its own detection rate — as the miss and as the hit.

`PassGeometry.cpa_bracketed` now records whether the track actually straddles the CPA, and
`audible_candidates` drops the ones that do not. It is withheld from *both* directions of the
join deliberately: a match made against an extrapolated geometry is a match to the wrong
numbers, and a miss recorded against it is a miss on a pass that has not happened. Consecutive
analysis windows overlap by `analysis_window_s - analysis_period_s`, which is 120 s as
deployed, so nothing is lost by waiting for the window that sees the whole pass. That overlap
is what it is for.

### One pass, counted twice

The positives were de-duplicated by `_already_stored` from the beginning. The negatives never
were, and the `UNIQUE (station_id, t_heard_cpa, icao24)` constraint does not catch them,
because `t_heard_cpa` is recomputed from a different set of ADS-B snapshots in each window and
moves by a fraction of a second. Eleven of forty `unheard` rows were one arrival filed twice.

The de-duplication window was measured rather than chosen. Two windows reporting one arrival
put it within 26 s, and 8 of the 11 agreed to under 1 s; the same aircraft genuinely coming
round again — an AS350 orbiting — was 87 s away. `unheard_dedup_s` is 60 s, between the two.

It also has to compare like with like. `passes.t_heard_cpa` is the *acoustic* time and carries
the fit's own error, 96 s of it in the case below, so testing a negative against that column
finds a phantom duplicate 96 s away and misses the real one sitting on top of it.
`adsb_t_heard_cpa` comes from the same source as the negative being tested, and on the three
real cases agreed to 0.6 s.

### A clock offset no second pass corroborated

A clock offset is one parameter shared by every pass, so *k* residuals leave *k*−1 degrees of
freedom to check it against. `estimate_clock_offset` accepted *k* = 1, where the fit is exactly
degenerate: the scan slides the offset until that one detection's timing error is zero. The
time term then contributes nothing to χ and geometry alone decides the association.

The station matched one detection to an aircraft **96.4 s** away at χ 1.16, with the estimator
reporting −96.4 s. That is not a near miss on a threshold; it is the threshold being removed.
And the stored rows show it was removed every time:

| pass | timing residual | reported offset |
|---|---|---|
| Bonanza (matched, χ 0.43) | +6.1 s | +6.1 s |
| Mooney | −11.7 s | −11.7 s |
| the 10:00 pass | −96.4 s | −96.4 s |
| the 08:02 pass | −5.7 s | −5.7 s |

In all four, the offset is *exactly* the single detection's own residual, so the time term of
χ was zero by construction on every association the station has ever made. Recomputing χ at
zero offset withdraws two of them: the 10:00 pass at χ 32.1, and the Mooney — fitted speed 27 % high,
distance 21 % high — at χ 4.84. The two good ones survive
comfortably, at χ 1.95 and 2.08.

So two of the station's four matches were produced by the estimator rather than by the
instrument — the opposite bias to the two above. An offset now needs at least
`MIN_OFFSET_RESIDUALS` accepted matches whose residuals agree to within
`OFFSET_AGREEMENT_SIGMA · SIGMA_T_S`, and reports zero otherwise. The ±120 s search is
unchanged: a host with no RTC and no network time really can be minutes out, and the problem
was never the width of the search but the absence of a second opinion.

### A re-trace that crashed, and was reported as a rejection

Two rows read `refinement failed: IndexError` — the catch-all in `detect.detect`, which turns
any exception during refinement into a rejection. One was a Cessna 172 that had scored **32.1**
against an 18.0 threshold.

`fit_comb` traces the ridge at a 1 s window, fits, then re-traces at the window matched to the
fitted slope. A trace at a different window length yields a ridge with a different number of
frames, so a spectrogram, its ridge, its `keep` mask and the parameters that mask produced are
one indivisible set. The code adopted the new trace and kept the old mask whenever the second
fit failed to converge, leaving `keep` indexing an array it was never built for:
`boolean index did not match indexed array along axis 0; size of axis is 902 but size of
corresponding boolean axis is 477`. Reproduced exactly in
`test_a_failed_rewindow_does_not_crash_the_fit`.

This is the second time a rejection reason has been a statement about the software rather than
the aircraft, and the first where it was a crash. A bare `except Exception` that writes its
verdict into a results table will keep doing this, so the reason string now has to be read as
a hypothesis, never a finding.

### What the rate actually is

Removing only what is verifiable from the stored columns — three rows recording aircraft the
station demonstrably heard, and seven arrivals filed twice — takes the propeller-only rate from
17.4 % to 23.5 % on 4 matches against 13 misses. Withdrawing the two uncorroborated
associations takes it back down. At single-digit match counts the ratio is dominated by its own
small numbers, and the useful conclusion is not a percentage but that **four independent
defects all landed on the one figure that is supposed to summarise the instrument, with
opposite signs, and none was visible in it.**

### The truth side of the join was the noisier side

Two lines in the station's `run.log`, repeating:

```
UserWarning: The maximal number of iterations maxit (set to 20 by the program)
allowed for finding a smoothing spline with fp=s has been reached: s too small.
```

`_smooth_enu` fits a cubic smoothing spline to each ADS-B track, and FITPACK's `s` is the
*total* allowed misfit — per point, the variance of the position error. It was a hard-coded
4 m², a 2 m RMS. ADS-B does not deliver 2 m. FITPACK cannot reach a misfit that small, gives
up after 20 iterations, and returns a spline that threads the noise — which is the one failure
the function's own docstring says it exists to prevent.

Differentiating that spline is where `speed_mps` comes from. Measured against known truth at
10 m of position jitter, the error was **9.5 %** on speed; at 25 m, **19.7 %**. The join
compares that number against the acoustic fit with `SIGMA_SPEED_FRAC` = 10 %. So the *truth*
was no better than the tolerance it was the truth for, and `error_model`, whose entire purpose
is to correct the fit's known overconfidence, has been measuring the spline.

No constant fixes this, because the noise is not a constant — it depends on the aircraft's
equipment and on its range. Smoothing less than the noise tracks the noise; smoothing more
cuts the corner on a turn. The noise is now estimated from the track itself, as the median
absolute deviation of its second differences (a smooth trajectory has none, so what is left is
noise with variance 6σ²; MAD rather than RMS so a dropped report cannot set the smoothing for
the whole track). It recovers the jitter it was given to within 2 %, and median speed error
falls to **0.04 %** at 10 m and **0.11 %** at 25 m.

`test_pass_geometry_survives_position_jitter` did not catch any of this, because it was written
to the tolerance the implementation happened to achieve: it allows 15 % on a quantity the join
needs to 10 %. A test written that way around records a defect instead of catching it.

### Half the stored passes have no comb in them

Asked why the station's spectrograms did not look like a textbook flyover, the answer turned
out to be in a column already being written. Sorting the 39 stored passes by `comb_contrast`:

| `comb_contrast` | rows | what is there |
|---|---|---|
| 2.36 | 1 | the ADS-B-confirmed Bonanza |
| 0.47 – 1.84 | 7 | real combs; all four matches are in this group |
| −0.04 – 0.15 | 20 | the comb is no stronger than the background |

Rendering them settles it. The Bonanza is a textbook harmonic comb with the Doppler S-curve
running through every harmonic. A blind detection from 10:42 that scored **32.8** against an
18.0 threshold has nothing in it at all: the fit is a clean S-curve through noise.

`detect` accepts `min_comb_contrast` and deliberately does not apply it. That decision now has
evidence against it, and `template_score` alone is demonstrably not sufficient — the noise fit
outscored three of the four real matches. Left as measured rather than acted on, because
changing a detection threshold on 39 rows would be fitting a threshold to an anecdote; what
the station needs first is enough matched passes to put a number on where the two populations
separate.

### The fix that was tested against the wrong kind of noise

Enabling persistent logging on the station turned up the warning the smoothing fix above was
supposed to have ended, still firing four times in the two and a half hours since that fix was
deployed — twice as the old "maximal number of iterations", and twice as a message that had
not appeared before:

```
A theoretically impossible result was found during the iteration
process for finding a smoothing spline with fp = s: s too small.
```

The estimator was validated against Gaussian jitter, where it recovers the injected noise to
within 2 %. Real ADS-B is not Gaussian; it is **quantised**, and in two ways the synthetic
tracks had no way to show.

**Geometric altitude arrives in 25 ft steps.** An aircraft in level flight reports the same
figure for a minute, so more than half its second differences are exactly zero and the median
absolute deviation — chosen precisely because it ignores outliers — ignores the steps and
measures the flat stretches between them. On 43 tracks taken live from the station's own feed,
the altitude axis estimated **σ ≈ 0** on nine of them, and all three axes on two more. Clipped
to the 1.0 m floor, that made `s` *smaller* than the fixed value it replaced.

**A position report is re-broadcast until the aircraft fixes a new one.** `t` is already
`now - seen_pos`, so each repeat arrives with an identical timestamp: the same measurement,
counted again. Kept, the trajectory becomes a staircase — flat stretches joined by steps — and
the speed is read off the derivative of a spline fitted to it. Measured against known truth,
with one fix held across *k* polls:

| fix held for | speed error, as deployed | after dropping the repeats |
|---|---|---|
| 1 poll | +0.07 % | +0.07 % |
| 2 polls | −22.8 % | +0.05 % |
| 3 polls | −33.0 % | +0.07 % |
| 5 polls | +331.8 % | +0.13 % |
| 8 polls | −94.8 % | +0.12 % |

Three things changed. Repeated reports are dropped where tracks are assembled, because they
are one observation. Each axis is smoothed to **its own** noise rather than the median of the
three — east, north and up do not share an error distribution, and the median was applying one
axis's scatter to another in both directions (0.05 m east against 9.2 m north on one live
track, 50 m east against 1.1 m up on another). And the floor is now the feed's own
quantisation, 25 ft / √12 = 2.2 m, rather than an arbitrary 1 m: below the step size there is
nothing to resolve, and asking FITPACK for a misfit it cannot reach is what produced the
warning in the first place.

The lesson is narrower than "test with real data" and worth stating exactly: **a noise model
is an assumption, and validating it against noise generated from that same assumption tests
nothing.** The synthetic check was not weak — it swept five jitter levels, straight and
turning tracks, sixteen seeds each — and it could not have failed, because every sample it drew
was independent and Gaussian by construction. What found this was a log file.

## What the fifth review found

The fifth review asked one question: had the per-axis smoothing fix ended the `s too small`
warning? The log said two occurrences, both before the restart, and none after. That reading
was wrong twice over, and the way it was wrong matters more than the answer.

**A log cannot measure a warning's rate.** Python prints a given warning once per process per
message text and then goes silent, however often it recurs. So the counts that looked like
rates were counts of *process starts*: the twenty-six occurrences in the journal came from
fourteen PIDs, and the same PID appears twice only because FITPACK has two wordings for this
failure. The fourth review read "4 in 2.5 h" as a rate; the fifth read "none since the
restart" as "fixed". Neither number meant what it was taken to mean.

**And the second wording was being missed entirely.** FITPACK reports this as either
`A theoretically impossible result ... : s too small` or `The maximal number of iterations
maxit ... has been reached: s\ntoo small` — and in the second the phrase wraps across a line
break, so a `grep "s too small"` finds the first and not the second. Seventeen of the
twenty-six occurrences were the wording the check could not see. The project's own test for
this already normalised the newline before matching; only the shell command used during the
review did not. The lesson from the fourth review was that a synthetic check can be
unfalsifiable. This one is narrower and sharper: **the instrument that reads the evidence is
part of the experiment, and a grep is an instrument.**

So the warning is now counted in the code, where the suppression cannot reach, and published
with the other health counters as `spline_nonconvergence`. `_smooth_enu` runs in two processes
— the analysis worker and the main loop — so the worker hands its count back rather than
letting it die with the process.

### The smoothing rules, A/B'd on live tracks

One hundred tracks captured from the live feed, every track run through each rule with the
warning filter forced to `always` so nothing is suppressed:

| smoothing rule | tracks tripping FITPACK |
|---|---|
| `s = 4n` (original) | 3 / 100 |
| one σ for all three axes (first fix) | 0 / 100 |
| per-axis σ, 2.2 m floor (deployed) | 0 / 100 |
| σ fixed at 10 m | 0 / 100 |

The deployed rule is better than the original, which is the question that was asked. It is not
perfect — the station logged one occurrence in eighteen analyses after the restart — but the
rate is now measured rather than inferred from a silence.

### The estimator measures the flight path, not the noise

The more serious finding is underneath that table. `_position_sigma_m` assumes "a trajectory is
smooth, so its second difference is near zero and what remains is noise". At the interval this
feed is polled, that is false. For constant acceleration the second difference is exactly
`a·dt²`, and a 100 m/s aircraft on a 5 km turn radius pulls 2 m/s², which is 500 m at
`dt` = 16 s against a true position error near 10 m.

Measured against known noise on synthetic tracks that *manoeuvre* — the case the fourth
review's sweep did not cover, because its turning tracks were sampled finely enough for the
assumption to hold:

| case (26 samples, true noise 10 m) | σ returned |
|---|---|
| straight and level, dt = 16 s | 10.8 m |
| 3 km turn radius, dt = 16 s | 115 m |
| 5 km turn radius, dt = 16 s | 197 m |
| 5 km turn radius, dt = 30 s | 719 m |

And on live traffic the horizontal estimate ran to a median of **52 m**, a 90th percentile of
389 m, and a maximum of 1300 m, with **28 of 158 axes pinned at the 200 m ceiling**. The
ceiling is not a safety margin; it is the only thing standing between the spline and a
trajectory smoothed by a kilometre. Third differences, which annihilate constant acceleration,
cut the error from a median of 1285 % to 166 % — better, and still not a measurement, because a
turn is not a quadratic.

Two consequences, stated separately because they differ in size:

* **It does not explain the slant bias.** Re-running every bracketed CPA with σ forced to 10 m
  moves the ADS-B slant by a median of **+0.05 %** (mean +0.04 %). Over-smoothing was the third
  hypothesis for the −22 % slant bias to be tested and the third to fail, after the octave slip
  and the held positions.
* **It does cost speed.** The same comparison moves the ADS-B speed by a mean of −4.4 %, and up
  to −14.5 % on an individual track. That lands on the side of the join used as truth, against
  a `SIGMA_SPEED_FRAC` of 10 %.

The honest conclusion is not that a better estimator is needed but that **this quantity is not
identifiable from this data**: between two samples 16 s apart, a manoeuvring aircraft's own
curvature exceeds its position error by more than an order of magnitude, so no estimator
recovers the noise from the track alone. The feed's documented accuracy — NACp horizontal, and
25 ft / √12 vertical for the quantisation — is a known quantity where the measured one is not.

### The comb gate, switched on

Contrast had been computed and reported since the matched filter was written, and deliberately
*not* gated on. The reason was a measurement, not an oversight: an aircraft at 4 km was detected
with a matched-filter score of 110 while its per-frame comb contrast sat at zero, so a bare
threshold on contrast demonstrably discards true positives.

What changed is that 66 live passes now say where the two populations sit — and the overnight
false positives are not quiet aircraft, they are not combs at all:

| blind detections | n | contrast < 0.20 | mean contrast |
|---|---|---|---|
| day, 07–18 | 35 | 6 | 0.858 |
| **night, 19–06** | **22** | **22** | **0.028** |
| ADS-B confirmed | 9 | **0** | ≥ 0.43 |

A thirty-fold separation by time of day, and the confirmed matches sit entirely on one side of
it. 0.20 falls in the natural gap between 0.179 and 0.27, rejects 28 of 57 blind detections
including every overnight one, and rejects none of the nine matches — a 2.2× margin below the
lowest. The value that had been sitting unused, 0.35, would have left only 1.24×.

The old objection still stands, so the gate waives itself on a matched-filter score of 80 or
more. That number is not a guess: every one of the 28 rejected rows scored between 18.0 and
53.9, and the highest score among all 66 stored passes is 77.7, so the waiver is above
everything the station has observed and below the 110 of the aircraft that motivated reporting
contrast rather than gating it. On all the evidence available it costs nothing and preserves a
case the project had already measured. A caller that offers no score gets the gate unwaived —
`cli.cmd_fit` fits raw audio with no detector behind it — because absence of the score is not
evidence for the waiver.

Two structural points. The gate is checked **last** among the physical gates, so "too slow" or
"transition unresolvable" is preferred wherever it applies: those name what the source is,
while this one only says the evidence is absent. And it gates `unexplained` only, after the
ADS-B join, so no confirmed match can be lost to it whatever its contrast — the join runs on
every detection, and screening touches the blind product alone.

What this does **not** do is explain the overnight detections. It removes them from the
published product, which is the right call for an instrument whose one unaffordable error is a
false "non-cooperative aircraft", but a contrast near zero with an epsilon near 2 % is still an
unexplained signal. It is now filed rather than published.

## The temperature readout, and what it is actually measuring

Noticed from outside the instrument: the sounding reported temperatures aloft of 67 to 133 C
on a day with a ~60 F (15.6 C) surface. Those are not temperatures, and the chain that
produces them is worth writing down because the number is the most legible readout of a bias
measured three other ways.

`c_eff_from_speed_bias` recovers the effective sound speed from the *disagreement* between the
acoustic speed and the ADS-B speed, and `temperature_from_c` inverts the dry-air relation. The
gain is brutal: around 340 m/s, **1% of speed error is about 3.4 m/s of c_eff and ~5.8 C of
implied temperature.** DESIGN's "1 m/s of wind or 1.7 C of temperature per pass" is stated per
m/s of c and is correct; expressed per percent of the *speed* observable, which is what is
actually biased, it is three and a half times larger.

Measured over the 11 stored matches:

| | mean | median | sign |
|---|---|---|---|
| speed, fit vs ADS-B | **-7.5%** | -7.2% | 10 of 11 negative |
| slant, fit vs ADS-B | **-21.3%** | -20.8% | 11 of 11 negative |
| implied c_eff | 372.7 m/s | 369.8 m/s | vs 340.6 at 15.6 C |
| implied temperature | — | **67 C** | worst pass 198 C |

Three things follow.

**The temperature is the speed bias, amplified.** The worst pass for temperature (198 C) is
the worst pass for speed (-21.2%); the one pass whose speed came out *high* (+2.2%) implies
7.1 C, which is plausible. Nothing separate is broken in the sounding.

**The bias is not one common scale error.** This is new and it constrains the hypotheses. The
slant bias is **2.8x the speed bias** and is 11 of 11 against 10 of 11. A single multiplicative
error in the time axis or the sample rate would move both together. It does not: the total
pitch drop, which carries speed, is nearly unbiased, while the transition sharpness, which
carries slant, is biased hard. Whatever the cause, it lives mostly in the transition width.
Three hypotheses have already failed when measured (octave slip, held positions,
over-smoothed ADS-B); this is a constraint on the fourth, not a fourth.

**Wind is not affected and temperature absorbs all of it.** The layer solve's design matrix
has a constant column for `c0` and two wind columns in `cos(elevation)cos(bearing - .)`. A
common-mode offset in `c_eff` projects predominantly onto the constant column, which is
exactly the parameter the temperature is read from. So the published wind survives the bias
and the published temperature does not — the opposite of a product that should be presented
with equal confidence.

### What was done about it

Temperature aloft is **withheld**, behind the single flag `atmos.PUBLISH_TEMPERATURE`. The
inversion is left exactly as it was, because nothing is wrong with it. `as_dict` reports
`temperature_c` as null rather than dropping the key — a consumer should see that there is no
value, not a stale one, and should not need to handle a `KeyError` to find out why — alongside
`temperature_suppressed` carrying the reason. `Sounding.temperature_c` still computes, so the
implied value stays available to whoever is chasing the bias.

The **effective sound speed** is published in its place, on the dashboard and in the layer
table. That is the quantity the instrument actually measures: `c(T)` plus the wind projection
along the path. Attributing it to temperature is the step that is untrustworthy, not the
measurement.

Wind keeps its stated confidence, and a test now asserts the reason rather than leaving it as
a claim in a comment: a 25 m/s offset applied to every sample moves `c0` by 25.0 m/s and moves
the wind vector by less than 0.5 m/s. If that ever stops holding, suppressing temperature
alone is the wrong remedy.

The row stays visible on the dashboard, labelled suppressed and carrying its reason. A product
withdrawn silently is worse than one published wrong, because nobody can ask why it went.

### A free diagnostic the suppression surfaced

Publishing the effective sound speed per layer instead of the temperature made the bias
legible in a way the temperature never was, because sound speed has a known vertical
structure to violate. From the first 11 matches:

| layer | n | effective sound speed |
|---|---|---|
| 0–1,000 m | 11 | 360.0 ± 3.4 m/s |
| 1,000–2,000 m | 5 | 404.0 ± 10.2 m/s |

Air cools with height, so `c` should **fall** by roughly 3 m/s per km. It rises by 44. Both
layers also sit above 355 m/s, which is dry air at +45 °C, so neither is a possible atmosphere
and the upper one is not close. Wind cannot explain it: the layer solve fits wind per layer, so
`c0` is already wind-free by construction.

This looked like an altitude dependence, and **the sixth review measured it per pass and it is
not one.** Spearman rho between the per-pass slant error and ADS-B AGL is **+0.04** over twelve
matches — nothing. The layer figures above are an artifact of the weighted inversion at small
n: the upper layer rested on five passes, and the worst-biased pass of the twelve sits at
761 m, among the lowest. The claim is withdrawn; the table is kept because the values are
real and still impossible, which is the part that holds.

Two honest limits on the surviving products while n is 11. The published wind is
16.3 m/s ± 30.3 — the uncertainty **exceeds the quantity**, so "wind is unaffected by this
bias" must not be read as "wind is measured". And the layer rows above rest on 11 and 5 passes.
Both need the same 20–30 matches the slant bias does.

## What the sixth review found (the comb gate's first night)

### The gate did not fire, and the premise it was sized on did not recur

Zero `unheard` rows carry a comb rejection reason. Not one. But the gate *is* being reached:
of 14 blind passes stored overnight, **none** has contrast below 0.20, which is exactly what
the gate guarantees. It rejected nothing because nothing arrived to reject.

That is the finding, and it is not the one the gate was deployed to produce. Compare the two
nights, same hours, same site:

| night | n | comb contrast | mean |
|---|---|---|---|
| Nov 7–8 (pre-gate) | 20 | −0.04 to 0.18 | 0.03 |
| Nov 8–9 (gate live) | 14 | **0.21 to 1.33** | **0.61** |

A twenty-fold shift in the quantity the threshold was placed on, overnight. The 0.20 value was
chosen to sit in an apparent gap between 0.179 and 0.27 in a sample whose entire night
population was near zero. Last night's night population straddles it, and one pass sits at
**0.21 — one hundredth above the threshold**. Fit quality moved the same way: overnight
epsilon ran 0.012–0.030 on the first night and 0.0042–0.0250 on the second.

So **the gate is unvalidated**. It has never rejected anything, the waiver has never been
exercised, and the distribution it was fitted to has not reappeared. Two readings are
consistent with the data: either the first night was anomalous and 0.20 sits at the bottom
edge of the real distribution rather than in a gap, or the second night was. One night of each
cannot say. The threshold should not move again until a night like the first recurs with the
gate live, because moving it now would be fitting to whichever night is in hand.

### Nothing real was suppressed, because nothing was suppressed

No comb rejection carries an `icao24`, so no genuine ADS-B arrival was refused. There are no
comb rejections at all, so there is also no maximum template score among them to compare
against the waiver at 80 — the question the waiver was sized for stays open.

### The non-convergence counter earned itself in one reading

`spline_nonconvergence` reports **3** over 8.85 h of uptime and 106 analyses. The log holds
**2** matching lines, one of each FITPACK wording. That gap of one *is the defect the counter
was built for*: Python prints a warning once per (location, message) per process, so two
distinct messages print twice however often they recur. Counting 3 against 2 printed is the
counter working, not disagreeing.

### The slant bias, now characterised rather than hypothesised

n is 12 (was 9). The bias survives, slightly worse: **slant −22.9 %, 12 of 12 negative**;
speed −8.6 %, 11 of 12; time +1.23 s. Four measurements that constrain the cause, with the
circular one named and excluded:

1. **It is one error, not two.** Spearman rho between the slant error and the speed error is
   **+0.895**. Earlier this was read as two errors because slant is biased 2.8x harder than
   speed; the rank correlation says one error with a gain ratio, which is a different thing.
2. **It is multiplicative, not additive.** As a ratio, `fit/true` slant is 0.771 with a
   coefficient of variation of **0.161**. As an offset it is −543 m with a cv of **0.869**.
   A fixed fraction describes it five times better than a fixed distance.
3. **It is not geometry and not fit quality.** rho against AGL +0.04, against slant −0.20,
   against elevation +0.01, against f0 +0.03, against epsilon +0.10. All nil.
4. **The chi correlation is circular and proves nothing.** rho against `match_chi` is −0.811,
   which looks like the strongest signal here until you read `_chi`: it is built from
   `(det.cpa_distance - geom.slant_cpa_m) / geom.slant_cpa_m`, which *is* the slant error.
   Recorded so the next reader does not spend the same hour on it.

A near-constant multiplicative factor, independent of range, height, angle, frequency, speed
and fit quality. Still no fifth hypothesis; this is the fifth measurement.

### A thread race in the clock model, found in `last_error`

`ClockModel.residual_s` built two NumPy arrays from two separate list slices while the capture
thread appended to both, and raised

    ValueError: operands could not be broadcast together with shapes (30005,) (30004,)

out of `_write_status` — a one-element tear, which is the signature of a single append landing
between the two reads. It cost that cycle's status file and left the dashboard stale. Both
`residual_s` and `observe` now pair the lists with `zip` before slicing, so a half-finished
append is not seen at all rather than seen as a length mismatch.

### Blade count: solvable from one pass, and it was never a sample-size problem

Propeller RPM had never been computed for a single pass of 81. The cause was
`infer_blade_counts`, which required **eight passes sharing a `type_code`** before it would
name a blade count, and `type_code` exists only on ADS-B-matched passes: 9 of 81, best type at
4. So the dict was empty, no blade count was stored, and `rpm` stayed null everywhere.

It was also about to produce a wrong answer. The four BE95 passes come from three airframes.
One sits repeatably at 112.1 and 112.9 Hz, another at 76.8 Hz — a ratio of 1.465, against the
1.465 that (3/2) x (2250/2304) predicts. That is a two-blade prop and a three-blade prop on the
same ICAO type, ordinary after a prop STC. Pooled by type the inference picked N=3 and the
separation test waved it through at 27x the 1.5x it asks for, which would have put the
two-blade airframe at a fictional 1,535 RPM for every pass it ever makes.

The fix is to stop averaging and start eliminating. A propeller has a hard RPM ceiling, so for
most fundamentals only one blade count puts the implied RPM inside a cruise band, and **that
identifies N from one pass**. All nine typed fundamentals in the corpus resolve this way, at
2,172–2,730 RPM on two blades and 2,242–2,516 on three, and the BE95 type splits into its two
real configurations instead of being averaged into neither.

Uniqueness is the part that needs care, and it is not free. It holds only while the band is
narrower than the ratio between adjacent blade counts, and **that ratio shrinks with N**: 3/2
is 1.50, but 5/4 is 1.25 against a band spanning 2750/2100 = 1.31. So the piston band separates
2 from 3 and 3 from 4, and cannot separate 4 from 5 between 175 and 183 Hz, nor 5 from 6
between 210 and 229 Hz. A 181.1 Hz detection is already in the corpus, so those windows are
real traffic. Two candidates in band returns nothing, and widening the band moves the windows
rather than removing them.

Turboprops get no answer at all rather than a wrong one. A constant-speed prop runs 1,600–2,000
RPM, outside the piston band, and a 4-blade turboprop at 1,700 RPM puts f0 at 113.3 Hz — which
the piston band resolves to three blades at 2,267 RPM. Only `P` has a band until a turboprop
one is chosen against real turboprop passes, of which there are none yet.
