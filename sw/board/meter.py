# Live level meter: reads cab_conv registers only (does not reload the overlay or touch the IR).
#   sudo -i python3 ~xilinx/cab_conv/meter.py [seconds]      Ctrl+C to stop
import math, sys, time
from pynq import MMIO
REG_STATUS, REG_CLEAR, REG_PEAK_IN, REG_PEAK_OUT = 0x0C, 0x18, 0x20, 0x24
mm = MMIO(0x43C00000, 0x10000)
if mm.read(0x00) != 0x43414256:
    sys.exit("cab_conv overlay not loaded")
def db(v):
    return 20 * math.log10(v / 8388607.0) if v else -999.0
def bar(d):
    n = max(0, min(40, int((d + 60) * 40 / 60)))
    return "#" * n + "." * (40 - n)
dur = float(sys.argv[1]) if len(sys.argv) > 1 else 1e9
t_end = time.time() + dur
try:
    while time.time() < t_end:
        mm.write(REG_CLEAR, 1)
        time.sleep(0.2)
        st = mm.read(REG_STATUS)
        i, o = db(mm.read(REG_PEAK_IN)), db(mm.read(REG_PEAK_OUT))
        flags = ("IN-CLIP " if st & 2 else "") + ("OUT-CLIP " if st & 4 else "") + ("OVERRUN" if st & 8 else "")
        print("in %6.1f dBFS |%s|  out %6.1f dBFS |%s| %s" % (i, bar(i), o, bar(o), flags), flush=True)
except KeyboardInterrupt:
    pass
