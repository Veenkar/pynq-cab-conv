// cab_conv_top.v
// Guitar cabinet IR convolver for PYNQ-Z2 (ADAU1761 codec).
//
// Single clock domain (aclk, nominally 122.88 MHz = 10 * MCLK).
// AXI4-Lite register map (byte addresses, 128 KiB window):
//
//   0x0000  ID        RO  0x43414256 ("CABV")
//   0x0004  PARAMS    RO  [15:0] taps total, [23:16] log2 taps/lane,
//                         [31:24] lanes
//   0x0008  CTRL      RW  [0] bypass (dry signal out)
//                         [1] mute
//                         [3:2] input: 0 = left, 1 = right, 2 = (L+R)/2
//                         [4] bank select (requested active coef bank)
//                         [5] output only on left channel (right = dry)
//                         [6] REC: recording (also toggled by BTN0)
//   0x000C  STATUS    RO  [0] active bank (applied)
//                         [1] input clip   (sticky)
//                         [2] output clip  (sticky)
//                         [3] engine overrun (sticky, should never be 1)
//   0x0010  SHIFT0    RW  [5:0] output shift for bank 0
//   0x0014  SHIFT1    RW  [5:0] output shift for bank 1
//   0x0018  CLEAR     WO  [0] clear sticky flags and peak meters
//   0x001C  SAMPLES   RO  sample counter (frames since reset)
//   0x0020  PEAK_IN   RO  max |x| since last clear (24-bit)
//   0x0024  PEAK_OUT  RO  max |y| since last clear (24-bit)
//   0x0028  REC_WPTR  RO  frames written since REC last went 0 -> 1 (free running)
//   0x8000+4*i  COEF[i]  WO  [17:0] signed tap i, written to the INACTIVE bank
//   0x10000+4*i CAPTURE[i] RO  ring of 2^CAP_LOG2 frames (8192 by default);
//                         word 2*k = dry x, word 2*k+1 = wet y of frame k
//                         (24-bit signed, sign-extended), k = frame mod 2^CAP_LOG2.
//                         Written only while REC = 1. x = selected engine input,
//                         y = engine output (independent of bypass/mute).
//
// The codec address pins are driven high (I2C address 0x3B, like PYNQ).

`timescale 1ns / 1ps

module cab_conv_top #(
    parameter NLANE = 4,
    parameter LW    = 11,           // 2048 taps per lane
    parameter HALF  = 20,           // aclk cycles per BCLK half period
    parameter CAP_LOG2 = 13,        // capture ring: 2^CAP_LOG2 frames
    parameter DEB_BITS = 22         // button debounce: 2^DEB_BITS stable cycles
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

    // record button / LED
    input  wire        btn_rec,
    output wire        led_rec
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

    reg        cw_en;
    reg [TBITS-1:0] cw_addr;
    reg signed [CW-1:0] cw_data;
    reg        clr;

    wire       bank_act, overrun;

    // ---------------- record button: 2-FF sync + debounce ----------------
    reg [1:0]          btn_sync;
    reg                btn_deb, btn_deb_q;
    reg [DEB_BITS-1:0] deb_cnt;
    always @(posedge aclk) begin
        btn_sync <= {btn_sync[0], btn_rec};
        if (rst) begin
            btn_deb   <= 1'b0;
            btn_deb_q <= 1'b0;
            deb_cnt   <= {DEB_BITS{1'b0}};
        end else begin
            btn_deb_q <= btn_deb;
            if (btn_sync[1] == btn_deb)
                deb_cnt <= {DEB_BITS{1'b0}};
            else begin
                deb_cnt <= deb_cnt + 1'b1;
                if (&deb_cnt) btn_deb <= btn_sync[1];
            end
        end
    end
    wire btn_press = btn_deb && !btn_deb_q;

    assign led_rec = r_rec;

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
            cw_en         <= 1'b0;
            cw_addr       <= {TBITS{1'b0}};
            cw_data       <= {CW{1'b0}};
            clr           <= 1'b0;
        end else begin
            cw_en <= 1'b0;
            clr   <= 1'b0;
            s_axi_awready <= 1'b0;
            s_axi_wready  <= 1'b0;

            if (btn_press)
                r_rec <= ~r_rec;

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
        end else begin
            s_axi_arready <= 1'b0;
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
                    6'h03: s_axi_rdata <= {28'd0, overrun, st_out_clip,
                                           st_in_clip, bank_act};
                    6'h04: s_axi_rdata <= {26'd0, r_shift0};
                    6'h05: s_axi_rdata <= {26'd0, r_shift1};
                    6'h07: s_axi_rdata <= samples;
                    6'h08: s_axi_rdata <= {8'd0, peak_in};
                    6'h09: s_axi_rdata <= {8'd0, peak_out};
                    6'h0A: s_axi_rdata <= rec_wptr;
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
    reg [DW-1:0] abs_x, abs_y;
    reg          m_in, m_out, m_out_sat;
    always @(posedge aclk) begin
        abs_x     <= absval(x_sel);
        abs_y     <= absval(y);
        m_in      <= frame_tick && !rst;
        m_out     <= y_done && !r_mute && !r_bypass && !rst;
        m_out_sat <= y_sat;
    end

    always @(posedge aclk) begin
        if (rst) begin
            tx_l        <= {DW{1'b0}};
            tx_r        <= {DW{1'b0}};
            st_in_clip  <= 1'b0;
            st_out_clip <= 1'b0;
            samples     <= 32'd0;
            peak_in     <= 24'd0;
            peak_out    <= 24'd0;
        end else begin
            if (clr) begin
                st_in_clip  <= 1'b0;
                st_out_clip <= 1'b0;
                peak_in     <= 24'd0;
                peak_out    <= 24'd0;
            end

            if (frame_tick)
                samples <= samples + 1'b1;

            if (m_in) begin
                if (abs_x >= CLIP_LVL)
                    st_in_clip <= 1'b1;
                if (abs_x > peak_in && !clr)
                    peak_in <= abs_x;
            end

            if (m_out) begin
                if (m_out_sat)
                    st_out_clip <= 1'b1;
                if (abs_y > peak_out && !clr)
                    peak_out <= abs_y;
            end

            if (y_done) begin
                if (r_mute) begin
                    tx_l <= {DW{1'b0}};
                    tx_r <= {DW{1'b0}};
                end else if (r_bypass) begin
                    tx_l <= x_dly;
                    tx_r <= x_dly;
                end else begin
                    tx_l <= y;
                    tx_r <= r_left_only ? x_dly : y;
                end
            end
        end
    end

    // ------------------------------------------------------------------
    // Capture ring: one frame = {dry x, wet y}, written while REC = 1
    // ------------------------------------------------------------------
    reg  r_rec_q;
    wire rec_rise = r_rec && !r_rec_q;
    wire cap_we   = r_rec && !rec_rise && y_done;   // x_dly belongs to this y

    (* ram_style = "block" *)
    reg [2*DW-1:0] cap_mem [0:(1<<CAP_LOG2)-1];

    always @(posedge aclk) begin
        r_rec_q <= r_rec;
        if (rst || rec_rise)
            rec_wptr <= 32'd0;
        else if (cap_we)
            rec_wptr <= rec_wptr + 1'b1;
    end

    always @(posedge aclk) begin
        if (cap_we)
            cap_mem[rec_wptr[CAP_LOG2-1:0]] <= {x_dly, y};
        cap_q <= cap_mem[s_axi_araddr[CAP_LOG2+2:3]];   // registered read
    end

endmodule
