// tape_echo.v
// "Space Echo" tape-echo effect (RE-201 style), sequential and time-multiplexed.
//
// Bit-exact with sw/cab_conv.py:echo_model, spec in docs/space_echo.md.
// One new sample per `start` pulse; `done` pulses ~50 clock cycles later with
// `out` valid. All multiplies go through one shared signed 25x18 multiplier
// (inferred DSP48, operand/product/result registers), issued at state n and
// collected at state n+3. The tape is a 2^ELOG x 24 inferred BRAM with a
// registered read. The register inputs are copied at `start`.

`timescale 1ns / 1ps

module tape_echo #(
    parameter ELOG = 15,                // tape length 2^ELOG samples
    parameter SLEW = 13                 // speed glide time constant 2^SLEW samples (sim: smaller)
) (
    input  wire                clk,
    input  wire                rst,
    input  wire                tick,    // frame tick, only used for the overrun flag
    input  wire                start,   // 1-cycle pulse, s_in valid
    input  wire signed [23:0]  s_in,

    input  wire                en,
    input  wire [2:0]          mask,    // playback heads 1..3
    input  wire                cancel,
    input  wire [31:0]         delay,   // Q16.16 samples
    input  wire [16:0]         fb,      // Q16
    input  wire [16:0]         level,   // Q16
    input  wire [16:0]         ingain,  // Q16
    input  wire signed [17:0]  bass,    // signed Q16
    input  wire signed [17:0]  treble,
    input  wire [15:0]         wow,     // Q8 samples
    input  wire [15:0]         flut,
    input  wire [15:0]         damp,    // Q16

    output reg                 busy,
    output reg                 done,    // 1-cycle pulse, out valid
    output reg signed [23:0]   out,
    output reg                 out_sat, // out saturated (valid with done, held)
    output reg  [24:0]         r_abs,   // |record signal| before the soft clip (held)
    output wire                miss     // tick while still busy
);

    localparam [31:0] W_INC = 32'd53687;            // wow 0.6 Hz
    localparam [31:0] F_INC = 32'd581601;           // flutter 6.5 Hz
    localparam [35:0] TMIN  = 36'd16 << 16;
    localparam [35:0] TMAX  = (((1 << ELOG) - 4) / 3) << 16;
    localparam [15:0] HPK   = 16'd683;              // ~80 Hz
    localparam [15:0] BLK   = 16'd2111;             // ~250 Hz
    localparam [15:0] TLK   = 16'd18307;            // ~2.5 kHz
    localparam [17:0] C23   = 18'd43691;            // 2/3 in Q16
    localparam [31:0] DELAY_RST = 32'd4800 << 16;

    assign miss = tick && busy;

    function signed [23:0] sat24;
        input signed [35:0] v;
        begin
            if (v > 36'sd8388607)       sat24 = 24'sh7FFFFF;
            else if (v < -36'sd8388608) sat24 = 24'sh800000;
            else                        sat24 = v[23:0];
        end
    endfunction

    // ---------------- shared multiplier: issue at state n, result in mp at n+3 ----
    reg signed [24:0] ma;
    reg signed [17:0] mb;
    reg signed [42:0] mm, mp;
    always @(posedge clk) begin
        mm <= ma * mb;
        mp <= mm;
    end

    // ---------------- tape memory ----------------
    (* ram_style = "block" *)
    reg [23:0] mem [0:(1<<ELOG)-1];
    reg                 mem_we;
    reg [ELOG-1:0]      mem_wa, mem_ra;
    reg [23:0]          mem_wd;
    reg signed [23:0]   mem_q;
    integer             mi;
    initial for (mi = 0; mi < (1<<ELOG); mi = mi + 1) mem[mi] = 24'd0;
    always @(posedge clk) begin
        if (mem_we) mem[mem_wa] <= mem_wd;
        mem_q <= mem[mem_ra];
    end

    // ---------------- state ----------------
    reg signed [23:0] s_q;
    reg               c_en, c_cancel;
    reg [2:0]         c_mask;
    reg [31:0]        c_delay;
    reg [16:0]        c_fb, c_lvl, c_ing;
    reg signed [17:0] c_bass, c_treb;
    reg [15:0]        c_wow, c_flut, c_damp;

    reg [31:0]        dcur, pw, pf;
    reg [ELOG-1:0]    wp;
    reg signed [23:0] lp, hz, bl, tl;
    reg [5:0]         st;

    // slew of the tape speed, wow/flutter triangles
    wire signed [33:0] dd  = $signed({2'b00, c_delay}) - $signed({2'b00, dcur});
    wire signed [33:0] dds = dd >>> SLEW;
    wire [16:0] uw = pw[31:15];
    wire [16:0] uf = pf[31:15];
    wire [15:0] tw_u = uw[16] ? ~uw[15:0] : uw[15:0];
    wire [15:0] tf_u = uf[16] ? ~uf[15:0] : uf[15:0];
    wire signed [15:0] tri_w = {~tw_u[15], tw_u[14:0]};
    wire signed [15:0] tri_f = {~tf_u[15], tf_u[14:0]};

    // temporaries
    reg signed [34:0] modsum;
    reg signed [35:0] tsum;
    reg        [30:0] tt, d1, d2, d3;
    reg signed [23:0] a1, a2, a3, v1, v2, v3;
    reg signed [25:0] esum;
    reg signed [23:0] e, f, eo;
    reg signed [24:0] inp, lev;
    reg signed [26:0] bpart, tpart, rsum;
    reg signed [24:0] r;
    reg signed [23:0] t, t2, t3, yv;
    reg signed [25:0] o;

    wire signed [26:0] tq = mp[42:16];              // (r * C23) >> 16
    wire signed [35:0] tclamp_lo = TMIN;
    wire signed [35:0] tclamp_hi = TMAX;

    always @(posedge clk) begin
        if (rst) begin
            busy    <= 1'b0;
            done    <= 1'b0;
            out     <= 24'sd0;
            out_sat <= 1'b0;
            r_abs   <= 25'd0;
            mem_we  <= 1'b0;
            wp      <= {ELOG{1'b0}};
            dcur    <= DELAY_RST;
            pw      <= 32'd0;
            pf      <= 32'd0;
            lp      <= 24'sd0;
            hz      <= 24'sd0;
            bl      <= 24'sd0;
            tl      <= 24'sd0;
            st      <= 6'd0;
        end else begin
            done   <= 1'b0;
            mem_we <= 1'b0;

            if (start && !busy) begin
                busy     <= 1'b1;
                st       <= 6'd0;
                s_q      <= s_in;
                c_en     <= en;
                c_cancel <= cancel;
                c_mask   <= mask;
                c_delay  <= delay;
                c_fb     <= fb;
                c_lvl    <= level;
                c_ing    <= ingain;
                c_bass   <= bass;
                c_treb   <= treble;
                c_wow    <= wow;
                c_flut   <= flut;
                c_damp   <= damp;
            end else if (busy) begin
                st <= st + 1'b1;
                case (st)
                // ---- tape speed glide, wow / flutter modulation ----
                6'd0: begin
                    dcur <= dcur + dds[31:0];
                    pw   <= pw + W_INC;
                    ma   <= tri_w;
                    mb   <= {2'b00, c_wow};
                end
                6'd1: begin
                    pf <= pf + F_INC;
                    ma <= tri_f;
                    mb <= {2'b00, c_flut};
                end
                6'd3: modsum <= mp[34:0];
                6'd4: modsum <= modsum + mp[34:0];
                6'd5: tsum <= $signed({4'b0000, dcur}) + (modsum >>> 7);
                6'd6: tt <= (tsum < tclamp_lo) ? TMIN[30:0] :
                            (tsum > tclamp_hi) ? TMAX[30:0] : tsum[30:0];
                6'd7: begin
                    d1 <= tt;
                    d2 <= {tt[29:0], 1'b0};
                    d3 <= tt + {tt[29:0], 1'b0};
                end
                // ---- playback heads: two reads per head, linear interpolation ----
                6'd8:  mem_ra <= wp - d1[16 +: ELOG];
                6'd9:  mem_ra <= wp - d1[16 +: ELOG] - 1'b1;
                6'd10: begin
                    mem_ra <= wp - d2[16 +: ELOG];
                    a1     <= mem_q;
                end
                6'd11: begin
                    mem_ra <= wp - d2[16 +: ELOG] - 1'b1;
                    ma     <= mem_q - a1;
                    mb     <= {2'b00, d1[15:0]};
                end
                6'd12: begin
                    mem_ra <= wp - d3[16 +: ELOG];
                    a2     <= mem_q;
                    ma     <= s_q;                      // record level
                    mb     <= {1'b0, c_ing};
                end
                6'd13: begin
                    mem_ra <= wp - d3[16 +: ELOG] - 1'b1;
                    ma     <= mem_q - a2;
                    mb     <= {2'b00, d2[15:0]};
                end
                6'd14: begin
                    a3 <= mem_q;
                    v1 <= a1 + (mp >>> 16);
                end
                6'd15: begin
                    ma  <= mem_q - a3;
                    mb  <= {2'b00, d3[15:0]};
                    inp <= (c_cancel || !c_en) ? 25'sd0 : (mp >>> 16);
                end
                6'd16: v2 <= a2 + (mp >>> 16);
                6'd18: v3 <= a3 + (mp >>> 16);
                6'd19: esum <= (c_mask[0] ? v1 : 24'sd0) + (c_mask[1] ? v2 : 24'sd0) +
                               (c_mask[2] ? v3 : 24'sd0);
                6'd20: e <= sat24(esum);
                // ---- tape lowpass, bass / treble filters ----
                6'd21: begin ma <= e - lp; mb <= {2'b00, c_damp}; end
                6'd22: begin ma <= e - bl; mb <= {2'b00, BLK};    end
                6'd23: begin ma <= e - tl; mb <= {2'b00, TLK};    end
                6'd24: lp <= lp + (mp >>> 16);
                6'd25: begin
                    bl <= bl + (mp >>> 16);
                    ma <= lp - hz;
                    mb <= {2'b00, HPK};
                end
                6'd26: tl <= tl + (mp >>> 16);
                6'd27: begin ma <= bl; mb <= c_bass; end
                6'd28: begin
                    hz <= hz + (mp >>> 16);
                    ma <= sat24(e - tl);
                    mb <= c_treb;
                end
                6'd29: f <= sat24(lp - hz);
                6'd30: begin
                    bpart <= mp >>> 16;
                    ma    <= f;
                    mb    <= {1'b0, c_fb};
                end
                6'd31: tpart <= mp >>> 16;
                6'd32: eo <= sat24(e + bpart + tpart);
                // ---- feedback sum, tape saturation ----
                6'd33: begin
                    rsum <= inp + (mp >>> 16);
                    ma   <= eo;
                    mb   <= {1'b0, c_lvl};
                end
                6'd34: r <= (rsum > 27'sd16777215) ? 25'sd16777215 :
                            (rsum < -27'sd16777215) ? -25'sd16777215 : rsum[24:0];
                6'd35: begin
                    r_abs <= r[24] ? -r : r;
                    ma    <= r;
                    mb    <= C23;
                end
                6'd36: lev <= mp >>> 16;
                6'd38: t <= (tq > 27'sd8388607)  ? 24'sd8388607 :
                            (tq < -27'sd8388607) ? -24'sd8388607 : tq[23:0];
                6'd39: begin ma <= t; mb <= t[23:6]; end
                6'd42: t2 <= mp >>> 17;
                6'd43: begin ma <= t; mb <= t2[23:6]; end
                6'd46: t3 <= mp >>> 17;
                6'd47: yv <= sat24((3 * t - t3) >>> 1);
                // ---- write tape, mix ----
                6'd48: begin
                    mem_we <= 1'b1;
                    mem_wa <= wp;
                    mem_wd <= c_en ? yv : 24'd0;
                    wp     <= wp + 1'b1;
                    o      <= c_en ? (s_q + lev) : s_q;
                end
                6'd49: begin
                    out     <= sat24(o);
                    out_sat <= (o > 26'sd8388607) || (o < -26'sd8388608);
                    done    <= 1'b1;
                    busy    <= 1'b0;
                end
                default: ;
                endcase
            end
        end
    end

endmodule
