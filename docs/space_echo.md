# Space Echo (RE-201 style tape echo)

A tape-echo effect after the cab FIR, modelled on the Roland RE-201: one tape loop
(circular buffer) with a record head and three playback heads, a feedback loop with tape
darkening and tape saturation, wow and flutter, bass/treble on the echo, and the 12-position
MODE SELECTOR. The spring reverb (modes 5..12, REVERB VOLUME) is live now: `ECHO_CTRL[5]` REV
and `ECHO_REVERB` drive `rtl/spring.v`, see [spring.md](spring.md).

```
                  ┌──────────────────────── feedback (INTENSITY) ◄── HPF ◄── tape LPF (DAMP) ◄──┐
s ─► × INGAIN ─► (+) ─► soft clip ─► [tape: 2^15 samples] ─► heads 1/2/3 (mask) ─► e ─────────┘
│                                                                              └─► bass/treble ─► × LEVEL ─┐
└──────────────────────────────────────────────────────────────────────────────────────────────► (+) ─► out
```

`s` = the signal that went to the DAC before this effect: cab output `y`, or the dry `x_dly`
in bypass. Mute still forces 0 at the DAC. Output: `tx_l = out`, `tx_r = left_only ? x_dly : out`.

## Registers (AXI, byte offsets)

| Off  | Name         | Access | Bits |
|------|--------------|--------|------|
| 0x40 | ECHO_CTRL    | RW | [0] EN (POWER; also toggled by BTN2, LD2 shows it), [3:1] head mask (bit1 = head 1 .. bit3 = head 3), [4] CANCEL (no new input onto the tape, repeats trail off), [5] REV (spring reverb input on; also toggled by BTN3, LD3 shows it) |
| 0x44 | ECHO_DELAY   | RW | [31:0] head-1 delay target, unsigned Q16.16 samples |
| 0x48 | ECHO_FB      | RW | [16:0] feedback gain, unsigned Q16 (0 .. 2.0) |
| 0x4C | ECHO_LEVEL   | RW | [16:0] echo volume, unsigned Q16 |
| 0x50 | ECHO_INGAIN  | RW | [16:0] record level, unsigned Q16 |
| 0x54 | ECHO_BASS    | RW | [17:0] signed Q16 (-2 .. +2), 0 = flat |
| 0x58 | ECHO_TREBLE  | RW | [17:0] signed Q16, 0 = flat |
| 0x5C | ECHO_WOW     | RW | [15:0] wow depth, [31:16] flutter depth, unsigned Q8 samples |
| 0x60 | ECHO_DAMP    | RW | [15:0] tape lowpass coefficient, Q16 |
| 0x64 | ECHO_REVERB  | RW | [16:0] spring reverb volume, Q16 (live, see spring.md) |
| 0x68 | ECHO_PEAK    | RO, read clears | [23:0] max \|tx_l\| (final DAC word, 0 when muted) since last read, [24] output mix saturated |
| 0x6C | ECHO_DRIVE   | RO, read clears | [24:0] max \|r\| (record signal before the soft clip) since last read |
| 0x0C | STATUS[4]    | RO sticky | echo overrun (engine not done before the next frame; should never be 1); cleared by CLEAR |

Reset: CTRL = 0x02 (EN = 0, head 1), DELAY = 4800·2^16 (100 ms), FB = 0, LEVEL = 0,
INGAIN = 65536, BASS = TREBLE = 0, WOW = 0, DAMP = 65535, REVERB = 0.
With EN = 0 the output is bit-identical to the design without the echo.

Panel knob → register mapping: `sw/cab_conv.py:echo_regs()` (single source of truth).

## Arithmetic (bit-exact, `sw/cab_conv.py:echo_model`)

All values are Python ints; `>>` is an arithmetic (floor) shift; `sat24(v)` clamps to
[-2^23, 2^23-1]; `clamp(v, lo, hi)`. `N = 2^15`, `M = N-1`. State (reset to 0 unless noted):
`mem[N]`, `wp`, `dcur` (= reset DELAY), `lp`, `hz`, `bl`, `tl`, `pw`, `pf` (32-bit phases).
Constants: `SLEW = 13`, `W_INC = 53687` (0.6 Hz), `F_INC = 581601` (6.5 Hz),
`TMIN = 16 << 16`, `TMAX = ((N-4)//3) << 16`, `HPK = 683` (~80 Hz), `BLK = 2111` (~250 Hz),
`TLK = 18307` (~2.5 kHz), `C23 = 43691` (2/3 in Q16).

