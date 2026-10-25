# CLAUDE.md

Guitar cabinet IR convolver for the **PYNQ-Z2** (Zynq-7020 + ADAU1761 codec).
Amp signal (via attenuator/load box) → LINE IN → time-domain FIR in PL → headphone out.
README.md (English) is the user-facing doc, README_PL.md its Polish translation; keep both in sync.
This file is for working on the code.

## Communication
- Code, comments and commit messages in English. Use SI units.

## Status
- RTL verified bit-exact in Icarus simulation (small and full 8192-tap config both PASS).
- `vivado/build.tcl` builds cleanly in Vivado 2026.1: timing met (WNS +0.30 ns, WHS +0.03 ns @ 122.88 MHz,
  with echo + spring reverb), 6 DSP (4 FIR, 1 echo, 1 spring), 52.5 BRAM tiles (12 capture ring, 24 tape,
  3 spring), 3462 LUT (6.5%); no critical warnings. Spring reverb: bit-exact in simulation and verified on hardware
  (riff through the Focusrite: reverb-only tail decays ~20 dB/s, RT60 ~2.5 s, as the model predicts).
- **Verified on hardware** (PYNQ Linux 3.0): overlay + codec init, bypass, IR load / bank switch,
  real-time convolution. Measured through a Focusrite (sweep-free, riff cross-spectrum): response
  matches the IR within ~0.5 dB 60 Hz–5 kHz, coherence 0.97–1.0, fs offset vs Focusrite ~5–10 ppm.
- Recording (dry + wet mono WAV pair via a PL BRAM ring, BTN0/LED0): verified on hardware: 30 s take with 0 frames lost, wet channel bit-exact vs `fir_model(dry)`; BTN0 toggling + LED0 work.
- PYNQ 3.0: run the driver as root via `sudo -i python3 ...` (login shell sources XRT + venv;
  plain `sudo python3` fails with "No Devices Found"). `import pynq` takes ~10 s on the board.
- Each `CabConv(...)` with `download=True` reloads the bitstream and wipes the IR; use
  `download=False` to attach to a running overlay. PROG button / power cycle also clears the PL.
- **Never reload the bitstream while anything polls the registers** (status.py, echo_server.py,
  record.py, meter.py): an AXI read during PL reprogramming hangs the bus and freezes the board (needs
  a power cycle). The helper units have `PartOf=cab-conv.service`, so `systemctl restart cab-conv`
  stops them first; stop hand-started copies and wait for them to exit before a reload.
- Analog: board USB to the same PC as the audio interface gives a ground loop (~-52 dBFS hum at
  LINE IN); power the board separately (Wi-Fi via USB RTL8188CUS works, `rtl8192cu` driver).
  Remaining floor (~-69 dBFS at HP out, 0 dB) is board-side analog; keep FPGA output near -3 dBFS
  (`load_ir(..., gain_db=+3)` for a -5 dBFS input) for best SNR.
- Tool: AMD Vivado 2026.1 with the free **Basic** tier license. Basic does not cover minor
  releases (2026.1.1 refuses to start). Vivado is not on PATH here
  (`C:\AMDDesignTools\2026.1\Vivado\bin`) and needs `XILINXD_LICENSE_FILE` pointing at the .lic.
- The meters (peak/clip) run one cycle behind the audio path for timing; I2S pins have
  dedicated output flops so they pack into IOBs.

