# Load an IR into the running overlay (no bitstream reload).  load_ir.py <wav> <gain_db> [hp_db]
import sys
sys.path.insert(0, "/home/xilinx/cab_conv")
from cab_conv import CabConv
cab = CabConv("/home/xilinx/cab_conv/cab_conv.bit", download=False,
              hp_volume_db=float(sys.argv[3]) if len(sys.argv) > 3 else 0)
print(cab.load_ir(sys.argv[1], gain_db=float(sys.argv[2])))
print(cab.status())
