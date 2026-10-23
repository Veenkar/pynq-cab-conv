# Sprzętowy symulator paczki gitarowej (IR convolver) na PYNQ-Z2

Splot sygnału z wzmacniacza (przez attenuator/load box) z odpowiedzią impulsową
paczki, liczony w FPGA w dziedzinie czasu. Nie dodaje opóźnienia algorytmicznego:
próbka wyjściowa `y[n]` korzysta już z `x[n]`.

| Parametr | Wartość |
|---|---|
| Długość IR | 8192 tapów = 170,7 ms przy 48 kHz |
| Architektura | 4 tory MAC × 2048 tapów, multipleksowane w czasie |
| Zegar | 122,88 MHz (MCLK kodeka = zegar / 10 = 12,288 MHz) |
| Budżet | 2560 cykli na próbkę, zużyte ok. 2060 |
| Arytmetyka | próbki 24 bit, współczynniki 18 bit, akumulator 48 bit (DSP48) |
| Opóźnienie cyfrowe | 2 próbki (41,7 µs); do tego filtry ADC/DAC kodeka |
| Zmiana IR | dwa banki współczynników, przełączanie bez trzasków |

## Struktura

```
rtl/
  cab_conv_top.v    rejestry AXI4-Lite, wybór wejścia, bypass/mute, mierniki
  fir_engine.v      silnik FIR: tory MAC, linie opóźniające, banki współczynników
  i2s_master.v      I2S master dla ADAU1761 (kodek jako slave)
  clk_fwd.v         wyprowadzenie MCLK przez ODDR
sim/
  tb_cab_conv.v     testbench: master AXI + model kodeka I2S
  run_sim.py        generuje bodźce, uruchamia Icarusa, sprawdza bit w bit
vivado/
  build.tcl         cały projekt + block design + bitstream jednym poleceniem
  ps7_pynq_z2.tcl   konfiguracja PS (DDR, MIO) z base overlay PYNQ
  pynq_z2_audio.xdc piny kodeka
sw/
  cab_conv.py       sterownik: overlay, kodek po I2C, ładowanie IR, model
  example.py        przykład użycia
```

## Jak to działa

**Tor sygnału.** Kodek ADAU1761 na płytce pracuje jako slave. FPGA generuje
MCLK (12,288 MHz = 256 × fs) oraz BCLK i LRCLK z jednego zegara, więc wszystko
jest ściśle synchroniczne. Kodek nie używa swojej PLL, dzięki czemu nie ma
poślizgów próbek. Wejście LINE IN idzie przez wejścia AUX mikserów nagrywania,
a wyjście na gniazdo słuchawkowe (HP+MIC).

**Silnik FIR.** Każdy tor ma własną linię opóźniającą (bufor kołowy 2048 próbek)
i 2 × 2048 współczynników. Przy nowej próbce najstarsza próbka toru k przechodzi
do toru k+1 pod tym samym adresem, więc wszystkie tory czytają pamięć tą samą
sekwencją adresów. MAC jest potokowy (BRAM, rejestry A/B, rejestr M, akumulator
P), czyli mapuje się wprost na DSP48E1. Po 2048 cyklach sumy torów są dodawane,
przesuwane o `shift` i saturowane do 24 bit.

**Skalowanie.** Współczynniki są w formacie block floating point:
`h = hq / 2^shift`. Sterownik dobiera `shift` tak, żeby największy współczynnik
wykorzystał pełne 18 bit, a żaden akumulator nie mógł się przepełnić nawet
przy pełnej skali na wejściu. Każdy bank ma swój `shift`, przełączany razem
ze współczynnikami.

**Podmiana IR.** Sterownik zapisuje współczynniki do nieaktywnego banku, ustawia
jego `shift` i przełącza bit banku w `CTRL`. Silnik przejmuje nowy bank na
początku następnej próbki. Linia opóźniająca jest wspólna, więc nowa IR od
razu ma pełną historię sygnału.

## Weryfikacja

`sim/run_sim.py` generuje syntetyczne IR i sygnał gitarowy, symuluje cały
układ (AXI, I2S, model kodeka) i porównuje każdą próbkę wyjścia z modelem
w Pythonie (`cab_conv.fir_model`). Scenariusz: IR jednostkowa → IR A →
podmiana na IR B w trakcie grania → bypass → mute. Sprawdzane są też
saturacja, flagi przesteru, mierniki szczytowe i to, że MCLK = 256 × fs.

```
python3 sim/run_sim.py          # 4 × 64 tapów, ok. 10 s
python3 sim/run_sim.py --full   # 4 × 2048 tapów, ponad 8192 próbek, kilka minut
```

Oba warianty przechodzą bit w bit (`RESULT: PASS`). Wariant `--full`
potwierdza, że przy 122,88 MHz silnik mieści się w budżecie próbki
(brak flagi overrun) i że przekazywanie próbek między wszystkimi czterema
torami działa.

Wymaga `iverilog`, `numpy` i `scipy`.

## Budowanie bitstreamu

Vivado 2020.2 lub nowsze. Skrypt nie wymaga plików płytki PYNQ-Z2,
bo konfiguracja PS jest w `ps7_pynq_z2.tcl`:

```
cd pynq-cab-conv
vivado -mode batch -source vivado/build.tcl -tclargs 8
```

Na końcu w `build/` będą `cab_conv.bit`, `cab_conv.hwh`, `utilization.rpt`
i `timing.rpt`. Skrypt wypisuje WNS. Jeśli timing nie zostanie spełniony,
przyślij mi `timing.rpt`.