## Layout
```
rtl/cab_conv_top.v   AXI4-Lite regs, input select, bypass/mute, peak/clip meters, MCLK divider
rtl/fir_engine.v     multi-lane time-multiplexed FIR, ping-pong coef banks
rtl/i2s_master.v     I2S master (codec is slave), 64 BCLK/frame, 24-bit in 32-bit slots
rtl/clk_fwd.v        ODDR clock forwarding (plain flop under `SIMULATION`)
rtl/debounce.v       button sync + debounce (BTN0 -> REC, BTN1 -> bypass, BTN2 -> echo EN, BTN3 -> ECHO_CTRL[5] REV; LED0 blinks while REC, LED1 = !bypass, LED2 = echo on, LED3 = rev on)
rtl/tape_echo.v      Space Echo (RE-201 style tape echo): sequential engine, 1 shared DSP multiplier, 2^15x24 BRAM tape,
                     ~50 cycles/sample, bit-exact with `cab_conv.echo_model` (spec docs/space_echo.md); runs after the FIR, before the DAC
rtl/spring.v         spring reverb (2 dispersive springs, time-shared, 1 shared DSP multiplier, 3 BRAM tiles, 728 cycles/sample),
                     runs in parallel with the FIR, bit-exact with `cab_conv.spring_model` (spec docs/spring.md);
                     DAC word = sat24(echo_out + wet), REV = ECHO_CTRL[5], volume = ECHO_REVERB, STATUS[5] = spring overrun
rtl/level_led.v      peak envelope -> PWM LED (RGB LD4 = input, RGB LD5 = DAC output level; colours show cab/bypass x echo EN (LD4) or REV (LD5)), 6 dB per brightness doubling
sim/tb_cab_conv.v    AXI BFM + behavioural ADAU1761 I2S slave
sim/run_sim.py       stimulus gen, runs iverilog/vvp, bit-exact check vs Python model
vivado/build.tcl     project + block design + bitstream, copies .bit/.hwh to build/
vivado/ps7_pynq_z2.tcl  full PS7 config (DDR/MIO) from PYNQ v2.7 base overlay (BSD-3) – no board files needed
vivado/pynq_z2_audio.xdc codec pins
sw/cab_conv.py       PYNQ driver: overlay, codec over /dev/i2c-*, IR prep/quantize, bit-exact model,
                     CaptureMixin (rec_start/rec_stop/recording/read_capture)
sw/example.py        usage example
sw/board/            helper scripts used on the board in ~xilinx/cab_conv/ (run with sudo -i):
                     load_cab.py (overlay + IR), load_ir.py (IR only), meter.py (live levels), ctl.py (mute/HP vol),
                     record.py + record.service (dry/wet WAV recorder, installed by default by `install_autostart.sh`, `--no-rec` to skip),
                     oled.py (SSD1306 128x32 driver over /dev/i2c-0 + 5x7 font + Canvas; `oled.py test [--ascii]`),
                     status.py + status.service (OLED daemon: mode, REC time, IR name from current_ir.txt written by
                     autostart.py/load_ir.py, peak meters; reads regs only, `install_autostart.sh --oled`),
                     echo_server.py + echo.service (Space Echo TCP server, port 5201, JSON lines, state in
                     echo_state.json; `--fake` for PC; installed by default, `--no-echo` skips; PC panel: sw/echo_panel.py
                     [--demo], presets sw/echo_presets.json, see docs/space_echo.md),
                     autostart.py + cab-conv.service (boot start, After=bootpy.service which loads base.bit),
                     install_autostart.sh [--fast|--restore] (installs the unit; --fast trims /boot/boot.py and
                     disables jupyter + clear_pl_statefile, which has Requires=jupyter),
                     eth-disable.sh / eth-enable.sh (copied to ~xilinx; bootargs= in /boot/uEnv.txt with
                     initcall_blacklist=macb_driver_init: macb is built in and its MDIO PHY scan stalls boot ~38 s)
```

## Commands
```
python3 sim/run_sim.py                 # 4 lanes x 64 taps, HALF=10, ~10 s
python3 sim/run_sim.py --full          # 4 x 2048 taps, HALF=20, ~5 min (>8192 samples)
vivado -mode batch -source vivado/build.tcl -tclargs <jobs>   # -> build/cab_conv.{bit,hwh}
```
**Python: use the repo venv `.venv`** (created from the Python on PATH by `setup_venv.bat` /
`setup_venv.sh`, `--reinstall` to rebuild; deps in `requirements.txt`; don't force pyenv, conda or
the global interpreter). Run PC-side Python with `.venv/Scripts/python.exe` (Windows) or
`.venv/bin/python`; repo scripts should re-exec into `.venv` when it exists, like `run_sim.py`.
New PC-side deps go into `requirements.txt`. Board scripts use the PYNQ image's Python.
Sim needs `iverilog`, `numpy`, `scipy`. Compile flags used: `-g2005 -DSIMULATION -DNLANE= -DLW= -DHALF=`.
`sim/work/` (small) and `sim/work_full/` (`--full`) are generated outputs, so both configs can run at once; don't commit them.

**After any RTL or model change, run `run_sim.py` (and `--full` for engine/timing changes) and keep it at PASS.**

## Architecture (invariants to keep)
- **Single clock domain**: `aclk` ≈ 122.88 MHz from clk_wiz on FCLK0. AXI, I2S and FIR all run on it.
- **Codec clocking**: MCLK = aclk/10 (12.288 MHz = 256·fs), codec R0 = 0x01 (MCLK direct, no codec PLL).
  BCLK = aclk/(2·HALF), LRCLK = BCLK/64, so everything is exactly synchronous. Don't switch to the
  PYNQ approach (10 MHz MCLK + codec PLL + LRCLK from 100 MHz/2048): that LRCLK is ~48.8 kHz vs 48 kHz core → slips.
