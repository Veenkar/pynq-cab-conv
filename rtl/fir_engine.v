// fir_engine.v
// Time-multiplexed direct-form FIR for guitar cabinet IR convolution.
//
//  * NLANE parallel MAC lanes, each handling LTAPS consecutive taps
//    -> total taps = NLANE * LTAPS (default 4 * 2048 = 8192, ~170 ms @ 48 kHz)
//  * one new sample per `start` pulse; result after LTAPS + ~10 clock cycles
//  * zero algorithmic latency (y[n] uses x[n] .. x[n-NTAPS+1])
//  * two coefficient banks per lane (ping-pong): software writes the
//    inactive bank, then flips `bank_req`; the switch happens atomically
//    at the start of the next sample, together with the matching shift.
//
// Arithmetic (bit-exact with sw/cab_conv.py:fir_model):
//    x   : signed DW (24) bit
//    h   : signed CW (18) bit, block-floating-point: h_real = h / 2^shift
//    acc : per-lane signed AW (48) bit, wraps (DSP48 P register)
//    y   = sat_DW( (sum_lanes acc) >>> shift )      (arithmetic shift = floor)
//
// Delay line: each lane keeps a circular buffer of LTAPS samples. Lane k
// holds x[n-k*LTAPS] .. x[n-k*LTAPS-LTAPS+1]. When a new sample arrives the
// oldest sample of lane k (found at the new write position) is moved into
// lane k+1 at the same address, so all lanes share one address sequence.

