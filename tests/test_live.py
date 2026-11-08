"""Live deployment pieces: internet ADS-B, indoor interference, ALSA capture.

These cover the configuration actually deployed -- an indoor USB microphone and no local
ADS-B receiver -- rather than the idealised one.
"""

import numpy as np
import pytest

from propdop import adsb, detect, node, synth


# -- internet ADS-B --------------------------------------------------------------------


def test_adsb_lol_payload_normalises_to_readsb_shape():
    payload = {
        "now": 1700.0,
        "ac": [
            {"hex": "a1b2c3", "lat": 51.23, "lon": -0.98, "alt_geom": 3000, "t": "C172",
             "r": "N12345", "seen_pos": 0.4},
        ],
    }
    snap = adsb.normalise_adsb_lol(payload)
    assert snap["now"] == 1700.0 and len(snap["aircraft"]) == 1
    assert snap["aircraft"][0]["hex"] == "a1b2c3"


def test_query_centre_is_rounded_before_it_leaves_the_machine():
    """A microphone's exact coordinates are the sensitive thing in this project; the search
    radius is tens of km, so rounding the centre costs no traffic.

    The coordinates here are fictional on purpose. A test that pinned the behaviour using the
    deployed station's real position would defeat the point of the check it is verifying --
    and tests are tracked by git.
    """
    seen = {}

    def fake_open(url, timeout=0):
        seen["url"] = url

        class R:
            def read(self):
                return b'{"now": 1.0, "ac": []}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R()

    import urllib.request

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_open
    try:
        adsb.fetch_adsb_lol(51.2345678, -0.9876543, round_deg=0.1)
    finally:
        urllib.request.urlopen = orig

    assert "51.2000" in seen["url"] and "-1.0000" in seen["url"]
    assert "51.2345" not in seen["url"] and "9876" not in seen["url"]


def test_live_tracks_assemble_from_polled_snapshots():
    """Tracks must build from repeated snapshots the same way as from a local receiver."""
    snaps = []
    for i in range(8):
        t = 1000.0 + i * 15.0
        snaps.append(
            {
                "now": t,
                "ac": [
                    {
                        "hex": "aa0001",
                        "lat": 51.23 + 0.004 * i,
                        "lon": -0.98,
                        "alt_geom": 4000,
                        "t": "C172",
                        "r": "N1",
                        "seen_pos": 0.2,
                    }
                ],
            }
        )
    calls = iter(snaps)
    tracks = adsb.collect_live_tracks(
        51.23, -0.98, duration_s=120.0, interval_s=15.0,
        sleep=lambda s: None, fetch=lambda: adsb.normalise_adsb_lol(next(calls)),
    )
    assert len(tracks) == 1
    assert len(tracks[0]) == 8
    assert tracks[0].type_code == "C172"


def test_a_failed_poll_does_not_end_collection():
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] == 2:
            raise OSError("transient")
        return {
            "now": 1000.0 + state["n"],
            "aircraft": [
                {"hex": "bb", "lat": 51.23 + 0.001 * state["n"], "lon": -0.98,
                 "alt_geom": 3000, "seen_pos": 0.1}
            ],
        }

    tracks = adsb.collect_live_tracks(
        51.23, -0.98, duration_s=75.0, interval_s=15.0, sleep=lambda s: None, fetch=flaky
    )
    assert tracks and len(tracks[0]) == 4  # five polls, one dropped


# -- indoor interference ---------------------------------------------------------------


def mains_comb(n, rate, amp=1.0, f=60.0, harmonics=6, seed=0):
    """60 Hz and its harmonics -- a stationary comb straight through the propeller band,
    and the dominant tone measured on the deployed indoor microphone."""
    rng = np.random.default_rng(seed)
    t = np.arange(n) / rate
    x = np.zeros(n)
    for k in range(1, harmonics + 1):
        x += (amp / k) * np.sin(2 * np.pi * k * f * t + rng.uniform(0, 6.28))
    return x


@pytest.mark.parametrize("amp", [0.5, 2.0])
def test_mains_hum_is_not_mistaken_for_an_aircraft(amp):
    """The measured indoor noise floor is dominated by 60 Hz and harmonics. A harmonic comb
    is exactly what the detector looks for, so this is the obvious way to fool it -- but a
    stationary comb has no sweep, and the templates require both halves of one."""
    rate, n = 8000, 8000 * 200
    rng = np.random.default_rng(3)
    x = mains_comb(n, rate, amp=amp) + synth._pink_noise(n, -1.5, rng) * 0.3
    x = x / np.max(np.abs(x)) * 0.9
    dets = detect.detect(x, rate, band=(50.0, 300.0), n_harmonics=4, min_score=0.0)
    assert not [d for d in dets if d.is_aircraft]
    assert max((d.template_score for d in dets), default=0.0) < detect.MIN_TEMPLATE_SCORE


def test_a_real_pass_survives_under_mains_hum():
    """Rejecting hum is only useful if the signal still gets through it."""
    rate = 8000
    scene = [(120.0, dict(speed=90, cpa_distance=2500, f0=97.0, duration=140.0))]
    air, _ = synth.render_scene(scene, duration=240.0, snr_db=6.0, seed=4)
    mixed = air + mains_comb(air.size, rate, amp=0.8)
    mixed = mixed / np.max(np.abs(mixed)) * 0.9
    found = [d for d in detect.detect(mixed, rate, band=(50.0, 300.0)) if d.is_aircraft]
    assert len(found) == 1
    assert found[0].t0_heard == pytest.approx(120.0, abs=8.0)
    assert found[0].f0 == pytest.approx(97.0, rel=0.05)


