// spring.v
// Spring reverb (RE-201 style): two dispersive spring models, time-shared on one
// engine with one shared signed 25x18 multiplier. Bit-exact with
// sw/cab_conv.py:spring_model, spec in docs/spring.md.
//
// One step per `start` pulse (the frame tick). It consumes `s_in` (the echo-stage input of
// the PREVIOUS sample, held by the caller) and runs in parallel with the FIR. Early in the
// step it also forms wet = (W[n-1] * rgain) >> 16 (W[n-1] = result of the previous step),
// valid a few cycles after `start` and held until the next step. `done` pulses when the
// step is finished (see docs/spring.md for the cycle count). Memories are inferred BRAM
// with registered reads.
//
// Multiplier: operands registered at state n, product registers mm/mp, result readable
// in mp at state n+3 (same scheme as tape_echo.v).
//   allpass state ST: word S[j][r] at base + 4*j + r (r = n mod K, K = 4), j = 0..M
//                     (S[0] = ring of x0, S[j] = output of section j-1)
//   delay lines DL:   {spring, n mod 1024}

`timescale 1ns / 1ps

module spring (
    input  wire                clk,
    input  wire                rst,
    input  wire                tick,    // frame tick, only used for the overrun flag
    input  wire                start,   // 1-cycle pulse
    input  wire signed [23:0]  s_in,    // echo-stage input of the previous sample
    input  wire                rev_on,  // spring input enable (sampled at start)
    input  wire [16:0]         rgain,   // REVERB, Q16

    output reg                 busy,
    output reg                 done,    // 1-cycle pulse, W updated
    output reg signed [24:0]   wet,     // (W[n-1] * rgain) >> 16
    output wire                miss     // tick while still busy
);

    localparam signed [17:0] INK = 18'sd32768;      // SPRING_INK, Q16
    localparam [17:0] HPK  = 18'd683;               // SPRING_HPK
    localparam [17:0] GK   = 18'd61604;             // loop gain, both springs
    localparam [17:0] CK   = 18'd39322;             // loop lowpass, both springs
    localparam [17:0] MODD = 18'd96;
    // per spring (0 / 1)
    localparam [6:0]  M0 = 7'd72,  M1 = 7'd64;
    localparam signed [17:0] A0 = -18'sd81265, A1 = -18'sd75367;
    localparam [9:0]  D0 = 10'd600, D1 = 10'd700;
    localparam [31:0] INC0 = 32'd80530, INC1 = 32'd116319;
    localparam [9:0]  BASE1 = 10'd292;              // (M0 + 1) * 4

    assign miss = tick && busy;

    function signed [23:0] sat24;
        input signed [35:0] v;
        begin
            if (v > 36'sd8388607)       sat24 = 24'sh7FFFFF;
            else if (v < -36'sd8388608) sat24 = 24'sh800000;
            else                        sat24 = v[23:0];
        end
    endfunction

    // ---------------- shared multiplier ----------------
    reg signed [24:0] ma;
    reg signed [17:0] mb;
    reg signed [42:0] mm, mp;
    always @(posedge clk) begin
        mm <= ma * mb;
        mp <= mm;
    end

    // ---------------- memories ----------------
    (* ram_style = "block" *)
    reg [23:0] st_mem [0:1023];
    reg                 st_we;
    reg [9:0]           st_wa, st_ra;
    reg [23:0]          st_wd;
    reg signed [23:0]   st_q;
    (* ram_style = "block" *)
    reg [23:0] dl_mem [0:2047];
    reg                 dl_we;
    reg [10:0]          dl_wa, dl_ra;
    reg [23:0]          dl_wd;
    reg signed [23:0]   dl_q;
    integer mi;
    initial begin
        for (mi = 0; mi < 1024; mi = mi + 1) st_mem[mi] = 24'd0;
        for (mi = 0; mi < 2048; mi = mi + 1) dl_mem[mi] = 24'd0;
    end
    always @(posedge clk) begin
        if (st_we) st_mem[st_wa] <= st_wd;
        st_q <= st_mem[st_ra];
        if (dl_we) dl_mem[dl_wa] <= dl_wd;
        dl_q <= dl_mem[dl_ra];
    end

    // ---------------- state ----------------
    reg [5:0]          st;
    reg                sp;                  // 0: spring 1, 1: spring 2
    reg [9:0]          wp;                  // n mod 1024
    reg signed [23:0]  c_in, c_sin, wcur, u1, u_q;
    reg signed [23:0]  x, xd, xdn, a_q, lp1, hz1, lp2, hz2, f;
    reg [31:0]         ph1, ph2, d;
    reg [6:0]          m;
    reg [9:0]          wa_r;
    reg signed [25:0]  q;

    wire [6:0]         mlast = (sp ? M1 : M0) - 7'd1;
    wire [9:0]         base  = sp ? BASE1 : 10'd0;
    wire [31:0]        ph    = sp ? ph2 : ph1;
    wire [1:0]         r     = wp[1:0];
    wire signed [23:0] lp    = sp ? lp2 : lp1;
    wire signed [23:0] hz    = sp ? hz2 : hz1;

    // triangle LFO, 16-bit signed
    wire [16:0] uu   = ph[31:15];
    wire [15:0] tu   = uu[16] ? ~uu[15:0] : uu[15:0];
    wire signed [15:0] tri_v = {~tu[15], tu[14:0]};

    wire signed [41:0] sin_full = c_in * INK;       // (s * SPRING_INK), >> 16 below
    wire signed [23:0] sin_w = sin_full[39:16];

    wire signed [23:0] x0v   = a_q + mp[39:16];
    wire signed [23:0] ysat  = sat24($signed({{12{xd[23]}}, xd}) + $signed({{10{q[25]}}, q}));
    wire signed [24:0] wsum  = $signed({u1[23], u1}) + $signed({u_q[23], u_q});
    wire signed [35:0] dlsum = $signed({{12{c_sin[23]}}, c_sin}) + $signed({{9{mp[42]}}, mp[42:16]});

    localparam [5:0] S_IDLE = 6'd0,
                     I0 = 6'd1,  I1 = 6'd2,  I2 = 6'd3,  I3 = 6'd4,  I4 = 6'd5,  I5 = 6'd6,
                     I6 = 6'd7,  I7 = 6'd8,  I8 = 6'd9,  I9 = 6'd10, I10 = 6'd11,
                     S0 = 6'd12, S1 = 6'd13, S2 = 6'd14, S3 = 6'd15, S4 = 6'd16,
                     P0 = 6'd17, P1 = 6'd18, P2 = 6'd19, P3 = 6'd20, P4 = 6'd21, P5 = 6'd22,
                     P6 = 6'd23, P7 = 6'd24, P8 = 6'd25, P9 = 6'd26, P10 = 6'd27,
                     P11 = 6'd28, P12 = 6'd29;

    always @(posedge clk) begin
        if (rst) begin
            busy  <= 1'b0;
            done  <= 1'b0;
            wet   <= 25'sd0;
            st    <= S_IDLE;
            sp    <= 1'b0;
            wp    <= 10'd0;
            wcur  <= 24'sd0;
            lp1   <= 24'sd0; hz1 <= 24'sd0; lp2 <= 24'sd0; hz2 <= 24'sd0;
            ph1   <= 32'd0;  ph2 <= 32'd0;
            st_we <= 1'b0;
            dl_we <= 1'b0;
        end else begin
            done  <= 1'b0;
            st_we <= 1'b0;
            dl_we <= 1'b0;

            case (st)
            S_IDLE: if (start) begin
                busy <= 1'b1;
                c_in <= rev_on ? s_in : 24'sd0;
                ma   <= {wcur[23], wcur};               // wet product, collected in I2
                mb   <= {1'b0, rgain};
                sp   <= 1'b0;
                st   <= I0;
            end

            // ---- per spring setup: LFO, delay-line read with interpolation ----
            I0: begin
                ma    <= {{9{tri_v[15]}}, tri_v};
                mb    <= MODD;
                if (sp) ph2 <= ph2 + INC1; else ph1 <= ph1 + INC0;
                st_ra <= base + r;                      // S[0][r]
                c_sin <= sin_w;                         // (c_in * INK) >> 16
                st    <= I1;
            end
            I1: begin
                st_ra <= base + 10'd4 + r;              // S[1][r]
                wa_r  <= base + 10'd4 + r;
                st    <= I2;
            end
            I2: begin
                xd <= st_q;                             // old S[0][r]
                if (!sp) wet <= mp[40:16];
                st <= I3;
            end
            I3: begin
                d  <= {(sp ? D1 : D0), 16'd0} + mp[35:4];
                st <= I4;
            end
            I4: begin
                dl_ra <= {sp, wp - d[25:16]};
                st    <= I5;
            end
            I5: begin
                dl_ra <= {sp, wp - d[25:16] - 10'd1};
                st    <= I6;
            end
            I6: begin
                a_q <= dl_q;
                st  <= I7;
            end
            I7: begin
                ma <= {dl_q[23], dl_q} - {a_q[23], a_q};
                mb <= {2'b00, d[15:0]};
                st <= I8;
            end
            I8: st <= I9;
            I9: st <= I10;
            I10: begin
                x     <= x0v;
                st_we <= 1'b1;
                st_wa <= base + r;
                st_wd <= x0v;
                m     <= 7'd0;
                st    <= S0;
            end

            // ---- allpass sections, 5 cycles each ----
            S0: begin
                ma  <= {x[23], x} - {st_q[23], st_q};
                mb  <= sp ? A1 : A0;
                xdn <= st_q;
                st  <= S1;
            end
            S1: begin
                st_ra <= wa_r + 10'd4;
                st    <= S2;
            end
            S2: st <= S3;
            S3: begin
                q  <= (mp + 43'sd65536) >>> 17;
                st <= S4;
            end
            S4: begin
                x     <= ysat;
                st_we <= 1'b1;
                st_wa <= wa_r;
                st_wd <= ysat;
                xd    <= xdn;
                wa_r  <= wa_r + 10'd4;
                if (m == mlast) st <= P0;
                else begin
                    m  <= m + 7'd1;
                    st <= S0;
                end
            end

            // ---- loop filters, feedback into the delay line ----
            P0: begin
                u_q <= x;
                ma  <= {x[23], x} - {lp[23], lp};
                mb  <= CK;
                st  <= P1;
            end
            P1: st <= P2;
            P2: st <= P3;
            P3: begin
                if (sp) lp2 <= lp2 + mp[39:16]; else lp1 <= lp1 + mp[39:16];
                st <= P4;
            end
            P4: begin
                ma <= {lp[23], lp} - {hz[23], hz};
                mb <= HPK;
                st <= P5;
            end
            P5: st <= P6;
            P6: st <= P7;
            P7: begin
                if (sp) hz2 <= hz2 + mp[39:16]; else hz1 <= hz1 + mp[39:16];
                st <= P8;
            end
            P8: begin
                f  <= sat24({{12{lp[23]}}, lp} - {{12{hz[23]}}, hz});
                st <= P9;
            end
            P9: begin
                ma <= {f[23], f};
                mb <= GK;
                st <= P10;
            end
            P10: st <= P11;
            P11: st <= P12;
            P12: begin
                dl_we <= 1'b1;
                dl_wa <= {sp, wp};
                dl_wd <= sat24(dlsum);
                if (!sp) begin
                    u1 <= u_q;
                    sp <= 1'b1;
                    st <= I0;
                end else begin
                    wcur <= wsum[24:1];
                    wp   <= wp + 10'd1;
                    busy <= 1'b0;
                    done <= 1'b1;
                    st   <= S_IDLE;
                end
            end
            default: st <= S_IDLE;
            endcase
        end
    end

endmodule
