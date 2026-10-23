# Small runtime control without reloading the overlay (IR stays loaded).
#   ctl.py fpga_mute 0|1     ctl.py hp <dB>|mute     ctl.py show
import sys
sys.path.insert(0, "/home/xilinx/cab_conv")
from pynq import MMIO
from cab_conv import ADAU1761, REG_CTRL
mm = MMIO(0x43C00000, 0x10000)
assert mm.read(0x00) == 0x43414256, "cab_conv overlay not loaded"
cmd = sys.argv[1]
if cmd == "fpga_mute":
    c = mm.read(REG_CTRL)
    mm.write(REG_CTRL, (c | 2) if sys.argv[2] == "1" else (c & ~2))
elif cmd == "hp":
    ADAU1761(bus=1).set_hp_volume(None if sys.argv[2] == "mute" else float(sys.argv[2]))
codec = ADAU1761(bus=1)
regs = {r: codec.read(r) for r in (0x4000, 0x400A, 0x400B, 0x400C, 0x400D, 0x401C, 0x401D, 0x401E, 0x401F, 0x4023, 0x4024)}
print("CTRL=0x%02x" % mm.read(REG_CTRL), " ".join("R%04X=%02X" % kv for kv in regs.items()))
