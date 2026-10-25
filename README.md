# Hardware guitar cabinet simulator (IR convolver) for PYNQ-Z2

[English](README.md) | [Polski](README_PL.md)

Convolves the signal from a guitar amp (via an attenuator / load box) with the
impulse response of a speaker cabinet, computed in the FPGA in the time domain.
No algorithmic latency: output sample `y[n]` already uses `x[n]`.

| Parameter | Value |
|---|---|
| IR length | 8192 taps = 170.7 ms at 48 kHz |
| Architecture | 4 MAC lanes × 2048 taps, time-multiplexed |
| Clock | 122.88 MHz (codec MCLK = clock / 10 = 12.288 MHz) |
| Budget | 2560 cycles per sample, about 2060 used |
| Arithmetic | 24-bit samples, 18-bit coefficients, 48-bit accumulator (DSP48) |
| Digital latency | 2 samples (41.7 µs), plus the codec's ADC/DAC filters |
| IR change | two coefficient banks, click-free switching |
| Resources | 4 DSP48, 26 BRAM tiles (12 for the recording ring), ~2% LUT of the Z-7020; timing met at 122.88 MHz |

Status: verified bit-exact in simulation, builds cleanly in Vivado 2026.1 and
runs on the board (PYNQ Linux 3.0). Measured through an audio interface, the
response matches the IR within about ±0.5 dB from 60 Hz to 5 kHz.

## Layout

```
rtl/
  cab_conv_top.v    AXI4-Lite registers, input select, bypass/mute, meters
  fir_engine.v      FIR engine: MAC lanes, delay lines, coefficient banks
  i2s_master.v      I2S master for the ADAU1761 (codec is the slave)
  clk_fwd.v         MCLK forwarded through an ODDR
  debounce.v        push-button synchroniser + debounce
  level_led.v       level envelope -> PWM LED brightness
sim/
  tb_cab_conv.v     testbench: AXI master + I2S codec model
  run_sim.py        generates stimulus, runs Icarus, checks bit-exact
vivado/
  build.tcl         whole project + block design + bitstream in one command
  ps7_pynq_z2.tcl   PS configuration (DDR, MIO) from the PYNQ base overlay
  pynq_z2_audio.xdc codec pins
sw/
  cab_conv.py       driver: overlay, codec over I2C, IR loading, model
  example.py        usage example
  board/            helper scripts for the board (load, meter, control, record, OLED status)
```

## How it works

An illustrated walk-through (taps, lanes, the DSP48 pipeline, the cycle
budget and I2S, each with its RTL excerpt) is in
[`docs/how_it_works.html`](docs/how_it_works.html); download it and open it in
a browser.

**Signal path.** The ADAU1761 codec on the board runs as a slave. The FPGA
generates MCLK (12.288 MHz = 256 × fs) as well as BCLK and LRCLK from one
clock, so everything is strictly synchronous. The codec doesn't use its PLL,
so there are no sample slips. LINE IN goes through the AUX inputs of the
record mixers, and the output goes to the headphone jack (HP+MIC).

**FIR engine.** Each lane has its own delay line (a circular buffer of 2048
samples) and 2 × 2048 coefficients. On a new sample, the oldest sample of lane
k moves to lane k+1 at the same address, so all lanes read memory with the
same address sequence. The MAC is pipelined (BRAM, A/B registers, M register,
P accumulator), so it maps directly onto a DSP48E1. After 2048 cycles the lane
sums are added, shifted by `shift` and saturated to 24 bits.

**Scaling.** Coefficients are block floating point: `h = hq / 2^shift`. The
driver picks `shift` so the largest coefficient uses the full 18 bits and no
accumulator can overflow, even at full-scale input. Each bank has its own
`shift`, switched together with the coefficients.

**IR change.** The driver writes the coefficients into the inactive bank, sets
its `shift` and flips the bank bit in `CTRL`. The engine takes over the new
bank at the start of the next sample. The delay line is shared, so the new IR
has the full signal history right away.

## Verification

