# Boot-time start: load the overlay, configure the codec and load an IR.
# Run by cab-conv.service after bootpy.service (which loads base.bit first).
#   autostart.py <ir.wav> [--gain-db G] [--hp-db H] [--aux-db A]
import argparse, os, sys
sys.path.insert(0, "/home/xilinx/cab_conv")
from cab_conv import CabConv

ap = argparse.ArgumentParser()
ap.add_argument("ir")
ap.add_argument("--gain-db", type=float, default=-3.0, help="IR gain relative to a 0 dB response peak")
ap.add_argument("--hp-db", type=float, default=-20.0, help="headphone volume, -57..+6 dB")
ap.add_argument("--aux-db", type=float, default=0.0, help="LINE IN gain, -12..+6 dB in 3 dB steps")
a = ap.parse_args()

cab = CabConv("/home/xilinx/cab_conv/cab_conv.bit", aux_gain_db=a.aux_db, hp_volume_db=a.hp_db)
info = cab.load_ir(a.ir, gain_db=a.gain_db)
print("cab_conv: %s loaded (%d taps, shift %d), gain %+.1f dB, HP %+.1f dB, LINE IN %+.1f dB"
      % (a.ir, info["taps"], info["shift"], a.gain_db, a.hp_db, a.aux_db), flush=True)
with open("/home/xilinx/cab_conv/current_ir.txt", "w") as f:      # shown by status.py (OLED)
    f.write(os.path.basename(a.ir) + "\n")
