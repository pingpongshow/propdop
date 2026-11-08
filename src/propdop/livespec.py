"""A shared-memory ring of spectrogram columns, so the dashboard can show audio as it arrives.

ALSA capture is exclusive, so the web process cannot open the microphone; the two processes
have always talked through files. The published `spectrogram.npz` is written once a heartbeat
and covers the preceding minute, which makes the dashboard panel a photograph up to 30 s old.

This is the same idea at the opposite cadence. One fixed-size file, mapped by both processes,
holding the last few minutes as single STFT columns. The writer fills a slot and *then*
publishes it by incrementing a counter; a reader takes the counter first, reads the slots
behind it, and re-reads the counter to confirm the window did not move underneath it. That is
a seqlock, and it is enough here because there is exactly one writer and the readers are
minutes behind being lapped.

Nothing is allocated, created or renamed per column, which is what makes 2 Hz affordable:
`mmap` writes land in the page cache and the same pages are rewritten for ever. On a host with
a runtime directory the file lives in tmpfs and never reaches the SD card at all.

The format is deliberately trivial -- a 64-byte header and `capacity` rows of `n_bins` bytes.
Columns are already quantised to the display range, so a reader needs no numpy to serve them
and the browser needs no decoding beyond a palette lookup.
"""

from __future__ import annotations

import mmap
import os
import struct

import numpy as np
from numpy.typing import NDArray

MAGIC = b"PDLIVE01"
# magic, archive rate, bins per column, band lo/hi, hop, window, dB range, capacity, sequence.
# The dB range is part of the format, not a convention: a column is already quantised when it
# is written, so a reader that does not know the range cannot label the colour bar and cannot
# say what any pixel means.
_HEADER = struct.Struct("<8sIIffffffIQ")
HEADER_BYTES = 64
_SEQ_OFFSET = _HEADER.size - 8


def ring_path(db_path: str | os.PathLike) -> str:
    """Where both processes agree the ring lives.

    A runtime directory is tmpfs, which is the right home for something rewritten twice a
    second and worthless after a reboot. `systemd --user` exports the same `XDG_RUNTIME_DIR`
    to every unit of the same user, so the capture process and the web process land on one
    path without either being told it. Falling back beside the database keeps a plain
    `propdop run` in a terminal working.
    """
    runtime = os.environ.get("PROPDOP_RUNTIME") or os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return os.path.join(runtime, "propdop-live-spectrogram.bin")
    return os.path.join(os.path.dirname(str(db_path)) or ".", "live_spectrogram.bin")


class Writer:
    """The capture process's end. One per station; not thread-safe by design."""

    def __init__(self, path, *, rate, n_bins, f_lo, f_hi, hop_s, window_s, capacity,
                 db_lo=-6.0, db_hi=24.0):
        self.path = str(path)
        self.n_bins = int(n_bins)
        self.capacity = int(capacity)
        size = HEADER_BYTES + self.capacity * self.n_bins
        # Always recreate. A ring left by a previous run describes audio that no longer
        # exists, and reusing one risks inheriting a geometry this run did not choose.
        tmp = self.path + ".new"
        with open(tmp, "wb") as fh:
            fh.write(
                _HEADER.pack(MAGIC, int(rate), self.n_bins, float(f_lo), float(f_hi),
                             float(hop_s), float(window_s), float(db_lo), float(db_hi),
                             self.capacity, 0)
            )
            fh.write(b"\x00" * (size - _HEADER.size))
        os.replace(tmp, self.path)
        self._fh = open(self.path, "r+b")
        self._mm = mmap.mmap(self._fh.fileno(), size)
        self._seq = 0

    @property
    def count(self) -> int:
        """Columns published since this writer opened the ring."""
        return self._seq

    def append(self, column: NDArray[np.uint8]) -> int:
        """Publish one column. The data is written before the counter that advertises it."""
        col = np.ascontiguousarray(column, dtype=np.uint8)
        if col.size != self.n_bins:
            raise ValueError(f"column has {col.size} bins, ring has {self.n_bins}")
        off = HEADER_BYTES + (self._seq % self.capacity) * self.n_bins
        self._mm[off : off + self.n_bins] = col.tobytes()
        self._seq += 1
        self._mm[_SEQ_OFFSET : _SEQ_OFFSET + 8] = struct.pack("<Q", self._seq)
        return self._seq

    def close(self) -> None:
        try:
            self._mm.close()
        finally:
            self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Reader:
    """The web process's end. Read-only, and tolerant of a writer that restarts."""

    def __init__(self, path):
        self.path = str(path)
        self._fh = open(self.path, "rb")
        st = os.fstat(self._fh.fileno())
        self._ident = (st.st_dev, st.st_ino)
        self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        (magic, self.rate, self.n_bins, self.f_lo, self.f_hi, self.hop_s, self.window_s,
         self.db_lo, self.db_hi, self.capacity, _) = _HEADER.unpack(
            self._mm[: _HEADER.size]
        )
        if magic != MAGIC:
            self.close()
            raise ValueError("not a PropDop live spectrogram ring")

    @property
    def seq(self) -> int:
        return struct.unpack("<Q", self._mm[_SEQ_OFFSET : _SEQ_OFFSET + 8])[0]

    def stale(self) -> bool:
        """True once the writer has replaced the file, so the caller can reopen."""
        try:
            st = os.stat(self.path)
        except OSError:
            return True
        return (st.st_dev, st.st_ino) != self._ident

    def read_since(self, since: int | None) -> tuple[int, NDArray[np.uint8]]:
        """Columns published after `since`; `None` means "as much history as there is".

        Returns the sequence those columns end at, so the caller can ask again from there.
        A caller that has fallen more than `capacity` behind cannot be served what it asked
        for, and is given the freshest full ring instead -- the alternative is to hand back
        slots the writer has already overwritten.
        """
        end = self.seq
        oldest = max(0, end - self.capacity)
        start = oldest if since is None else max(int(since), oldest)
        if start >= end:
            return end, np.empty((0, self.n_bins), dtype=np.uint8)

        flat = np.frombuffer(self._mm, dtype=np.uint8, offset=HEADER_BYTES,
                             count=self.capacity * self.n_bins)
        idx = np.arange(start, end) % self.capacity
        cols = flat.reshape(self.capacity, self.n_bins)[idx].copy()

        # Re-read the counter: if the writer lapped us while we copied, the oldest of those
        # rows are now something else, so say so by starting again from what is certainly new.
        if self.seq - start > self.capacity:
            return self.seq, np.empty((0, self.n_bins), dtype=np.uint8)
        return end, cols

    def close(self) -> None:
        try:
            self._mm.close()
        finally:
            self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_reader(db_path: str | os.PathLike) -> Reader | None:
    """A reader for this station's ring, or None when the capture process publishes none."""
    try:
        return Reader(ring_path(db_path))
    except (OSError, ValueError):
        return None