`sim/run_sim.py` generates synthetic IRs and a guitar-like signal, simulates
the whole design (AXI, I2S, codec model) and compares every output sample with
the Python model (`cab_conv.fir_model`). Scenario: unity IR → IR A → switch to
IR B while playing → bypass → mute. It also checks saturation, the clip flags,
the peak meters and that MCLK = 256 × fs.

```
python3 sim/run_sim.py          # 4 × 64 taps, about 10 s
python3 sim/run_sim.py --full   # 4 × 2048 taps, over 8192 samples, a few minutes
```

Both pass bit-exact (`RESULT: PASS`). `--full` confirms that at 122.88 MHz the
engine fits the per-sample budget (no overrun flag) and that sample hand-over
works across all four lanes.

Requires `iverilog` (Icarus) plus `numpy` and `scipy` from `requirements.txt`.
Set up a local venv from the Python on PATH once; `run_sim.py` re-runs itself
inside `.venv` when it exists:

```
.\setup_venv.bat               # Windows (--reinstall after a Python upgrade)
./setup_venv.sh                # Linux / macOS
```

The scripts also recreate `.venv` on their own when the Python version on PATH
has changed.

## Building the bitstream

Tested with Vivado 2026.1 and the free **Basic** license. The Basic tier does
not cover minor releases: 2026.1.1 refuses to start, 2026.1 works. If Vivado
doesn't find the license, set `XILINXD_LICENSE_FILE` to the path of the `.lic`
file. No PYNQ-Z2 board files are needed, since the PS configuration is in
`ps7_pynq_z2.tcl`:

```
cd pynq-cab-conv
vivado -mode batch -source vivado/build.tcl -tclargs 8
```

At the end, `build/` contains `cab_conv.bit`, `cab_conv.hwh`,
`utilization.rpt` and `timing.rpt`. The script prints the WNS.

## Running on the board

Copy to the PYNQ (e.g. into `/home/xilinx/cab_conv/`): `cab_conv.bit`,
`cab_conv.hwh`, `sw/cab_conv.py`, the scripts from `sw/board/` and your IR
files (`.wav`). Then, in Jupyter:

```python
from cab_conv import CabConv
cab = CabConv("cab_conv.bit")
cab.load_ir("V30_SM57.wav", gain_db=-3)
cab.status()
```

From a shell, the driver has to run as root through a login shell, because on
PYNQ 3.0 that is what sets up XRT and the Python environment (plain
`sudo python3` fails with "No Devices Found"):

```
sudo -i python3 ~xilinx/cab_conv/load_cab.py       # overlay + codec + cab.wav
sudo -i python3 ~xilinx/cab_conv/load_ir.py ir.wav 3  # change IR (+3 dB), overlay stays
sudo -i python3 ~xilinx/cab_conv/meter.py          # live meters, Ctrl+C to stop
sudo -i python3 ~xilinx/cab_conv/ctl.py hp -6      # headphone volume / fpga_mute 0|1
sudo -i python3 ~xilinx/cab_conv/record.py --once  # record one take (see Recording)
```

To start the convolver automatically at boot, run the install script from
`sw/board/` on the board. It installs `cab-conv.service`, which runs
`autostart.py` after PYNQ's own `bootpy.service`; edit `ExecStart` in
`/etc/systemd/system/cab-conv.service` to change the IR or levels.

```
cd ~xilinx/cab_conv
sudo ./install_autostart.sh            # autostart + BTN0 recorder (IR ready ~160 s after power-on)
sudo ./install_autostart.sh --fast     # + faster boot (IR ready ~85 s, ~50 s with eth-disable.sh)
sudo ./install_autostart.sh --no-rec   # without record.service (BTN0 then only toggles REC/LED0)
sudo ./install_autostart.sh --no-echo   # without echo.service (Space Echo server, see Space Echo panel)
sudo ./install_autostart.sh --oled     # + status.service (OLED display, see Status display)
sudo ./install_autostart.sh --restore  # undo everything, back to PYNQ defaults
```

