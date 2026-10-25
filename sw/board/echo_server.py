# Space Echo control server: JSON lines over TCP for the PC panel (sw/echo_panel.py).
# Keeps the knob state, writes the echo registers (cab_conv.echo_regs) and persists the state
# so the echo comes back after a reboot. Protocol: docs/space_echo.md ("Panel and board server").
#   sudo -i python3 ~xilinx/cab_conv/echo_server.py [--port 5201] [--base 0x43C00000] [--state FILE]
#   python3 sw/board/echo_server.py --fake          # on the PC: in-memory fake registers
# The OUTPUT H/M/L switch (knob "output") sets the headphone volume over the codec's I2C
# (ECHO_OUTPUT_DB), so it overrides autostart.py's --hp-db once the state has been applied.
# The server never writes REG_CLEAR (status.py / meter.py do); reading ECHO_PEAK / ECHO_DRIVE
# clears those two registers, so run only one meter client for them.
import argparse, json, math, os, signal, socketserver, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, "/home/xilinx/cab_conv")
from cab_conv import (MMIO_RANGE, REG_ID, ID_VALUE, REG_CTRL, CTRL_BYPASS, REG_ECHO_PEAK,
                      REG_ECHO_DRIVE, REG_ECHO_CTRL, ECHO_EN, ECHO_REV, ECHO_KNOBS, ECHO_MODES, ECHO_OUTPUT_DB,
                      echo_regs)

STATE_FILE = "/home/xilinx/cab_conv/echo_state.json"
BOOL_KNOBS = ("power", "cancel", "reverb")
POLL_S = 0.5                    # overlay presence check, BTN2 state sync


def log(msg):
    print(msg, flush=True)


class FakeMMIO:
    """In-memory register file for --fake; the meter registers return moving values."""

    def __init__(self):
        self.regs = {REG_ID: ID_VALUE, REG_CTRL: 0}
        self.t0 = time.time()

    def read(self, off):
        t = time.time() - self.t0
        if off in (REG_ECHO_PEAK, REG_ECHO_DRIVE):
            on = self.regs.get(0x40, 0) & 1
            lvl = (0.35 + 0.3 * math.sin(2.1 * t) * math.sin(0.37 * t)) * on
            if off == REG_ECHO_PEAK:
                return int(max(lvl, 0.0) * 0.6 * (1 << 23)) | ((1 << 24) if lvl > 0.62 else 0)
            return int(max(lvl, 0.0) * 1.7 * (1 << 23))
        return self.regs.get(off, 0)

    def write(self, off, val):
        self.regs[off] = val & 0xFFFFFFFF


