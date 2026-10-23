"""
Usage example on the PYNQ-Z2 (can be pasted into a Jupyter notebook).

On the board, in one directory: cab_conv.bit, cab_conv.hwh, cab_conv.py, IRs (*.wav).
"""

import time
from cab_conv import CabConv

# loads the overlay, configures the codec: LINE IN 0 dB (~1 Vrms full scale),
# headphone output 0 dB
cab = CabConv("cab_conv.bit", aux_gain_db=0, hp_volume_db=0)

print("taps:", cab.ntaps, "| measured fs: %.1f Hz" % cab.measure_fs(0.5))

# cab IR: normalized to a 0 dB response peak, -3 dB headroom
info = cab.load_ir("ir/V30_SM57.wav", gain_db=-3)
print("IR:", info)          # {'taps': ..., 'shift': ..., 'ms': ...}

# levels: play as hard as you play for a moment, then check
cab.clear()
time.sleep(5)
print(cab.status())
# input_clip=True  -> lower the signal from the attenuator, or aux_gain_db
# output_clip=True -> lower gain_db in load_ir

# A/B: left channel through the cab, right dry
# cab.set_left_only(True)

# dry / wet comparison
# cab.bypass = True
# cab.bypass = False

# switching IRs live (click-free, on a sample boundary)
# cab.load_ir("ir/G12M_MD421.wav", gain_db=-3)

# output volume (headphones / FRFR): -57..+6 dB
# cab.codec.set_hp_volume(-6)
