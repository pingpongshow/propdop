"""Node capture: ring buffer, clock model, and windows on request."""

import numpy as np
import pytest

from propdop import detect, fit, node, synth


def make_node(samples, rate=8000, ppm=0.0, buffer_s=600.0, t0=1000.0):
    src = node.SyntheticSource(samples, nominal_rate=rate, rate_error_ppm=ppm)
    return node.Node(source=src, buffer_s=buffer_s, start_time=t0), src


def test_ring_buffer_round_trips_and_wraps():
    rb = node.RingBuffer(sample_rate=100, duration_s=1.0)  # 100 frames
    rb.append(np.arange(60, dtype=float))
    assert rb.available_frames == (0, 60)
    assert np.allclose(rb.read_frames(10, 20), np.arange(10, 20))

    rb.append(np.arange(60, 160, dtype=float))  # wraps
    lo, hi = rb.available_frames
    assert (lo, hi) == (60, 160)
    assert np.allclose(rb.read_frames(150, 160), np.arange(150, 160))
    with pytest.raises(IndexError):
        rb.read_frames(0, 10)  # overwritten, and must say so rather than return rubbish


def test_ring_buffer_handles_a_write_larger_than_itself():
    rb = node.RingBuffer(sample_rate=100, duration_s=1.0)
    rb.append(np.arange(250, dtype=float))
    lo, hi = rb.available_frames
    assert (lo, hi) == (150, 250)
    assert np.allclose(rb.read_frames(240, 250), np.arange(240, 250))


def test_clock_timestamps_come_from_the_frame_counter():
    """Not from wall clock at write time: that would fold scheduling jitter into the audio
    timeline, which is indistinguishable from a real Doppler shift."""
    clk = node.ClockModel(nominal_rate=8000, t0=1000.0)
    assert clk.time_of(0) == pytest.approx(1000.0)
    assert clk.time_of(8000) == pytest.approx(1001.0)
    assert int(clk.frame_of(1001.0)) == 8000


def test_clock_measures_the_sound_card_drift():
    """50 ppm is 3 ms over 60 s -- bigger than the NTP offset it would otherwise be blamed on."""
    true_ppm = 50.0
    clk = node.ClockModel(nominal_rate=8000, t0=0.0)
    true_rate = 8000 * (1 + true_ppm * 1e-6)
    for frame in range(0, 8000 * 600, 8000 * 30):
        clk.observe(frame, frame / true_rate)
    assert clk.drift_ppm == pytest.approx(true_ppm, abs=2.0)
    assert clk.rate == pytest.approx(true_rate, rel=1e-6)
    assert clk.residual_s < 1e-6


def test_window_request_returns_the_right_audio():
    """The node's whole API: the server asks for a computed acoustic window."""
    rate = 8000
    x = np.sin(2 * np.pi * 100.0 * np.arange(rate * 120) / rate)
    nd, _ = make_node(x, rate=rate, t0=5000.0)
    nd.pump(blocks=120)

    got, t_actual = nd.read_window(5030.0, 5040.0)
    assert got.size == pytest.approx(10 * rate, abs=2)
    assert t_actual == pytest.approx(5030.0, abs=1.0 / rate)
    expected = x[int(30 * rate) : int(30 * rate) + got.size]
    assert np.allclose(got, expected, atol=1e-6)


def test_window_outside_the_buffer_is_refused():
    rate = 8000
    nd, _ = make_node(np.zeros(rate * 120), rate=rate, buffer_s=10.0, t0=0.0)
    nd.pump(blocks=120)
    with pytest.raises(IndexError):
        nd.read_window(0.0, 5.0)  # long since overwritten


def test_archive_is_decimated_for_the_blind_detector():
    rate = 48000
    nd, _ = make_node(np.zeros(rate * 5), rate=rate, t0=0.0)
    nd.pump(blocks=5)
    assert nd.decimator.actual_rate == pytest.approx(8000.0)
    assert nd.archived().size == pytest.approx(5 * 8000, rel=0.01)
    st = nd.status()
    assert st["archive_rate"] == pytest.approx(8000.0)
    assert st["buffer_span_s"] == pytest.approx(5.0, abs=0.1)