Per sample, with input `s` and the register values sampled at the start of this sample:

```
dcur += (DELAY - dcur) >> SLEW                      # tape-speed glide (pitch bend)
tri(p) = (u if u < 65536 else 131071 - u) - 32768   where u = p >> 15   (p unsigned 32-bit)
mod  = (tri(pw) * WOW_DEPTH + tri(pf) * FLUT_DEPTH) >> 7      # Q16 samples
pw = (pw + W_INC) mod 2^32 ;  pf = (pf + F_INC) mod 2^32
T    = clamp(dcur + mod, TMIN, TMAX)
for h in 1, 2, 3:                                   # playback head h sits at h*T
    d = h*T ; di = d >> 16 ; fr = d & 0xFFFF
    a = mem[(wp - di) & M] ; b = mem[(wp - di - 1) & M]
    v[h] = a + (((b - a) * fr) >> 16)               # linear interpolation
e   = sat24(sum(v[h] for heads in the mask))        # 0 if the mask is empty
lp += ((e - lp) * DAMP) >> 16                       # tape darkens each repeat
hz += ((lp - hz) * HPK) >> 16
f   = sat24(lp - hz)                                # ... and loses lows
fb  = (f * FB) >> 16
inp = 0 if (CANCEL or not EN) else (s * INGAIN) >> 16
r   = clamp(inp + fb, -(2^24-1), 2^24-1)            # ECHO_DRIVE tracks |r|
t   = clamp((r * C23) >> 16, -(2^23-1), 2^23-1)
t2  = (t * (t >> 6)) >> 17
t3  = (t * (t2 >> 6)) >> 17
y   = sat24((3*t - t3) >> 1)                        # tape saturation: 1.5t - 0.5t^3
mem[wp] = y if EN else 0 ;  wp = (wp + 1) & M       # EN = 0 erases the tape
bl += ((e - bl) * BLK) >> 16
tl += ((e - tl) * TLK) >> 16
eo  = sat24(e + ((bl * BASS) >> 16) + ((sat24(e - tl) * TREBLE) >> 16))
out = sat24(s + ((eo * LEVEL) >> 16)) if EN else s  # ECHO_PEAK[24] when this saturates
```

Every multiply fits a DSP48 (25 × 18 signed). The engine starts when the cab result is ready
(`y_done`) and must write `tx_l/tx_r` before the next `frame_tick`; at HALF = 20 that leaves
~480 cycles after the FIR, so keep it well under ~300 cycles.

## Panel (PC GUI) and board server

`sw/echo_panel.py` (PC, tkinter) talks to `sw/board/echo_server.py` (board, systemd
`echo.service`) over TCP port 5201, one JSON object per line:

| Request | Reply |
|---|---|
| `{"cmd": "get"}` | `{"ok": true, "knobs": {...}, "cab_bypass": bool}` |
| `{"cmd": "set", "knobs": {partial}}` | `{"ok": true}` (registers written, state saved) |
| `{"cmd": "set", "cab_bypass": bool}` | `{"ok": true}` (CTRL[0] read-modify-write) |
| `{"cmd": "meters"}` | `{"ok": true, "out": peak, "out_sat": bool, "drive": peak}` (raw ints, reads clear) |
| anything else | `{"ok": false, "error": "..."}` |

Knob keys and defaults: `sw/cab_conv.py:ECHO_KNOBS`. The knob `output` (`"H"`/`"M"`/`"L"`, the OUTPUT
slide switch) is not an echo register: the server sets the headphone volume over the codec's I2C
(`ECHO_OUTPUT_DB`: 0 / -10 / -20 dB; H = the cab-conv.service default). The server keeps the knob state in
`/home/xilinx/cab_conv/echo_state.json` and re-applies it at start (after the overlay is loaded),
so the echo comes back after a reboot. The server never writes CLEAR (status.py / meter.py do).
