# Spring reverb (RE-201 style spring tank)

Two parallel dispersive spring models, after Välimäki, Parker & Abel, "Parametric Spring
Reverberation Effect" (JAES 2010) and Abel et al. (DAFx 2006): each spring is a feedback loop
containing a delay line, a cascade of stretched first-order allpass sections (the dispersion that
makes the "boing" / descending "drip"), a loop lowpass and a DC blocker, with a slowly modulated
delay. Only the low-frequency chirp branch is implemented (the high-frequency branch is optional
in the paper and not needed for the RE-201 sound).
Bit-exact model: `sw/cab_conv.py:spring_model` (+ `_spring_one`, constants `SPRING_*`).

```
                 ┌────────────────────────────────────────────────────────────┐
                 │   fb = G·(HPF(LPF(u)))                                     │
 sin ──►(+)──► [delay line 2^10, read at D + mod (lin. interp.)] ──► x0        │
        ▲                                                            │         │
        └──── fb ◄──────────────────────────────────────────────── [M allpass sections (a + z^-K)/(1 + a z^-K)] ─► u ─┘
                                                                        u ─► W_spring (output tap)

 s ─► (rev_on ? s[n-1] : 0) ─► × INK ─► sin  (both springs)
 W = (u_1 + u_2) >> 1          wet = (W · REVERB) >> 16          out = sat24(echo_out + wet)
```

## Behaviour

* `ECHO_CTRL[5]` REV gates only the spring **input** (sin = 0 when REV = 0): the tail rings out
  after switching off (RE-201 like). The wet path (`W * REVERB`) always runs, so the reverb works
  with ECHO_CTRL.EN = 0 and the REVERB VOLUME knob always acts. Knob mapping: `echo_regs()`
  (`REVERB = round(1.5 * (k/10)^2 * 65536)`, unity at knob ~8.2).
* The spring step for frame n consumes `s[n-1]` (the echo-stage input of the previous sample,
  `s` = cab output, or dry in bypass) and finishes during frame n in parallel with the FIR; its
  result `W[n]` is added in frame n+1: `wet[n] = (W[n-1] * REVERB[n]) >> 16`, `W[-1] = 0`, `s[-1] = 0`.
  Total spring latency 2 samples (irrelevant, there is a pre-delay of ~13 ms anyway).
* REVERB/REV are sampled when the step starts (REV) and when the wet product is formed (REVERB).
* Reset: all state 0. Mute still forces 0 at the DAC (the engine keeps running).

## Constants

| Name | Spring 1 | Spring 2 | Meaning |
|---|---|---|---|
| `M` | 72 | 64 | allpass sections |
| `K` | 4 | 4 | stretch (delay of each section, in samples) |
| `A` | -81265 (-0.620, Q17) | -75367 (-0.575, Q17) | allpass coefficient, 18-bit signed |
| `D` | 600 | 700 | loop delay (samples), + modulation |
| `G` | 61604 (0.940, Q16) | 61604 | loop gain |
| `C` | 39322 (0.600, Q16) | 39322 | loop lowpass (~5.8 kHz one-pole) |
| `MODD` | 96 (3 samples, Q5) | 96 | modulation depth, peak |
| `INC` | 80530 (0.9 Hz) | 116319 (1.3 Hz) | LFO phase increment, 32-bit phase |
| `SPRING_INK` | 32768 (0.5, Q16) | | input gain (headroom; wet ~ 0.47 x dry RMS at REVERB = 1.0) |
| `SPRING_HPK` | 683 (~80 Hz) | | loop DC blocker |

Dispersion: a < 0 gives group delay `M·K(1-a²)/(1+2a·cos(Kω)+a²)`: ~25 ms at 100 Hz, 12 ms
at 1 kHz, 5 ms at 2 kHz, 1.6 ms at 4.5 kHz (spring 1): highs arrive first, lows trail
(descending "pew"). The loop time is therefore ~38 ms at LF, ~24 ms at 1 kHz. The `K = 4` stretch
repeats the dispersion pattern every fs/K = 12 kHz; the loop lowpass removes the replicas.

## Arithmetic (per spring, per sample n; Python ints, `>>` is floor)

`sat24` clamps to [-2^23, 2^23-1]. State: `dl[1024]` (24-bit), write pointer = n mod 1024,
`Y[m][n mod K]` for m = 0..M-1 (24-bit; the allpass input state `x[n-K]` of section m is the
output state of section m-1, see below), `X0[n mod K]`, `lp`, `hz`, `ph` (32-bit). All reset to 0.

```
tri(p) = (u if u < 65536 else 131071 - u) - 32768       where u = p >> 15
d   = (D << 16) + ((tri(ph) * MODD) >> 4) ;  ph = (ph + INC) mod 2^32       # Q16 samples
di  = d >> 16 ; fr = d & 0xFFFF
a   = dl[(n - di) & 1023] ; b = dl[(n - di - 1) & 1023]
x   = a + (((b - a) * fr) >> 16)                         # x0, linear interpolation (reads before the write of dl[n])
for m in 0..M-1:                                         # allpass sections, in series
    xd = x_in(m)[n-K] ; yd = Y[m][n mod K]               # = x_in(m) delayed K samples, y delayed K samples
    y  = sat24(xd + ((A * (x - yd) + 65536) >> 17))      # A (18 b) * (x-yd) (25 b): one DSP48 multiply
    Y[m][n mod K] = y ; x = y
u   = x                                                   # chain output
lp += ((u - lp) * C) >> 16
hz += ((lp - hz) * HPK) >> 16
f   = sat24(lp - hz)
dl[n & 1023] = sat24(sin + ((f * G) >> 16))
W   = u
```

