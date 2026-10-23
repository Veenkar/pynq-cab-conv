"""
cab_conv.py - driver for the PYNQ-Z2 guitar cabinet IR convolver.

Usage on the board (Jupyter):

    from cab_conv import CabConv
    cab = CabConv("cab_conv.bit")          # loads overlay, configures codec
    cab.load_ir("Mesa_OS_4x12_V30_SM57.wav", gain_db=-3)
    cab.status()

The IR processing / fixed-point helpers (prepare_ir, quantize, fir_model)
have no PYNQ dependency and are also used by the RTL testbench.
"""

import glob
import math
import os
import time
from fractions import Fraction

import numpy as np

# ----------------------------------------------------------------------
# Hardware constants (must match rtl/cab_conv_top.v)
# ----------------------------------------------------------------------
DW = 24                     # sample width
CW = 18                     # coefficient width
AW = 48                     # per-lane accumulator width
CMAX = (1 << (CW - 1)) - 1
YMAX = (1 << (DW - 1)) - 1
YMIN = -(1 << (DW - 1))

REG_ID, REG_PARAMS, REG_CTRL, REG_STATUS = 0x00, 0x04, 0x08, 0x0C
REG_SHIFT0, REG_SHIFT1, REG_CLEAR, REG_SAMPLES = 0x10, 0x14, 0x18, 0x1C
REG_PEAK_IN, REG_PEAK_OUT = 0x20, 0x24
COEF_BASE = 0x8000
ID_VALUE = 0x43414256

INPUT_SEL = {"left": 0, "right": 1, "mix": 2}


# ----------------------------------------------------------------------
# IR preparation and fixed point (pure numpy / scipy)
# ----------------------------------------------------------------------
def read_wav(path):
    """Return (float64 mono samples, sample rate). Handles 16/24/32-bit PCM
    and float WAVs; multichannel files are averaged."""
    from scipy.io import wavfile
    fs, data = wavfile.read(path)
    if data.dtype.kind in "iu":
        if data.dtype == np.uint8:
            data = (data.astype(np.float64) - 128.0) / 128.0
        else:
            data = data.astype(np.float64) / float(np.iinfo(data.dtype).max)
    else:
        data = data.astype(np.float64)
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data, int(fs)


def resample(h, fs_in, fs_out):
    if fs_in == fs_out:
        return np.asarray(h, dtype=np.float64)
    from scipy.signal import resample_poly
    r = Fraction(int(round(fs_out)), int(fs_in)).limit_denominator(1000)
    return resample_poly(h, r.numerator, r.denominator)


def prepare_ir(h, fs_ir, fs=48000, ntaps=8192, max_ms=None, fade_ms=5.0,
               gain_db=0.0, normalize="response"):
    """Resample, trim, fade out and normalise an impulse response.

    normalize:
      "response" - peak of |H(f)| = 0 dB (no boost at any frequency;
                   sensible default for cab IRs)
      "energy"   - unit L2 norm
      "peak"     - max |h| = 1
      None       - keep the file's own level
    Then gain_db is applied.
    """
    h = resample(np.asarray(h, dtype=np.float64), fs_ir, fs)

    # drop leading silence (pre-delay) but keep a couple of samples
    thr = np.max(np.abs(h)) * 1e-3
    first = int(np.argmax(np.abs(h) > thr))
    h = h[max(0, first - 2):]

    n = ntaps
    if max_ms is not None:
        n = min(n, int(round(max_ms * 1e-3 * fs)))
    h = h[:n]

    nf = min(len(h), int(round(fade_ms * 1e-3 * fs)))
    if nf > 1:
        h = h.copy()
        h[-nf:] *= 0.5 * (1 + np.cos(np.linspace(0, np.pi, nf)))

    if normalize == "response":
        H = np.fft.rfft(h, n=max(8192, 1 << int(math.ceil(math.log2(len(h) * 4)))))
        h = h / np.max(np.abs(H))
    elif normalize == "energy":
        h = h / np.sqrt(np.sum(h * h))
    elif normalize == "peak":
        h = h / np.max(np.abs(h))
    elif normalize is not None:
        raise ValueError("normalize must be 'response', 'energy', 'peak' or None")

    return h * 10 ** (gain_db / 20.0)


