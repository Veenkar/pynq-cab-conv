# Sprzętowy symulator paczki gitarowej (IR convolver) na PYNQ-Z2

[English](README.md) | [Polski](README_PL.md)

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
| Zasoby | 4 DSP48, 26 kafelków BRAM (12 na bufor nagrywania), ok. 2% LUT Z-7020; timing spełniony przy 122,88 MHz |

Stan: zweryfikowane bit w bit w symulacji, buduje się bez błędów w Vivado 2026.1
i działa na płytce (PYNQ Linux 3.0). Charakterystyka zmierzona przez interfejs
audio zgadza się z IR w granicach ok. ±0,5 dB od 60 Hz do 5 kHz.

## Struktura

```
rtl/
  cab_conv_top.v    rejestry AXI4-Lite, wybór wejścia, bypass/mute, mierniki
  fir_engine.v      silnik FIR: tory MAC, linie opóźniające, banki współczynników
  i2s_master.v      I2S master dla ADAU1761 (kodek jako slave)
  clk_fwd.v         wyprowadzenie MCLK przez ODDR
  debounce.v        synchronizacja i eliminacja drgań styków przycisków
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
  board/            skrypty pomocnicze na płytkę (ładowanie, mierniki, sterowanie, nagrywanie, wyświetlacz OLED)
```

## Jak to działa

Ilustrowany opis (tapy, tory, potok DSP48, budżet cykli i I2S, każdy z
fragmentem RTL) jest w [`docs/how_it_works.html`](docs/how_it_works.html)
(po angielsku); pobierz plik i otwórz go w przeglądarce.

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

Wymaga `iverilog` (Icarus) oraz `numpy` i `scipy` z `requirements.txt`.
Raz trzeba założyć lokalny venv z Pythona dostępnego w PATH; `run_sim.py` sam
uruchamia się ponownie w `.venv`, jeśli ten istnieje:

```
.\setup_venv.bat               # Windows (--reinstall po zmianie Pythona)
./setup_venv.sh                # Linux / macOS
```

Skrypty same odtwarzają `.venv`, gdy wersja Pythona w PATH się zmieniła.

## Budowanie bitstreamu

Sprawdzone z Vivado 2026.1 i darmową licencją **Basic**. Poziom Basic nie
obejmuje wydań pośrednich: 2026.1.1 się nie uruchamia, 2026.1 działa. Jeśli
Vivado nie znajduje licencji, ustaw `XILINXD_LICENSE_FILE` na ścieżkę do pliku
`.lic`. Pliki płytki PYNQ-Z2 nie są potrzebne, bo konfiguracja PS jest
w `ps7_pynq_z2.tcl`:

```
cd pynq-cab-conv
vivado -mode batch -source vivado/build.tcl -tclargs 8
```

Na końcu w `build/` będą `cab_conv.bit`, `cab_conv.hwh`, `utilization.rpt`
i `timing.rpt`. Skrypt wypisuje WNS.

## Uruchomienie na płytce

Skopiuj na PYNQ (np. do `/home/xilinx/cab_conv/`): `cab_conv.bit`,
`cab_conv.hwh`, `sw/cab_conv.py`, skrypty z `sw/board/` i pliki IR (`.wav`).
Potem w Jupyterze:

```python
from cab_conv import CabConv
cab = CabConv("cab_conv.bit")
cab.load_ir("V30_SM57.wav", gain_db=-3)
cab.status()
```

Z powłoki sterownik trzeba uruchamiać jako root przez powłokę logowania, bo na
PYNQ 3.0 to ona ustawia XRT i środowisko Pythona (zwykłe `sudo python3` kończy
się błędem "No Devices Found"):

```
sudo -i python3 ~xilinx/cab_conv/load_cab.py       # overlay + kodek + cab.wav
sudo -i python3 ~xilinx/cab_conv/load_ir.py ir.wav 3  # zmiana IR (+3 dB), overlay zostaje
sudo -i python3 ~xilinx/cab_conv/meter.py          # mierniki na żywo, Ctrl+C kończy
sudo -i python3 ~xilinx/cab_conv/ctl.py hp -6      # głośność słuchawek / fpga_mute 0|1
sudo -i python3 ~xilinx/cab_conv/record.py --once  # nagraj jedno ujęcie (patrz Nagrywanie)
```

