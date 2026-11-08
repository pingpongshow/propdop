"""The acoustic node: continuous capture, a ring buffer, and windows on request.

Per DESIGN.md 3.3 the server *pulls* windows rather than the node pushing a stream, so a node
does as little as possible: capture, timestamp, keep ten minutes, answer "give me t1..t2", and
ship a decimated archive lazily. That also means a node tolerates network loss -- the buffer
covers the gap and it backfills when it returns.

Two timing details matter more than they look:

* **Timestamp from the frame counter, not from wall clock at write time.** Wall clock at write
  time absorbs scheduling jitter into the audio timeline, which is indistinguishable from a
  real Doppler shift.
* **Measure the actual sample rate.** Sound cards run 10-100 ppm off nominal, which is 3 ms
  over 60 s at 50 ppm -- larger than the NTP offset it would otherwise be blamed on. Recording
  the measured rate is what makes drift fittable rather than mysterious.

Capture hardware is behind `CaptureSource`, so the logic here is testable without a microphone.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
from numpy.typing import NDArray
from scipy import signal


class CaptureSource(Protocol):
    """Anything that yields consecutive mono frames at a roughly known rate."""

    nominal_rate: int

    def read(self, n_frames: int) -> NDArray[np.float64]:
        """Return exactly `n_frames` consecutive samples, blocking if need be."""


@dataclass
class SyntheticSource:
    """Replays a fixed array, optionally with a sample-clock error. For tests.

    `rate_error_ppm` simulates a real sound card's crystal offset, which is the thing the clock
    model has to discover.
    """

    samples: NDArray[np.float64]
    nominal_rate: int = 8000
    rate_error_ppm: float = 0.0
    position: int = 0

    @property
    def true_rate(self) -> float:
        return self.nominal_rate * (1.0 + self.rate_error_ppm * 1e-6)

    def read(self, n_frames: int) -> NDArray[np.float64]:
        out = np.zeros(n_frames)
        n = min(n_frames, self.samples.size - self.position)
        if n > 0:
            out[:n] = self.samples[self.position : self.position + n]
        self.position += n_frames
        return out

    @property
    def exhausted(self) -> bool:
        return self.position >= self.samples.size


@dataclass
class ClockModel:
    """Maps frame index to timestamp, and learns the device's true sample rate.

    `t(frame) = t0 + frame / rate`. The rate starts nominal and is refined from wall-clock
    observations; `drift_ppm` is then a measured property of the hardware rather than an
    assumption, and `residual_s` says how well the linear model actually holds.
    """

    nominal_rate: int
    t0: float
    rate: float = 0.0
    warmup_obs: int = 4
    _obs_frames: list[int] = field(default_factory=list)
    _obs_times: list[float] = field(default_factory=list)

    def __post_init__(self):
        if self.rate <= 0.0:
            self.rate = float(self.nominal_rate)

    def time_of(self, frame: int | NDArray) -> float | NDArray:
        return self.t0 + np.asarray(frame, dtype=float) / self.rate

    def frame_of(self, t: float | NDArray) -> NDArray:
        return np.round((np.asarray(t, dtype=float) - self.t0) * self.rate).astype(np.int64)

    def observe(self, frame: int, wall_time: float) -> None:
        """Record a (frame, wall time) pair and refit.

        The first few are discarded. The device is opened inside the first read, so the
        timestamp taken before it precedes any audio by however long the device takes to start
        -- and being at the extreme end of the fit, that point has high leverage. In deployment
        it produced an apparent 3659 ppm of drift on a device actually running within a few
        hundred ppm, which matters because 3659 ppm is 2.5 s of timestamp error over ten
        minutes, comparable to the ADS-B match tolerance itself.
        """
        self._obs_frames.append(int(frame))
        self._obs_times.append(float(wall_time))
        if len(self._obs_frames) >= self.warmup_obs + 3:
            f = np.array(self._obs_frames[self.warmup_obs :], dtype=float)
            t = np.array(self._obs_times[self.warmup_obs :], dtype=float)
            slope, intercept = np.polyfit(f, t, 1)
            if slope > 0:
                self.rate = 1.0 / slope
                self.t0 = float(intercept)

    @property
    def drift_ppm(self) -> float:
        return (self.rate / self.nominal_rate - 1.0) * 1e6

    @property
    def residual_s(self) -> float:
        if len(self._obs_frames) < self.warmup_obs + 3:
            return float("nan")
        f = np.array(self._obs_frames[self.warmup_obs :], dtype=float)
        t = np.array(self._obs_times[self.warmup_obs :], dtype=float)
        return float(np.std(t - self.time_of(f)))


class RingBuffer:
    """Fixed-duration circular store of timestamped audio.

    Ten minutes at 48 kHz is ~58 MB, so the depth is free. It is also what makes the pull
    architecture work: the server has ample slack to decide which windows it wants, and the
    acoustic arrival is at most ~20 s behind the ADS-B fix at 7 km.
    """

    def __init__(self, sample_rate: int, duration_s: float = 600.0):
        self.sample_rate = sample_rate
        self.capacity = int(round(sample_rate * duration_s))
        self._buf = np.zeros(self.capacity, dtype=np.float32)
        self._written = 0
        self._lock = threading.Lock()

    @property
    def frames_written(self) -> int:
        return self._written

    @property
    def available_frames(self) -> tuple[int, int]:
        """(first, last) frame indices still held, as a half-open range."""
        with self._lock:
            first = max(0, self._written - self.capacity)
            return first, self._written

    def append(self, samples: NDArray[np.float64]) -> None:
        """Append frames, keeping only the most recent `capacity`.

        Every frame must live at `frame % capacity`, because that is what `read_frames` relies
        on. An earlier version special-cased a write larger than the buffer by dumping its tail
        at position 0, which silently broke that mapping and returned the wrong audio for a
        valid frame range -- the worst kind of bug here, since the samples look plausible.
        """
        x = np.asarray(samples, dtype=np.float32)
        if x.size == 0:
            return
        with self._lock:
            n_keep = min(x.size, self.capacity)
            tail = x[x.size - n_keep :]
            start_frame = self._written + (x.size - n_keep)
            pos = start_frame % self.capacity
            end = pos + n_keep
            if end <= self.capacity:
                self._buf[pos:end] = tail
            else:
                split = self.capacity - pos
                self._buf[pos:] = tail[:split]
                self._buf[: end - self.capacity] = tail[split:]
            self._written += x.size

    def read_frames(self, first: int, last: int) -> NDArray[np.float64]:
        """Frames [first, last). Raises if any of it has already been overwritten."""
        if last <= first:
            return np.zeros(0)
        lo, hi = self.available_frames
        if first < lo or last > hi:
            raise IndexError(
                f"frames [{first}, {last}) outside the buffer's [{lo}, {hi})"
            )
        with self._lock:
            idx = np.arange(first, last) % self.capacity
            return self._buf[idx].astype(np.float64)


class Decimator:
    """Anti-aliased downsample for the rolling archive.

    The archive exists only to feed the blind detector for aircraft ADS-B cannot announce
    (DESIGN.md 3.1). 8 kHz is ample: a prop fundamental is 80-90 Hz and its 20th harmonic is
    1800 Hz, well inside a 4 kHz Nyquist.
    """

    def __init__(self, sample_rate: int, target_rate: int = 8000):
        self.sample_rate = sample_rate
        self.target_rate = target_rate
        self.factor = max(1, int(round(sample_rate / target_rate)))

    def __call__(self, samples: NDArray[np.float64]) -> NDArray[np.float64]:
        if self.factor == 1:
            return np.asarray(samples, dtype=float)
        return signal.decimate(np.asarray(samples, dtype=float), self.factor, ftype="fir")

    @property
    def actual_rate(self) -> float:
        return self.sample_rate / self.factor


@dataclass
class Node:
    """One acoustic node. Owns the source, the clock, the ring buffer and the archive."""

    source: CaptureSource
    station_id: str = "node"
    buffer_s: float = 600.0
    archive_rate: int = 8000
    block_s: float = 1.0
    start_time: float = 0.0

    archive_s: float = 1800.0

    def __post_init__(self):
        rate = self.source.nominal_rate
        self.ring = RingBuffer(rate, self.buffer_s)
        self.clock = ClockModel(nominal_rate=rate, t0=self.start_time)
        self.decimator = Decimator(rate, self.archive_rate)
        # A bounded ring, not a list. An unbounded accumulator is fine for a test and a memory
        # leak in a process meant to run for months.
        self.archive_ring = RingBuffer(int(self.decimator.actual_rate), self.archive_s)
        self._block = max(1, int(round(self.block_s * rate)))

    def pump(self, blocks: int = 1, wall_clock=None) -> int:
        """Read `blocks` blocks from the source into the ring buffer and the archive.

        `wall_clock(frame)` stands in for a real clock so the drift estimation is testable; in
        deployment it is `time.time()` sampled at the capture callback.
        """
        total = 0
        for _ in range(blocks):
            frame_at_start = self.ring.frames_written
            # Timestamp BEFORE the read: `read` blocks for a whole block, so sampling the
            # clock afterwards pairs the block's *end* time with its *start* frame. Over a
            # short baseline that one-block offset looks like an enormous rate error -- it
            # showed up in deployment as 3598 ppm of apparent drift on a crystal that is
            # really within ~100 ppm.
            t_at_start = wall_clock(frame_at_start) if wall_clock is not None else None
            x = self.source.read(self._block)
            self.ring.append(x)
            self.archive_ring.append(self.decimator(x))
            if t_at_start is not None:
                self.clock.observe(frame_at_start, t_at_start)
            total += x.size
        return total

    def read_window(self, t_start: float, t_end: float) -> tuple[NDArray[np.float64], float]:
        """Audio between two timestamps, plus the timestamp actually delivered.

        This is the whole node-facing API: the server asks for the acoustic window it computed
        from a retarded-time solve, and gets samples back.
        """
        first = int(self.clock.frame_of(t_start))
        last = int(self.clock.frame_of(t_end))
        samples = self.ring.read_frames(first, last)
        return samples, float(self.clock.time_of(first))

    def archived(self, seconds: float | None = None) -> NDArray[np.float64]:
        """The decimated rolling archive, most recent `seconds` (all of it by default)."""
        lo, hi = self.archive_ring.available_frames
        if seconds is not None:
            lo = max(lo, hi - int(seconds * self.archive_ring.sample_rate))
        return self.archive_ring.read_frames(lo, hi)

    def archive_time_of(self, frame: int) -> float:
        """Timestamp of an archive frame, on the same clock as `read_window`."""
        return float(self.clock.t0 + frame / self.archive_ring.sample_rate)

    def status(self) -> dict:
        lo, hi = self.ring.available_frames
        return {
            "station_id": self.station_id,
            "sample_rate_nominal": self.source.nominal_rate,
            "sample_rate_measured": self.clock.rate,
            "drift_ppm": self.clock.drift_ppm,
            "clock_residual_s": self.clock.residual_s,
            "buffer_span_s": (hi - lo) / self.clock.rate,
            "oldest_time": float(self.clock.time_of(lo)),
            "newest_time": float(self.clock.time_of(hi)),
            "archive_rate": self.decimator.actual_rate,
            "archive_frames": self.archive_ring.frames_written,
        }


@dataclass
class AlsaSource:
    """Capture from an ALSA device by piping `arecord`.

    Deliberately a subprocess rather than a PortAudio binding: `arecord` is already the
    reliable path on a headless host, it needs no Python audio library, and it keeps the image
    free of a native dependency that would have to be built per architecture. The host itself
    need not have ALSA tooling -- only the container does, with `/dev/snd` passed through.

    Defaults match the device tested on the deployment host: mono, S16_LE, 48 kHz.
    """

    device: str = "plughw:0,0"
    nominal_rate: int = 48000
    channels: int = 1
    binary: str = "arecord"
    _proc: object | None = field(default=None, repr=False)

    def start(self) -> "AlsaSource":
        import subprocess

        if self._proc is not None:
            return self
        self._proc = subprocess.Popen(
            [
                self.binary,
                "-D", self.device,
                "-f", "S16_LE",
                "-r", str(self.nominal_rate),
                "-c", str(self.channels),
                "-t", "raw",
                "-q",
                "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        return self

    def read(self, n_frames: int) -> NDArray[np.float64]:
        if self._proc is None:
            self.start()
        want = n_frames * 2 * self.channels
        buf = bytearray()
        while len(buf) < want:
            chunk = self._proc.stdout.read(want - len(buf))  # type: ignore[union-attr]
            if not chunk:
                break  # device went away; pad with silence rather than deadlock
            buf.extend(chunk)
        buf.extend(b"\x00" * (want - len(buf)))
        x = np.frombuffer(bytes(buf), dtype="<i2").astype(np.float64) / 32768.0
        if self.channels > 1:
            x = x.reshape(-1, self.channels).mean(axis=1)
        return x

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
            self._proc = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
