#!/usr/bin/env python3
"""
Run the RTL testbench with Icarus Verilog and check the output bit-exactly
against the Python model in sw/cab_conv.py.

    python3 sim/run_sim.py            # small config: 4 lanes x 64 taps, fast
    python3 sim/run_sim.py --full     # real config: 4 x 2048 taps, 122.88 MHz frame budget

The captured stream must be: unity IR -> IR A -> IR B -> bypass -> IR B + Space Echo
program (tape_echo.v vs cab_conv.echo_model) -> mute, with every switch landing exactly on
a sample boundary. Also checks the capture
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


ECHO_SLEW = 5       # sim only: speed glide settles within ~100 samples instead of ~40000


def echo_program():
    """Space Echo command list for tb_cab_conv.v: (op, a, b, tag) with
    op 0 write, 1 wait frames, 2 read+log, 3 event, 4 read+expect, 5 sync (see the TB).
    Writes after a sync land in a single frame. Returns (cmds, tag names)."""
    R = cc
    tags = []

    def tag(name):
        tags.append(name)
        return len(tags) - 1

    def q16(v):
        return int(round(v * 65536))

    cfg = {R.REG_ECHO_DELAY: q16(60.5), R.REG_ECHO_FB: q16(1.5), R.REG_ECHO_LEVEL: 45000,
           R.REG_ECHO_INGAIN: 80000, R.REG_ECHO_BASS: q16(0.5),
           R.REG_ECHO_TREBLE: q16(-0.5) & 0x3FFFF,
           R.REG_ECHO_WOW: (2 * 256) << 16 | (20 * 256), R.REG_ECHO_DAMP: 30000,
           R.REG_ECHO_REVERB: 12345}
    on = R.ECHO_EN | 0b1110 | R.ECHO_REV            # all heads
    cmds = []
    cmds += [(5, 0, 0, 0), (2, R.REG_ECHO_PEAK, 0, tag("p0")), (2, R.REG_ECHO_DRIVE, 0, tag("d0"))]
    cmds += [(5, 0, 0, 0), (0, 0x08, 0x00, 0), (3, 0, 0, tag("byp_off"))]
    cmds += [(1, 0, 20, 0)]
    cmds += [(5, 0, 0, 0)] + [(0, a, v, 0) for a, v in cfg.items()]
    cmds += [(0, R.REG_ECHO_CTRL, on, 0), (3, 0, 0, tag("cfg"))]
    cmds += [(4, a, v, 0) for a, v in cfg.items()] + [(4, R.REG_ECHO_CTRL, on, 0)]
    cmds += [(1, 0, 300, 0)]
    cmds += [(5, 0, 0, 0), (2, R.REG_ECHO_PEAK, 0, tag("p1")), (2, R.REG_ECHO_DRIVE, 0, tag("d1"))]
    cmds += [(5, 0, 0, 0), (0, R.REG_ECHO_DELAY, q16(25.25), 0), (3, 0, 0, tag("delay"))]
    cmds += [(1, 0, 300, 0)]
    cmds += [(5, 0, 0, 0), (0, R.REG_ECHO_CTRL, on | R.ECHO_CANCEL, 0), (3, 0, 0, tag("cancel"))]
    cmds += [(1, 0, 200, 0)]
    cmds += [(5, 0, 0, 0), (0, R.REG_ECHO_CTRL, on, 0), (3, 0, 0, tag("uncancel"))]
    cmds += [(1, 0, 150, 0)]
    cmds += [(5, 0, 0, 0), (2, R.REG_ECHO_PEAK, 0, tag("p2")), (2, R.REG_ECHO_DRIVE, 0, tag("d2"))]
    cmds += [(5, 0, 0, 0), (0, R.REG_ECHO_DELAY, q16(70.75), 0),
             (0, R.REG_ECHO_CTRL, R.ECHO_EN | 0b1100, 0), (3, 0, 0, tag("heads23"))]
    cmds += [(1, 0, 250, 0)]
    cmds += [(5, 0, 0, 0), (0, R.REG_ECHO_CTRL, 0x02, 0), (3, 0, 0, tag("off"))]
    cmds += [(1, 0, 100, 0)]
    cmds += [(5, 0, 0, 0), (2, R.REG_ECHO_PEAK, 0, tag("p3")), (2, R.REG_ECHO_DRIVE, 0, tag("d3"))]
    cmds += [(5, 0, 0, 0), (0, 0x08, 0x02, 0), (3, 0, 0, tag("mute"))]
    cmds += [(0xF, 0, 0, 0)]
    # the schedule the model sees: tag -> {reg: value}
    sched = {"cfg": {**cfg, R.REG_ECHO_CTRL: on},
             "delay": {R.REG_ECHO_DELAY: q16(25.25)},
             "cancel": {R.REG_ECHO_CTRL: on | R.ECHO_CANCEL},
             "uncancel": {R.REG_ECHO_CTRL: on},
             "heads23": {R.REG_ECHO_DELAY: q16(70.75), R.REG_ECHO_CTRL: R.ECHO_EN | 0b1100},
             "off": {R.REG_ECHO_CTRL: 0x02}}
    return cmds, tags, sched


def hex24(v):
    return "%06x" % (int(v) & 0xFFFFFF)


def hex18(v):
    return "%05x" % (int(v) & 0x3FFFF)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="4 x 2048 taps")
    ap.add_argument("--reuse", action="store_true", help="re-check the last simulation log/output")
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
    ecmds, etags, esched = echo_program()
    echo_frames = sum(c[2] for c in ecmds if c[0] == 1) + 3 * sum(1 for c in ecmds if c[0] == 5) + 40
    rev_frames = 800                     # two BTN3 (reverb) test phases with unmuted output
    nin = f0 + f1 + f2 + f3 + tail + 2 * load_frames + 20 + rec_frames + echo_frames + rev_frames
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

    with open(os.path.join(w, "echo_cmd.hex"), "w") as f:
        f.write("\n".join("%x %x %x %x" % c for c in ecmds) + "\n")

    rtl = [os.path.join(ROOT, "rtl", n) for n in
           ("clk_fwd.v", "debounce.v", "level_led.v", "i2s_master.v", "fir_engine.v", "tape_echo.v", "cab_conv_top.v")]
    vvp = os.path.join(w, "tb.vvp")
    logf = os.path.join(w, "sim.log")
    if not args.reuse:
        subprocess.check_call(
            ["iverilog", "-g2005", "-DSIMULATION", "-DNLANE=%d" % nlane,
             "-DLW=%d" % lw, "-DHALF=%d" % half,
             "-DCAP_LOG2=%d" % cap_log2, "-DDEB_BITS=%d" % deb_bits,
             "-DECHO_SLEW=%d" % ECHO_SLEW, "-o", vvp,
             os.path.join(HERE, "tb_cab_conv.v")] + rtl)
    print("taps=%d (%d x %d), shift A=%d, shift B=%d, %d frames"
          % (ntaps, nlane, ltaps, sha, shb, nin))
    if args.reuse:
        class log:                       # saved result of the previous run
            returncode, stderr = 0, ""
            stdout = open(logf).read()
    else:
        log = subprocess.run(
            ["vvp", "-n", vvp, "+NIN=%d" % nin, "+SHA=%d" % sha, "+SHB=%d" % shb,
             "+F0=%d" % f0, "+F1=%d" % f1, "+F2=%d" % f2, "+F3=%d" % f3],
            cwd=w, capture_output=True, text=True)
        with open(logf, "w") as f:
            f.write(log.stdout)
    print(log.stdout)
    if log.returncode != 0 or "DONE" not in log.stdout:
        print(log.stderr)
        sys.exit("simulation failed")

    out = np.loadtxt(os.path.join(w, "out.txt"), dtype=np.int64, ndmin=2)
    ol, orr = out[:, 0], out[:, 1]

    # expected streams (left input selected)
    ya, sata = cc.fir_model(xl, qa, sha, ltaps)
    yb, satb = cc.fir_model(xl, qb, shb, ltaps)
    phases = [("unity", xl), ("IR A", ya), ("IR B", yb), ("bypass", xl)]

    # events logged by the testbench: "EVT tag S" and "RD tag S addr value" (S = frames ticked)
    ev, rd = {}, {}
    for line in log.stdout.splitlines():
        p_ = line.split()
        if len(p_) == 3 and p_[0] == "EVT":
            ev[etags[int(p_[1])]] = int(p_[2])
        elif len(p_) == 5 and p_[0] == "RD":
            rd[etags[int(p_[1])]] = (int(p_[2]), int(p_[4]))
    if len(ev) != 8 or len(rd) != 8:
        sys.exit("FAIL: echo program log incomplete (%d events, %d reads)" % (len(ev), len(rd)))
    n_cmp = ev["byp_off"] - 8        # phases up to here are checked sample by sample below

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
        if n >= nin or n >= n_cmp:
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

    # ---- Space Echo: DAC stream vs echo_model(cab output), ECHO_PEAK / ECHO_DRIVE ----
    echo_ok = False
    tb, ta, tbp = dict(trans)["IR B"], dict(trans)["IR A"], dict(trans)["bypass"]
    ns = min(len(ol) - lag, nin)
    best = None
    # The echo runs once per DUT frame j from reset; sample n of the input stream is frame
    # j = n - dly (dly is the unknown, constant frame offset: found by search).
    # Register writes and reads are logged in frames (S = frames ticked so far).
    for dly in range(-4, 5):
        nb = ev["byp_off"] + dly
        s_full = np.concatenate([xl[:ta], ya[ta:tb], yb[tb:tbp], xl[tbp:nb], yb[nb:]])[:ns]
        s_fr = np.concatenate([np.zeros(max(0, -dly), dtype=np.int64), s_full[max(0, dly):]])
        sched = [(ev[k], v) for k, v in esched.items()]
        out_f, sat_f, rabs_f = cc.echo_model(s_fr, schedule=sched, want_drive=True, slew=ECHO_SLEW)
        out_m = out_f[-dly:] if dly <= 0 else np.concatenate([np.zeros(dly, dtype=np.int64), out_f])
        out_m = out_m[:ns]
        exp = out_m.copy()
        exp[ev["mute"] + dly:] = 0
        bad = int(np.count_nonzero(exp[tbp:] != ol[lag + tbp:lag + len(exp)]))
        if best is None or bad < best[0]:
            best = (bad, dly, exp, s_fr, out_f, sat_f, rabs_f)
        if bad == 0:
            break
    bad, dly, exp, s_fr, out_f, sat_f, rabs_f = best
    print("echo: %d mismatching samples of %d (frame offset %d)" % (bad, len(exp) - tbp, dly))
    echo_ok = bad == 0
    for k in np.nonzero(exp[tbp:] != ol[lag + tbp:lag + len(exp)])[0][:5]:
        print("  echo MISMATCH n=%d got %d, expected %d" % (tbp + k, ol[lag + tbp + k], exp[tbp + k]))
    # the echo must actually have been audible and exercised its limiters
    en0, en1 = ev["cfg"], ev["off"]
    diff = int(np.count_nonzero(out_f[en0:en1] != s_fr[en0:en1]))
    print("echo: %d of %d samples differ from the dry cab output, %d saturated in model, "
          "peak |r| = %d" % (diff, en1 - en0, int(sat_f[en0:en1].sum()), int(rabs_f.max())))
    echo_ok &= diff > (en1 - en0) // 2
    prev = 0
    for i in range(4):
        sp, pk = rd["p%d" % i]
        sd, dr = rd["d%d" % i]
        exp_pk = min(int(np.max(np.abs(out_f[prev:sp]))), cc.YMAX)   # |full-scale negative| clips
        exp_pk |= int(sat_f[prev:sp].any()) << 24
        exp_dr = int(rabs_f[prev:sp].max())
        good = (pk == exp_pk and dr == exp_dr and sp == sd)
        print("echo meters p%d: PEAK %07x (model %07x), DRIVE %07x (model %07x) %s"
              % (i, pk, exp_pk, dr, exp_dr, "ok" if good else "MISMATCH"))
        echo_ok &= good
        prev = sp
    ok = (errors == 0 and cap_ok and names == ["IR A", "IR B", "bypass"] and echo_ok
          and "FAIL" not in log.stdout)
    print("RESULT:", "PASS" if ok else "FAIL (%d errors)" % errors)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