`timescale 1ns / 1ps

module fir_engine #(
    parameter NLANE = 4,
    parameter LW    = 11,              // log2(taps per lane)
    parameter DW    = 24,              // sample width
    parameter CW    = 18,              // coefficient width
    parameter AW    = 48               // accumulator width
) (
    input  wire                         clk,
    input  wire                         rst,

    // processing
    input  wire                         start,      // 1-cycle pulse, new sample
    input  wire signed [DW-1:0]         x_in,
    output reg                          done,       // 1-cycle pulse, y_out valid
    output reg  signed [DW-1:0]         y_out,
    output reg                          y_sat,      // y_out was saturated
    output reg                          busy,
    output reg                          overrun,    // sticky: start while busy

    // bank control (bank_req sampled at start)
    input  wire                         bank_req,
    input  wire [5:0]                   shift0,
    input  wire [5:0]                   shift1,
    output reg                          bank_act,

    // coefficient write port (always writes the INACTIVE bank)
    input  wire                         cw_en,
    // width = LANE_BITS + LW (localparam can't be used before declaration)
    input  wire [((NLANE <= 1) ? 1 : $clog2(NLANE))+LW-1:0]
                                        cw_addr,    // tap index 0..NTAPS-1
    input  wire signed [CW-1:0]         cw_data
);

    localparam LTAPS     = 1 << LW;
    localparam LANE_BITS = (NLANE <= 1) ? 1 : $clog2(NLANE);
    localparam SW        = AW + LANE_BITS + 1;      // lane-sum width

    // ------------------------------------------------------------------
    // FSM
    // ------------------------------------------------------------------
    localparam S_IDLE  = 3'd0,
               S_RD1   = 3'd1,
               S_RD2   = 3'd2,
               S_MAC   = 3'd3,
               S_DRAIN = 3'd4,
               S_SUM   = 3'd5,
               S_SHIFT = 3'd6,
               S_SAT   = 3'd7;

    reg [2:0]              state;
    reg [LW-1:0]           wp;          // position of newest sample
    reg [LW:0]             j;           // tap counter within lane (one extra bit)
    reg signed [DW-1:0]    x_lat;
    reg [5:0]              shift_act;

    // shared read address for delay lines and coefficient memories
    reg [LW-1:0]           dl_raddr;
    reg [LW-1:0]           cf_raddr;
    reg                    dl_we;

    // pipeline valid bits
    reg v0, v1, v2, v3;

    // ------------------------------------------------------------------
    // Per-lane memories and MACs
    // ------------------------------------------------------------------
    wire signed [DW-1:0] dl_q   [0:NLANE-1];
    wire signed [AW-1:0] acc_w  [0:NLANE-1];

    genvar k;
    generate
        for (k = 0; k < NLANE; k = k + 1) begin : g_lane
            // ---- delay line (simple dual port, registered read) ----
            (* ram_style = "block" *)
            reg signed [DW-1:0] dl_mem [0:LTAPS-1];
            reg signed [DW-1:0] dl_rd;
            wire signed [DW-1:0] dl_wd;

            if (k == 0) begin : g_first
                assign dl_wd = x_lat;
            end else begin : g_next
                assign dl_wd = dl_q[k-1];
            end

            always @(posedge clk) begin
                if (dl_we)
                    dl_mem[wp] <= dl_wd;
                dl_rd <= dl_mem[dl_raddr];
            end
            assign dl_q[k] = dl_rd;

            // ---- coefficients: two banks (bank bit is the address MSB) ----
            (* ram_style = "block" *)
            reg signed [CW-1:0] cf_mem [0:2*LTAPS-1];
            reg signed [CW-1:0] cf_rd;

            wire cw_hit = cw_en && (cw_addr[LANE_BITS+LW-1:LW] == k);

            always @(posedge clk) begin
                if (cw_hit)
                    cf_mem[{~bank_act, cw_addr[LW-1:0]}] <= cw_data;
                cf_rd <= cf_mem[{bank_act, cf_raddr}];
            end

            // ---- MAC pipeline: A/B regs -> M reg -> P accumulator (DSP48) ----
            reg signed [DW-1:0]    a_r;
            reg signed [CW-1:0]    b_r;
            reg signed [DW+CW-1:0] m_r;
            reg signed [AW-1:0]    acc;

            always @(posedge clk) begin
                a_r <= dl_rd;
                b_r <= cf_rd;
                m_r <= a_r * b_r;
                if (state == S_RD1)
                    acc <= {AW{1'b0}};
                else if (v3)
                    acc <= acc + {{(AW-DW-CW){m_r[DW+CW-1]}}, m_r};
            end
            assign acc_w[k] = acc;

            // simulation / bitstream init: bank 0 = unity pass-through
            integer i;
            initial begin
                for (i = 0; i < LTAPS; i = i + 1)
                    dl_mem[i] = {DW{1'b0}};
                for (i = 0; i < 2*LTAPS; i = i + 1)
                    cf_mem[i] = {CW{1'b0}};
                if (k == 0)
                    cf_mem[0] = 18'sd65536;        // 1.0 with shift0 = 16
            end
        end
    endgenerate

    // ------------------------------------------------------------------
    // Lane sum (pipelined after the MAC finishes; not timing critical,
    // but registered stage by stage anyway)
    // ------------------------------------------------------------------
    reg signed [SW-1:0] total;
    reg signed [SW-1:0] shifted;

    integer n;
    reg signed [SW-1:0] sum_c;
    always @(*) begin
        sum_c = {SW{1'b0}};
        for (n = 0; n < NLANE; n = n + 1)
            sum_c = sum_c + {{(SW-AW){acc_w[n][AW-1]}}, acc_w[n]};
    end

    localparam signed [SW-1:0] YMAX =  (1 <<< (DW-1)) - 1;
    localparam signed [SW-1:0] YMIN = -(1 <<< (DW-1));

    // ------------------------------------------------------------------
    // Control
    // ------------------------------------------------------------------
    always @(posedge clk) begin
        if (rst) begin
            state     <= S_IDLE;
            wp        <= {LW{1'b0}};
            j         <= {(LW+1){1'b0}};
            dl_we     <= 1'b0;
            v0 <= 1'b0; v1 <= 1'b0; v2 <= 1'b0; v3 <= 1'b0;
            done      <= 1'b0;
            busy      <= 1'b0;
            overrun   <= 1'b0;
            bank_act  <= 1'b0;
            shift_act <= 6'd16;
            y_out     <= {DW{1'b0}};
            y_sat     <= 1'b0;
            x_lat     <= {DW{1'b0}};
            dl_raddr  <= {LW{1'b0}};
            cf_raddr  <= {LW{1'b0}};
        end else begin
            done  <= 1'b0;
            dl_we <= 1'b0;

            // pipeline valids
            v1 <= v0;
            v2 <= v1;
            v3 <= v2;

            if (start && busy)
                overrun <= 1'b1;

            case (state)
            S_IDLE: begin
                v0 <= 1'b0;
                if (start) begin
                    busy      <= 1'b1;
                    x_lat     <= x_in;
                    bank_act  <= bank_req;
                    shift_act <= bank_req ? shift1 : shift0;
                    wp        <= wp + 1'b1;
                    dl_raddr  <= wp + 1'b1;     // oldest sample of each lane
                    state     <= S_RD1;
                end
            end
            S_RD1: begin                        // memory read in flight
                state <= S_RD2;
            end
            S_RD2: begin                        // dl_q = oldest; shift lanes
                dl_we <= 1'b1;                  // write happens next edge at wp
                j     <= {(LW+1){1'b0}};
                state <= S_MAC;
            end
            S_MAC: begin
                // issue tap j: x[n-j] (lane-local) and h[j]
                dl_raddr <= wp - j[LW-1:0];
                cf_raddr <= j[LW-1:0];
                v0       <= 1'b1;
                j        <= j + 1'b1;
                if (j == LTAPS - 1)
                    state <= S_DRAIN;
            end
            S_DRAIN: begin
                v0 <= 1'b0;
                // wait until the last product has been accumulated
                if (!v0 && !v1 && !v2 && !v3)
                    state <= S_SUM;
            end
            S_SUM: begin
                total <= sum_c;
                state <= S_SHIFT;
            end
            S_SHIFT: begin
                shifted <= total >>> shift_act;
                state   <= S_SAT;
            end
            S_SAT: begin
                if (shifted > YMAX) begin
                    y_out <= YMAX[DW-1:0];
                    y_sat <= 1'b1;
                end else if (shifted < YMIN) begin
                    y_out <= YMIN[DW-1:0];
                    y_sat <= 1'b1;
                end else begin
                    y_out <= shifted[DW-1:0];
                    y_sat <= 1'b0;
                end
                done  <= 1'b1;
                busy  <= 1'b0;
                state <= S_IDLE;
            end
            default: state <= S_IDLE;
            endcase
        end
    end

endmodule
