# Status display daemon: shows cab on, REC time, IR name, echo / reverb on and input/output peak meters
# on a 128x32 SSD1306 OLED (see oled.py). Reads cab_conv registers only (overlay is
# not reloaded, codec and IR untouched).
#   sudo -i python3 ~xilinx/cab_conv/status.py [--bus 0] [--addr 0x3C] [--rate 8] [--flip]
# The IR name comes from current_ir.txt (written by autostart.py / load_ir.py).
# Each refresh reads PEAK_IN/PEAK_OUT and then writes CLEAR, which also clears the sticky
# clip flags; clips are therefore held for 2 s here. meter.py does the same, so running both
# at once makes each see only part of the peaks.
import argparse, math, os, signal, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, "/home/xilinx/cab_conv")
from oled import SSD1306, Canvas, W
from cab_conv import (MMIO_RANGE, REG_ID, ID_VALUE, REG_CTRL, REG_STATUS, REG_CLEAR,
                      REG_PEAK_IN, REG_PEAK_OUT, REG_REC_WPTR, REG_ECHO_CTRL, ECHO_EN, ECHO_REV)

FS = 48000
FULL = 8388607.0
DB_MIN = -48.0                  # bar scale: DB_MIN .. 0 dBFS (ticks at TICKS_DB)
TICKS_DB = (-24.0, -12.0, -6.0)
FALL_DB_S = 40.0                # bar fall speed
CLIP_HOLD_S = 2.0
IR_FILE = "/home/xilinx/cab_conv/current_ir.txt"


def db(v):
    return 20 * math.log10(v / FULL) if v else -999.0


def render(s):
    """s: dict(overlay, bypass, mute, rec, rec_s, blink, ir, echo, rev, in_db, out_db, in_clip, out_clip)
    -> Canvas."""
    c = Canvas()
    if not s["overlay"]:
        c.text(6, 9, "no overlay", scale=2)
        return c
    # one text line: inverted CAB tag (only while the cab is active, gone in bypass), then the IR name
    # (or the REC timer while recording); inverted REV and ECHO tags at fixed places on the right while
    # the reverb (BTN3) / the tape echo (BTN2) is on
    echo, rev = s.get("echo", False), s.get("rev", False)
    if not s["bypass"]:
        c.rect(0, 0, 19, 7)
        c.text(1, 0, "CAB", on=False)
    if s["rec"]:
        m, sec = divmod(int(s["rec_s"]), 60)
        if s["blink"]:                              # blinking box: dot + REC, inverted
            c.rect(21, 0, 27, 7)
            c.rect(23, 2, 3, 3, on=False)
            c.text(29, 0, "REC", on=False)
        c.text(51, 0, "%02d:%02d" % (min(m, 99), sec))
        if s["mute"] and not rev:
            c.text(84, 0, "M")
    else:
        c.text(22, 0, (("MUTE " if s["mute"] else "") + s["ir"])[:10 if rev else 13 if echo else 17])
    if rev:
        c.rect(W - 44, 0, 19, 7)
        c.text(W - 43, 0, "REV", on=False)
    if echo:
        c.rect(W - 24, 0, 24, 7)
        c.text(W - 23, 0, "ECHO", on=False)
    # two 11 px meters (input above output, 2 px apart), 2 px border, DB_MIN..0 dBFS, ticks at TICKS_DB;
    # a clip (held CLIP_HOLD_S) shows as an inverted 8 px cap at the right end of that meter
    ticks = tuple((t - DB_MIN) / -DB_MIN for t in TICKS_DB)
    for y, d, clip in ((8, s["in_db"], s["in_clip"]), (21, s["out_db"], s["out_clip"])):
        c.hbar(0, y, W, 11, (d - DB_MIN) / -DB_MIN, ticks=ticks, border=2)
        if clip:
            c.invert(W - 11, y + 3, 8, 5)
    return c