# -- ALSA capture ----------------------------------------------------------------------


def test_alsa_source_builds_the_expected_arecord_command(monkeypatch):
    captured = {}

    class FakeProc:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            self.stdout = self

        def read(self, n):
            return b"\x00\x01" * (n // 2)

        def terminate(self):
            captured["terminated"] = True

        def wait(self, timeout=None):
            return 0

    import subprocess

    monkeypatch.setattr(subprocess, "Popen", FakeProc)
    src = node.AlsaSource(device="plughw:0,0", nominal_rate=48000)
    with src:
        x = src.read(1024)
    argv = captured["argv"]
    assert argv[0] == "arecord"
    assert "plughw:0,0" in argv and "S16_LE" in argv and "48000" in argv
    assert "raw" in argv  # must be headerless, or the WAV header corrupts the first samples
    assert x.size == 1024 and np.all(np.abs(x) <= 1.0)
    assert captured.get("terminated")


def test_alsa_source_pads_rather_than_deadlocking_when_the_device_stops(monkeypatch):
    class DeadProc:
        def __init__(self, argv, **kw):
            self.stdout = self

        def read(self, n):
            return b""

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    import subprocess

    monkeypatch.setattr(subprocess, "Popen", DeadProc)
    x = node.AlsaSource().start().read(512)
    assert x.size == 512 and np.allclose(x, 0.0)


def test_node_runs_on_an_alsa_style_source(monkeypatch):
    """The capture path end to end, with a synthetic flyover standing in for the device."""
    fo = synth.Flyover(seed=7, snr_db=15, speed=90, cpa_distance=2000, f0=90.0, duration=60.0)
    samples, _ = synth.render(fo)
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()

    class PcmProc:
        def __init__(self, argv, **kw):
            self.stdout = self
            self.pos = 0

        def read(self, n):
            out = pcm[self.pos : self.pos + n]
            self.pos += n
            return out

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    import subprocess

    monkeypatch.setattr(subprocess, "Popen", PcmProc)
    src = node.AlsaSource(nominal_rate=fo.sample_rate).start()
    nd = node.Node(source=src, buffer_s=120.0, start_time=0.0)
    nd.pump(blocks=60)
    window, _ = nd.read_window(5.0, 55.0)
    assert window.size == pytest.approx(50 * fo.sample_rate, abs=2)
    assert float(np.sqrt((window**2).mean())) > 0.01


def test_millisecond_timestamps_are_normalised():
    """adsb.lol serves `now` in milliseconds while `seen_pos` stays in seconds.

    Taken at face value this stretches every track's time span by 1000x and divides every
    speed by 1000, so a 90 m/s aircraft reports 0.09 m/s and is then dropped by the >=30 m/s
    audibility filter. The join produces nothing while every component reports success -- which
    is exactly what happened in deployment, for hours, undetected.
    """
    ms = 1794098122501.0
    snap = adsb.normalise_adsb_lol({"now": ms, "ac": []})
    assert snap["now"] == pytest.approx(ms / 1000.0)
    # a source already in seconds must be left alone
    assert adsb.normalise_adsb_lol({"now": 1794098122.5, "ac": []})["now"] == pytest.approx(
        1794098122.5
    )


def test_speed_survives_millisecond_timestamps_end_to_end():
    """The check that would have caught it: build tracks from millisecond snapshots and
    confirm the implied speed is aviation-plausible rather than a thousandth of it."""
    from propdop import geo

    rx = geo.Receiver(lat=51.23, lon=-0.98, elevation_msl_m=68.0, geoid_offset_m=46.0)
    truth = adsb.synthetic_track(rx, 90.0, 2500.0, 2000.0, heading_deg=40.0, t_cpa=0.0,
                                 half_span_s=60.0, rate_hz=1.0)
    base_ms = 1794098122501.0
    snaps = []
    for i in range(8):
        t = -40.0 + i * 12.0
        snaps.append(
            adsb.normalise_adsb_lol(
                {
                    "now": base_ms + (t + 40.0) * 1000.0,
                    "ac": [
                        {
                            "hex": "aa0001", "t": "C172", "r": "N1", "seen_pos": 0.2,
                            "lat": float(np.interp(t, truth.times, truth.lat)),
                            "lon": float(np.interp(t, truth.times, truth.lon)),
                            "alt_geom": float(
                                np.interp(t, truth.times, truth.alt_hae_m) / 0.3048
                            ),
                        }
                    ],
                }
            )
        )
    tracks = adsb.tracks_from_readsb(snaps)
    assert len(tracks) == 1
    g = adsb.pass_geometry(tracks[0], rx)
    assert g is not None
    assert g.speed_mps == pytest.approx(90.0, rel=0.25), f"got {g.speed_mps:.2f} m/s"
    # and it must now survive the audibility filter that was silently rejecting everything
    assert adsb.audible_candidates(tracks, rx)
