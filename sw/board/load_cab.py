# Load the overlay (bitstream download), configure the codec and load cab.wav.
#   sudo -i python3 ~xilinx/cab_conv/load_cab.py
import sys, time
sys.path.insert(0, "/home/xilinx/cab_conv")
from cab_conv import CabConv
cab = CabConv("/home/xilinx/cab_conv/cab_conv.bit", hp_volume_db=-20)
print("fs measured %.2f Hz" % cab.measure_fs(1.0))
cab.bypass = False
t0 = time.time()
info = cab.load_ir("/home/xilinx/cab_conv/cab.wav", gain_db=-3)
print("IR loaded in %.2f s:" % (time.time() - t0), info)
cab.clear()
time.sleep(3)
print("STATUS", cab.status())
