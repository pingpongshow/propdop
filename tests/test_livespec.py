"""The shared-memory ring behind the live spectrogram panel.

Two processes agree on a file and a counter and never lock anything, so the tests that matter
are about what a reader sees when the writer is mid-stride: a reader that is behind, a reader
that has been lapped, and a reader whose writer restarted underneath it.
"""

import os
import threading
import time

import numpy as np
import pytest

from propdop import geo, livespec, node, pipeline, synth

RX = geo.Receiver(lat=51.23, lon=-0.98, elevation_msl_m=68.0, geoid_offset_m=46.0, name="home")


@pytest.fixture
def ring(tmp_path, monkeypatch):
    """A ring under this test's own directory, never the real runtime one."""
    monkeypatch.setenv("PROPDOP_RUNTIME", str(tmp_path))
    return str(tmp_path / "passes.sqlite")


def make_writer(db_path, n_bins=4, capacity=8):
    return livespec.Writer(livespec.ring_path(db_path), rate=8000, n_bins=n_bins,
                           f_lo=20.0, f_hi=400.0, hop_s=0.5, window_s=1.0, capacity=capacity)


def test_both_processes_find_the_same_ring_without_being_told(ring, tmp_path):
    """The capture process and the web process agree by convention, as they do for the
    status file -- `systemd --user` hands every unit the same runtime directory."""
    assert livespec.ring_path(ring) == str(tmp_path / "propdop-live-spectrogram.bin")


