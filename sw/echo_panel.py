#!/usr/bin/env python3
"""
Roland RE-201 Space Echo style control panel for the cab convolver's tape echo.

    python sw/echo_panel.py --host 192.168.0.31 [--port 5201]
    python sw/echo_panel.py --demo            # in-process fake board server, no hardware needed

Talks JSON lines over TCP to sw/board/echo_server.py (docs/space_echo.md, "Panel and board
server"). The whole front panel is drawn on one tkinter canvas in a normalised coordinate
system, so it scales with the window (aspect ratio 1.9:1, letterboxed).

Controls: drag a knob vertically (Shift = fine), mouse wheel (+-0.25, Shift +-0.05), double-click
resets, right-click types a value. MODE SELECTOR: click a position, drag around the knob or use
the wheel. Keys: Space = POWER, R = REVERB, C = ECHO CANCEL, B = cab bypass, 1-9 0 - = = mode 1..12.
"""

import argparse
import json
import math
import os
import queue
import random
import socket
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# prefer the repo venv (setup_venv.bat / setup_venv.sh) when it exists
_venv = os.path.join(ROOT, ".venv")
_vpy = os.path.join(_venv, "Scripts", "python.exe") if os.name == "nt" else os.path.join(_venv, "bin", "python")
if os.path.exists(_vpy) and os.path.normcase(os.path.abspath(sys.prefix)) != os.path.normcase(_venv):
    sys.exit(subprocess.call([_vpy, os.path.abspath(__file__)] + sys.argv[1:]))

import tkinter as tk  # noqa: E402
import tkinter.font as tkfont  # noqa: E402
from tkinter import simpledialog, ttk  # noqa: E402

sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "board"))
from cab_conv import ECHO_KNOBS, ECHO_MODES  # noqa: E402

PRESET_FILE = os.path.join(HERE, "echo_presets.json")
USER_PRESET_FILE = os.path.expanduser("~/.cab_echo_presets.json")
FULL = float(1 << 23)
ASPECT = 1.9                    # unit width / height

# colours
TOLEX, TOLEX_DARK, TOLEX_LIGHT = "#1e1a19", "#120f0e", "#2c2725"
BG = "#0d0c0c"
ALU_HEAD, ALU_FOOT = "#c8cbce", "#b4b7ba"
BLACK_PANEL, MODE_BOX = "#111111", "#1a1a1a"
WHITE, SILVER = "#ececec", "#8f9396"
LAMP_ON, LAMP_OFF = "#d01a1a", "#4a0d0d"
BOX_GREY = "#d6d8da"


def lerp_col(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(x + (y - x) * t))) for x, y in zip(a, b))


def _arc(cx, cy, rx, ry, a0, a1, n=16):
    """Points on an elliptical arc, angles in degrees (0 = +x, 90 = +y, i.e. down)."""
    return [(cx + rx * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cy + ry * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]



# ----------------------------------------------------------------------
# Presets
# ----------------------------------------------------------------------
def load_presets():
    """-> (builtin dict, user dict); preset = partial knob dict without power / cancel."""
    out = []
    for path in (PRESET_FILE, USER_PRESET_FILE):
        try:
            with open(path) as f:
                d = json.load(f)
            out.append({str(n): {k: v for k, v in p.items() if k in ECHO_KNOBS and k not in ("power", "cancel", "reverb")}
                        for n, p in d.items() if isinstance(p, dict)})
        except (OSError, ValueError):
            out.append({})
    return out[0], out[1]


def save_user_preset(name, knobs):
    try:
        with open(USER_PRESET_FILE) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d[name] = {k: v for k, v in knobs.items() if k not in ("power", "cancel", "reverb")}
    tmp = USER_PRESET_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, USER_PRESET_FILE)


# ----------------------------------------------------------------------
# Networking (background thread, auto-reconnect)
# ----------------------------------------------------------------------
class Net(threading.Thread):
    """Sends knob changes (rate limited, latest value wins), polls meters, reconnects.
    Results go to .events (queue) for the GUI thread:
    ("conn", bool, text), ("state", knobs, cab_bypass), ("meters", reply), ("error", text)."""

    SET_HZ, METER_HZ = 30.0, 15.0

    def __init__(self, host, port):
        super().__init__(daemon=True)
        self.host, self.port = host, port
        self.events = queue.Queue()
        self.lock = threading.Lock()
        self.pending, self.pending_byp = {}, None
        self.wake = threading.Event()
        self.stop_flag = False

    def send_knobs(self, knobs):
        with self.lock:
            self.pending.update(knobs)
        self.wake.set()

    def send_bypass(self, byp):
        with self.lock:
            self.pending_byp = bool(byp)
        self.wake.set()

    def _rpc(self, f, sock, req):
        sock.sendall((json.dumps(req) + "\n").encode())
        line = f.readline()
        if not line:
            raise ConnectionError("connection closed")
        return json.loads(line)

    def run(self):
        while not self.stop_flag:
            try:
                with socket.create_connection((self.host, self.port), timeout=2.0) as sock:
                    sock.settimeout(2.0)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    f = sock.makefile("rb")
                    rep = self._rpc(f, sock, {"cmd": "get"})
                    if not rep.get("ok"):
                        raise ConnectionError(rep.get("error", "get failed"))
                    with self.lock:
                        self.pending.clear()
                        self.pending_byp = None
                    self.events.put(("conn", True, ""))
                    self.events.put(("state", rep["knobs"], rep.get("cab_bypass", False)))
                    last_set, next_meter = 0.0, 0.0
                    while not self.stop_flag:
                        now = time.monotonic()
                        if now - last_set >= 1.0 / self.SET_HZ:
                            with self.lock:
                                kn, byp = self.pending, self.pending_byp
                                self.pending, self.pending_byp = {}, None
                            if kn or byp is not None:
                                req = {"cmd": "set"}
                                if kn:
                                    req["knobs"] = kn
                                if byp is not None:
                                    req["cab_bypass"] = byp
                                rep = self._rpc(f, sock, req)
                                last_set = now
                                if not rep.get("ok"):
                                    self.events.put(("error", rep.get("error", "set failed")))
                        if now >= next_meter:
                            next_meter = now + 1.0 / self.METER_HZ
                            rep = self._rpc(f, sock, {"cmd": "meters"})
                            if rep.get("ok"):
                                self.events.put(("meters", rep))
                            else:
                                self.events.put(("error", rep.get("error", "meters failed")))
                        self.wake.wait(0.01)
                        self.wake.clear()
            except (OSError, ValueError, ConnectionError) as e:
                self.events.put(("conn", False, str(e)))
                t_end = time.monotonic() + 1.0
                while not self.stop_flag and time.monotonic() < t_end:
                    time.sleep(0.05)


