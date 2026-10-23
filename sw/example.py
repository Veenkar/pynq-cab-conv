"""
Przykład użycia na PYNQ-Z2 (można wkleić do notebooka Jupyter).

Na płytce w jednym katalogu: cab_conv.bit, cab_conv.hwh, cab_conv.py, IR-y (*.wav).
"""

import time
from cab_conv import CabConv

# wczytuje overlay, konfiguruje kodek: LINE IN 0 dB (~1 Vrms pełnej skali),
# wyjście słuchawkowe 0 dB
cab = CabConv("cab_conv.bit", aux_gain_db=0, hp_volume_db=0)

print("taps:", cab.ntaps, "| fs zmierzone: %.1f Hz" % cab.measure_fs(0.5))

# IR paczki: normalizacja do 0 dB szczytu charakterystyki, -3 dB zapasu
info = cab.load_ir("ir/V30_SM57.wav", gain_db=-3)
print("IR:", info)          # {'taps': ..., 'shift': ..., 'ms': ...}

# poziomy: zagraj chwilę najmocniej jak grasz i sprawdź
cab.clear()
time.sleep(5)
print(cab.status())
# input_clip=True  -> zmniejsz sygnał z attenuatora albo aux_gain_db
# output_clip=True -> zmniejsz gain_db w load_ir

# A/B: lewy kanał przez paczkę, prawy suchy
# cab.set_left_only(True)

# porównanie suchy / mokry
# cab.bypass = True
# cab.bypass = False

# przełączanie IR na żywo (bez trzasków, na granicy próbki)
# cab.load_ir("ir/G12M_MD421.wav", gain_db=-3)

# głośność wyjścia (słuchawki / FRFR): -57..+6 dB
# cab.codec.set_hp_volume(-6)