class Echo:
    """Knob state + register access, all under one lock."""

    def __init__(self, mm, state_file, set_hp=None):
        self.mm, self.state_file, self.set_hp = mm, state_file, set_hp
        self.lock = threading.Lock()
        self.knobs = dict(ECHO_KNOBS)
        self.present = False
        try:
            with open(state_file) as f:
                saved = json.load(f)
            ok, err = self.validate(saved)
            if err:
                log("state file ignored: " + err)
            else:
                self.knobs.update(ok)
                log("state restored from " + state_file)
        except (OSError, ValueError):
            pass

    @staticmethod
    def validate(knobs):
        """-> (clean dict, error string or None)."""
        if not isinstance(knobs, dict):
            return {}, "knobs must be an object"
        out = {}
        for k, v in knobs.items():
            if k not in ECHO_KNOBS:
                return {}, "unknown knob '%s'" % k
            if k in BOOL_KNOBS:
                if not isinstance(v, bool):
                    return {}, "'%s' must be true/false" % k
                out[k] = v
            elif k == "output":
                if v not in ECHO_OUTPUT_DB:
                    return {}, "'output' must be one of %s" % "/".join(ECHO_OUTPUT_DB)
                out[k] = v
            elif k == "mode":
                if isinstance(v, bool) or not isinstance(v, int) or v not in ECHO_MODES:
                    return {}, "'mode' must be an integer 1..%d" % max(ECHO_MODES)
                out[k] = v
            else:
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 <= v <= 10.0:
                    return {}, "'%s' must be a number 0..10" % k
                out[k] = float(v)
        return out, None

    def _apply(self, hp=True):
        for off, val in echo_regs(self.knobs).items():
            self.mm.write(off, val)
        if hp and self.set_hp:
            try:
                self.set_hp(ECHO_OUTPUT_DB[self.knobs["output"]])
            except (OSError, RuntimeError) as e:
                log("headphone volume not set (%s)" % e)

    def _save(self):
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self.knobs, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_file)
        except OSError as e:
            log("cannot save state (%s)" % e)

    def _sync_buttons(self):
        """BTN2 / BTN3 toggle ECHO_CTRL.EN / REV in the PL: adopt them so a later register write doesn't
        undo the press, and save them so the last state comes back after a reboot."""
        c = self.mm.read(REG_ECHO_CTRL)
        for key, bit, name in (("power", ECHO_EN, "echo (BTN2)"), ("reverb", ECHO_REV, "reverb (BTN3)")):
            if bool(c & bit) != self.knobs[key]:
                self.knobs[key] = bool(c & bit)
                self._save()
                log("%s %s" % (name, "on" if c & bit else "off"))

    def poll_overlay(self):
        """Re-apply the state whenever the overlay (re)appears; returns presence."""
        with self.lock:
            now = self.mm.read(REG_ID) == ID_VALUE
            if now and not self.present:
                self._apply()
                log("overlay present, echo state applied")
            elif now:
                self._sync_buttons()
            elif self.present:
                log("overlay gone, waiting")
            self.present = now
            return now

    def get(self):
        with self.lock:
            return {"ok": True, "knobs": dict(self.knobs),
                    "cab_bypass": bool(self.mm.read(REG_CTRL) & CTRL_BYPASS) if self.present else False}

    def set(self, req):
        clean, err = self.validate(req.get("knobs", {}))
        if err:
            return {"ok": False, "error": err}
        byp = req.get("cab_bypass")
        if byp is not None and not isinstance(byp, bool):
            return {"ok": False, "error": "'cab_bypass' must be true/false"}
        if "knobs" not in req and byp is None:
            return {"ok": False, "error": "nothing to set"}
        with self.lock:
            if not self.present:
                return {"ok": False, "error": "overlay not loaded"}
            if clean:
                self._sync_buttons()
                # turning the MODE SELECTOR sets reverb per the RE-201 table unless the request says otherwise
                if "mode" in clean and "reverb" not in clean and clean["mode"] != self.knobs["mode"]:
                    clean["reverb"] = clean["mode"] >= 5
                changed = {k: v for k, v in clean.items() if self.knobs[k] != v}
                if changed:
                    self.knobs.update(changed)
                    self._apply(hp="output" in changed)
                    self._save()
            if byp is not None:
                c = self.mm.read(REG_CTRL)
                self.mm.write(REG_CTRL, (c | CTRL_BYPASS) if byp else (c & ~CTRL_BYPASS))
        return {"ok": True}

    def meters(self):
        with self.lock:
            if not self.present:
                return {"ok": False, "error": "overlay not loaded"}
            pk, dr = self.mm.read(REG_ECHO_PEAK), self.mm.read(REG_ECHO_DRIVE)
            en, ctrl = self.mm.read(REG_ECHO_CTRL), self.mm.read(REG_CTRL)
        return {"ok": True, "out": pk & 0xFFFFFF, "out_sat": bool(pk >> 24 & 1), "drive": dr & 0x1FFFFFF,
                "power": bool(en & ECHO_EN), "reverb": bool(en & ECHO_REV), "cab_bypass": bool(ctrl & CTRL_BYPASS)}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            self._serve()
        except OSError:                                 # client went away (e.g. panel closed)
            pass

    def _serve(self):
        echo = self.server.echo
        for line in self.rfile:
            try:
                req = json.loads(line)
                if not isinstance(req, dict):
                    raise ValueError("request must be an object")
                cmd = req.get("cmd")
                if cmd == "get":
                    rep = echo.get()
                elif cmd == "set":
                    rep = echo.set(req)
                elif cmd == "meters":
                    rep = echo.meters()
                else:
                    rep = {"ok": False, "error": "unknown cmd %r" % (cmd,)}
            except ValueError as e:
                rep = {"ok": False, "error": "bad request: %s" % e}
            except Exception as e:                      # keep the connection alive on any bug
                rep = {"ok": False, "error": "internal error: %r" % e}
            try:
                self.wfile.write((json.dumps(rep) + "\n").encode())
                self.wfile.flush()
            except OSError:
                return


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser(description="Space Echo control server (JSON lines over TCP)")
    ap.add_argument("--host", default="0.0.0.0", help="listen address")
    ap.add_argument("--port", type=int, default=5201)
    ap.add_argument("--base", type=lambda x: int(x, 0), default=0x43C00000, help="IP base address")
    ap.add_argument("--state", default=STATE_FILE, help="knob state file")
    ap.add_argument("--fake", action="store_true", help="in-memory fake registers (PC testing)")
    a = ap.parse_args()

    if a.fake:
        mm = FakeMMIO()
    else:
        from pynq import MMIO
        mm = MMIO(a.base, MMIO_RANGE)
    set_hp = None
    if not a.fake:
        from cab_conv import ADAU1761
        codec = []                                   # opened on first use, then kept

        def set_hp(db):
            if not codec:
                codec.append(ADAU1761(bus=1))
            codec[0].set_hp_volume(db)
    echo = Echo(mm, a.state, set_hp)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    srv = Server((a.host, a.port), Handler)
    srv.echo = echo
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("echo server listening on %s:%d%s" % (a.host, a.port, " (fake registers)" if a.fake else ""))
    while not stop.is_set():
        try:
            echo.poll_overlay()
        except Exception as e:
            log("register access failed: %r" % e)
        stop.wait(POLL_S)
    srv.shutdown()
    srv.server_close()
    log("stopped")


if __name__ == "__main__":
    main()