`--fast` changes the PYNQ image: `/boot/boot.py` no longer loads `base.bit`
and blinks the LEDs (original kept as `/boot/boot.py.orig`), and
`jupyter.service` and `clear_pl_statefile.service` are disabled (the latter
has `Requires=jupyter.service`). Jupyter at port 9090 is then gone; SSH stays.
Boot was slow mainly because three Python processes importing `pynq` (~10 s
each on the 650 MHz Cortex-A9) started at once, not because of the SD card.

The other ~38 s go to the on-board Ethernet: the kernel's `macb` driver stalls
in its MDIO PHY scan at boot. If you only use Wi-Fi, switch it off (kept
across reboots, applies at the next boot):

```
sudo ./eth-disable.sh    # adds initcall_blacklist=macb_driver_init via /boot/uEnv.txt
sudo ./eth-enable.sh     # undo
```

With `--fast` and Ethernet off, the IR is ready about 49 s after power-on
(kernel 2.8 s instead of ~41 s). If the board ever fails to boot after
this, delete `uEnv.txt` from the SD card's FAT partition on a PC.

Note: `CabConv(...)` loads the bitstream by default, which clears the loaded
IR. To attach to an overlay that is already running, use `download=False`. The
PROG button and a power cycle also clear the FPGA. Starting `pynq` takes about
10 s on the board.

See `sw/example.py` for more. The default normalization sets the peak of the
IR's magnitude response to 0 dB, and `gain_db` adds headroom relative to that.

IR file requirements (`read_wav` / `prepare_ir` in `sw/cab_conv.py`):

| | |
|---|---|
| Format | WAV only, uncompressed: PCM 8/16/24/32-bit or float 32/64-bit (not ADPCM, µ-law, FLAC, AIFF) |
| Sample rate | any, resampled to 48 kHz; 44.1 / 48 / 88.2 / 96 / 192 kHz exactly, unusual rates with an error of a few ppm |
| Channels | any number, but they are **averaged**: a stereo / two-mic IR gets summed (possible comb filtering), so pick one channel beforehand |
| Length | any; after resampling, cut to 8192 taps (170.7 ms) with a 5 ms fade, and leading silence (below −60 dB of the peak) is trimmed |

The guitar/amp signal has no such requirements: it comes in as analog through
LINE IN (full scale about 1 Vrms at 0 dB AUX gain).

Buttons and LEDs on the board (handled in the FPGA, so they work without any
script running; software sees and can change the same `CTRL` bits):