Żeby konwolwer startował sam po włączeniu płytki, uruchom na płytce skrypt
instalacyjny z `sw/board/`. Instaluje on `cab-conv.service`, która uruchamia
`autostart.py` po PYNQ-owym `bootpy.service`; IR i poziomy zmienia się
w `ExecStart` w `/etc/systemd/system/cab-conv.service`.

```
cd ~xilinx/cab_conv
sudo ./install_autostart.sh            # sam autostart (IR gotowa ok. 160 s po włączeniu)
sudo ./install_autostart.sh --fast     # + szybszy start (IR gotowa ok. 85 s)
sudo ./install_autostart.sh --rec      # + record.service (nagrywanie BTN0); razem: --fast --rec
sudo ./install_autostart.sh --oled     # + status.service (wyświetlacz OLED, patrz Wyświetlacz stanu)
sudo ./install_autostart.sh --restore  # cofa wszystko, ustawienia PYNQ jak w obrazie
```

`--fast` zmienia obraz PYNQ: `/boot/boot.py` nie ładuje już `base.bit` i nie
miga diodami (oryginał zostaje jako `/boot/boot.py.orig`), a `jupyter.service`
i `clear_pl_statefile.service` są wyłączone (ta druga ma
`Requires=jupyter.service`). Jupyter na porcie 9090 znika, SSH zostaje.
Start był wolny głównie dlatego, że naraz ruszały trzy procesy Pythona
importujące `pynq` (ok. 10 s każdy na Cortex-A9 650 MHz), a nie przez kartę SD.

Kolejne ~38 s zabiera wbudowany Ethernet: sterownik `macb` w jądrze zawiesza
się przy starcie na skanowaniu PHY po MDIO. Jeśli używasz tylko Wi-Fi, można
go wyłączyć (zostaje po restarcie, działa od następnego bootu):

```
sudo ./eth-disable.sh    # dodaje initcall_blacklist=macb_driver_init przez /boot/uEnv.txt
sudo ./eth-enable.sh     # cofa zmianę
```

Z `--fast` i wyłączonym Ethernetem IR jest gotowa ok. 49 s po włączeniu
(jądro 2,8 s zamiast ok. 41 s). Gdyby płytka po tym nie wstała, usuń
`uEnv.txt` z partycji FAT karty SD na PC.

Uwaga: `CabConv(...)` domyślnie wgrywa bitstream, co kasuje załadowaną IR.
Żeby podłączyć się do działającego overlayu, użyj `download=False`. Przycisk
PROG i odłączenie zasilania też czyszczą FPGA. Start `pynq` na płytce trwa
ok. 10 s.

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

Przyciski i diody na płytce (obsługiwane w FPGA, więc działają bez żadnego
skryptu; program widzi i może zmieniać te same bity `CTRL`):

