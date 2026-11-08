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

The Cirrus SR22T at 05:15, stored as *"poor fit (epsilon 3.13 %)"*:

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