(y(m) is the section output, `x_in(m)` the previous section's output; `xd` of section m is the old
`Y[m-1][n mod K]` value read before it was overwritten, section 0 uses `X0`, a K-deep ring of x0.
Memory per spring: (M + 1)·K words; equivalent to the two-ring formulation `y = xd + a(x-yd)`.)

Top level, with `s_prev = s[n-1]`:
```
sin  = (rev_on ? (s_prev * SPRING_INK) >> 16 : 0)            # same for both springs
W    = (u_1 + u_2) >> 1
wet  = (W_delayed_by_one_frame * REVERB) >> 16               # REVERB = ECHO_REVERB[16:0] unsigned Q16
out  = sat24(echo_out + wet)                                  # echo_out = echo_model output (after its own out = s + ...)
```
`out` goes to `tx_l` (and `tx_r` unless left_only). ECHO_PEAK[24] (output saturated) should include
this final sum. All multiplies fit the shared 25 x 18 signed DSP48: allpass `A*(x-yd)`; the other
products are `<=25 bit x <=18 bit` (the interpolation `(b-a)` is 25 bit x 16-bit fraction; `C`, `HPK`,
`G`, `INK`, `REVERB` are <= 17-bit unsigned).

## Cost

* Multiplies per sample: spring 1: 72 + 5 (interp, mod, lp, hz, fb) = 77; spring 2: 64 + 5 = 69; sin gain 1 (shared);
  final REVERB 1: ~148. At ~3 cycles each: ~450 cycles (of ~2400); one engine instance can be
  time-shared by both springs. There is headroom to ~4x (more sections or a HF branch).
* Memory: delay lines 2 x 1024 x 24 b = 48 Kbit; allpass state (73 + 65) x 4 x 24 b = 13 Kbit; total ~61 Kbit
  -> 2-3 BRAM36 (distributed RAM would also do for the allpass state).
* Dependency note for the RTL: a loop pass needs `D - 4 > M + 1` so the whole chain for sample n can
  finish before its output is needed (always true here), and `D + 4 < 1022`. The model evaluates the
  M x N cells as a wavefront (cell (m, n) at step n + m); this is bit-identical to the sequential order
  above (checked against a naive reference).

## RTL (`rtl/spring.v`, status: implemented, bit-exact in simulation)

* One sequential engine for both springs, one shared signed 25 x 18 multiplier (inferred DSP48,
  operand/product/result registers, result 3 states after the issue, like `tape_echo.v`), allpass state
  in a 1024 x 24 BRAM, the two delay lines in a 2048 x 24 BRAM (read/write ports separate, registered reads).
* A step starts at `frame_tick` and runs in parallel with the FIR: **728 clock cycles** (spring 1: 11 setup
  + 72 x 5 + 13; spring 2: 11 + 64 x 5 + 13; + start), of 2560 per frame (1280 in the small sim config).
  Each allpass section takes 5 cycles (diff, multiply 3, add/saturate/write); the memory holds `S[j][r]`,
  and the old `S[m+1][r]` read for the yd of section m is also the xd of section m+1.
* `wet = (W[n-1] * REVERB) >> 16` is formed in the first cycles of the step (REVERB sampled then),
  so it is ready long before the echo result of the same frame; `W[n]` is stored at the end of the step.
* The input `s` is latched when the echo stage starts (`y_done`); the step of the next frame uses it.
* Top level: `out = sat24(echo_out + wet)` in two register stages before `tx`, ECHO_PEAK / the output
  meter / LD5 see this final word. STATUS[5] is set (sticky, CLEAR resets it) if a frame tick arrives
  while a step is still running.
* Simulation (`run_sim.py`): REV on with the echo on, REV on with the echo off (other REVERB value), REV
  switched off while the tail rings out, REV toggled by BTN3 earlier in the run; the whole DAC stream is
  compared bit-exactly with `spring_model(..., echo_out=echo_model(fir_model(...)))`.

## Self-check results (`render_fx.py`, REVERB = 1.5 unless stated)

* Impulse response RT60 (Schroeder, T20 x 3, REVERB irrelevant): 1.7 s (100-300 Hz), 2.5-2.8 s (300-800 Hz),
  2.0-2.3 s (0.8-2 kHz), ~1 s (2-4 kHz): at loop gain 0.94 / lowpass 0.6 (the table values).
* Full-scale noise burst (0.5 s), 1 kHz sine, 110 Hz sine, 220 Hz square, 1 s each, then 20 s of silence:
  no instability; the wet tail decays from ~0.08 FS rms to ~1e-4 FS within 3 s and then sits on a rounding
  floor of 50-100 LSB rms (about -100 dBFS before REVERB, constant, from rounding in the loop).
  Internal `sat24` can occur in the chain at full-scale inputs (stable, brief); the final sum saturated
  on noise/square at REVERB = 1.5 (33 and 918 samples) and not on the sines.