class Status:
    def __init__(self, mmio, ir_file):
        self.mm, self.ir_file = mmio, ir_file
        self.disp = [DB_MIN, DB_MIN]
        self.clip_t = [0.0, 0.0]
        self.t_prev = time.time()
        self.ir, self.t_ir = "", 0.0

    def _ir_name(self, now):
        if now - self.t_ir >= 2.0:
            self.t_ir = now
            try:
                with open(self.ir_file) as f:
                    self.ir = f.read().strip()
            except OSError:
                self.ir = ""
        return self.ir

    def sample(self):
        now = time.time()
        if self.mm.read(REG_ID) != ID_VALUE:
            return dict(overlay=False)
        ctrl, st = self.mm.read(REG_CTRL), self.mm.read(REG_STATUS)
        pk = [self.mm.read(REG_PEAK_IN), self.mm.read(REG_PEAK_OUT)]
        self.mm.write(REG_CLEAR, 1)
        dt, self.t_prev = now - self.t_prev, now
        for i in (0, 1):
            self.disp[i] = max(max(db(pk[i]), DB_MIN - 20), self.disp[i] - FALL_DB_S * dt)
            if st & (2 << i):
                self.clip_t[i] = now
        rec = bool(ctrl & 0x40)
        wptr = self.mm.read(REG_REC_WPTR) if rec else 0
        ectrl = self.mm.read(REG_ECHO_CTRL)
        # same phase as the REC LED: on for the first half of each 2^26-cycle period (0.55 s) since REC
        # rose; frames since REC rose = REC_WPTR, 2560 aclk cycles per frame
        blink = not (wptr * 2560 >> 25) & 1
        return dict(overlay=True, bypass=bool(ctrl & 1), mute=bool(ctrl & 2), rec=rec,
                    rec_s=wptr / FS, blink=blink,
                    ir=self._ir_name(now), echo=bool(ectrl & ECHO_EN), rev=bool(ectrl & ECHO_REV), in_db=self.disp[0], out_db=self.disp[1],
                    in_clip=now - self.clip_t[0] < CLIP_HOLD_S, out_clip=now - self.clip_t[1] < CLIP_HOLD_S)


def log(msg):
    print(msg, flush=True)


def main():
    ap = argparse.ArgumentParser(description="OLED status display for the cab convolver")
    ap.add_argument("--bus", type=int, default=0, help="I2C bus of the OLED (/dev/i2c-N)")
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=0x3C)
    ap.add_argument("--rate", type=float, default=8.0, help="refreshes per second")
    ap.add_argument("--flip", action="store_true", help="rotate the image by 180 degrees")
    ap.add_argument("--contrast", type=lambda x: int(x, 0), default=0xFF)
    ap.add_argument("--ir-file", default=IR_FILE)
    ap.add_argument("--base", type=lambda x: int(x, 0), default=0x43C00000, help="IP base address")
    a = ap.parse_args()

    from pynq import MMIO
    st = Status(MMIO(a.base, MMIO_RANGE), a.ir_file)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))

    disp, backoff, was_ov = None, 1.0, None
    try:
        while not stop:
            if disp is None:
                try:
                    disp = SSD1306(a.bus, a.addr, a.flip)
                    disp.contrast(a.contrast)
                    log("OLED ok on /dev/i2c-%d addr 0x%02X" % (a.bus, a.addr))
                    backoff = 1.0
                except OSError as e:
                    log("OLED not available on /dev/i2c-%d addr 0x%02X (%s); retry in %.0f s"
                        % (a.bus, a.addr, e, backoff))
                    t_end = time.time() + backoff
                    while not stop and time.time() < t_end:
                        time.sleep(0.2)
                    backoff = min(backoff * 2, 30.0)
                    continue
            s = st.sample()
            if s["overlay"] != was_ov:
                log("overlay loaded" if s["overlay"] else "cab_conv overlay not loaded, waiting")
                was_ov = s["overlay"]
            try:
                disp.show(render(s))
            except OSError as e:
                log("OLED I2C error (%s), reinitialising" % e)
                disp.close()
                disp = None
                continue
            time.sleep(1.0 / a.rate if s["overlay"] else 1.0)
    finally:
        if disp is not None:
            try:
                disp.clear()
                disp.on(False)
            except OSError:
                pass
            disp.close()


if __name__ == "__main__":
    main()
