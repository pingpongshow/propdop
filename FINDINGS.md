# Findings

**What a single backyard microphone can and cannot measure about aircraft overhead.**

This is the experimental report for PropDop: a one-station passive acoustic instrument that
listens for propeller aircraft, fits the Doppler shift of the blade-pass tone, and joins each
detection against ADS-B to get truth. It is written for someone considering the same
experiment. It reports what worked, what did not, how each claim was tested, and which
plausible ideas turned out to be wrong when measured.

Two biases worth declaring up front. The positive results come from one site over a short
deployment, so treat them as existence proofs rather than performance figures. The negative
results are the more transferable half, and they are the reason this document exists: several
of them look like they should work right up until you measure them.

**Contents**

- [Summary of results](#summary-of-results)
- [The instrument](#the-instrument)
- [How claims were tested](#how-claims-were-tested)
- [Positive results](#positive-results)
- [Negative results](#negative-results)
- [Hypotheses that were measured and failed](#hypotheses-that-were-measured-and-failed)
- [Traps, with the measurement that exposed each](#traps-with-the-measurement-that-exposed-each)
- [What limits the experiment](#what-limits-the-experiment)
- [If you are repeating this](#if-you-are-repeating-this)
- [Open questions](#open-questions)

---

## Summary of results

| Claim | Verdict | Evidence |
|---|---|---|
| Propeller blade count is recoverable from one pass | **Works** | 10 of 10 typed passes resolve; 4.5 % RPM cluster across airframes |
| Propeller RPM to ~1 % | **Works, given blade count** | `ε` 0.4–3.0 % fractional frequency precision |
| Aircraft detected without any transponder | **Works** | 84 unexplained detections, farthest 16.4 km |
| Detection rate with a real denominator | **Works** | 17 % live; 68.2 % on a labelled corpus |
| Site shielding measured by bearing | **Works** | Per-sector heard vs should-have-been-heard |
| Diurnal turbulence shape | **Weak positive** | ~5× swing where theory predicts ~150× |
| Slant range to the aircraft | **Fails** | −22.9 %, 12 of 12 negative |
| Aircraft speed | **Marginal** | −8.6 %, 11 of 12 negative |
| Wind aloft | **Not demonstrated** | 11.2 m/s ± 29.8 — error bar exceeds the quantity |
| Temperature aloft | **Withdrawn** | Read 67–198 °C against a 15.6 °C surface |
| ADS-B position noise estimated from the feed | **Not identifiable** | 52 m measured against a documented ~10 m |
| Hourly atmospheric soundings at one quiet site | **Structurally impossible** | ~6 matched passes/day against ~20 needed per aggregate |

The single most useful number in this report, if you take only one: **the measured slant error
was 16.5× the fit's own residual.** Error bars propagated from the fit covariance were
worthless. Only paired comparison against independent truth revealed the real error, and this
held across every product.

---

## The instrument

Deliberately cheap, because the question is what the cheap version can do.

**Hardware.** A Raspberry Pi, a USB sound card, an electret measurement microphone with a
90 mm foam windscreen, outdoors. Nothing calibrated for absolute sound pressure level — every
product here is derived from *frequency* and from *relative* amplitude, both of which survive
an uncalibrated microphone.

**Capture.** 48 kHz mono into a ten-minute ring buffer (~58 MB, so depth is effectively free).
The analysis pass runs every 300 s over a 420 s window, overlapping deliberately: the acoustic
arrival lags the aircraft's position fix by up to ~20 s at 7 km, and a pass can straddle a
window boundary.

**Clock.** The sound card's true sample rate is learned from wall-clock observations rather
than assumed. On this hardware it measured 48,062 Hz against a nominal 48,000 — **1,290 ppm**,
which is 0.77 s of timestamp error over ten minutes and comparable to the ADS-B association
tolerance. A nominal rate would have been a silent systematic. The first few observations are
discarded because the device is opened inside the first read, putting a high-leverage point at
the extreme end of the fit; left in, it produced an apparent 3,659 ppm.

**Signal chain.** In log-frequency a flyby's shape depends only on `v/c`, so `f0` is a pure
vertical shift and `d/v` a pure horizontal scale. A small template bank plus one 2-D FFT
correlation therefore scores every `(f0, t0)` hypothesis at once, and the harmonic comb becomes
a fixed vertical pattern — comb-matched for free. Detections are then fitted properly:
per-harmonic frequency and amplitude tracks, and `ε`, the fractional frequency residual, which
is the master precision parameter because required pass counts scale as `ε²`.

**Storage.** Per-harmonic tracks are kept at 5 Hz as float16 *residuals* from the fitted model,
about 15 kB per pass. Three choices got it there: 5 Hz rather than 2 Hz, because the
scintillation decorrelation time is 1–2.5 s and 2 Hz sits on its Nyquist limit; residuals
rather than absolutes, which are small, zero-centred and compress well; and no stored time axis,
since the grid is uniform. Storing 20 Hz float32 absolutes instead cost 164 kB per pass.

**Truth.** A live ADS-B feed, queried on a rounded centre. The join is a two-way association,
not a trigger — which matters, see below.

---

## How claims were tested

Four methodological choices did more for the quality of these results than any algorithm, and
they are the part most worth copying.

**1. Store the negatives.** Every ADS-B pass that *should* have been audible and was not is
written to its own table, with the best detector score near that arrival and the reason the
best candidate was rejected. Without this, "unexplained detection" has no denominator and a
detection rate cannot be computed at all. A negative without the score attached is
uninterpretable, so the score is stored even though it failed the threshold.

**2. Join first, screen second.** The association runs over *all* detections, and the
"not an aircraft" screening is applied only to the leftovers. Done the other way round,
screening can discard a detection that ADS-B would have identified — and a match whose own
screening said "not an aircraft" is the single most valuable row in the database, because it is
a pass with a known trajectory that the gate would have thrown away. Those rows are the only
honest way to recalibrate the screening.

**3. Measure error against truth, never from the fit covariance.** This produced the 16.5×
discrepancy above. The fit's self-reported residual is a statement about how well the model fit
the data it was given, not about how far the answer is from reality.

**4. Every number carries its contributing count.** A precision figure without the number of
passes behind it is not a measurement.

**Testing.** 274 automated tests, covering synthetic passes with known ground truth, the
storage round-trip, and the specific defects listed later in this document — each regression has
a test named after the failure rather than after the function. Where a claim in a code comment
mattered (for instance "a common-mode bias lands on the isotropic term and leaves wind alone"),
it was converted into a test that asserts it rather than left as prose.

**Offline corpus validation.** Before deployment, against 2,135 labelled clips of a public
aircraft-audio corpus:

| | |
|---|---|
| Propeller aircraft detected | **68.2 %** (turboprop 80.7 %, **piston 29.7 %**) |
| False positives on 274 length-matched, label-verified windows | **0** |
| Turbofans fired on | 2 of 474 |
| Blade-pass frequency, one turboprop type, 21 passes | **91.4 Hz measured vs 92.3 Hz theoretical** |

That corpus changed two things no synthetic test could have. `ε` is about **2.4× worse** on
real audio than on synthetics, and since pass counts scale as `ε²`, every precision estimate
derived from synthetics is optimistic by roughly **6×** in required passes. And the detection
threshold had been calibrated against the wrong noise: pink noise peaks at 8.9 where real
background reaches 17.8.

---

## Positive results

### Propeller blade count from a single pass

This is the strongest result here, and the one most worth reproducing.

The blade-pass tone is `f0 = N × RPM / 60`. RPM is what you want; `N`, the blade count, is the
obstruction — **it is not in ICAO Doc 8643**, which encodes engine count and engine type but
not blades, and no other public table carries it either. Without `N`, a 90 Hz tone is 2,700 RPM
on two blades or 1,800 on three, and the measurement is unusable.

`N` is nonetheless recoverable, because a propeller has a hard upper limit on tip speed and
piston cruise settings occupy a narrow band. For most fundamentals **only one candidate blade
count puts the implied RPM inside that band**, which identifies `N` from one pass with no
repeats. Using 2,100–2,750 RPM for piston, all ten ADS-B-typed passes in the corpus resolved:

| f0 (Hz) | N | implied RPM |
|---|---|---|
| 72.4 | 2 | 2,171 |
| 76.8 | 2 | 2,303 |
| 77.7 | 2 | 2,331 |
| 83.9 | 2 | 2,516 |
| 91.0 | 2 | 2,731 |
| 112.1 | 3 | 2,242 |
| 112.9 | 3 | 2,257 |
| 117.2 | 3 | 2,344 |
| 118.9 | 3 | 2,378 |
| 125.8 | 3 | 2,517 |

Three independent checks say these are right rather than merely self-consistent. The implied
RPMs land in plausible cruise ranges for the types involved. Three passes of one type from
**two different airframes** cluster at 2,242 / 2,257 / 2,344 RPM — a 4.5 % spread, where a wrong
`N` would scatter, because the underlying throttle settings are unrelated. And two separate
ICAO types each split cleanly into 2-blade and 3-blade airframes, which is the correct physical
picture: both configurations fly under one type code, ordinary after a propeller STC.

**Two things this needs that are easy to get wrong.**

*Uniqueness is not free.* It holds only while the RPM band is narrower than the ratio between
adjacent blade counts — and **that ratio shrinks with N**. 3/2 is 1.50, but 5/4 is only 1.25,
against a band spanning 2750/2100 = 1.31. So this band separates 2 from 3 and 3 from 4, and
genuinely **cannot** separate 4 from 5 between 175 and 183 Hz, nor 5 from 6 between 210 and
229 Hz. Those windows contain real traffic — a 181.1 Hz detection sits in the corpus. Two
candidates in band must return nothing, and widening the band moves these windows rather than
removing them.

*Turboprops are a trap, not a gap.* A constant-speed propeller runs 1,600–2,000 RPM, outside
the piston band. Refusing to answer is safe; guessing is not. **A 4-blade turboprop at 1,700 RPM
puts f0 at 113.3 Hz, and 113.3 Hz read against the piston band resolves confidently to 3 blades
at 2,267 RPM.** The engine class must gate the band, and a class with no measured band gets no
answer rather than the piston one.

**An earlier approach failed and is worth recording.** The original method pooled fundamentals
by ICAO type code and required eight passes of one type before naming `N`, scoring candidates
by how consistently the implied RPM landed in band. It never fired once in 81 passes, because
type code only exists on ADS-B-matched passes — 9 of 81 — and the best-populated type reached 4.
Worse, it was about to produce a confident wrong answer: pooling the type that carried both
2-blade and 3-blade airframes, it selected a single `N`=3 and its separation test passed at 27×
the 1.5× margin it required, which would have put the 2-blade airframe at a fictional 1,535 RPM
on every pass it ever made. **Blade count is a property of the airframe, not of the type**, and
pooling an airframe property by type averages two physical configurations into neither.

### Aircraft heard that no transponder reported

84 detections with no ADS-B counterpart, the farthest at 16.4 km. ADS-B Out is not required in
US Class G airspace below 10,000 ft, so this is an expected and real population rather than an
error class — and it is the capability a passive acoustic station has that a feed-based one
does not.

This number only means something because the negatives are stored. The live detection rate was
**17 %**, computed only over ADS-B arrivals carrying an identity, with jets and rotorcraft
excluded from both numerator and denominator.

### The instrument runs unattended

Over a continuous ~9-hour window on a Raspberry Pi: capture **1.00128 blocks/s** against a
target of 1.00, live spectrogram columns **0.99899** of expected, **zero** coverage gaps, zero
analysis failures, zero analysis overruns, zero service restarts, 42.8 °C with no thermal
throttling, 11 % disk. Analysis takes ~6.8 s inside a 300 s period, so there is ample headroom.

Two deployment details that are not optional. **Automatic gain control ships enabled on USB
sound cards and must be turned off** — it varies gain dynamically, which corrupts every
amplitude-derived product, including the log-amplitude variance the turbulence measurement is
built on. And **mixer state does not survive a reboot without root**, so it must be reasserted
on every service start.

### Site shielding, measured rather than guessed

For each bearing sector: the farthest pass actually heard, and the nearest pass ADS-B says
should have been audible and was not. The gap between them is where the site is deaf, and a road
embankment, building or hillside shows up as a notch. This is a *measurement* of the site, and
it exists only because unheard passes are recorded.

### Diurnal turbulence — a weak positive

Log-amplitude variance of the blade tone, aggregated by local hour, after two fixes described
later:

| local hour | 05 | 09 | 12 | 13 | 14 | 15 | **16** | 17 | 21 |
|---|---|---|---|---|---|---|---|---|---|
| mean variance | 0.25 | 0.51 | 0.97 | 0.96 | 0.97 | 0.71 | **1.32** | 1.07 | 0.72 |

The late-afternoon maximum is where convective turbulence should be. But the swing is about
**5×** where theory predicts ~150× at matched geometry, this is a single day, several hours rest
on one or two passes, and the geometries are *not* matched — contributing slant ranges from
258 m to 16 km, which dominates the variance. The honest claim is that the measurement is
readable and not inconsistent with the prediction. It is not a detection.

---

## Negative results

These cost the most to establish and are the most useful to anyone repeating the work.

### Slant range is biased by −23 %, and speed by −9 %

Against ADS-B truth over 12 matched passes:

| quantity | bias | sign consistency |
|---|---|---|
| slant range | **−22.9 %** | **12 of 12 negative** |
| speed | **−8.6 %** | 11 of 12 negative |
| heard-CPA time | +1.23 s | — |

Four measurements characterise it. Reporting them this way — constraints rather than a story —
is the discipline that stopped a fifth wrong hypothesis.

1. **It is one error, not two.** Spearman ρ between the slant error and the speed error is
   **+0.895**. The 2.8× difference in magnitude initially read as two separate problems; the
   rank correlation says one error with a gain ratio, which is a different thing to look for.
2. **It is multiplicative, not additive.** As a ratio, fitted/true slant is 0.771 with a
   coefficient of variation of **0.161**. As a fixed offset it is −543 m with a cv of **0.869**.
   A constant fraction describes it five times better than a constant distance.
3. **It is independent of everything about the pass.** ρ against altitude +0.04, against slant
   range −0.20, against elevation angle +0.01, against `f0` +0.03, against `ε` +0.10. All nil.
4. **One apparent strong correlation is circular.** ρ against the association χ is −0.811,
   which looks like the best signal in the table until you read how χ is defined: it is built
   from `(fitted slant − true slant) / true slant`, among other terms. It *is* the slant error.
   Recorded here because it is an hour anyone would otherwise spend twice.

A near-constant multiplicative factor that ignores every geometric and quality variable is the
signature of **a wrong calibration constant, not of physics that fails to close**. That is an
encouraging form for a defect to take, and it was not resolved within this deployment.

### Fit-reported error bars were useless

**Measured slant error is 16.5× the fit's own residual.** The error model used for every
downstream product is therefore derived from paired rows against truth, not propagated from the
covariance. If you take one methodological lesson from this document, take this one: a fit
residual tells you how well a model matched its input, which is not the same question as how
far the answer is from reality.

### Temperature aloft is unrecoverable at this precision

The effective sound speed is recovered in closed form from the *disagreement* between the
acoustic speed and the ADS-B speed. That is elegant, and it means any common-mode bias in the
acoustic speed is indistinguishable from a change in the medium.

The amplification is brutal. Around 340 m/s, **1 % of speed error is about 3.4 m/s of sound
speed and about 5.8 °C of implied temperature.** With the −8.6 % speed bias above, the product
reported **67 °C mean and 198 °C worst** against a 15.6 °C surface — physically impossible
values, published with a confidence label, until the temperature was withdrawn and the
effective sound speed published in its place.

Two structural notes. The per-pass worst temperature was the per-pass worst speed error, and
the single pass whose speed came out *high* implied a plausible 7 °C — so nothing was wrong with
the inversion. And **wind survives this bias while temperature absorbs all of it**: the layer
solve gives sound speed a constant column and wind two columns in
`cos(elevation)·cos(bearing − wind bearing)`, so a common-mode offset projects predominantly
onto the constant column, which is the single parameter temperature is read from. A 25 m/s
offset applied to every sample moves the sound-speed term by 25.0 m/s and the wind vector by
under 0.5. This inverts the natural assumption that two products from one inversion share a
fate, and it is asserted by a test rather than argued in a comment.

Publishing the sound speed instead of the temperature also paid for itself immediately, because
sound speed has a known vertical structure to violate: the two populated layers read 359 and
420 m/s, both above dry air at +45 °C (355 m/s), and *rising* with height where air cools and
sound speed should fall ~3 m/s per km.

### Wind aloft was never demonstrated

11.2 m/s from 175° **± 29.8**. The uncertainty exceeds the quantity. Wind being unaffected by
the speed bias is not the same as wind being measured; it needs the same 20–30 matched passes
the bias characterisation does.

### ADS-B position noise is not identifiable from the feed

The atmospheric inversion needs an uncertainty on the ADS-B geometry, and the obvious approach
is to estimate it from the data: take the median absolute deviation of second differences of the
position series, on the reasoning that a trajectory is smooth, so its second difference is near
zero and what remains is noise.

**That reasoning is wrong at realistic polling intervals, and it is wrong by a factor of five.**
Second differences of a trajectory equal `a·dt²` for constant acceleration, so at 15–30 s
polling they measure *manoeuvring*, not noise. Measured:

| | |
|---|---|
| Live horizontal σ, median | **51.9 m** (p90 389 m, max 1,300 m) |
| Documented feed accuracy | ~10 m horizontal |
| Axes pinned at the 200 m ceiling | **28 of 158** |

On synthetic tracks with 10 m of *known* injected noise, the estimator returned 10.8 m on a
straight track — and 115 m on a 3 km-radius turn at dt=16 s, 197 m on a 5 km turn at dt=16 s,
and **719 m** on a 5 km turn at dt=30 s. Third differences cut the median error from 1285 % to
166 %, and a local-quadratic fit to 126 %: better, and still not usable.

The conclusion is that **the quantity is not identifiable from this data at this cadence**. The
feed's own documented accuracy is known where the measured one is not, and is the better input
despite being a constant rather than a measurement.

### A detection threshold could not be validated, because the environment moved

A harmonic-comb contrast gate was placed at 0.20, in what appeared to be a natural gap: over
24 hours, 22 of 22 overnight blind detections fell below 0.20 with a mean of 0.028, against a
daytime mean of 0.858, and all nine confirmed matches sat at 0.43 or above. The gap ran from
0.179 to 0.27.

On the first night with the gate live, it rejected **nothing**. The gate was being reached — 14
of 14 overnight blind detections sat above 0.20, which is what the gate guarantees — but the
population it was built to reject did not reappear:

| night | n | contrast range | mean |
|---|---|---|---|
| first (pre-gate) | 20 | −0.04 to 0.18 | 0.03 |
| second (gate live) | 14 | **0.21 to 1.33** | **0.61** |

A twenty-fold shift in the quantity the threshold was placed on, overnight, same site, same
hours — with one pass landing at 0.21, a hundredth above the threshold. Fit quality moved the
same way. **Either night is consistent with the data, and one night of each cannot say which is
typical.** The lesson is not about this threshold: it is that a gate fitted to a gap in a
single night's distribution is fitted to that night, and a backyard acoustic environment is not
stationary enough overnight to assume otherwise.

---

## Hypotheses that were measured and failed

Each of these was plausible, and each was wrong. They are listed because the pattern matters
more than the specifics: every one of them survived reasoning and died to a measurement.

**1. Octave slip in the harmonic fit.** If the fit locked onto 2× or ½× the true fundamental,
speed and range would be systematically off. Measured against the stored per-harmonic tracks:
not happening.

**2. Held or stale ADS-B positions.** A feed repeating the last known position would compress
apparent motion. Measured: positions update as expected.

**3. Over-smoothed ADS-B trajectories inflating the truth side.** The spline smoothing of
positions could plausibly have been stretching the reference slant. Forcing the smoothing
parameter to a true 10 m moved the ADS-B slant by **+0.05 %**, against a −22.9 % discrepancy.
This was the author's own hypothesis and it failed by three orders of magnitude.

**4. An altitude dependence.** The per-layer sound speeds (359 m/s low, 420 m/s high) strongly
suggested the bias grew with height. Measured per pass, Spearman ρ against altitude is **+0.04**
— nothing. The layer figures were an artifact of a weighted inversion resting on five passes in
the upper layer, and the worst-biased pass of the twelve sits among the lowest. **The claim had
already been written into the project's own validation notes and had to be withdrawn.** Aggregated
quantities from a regularised inversion at small n are not evidence about per-pass behaviour.

The useful residue of four failures is the set of constraints in the previous section. The next
step indicated — not taken within this deployment — is a sensitivity analysis rather than a
fifth hypothesis: perturb each fit input one at a time on synthetic passes with known truth, and
compute the ratio of the induced slant error to the induced speed error. The observed
fingerprint is **2.8, with both signs negative and ρ ≈ +0.9**; only one or two inputs will
reproduce it, which identifies the parameter by elimination instead of by guessing.

---

## Traps, with the measurement that exposed each

Infrastructure and tooling defects that silently corrupted results. Most cost more time than any
algorithm here.

**A warning that recurs is printed once.** Python's warning registry fires once per
(code location, message text) per process, so a warning recurring thousands of times appears
once in the log and a recurrence *rate* is invisible. A spline non-convergence warning was
believed resolved on this basis. Instrumenting it properly — `catch_warnings(record=True)` with
`simplefilter("always")`, plus a counter read and zeroed by the caller — gave **3 occurrences
against 2 printed lines** in the first reading, which is the defect demonstrating itself. If a
library warning matters, count it in code; do not infer its rate from logs.

**The same library warning had two wordings, one wrapping a newline.** FITPACK reports
non-convergence as both `A theoretically impossible result ... : s too small` and
`The maximal number of iterations maxit ... has been reached: s\ntoo small` — the second breaking
the phrase across a line break. A grep for the phrase missed **17 of 26** occurrences. Match on
the file-and-line prefix, not on the message text.

**A variance came out negative, and that one value destroyed a plot.** Log-amplitude variance is
corrected by subtracting an in-band noise estimate. The per-pass analysis correctly excluded any
harmonic whose raw variance did not clear its own noise bias by 1.5×; the hourly aggregation that
fed the dashboard subtracted unconditionally. Of 80 passes, **11 came out negative** — a variance
cannot be — the worst at **−82.7** against a real signal spanning 0.4 to 1.3. A single impossible
value set the plot's y-range, so the real signal occupied about 3 % of the plot height and read
as a flat line. The diurnal result above was invisible until this was fixed. **A guard applied in
one code path and not the other is worse than no guard**, because the inconsistency is invisible.

**A diurnal plot in UTC cannot be read.** The same plot binned by UTC hour while the rest of the
interface rendered local time. The cycle under test follows the sun over the station, so the
convective afternoon was drawn in the small hours. The defect was found by a human noticing the
plot disagreed with an expectation that turned out to be correct — which is the most reliable
bug detector in this entire project.

**`cp` is not a backup of a WAL-mode SQLite database.** A copy of the main database file taken
before a data migration missed **4.2 MB sitting in the write-ahead log**. Use the SQLite backup
API (`src.backup(dst)`), and verify the row counts in the copy against the live database.

**A thread race in a two-slice read.** A clock model held observations in two parallel lists,
appended by the capture thread, and the status thread built two NumPy arrays from two separate
slices. A single append landing between the two reads raised
`ValueError: operands could not be broadcast together with shapes (30005,) (30004,)` — a
one-element tear — which crashed the status writer and left the interface stale. Pair the lists
(`zip`) *before* slicing: a half-finished append is then not seen at all, rather than seen as a
length mismatch.

**Logs that are not where you expect.** Under systemd, `StandardOutput=append:` sends service
stdout to a file and *not* to the journal, so `journalctl` looks empty from the moment it was
added. Separately, a journal configured `Storage=volatile` means `journalctl --user -u <unit>`
finds nothing at all, while `journalctl _SYSTEMD_USER_UNIT=<unit>` works. Two different
"the logs are empty" failures with two different causes.

**A query cutoff in the future returns nothing, by construction.** A timestamp filter computed
with a timezone slip landed 12 minutes ahead of the clock and returned zero rows. Zero rows
because nothing matched and zero rows because the window had not happened yet are
indistinguishable in the output. Check the cutoff against the clock before concluding anything
from an empty result.

**NumPy 2 removed `ndarray.ptp()`.** Use `np.ptp(x)`.

---

## What limits the experiment

Two ceilings, and the second is the decisive one.

**The bias.** Slant range is off by 23 % and speed by 9 %, in a form that looks like a
calibration constant but was not identified within this deployment. Every product depending on
absolute geometry inherits it.

**The traffic, which no amount of work fixes.** The atmospheric inversion needs the geometry
from ADS-B, so it only runs on *matched* passes. This site produced about **6 matched propeller
passes per day**. A useful sounding is an aggregate of ~20 passes, which is therefore **three
days**. Even with the bias solved perfectly, a quiet single site can produce a three-day average
wind aloft — against numerical weather models that update hourly and commercial aircraft that
already report winds aloft over the same airspace at better quality. **That is a dead end on the
merits, not on effort**, and it is worth establishing from traffic counts early rather than
discovering it after a calibration problem is solved.

The products that do work share a property worth noticing: **each pass is its own datum.**
Blade count, RPM, a non-cooperative detection, a shielding measurement — none of them need
accumulation, and none need absolute range accuracy. They work at the traffic a backyard site
actually has. The atmospheric products need both, and get neither.

---

## If you are repeating this

In rough order of what will save you the most time.

1. **Count your traffic before you build anything.** Pull a day of ADS-B for your location,
   filter to propeller aircraft within your likely audible radius, and count. That number caps
   every product that needs the join, and it is a ten-minute check that can redirect months.
2. **Store your negatives from the first day.** Retrofitting a denominator is impossible; the
   passes you did not hear are gone.
3. **Build the truth comparison before the products.** The 16.5× gap between fitted and real
   error would have been invisible, and every precision figure derived from fit covariance was
   wrong by an order of magnitude.
4. **Turn off AGC**, and reassert mixer state on every service start.
5. **Learn the sample rate; never assume it.** 1,290 ppm of offset was 0.77 s over ten minutes
   on ordinary hardware.
6. **Validate against a labelled corpus before deploying**, and expect `ε` to be ~2.4× worse on
   real audio than on synthetics — which is ~6× in required passes. Calibrate thresholds against
   *real* background, not synthetic noise: pink noise peaked at 8.9 where real background reached
   17.8.
7. **Count library warnings in code.** Do not infer rates from logs.
8. **Apply every guard in every path**, and test that the aggregate path agrees with the per-item
   path. Prefer calling the same guarded function over reimplementing the computation.
9. **Plot in local time** when the hypothesis is diurnal.
10. **Expect piston aircraft to be the hard case.** They were detected at ~30 % against 80.7 %
    for turboprops, and the measurement says that is a limit rather than a setting: their median
    peak score sits *below* the real-background ceiling, so the comb is not above the noise. They
    are also most of what a backyard station hears.
11. **Do not tune a threshold to a gap seen in one night's data.** The distribution moved 20×
    overnight.
12. **When a plot disagrees with your physical expectation, suspect the plot.** Two real defects
    here were found that way, and in both cases the expectation was right.

---

## Open questions

Left unresolved, stated so they are not rediscovered from scratch.

**What is the 0.771 slant ratio?** The fingerprint is a multiplicative error independent of
range, altitude, elevation, frequency, speed and fit quality, with slant responding 2.8× harder
than speed and ρ ≈ +0.9 between them. The indicated next measurement is the per-input
sensitivity analysis described above, not another hypothesis.

**Is that ratio constant?** At n=12 it is 0.771 ± 0.046, which cannot distinguish a constant
from a 10 % drift. Roughly 80 matched passes would. If it is constant it is a calibration to be
found; if it wanders, the geometry model is wrong and the atmospheric products should be
abandoned rather than fixed.

**Would multiple stations change the traffic ceiling?** Each station independently measures the
same air mass on different bearings, so aggregating across sites attacks the pass-count problem
directly rather than waiting for traffic. This was designed for and never tested.

**Is the comb-contrast population bimodal or non-stationary?** Two nights gave means of 0.03 and
0.61. Enough nights would say whether there are two regimes or one that drifts, which decides
whether a fixed contrast gate can work at all at a backyard site.

---

## Reproducing the figures in this report

All of it comes from one SQLite database of passes plus the stored per-harmonic tracks, which is
why those tracks are stored at all. See [README.md](README.md) for setup,
[DESIGN.md](DESIGN.md) for architecture and the data model, [APPLICATIONS.md](APPLICATIONS.md)
for the derivations behind the precision estimates, and [VALIDATION.md](VALIDATION.md) for the
full chronological record of what was measured and when, including the measurements that killed
each hypothesis above.

Licensed for noncommercial and research use; see [LICENSE](LICENSE).