def quantize(h, ntaps, ltaps):
    """Block-floating-point quantisation for the hardware.

    Returns (hq int64[ntaps], shift) so that h ~= hq / 2**shift.
    The shift is chosen as large as possible (best precision) while every
    coefficient fits in CW bits and no lane accumulator can overflow 48 bits
    even for a full-scale worst-case input.
    """
    h = np.asarray(h, dtype=np.float64)
    if len(h) > ntaps:
        raise ValueError("IR has %d taps, hardware supports %d" % (len(h), ntaps))
    hp = np.zeros(ntaps)
    hp[:len(h)] = h
    peak = np.max(np.abs(hp))
    if peak == 0:
        return np.zeros(ntaps, dtype=np.int64), 0

    shift = int(math.floor(math.log2(CMAX / peak)))
    shift = max(0, min(63, shift))
    while True:
        hq = np.clip(np.round(hp * 2.0 ** shift), -CMAX, CMAX).astype(np.int64)
        lane_l1 = np.abs(hq).reshape(-1, ltaps).sum(axis=1).max()
        if lane_l1 * (1 << (DW - 1)) < (1 << (AW - 1)) or shift == 0:
            break
        shift -= 1
    return hq, shift


def fir_model(x, hq, shift, ltaps):
    """Bit-exact model of rtl/fir_engine.v (zero initial state).

    x: int samples (24-bit signed), hq: int coefficients (len = ntaps).
    Returns (y, sat). Per-lane sums fit in int64 (24+18+11 bits), so the
    integer convolution is exact; each lane then wraps to 48 bits like the
    DSP48 accumulator.
    """
    x = np.asarray(x, dtype=np.int64)
    hq = np.asarray(hq, dtype=np.int64)
    n = len(x)
    total = np.zeros(n, dtype=object)
    for k in range(len(hq) // ltaps):
        hk = hq[k * ltaps:(k + 1) * ltaps]
        lane = np.zeros(n, dtype=np.int64)
        off = k * ltaps
        if off < n and np.any(hk):
            lane[off:] = np.convolve(x, hk)[:n - off]
        total = total + _wrap_arr(lane, AW).astype(object)
    v = np.array([int(t) >> shift for t in total], dtype=object)
    sat = (v > YMAX) | (v < YMIN)
    y = np.clip(v, YMIN, YMAX).astype(np.int64)
    return y, sat.astype(bool)


def _wrap_arr(a, bits):
    m = np.int64(1) << np.int64(bits)
    h = m >> np.int64(1)
    return ((a + h) % m) - h


# ----------------------------------------------------------------------
# ADAU1761 over Linux i2c-dev
# ----------------------------------------------------------------------
I2C_SLAVE = 0x0703


class ADAU1761:
    """Minimal ADAU1761 control for the PYNQ-Z2: codec as I2S slave,
    MCLK = 256 * fs from the FPGA (no codec PLL), LINE IN via the AUX
    inputs of the record mixers, output on the headphone jack."""

    def __init__(self, bus=None, addr=0x3B):
        import fcntl                    # Linux only; keep module importable elsewhere
        self.addr = addr
        buses = [bus] if bus is not None else self._candidate_buses()
        last = None
        for b in buses:
            try:
                fd = os.open("/dev/i2c-%d" % b, os.O_RDWR)
                fcntl.ioctl(fd, I2C_SLAVE, addr)
                self.fd, self.bus = fd, b
                self.read(0x4000)               # probe
                return
            except OSError as e:
                last = e
                try:
                    os.close(fd)
                except Exception:
                    pass
        raise RuntimeError("ADAU1761 not found on %s (%s)" % (buses, last))

    @staticmethod
    def _candidate_buses():
        found = sorted(int(p.rsplit("-", 1)[1]) for p in glob.glob("/dev/i2c-*"))
        # PYNQ uses /dev/i2c-1 for the codec; try it first
        return sorted(found, key=lambda b: (b != 1, b))

    def write(self, reg, val):
        os.write(self.fd, bytes([reg >> 8, reg & 0xFF, val & 0xFF]))

    def read(self, reg):
        os.write(self.fd, bytes([reg >> 8, reg & 0xFF]))
        return os.read(self.fd, 1)[0]

    def configure(self, aux_gain_db=0, hp_volume_db=0):
        w = self.write
        w(0x4000, 0x00)     # R0: core clock off while configuring
        w(0x4000, 0x01)     # R0: MCLK direct, 256 x fs, core clock on
        time.sleep(0.01)
        w(0x4015, 0x00)     # R15: serial port slave
        w(0x4016, 0x00)     # R16: 64 BCLK/frame, I2S, defaults
        w(0x4017, 0x00)     # R17: converter rate = fs
        w(0x40F8, 0x00)     # R64: serial port rate = fs
        w(0x400E, 0x00)     # R8/R9: differential PGA off (mic path unused)
        w(0x400F, 0x00)
        w(0x4019, 0x33)     # R19: both ADCs on, high-pass filter on
        self.set_aux_gain(aux_gain_db)
        w(0x401D, 0x00)     # R23/R25: aux bypass into playback mixers off
        w(0x401F, 0x00)
        w(0x401C, 0x21)     # R22: mixer 3 on, left DAC
        w(0x401E, 0x41)     # R24: mixer 4 on, right DAC
        w(0x4029, 0x03)     # R35: playback L/R on
        w(0x402A, 0x03)     # R36: both DACs on
        w(0x40F2, 0x01)     # R58: serial in -> DACs
        w(0x40F3, 0x01)     # R59: ADCs -> serial out
        w(0x40F5, 0x01)     # R61/R62: DSP enable/run (as PYNQ does)
        w(0x40F6, 0x01)
        w(0x40F9, 0x7F)     # R65/R66: clock generators on
        w(0x40FA, 0x03)
        self.set_hp_volume(hp_volume_db)

    def set_aux_gain(self, db):
        """LINE IN gain: -12 .. +6 dB in 3 dB steps (0 dB ~ 1 Vrms full scale)."""
        code = int(round((db + 15) / 3.0))
        if not 1 <= code <= 7:
            raise ValueError("aux gain must be -12..+6 dB")
        self.write(0x400A, 0x01)        # R4: mixer 1 on, LINN/LINP muted
        self.write(0x400B, code)        # R5: LAUX gain
        self.write(0x400C, 0x01)        # R6: mixer 2 on
        self.write(0x400D, code)        # R7: RAUX gain

    def set_hp_volume(self, db):
        """Headphone output: -57 .. +6 dB in 1 dB steps, None = mute."""
        if db is None:
            v = 0xE5                    # enabled, muted (as PYNQ does)
        else:
            code = int(round(57 + db))
            if not 0 <= code <= 63:
                raise ValueError("headphone volume must be -57..+6 dB")
            v = (code << 2) | 0x03
        self.write(0x4023, v)
        self.write(0x4024, v)


# ----------------------------------------------------------------------
# Overlay driver
# ----------------------------------------------------------------------
class CabConv:
    def __init__(self, bitfile="cab_conv.bit", fs=48000, aux_gain_db=0,
                 hp_volume_db=0, i2c_bus=None, download=True):
        from pynq import MMIO, Overlay
        self.ol = Overlay(bitfile, download=download)
        name = next(k for k in self.ol.ip_dict if "cab_conv" in k)
        ip = self.ol.ip_dict[name]
        self.mmio = MMIO(ip["phys_addr"], ip.get("addr_range", 0x10000))
        if self.mmio.read(REG_ID) != ID_VALUE:
            raise RuntimeError("cab_conv IP not found (ID=0x%08x)" % self.mmio.read(REG_ID))
        p = self.mmio.read(REG_PARAMS)
        self.ntaps = p & 0xFFFF
        self.ltaps = 1 << ((p >> 16) & 0xFF)
        self.nlanes = p >> 24
        self.fs = fs
        self.codec = ADAU1761(bus=i2c_bus)
        self.codec.configure(aux_gain_db, hp_volume_db)
        self.clear()

    # ---- control register helpers ----
    def _ctrl(self):
        return self.mmio.read(REG_CTRL)

    def _set_ctrl_bits(self, mask, value):
        self.mmio.write(REG_CTRL, (self._ctrl() & ~mask) | (value & mask))

    @property
    def bypass(self):
        return bool(self._ctrl() & 1)

    @bypass.setter
    def bypass(self, on):
        self._set_ctrl_bits(0x01, 0x01 if on else 0)

    @property
    def mute(self):
        return bool(self._ctrl() & 2)

    @mute.setter
    def mute(self, on):
        self._set_ctrl_bits(0x02, 0x02 if on else 0)

    def set_input(self, which="left"):
        self._set_ctrl_bits(0x0C, INPUT_SEL[which] << 2)

    def set_left_only(self, on):
        """Wet signal only on the left output, dry on the right (A/B check)."""
        self._set_ctrl_bits(0x20, 0x20 if on else 0)

    @property
    def active_bank(self):
        return self.mmio.read(REG_STATUS) & 1

    # ---- IR loading ----
    def load_ir(self, ir, fs_ir=None, gain_db=0.0, max_ms=None,
                normalize="response", fade_ms=5.0, timeout=1.0):
        """Load an IR (WAV path or array) into the inactive bank and switch
        to it glitch-free at the next sample boundary."""
        if isinstance(ir, str):
            h, fs_ir = read_wav(ir)
        else:
            h = np.asarray(ir, dtype=np.float64)
            fs_ir = fs_ir or self.fs
        h = prepare_ir(h, fs_ir, self.fs, self.ntaps, max_ms, fade_ms,
                       gain_db, normalize)
        hq, shift = quantize(h, self.ntaps, self.ltaps)

        cur = self.active_bank
        if ((self._ctrl() >> 4) & 1) != cur:
            raise RuntimeError("previous bank switch still pending")
        new = cur ^ 1

        words = (hq & ((1 << CW) - 1)).astype(np.uint32)
        nz = int(np.max(np.nonzero(hq)[0])) + 1 if np.any(hq) else 0
        # writes go to the inactive bank; clear the tail only if needed
        self._write_coefs(words)
        self.mmio.write(REG_SHIFT1 if new else REG_SHIFT0, shift)
        self._set_ctrl_bits(0x10, new << 4)

        t0 = time.time()
        while self.active_bank != new:
            if time.time() - t0 > timeout:
                raise RuntimeError("bank switch timed out - is the codec clocked?")
            time.sleep(0.001)
        self.last_ir = dict(taps=nz, shift=shift, ms=1000.0 * nz / self.fs)
        return self.last_ir

    def _write_coefs(self, words):
        arr = getattr(self.mmio, "array", None)
        if arr is not None:
            arr[COEF_BASE // 4: COEF_BASE // 4 + len(words)] = words
        else:
            for i, w in enumerate(words):
                self.mmio.write(COEF_BASE + 4 * i, int(w))

    # ---- metering ----
    def clear(self):
        self.mmio.write(REG_CLEAR, 1)

    def status(self):
        st = self.mmio.read(REG_STATUS)
        pin = self.mmio.read(REG_PEAK_IN)
        pout = self.mmio.read(REG_PEAK_OUT)

        def dbfs(v):
            return 20 * math.log10(v / float(YMAX)) if v else float("-inf")

        return dict(active_bank=st & 1, input_clip=bool(st & 2),
                    output_clip=bool(st & 4), overrun=bool(st & 8),
                    peak_in_dbfs=round(dbfs(pin), 1),
                    peak_out_dbfs=round(dbfs(pout), 1),
                    samples=self.mmio.read(REG_SAMPLES))

    def measure_fs(self, seconds=1.0):
        s0, t0 = self.mmio.read(REG_SAMPLES), time.time()
        time.sleep(seconds)
        s1, t1 = self.mmio.read(REG_SAMPLES), time.time()
        return ((s1 - s0) & 0xFFFFFFFF) / (t1 - t0)