def test_decimation_preserves_the_propeller_band():
    """8 kHz is ample: a prop fundamental is ~90 Hz and its 20th harmonic is 1800 Hz."""
    rate = 48000
    t = np.arange(rate * 4) / rate
    x = np.sin(2 * np.pi * 90 * t) + 0.5 * np.sin(2 * np.pi * 1800 * t)
    dec = node.Decimator(rate, 8000)
    y = dec(x)
    f = np.fft.rfftfreq(y.size, 1.0 / dec.actual_rate)
    mag = np.abs(np.fft.rfft(y))
    for want in (90.0, 1800.0):
        i = int(np.argmin(np.abs(f - want)))
        peak = mag[max(0, i - 3) : i + 4].max()
        assert peak > 0.2 * mag.max(), f"{want} Hz did not survive decimation"


def test_a_pass_captured_through_the_node_still_fits():
    """End to end: audio goes in through capture and comes out measurable.

    Includes a 60 ppm sample-clock error, which is realistic hardware and must not wreck the
    fit -- the Doppler model absorbs a small rate error, which is exactly why single-mic work
    needs no special timing.
    """
    fo = synth.Flyover(seed=7, snr_db=15, speed=90, cpa_distance=2000, f0=90.0, duration=120.0)
    samples, truth = synth.render(fo)
    nd, _ = make_node(samples, rate=fo.sample_rate, ppm=60.0, t0=10_000.0)
    nd.pump(blocks=120)

    window, t_actual = nd.read_window(10_000.0, 10_120.0)
    r = fit.fit_comb(window, fo.sample_rate, band=(50.0, 300.0), n_harmonics=4)
    assert r.f0 == pytest.approx(truth["f0"], rel=0.02)
    assert r.speed == pytest.approx(truth["speed"], rel=0.10)
    assert r.epsilon < 0.02


def test_blind_detection_runs_on_the_decimated_archive():
    """The archive's only job is feeding the detector for aircraft ADS-B cannot announce."""
    scene = [(90.0, dict(speed=90, cpa_distance=2000, f0=90.0, duration=120.0))]
    samples, _ = synth.render_scene(scene, duration=200.0, snr_db=15.0, seed=4)
    nd, _ = make_node(samples, rate=8000, t0=0.0)
    nd.pump(blocks=200)
    dets = detect.detect(nd.archived(), int(nd.decimator.actual_rate), band=(50.0, 300.0))
    assert any(d.is_aircraft for d in dets)


def test_clock_is_sampled_before_the_blocking_read():
    """`read` blocks for a whole block, so timestamping after it pairs the block's end time
    with its start frame. In deployment that one-block offset read as 3598 ppm of drift on a
    crystal that is really within ~100 ppm."""
    rate, block = 8000, 8000
    fake_time = {"t": 1000.0}

    class SlowSource:
        nominal_rate = rate

        def read(self, n):
            fake_time["t"] += n / rate  # the read itself consumes real time
            return np.zeros(n)

    nd = node.Node(source=SlowSource(), buffer_s=60.0, start_time=1000.0)
    nd.pump(blocks=12, wall_clock=lambda frame: fake_time["t"])
    # a perfect clock must come out as a perfect clock, not as a one-block error
    assert abs(nd.clock.drift_ppm) < 500.0


def test_clock_discards_the_device_startup_transient():
    """The device opens inside the first read, so the timestamp taken before it precedes any
    audio. At the end of the fit that point has high leverage: in deployment it produced
    3659 ppm of apparent drift, which is 2.5 s of timestamp error over ten minutes."""
    rate = 8000
    clk = node.ClockModel(nominal_rate=rate, t0=0.0)
    startup = 2.5  # seconds the device took to start producing audio
    for i in range(60):
        frame = i * rate
        wall = (0.0 if i == 0 else startup + i * 1.0)
        clk.observe(frame, wall)
    assert abs(clk.drift_ppm) < 500.0, f"startup transient leaked in: {clk.drift_ppm:.0f} ppm"