> Tego skryptu nie uruchomiłem, bo nie mam tu Vivado. RTL jest
> zweryfikowany w symulacji, ale pierwsza synteza może wymagać drobnych
> poprawek w TCL (np. nazwy segmentów adresowych w nowszych wersjach).
> Wystarczy wkleić błąd z logu.

Szacunkowe zasoby: 4 DSP48, ok. 16 RAMB36 (ok. 11% Z-7020), kilkaset LUT.

## Uruchomienie na płytce

Skopiuj na PYNQ (np. do `/home/xilinx/jupyter_notebooks/cab_conv/`):
`cab_conv.bit`, `cab_conv.hwh`, `sw/cab_conv.py`, pliki IR (`.wav`).
Potem w Jupyterze:

```python
from cab_conv import CabConv
cab = CabConv("cab_conv.bit")
cab.load_ir("V30_SM57.wav", gain_db=-3)
cab.status()
```

Więcej w `sw/example.py`. Domyślna normalizacja ustawia szczyt charakterystyki
amplitudowej IR na 0 dB, a `gain_db` dodaje od tego zapas.

Wymagania co do pliku IR (`read_wav` / `prepare_ir` w `sw/cab_conv.py`):

| | |
|---|---|
| Format | tylko WAV, nieskompresowany: PCM 8/16/24/32 bit albo float 32/64 bit (ADPCM, µ-law, FLAC, AIFF nie) |
| Częstotliwość próbkowania | dowolna, przepróbkowanie do 48 kHz; 44,1 / 48 / 88,2 / 96 / 192 kHz dokładnie, nietypowe z błędem rzędu ppm |
| Kanały | dowolna liczba, ale są **uśredniane**: IR stereo / z dwóch mikrofonów zostanie zsumowana (możliwy filtr grzebieniowy), lepiej wcześniej wybrać jeden kanał |
| Długość | dowolna; po przepróbkowaniu ucinana do 8192 tapów (170,7 ms) z 5 ms wygaszeniem, cisza na początku (poniżej -60 dB szczytu) jest wycinana |

Sygnał z gitary/wzmacniacza nie ma takich wymagań: wchodzi analogowo na LINE IN
(pełna skala ok. 1 Vrms przy wzmocnieniu AUX 0 dB).

## Podłączenie wzmacniacza: ważne

- **Nigdy nie podłączaj wyjścia głośnikowego wprost do LINE IN.** Wzmacniacz
  50 W na 8 Ω daje ok. 20 Vrms, a pełna skala wejścia kodeka przy wzmocnieniu
  AUX 0 dB to ok. 1 Vrms (wg noty ADAU1761). Potrzebne jest tłumienie rzędu
  30 dB lub więcej.
- **Wzmacniacz lampowy musi mieć obciążenie** (load box lub attenuator
  z obciążeniem). Praca bez obciążenia uszkodzi transformator wyjściowy.
- Najlepiej brać sygnał z wyjścia liniowego attenuatora albo przez DI box
  z wejściem głośnikowym. Separacja galwaniczna (transformator) zabezpiecza
  przed pętlą masy między wzmacniaczem a płytką zasilaną z USB.
- Poziom ustaw tak, żeby przy najmocniejszym graniu `status()` pokazywał
  `peak_in_dbfs` około −6 dBFS i `input_clip=False`. Pomaga w tym
  `aux_gain_db` (−12…+6 dB co 3 dB).
- Wyjście to gniazdo HP+MIC, które ma wzmacniacz słuchawkowy. Można nim
  zasilić słuchawki, wejście liniowe miksera albo aktywną kolumnę FRFR.

## Mapa rejestrów (AXI4-Lite, 64 KiB, 0x43C00000)

| Offset | Nazwa | Opis |
|---|---|---|
| 0x0000 | ID | `0x43414256` ("CABV") |
| 0x0004 | PARAMS | [15:0] liczba tapów, [23:16] log2 tapów na tor, [31:24] tory |
| 0x0008 | CTRL | [0] bypass, [1] mute, [3:2] wejście (0 L, 1 R, 2 (L+R)/2), [4] bank, [5] mokry tylko na L |
| 0x000C | STATUS | [0] aktywny bank, [1] przester wejścia, [2] przester wyjścia, [3] overrun |
| 0x0010 | SHIFT0 | przesunięcie dla banku 0 |
| 0x0014 | SHIFT1 | przesunięcie dla banku 1 |
| 0x0018 | CLEAR | [0] kasuje flagi i mierniki szczytowe |
| 0x001C | SAMPLES | licznik próbek |
| 0x0020 | PEAK_IN | max \|x\| od ostatniego CLEAR |
| 0x0024 | PEAK_OUT | max \|y\| od ostatniego CLEAR |
| 0x8000 + 4i | COEF[i] | tap i (18 bit ze znakiem), zapis do nieaktywnego banku |

## Rozbudowa

- **Dłuższa IR:** `NLANE = 8` w `cab_conv_top.v` daje 16 384 tapów
  (341 ms) kosztem 4 kolejnych DSP i ok. 16 RAMB36. Długość toru (`LW = 11`)
  jest już blisko limitu 2560 cykli na próbkę.
- **Dwie IR jednocześnie** (np. dwa mikrofony albo stereo): drugi
  `fir_engine` z osobnymi współczynnikami na prawy kanał.
- **Wyższe fs:** 96 kHz wymaga zegara 245,76 MHz albo dwa razy więcej torów
  przy tej samej długości IR w milisekundach.
