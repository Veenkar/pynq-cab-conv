#!/usr/bin/env python3
"""
Offline render of the board's effect chain: [cab FIR] -> Space Echo (echo_model) -> spring
reverb (spring_model), all bit-exact fixed-point models from cab_conv.py.

    python3 sw/render_fx.py [riff.wav] [--out DIR] [--no-cab] [--reverb 1.0]

Writes spring_only / echo_spring / echo_only / clicks_spring WAVs (48 kHz, 24-bit) to DIR.
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
_venv = os.path.join(ROOT, ".venv")
_vpy = os.path.join(_venv, "Scripts", "python.exe") if os.name == "nt" else os.path.join(_venv, "bin", "python")
if os.path.exists(_vpy) and os.path.normcase(os.path.abspath(sys.prefix)) != os.path.normcase(_venv):
    sys.exit(subprocess.call([_vpy, os.path.abspath(__file__)] + sys.argv[1:]))

import numpy as np  # noqa: E402
from scipy.io import wavfile  # noqa: E402

sys.path.insert(0, HERE)
import cab_conv as cc  # noqa: E402

DEFAULT_OUT = r"C:\Users\xeenk\AppData\Local\Temp\claude\C--git-pynq-cab-conv\a3c76035-27c9-464e-a3e7-14ce45c3385a\scratchpad"


def save(path, y):
    wavfile.write(path, 48000, np.asarray(y, dtype=np.int64).astype(np.int32) << 8)
    print("wrote", path, "peak %.1f dBFS" % (20 * np.log10(max(1, np.abs(y).max()) / 2.0 ** 23)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", nargs="?", default=os.path.join(ROOT, "riff.wav"))
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--no-cab", action="store_true")
    ap.add_argument("--reverb", type=float, default=6.0, help="REVERB VOLUME knob 0..10")
    ap.add_argument("--peak", type=float, default=-6.0, help="input peak, dBFS")
    a = ap.parse_args()

    x, fs = cc.read_wav(a.wav)
    x = cc.resample(x, fs, cc.FS)
    x = x / np.max(np.abs(x)) * 10 ** (a.peak / 20.0)
    s = np.round(x * 2 ** 23).astype(np.int64)
    cab = os.path.join(ROOT, "cab.wav")
    if not a.no_cab and os.path.exists(cab):
        h, fsh = cc.read_wav(cab)
        h = cc.prepare_ir(h, fsh, gain_db=3.0)
        hq, sh = cc.quantize(h, 8192, 2048)
        s, _ = cc.fir_model(s, hq, sh, 2048)
        print("cab applied")
    os.makedirs(a.out, exist_ok=True)
    tail = np.zeros(4 * cc.FS, dtype=np.int64)
    s = np.concatenate([s, tail])

    rev = cc.echo_regs(dict(reverb_volume=a.reverb, reverb=True))
    rg = rev[cc.REG_ECHO_REVERB]
    # 1) spring only (echo off): REV on, tail rings out
    o, _ = cc.spring_model(s, True, rg, echo_out=s)
    save(os.path.join(a.out, "spring_riff.wav"), o)
    # 2) echo + spring
    regs = cc.echo_regs(dict(cc.ECHO_KNOBS, power=True, reverb=True, reverb_volume=a.reverb))
    e, _ = cc.echo_model(s, regs)
    o, _ = cc.spring_model(s, True, rg, echo_out=e)
    save(os.path.join(a.out, "spring_echo_riff.wav"), o)
    # 3) percussive test: plucks / clicks
    n = 8 * cc.FS
    c = np.zeros(n, dtype=np.int64)
    for i, t in enumerate((0.2, 1.8, 3.4, 5.0)):
        k = int(t * cc.FS)
        env = np.exp(-np.arange(2400) / (60 + 40 * i))
        f = (110, 220, 330, 440)[i]
        c[k:k + 2400] = np.round(0.5 * 2 ** 23 * env * np.sin(2 * np.pi * f * np.arange(2400) / cc.FS)).astype(np.int64)
    c[int(6.6 * cc.FS)] = 2 ** 22          # single click
    o, _ = cc.spring_model(c, True, 65536, echo_out=c)
    save(os.path.join(a.out, "spring_clicks.wav"), o)


if __name__ == "__main__":
    main()
