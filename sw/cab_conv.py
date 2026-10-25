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
REG_REC_WPTR = 0x28
CTRL_BYPASS = 1 << 0            # CTRL[0]: bypass (also toggled by BTN1, LED1 = !bypass)
REC_WPTR = REG_REC_WPTR
CTRL_REC = 1 << 6               # CTRL[6]: recording (also toggled by BTN0)
COEF_BASE = 0x8000
CAP_BASE = 0x10000              # capture ring: word 2k = dry x, 2k+1 = wet y
CAP_FRAMES = 8192
MMIO_RANGE = 0x20000            # 128 KiB AXI window
ID_VALUE = 0x43414256

INPUT_SEL = {"left": 0, "right": 1, "mix": 2}

# Space Echo (rtl/tape_echo.v), see docs/space_echo.md
REG_ECHO_CTRL, REG_ECHO_DELAY, REG_ECHO_FB, REG_ECHO_LEVEL = 0x40, 0x44, 0x48, 0x4C
REG_ECHO_INGAIN, REG_ECHO_BASS, REG_ECHO_TREBLE, REG_ECHO_WOW = 0x50, 0x54, 0x58, 0x5C
REG_ECHO_DAMP, REG_ECHO_REVERB, REG_ECHO_PEAK, REG_ECHO_DRIVE = 0x60, 0x64, 0x68, 0x6C
ECHO_EN, ECHO_CANCEL, ECHO_REV = 1 << 0, 1 << 4, 1 << 5     # ECHO_CTRL bits; [3:1] head mask
ECHO_LOG2 = 15                  # tape buffer 2^15 samples (683 ms)
FS = 48000

# RE-201 MODE SELECTOR: position -> (playback heads, spring reverb)
ECHO_MODES = {1: (1,), 2: (2,), 3: (3,), 4: (2, 3),
              5: (1,), 6: (2,), 7: (3,), 8: (1, 2), 9: (2, 3), 10: (1, 3), 11: (1, 2, 3),
              12: ()}
ECHO_KNOBS = dict(input=7.0, wow=3.0, tape=4.0, mode=3, repeat_rate=5.0, intensity=4.0,
                  echo_volume=6.0, reverb_volume=0.0, bass=5.0, treble=5.0,
                  power=False, cancel=False, output="H", reverb=False)
ECHO_OUTPUT_DB = {"H": 0.0, "M": -10.0, "L": -20.0}        # OUTPUT switch -> headphone volume (codec);
                                                           # H = cab-conv.service --hp-db 0


def echo_regs(k):
    """Panel knobs (0..10, mode 1..12, power/cancel bools; missing keys = ECHO_KNOBS)
    -> {register offset: value}. Shared by the board server, the GUI and the tests."""
    k = dict(ECHO_KNOBS, **k)
    u = lambda name: min(max(float(k[name]), 0.0), 10.0) / 10.0
    q16 = lambda v, hi=131071: int(min(max(round(v * 65536), 0), hi))
    s16 = lambda v: int(min(max(round(v * 65536), -131072), 131071)) & 0x3FFFF
    mode = int(min(max(int(k["mode"]), 1), 12))
    heads = sum(1 << (h - 1) for h in ECHO_MODES[mode])
    ctrl = (ECHO_EN if k["power"] else 0) | (heads << 1) | (ECHO_CANCEL if k["cancel"] else 0)
    ctrl |= ECHO_REV if k["reverb"] else 0             # set from the mode table by the panel/server, BTN3
    d_ms = 200.0 * (50.0 / 200.0) ** u("repeat_rate")       # head 1: 200 ms (0) .. 50 ms (10)
    tape_hz = 12000.0 * (2000.0 / 12000.0) ** u("tape")     # loop lowpass: fresh .. worn tape
    return {
        REG_ECHO_CTRL: ctrl,
        REG_ECHO_DELAY: int(round(d_ms * FS / 1000.0 * 65536)),
        REG_ECHO_FB: q16(1.25 * u("intensity") ** 0.8),
        REG_ECHO_LEVEL: q16(1.5 * u("echo_volume") ** 2),
        REG_ECHO_INGAIN: q16(2.0 * u("input") ** 2),
        REG_ECHO_BASS: s16(2.0 * u("bass") - 1.0),
        REG_ECHO_TREBLE: s16(2.0 * u("treble") - 1.0),
        REG_ECHO_WOW: (int(round(u("wow") * 3 * 256)) << 16) | int(round(u("wow") * 40 * 256)),
        REG_ECHO_DAMP: max(1, min(65535, int(round((1 - math.exp(-2 * math.pi * tape_hz / FS)) * 65536)))),
        REG_ECHO_REVERB: q16(1.5 * u("reverb_volume") ** 2),
    }


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