def test_falls_back_beside_the_database_without_a_runtime_directory(tmp_path, monkeypatch):
    """A plain `propdop run` in a terminal has no runtime directory and must still work."""
    monkeypatch.delenv("PROPDOP_RUNTIME", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    db = tmp_path / "db" / "passes.sqlite"
    assert livespec.ring_path(db) == str(tmp_path / "db" / "live_spectrogram.bin")


def test_a_reader_sees_only_what_was_published(ring):
    """The counter is written after the data, so a column is either absent or complete."""
    w = make_writer(ring)
    r = livespec.Reader(livespec.ring_path(ring))
    assert r.seq == 0
    assert r.read_since(None)[1].shape == (0, 4)

    w.append(np.full(4, 7, np.uint8))
    seq, cols = r.read_since(None)
    assert seq == 1 and cols.tolist() == [[7, 7, 7, 7]]
    w.close()
    r.close()


def test_a_reader_resumes_from_where_it_stopped(ring):
    w = make_writer(ring)
    r = livespec.Reader(livespec.ring_path(ring))
    for i in range(3):
        w.append(np.full(4, i, np.uint8))
    seq, first = r.read_since(None)
    for i in range(3, 6):
        w.append(np.full(4, i, np.uint8))
    seq2, second = r.read_since(seq)

    assert [c[0] for c in first] == [0, 1, 2]
    assert [c[0] for c in second] == [3, 4, 5]
    assert r.read_since(seq2)[1].size == 0        # nothing new, nothing repeated
    w.close()
    r.close()


def test_history_is_capped_at_the_ring_and_not_forged(ring):
    """Asking for everything gets the ring, not a reconstruction of what fell out of it."""
    w = make_writer(ring, capacity=4)
    for i in range(6):
        w.append(np.full(4, i, np.uint8))
    r = livespec.Reader(livespec.ring_path(ring))
    seq, cols = r.read_since(None)
    assert seq == 6
    assert [c[0] for c in cols] == [2, 3, 4, 5]
    w.close()
    r.close()


def test_a_reader_that_was_lapped_is_moved_on_rather_than_served_overwritten_slots(ring):
    """The failure this format has to get right.

    A reader asking from sequence 0 after the writer has gone round twice cannot be given
    what it asked for -- those slots hold something else now. It must be moved forward, never
    handed the bytes that happen to be sitting there.
    """
    w = make_writer(ring, capacity=4)
    for i in range(10):
        w.append(np.full(4, i, np.uint8))
    r = livespec.Reader(livespec.ring_path(ring))
    seq, cols = r.read_since(0)
    assert seq == 10
    assert [c[0] for c in cols] == [6, 7, 8, 9]   # the freshest ring, not slots 0-3
    w.close()
    r.close()


def test_a_restarted_writer_is_noticed(ring):
    """The capture process recreates the ring on start; a reader holding the old mapping
    would otherwise sit on a file nobody writes to any more and show a frozen panel."""
    w = make_writer(ring)
    w.append(np.full(4, 1, np.uint8))
    r = livespec.Reader(livespec.ring_path(ring))
    assert r.stale() is False
    w.close()

    w2 = make_writer(ring)
    assert r.stale() is True
    r.close()
    w2.close()


def test_open_reader_returns_none_when_nothing_publishes(ring):
    assert livespec.open_reader(ring) is None


def test_a_wrong_column_width_is_refused(ring):
    w = make_writer(ring, n_bins=4)
    with pytest.raises(ValueError):
        w.append(np.zeros(5, np.uint8))
    w.close()


def test_the_live_panel_follows_a_real_doppler_sweep(ring, tmp_path, monkeypatch):
    """End to end from the capture ring: the brightest bin has to move the way the tone does.

    A panel that merely updates is not evidence of anything -- the thing being claimed is that
    what it draws is this second's audio.
    """
    samples, _ = synth.render_scene(
        [(150.0, dict(speed=90, cpa_distance=1200, f0=95.0, duration=90.0))],
        duration=300.0, snr_db=18.0, seed=3,
    )
    cfg = pipeline.PipelineConfig(
        station=RX, station_id="home", db_path=ring,
        sample_rate=8000, archive_rate=8000, buffer_s=400.0, archive_s=400.0,
        live_hop_s=0.25, live_window_s=1.0, live_history_s=30.0,
    )
    p = pipeline.Pipeline(cfg, source=node.SyntheticSource(samples, nominal_rate=8000))
    p.node.clock.t0 = 0.0
    p.node.pump(blocks=120)                       # quiet audio, before the aircraft

    t = threading.Thread(target=p._live_spectrogram_loop, daemon=True)
    t.start()
    try:
        for _ in range(14):                       # feed the flyover through
            p.node.pump(blocks=5)
            time.sleep(0.2)
    finally:
        p._stop.set()
        t.join(timeout=5)

    r = livespec.Reader(livespec.ring_path(ring))
    seq, cols = r.read_since(None)
    assert seq >= 6, "the loop published almost nothing"
    bin_hz = np.linspace(r.f_lo, r.f_hi, r.n_bins)
    peaks = [bin_hz[int(np.argmax(c))] for c in cols[-6:]]
    r.close()

    # Past CPA the tone is receding, so every peak sits below the 95 Hz rest fundamental and
    # the sequence descends. Allowed one tie, because adjacent columns overlap by half.
    assert all(20.0 < hz < 95.0 for hz in peaks), peaks
    assert peaks[-1] < peaks[0], peaks
    assert cols.max() > cols.min(), "the panel is a flat colour, so the baseline never seeded"


def test_the_baseline_is_seeded_so_the_panel_is_not_blank_at_startup(ring, monkeypatch):
    """Taking the median over one column gives that column, so every bin reads zero.

    Without seeding, the panel is a flat grey for its first minute -- exactly when somebody has
    opened it to see whether the station is working.
    """
    rng = np.random.default_rng(0)
    n = 8000 * 120
    audio = rng.normal(0, 0.01, n) + 0.05 * np.sin(2 * np.pi * 95.0 * np.arange(n) / 8000.0)
    cfg = pipeline.PipelineConfig(
        station=RX, station_id="home", db_path=ring,
        sample_rate=8000, archive_rate=8000, buffer_s=200.0, archive_s=200.0,
        live_hop_s=0.25, live_window_s=1.0, live_history_s=20.0,
    )
    p = pipeline.Pipeline(cfg, source=node.SyntheticSource(audio, nominal_rate=8000))
    p.node.clock.t0 = 0.0
    p.node.pump(blocks=120)

    t = threading.Thread(target=p._live_spectrogram_loop, daemon=True)
    t.start()
    time.sleep(0.8)
    p._stop.set()
    t.join(timeout=5)

    r = livespec.Reader(livespec.ring_path(ring))
    cols = r.read_since(None)[1]
    r.close()
    assert cols.shape[0] >= 1
    # The steady 95 Hz tone is in the baseline too, so it does NOT stand out -- what must be
    # true is simply that the column is not uniformly one value.
    assert len(set(cols[-1].tolist())) > 3, "the first column carries no structure at all"