- **Cycle budget**: per sample 2·HALF·64 cycles (2560 at HALF=20). Engine needs `2^LW + ~12`.
  `LW=11` is near the limit; longer IR = more lanes (`NLANE`), not longer lanes.
- **Delay line cascade**: each lane is a circular buffer of 2^LW samples; on a new sample the oldest
  entry of lane k moves to lane k+1 at the same address, so all lanes share one address sequence.
- **Arithmetic** (must match `sw/cab_conv.py:fir_model` exactly): x 24-bit, h 18-bit, per-lane acc
  48-bit wrapping (DSP48 P), lanes summed, arithmetic right shift by `shift`, saturate to 24-bit.
  Coefficients are block floating point `h = hq / 2^shift`; `quantize()` picks the largest shift that
  fits 18 bits and cannot overflow 48-bit lane accumulators at full-scale input.
- **Bank switching**: coef writes always go to the inactive bank; `CTRL[4]` requests a bank; the engine
  latches bank + matching `SHIFTn` at the next sample start; `STATUS[0]` shows the applied bank.
  The driver waits for STATUS before writing the next IR.
- **Latency**: 2 samples ADC word → DAC word (rx in frame f, compute in f+1, tx in f+2).
- Power-on state: bank 0 = unity (h[0]=65536, shift0=16) → audio passes through before any IR is loaded.

## Register map
Documented at the top of `rtl/cab_conv_top.v` and in README.md / README_PL.md. Coefs at `0x8000 + 4*i`, write-only.
AXI window is 128 KiB. Recording: `CTRL[6]` REC (RW, BTN0 toggles, LED0 blinks while set), `0x28` REC_WPTR (RO, frames since
REC rose), `0x10000 + 4*i` CAPTURE ring (RO, 8192 frames, word 2k = dry x, 2k+1 = wet y, sign-extended 24-bit).
Space Echo: regs `0x40..0x6C` (ECHO_*, `ECHO_PEAK`/`ECHO_DRIVE` clear on read), `STATUS[4]` echo overrun, `STATUS[5]` spring overrun; tx = sat24(echo output + spring wet)
(EN = 0 is bit-identical to no echo; REV = 0, REVERB = 0 and an empty tail is bit-identical to no reverb); the capture ring still records the cab output y. The sim overrides the glide
shift (`ECHO_SLEW` param, 5 in the TB, 13 in hardware) so echoes settle within a short run.
`record.py` attaches by raw MMIO (no CabConv, codec not reconfigured) and writes `~xilinx/cab_conv/rec/*.wav`.
If you change the map, update RTL header, both README tables, `sw/cab_conv.py` constants and the testbench together.

## Board / codec facts (verified against PYNQ v2.7 sources)
- Pins: MCLK U5, BCLK R18, LRCLK T17, SDATA to codec G18 (`sdata_o`), from codec F17 (`sdata_i`),
  ADDR0/1 M17/M18 (driven high → I2C 0x3B), I2C SCL U9 / SDA T9 via PS I2C1 on EMIO → `/dev/i2c-1`.
- LINE IN goes through AUX inputs (R5/R7 gain code `(dB+15)/3`, 0 dB ≈ 1 Vrms full scale).
- Output is the headphone amp (R29/R30, 0 dB = code 57). Init sequence lives in `ADAU1761.configure()`.
- `ps7_pynq_z2.tcl` overrides: GP0 only, FCLK0 100 MHz, I2C1 EMIO, no fabric IRQs / HP ports.

## Conventions
- Verilog-2001 (Vivado module reference + Icarus `-g2005`), synchronous active-high `rst = ~aresetn`.
- Xilinx primitives only behind `ifndef SIMULATION`.
- Keep BRAM/DSP inference friendly: registered reads, no reset on memory/MAC datapath.
- Python driver must stay importable without `pynq` for the model/quantize helpers (sim uses them).

## Ideas / next steps
- Possible features: NLANE=8 (16384 taps; needs a register map change, the coef window at 0x8000
  only holds 8192 words in 64 KiB), second engine for a stereo / dual-mic IR, Jupyter notebook UI,
  longer reverb IRs (~40 lanes fit in BRAM, needs a pipelined lane-sum tree).