| Button | Action | LED |
|---|---|---|
| BTN0 | toggle recording (`CTRL[6]`, see [Recording](#recording)) | LED0 blinks (~1.8 Hz) while recording |
| BTN1 | toggle bypass (`CTRL[0]`): dry signal instead of the cab | LED1 on while the cab is active (bypass off, the power-on state) |
| BTN2 | toggle the Space Echo on/off (`ECHO_CTRL[0]`) | LED2 on while the echo is on |
| BTN3 | toggle the spring reverb input on/off (`ECHO_CTRL[5]`; the tail rings out after switching off) | LED3 on while reverb is on |
| | | RGB LEDs LD4 / LD5: input / output level (LD4 colour: cab on = green (echo off) / blue (echo on); bypass = white (echo off) / cyan (echo on); LD5 the same with reverb instead of echo; while recording both blink full-brightness red in step with LED0); brightness doubles every 6 dB (dark below about -48 dBFS), ~85 ms release |

## Connecting the amp: important

- **Never connect the speaker output directly to LINE IN.** A 50 W amp into
  8 Ω gives about 20 Vrms, while the codec input's full scale at 0 dB AUX gain
  is about 1 Vrms (per the ADAU1761 datasheet). You need 30 dB of attenuation
  or more.
- **A tube amp must have a load** (a load box, or an attenuator with a load).
  Running without a load will damage the output transformer.
- Best to take the signal from the attenuator's line output, or through a DI
  box with a speaker-level input. Galvanic isolation (a transformer) protects
  against a ground loop between the amp and the USB-powered board.
- Set the level so that when you play hardest, `status()` shows
  `peak_in_dbfs` around −6 dBFS and `input_clip=False`. `aux_gain_db`
  (−12…+6 dB in 3 dB steps) helps with that.
- The output is the HP+MIC jack, which has a headphone amp. It can drive
  headphones, a mixer's line input or an active FRFR speaker.

Practical notes from testing with an audio interface:

- **Ground loop:** if the board's USB and the audio interface go to the same
  PC, about −52 dBFS of hum appears at LINE IN. Power the board separately
  (e.g. a phone charger, with network access over Wi-Fi or Ethernet) and the
  hum disappears.
- **Gain staging:** keep the FPGA output near −3 dBFS peak (e.g.
  `load_ir(..., gain_db=+3)` for −5 dBFS at the input) and the headphone
  volume at 0 dB, and set the interface's input gain so the recording peaks
  around −12 dBFS. The remaining noise floor comes from the board's analog
  output, so a louder digital output gives a better signal-to-noise ratio.

## Register map (AXI4-Lite, 128 KiB, 0x43C00000)

| Offset | Name | Description |
|---|---|---|
| 0x0000 | ID | `0x43414256` ("CABV") |
| 0x0004 | PARAMS | [15:0] number of taps, [23:16] log2 taps per lane, [31:24] lanes |
| 0x0008 | CTRL | [0] bypass (BTN1 toggles it), [1] mute, [3:2] input (0 L, 1 R, 2 (L+R)/2), [4] bank, [5] wet on L only, [6] REC (recording; BTN0 toggles it) |
| 0x000C | STATUS | [0] active bank, [1] input clip, [2] output clip, [3] overrun, [4] echo overrun, [5] spring reverb overrun (sticky; 4 and 5 should never be 1) |
| 0x0010 | SHIFT0 | shift for bank 0 |
| 0x0014 | SHIFT1 | shift for bank 1 |
| 0x0018 | CLEAR | [0] clears the flags and peak meters |
| 0x001C | SAMPLES | sample counter |
| 0x0020 | PEAK_IN | max \|x\| since the last CLEAR |
| 0x0024 | PEAK_OUT | max \|y\| since the last CLEAR |
| 0x0028 | REC_WPTR | frames written since REC last went high (read-only, 32-bit, reset on the rising edge of REC) |
| 0x0040 | ECHO_CTRL | [0] EN (BTN2 toggles it), [3:1] head mask (bit 1 = head 1), [4] CANCEL, [5] REV (BTN3 toggles it; spring reverb input on); reset 0x02 |
| 0x0044 | ECHO_DELAY | head-1 delay, unsigned Q16.16 samples (reset 100 ms) |
| 0x0048 | ECHO_FB | [16:0] feedback, Q16 (0 .. 2.0) |
| 0x004C | ECHO_LEVEL | [16:0] echo volume, Q16 |
| 0x0050 | ECHO_INGAIN | [16:0] record level, Q16 (reset 1.0) |
| 0x0054 | ECHO_BASS | [17:0] signed Q16, 0 = flat |
| 0x0058 | ECHO_TREBLE | [17:0] signed Q16, 0 = flat |
| 0x005C | ECHO_WOW | [15:0] wow depth, [31:16] flutter depth, Q8 samples |
| 0x0060 | ECHO_DAMP | [15:0] tape lowpass coefficient, Q16 (reset 65535) |
| 0x0064 | ECHO_REVERB | [16:0] spring reverb volume (REVERB VOLUME), Q16; works with and without the echo |
| 0x0068 | ECHO_PEAK | read-only, read clears: [23:0] max \|DAC word\|, [24] echo mix saturated |
| 0x006C | ECHO_DRIVE | read-only, read clears: [24:0] max \|record signal\| before the tape clip |
| 0x8000 + 4i | COEF[i] | tap i (18-bit signed), written to the inactive bank |
| 0x10000 + 4i | CAPTURE[i] | read-only ring of 8192 frames (170.7 ms); word 2k = dry x, 2k+1 = wet y of frame k, 24-bit signed, sign-extended to 32 bits; frame n is stored at k = n mod 8192 |

## Recording

The board can record the dry input and the convolved output as two mono
24-bit 48 kHz WAVs on the SD card, for example to compare or re-process a take offline. No DMA is involved: the PL
writes both signals into a ring buffer of 8192 frames (170.7 ms) while `CTRL[6]`
(REC) is set, and the PS drains it over the AXI4-Lite link
(`CabConv.read_capture`). Dry is the engine input after the input select, wet
is the engine output (before bypass/mute), and both are aligned to the same
sample index, so `wet = IR * dry` holds exactly between the two files.

REC is switched by software (`cab.rec_start()` / `cab.rec_stop()`, or the
`--start` option of `record.py`) or by a press of **BTN0** (debounced, toggles).
**LED0** blinks while REC is on. The PS reads the ring about every 20 ms, so the ring only
overflows if the process is starved for more than ~170 ms; an overflow is
reported and the lost frames are filled with zeros.

```
sudo -i python3 ~xilinx/cab_conv/record.py              # daemon: one dry + wet pair per REC period
sudo -i python3 ~xilinx/cab_conv/record.py --once       # wait for REC, record one take, exit
sudo -i python3 ~xilinx/cab_conv/record.py --once --start   # start now, stop with BTN0 or Ctrl+C
sudo -i python3 ~xilinx/cab_conv/record.py --dir /media/usb # other target directory
```

Each take is a pair `rec_YYYYmmdd_HHMMSS_dry.wav` + `rec_YYYYmmdd_HHMMSS_wet.wav`
in `~xilinx/cab_conv/rec/`, sample-aligned and of equal length (the dry file
can go straight back into a DAW for reamping). They are written incrementally;
the headers are patched every 2 s, so a power cut loses
at most the last couple of seconds. `install_autostart.sh` also installs
`record.service` (after `cab-conv.service`) unless given `--no-rec`, so BTN0
starts and stops a take at any time after boot. Each 24-bit mono file grows by about 8.6 MB per minute.

## Status display (OLED)

An optional 0.91" SSD1306 OLED module (128×32, I2C address 0x3C) shows the
state of the convolver. It sits on PS I2C0, which the bitstream routes to the
Arduino header SCL/SDA pins (FPGA pins P15 / P16), so it is `/dev/i2c-0` on
the board (the codec uses `/dev/i2c-1`). Four female-female jumper wires:

| OLED | PYNQ-Z2 (Arduino header) |
|------|--------------------------|
| GND | GND |
| VCC | 3.3V or 5V (power header), see below |
| SCL | SCL (P15) |
| SDA | SDA (P16) |

VCC: 3.3V always works and is the recommended choice; on the tested module 5V
was not noticeably brighter. 5V is safe only if the module's SCL/SDA pull-ups go to its own 3.3 V regulator, not
to VCC: power the module from 5V with SCL/SDA **disconnected** and measure
SCL and SDA to GND. 3.3 V means 5V is fine; 5 V means stay on 3.3V (the
FPGA pins are not 5 V tolerant). The tested module reads 3.3 V.

Change the wiring with the board off. If the display loses power in the middle
of a transfer, the I2C0 bus can stay stuck (`EAGAIN` / timeouts even with
nothing connected) until the board is power cycled.

```
CAB V30_SM57_blend.wav             mode (CAB / BYPASS, + MUTE) and IR name (current_ir.txt);
                                   while recording: mode + REC with blinking dot + mm:ss
[[#########.........:.....]]       input peak  (bar -60..0 dBFS, tick at -12 dB, 2 px frame)
[[##############....:.....]]       output peak (a clipping meter is shown inverted)
```

```
sudo -i python3 ~xilinx/cab_conv/oled.py test      # test pattern for 5 s
sudo -i python3 ~xilinx/cab_conv/status.py         # the display daemon (8 Hz)
sudo ./install_autostart.sh --oled                 # status.service at boot; combine: --fast --oled
```

`status.py` only reads registers (no overlay reload) and shows "no overlay"
until the overlay is loaded. If the OLED is missing it logs the I2C error and
retries with a growing delay (1 s up to 30 s). Options: `--bus`, `--addr`,
`--rate`, `--contrast`, `--flip` (rotate 180°). Like `meter.py` it clears the
peak registers on every refresh, so do not run both at the same time. The IR
name is read from `~xilinx/cab_conv/current_ir.txt`, which `autostart.py` and
`load_ir.py` write when they load an IR.

## Space Echo panel

The tape echo ([docs/space_echo.md](docs/space_echo.md)) is controlled from the PC with a
panel drawn after the Roland RE-201 front panel. `echo.service` (installed by default,
`--no-echo` skips it) runs `~xilinx/cab_conv/echo_server.py` on TCP port 5201; it keeps the
knob state in `~xilinx/cab_conv/echo_state.json` and re-applies it after boot.

```
python sw/echo_panel.py --host 192.168.0.31 [--port 5201]   # the panel (tkinter, stdlib only)
python sw/echo_panel.py --demo                              # fake board, no hardware needed
sudo -i python3 ~xilinx/cab_conv/echo_server.py             # the server by hand (--fake on a PC)
```

Drag a knob vertically (Shift = fine), mouse wheel, double-click = default, right-click = type
a value. MODE SELECTOR: click a position or drag around it. Keys: Space = POWER, C = ECHO
CANCEL, B = cab bypass, 1-9 0 - = select mode 1..12. The slide switch in the INSTRUMENT box
switches the cab convolver to bypass; the H M L switch in the OUTPUT box sets the headphone
volume (0 / -10 / -20 dB, H = the boot default). Presets: built-in ones in `sw/echo_presets.json`, your own
(status bar, "Save preset...") in `~/.cab_echo_presets.json`. The panel reconnects by itself.
The server only reads the echo meters (no CLEAR); run just one panel at a time.

The echo's arithmetic and the register details are in [docs/space_echo.md](docs/space_echo.md);
the RTL is `rtl/tape_echo.v` (about 50 clock cycles per sample, one shared DSP multiplier, a
2^15-sample tape in BRAM) and the bit-exact model is `echo_model()` in `sw/cab_conv.py`. With
`ECHO_CTRL.EN = 0` the output is bit-identical to the design without the echo.

The spring reverb (MODE SELECTOR positions 5-12, REVERB VOLUME knob; BTN3 toggles `ECHO_CTRL[5]`) is
two dispersive spring models in `rtl/spring.v` (one shared multiplier, BRAM state, 728 cycles per
sample, running in parallel with the FIR; bit-exact model `spring_model()`, details in
[docs/spring.md](docs/spring.md)). REV only gates the spring input; the wet signal
`(W * ECHO_REVERB) >> 16` is added to the echo output (saturated) and also works with the echo
off (`EN = 0`). With REV = 0, REVERB = 0 and an empty tail the output is bit-identical to the
design without the reverb. The reverb adds 2 samples of latency; `STATUS[5]` flags a step that
did not finish within a frame.

## Extensions

- **Longer IR:** `NLANE = 8` in `cab_conv_top.v` gives 16384 taps (341 ms)
  for 4 more DSPs and about 14 more BRAM tiles. The lane length (`LW = 11`)
  is already close to the 2560-cycle-per-sample limit. The register map also
  has to change: the coefficient window at `0x8000` only has room for 8192
  taps within the 0x8000-0xFFFF address range (the AXI window is now 128 KiB, but
  the capture ring sits at `0x10000`).
- **Two IRs at once** (e.g. two mics or stereo): a second `fir_engine` with
  its own coefficients for the right channel.
- **Higher fs:** 96 kHz needs a 245.76 MHz clock, or twice as many lanes for
  the same IR length in milliseconds.

## License

GPL-3.0, full text in `LICENSE`. Exception: `vivado/ps7_pynq_z2.tcl` comes
from PYNQ v2.7.0 (Copyright (c) 2018, Xilinx) and is under BSD-3-Clause, text
in `vivado/LICENSE.PYNQ`.
