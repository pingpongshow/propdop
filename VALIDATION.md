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