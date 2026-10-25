// cab_conv_top.v
// Guitar cabinet IR convolver for PYNQ-Z2 (ADAU1761 codec).
//
// Single clock domain (aclk, nominally 122.88 MHz = 10 * MCLK).
// AXI4-Lite register map (byte addresses, 128 KiB window):
//
//   0x0000  ID        RO  0x43414256 ("CABV")
//   0x0004  PARAMS    RO  [15:0] taps total, [23:16] log2 taps/lane,
//                         [31:24] lanes
//   0x0008  CTRL      RW  [0] bypass (dry signal out, also toggled by BTN1)
//                         [1] mute
//                         [3:2] input: 0 = left, 1 = right, 2 = (L+R)/2
//                         [4] bank select (requested active coef bank)
//                         [5] output only on left channel (right = dry)
//                         [6] REC: recording (also toggled by BTN0)
//   0x000C  STATUS    RO  [0] active bank (applied)
//                         [1] input clip   (sticky)
//                         [2] output clip  (sticky): the final DAC word (after echo + reverb,
//                             dry in bypass; muted = 0, not counted) saturated or |word| >= 0x7E0000
//                         [3] engine overrun (sticky, should never be 1)
//                         [4] echo overrun (sticky, should never be 1)
//                         [5] spring reverb overrun (sticky, should never be 1)
//   0x0010  SHIFT0    RW  [5:0] output shift for bank 0
//   0x0014  SHIFT1    RW  [5:0] output shift for bank 1
//   0x0018  CLEAR     WO  [0] clear sticky flags and peak meters
//   0x001C  SAMPLES   RO  sample counter (frames since reset)
//   0x0020  PEAK_IN   RO  max |x| since last clear (24-bit)
//   0x0024  PEAK_OUT  RO  max |final DAC word| since last clear (24-bit)
//   0x0028  REC_WPTR  RO  frames written since REC last went 0 -> 1 (free running)
//   0x0040  ECHO_CTRL   RW  [0] EN (also toggled by BTN2), [3:1] head mask (bit 1 = head 1), [4] CANCEL, [5] REV (also toggled by BTN3)
//                         (Space Echo, see docs/space_echo.md; reset 0x02 = off, head 1)
//   0x0044  ECHO_DELAY  RW  head-1 delay target, unsigned Q16.16 samples (reset 100 ms)
//   0x0048  ECHO_FB     RW  [16:0] feedback gain, Q16            (reset 0)
//   0x004C  ECHO_LEVEL  RW  [16:0] echo volume, Q16              (reset 0)
//   0x0050  ECHO_INGAIN RW  [16:0] record level, Q16             (reset 65536)
//   0x0054  ECHO_BASS   RW  [17:0] signed Q16                    (reset 0)
//   0x0058  ECHO_TREBLE RW  [17:0] signed Q16                    (reset 0)
//   0x005C  ECHO_WOW    RW  [15:0] wow depth, [31:16] flutter depth, Q8 samples (reset 0)
//   0x0060  ECHO_DAMP   RW  [15:0] tape lowpass coefficient, Q16 (reset 65535)
//   0x0064  ECHO_REVERB RW  [16:0] spring reverb volume, Q16     (reset 0)
//   0x0068  ECHO_PEAK   RO  [23:0] max |DAC word| since last read, [24] output mix saturated
//                         (final word, including the reverb); reading clears
//   0x006C  ECHO_DRIVE  RO  [24:0] max |record signal| before the tape clip since last read;
//                         reading clears
//   0x8000+4*i  COEF[i]  WO  [17:0] signed tap i, written to the INACTIVE bank
//   0x10000+4*i CAPTURE[i] RO  ring of 2^CAP_LOG2 frames (8192 by default);
//                         word 2*k = dry x, word 2*k+1 = wet of frame k
//                         (24-bit signed, sign-extended), k = frame mod 2^CAP_LOG2.
//                         Written only while REC = 1. x = selected engine input,
//                         wet = the final left DAC word (tx_l) of the same frame: cab FIR or
//                         bypass, tape echo and spring reverb as switched on, 0 while muted.
//
// Audio path: x -> FIR (y) -> [bypass ? x : y] = s -> Space Echo -> + spring wet -> DAC
// (mute forces 0). With ECHO_CTRL.EN = 0 the tape echo is transparent.
// Spring reverb (spring.v, docs/spring.md): runs in parallel with the FIR, input = s of the
// previous sample gated by ECHO_CTRL[5] REV (the tail rings out after REV is cleared),
// wet = (W * ECHO_REVERB) >> 16 added to the echo output with saturation, independent of EN.
// With REV = 0, ECHO_REVERB = 0 and an empty tail the output equals the echo output.
//
// The codec address pins are driven high (I2C address 0x3B, like PYNQ).
// Buttons: BTN0 toggles REC (LED0 blinks while recording), BTN1 toggles bypass
// (LED1 = cab on), BTN2 toggles the Space Echo EN bit (LED2 = echo on),
// BTN3 toggles ECHO_CTRL.REV (LED3 = reverb on).
// Level meters as PWM brightness: input on RGB LED LD4, output on RGB LED LD5.
// RGB LD4 shows the input level (PWM) in a colour that tells the mode:
//   cab on,     echo off: green      cab on,     echo on: blue
//   cab bypass, echo off: white      cab bypass, echo on: cyan (G+B)
// While recording, in the on-phase of LED0's blink LD4 is pure red at full brightness
// (no PWM); in the off-phase the level colour shows.
// RGB LD5 shows the output level (DAC word, 0 when muted) with the same colours, but
// its last mode is REV instead of echo EN:
//   cab on,     rev off: green       cab on,     rev on: blue
//   cab bypass, rev off: white       cab bypass, rev on: cyan (G+B)
// and the same pure red during the REC blink on-phase.