# ----------------------------------------------------------------------
# Panel geometry
# ----------------------------------------------------------------------
# small knobs: key -> (x, y, label lines)
KNOBS = {
    "wow": (0.07, 0.55, ("WOW &", "FLUTTER")),
    "tape": (0.165, 0.55, ("TAPE", "AGE")),
    "input": (0.265, 0.55, ("INSTRUMENT", "VOLUME")),
    "bass": (0.61, 0.34, ("BASS",)),
    "treble": (0.715, 0.34, ("TREBLE",)),
    "reverb_volume": (0.815, 0.34, ("REVERB", "VOLUME")),
    "repeat_rate": (0.61, 0.62, ("REPEAT", "RATE")),
    "intensity": (0.715, 0.62, ("INTENSITY",)),
    "echo_volume": (0.815, 0.62, ("ECHO", "VOLUME")),
}
MODE_C = (0.445, 0.47)
R_SMALL, R_MODE = 0.024, 0.045          # chrome cap radius, in face widths
VU_BOX = (0.095, 0.165, 0.235, 0.36)
PEAK_LAMP, PILOT_LAMP, TOGGLE = (0.045, 0.22), (0.905, 0.62), (0.955, 0.62)
REV_LAMP, REV_BTN = (0.905, 0.27), (0.955, 0.27)    # REVERB on/off (not on the original; BTN3 on the board)
SW_BYPASS = (0.20, 0.905)              # instrument-box slide switch centre
SW_OUTPUT = (0.60, 0.905)              # OUTPUT box H/M/L slide switch centre
CANCEL_BOX = (0.74, 0.83, 0.86, 0.97)
VU_AMP_MAX = 10 ** (3 / 20.0)           # +3 VU


