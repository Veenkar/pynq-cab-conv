#!/usr/bin/env python3
"""
Run the RTL testbench with Icarus Verilog and check the output bit-exactly
against the Python model in sw/cab_conv.py.

    python3 sim/run_sim.py            # small config: 4 lanes x 64 taps, fast
    python3 sim/run_sim.py --full     # real config: 4 x 2048 taps, 122.88 MHz frame budget

The captured stream must be: unity IR -> IR A -> IR B -> bypass -> mute,
with every switch landing exactly on a sample boundary. Also checks the capture
ring (REC via AXI, ring wraps) bit-exactly against the model and the debounced
record button / led_rec and the bypass button / led_cab.
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# prefer the repo venv (setup_venv.bat / setup_venv.sh) when it exists
_venv = os.path.join(ROOT, ".venv")
_vpy = os.path.join(_venv, "Scripts", "python.exe") if os.name == "nt" else os.path.join(_venv, "bin", "python")
if os.path.exists(_vpy) and os.path.normcase(os.path.abspath(sys.prefix)) != os.path.normcase(_venv):
    sys.exit(subprocess.call([_vpy, os.path.abspath(__file__)] + sys.argv[1:]))

import numpy as np  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "sw"))
import cab_conv as cc  # noqa: E402


def synth_cab_ir(n, fs, rng, bright=1.0):
    """Cab-like test IR: decaying noise through a few resonances."""
    from scipy.signal import lfilter, butter
    t = np.arange(n) / fs
    h = rng.standard_normal(n) * np.exp(-t / 0.004)
    b, a = butter(2, [80 / (fs / 2), min(0.99, 5000 * bright / (fs / 2))], "band")
    h = lfilter(b, a, h)
    return h


def guitar_like(n, fs, rng, level=0.5):
    t = np.arange(n) / fs
    s = np.zeros(n)
    for f in (82.4, 110.0, 146.8, 196.0, 246.9, 329.6):
        s += np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28)) * rng.uniform(0.3, 1.0)
    s += 0.3 * rng.standard_normal(n)
    s = s / np.max(np.abs(s)) * level
    # a couple of full-scale bursts to exercise clipping logic
    s[n // 2: n // 2 + 40] = np.sign(s[n // 2: n // 2 + 40] + 1e-9)
    return np.clip(np.round(s * cc.YMAX), cc.YMIN, cc.YMAX).astype(np.int64)


def hex24(v):
    return "%06x" % (int(v) & 0xFFFFFF)


def hex18(v):
    return "%05x" % (int(v) & 0x3FFFF)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="4 x 2048 taps")
    ap.add_argument("--workdir", help="default sim/work, or sim/work_full with --full")
    args = ap.parse_args()

    if args.full:
        nlane, lw, half, cap_log2 = 4, 11, 20, 6
        f0, f1, f2, f3, tail = 40, 60, 8400, 30, 30   # B runs > 8192 samples
    else:
        nlane, lw, half, cap_log2 = 4, 6, 10, 5
        f0, f1, f2, f3, tail = 40, 400, 400, 40, 40
    deb_bits = 4
    capn = 1 << cap_log2
    ltaps = 1 << lw
    ntaps = nlane * ltaps
    fs = 48000
    rng = np.random.default_rng(1234)

    # IR A: normal level, IR B: +9 dB to force output saturation sometimes
    ha = cc.prepare_ir(synth_cab_ir(ntaps, fs, rng), fs, fs, ntaps, gain_db=-3)
    hb = cc.prepare_ir(synth_cab_ir(ntaps, fs, rng, 0.5), fs, fs, ntaps, gain_db=9,
                       fade_ms=0.5)
    # make sure every lane carries energy (tests the lane cascade)
    hb[-ltaps // 2] += 0.2
    qa, sha = cc.quantize(ha, ntaps, ltaps)
    qb, shb = cc.quantize(hb, ntaps, ltaps)

    # loading coefficients takes a few frames of simulated time
    load_frames = (ntaps * 6) // (2 * half * 64) + 6
    rec_frames = 3 * capn + 200          # capture + button test, inside the IR B phase
    nin = f0 + f1 + f2 + f3 + tail + 2 * load_frames + 20 + rec_frames
    xl = guitar_like(nin, fs, rng, 0.5)
    xr = guitar_like(nin, fs, rng, 0.25)

    if args.workdir is None:      # separate dirs so both configs can run at once
        args.workdir = os.path.join(HERE, "work_full" if args.full else "work")
    os.makedirs(args.workdir, exist_ok=True)
    w = os.path.abspath(args.workdir)
    with open(os.path.join(w, "inl.hex"), "w") as f:
        f.write("\n".join(hex24(v) for v in xl) + "\n")
    with open(os.path.join(w, "inr.hex"), "w") as f:
        f.write("\n".join(hex24(v) for v in xr) + "\n")
    with open(os.path.join(w, "coefa.hex"), "w") as f:
        f.write("\n".join(hex18(v) for v in qa) + "\n")
    with open(os.path.join(w, "coefb.hex"), "w") as f:
        f.write("\n".join(hex18(v) for v in qb) + "\n")

    rtl = [os.path.join(ROOT, "rtl", n) for n in
           ("clk_fwd.v", "debounce.v", "level_led.v", "i2s_master.v", "fir_engine.v", "cab_conv_top.v")]
    vvp = os.path.join(w, "tb.vvp")
    subprocess.check_call(
        ["iverilog", "-g2005", "-DSIMULATION", "-DNLANE=%d" % nlane,
         "-DLW=%d" % lw, "-DHALF=%d" % half,
         "-DCAP_LOG2=%d" % cap_log2, "-DDEB_BITS=%d" % deb_bits, "-o", vvp,
         os.path.join(HERE, "tb_cab_conv.v")] + rtl)
    print("taps=%d (%d x %d), shift A=%d, shift B=%d, %d frames"
          % (ntaps, nlane, ltaps, sha, shb, nin))
    log = subprocess.run(
        ["vvp", "-n", vvp, "+NIN=%d" % nin, "+SHA=%d" % sha, "+SHB=%d" % shb,
         "+F0=%d" % f0, "+F1=%d" % f1, "+F2=%d" % f2, "+F3=%d" % f3],
        cwd=w, capture_output=True, text=True)
    print(log.stdout)
    if log.returncode != 0 or "DONE" not in log.stdout:
        print(log.stderr)
        sys.exit("simulation failed")

    out = np.loadtxt(os.path.join(w, "out.txt"), dtype=np.int64, ndmin=2)
    ol, orr = out[:, 0], out[:, 1]

    # expected streams (left input selected)
    ya, sata = cc.fir_model(xl, qa, sha, ltaps)
    yb, satb = cc.fir_model(xl, qb, shb, ltaps)
    phases = [("unity", xl), ("IR A", ya), ("IR B", yb),
              ("bypass", xl), ("mute", np.zeros(nin, dtype=np.int64))]

    # latency: find lag where output == input during the unity phase
    lag = None
    for cand in range(0, 8):
        seg = range(cand + 4, f0 - 4)
        if all(ol[m] == xl[m - cand] for m in seg):
            lag = cand
            break
    if lag is None:
        sys.exit("FAIL: could not align output with input")
    print("latency: %d samples between ADC word and DAC word" % lag)

    p, trans, errors = 0, [], 0
    for m in range(lag, len(ol)):
        n = m - lag
        if n >= nin:
            break
        if ol[m] != orr[m]:
            errors += 1
            if errors < 5:
                print("L/R mismatch at %d" % n)
        if ol[m] == phases[p][1][n]:
            continue
        for q in range(p + 1, len(phases)):
            if ol[m] == phases[q][1][n]:
                trans.append((phases[q][0], n))
                p = q
                break
        else:
            errors += 1
            if errors < 10:
                print("MISMATCH n=%d got %d, expected %s=%d"
                      % (n, ol[m], phases[p][0], phases[p][1][n]))

    names = [t[0] for t in trans]
    print("transitions:", ", ".join("%s @ sample %d" % t for t in trans))
    nb0 = dict(trans).get("IR B")
    nb1 = dict(trans).get("bypass")
    if nb0 and nb1:
        print("IR B segment: %d samples, %d saturated in model"
              % (nb1 - nb0, int(satb[nb0:nb1].sum())))
    # ---- capture ring: REC_WPTR, then {x, y} per ring slot ----
    with open(os.path.join(w, "cap.txt")) as f:
        wptr = int(f.readline())
    cap = np.loadtxt(os.path.join(w, "cap.txt"), dtype=np.int64, skiprows=1, ndmin=2)
    if cap.shape != (capn, 2) or wptr <= capn:
        sys.exit("FAIL: capture dump (wptr=%d, shape=%s)" % (wptr, cap.shape))
    # frame n lives in slot n mod capn; unroll the last capn frames in order
    ns = np.arange(wptr - capn, wptr)
    cx, cy = cap[ns % capn, 0], cap[ns % capn, 1]
    # locate the recorded window in the input stream (dry x is the left input)
    starts = [s for s in range(len(xl) - capn + 1) if np.array_equal(xl[s:s + capn], cx)]
    cap_ok = len(starts) == 1
    if cap_ok:
        s0 = starts[0]
        cap_ok = np.array_equal(cy, yb[s0:s0 + capn])
        print("capture: %d frames recorded, ring %d, window = input samples %d..%d, "
              "x/y bit-exact: %s" % (wptr, capn, s0, s0 + capn - 1, cap_ok))
    else:
        print("capture: could not align ring with input (%d matches)" % len(starts))

    ok = (errors == 0 and cap_ok and names == ["IR A", "IR B", "bypass", "mute"]
          and "FAIL" not in log.stdout)
    print("RESULT:", "PASS" if ok else "FAIL (%d errors)" % errors)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