`timescale 1ns / 1ps

module cab_conv_top #(
    parameter NLANE = 4,
    parameter LW    = 11,           // 2048 taps per lane
    parameter HALF  = 20,           // aclk cycles per BCLK half period
    parameter CAP_LOG2 = 13,        // capture ring: 2^CAP_LOG2 frames
    parameter DEB_BITS = 22,        // button debounce: 2^DEB_BITS stable cycles
    parameter BLINK_BIT = 25,       // REC LED blink period 2^(BLINK_BIT+1) cycles (0.55 s)
    parameter ECHO_SLEW = 13        // echo tape-speed glide 2^ECHO_SLEW samples (sim: smaller)
) (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 aclk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF s_axi, ASSOCIATED_RESET aresetn" *)
    input  wire        aclk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input  wire        aresetn,

    // AXI4-Lite slave
    input  wire [16:0] s_axi_awaddr,
    input  wire [2:0]  s_axi_awprot,
    input  wire        s_axi_awvalid,
    output reg         s_axi_awready,
    input  wire [31:0] s_axi_wdata,
    input  wire [3:0]  s_axi_wstrb,
    input  wire        s_axi_wvalid,
    output reg         s_axi_wready,
    output wire [1:0]  s_axi_bresp,
    output reg         s_axi_bvalid,
    input  wire        s_axi_bready,
    input  wire [16:0] s_axi_araddr,
    input  wire [2:0]  s_axi_arprot,
    input  wire        s_axi_arvalid,
    output reg         s_axi_arready,
    output reg  [31:0] s_axi_rdata,
    output wire [1:0]  s_axi_rresp,
    output reg         s_axi_rvalid,
    input  wire        s_axi_rready,

    // codec
    output wire        mclk,        // to ADAU1761 MCLK (aclk / 10)
    output wire        bclk,
    output wire        lrclk,
    output wire        sdata_o,
    input  wire        sdata_i,
    output wire [1:0]  codec_addr,

    // buttons / LEDs
    input  wire        btn_rec,     // BTN0: toggle REC
    output wire        led_rec,     // LED0: blinks while recording
    input  wire        btn_byp,     // BTN1: toggle bypass
    output wire        led_cab,     // LED1: cab on (bypass off)
    input  wire        btn_echo,    // BTN2: toggle Space Echo EN
    output wire        led_echo,    // LED2: Space Echo on
    output wire        led_rgb_r,   // RGB LD4 red (see the LD4 colour table in the header)
    output wire        led_lvl_in,  // RGB LD4 green
    output wire        led_rgb_b,   // RGB LD4 blue
    input  wire        btn_rev,     // BTN3: toggle reverb (ECHO_CTRL[5])
    output wire        led_rev,     // LED3: reverb on
    output wire        led5_r,      // RGB LD5 red (output level, colour table in the header)
    output wire        led5_g,      // RGB LD5 green
    output wire        led5_b       // RGB LD5 blue
);

    localparam DW        = 24;
    localparam CW        = 18;
    localparam NTAPS     = NLANE << LW;
    localparam LANE_BITS = (NLANE <= 1) ? 1 : $clog2(NLANE);
    localparam TBITS     = LANE_BITS + LW;
    localparam [31:0] PARAMS_WORD = (NLANE << 24) | (LW << 16) | (NTAPS & 32'hFFFF);

    wire rst = ~aresetn;

    assign codec_addr  = 2'b11;
    assign s_axi_bresp = 2'b00;
    assign s_axi_rresp = 2'b00;

    // ------------------------------------------------------------------
    // MCLK = aclk / 10, forwarded through an IOB DDR register
    // ------------------------------------------------------------------
    reg [2:0] mdiv;
    reg       mclk_d;
    always @(posedge aclk) begin
        if (rst) begin
            mdiv   <= 3'd0;
            mclk_d <= 1'b0;
        end else if (mdiv == 3'd4) begin
            mdiv   <= 3'd0;
            mclk_d <= ~mclk_d;
        end else begin
            mdiv <= mdiv + 1'b1;
        end
    end
    clk_fwd u_mclk (.clk(aclk), .d(mclk_d), .q(mclk));

    // ------------------------------------------------------------------
    // Registers
    // ------------------------------------------------------------------
    reg        r_bypass, r_mute, r_bank, r_left_only, r_rec;
    reg [1:0]  r_insel;
    reg [5:0]  r_shift0, r_shift1;
    reg        st_in_clip, st_out_clip;
    reg [31:0] samples;
    reg [23:0] peak_in, peak_out;

    // Space Echo registers
    reg [5:0]  e_ctrl;
    reg [31:0] e_delay, e_wow;
    reg [16:0] e_fb, e_lvl, e_ing, e_rev;
    reg [17:0] e_bass, e_treb;
    reg [15:0] e_damp;
    reg [23:0] e_peak;
    reg [24:0] e_drive;
    reg        e_psat, st_echo, st_spring;
    reg        rd_pk, rd_dr;            // peak / drive read: clear

    reg        cw_en;
    reg [TBITS-1:0] cw_addr;
    reg signed [CW-1:0] cw_data;
    reg        clr;

    wire       bank_act, overrun;

    // ---------------- buttons (debounced) and LEDs ----------------
    wire rec_press, byp_press, echo_press, rev_press;
    debounce #(.DEB_BITS(DEB_BITS)) u_deb_rec (.clk(aclk), .rst(rst), .btn(btn_rec), .press(rec_press));
    debounce #(.DEB_BITS(DEB_BITS)) u_deb_byp (.clk(aclk), .rst(rst), .btn(btn_byp), .press(byp_press));

    debounce #(.DEB_BITS(DEB_BITS)) u_deb_echo (.clk(aclk), .rst(rst), .btn(btn_echo), .press(echo_press));

    debounce #(.DEB_BITS(DEB_BITS)) u_deb_rev (.clk(aclk), .rst(rst), .btn(btn_rev), .press(rev_press));

    assign led_cab  = !r_bypass;
    assign led_echo = e_ctrl[0];
    assign led_rev  = e_ctrl[5];
    wire lvl_in_pwm, lvl_out_pwm;
    // led_rec is driven below, next to the capture ring (blinks while REC = 1)

    // ---------------- AXI write channel ----------------
    always @(posedge aclk) begin
        if (rst) begin
            s_axi_awready <= 1'b0;
            s_axi_wready  <= 1'b0;
            s_axi_bvalid  <= 1'b0;
            r_bypass      <= 1'b0;
            r_mute        <= 1'b0;
            r_bank        <= 1'b0;
            r_left_only   <= 1'b0;
            r_rec         <= 1'b0;
            r_insel       <= 2'd0;
            r_shift0      <= 6'd16;
            r_shift1      <= 6'd16;
            e_ctrl        <= 6'h02;
            e_delay       <= 32'd4800 << 16;
            e_fb          <= 17'd0;
            e_lvl         <= 17'd0;
            e_ing         <= 17'd65536;
            e_bass        <= 18'd0;
            e_treb        <= 18'd0;
            e_wow         <= 32'd0;
            e_damp        <= 16'd65535;
            e_rev         <= 17'd0;
            cw_en         <= 1'b0;
            cw_addr       <= {TBITS{1'b0}};
            cw_data       <= {CW{1'b0}};
            clr           <= 1'b0;
        end else begin
            cw_en <= 1'b0;
            clr   <= 1'b0;
            s_axi_awready <= 1'b0;
            s_axi_wready  <= 1'b0;

            if (rec_press)
                r_rec <= ~r_rec;
            if (byp_press)
                r_bypass <= ~r_bypass;
            if (echo_press)
                e_ctrl[0] <= ~e_ctrl[0];
            if (rev_press)
                e_ctrl[5] <= ~e_ctrl[5];

            if (s_axi_bvalid && s_axi_bready)
                s_axi_bvalid <= 1'b0;

            // accept address and data together
            if (s_axi_awvalid && s_axi_wvalid && !s_axi_awready &&
                !s_axi_bvalid) begin
                s_axi_awready <= 1'b1;
                s_axi_wready  <= 1'b1;
                s_axi_bvalid  <= 1'b1;

                if (s_axi_awaddr[16]) begin
                    // capture window: read-only
                end else if (s_axi_awaddr[15]) begin
                    cw_en   <= 1'b1;
                    cw_addr <= s_axi_awaddr[TBITS+1:2];
                    cw_data <= s_axi_wdata[CW-1:0];
                end else begin
                    case (s_axi_awaddr[7:2])
                    6'h02: begin
                        r_bypass    <= s_axi_wdata[0];
                        r_mute      <= s_axi_wdata[1];
                        r_insel     <= s_axi_wdata[3:2];
                        r_bank      <= s_axi_wdata[4];
                        r_left_only <= s_axi_wdata[5];
                        r_rec       <= s_axi_wdata[6];
                    end
                    6'h04: r_shift0 <= s_axi_wdata[5:0];
                    6'h05: r_shift1 <= s_axi_wdata[5:0];
                    6'h06: clr      <= s_axi_wdata[0];
                    6'h10: e_ctrl   <= s_axi_wdata[5:0];
                    6'h11: e_delay  <= s_axi_wdata;
                    6'h12: e_fb     <= s_axi_wdata[16:0];
                    6'h13: e_lvl    <= s_axi_wdata[16:0];
                    6'h14: e_ing    <= s_axi_wdata[16:0];
                    6'h15: e_bass   <= s_axi_wdata[17:0];
                    6'h16: e_treb   <= s_axi_wdata[17:0];
                    6'h17: e_wow    <= s_axi_wdata;
                    6'h18: e_damp   <= s_axi_wdata[15:0];
                    6'h19: e_rev    <= s_axi_wdata[16:0];
                    default: ;
                    endcase
                end
            end
        end
    end

    // capture ring state (memory itself is below, after the audio path)
    reg [31:0]          rec_wptr;
    reg [2*DW-1:0]      cap_q;
    reg                 cap_wait, cap_sel;

    // ---------------- AXI read channel ----------------
    always @(posedge aclk) begin
        if (rst) begin
            s_axi_arready <= 1'b0;
            s_axi_rvalid  <= 1'b0;
            s_axi_rdata   <= 32'd0;
            cap_wait      <= 1'b0;
            cap_sel       <= 1'b0;
            rd_pk         <= 1'b0;
            rd_dr         <= 1'b0;
        end else begin
            s_axi_arready <= 1'b0;
            rd_pk         <= 1'b0;
            rd_dr         <= 1'b0;
            if (s_axi_rvalid && s_axi_rready)
                s_axi_rvalid <= 1'b0;

            // capture reads: BRAM data is valid one cycle after the accept
            if (cap_wait) begin
                cap_wait     <= 1'b0;
                s_axi_rvalid <= 1'b1;
                s_axi_rdata  <= cap_sel ? {{(32-DW){cap_q[DW-1]}}, cap_q[DW-1:0]}
                                        : {{(32-DW){cap_q[2*DW-1]}}, cap_q[2*DW-1:DW]};
            end

            if (s_axi_arvalid && !s_axi_arready && !s_axi_rvalid && !cap_wait) begin
                s_axi_arready <= 1'b1;
                s_axi_rvalid  <= 1'b1;
                if (s_axi_araddr[16]) begin
                    s_axi_rvalid <= 1'b0;
                    cap_wait     <= 1'b1;
                    cap_sel      <= s_axi_araddr[2];
                end else if (s_axi_araddr[15])
                    s_axi_rdata <= 32'd0;           // coefficients: write-only
                else begin
                    case (s_axi_araddr[7:2])
                    6'h00: s_axi_rdata <= 32'h43414256;
                    6'h01: s_axi_rdata <= PARAMS_WORD;
                    6'h02: s_axi_rdata <= {25'd0, r_rec, r_left_only, r_bank,
                                           r_insel, r_mute, r_bypass};
                    6'h03: s_axi_rdata <= {26'd0, st_spring, st_echo, overrun, st_out_clip,
                                           st_in_clip, bank_act};
                    6'h04: s_axi_rdata <= {26'd0, r_shift0};
                    6'h05: s_axi_rdata <= {26'd0, r_shift1};
                    6'h07: s_axi_rdata <= samples;
                    6'h08: s_axi_rdata <= {8'd0, peak_in};
                    6'h09: s_axi_rdata <= {8'd0, peak_out};
                    6'h0A: s_axi_rdata <= rec_wptr;
                    6'h10: s_axi_rdata <= {26'd0, e_ctrl};
                    6'h11: s_axi_rdata <= e_delay;
                    6'h12: s_axi_rdata <= {15'd0, e_fb};
                    6'h13: s_axi_rdata <= {15'd0, e_lvl};
                    6'h14: s_axi_rdata <= {15'd0, e_ing};
                    6'h15: s_axi_rdata <= {14'd0, e_bass};
                    6'h16: s_axi_rdata <= {14'd0, e_treb};
                    6'h17: s_axi_rdata <= e_wow;
                    6'h18: s_axi_rdata <= {16'd0, e_damp};
                    6'h19: s_axi_rdata <= {15'd0, e_rev};
                    6'h1A: begin s_axi_rdata <= {7'd0, e_psat, e_peak}; rd_pk <= 1'b1; end
                    6'h1B: begin s_axi_rdata <= {7'd0, e_drive};        rd_dr <= 1'b1; end
                    default: s_axi_rdata <= 32'd0;
                    endcase
                end
            end
        end
    end

    // ------------------------------------------------------------------
    // Audio path
    // ------------------------------------------------------------------
    wire signed [DW-1:0] rx_l, rx_r;
    wire                 frame_tick;
    reg  signed [DW-1:0] tx_l, tx_r;

    i2s_master #(.HALF(HALF), .DW(DW)) u_i2s (
        .clk(aclk), .rst(rst),
        .bclk(bclk), .lrclk(lrclk), .sdata_o(sdata_o), .sdata_i(sdata_i),
        .tx_l(tx_l), .tx_r(tx_r), .rx_l(rx_l), .rx_r(rx_r),
        .frame_tick(frame_tick)
    );

    // input selection
    wire signed [DW:0] lr_sum = {rx_l[DW-1], rx_l} + {rx_r[DW-1], rx_r};
    reg  signed [DW-1:0] x_sel;
    always @(*) begin
        case (r_insel)
        2'd1:    x_sel = rx_r;
        2'd2:    x_sel = lr_sum[DW:1];
        default: x_sel = rx_l;
        endcase
    end

    wire signed [DW-1:0] y;
    wire                 y_done, y_sat, eng_busy;

    fir_engine #(.NLANE(NLANE), .LW(LW), .DW(DW), .CW(CW), .AW(48)) u_fir (
        .clk(aclk), .rst(rst),
        .start(frame_tick), .x_in(x_sel),
        .done(y_done), .y_out(y), .y_sat(y_sat),
        .busy(eng_busy), .overrun(overrun),
        .bank_req(r_bank), .shift0(r_shift0), .shift1(r_shift1),
        .bank_act(bank_act),
        .cw_en(cw_en), .cw_addr(cw_addr), .cw_data(cw_data)
    );

    // keep the dry sample aligned with the wet result
    reg signed [DW-1:0] x_dly;
    always @(posedge aclk)
        if (frame_tick) x_dly <= x_sel;

    // Space Echo: s = what used to go to the DAC (cab output, or dry in bypass)
    wire signed [DW-1:0] echo_s = r_bypass ? x_dly : y;
    wire signed [DW-1:0] echo_out;
    wire                 echo_done, echo_sat, echo_miss;
    wire        [24:0]   echo_rabs;

    tape_echo #(.SLEW(ECHO_SLEW)) u_echo (
        .clk(aclk), .rst(rst), .tick(frame_tick),
        .start(y_done), .s_in(echo_s),
        .en(e_ctrl[0]), .mask(e_ctrl[3:1]), .cancel(e_ctrl[4]),
        .delay(e_delay), .fb(e_fb), .level(e_lvl), .ingain(e_ing),
        .bass(e_bass), .treble(e_treb), .wow(e_wow[15:0]), .flut(e_wow[31:16]),
        .damp(e_damp),
        .busy(), .done(echo_done), .out(echo_out), .out_sat(echo_sat),
        .r_abs(echo_rabs), .miss(echo_miss)
    );

    // Spring reverb: s_lat = echo-stage input of the previous sample (see spring.v)
    reg  signed [DW-1:0] s_lat;
    always @(posedge aclk)
        if (rst) s_lat <= {DW{1'b0}};
        else if (y_done) s_lat <= echo_s;

    wire signed [24:0] spr_wet;
    wire               spr_done, spr_miss;
    spring u_spring (
        .clk(aclk), .rst(rst), .tick(frame_tick), .start(frame_tick),
        .s_in(s_lat), .rev_on(e_ctrl[5]), .rgain(e_rev),
        .busy(), .done(spr_done), .wet(spr_wet), .miss(spr_miss)
    );

    // final DAC word = sat24(echo_out + wet), two register stages (timing)
    reg signed [25:0]    o_sum;
    reg signed [DW-1:0]  fin_out;
    reg                  a_v, b_v, fin_sat;
    always @(posedge aclk) begin
        a_v <= echo_done && !rst;
        b_v <= a_v && !rst;
        o_sum <= {{2{echo_out[DW-1]}}, echo_out} + {spr_wet[24], spr_wet};
        if (a_v) begin
            fin_out <= (o_sum > 26'sd8388607)  ? 24'sh7FFFFF :
                       (o_sum < -26'sd8388608) ? 24'sh800000 : o_sum[DW-1:0];
            fin_sat <= echo_sat || (o_sum > 26'sd8388607) || (o_sum < -26'sd8388608);
        end
    end

    function [DW-1:0] absval;
        input signed [DW-1:0] v;
        begin
            if (v == {1'b1, {(DW-1){1'b0}}})
                absval = {1'b0, {(DW-1){1'b1}}};
            else
                absval = v[DW-1] ? -v : v;
        end
    endfunction

    localparam [DW-1:0] CLIP_LVL = 24'h7E0000;   // ~ -0.14 dBFS

    // meters run one cycle behind the audio path (registered |x|, |y|)
    // to keep input select + abs + compare out of a single clock cycle
    reg [DW-1:0] abs_x, abs_o;
    reg          m_in, m_e;
    always @(posedge aclk) begin
        abs_x     <= absval(x_sel);
        m_in      <= frame_tick && !rst;
        abs_o     <= absval(fin_out);
        m_e       <= b_v && !rst;
    end

    // level LEDs: input, and what actually goes to the DAC (after echo + reverb, 0 when muted)
    level_led u_led_in (.clk(aclk), .rst(rst), .valid(m_in), .level(abs_x), .led(lvl_in_pwm));
    level_led u_led_out (.clk(aclk), .rst(rst), .valid(m_e),
                         .level(r_mute ? {DW{1'b0}} : abs_o), .led(lvl_out_pwm));

    always @(posedge aclk) begin
        if (rst) begin
            tx_l        <= {DW{1'b0}};
            tx_r        <= {DW{1'b0}};
            st_in_clip  <= 1'b0;
            st_out_clip <= 1'b0;
            samples     <= 32'd0;
            peak_in     <= 24'd0;
            peak_out    <= 24'd0;
            e_peak      <= 24'd0;
            e_drive     <= 25'd0;
            e_psat      <= 1'b0;
            st_echo     <= 1'b0;
            st_spring   <= 1'b0;
        end else begin
            if (clr) begin
                st_in_clip  <= 1'b0;
                st_out_clip <= 1'b0;
                st_echo     <= 1'b0;
                st_spring   <= 1'b0;
                peak_in     <= 24'd0;
                peak_out    <= 24'd0;
            end

            if (echo_miss)
                st_echo <= 1'b1;
            if (spr_miss)
                st_spring <= 1'b1;

            // echo meters: a read clears them (a sample landing in the same cycle is kept)
            if (rd_pk) begin
                e_peak <= 24'd0;
                e_psat <= 1'b0;
            end
            if (rd_dr)
                e_drive <= 25'd0;
            if (m_e) begin
                if (!r_mute && (rd_pk ? abs_o > 24'd0 : abs_o > e_peak))
                    e_peak <= abs_o;
                if (fin_sat)
                    e_psat <= 1'b1;
                // output meter / clip flag: what is heard (muted = 0 is not counted)
                if (!r_mute) begin
                    if (fin_sat || abs_o >= CLIP_LVL)
                        st_out_clip <= 1'b1;
                    if (abs_o > peak_out && !clr)
                        peak_out <= abs_o;
                end
                if (rd_dr ? echo_rabs > 25'd0 : echo_rabs > e_drive)
                    e_drive <= echo_rabs;
            end

            if (frame_tick)
                samples <= samples + 1'b1;

            if (m_in) begin
                if (abs_x >= CLIP_LVL)
                    st_in_clip <= 1'b1;
                if (abs_x > peak_in && !clr)
                    peak_in <= abs_x;
            end

            if (b_v) begin
                if (r_mute) begin
                    tx_l <= {DW{1'b0}};
                    tx_r <= {DW{1'b0}};
                end else begin
                    tx_l <= fin_out;
                    tx_r <= r_left_only ? x_dly : fin_out;
                end
            end
        end
    end

    // ------------------------------------------------------------------
    // Capture ring: one frame = {dry x, wet = final DAC word}, written while REC = 1
    // ------------------------------------------------------------------
    reg  r_rec_q;
    wire rec_rise = r_rec && !r_rec_q;
    wire cap_we   = r_rec && !rec_rise && b_v;      // same strobe that loads tx_l; x_dly is still this frame's

    (* ram_style = "block" *)
    reg [2*DW-1:0] cap_mem [0:(1<<CAP_LOG2)-1];

    // REC LED: on for the first half of each blink period, restarted at REC start
    reg [BLINK_BIT:0] blink;
    always @(posedge aclk) begin
        if (rst || rec_rise)
            blink <= {(BLINK_BIT+1){1'b0}};
        else
            blink <= blink + 1'b1;
    end
    assign led_rec = r_rec && !blink[BLINK_BIT];
    wire lvl_in_c = lvl_in_pwm && !led_rec;
    assign led_rgb_r  = led_rec || (lvl_in_c && r_bypass && !e_ctrl[0]);
    assign led_lvl_in = lvl_in_c && (!e_ctrl[0] || r_bypass);
    assign led_rgb_b  = lvl_in_c && (e_ctrl[0] || r_bypass);
    wire lvl_out_c = lvl_out_pwm && !led_rec;
    assign led5_r = led_rec || (lvl_out_c && r_bypass && !e_ctrl[5]);
    assign led5_g = lvl_out_c && (!e_ctrl[5] || r_bypass);
    assign led5_b = lvl_out_c && (e_ctrl[5] || r_bypass);

    always @(posedge aclk) begin
        r_rec_q <= r_rec;
        if (rst || rec_rise)
            rec_wptr <= 32'd0;
        else if (cap_we)
            rec_wptr <= rec_wptr + 1'b1;
    end

    always @(posedge aclk) begin
        if (cap_we)
            cap_mem[rec_wptr[CAP_LOG2-1:0]] <= {x_dly, r_mute ? {DW{1'b0}} : fin_out};   // = tx_l
        cap_q <= cap_mem[s_axi_araddr[CAP_LOG2+2:3]];   // registered read
    end

endmodule