class Panel:
    def __init__(self, root, net, host, port, geometry=None):
        self.root, self.net = root, net
        self.host, self.port = host, port
        self.knobs = dict(ECHO_KNOBS)
        self.cab_bypass = False
        self.t_local = {}               # key -> time of the last local change (board state sync)
        self.connected = False
        self.last_error = ""
        self.vu_amp, self.out_target = 0.0, 0.0
        self.peak_until = 0.0
        self.t_prev = time.monotonic()
        self.hover = None               # key under the mouse (tooltip)
        self.drag = None                # dict(kind, key, y0, v0)
        self.hits = []
        self.builtin, self.user = load_presets()

        fams = set(tkfont.families())
        self.family = "Arial Narrow" if "Arial Narrow" in fams else ("Arial" if "Arial" in fams else "Helvetica")
        rng = random.Random(201)
        self.grain = [(rng.random(), rng.random(), rng.choice((1, 1, 2)),
                       rng.choice((TOLEX_DARK, TOLEX_DARK, TOLEX_DARK, TOLEX_LIGHT))) for _ in range(1400)]

        root.title("Space Echo RE-201 panel")
        root.configure(bg=BG)
        root.minsize(900, 520)
        root.geometry(geometry or "1300x690")
        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=0, cursor="arrow")
        self.canvas.pack(fill="both", expand=True)
        self._build_status()

        c = self.canvas
        c.bind("<Configure>", lambda e: self.layout())
        c.bind("<ButtonPress-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_motion)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Double-Button-1>", self.on_double)
        c.bind("<ButtonPress-3>", self.on_right)
        c.bind("<Motion>", self.on_hover)
        c.bind("<Leave>", lambda e: self.set_hover(None))
        c.bind("<MouseWheel>", lambda e: self.on_wheel(e, 1 if e.delta > 0 else -1))
        c.bind("<Button-4>", lambda e: self.on_wheel(e, 1))
        c.bind("<Button-5>", lambda e: self.on_wheel(e, -1))
        root.bind("<Key>", self.on_key)
        root.after(33, self.tick)

    # ---------------- status bar ----------------
    def _build_status(self):
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        bg, fg = "#2a2a2a", "#c4c4c4"
        st.configure("S.TFrame", background=bg)
        st.configure("S.TLabel", background=bg, foreground=fg, font=("Segoe UI", 9))
        st.configure("S.TButton", background="#3a3a3a", foreground=fg, borderwidth=0, padding=(8, 2))
        st.map("S.TButton", background=[("active", "#4a4a4a")])
        st.configure("S.TCombobox", fieldbackground="#3a3a3a", background="#3a3a3a", foreground=fg,
                     arrowcolor=fg, borderwidth=0, selectbackground="#3a3a3a", selectforeground=fg)
        st.map("S.TCombobox", fieldbackground=[("readonly", "#3a3a3a")], foreground=[("readonly", fg)],
               background=[("readonly", "#3a3a3a")], selectbackground=[("readonly", "#3a3a3a")])
        self.root.option_add("*TCombobox*Listbox.background", "#3a3a3a")
        self.root.option_add("*TCombobox*Listbox.foreground", fg)
        bar = ttk.Frame(self.root, style="S.TFrame")
        bar.pack(fill="x", side="bottom")
        self.lb_host = ttk.Label(bar, text="%s:%d" % (self.host, self.port), style="S.TLabel")
        self.lb_host.pack(side="left", padx=(10, 6), pady=4)
        self.lb_state = ttk.Label(bar, text="connecting...", style="S.TLabel", width=14)
        self.lb_state.pack(side="left", padx=4)
        self.preset_var = tk.StringVar()
        self.cb = ttk.Combobox(bar, textvariable=self.preset_var, state="readonly", width=22, style="S.TCombobox")
        self.cb.pack(side="left", padx=(16, 4))
        self.cb.bind("<<ComboboxSelected>>", lambda e: self.apply_preset(self.preset_var.get()))
        ttk.Button(bar, text="Save preset...", style="S.TButton", command=self.save_preset).pack(side="left", padx=4)
        self.lb_err = ttk.Label(bar, text="", style="S.TLabel", foreground="#d98080")
        self.lb_err.pack(side="left", padx=14)
        self.refresh_presets()

    def refresh_presets(self):
        names = list(self.builtin) + [n for n in self.user if n not in self.builtin]
        self.cb["values"] = names
        self.preset_names = names

    def apply_preset(self, name):
        p = self.user.get(name) or self.builtin.get(name)
        if not p:
            return
        p = dict(p)
        if "mode" in p:                         # same rule as turning the MODE SELECTOR
            p["reverb"] = p["mode"] >= 5
        for k, v in p.items():
            self.knobs[k] = v
        self.net.send_knobs(p)
        self.redraw_dynamic()

    def save_preset(self):
        name = simpledialog.askstring("Save preset", "Preset name:", parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        try:
            save_user_preset(name, self.knobs)
        except OSError as e:
            self.set_error("cannot save preset: %s" % e)
            return
        self.builtin, self.user = load_presets()
        self.refresh_presets()
        self.preset_var.set(name)

    def set_error(self, text):
        self.last_error = text
        self.lb_err.configure(text=text)

    # ---------------- model access ----------------
    def set_knob(self, key, val):
        if key in ("power", "cancel", "reverb"):
            val = bool(val)
        elif key == "output":
            val = val if val in ("H", "M", "L") else "H"
        elif key == "mode":
            val = int(min(max(val, 1), 12))
        else:
            val = round(min(max(float(val), 0.0), 10.0), 2)
        if self.knobs[key] == val:
            return
        self.knobs[key] = val
        self.t_local[key] = time.monotonic()
        send = {key: val}
        if key == "mode":                       # the MODE SELECTOR sets reverb per the RE-201 table
            self.knobs["reverb"] = send["reverb"] = val >= 5
            self.t_local["reverb"] = self.t_local[key]
        self.net.send_knobs(send)
        self.redraw_dynamic()

    def set_bypass(self, byp):
        self.cab_bypass = bool(byp)
        self.t_local["byp"] = time.monotonic()
        self.net.send_bypass(self.cab_bypass)
        self.redraw_dynamic()

    # ---------------- periodic ----------------
    def tick(self):
        now = time.monotonic()
        dt, self.t_prev = now - self.t_prev, now
        try:
            while True:
                ev = self.net.events.get_nowait()
                if ev[0] == "conn":
                    self.connected = ev[1]
                    self.lb_state.configure(text="connected" if ev[1] else "offline")
                    if ev[1]:
                        self.set_error("")
                    else:
                        self.out_target = 0.0
                        self.set_error(ev[2])
                    self.redraw_dynamic()
                elif ev[0] == "state":
                    self.knobs.update({k: v for k, v in ev[1].items() if k in ECHO_KNOBS})
                    self.cab_bypass = bool(ev[2])
                    self.redraw_dynamic()
                elif ev[0] == "meters":
                    m = ev[1]
                    # 0 VU = -12 dBFS
                    self.out_target = m["out"] / FULL * 10 ** (12 / 20.0)
                    if m["drive"] > FULL:
                        self.peak_until = now + 0.15
                    # BTN2 (echo on/off) and BTN1 (cab bypass) on the board; a local click wins for 1 s
                    for key in ("power", "reverb"):
                        if key in m and m[key] != self.knobs[key] and now - self.t_local.get(key, 0) > 1.0:
                            self.knobs[key] = m[key]
                            self.redraw_dynamic()
                    if "cab_bypass" in m and m["cab_bypass"] != self.cab_bypass and now - self.t_local.get("byp", 0) > 1.0:
                        self.cab_bypass = m["cab_bypass"]
                        self.redraw_dynamic()
                elif ev[0] == "error":
                    self.set_error(ev[1])
        except queue.Empty:
            pass
        tau = 0.1
        self.vu_amp += (self.out_target - self.vu_amp) * (1 - math.exp(-dt / tau))
        self.redraw_fast()
        self.root.after(33, self.tick)

    # ---------------- layout ----------------
    def layout(self):
        cw, ch = max(self.canvas.winfo_width(), 50), max(self.canvas.winfo_height(), 50)
        W = min(cw, ch * ASPECT)
        H = W / ASPECT
        self.W, self.H = W, H
        self.ox, self.oy = (cw - W) / 2, (ch - H) / 2
        self.fx0, self.fy0 = self.ox + 0.04 * W, self.oy + 0.10 * H
        self.fw, self.fh = 0.92 * W, 0.84 * H
        self.u = self.fw
        self.draw_static()
        self.redraw_dynamic()

    def X(self, x):
        return self.fx0 + x * self.fw

    def Y(self, y):
        return self.fy0 + y * self.fh

    def font(self, k, bold=True):
        px = max(6, round(k * self.u))
        return (self.family, -px, "bold" if bold else "normal")

    # ---------------- drawing primitives ----------------
    def rrect(self, x0, y0, x1, y1, r, **kw):
        pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1,
               x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def brushed(self, x0, y0, x1, y1, base, rng_seed, tags):
        c = self.canvas
        rng = random.Random(rng_seed)
        c.create_rectangle(x0, y0, x1, y1, fill=base, outline="", tags=tags)
        y = y0
        n = max(1.0, y1 - y0)
        while y < y1:
            t = (y - y0) / n
            col = lerp_col(lerp_col(base, "#ffffff", 0.06), lerp_col(base, "#000000", 0.08), t)
            col = lerp_col(col, "#ffffff" if rng.random() < 0.5 else "#000000", rng.random() * 0.035)
            c.create_line(x0, y, x1, y, fill=col, tags=tags)
            y += 2

    def txt(self, x, y, text, k, fill=WHITE, anchor="center", bold=True, angle=0, tags="static"):
        return self.canvas.create_text(x, y, text=text, font=self.font(k, bold), fill=fill, anchor=anchor,
                                       angle=angle, tags=tags, justify="center")

    def circ(self, x, y, r, tags, **kw):
        return self.canvas.create_oval(x - r, y - r, x + r, y + r, tags=tags, **kw)

    def chrome(self, cx, cy, R, tags):
        n = 8
        for i in range(n):
            t = i / (n - 1)
            r = R * (1 - 0.8 * t)
            off = -0.22 * R * t
            self.circ(cx + off, cy + off, r, tags, fill=lerp_col("#6f7377", "#eef0f2", t ** 0.8),
                      outline="#4a4d50" if i == 0 else "")

    def lamp(self, x, y, lit, tags):
        u = self.u
        cx, cy, r = self.X(x), self.Y(y), 0.0105 * u
        self.circ(cx, cy, r * 1.45, tags, fill="#9ea2a6", outline="#3d4043")
        self.circ(cx, cy, r * 1.2, tags, fill="#d9dcdf", outline="#6f7377")
        if lit:
            self.circ(cx, cy, r * 1.9, tags, fill="", outline="#8a1818", width=max(1, round(0.002 * u)))
        self.circ(cx, cy, r, tags, fill=LAMP_ON if lit else LAMP_OFF, outline="#240606")
        self.circ(cx - r * 0.3, cy - r * 0.35, r * 0.3, tags, fill="#ff8a7a" if lit else "#7a3030", outline="")

    def arc_text(self, cx, cy, radius, centre_deg, text, k, fill=WHITE):
        """Text along a circle, centre_deg clockwise from 12 o'clock, upright on the top half."""
        step = 0.95 * self.fp(k) / radius * 180 / math.pi
        a0 = centre_deg - step * (len(text) - 1) / 2
        for i, ch in enumerate(text):
            if ch == " ":
                continue
            a = a0 + i * step
            r = math.radians(a)
            self.txt(cx + radius * math.sin(r), cy - radius * math.cos(r), ch, k, fill, angle=-a)

    def fp(self, k):
        return max(6, round(k * self.u))

    # ---------------- static layer ----------------
    def draw_static(self):
        c = self.canvas
        c.delete("all")
        self.hits = []
        u, W, H = self.u, self.W, self.H
        ox, oy = self.ox, self.oy
        # tolex body
        self.rrect(ox, oy, ox + W, oy + H, 0.03 * H, fill=TOLEX, outline="#0a0908", tags="static")
        fx0, fy0, fx1, fy1 = self.fx0, self.fy0, self.fx0 + self.fw, self.fy0 + self.fh
        for nx, ny, sz, col in self.grain:
            x, y = ox + nx * W, oy + ny * H
            if x < ox + 0.012 * H or y < oy + 0.012 * H or x > ox + W - 0.014 * H or y > oy + H - 0.014 * H:
                continue
            if fx0 - 3 < x < fx1 + 3 and fy0 - 3 < y < fy1 + 3:
                continue
            c.create_rectangle(x, y, x + sz, y + sz, fill=col, outline="", tags="static")
        # face shadow + face
        c.create_rectangle(fx0 - 2, fy0 - 2, fx1 + 2, fy1 + 2, fill="#050505", outline="", tags="static")
        # B) black panel
        c.create_rectangle(fx0, self.Y(0.13), fx1, self.Y(0.80), fill=BLACK_PANEL, outline="", tags="static")
        # A) header
        self.brushed(fx0, fy0, fx1, self.Y(0.13), ALU_HEAD, 1, "static")
        hy = (self.Y(0) + self.Y(0.13)) / 2
        t1 = self.txt(self.X(0.03), hy, "SPACE ECHO", 0.031, "#0a0a0a", "w")
        bb = c.bbox(t1)
        self.txt(bb[2] + 0.012 * u, hy + 0.003 * u, "RE-201", 0.022, "#0a0a0a", "w")
        # in place of the Roland "R": a white "P" over the word "Poland" in red (Polish flag colours)
        lx = self.X(0.935)
        wide = next((f for f in ("Arial Black", "Verdana") if f in tkfont.families()), self.family)
        tb = c.bbox(c.create_text(lx, self.Y(0.105), text="Poland", fill="#a8323a", tags="static",
                                  font=(wide, -max(6, round(0.0085 * u)), "bold")))
        # bold grotesque P outline in a unit box (x 0..0.82, y 0..1), drawn as a 2 px stroke only
        # (transparent interior), a bit narrower than the word and standing just above it
        lw = 0.7 * (tb[2] - tb[0]) / 0.82
        lh = lw * 0.85
        ox, oy = lx - lw * 0.41, tb[1] - 0.004 * u - lh
        outer = ([(0, 0), (0.50, 0)] + _arc(0.50, 0.29, 0.32, 0.29, -90, 90) +
                 [(0.50, 0.58), (0.27, 0.58), (0.27, 1), (0, 1)])
        hole = [(0.27, 0.16), (0.48, 0.16)] + _arc(0.48, 0.29, 0.14, 0.13, -90, 90) + [(0.48, 0.42), (0.27, 0.42)]
        for poly in (outer, hole):
            c.create_polygon([v for x, y in poly for v in (ox + x * lw, oy + y * lh)], fill="",
                             outline="#ffffff", width=2, tags="static")
        c.create_line(fx0, self.Y(0.13), fx1, self.Y(0.13), fill="#050505", width=2, tags="static")

        self.frame(0.02, 0.16, 0.34, 0.78)
        self.frame(0.55, 0.16, 0.875, 0.78)
        # left group: PEAK lamp label, VU meter, knob frames
        self.txt(self.X(PEAK_LAMP[0]), self.Y(0.298), "PEAK", 0.0088)
        self.txt(self.X(PEAK_LAMP[0]), self.Y(0.33), "LEVEL", 0.0088)
        self.draw_vu_static()
        for key in ("wow", "tape", "input"):
            x, y, _ = KNOBS[key]
            self.frame(x - 0.045, 0.40, x + 0.045, 0.70)
        # mode selector box
        x0, y0, x1, y1 = 0.355, 0.16, 0.535, 0.78
        c.create_rectangle(self.X(x0), self.Y(y0), self.X(x1), self.Y(y1), fill=MODE_BOX, outline=SILVER,
                           width=max(1, round(0.0015 * u)), tags="static")
        self.txt(self.X(0.445), self.Y(0.205), "MODE SELECTOR", 0.0105)
        self.draw_mode_static()
        # small knobs: ticks, numerals, labels
        for key, (x, y, label) in KNOBS.items():
            self.draw_knob_static(key, x, y, label)
        self.txt(self.X(0.815) - 0.004 * u, self.Y(0.62) + 0.058 * u, "STRAIGHT", 0.0075, anchor="e")
        self.txt(self.X(0.815) + 0.004 * u, self.Y(0.62) + 0.058 * u, "ECHO", 0.0075, anchor="w")
        # reverb group (lamp + push button), power group
        self.txt(self.X(0.93), self.Y(0.19), "REVERB", 0.0095)
        self.txt(self.X(0.945), self.Y(0.40), "POWER", 0.0095)
        self.txt(TOGGLE[0] * self.fw + self.fx0, self.Y(0.505), "ON", 0.0088)
        self.txt(TOGGLE[0] * self.fw + self.fx0, self.Y(0.735), "OFF", 0.0088)
        self.draw_bottom_static()

    def frame(self, x0, y0, x1, y1):
        self.canvas.create_rectangle(self.X(x0), self.Y(y0), self.X(x1), self.Y(y1), fill="", outline=SILVER,
                                     width=max(1, round(0.0015 * self.u)), tags="static")

    def draw_knob_static(self, key, x, y, label):
        u = self.u
        cx, cy = self.X(x), self.Y(y)
        dim = key == "reverb_volume"
        dotc = "#707070" if dim else WHITE
        for i in range(11):
            a = math.radians(-150 + 30 * i)
            px, py = cx + 0.037 * u * math.sin(a), cy - 0.037 * u * math.cos(a)
            r = 0.0026 * u * (1.5 if i in (0, 10) else 1.0)
            self.circ(px, py, r, "static", fill=dotc, outline="")
        for i, s in ((0, "0"), (10, "10")):
            a = math.radians(-150 + 30 * i)
            self.txt(cx + 0.049 * u * math.sin(a), cy - 0.047 * u * math.cos(a), s, 0.0078, dotc)
        ly = cy - 0.05 * u - 0.0115 * u * len(label)
        if key in ("wow", "tape", "input"):
            ly = self.Y(0.40) + 0.011 * u
        for i, s in enumerate(label):
            self.txt(cx, ly + i * 0.0115 * u, s, 0.0088)
        self.hits.append(("knob", key, cx, cy, 0.04 * u))

    def draw_mode_static(self):
        u = self.u
        cx, cy = self.X(MODE_C[0]), self.Y(MODE_C[1])
        for p in range(1, 13):
            a = math.radians(180 + 30 * p)
            s, co = math.sin(a), -math.cos(a)
            self.circ(cx + 0.059 * u * s, cy + 0.059 * u * co, 0.0028 * u, "static", fill=WHITE, outline="")
            if p < 12:
                self.txt(cx + 0.07 * u * s, cy + 0.07 * u * co, str(p), 0.0092)
        self.txt(cx, cy + 0.0705 * u, "REVERB", 0.0075)
        self.txt(cx, cy + 0.0785 * u, "ONLY", 0.0075)
        self.arc_text(cx, cy, 0.0845 * u, 255, "ECHO", 0.0075)
        self.arc_text(cx, cy, 0.0845 * u, 60, "REVERB ECHO", 0.0075)
        self.hits.append(("mode", "mode", cx, cy, 0.086 * u))

    def draw_vu_static(self):
        c, u = self.canvas, self.u
        x0, y0, x1, y1 = self.X(VU_BOX[0]), self.Y(VU_BOX[1]), self.X(VU_BOX[2]), self.Y(VU_BOX[3])
        self.rrect(x0, y0, x1, y1, 0.004 * u, fill="#050505", outline="#3a3a3a", tags="static")
        m = 0.007 * u
        fx0, fy0, fx1, fy1 = x0 + m, y0 + m, x1 - m, y1 - m
        n = 14
        for i in range(n):
            t = i / (n - 1)
            c.create_rectangle(fx0, fy0 + (fy1 - fy0) * i / n, fx1, fy0 + (fy1 - fy0) * (i + 1) / n + 1,
                               fill=lerp_col("#f6e6b4", "#e4be72", t ** 1.3), outline="", tags="static")
        c.create_rectangle(fx0, fy0, fx1, fy1, fill="", outline="#2a2a2a", tags="static")
        self.vu = dict(px=(fx0 + fx1) / 2, py=fy1 + 0.012 * u, rs=0.07 * u, box=(fx0, fy0, fx1, fy1))
        px, py, rs = self.vu["px"], self.vu["py"], self.vu["rs"]
        ang = lambda amp: -40 + 80 * amp / VU_AMP_MAX
        a0 = ang(1.0)
        w = max(1, round(0.0018 * u))
        c.create_arc(px - rs, py - rs, px + rs, py + rs, start=90 - a0, extent=a0 + 40, style="arc",
                     outline="#161616", width=w, tags="static")
        c.create_arc(px - rs, py - rs, px + rs, py + rs, start=90 - 40, extent=40 - a0, style="arc",
                     outline="#c01818", width=w + 1, tags="static")
        majors = {-20: "20", -10: "10", -7: "7", -5: "5", -3: "3", 0: "0", 1: "1", 2: "2", 3: "3"}
        for db in range(-20, 4):
            a = math.radians(ang(10 ** (db / 20.0)))
            col = "#c01818" if db > 0 else "#161616"
            ln = 0.009 * u if db in majors else 0.005 * u
            c.create_line(px + rs * math.sin(a), py - rs * math.cos(a),
                          px + (rs + ln) * math.sin(a), py - (rs + ln) * math.cos(a), fill=col, width=w, tags="static")
            if db in majors:
                self.txt(px + (rs + 0.0165 * u) * math.sin(a), py - (rs + 0.0165 * u) * math.cos(a),
                         majors[db], 0.0066, col)
        self.txt(px, fy1 - 0.026 * u, "VU", 0.0125, "#161616")
        self.txt(fx1 - 0.004 * u, fy1 - 0.007 * u, "Poland", 0.0058, "#3a3a3a", "se", bold=False)

    def draw_bottom_static(self):
        c, u = self.canvas, self.u
        self.brushed(self.fx0, self.Y(0.80), self.fx0 + self.fw, self.fy0 + self.fh, ALU_FOOT, 2, "static")
        c.create_line(self.fx0, self.Y(0.80), self.fx0 + self.fw, self.Y(0.80), fill="#050505", width=2, tags="static")
        by0, by1 = self.Y(0.83), self.Y(0.97)

        def box(x0, x1, label):
            self.rrect(self.X(x0), by0, self.X(x1), by1, 0.003 * u, fill=BOX_GREY, outline="#4a4d50", tags="static")
            self.txt((self.X(x0) + self.X(x1)) / 2, by0 + 0.0095 * u, label, 0.0072, "#111111")

        jy = self.Y(0.905)
        box(0.14, 0.26, "INSTRUMENT")
        box(0.34, 0.46, "FROM P.A.")
        box(0.54, 0.66, "OUTPUT")
        # H M L slide switch (headphone level, see ECHO_OUTPUT_DB); the knob is in the dynamic layer
        sx, sw = self.X(SW_OUTPUT[0]), 0.021 * u
        for dx, s in ((-1, "H"), (0, "M"), (1, "L")):
            self.txt(sx + dx * sw * 0.8, self.Y(0.955), s, 0.0065, "#111111")
        box(0.74, 0.86, "FOOT SW")
        self.txt(self.X(0.80), self.Y(0.953), "ECHO CANCEL", 0.0062, "#111111")
        # CAB / BYPASS labels of the instrument slide switch
        sw_x = self.X(SW_BYPASS[0])
        self.txt(sw_x - 0.0215 * u, self.Y(0.955), "CAB", 0.0065, "#111111")
        self.txt(sw_x + 0.0215 * u, self.Y(0.955), "BYPASS", 0.0065, "#111111")
        self.hits.extend([("cancel", "cancel", self.X(CANCEL_BOX[0]), self.Y(CANCEL_BOX[1]),
                           self.X(CANCEL_BOX[2]), self.Y(CANCEL_BOX[3])),
                          ("bypass", "bypass", sw_x - 0.032 * u, jy - 0.016 * u, sw_x + 0.032 * u, jy + 0.016 * u),
                          ("output", "output", sx - 0.03 * u, jy - 0.016 * u, sx + 0.03 * u, jy + 0.016 * u),
                          ("reverb", "reverb", self.X(REV_BTN[0]) - 0.016 * u, self.Y(REV_BTN[1]) - 0.016 * u,
                           self.X(REV_BTN[0]) + 0.016 * u, self.Y(REV_BTN[1]) + 0.016 * u),
                          ("power", "power", self.X(TOGGLE[0]) - 0.02 * u, self.Y(0.48),
                           self.X(TOGGLE[0]) + 0.02 * u, self.Y(0.76))])

    # ---------------- dynamic layer ----------------
    def redraw_dynamic(self):
        if not hasattr(self, "vu"):
            return
        c = self.canvas
        c.delete("dyn")
        c.delete("tip")
        u = self.u
        for key, (x, y, _) in KNOBS.items():
            cx, cy = self.X(x), self.Y(y)
            a = math.radians(-150 + 30 * self.knobs[key])
            self.draw_knob(cx, cy, R_SMALL * u, a, "dyn")
        cx, cy = self.X(MODE_C[0]), self.Y(MODE_C[1])
        a = math.radians(180 + 30 * self.knobs["mode"])
        self.draw_knob(cx, cy, R_MODE * u, a, "dyn", fluted=True)
        # REVERB push button: chrome cap in a bezel, drawn pressed in while on
        bx, by = self.X(REV_BTN[0]), self.Y(REV_BTN[1])
        self.circ(bx, by, 0.0135 * u, "dyn", fill="#b9bcbf", outline="#4a4d50")
        self.chrome(bx, by, (0.0085 if self.knobs["reverb"] else 0.0105) * u, "dyn")
        # POWER toggle
        tx, ty = self.X(TOGGLE[0]), self.Y(TOGGLE[1])
        on = self.knobs["power"]
        self.rrect(tx - 0.0135 * u, ty - 0.0135 * u, tx + 0.0135 * u, ty + 0.0135 * u, 0.003 * u, fill="#b9bcbf",
                   outline="#4a4d50", tags="dyn")
        self.circ(tx, ty, 0.0085 * u, "dyn", fill="#8c9094", outline="#3a3d40")
        tip = ty + (-1 if on else 1) * 0.034 * u
        c.create_line(tx, ty, tx, tip, fill="#6f7377", width=max(3, round(0.0075 * u)), capstyle="round", tags="dyn")
        c.create_line(tx - 0.001 * u, ty, tx - 0.001 * u, tip, fill="#e4e6e8", width=max(1, round(0.003 * u)),
                      capstyle="round", tags="dyn")
        self.circ(tx, tip, 0.0072 * u, "dyn", fill="#d5d8db", outline="#5a5d60")
        self.circ(tx - 0.002 * u, tip - 0.002 * u, 0.0026 * u, "dyn", fill="#ffffff", outline="")
        # instrument slide switch: CAB (left) / BYPASS (right)
        sx, sy = self.X(SW_BYPASS[0]), self.Y(SW_BYPASS[1])
        sw, sh = 0.021 * u, 0.0085 * u
        c.create_rectangle(sx - sw, sy - sh, sx + sw, sy + sh, fill="#111111", outline="#000000", tags="dyn")
        kx = sx + (0.011 if self.cab_bypass else -0.011) * u
        c.create_rectangle(kx - 0.005 * u, sy - sh * 0.85, kx + 0.005 * u, sy + sh * 0.85, fill="#d9dcdf",
                           outline="#555555", tags="dyn")
        c.create_line(kx, sy - sh * 0.6, kx, sy + sh * 0.6, fill="#777777", tags="dyn")
        # OUTPUT H / M / L slide switch
        sx, sy = self.X(SW_OUTPUT[0]), self.Y(SW_OUTPUT[1])
        c.create_rectangle(sx - sw, sy - sh, sx + sw, sy + sh, fill="#111111", outline="#000000", tags="dyn")
        kx = sx + {"H": -0.013, "M": 0.0, "L": 0.013}[self.knobs["output"]] * u
        c.create_rectangle(kx - 0.004 * u, sy - sh * 0.85, kx + 0.004 * u, sy + sh * 0.85, fill="#d9dcdf",
                           outline="#555555", tags="dyn")
        c.create_line(kx, sy - sh * 0.6, kx, sy + sh * 0.6, fill="#777777", tags="dyn")
        # ECHO CANCEL indicator
        ix, iy = self.X(0.80), self.Y(0.903)
        r = 0.0045 * u
        if self.knobs["cancel"]:
            self.circ(ix, iy, r * 2.2, "dyn", fill="#e8a8a0", outline="")
            self.circ(ix, iy, r * 1.4, "dyn", fill="#e04030", outline="")
            self.circ(ix, iy, r, "dyn", fill=LAMP_ON, outline="#500000")
        else:
            self.circ(ix, iy, r, "dyn", fill="#8a8a8a", outline="#555555")
        # connection status in the FROM P.A. box
        if self.connected:
            self.txt(self.X(0.40), self.Y(0.915), "● %s" % self.host, 0.0068, "#1f7a2e", tags="dyn")
        else:
            self.txt(self.X(0.40), self.Y(0.915), "○ offline", 0.0068, "#8a4040", tags="dyn")
        self.redraw_fast(force=True)
        self.draw_tip()

    def draw_knob(self, cx, cy, R, a, tags, fluted=False):
        c = self.canvas
        sk = 1.13 * R
        self.circ(cx, cy, sk, tags, fill="#0a0a0a", outline="#000000")
        if fluted:
            for i in range(48):
                t = math.radians(i * 7.5)
                c.create_line(cx + R * 1.01 * math.sin(t), cy - R * 1.01 * math.cos(t),
                              cx + sk * math.sin(t), cy - sk * math.cos(t), fill="#2e2e2e", tags=tags)
        self.chrome(cx, cy, R, tags)
        w = max(2, round(R * (0.16 if not fluted else 0.12)))
        c.create_line(cx, cy, cx + 0.93 * R * math.sin(a), cy - 0.93 * R * math.cos(a), fill="#111111", width=w,
                      capstyle="round", tags=tags)

    def redraw_fast(self, force=False):
        """Lamps and the VU needle (30 Hz)."""
        if not hasattr(self, "vu"):
            return
        now = time.monotonic()
        c = self.canvas
        c.delete("fast")
        u = self.u
        self.lamp(PEAK_LAMP[0], PEAK_LAMP[1], now < self.peak_until, "fast")
        self.lamp(PILOT_LAMP[0], PILOT_LAMP[1], self.knobs["power"], "fast")
        self.lamp(REV_LAMP[0], REV_LAMP[1], self.knobs["reverb"], "fast")
        v = self.vu
        amp = min(self.vu_amp, VU_AMP_MAX * 1.04)
        a = math.radians(-40 + 80 * amp / VU_AMP_MAX)
        ln = v["rs"] + 0.006 * u
        c.create_line(v["px"], v["py"], v["px"] + ln * math.sin(a), v["py"] - ln * math.cos(a), fill="#080808",
                      width=max(1, round(0.0022 * u)), tags="fast")
        fx0, fy0, fx1, fy1 = v["box"]
        c.create_rectangle(fx0 - 1, fy1, fx1 + 1, fy1 + 0.012 * u, fill="#050505", outline="", tags="fast")
        self.circ(v["px"], fy1 + 0.002 * u, 0.0045 * u, "fast", fill="#151515", outline="#2a2a2a")
        c.tag_raise("tip")

    # ---------------- tooltip ----------------
    def draw_tip(self):
        self.canvas.delete("tip")
        key = self.hover or (self.drag and self.drag.get("key"))
        if not key or key not in self.knobs or key in ("power", "cancel", "reverb"):
            return
        u = self.u
        if key == "mode":
            cx, cy, r = self.X(MODE_C[0]), self.Y(MODE_C[1]), 0.1 * u
            text = "%d" % self.knobs["mode"]
        else:
            x, y, _ = KNOBS[key]
            cx, cy, r = self.X(x), self.Y(y), 0.05 * u
            text = "%.1f" % self.knobs[key]
        ty = cy - r - 0.004 * u
        w, h = 0.021 * u, 0.0085 * u
        self.canvas.create_rectangle(cx - w, ty - h, cx + w, ty + h, fill="#161616", outline="#6a6a6a", tags="tip")
        self.txt(cx, ty, text, 0.0095, "#f0f0f0", tags="tip")

    def set_hover(self, key):
        if key != self.hover:
            self.hover = key
            self.draw_tip()

    # ---------------- interaction ----------------
    def hit(self, x, y):
        for h in self.hits:
            if h[0] in ("knob", "mode"):
                if math.hypot(x - h[2], y - h[3]) <= h[4]:
                    return h
            elif h[0] in ("cancel", "bypass", "power", "output", "reverb"):
                if h[2] <= x <= h[4] and h[3] <= y <= h[5]:
                    return h
        return None

    def mode_from_xy(self, x, y):
        cx, cy = self.X(MODE_C[0]), self.Y(MODE_C[1])
        th = math.degrees(math.atan2(x - cx, -(y - cy))) % 360
        p = round(((th - 180) % 360) / 30.0)
        return 12 if p in (0, 12) else p

    def on_press(self, e):
        self.canvas.focus_set()
        h = self.hit(e.x, e.y)
        self.drag = None
        if not h:
            return
        kind = h[0]
        if kind == "knob":
            self.drag = dict(kind="knob", key=h[1], y0=e.y, v0=self.knobs[h[1]])
        elif kind == "mode":
            cx, cy = self.X(MODE_C[0]), self.Y(MODE_C[1])
            self.drag = dict(kind="mode", key="mode")
            if math.hypot(e.x - cx, e.y - cy) > R_MODE * self.u * 0.35:
                self.set_knob("mode", self.mode_from_xy(e.x, e.y))
        elif kind == "power":
            self.set_knob("power", not self.knobs["power"])
        elif kind == "reverb":
            self.set_knob("reverb", not self.knobs["reverb"])
        elif kind == "cancel":
            self.set_knob("cancel", not self.knobs["cancel"])
        elif kind == "bypass":
            self.set_bypass(not self.cab_bypass)
        elif kind == "output":                  # left / middle / right third of the switch
            dx = (e.x - self.X(SW_OUTPUT[0])) / self.u
            self.set_knob("output", "H" if dx < -0.0065 else "L" if dx > 0.0065 else "M")
        self.draw_tip()

    def on_motion(self, e):
        d = self.drag
        if not d:
            return
        if d["kind"] == "knob":
            scale = 10.0 / 250.0 * (0.2 if e.state & 1 else 1.0)
            self.set_knob(d["key"], d["v0"] + (d["y0"] - e.y) * scale)
        elif d["kind"] == "mode":
            cx, cy = self.X(MODE_C[0]), self.Y(MODE_C[1])
            if math.hypot(e.x - cx, e.y - cy) > R_MODE * self.u * 0.35:
                self.set_knob("mode", self.mode_from_xy(e.x, e.y))
        self.draw_tip()

    def on_release(self, e):
        self.drag = None
        self.on_hover(e)

    def on_hover(self, e):
        h = self.hit(e.x, e.y)
        self.set_hover(h[1] if h and h[0] in ("knob", "mode") else None)
        self.canvas.configure(cursor="hand2" if h else "arrow")

    def on_double(self, e):
        h = self.hit(e.x, e.y)
        if h and h[0] == "knob":
            self.set_knob(h[1], ECHO_KNOBS[h[1]])

    def on_wheel(self, e, direction):
        h = self.hit(e.x, e.y)
        if not h:
            return
        if h[0] == "knob":
            self.set_knob(h[1], self.knobs[h[1]] + direction * (0.05 if e.state & 1 else 0.25))
        elif h[0] == "mode":
            self.set_knob("mode", self.knobs["mode"] + direction)
        elif h[0] == "power":
            self.set_knob("power", direction > 0)
        self.draw_tip()

    def on_right(self, e):
        h = self.hit(e.x, e.y)
        if not h or h[0] not in ("knob", "mode"):
            return
        key = h[1]
        if key == "mode":
            v = simpledialog.askinteger("MODE SELECTOR", "Position (1..12):", parent=self.root,
                                        initialvalue=self.knobs["mode"], minvalue=1, maxvalue=12)
        else:
            v = simpledialog.askfloat(key.upper().replace("_", " "), "Value (0..10):", parent=self.root,
                                      initialvalue=self.knobs[key], minvalue=0.0, maxvalue=10.0)
        if v is not None:
            self.set_knob(key, v)

    def on_key(self, e):
        if isinstance(e.widget, (ttk.Combobox, tk.Entry)):
            return
        ch = e.char
        if ch == " ":
            self.set_knob("power", not self.knobs["power"])
        elif ch in ("c", "C"):
            self.set_knob("cancel", not self.knobs["cancel"])
        elif ch in ("r", "R"):
            self.set_knob("reverb", not self.knobs["reverb"])
        elif ch in ("b", "B"):
            self.set_bypass(not self.cab_bypass)
        elif ch and ch in "1234567890-=":
            self.set_knob("mode", {"0": 10, "-": 11, "=": 12}.get(ch) or int(ch))


# ----------------------------------------------------------------------
# Screenshot helper (Windows, no Pillow needed): copies the window rectangle from the screen
# ----------------------------------------------------------------------
PS_SHOT = r"""
Add-Type -AssemblyName System.Drawing
Add-Type @'
using System.Runtime.InteropServices;
public class Dpi { [DllImport("user32.dll")] public static extern bool SetProcessDPIAware(); }
'@
[Dpi]::SetProcessDPIAware() | Out-Null
$bmp = New-Object System.Drawing.Bitmap %d, %d
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen(%d, %d, 0, 0, $bmp.Size)
$bmp.Save('%s', [System.Drawing.Imaging.ImageFormat]::Png)
"""


def screenshot(root, path):
    root.update()
    x, y, w, h = root.winfo_rootx(), root.winfo_rooty(), root.winfo_width(), root.winfo_height()
    ps = PS_SHOT % (w, h, x, y, path.replace("'", "''"))
    subprocess.call(["powershell", "-NoProfile", "-Command", ps])


# ----------------------------------------------------------------------
def start_demo_server():
    import echo_server as es
    import tempfile
    echo = es.Echo(es.FakeMMIO(), os.path.join(tempfile.mkdtemp(prefix="echo_demo_"), "echo_state.json"))
    echo.poll_overlay()
    srv = es.Server(("127.0.0.1", 0), es.Handler)
    srv.echo = echo
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def poll():
        while True:
            time.sleep(1.0)
            echo.poll_overlay()
    threading.Thread(target=poll, daemon=True).start()
    return srv.server_address[1]


def main():
    ap = argparse.ArgumentParser(description="RE-201 style control panel for the Space Echo")
    ap.add_argument("--host", default="192.168.0.31", help="board address")
    ap.add_argument("--port", type=int, default=5201)
    ap.add_argument("--demo", action="store_true", help="in-process fake server (no board needed)")
    ap.add_argument("--geometry", default=None, help="window size, e.g. 1300x690")
    ap.add_argument("--screenshot", default=None, help=argparse.SUPPRESS)
    a = ap.parse_args()

    if a.demo:
        a.host, a.port = "127.0.0.1", start_demo_server()
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    net = Net(a.host, a.port)
    net.start()
    root = tk.Tk()
    panel = Panel(root, net, a.host, a.port, a.geometry)
    if a.screenshot:
        def shot():
            # the demo board has no audio: show a plausible state before the shot
            panel.set_knob("power", True)
            panel.out_target = panel.vu_amp = 0.55
            panel.peak_until = time.monotonic() + 3600
            root.after(400, lambda: (screenshot(root, a.screenshot), root.destroy()))
        root.attributes("-topmost", True)
        root.after(1500, shot)
    try:
        root.mainloop()
    finally:
        net.stop_flag = True


if __name__ == "__main__":
    main()