# Reset values of the echo registers (rtl/tape_echo.v inputs after reset)
ECHO_RESET = {REG_ECHO_CTRL: 0x02, REG_ECHO_DELAY: 4800 << 16, REG_ECHO_FB: 0,
              REG_ECHO_LEVEL: 0, REG_ECHO_INGAIN: 65536, REG_ECHO_BASS: 0,
              REG_ECHO_TREBLE: 0, REG_ECHO_WOW: 0, REG_ECHO_DAMP: 65535,
              REG_ECHO_REVERB: 0}
_ECHO_SLEW, _ECHO_W_INC, _ECHO_F_INC = 13, 53687, 581601
_ECHO_HPK, _ECHO_BLK, _ECHO_TLK, _ECHO_C23 = 683, 2111, 18307, 43691


def echo_model(s, regs=None, schedule=None, want_drive=False, slew=_ECHO_SLEW):
    """Bit-exact model of rtl/tape_echo.v (spec: docs/space_echo.md).

    s: int samples (24-bit signed), the signal that goes to the DAC before the echo.
    regs: {REG_ECHO_* offset: value} overriding the reset values.
    schedule: [(n, {offset: value}), ...] register changes applied before sample n.
    slew: speed glide shift (13 in the real design; the testbench uses a smaller one so
    the glide settles within a short simulation).
    Returns (out, sat), plus the record-path |r| (ECHO_DRIVE) when want_drive.
    """
    n_tape = 1 << ECHO_LOG2
    m_tape = n_tape - 1
    tmin, tmax = 16 << 16, ((n_tape - 4) // 3) << 16
    sat24 = lambda v: YMAX if v > YMAX else YMIN if v < YMIN else v
    rg = dict(ECHO_RESET)
    rg.update(regs or {})
    sched = sorted(schedule or [], key=lambda e: e[0])
    si = 0
    mem = [0] * n_tape
    wp = lp = hz = bl = tl = pw = pf = 0
    dcur = ECHO_RESET[REG_ECHO_DELAY]
    n = len(s)
    out = np.zeros(n, dtype=np.int64)
    sat = np.zeros(n, dtype=bool)
    rabs = np.zeros(n, dtype=np.int64)
    for k in range(n):
        while si < len(sched) and sched[si][0] <= k:
            rg.update(sched[si][1])
            si += 1
        ctrl = rg[REG_ECHO_CTRL]
        en, cancel, mask = ctrl & 1, (ctrl >> 4) & 1, (ctrl >> 1) & 7
        delay, fb_g, lvl = rg[REG_ECHO_DELAY] & 0xFFFFFFFF, rg[REG_ECHO_FB] & 0x1FFFF, rg[REG_ECHO_LEVEL] & 0x1FFFF
        ing, damp = rg[REG_ECHO_INGAIN] & 0x1FFFF, rg[REG_ECHO_DAMP] & 0xFFFF
        bass, treb = rg[REG_ECHO_BASS] & 0x3FFFF, rg[REG_ECHO_TREBLE] & 0x3FFFF
        bass, treb = bass - (1 << 18) if bass >> 17 else bass, treb - (1 << 18) if treb >> 17 else treb
        wow, flut = rg[REG_ECHO_WOW] & 0xFFFF, (rg[REG_ECHO_WOW] >> 16) & 0xFFFF
        x = int(s[k])
        dcur += (delay - dcur) >> slew
        tri = lambda p: ((p >> 15) if (p >> 15) < 65536 else 131071 - (p >> 15)) - 32768
        mod = (tri(pw) * wow + tri(pf) * flut) >> 7
        pw = (pw + _ECHO_W_INC) & 0xFFFFFFFF
        pf = (pf + _ECHO_F_INC) & 0xFFFFFFFF
        t_d = min(max(dcur + mod, tmin), tmax)
        e = 0
        for h in (1, 2, 3):
            if not (mask >> (h - 1)) & 1:
                continue
            d = h * t_d
            di, fr = d >> 16, d & 0xFFFF
            a, b = mem[(wp - di) & m_tape], mem[(wp - di - 1) & m_tape]
            e += a + (((b - a) * fr) >> 16)
        e = sat24(e)
        lp += ((e - lp) * damp) >> 16
        hz += ((lp - hz) * _ECHO_HPK) >> 16
        f = sat24(lp - hz)
        fb = (f * fb_g) >> 16
        inp = 0 if (cancel or not en) else (x * ing) >> 16
        r = min(max(inp + fb, -(2**24 - 1)), 2**24 - 1)
        rabs[k] = abs(r)
        t = min(max((r * _ECHO_C23) >> 16, -(2**23 - 1)), 2**23 - 1)
        t2 = (t * (t >> 6)) >> 17
        t3 = (t * (t2 >> 6)) >> 17
        y = sat24((3 * t - t3) >> 1)
        mem[wp] = y if en else 0
        wp = (wp + 1) & m_tape
        bl += ((e - bl) * _ECHO_BLK) >> 16
        tl += ((e - tl) * _ECHO_TLK) >> 16
        eo = sat24(e + ((bl * bass) >> 16) + ((sat24(e - tl) * treb) >> 16))
        o = x + ((eo * lvl) >> 16) if en else x
        sat[k] = o > YMAX or o < YMIN
        out[k] = sat24(o)
    return (out, sat, rabs) if want_drive else (out, sat)


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
# Capture ring (dry/wet recording); needs only an MMIO-like object
# ----------------------------------------------------------------------
class CaptureOverflow(RuntimeError):
    """The reader fell more than CAP_FRAMES behind the writer.
    .wptr is the writer position, .lost the number of frames that were
    overwritten (already counted from the requested rptr)."""

    def __init__(self, wptr, lost):
        RuntimeError.__init__(self, "capture overflow: %d frames lost" % lost)
        self.wptr, self.lost = wptr, lost


class CaptureMixin:
    """Recording control + capture ring readout. Requires self.mmio."""

    def _rec_ctrl(self):
        return self.mmio.read(REG_CTRL)

    def rec_start(self):
        self.mmio.write(REG_CTRL, self._rec_ctrl() | CTRL_REC)

    def rec_stop(self):
        self.mmio.write(REG_CTRL, self._rec_ctrl() & ~CTRL_REC)

    @property
    def recording(self):
        return bool(self._rec_ctrl() & CTRL_REC)

    def rec_wptr(self):
        """Frames written since recording last started (32-bit)."""
        return self.mmio.read(REG_REC_WPTR)

    def _cap_copy(self, k0, n):
        """Copy n frames starting at ring slot k0 (no wrap) -> int32 (n, 2)."""
        w0 = CAP_BASE // 4 + 2 * k0
        arr = getattr(self.mmio, "array", None)
        if arr is not None:
            raw = np.array(arr[w0:w0 + 2 * n], dtype=np.uint32)   # copy
        else:
            raw = np.array([self.mmio.read(CAP_BASE + 4 * (2 * k0 + i))
                            for i in range(2 * n)], dtype=np.uint32)
        return raw.view(np.int32).reshape(n, 2)

    def read_capture(self, rptr):
        """Read frames rptr..wptr from the capture ring.

        Returns (new_rptr, int32 array (n, 2)); column 0 = dry, 1 = wet,
        sign-extended 24-bit values. rptr counts frames since REC started.
        Raises CaptureOverflow if more than CAP_FRAMES frames accumulated
        (or were overwritten while reading); resync with e.wptr.
        """
        wptr = self.rec_wptr()
        n = (wptr - rptr) & 0xFFFFFFFF
        if n > CAP_FRAMES:
            raise CaptureOverflow(wptr, n - CAP_FRAMES)
        if n == 0:
            return rptr, np.zeros((0, 2), dtype=np.int32)
        k0 = rptr % CAP_FRAMES
        n1 = min(n, CAP_FRAMES - k0)
        parts = [self._cap_copy(k0, n1)]
        if n > n1:
            parts.append(self._cap_copy(0, n - n1))
        data = parts[0] if len(parts) == 1 else np.concatenate(parts)
        # frames may have been overwritten while we were copying
        w2 = self.rec_wptr()
        over = ((w2 - rptr) & 0xFFFFFFFF) - CAP_FRAMES
        if over > 0:
            raise CaptureOverflow(w2, over)
        return (rptr + n) & 0xFFFFFFFF, data


# ----------------------------------------------------------------------
# Overlay driver
# ----------------------------------------------------------------------
class CabConv(CaptureMixin):
    def __init__(self, bitfile="cab_conv.bit", fs=48000, aux_gain_db=0,
                 hp_volume_db=0, i2c_bus=None, download=True):
        from pynq import MMIO, Overlay
        self.ol = Overlay(bitfile, download=download)
        name = next(k for k in self.ol.ip_dict if "cab_conv" in k)
        ip = self.ol.ip_dict[name]
        self.mmio = MMIO(ip["phys_addr"], max(MMIO_RANGE, ip.get("addr_range", 0)))
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
