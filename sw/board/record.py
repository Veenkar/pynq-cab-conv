# Recorder daemon: saves the dry input and the convolved output to two mono
# 24-bit 48 kHz WAVs while CTRL[6] REC is set (software or BTN0).
# Reads cab_conv registers only (no overlay reload, codec untouched).
#   sudo -i python3 ~xilinx/cab_conv/record.py [--dir D] [--once] [--start]
# Files: <dir>/rec_YYYYmmdd_HHMMSS_dry.wav + _wet.wav, sample-aligned
# (default dir ~xilinx/cab_conv/rec).
# The WAV headers are patched every ~2 s, so a power cut leaves usable files.
import argparse, math, os, signal, struct, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/home/xilinx/cab_conv")
from cab_conv import CaptureMixin, CaptureOverflow, MMIO_RANGE, REG_ID, ID_VALUE

FS = 48000


class WavWriter:
    """Incremental 24-bit PCM WAV (any channel count) with a header that is always valid
    on disk (patched every `patch_s` seconds and on close)."""

    def __init__(self, path, fs=FS, channels=2, patch_s=2.0):
        self.path, self.fs, self.ch, self.patch_s = path, fs, channels, patch_s
        self.f = open(path, "wb")
        self.frames = 0
        self._t_patch = time.time()
        self.f.write(self._header(0))
        self.f.flush()

    def _header(self, nframes):
        ba = self.ch * 3
        dlen = nframes * ba
        return (b"RIFF" + struct.pack("<I", 36 + dlen) + b"WAVEfmt " +
                struct.pack("<IHHIIHH", 16, 1, self.ch, self.fs, self.fs * ba, ba, 24) +
                b"data" + struct.pack("<I", dlen))

    def write(self, frames):
        """frames: int array (n, ch) of signed 24-bit values."""
        a = np.ascontiguousarray(frames, dtype="<i4")
        if a.size == 0:
            return
        self.f.write(a.view(np.uint8).reshape(-1, 4)[:, :3].tobytes())
        self.frames += a.shape[0]
        if time.time() - self._t_patch >= self.patch_s:
            self.patch()

    def patch(self):
        self.f.flush()
        pos = self.f.tell()
        self.f.seek(0)
        self.f.write(self._header(self.frames))
        self.f.seek(pos)
        self.f.flush()
        os.fsync(self.f.fileno())
        self._t_patch = time.time()

    def close(self):
        if self.f:
            self.patch()
            self.f.close()
            self.f = None


def db(v):
    return 20 * math.log10(v / 8388607.0) if v else -999.0


class Recorder(CaptureMixin):
    def __init__(self, mmio, outdir):
        self.mmio, self.outdir = mmio, outdir
        self.w = None               # [dry, wet] writers while recording
        self.rptr = 0
        self.lost = 0
        self.pk = [0, 0]
        self.t_print = 0.0

    def open(self):
        if not os.path.isdir(self.outdir):
            os.makedirs(self.outdir)
            # a new dir gets the parent's owner (xilinx, not root under sudo)
            self._chown(self.outdir, os.path.dirname(os.path.abspath(self.outdir)))
        base = os.path.join(self.outdir, time.strftime("rec_%Y%m%d_%H%M%S"))
        self.w = [WavWriter(base + "_dry.wav", channels=1),
                  WavWriter(base + "_wet.wav", channels=1)]
        for w in self.w:
            self._chown(w.path, self.outdir)
        self.rptr, self.lost, self.pk = 0, 0, [0, 0]
        self.t_print = time.time()
        print("recording -> %s_{dry,wet}.wav" % base, flush=True)

    def _write(self, d):
        for c, w in enumerate(self.w):
            w.write(d[:, c:c + 1])

    def _chown(self, path, like):
        try:
            st = os.stat(like)
            os.chown(path, st.st_uid, st.st_gid)
        except (OSError, AttributeError):
            pass

    def drain(self):
        """Read everything available; on overflow, pad with zeros and go on."""
        try:
            self.rptr, d = self.read_capture(self.rptr)
        except CaptureOverflow as e:
            self.lost += e.lost
            print("OVERFLOW: %d frames lost (zeros inserted)" % e.lost, flush=True)
            n = (e.wptr - self.rptr) & 0xFFFFFFFF
            self._write(np.zeros((n, 2), dtype=np.int32))
            self.rptr = e.wptr
            return
        if len(d):
            self._write(d)
            self.pk[0] = max(self.pk[0], int(np.abs(d[:, 0]).max()))
            self.pk[1] = max(self.pk[1], int(np.abs(d[:, 1]).max()))
        now = time.time()
        if now - self.t_print >= 1.0:
            print("%7.1f s  dry %6.1f dBFS  wet %6.1f dBFS  lost %d"
                  % (self.w[0].frames / FS, db(self.pk[0]), db(self.pk[1]), self.lost), flush=True)
            self.pk, self.t_print = [0, 0], now

    def close(self):
        if self.w:
            try:
                self.drain()        # REC is off by now, the ring is stable
            except Exception as e:
                print("final drain failed: %s" % e, flush=True)
            for w in self.w:
                w.close()
            print("closed %s + _wet.wav: %.1f s, %d frames lost" %
                  (self.w[0].path, self.w[0].frames / FS, self.lost), flush=True)
            self.w = None


def main():
    ap = argparse.ArgumentParser(description="Record dry and wet to two mono 24-bit WAVs while REC is set")
    ap.add_argument("--dir", default=os.path.expanduser("~xilinx/cab_conv/rec"))
    ap.add_argument("--once", action="store_true",
                    help="record one take (the next REC) until REC=0, then exit")
    ap.add_argument("--start", action="store_true", help="set REC now (software start)")
    ap.add_argument("--base", type=lambda x: int(x, 0), default=0x43C00000, help="IP base address")
    ap.add_argument("--poll-ms", type=float, default=20.0)
    a = ap.parse_args()

    from pynq import MMIO
    mm = MMIO(a.base, MMIO_RANGE)
    if mm.read(REG_ID) != ID_VALUE:
        sys.exit("cab_conv overlay not loaded")
    rec = Recorder(mm, a.dir)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))

    if a.start:
        rec.rec_start()
    if a.once and not rec.recording:
        print("waiting for REC (BTN0 or CTRL[6])...", flush=True)
    done = False
    try:
        while not stop and not done:
            on = rec.recording
            if on and rec.w is None:
                rec.open()
            if rec.w is not None:
                rec.drain()
                if not on:
                    rec.close()
                    done = a.once
            time.sleep(a.poll_ms / 1000.0)
    finally:
        if rec.w is not None:
            if stop and a.once:
                rec.rec_stop()      # Ctrl+C in a one-shot take also ends the recording
            rec.close()


if __name__ == "__main__":
    main()