| Przycisk | Działanie | Dioda |
|---|---|---|
| BTN0 | włącza/wyłącza nagrywanie (`CTRL[6]`, zob. [Nagrywanie](#nagrywanie)) | LED0 miga (ok. 1,8 Hz) podczas nagrywania |
| BTN1 | włącza/wyłącza bypass (`CTRL[0]`): sygnał suchy zamiast kolumny | LED1 świeci, gdy kolumna działa (bypass wyłączony, stan po włączeniu) |

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

Praktyczne uwagi z testów z interfejsem audio:

- **Pętla masy:** jeśli USB płytki i interfejs audio są podłączone do tego
  samego komputera, na LINE IN pojawia się przydźwięk ok. −52 dBFS. Zasil
  płytkę osobno (np. z ładowarki, dostęp przez Wi-Fi albo Ethernet), a
  przydźwięk znika.
- **Ustawienie poziomów:** utrzymuj wyjście FPGA w okolicy −3 dBFS szczytowo
  (np. `load_ir(..., gain_db=+3)` przy −5 dBFS na wejściu) i głośność
  słuchawek 0 dB, a wzmocnienie wejścia interfejsu ustaw tak, żeby nagranie
  miało szczyty ok. −12 dBFS. Pozostały szum pochodzi z analogowego wyjścia
  płytki, więc głośniejsze wyjście cyfrowe daje lepszy stosunek sygnału do szumu.

## Mapa rejestrów (AXI4-Lite, 128 KiB, 0x43C00000)

| Offset | Nazwa | Opis |
|---|---|---|
| 0x0000 | ID | `0x43414256` ("CABV") |
| 0x0004 | PARAMS | [15:0] liczba tapów, [23:16] log2 tapów na tor, [31:24] tory |
| 0x0008 | CTRL | [0] bypass (BTN1 przełącza), [1] mute, [3:2] wejście (0 L, 1 R, 2 (L+R)/2), [4] bank, [5] mokry tylko na L, [6] REC (nagrywanie; BTN0 przełącza) |
| 0x000C | STATUS | [0] aktywny bank, [1] przester wejścia, [2] przester wyjścia, [3] overrun |
| 0x0010 | SHIFT0 | przesunięcie dla banku 0 |
| 0x0014 | SHIFT1 | przesunięcie dla banku 1 |
| 0x0018 | CLEAR | [0] kasuje flagi i mierniki szczytowe |
| 0x001C | SAMPLES | licznik próbek |
| 0x0020 | PEAK_IN | max \|x\| od ostatniego CLEAR |
| 0x0024 | PEAK_OUT | max \|y\| od ostatniego CLEAR |
| 0x0028 | REC_WPTR | liczba klatek zapisanych od ostatniego wzrostu REC (tylko odczyt, 32 bity, zerowana zboczem narastającym REC) |
| 0x8000 + 4i | COEF[i] | tap i (18 bit ze znakiem), zapis do nieaktywnego banku |
| 0x10000 + 4i | CAPTURE[i] | bufor kołowy 8192 klatek (170,7 ms), tylko odczyt; słowo 2k = suchy x, 2k+1 = mokry y klatki k, 24 bity ze znakiem rozszerzone do 32; klatka n leży pod k = n mod 8192 |

## Nagrywanie

Płytka potrafi nagrać suchy sygnał wejściowy i sygnał po konwolucji jako dwa
monofoniczne pliki WAV 24 bit / 48 kHz na karcie SD, np. do porównań albo dalszej obróbki offline. Bez DMA: PL zapisuje oba
sygnały do bufora kołowego 8192 klatek (170,7 ms), dopóki ustawiony jest
`CTRL[6]` (REC), a PS opróżnia go przez łącze AXI4-Lite
(`CabConv.read_capture`). Sygnał suchy to wejście silnika po wyborze wejścia,
mokry to wyjście silnika (przed bypass/mute); oba są zgrane na ten sam numer
próbki, więc między plikami zachodzi dokładnie `mokry = IR * suchy`.

REC włącza się programowo (`cab.rec_start()` / `cab.rec_stop()` albo opcja
`--start` w `record.py`) lub naciśnięciem **BTN0** (z odbijaniem styków,
przełącza). **LED0** miga, dopóki REC jest włączony. PS czyta bufor co ok. 20 ms, więc
przepełnienie grozi dopiero, gdy proces zostanie zagłodzony na ponad ~170 ms;
przepełnienie jest zgłaszane, a utracone klatki wypełniane zerami.

```
sudo -i python3 ~xilinx/cab_conv/record.py              # demon: para suchy + mokry na okres REC
sudo -i python3 ~xilinx/cab_conv/record.py --once       # czeka na REC, nagrywa jedno ujęcie, kończy
sudo -i python3 ~xilinx/cab_conv/record.py --once --start   # start od razu, stop BTN0 lub Ctrl+C
sudo -i python3 ~xilinx/cab_conv/record.py --dir /media/usb # inny katalog docelowy
```

Każde ujęcie to para `rec_YYYYmmdd_HHMMSS_dry.wav` + `rec_YYYYmmdd_HHMMSS_wet.wav`
w `~xilinx/cab_conv/rec/`, zgranych co do próbki i równej długości (plik suchy
można od razu wrzucić do DAW do reampingu). Są zapisywane na bieżąco, a nagłówki
są poprawiane co 2 s, więc po utracie
zasilania przepada najwyżej kilka ostatnich sekund. Na stałe: `install_autostart.sh
--rec` instaluje `record.service` (po `cab-conv.service`), a wtedy BTN0 w każdej
chwili startuje i kończy ujęcie. Każdy plik mono 24 bit rośnie o ok. 8,6 MB na minutę.

## Wyświetlacz stanu (OLED)

Opcjonalny moduł OLED 0,91" z SSD1306 (128×32, adres I2C 0x3C) pokazuje stan
konwolwera. Jest podłączony do PS I2C0, który bitstream wyprowadza na piny
SCL/SDA złącza Arduino (piny FPGA P15 / P16), czyli na płytce to
`/dev/i2c-0` (kodek używa `/dev/i2c-1`). Cztery przewody żeńsko-żeńskie:

| OLED | PYNQ-Z2 (złącze Arduino) |
|------|--------------------------|
| GND | GND |
| VCC | 3.3V lub 5V (złącze zasilania), zob. niżej |
| SCL | SCL (P15) |
| SDA | SDA (P16) |

VCC: 3.3V działa zawsze i jest zalecane; na testowanym module 5V nie dało
wyraźnie jaśniejszego obrazu. 5V jest bezpieczne tylko wtedy, gdy rezystory podciągające SCL/SDA modułu idą do jego
własnego stabilizatora 3,3 V, a nie do VCC: zasil moduł z 5V przy
**odłączonych** SCL/SDA i zmierz SCL oraz SDA względem GND. 3,3 V oznacza, że
5V jest w porządku; 5 V oznacza: zostań przy 3.3V (piny FPGA nie tolerują
5 V). Testowany moduł pokazuje 3,3 V.

Okablowanie zmieniaj przy wyłączonej płytce. Jeśli wyświetlacz straci zasilanie
w trakcie transmisji, magistrala I2C0 może się zablokować (`EAGAIN` / timeouty
nawet bez niczego podłączonego) aż do wyłączenia i włączenia płytki.

```
CAB                  REC 01:23     tryb (CAB / BYPASS, + MUTE), REC z migającą kropką
V30_SM57_blend.wav                 nazwa IR (current_ir.txt)
IN [#####..........:...]  -14dB    szczyt wejścia, etykieta odwrócona przy przesterowaniu
OT [##########.....:...]   -6dB    szczyt wyjścia (pasek -60..0 dBFS, kreska przy -12 dB)
```

```
sudo -i python3 ~xilinx/cab_conv/oled.py test      # obraz testowy na 5 s
sudo -i python3 ~xilinx/cab_conv/status.py         # demon wyświetlacza (8 Hz)
sudo ./install_autostart.sh --oled                 # status.service przy starcie; razem: --fast --rec --oled
```

`status.py` tylko czyta rejestry (bez przeładowania overlayu) i do czasu
załadowania overlayu pokazuje "no overlay". Gdy brak OLED-a, loguje błąd I2C
i ponawia próby z rosnącym opóźnieniem (od 1 s do 30 s). Opcje: `--bus`,
`--addr`, `--rate`, `--contrast`, `--flip` (obrót o 180°). Podobnie jak
`meter.py` zeruje rejestry szczytów przy każdym odświeżeniu, więc nie uruchamiaj
ich jednocześnie. Nazwa IR jest czytana z `~xilinx/cab_conv/current_ir.txt`,
który zapisują `autostart.py` i `load_ir.py` przy ładowaniu IR.

## Rozbudowa

- **Dłuższa IR:** `NLANE = 8` w `cab_conv_top.v` daje 16 384 tapów
  (341 ms) kosztem 4 kolejnych DSP i ok. 14 kafelków BRAM. Długość toru
  (`LW = 11`) jest już blisko limitu 2560 cykli na próbkę. Trzeba też zmienić
  mapę rejestrów: okno współczynników od `0x8000` mieści tylko 8192 tapy
  w zakresie 0x8000-0xFFFF (okno AXI ma teraz 128 KiB, ale bufor nagrywania
  leży od `0x10000`).
- **Dwie IR jednocześnie** (np. dwa mikrofony albo stereo): drugi
  `fir_engine` z osobnymi współczynnikami na prawy kanał.
- **Wyższe fs:** 96 kHz wymaga zegara 245,76 MHz albo dwa razy więcej torów
  przy tej samej długości IR w milisekundach.

## Licencja

GPL-3.0, pełny tekst w `LICENSE`. Wyjątek: `vivado/ps7_pynq_z2.tcl` pochodzi
z PYNQ v2.7.0 (Copyright (c) 2018, Xilinx) i jest na licencji BSD-3-Clause,
tekst w `vivado/LICENSE.PYNQ`.
