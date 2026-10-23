// i2s_master.v
// I2S master for the ADAU1761 running in slave mode (as on PYNQ-Z2).
//
//  BCLK  = clk / (2*HALF)          (HALF = 20 @ 122.88 MHz -> 3.072 MHz)
//  LRCLK = BCLK / 64               (48 kHz), low = left channel
//  32-bit slots, 24-bit data, MSB one BCLK after the LRCLK edge (I2S),
//  data launched on BCLK falling edge, captured on BCLK rising edge.
//
//  MCLK for the codec is generated separately as clk/10 (256 * fs),
//  so BCLK/LRCLK are exactly synchronous with the codec core clock.
//
//  frame_tick pulses once per frame (at the left-slot start). At that
//  cycle rx_l/rx_r are updated with the previous frame and tx_l/tx_r
//  are latched for transmission in the current frame.

`timescale 1ns / 1ps

module i2s_master #(
    parameter HALF = 20,                // clk cycles per BCLK half period
    parameter DW   = 24
) (
    input  wire                 clk,
    input  wire                 rst,

    output reg                  bclk,
    output reg                  lrclk,
    output reg                  sdata_o,    // to codec DAC
    input  wire                 sdata_i,    // from codec ADC

    input  wire signed [DW-1:0] tx_l,
    input  wire signed [DW-1:0] tx_r,
    output reg  signed [DW-1:0] rx_l,
    output reg  signed [DW-1:0] rx_r,
    output reg                  frame_tick
);

    localparam PW   = $clog2(2*HALF);
    // sample codec data late in the BCLK high phase (after 2-FF sync)
    localparam SAMP = 2*HALF - 3;

    reg [PW-1:0]        ph;         // phase within one BCLK period
    reg [5:0]           bitn;       // bit slot within the frame (0..63)

    reg                 si_m, si_s; // input synchroniser
    reg [DW-1:0]        sh_l, sh_r; // receive shift registers
    reg [DW-1:0]        tl, tr;     // latched transmit words
    reg                 bclk_r, lrclk_r, sdo_r;

    // bit to transmit in slot b of the frame
    function txbit;
        input [5:0]    b;
        input [DW-1:0] wl;
        input [DW-1:0] wr;
        begin
            if (b >= 1 && b <= DW)
                txbit = wl[DW - b];
            else if (b >= 33 && b <= 32 + DW)
                txbit = wr[DW + 32 - b];
            else
                txbit = 1'b0;
        end
    endfunction

    always @(posedge clk) begin
        if (rst) begin
            ph         <= {PW{1'b0}};
            bitn       <= 6'd0;
            bclk_r     <= 1'b0;
            lrclk_r    <= 1'b0;
            sdo_r      <= 1'b0;
            si_m       <= 1'b0;
            si_s       <= 1'b0;
            sh_l       <= {DW{1'b0}};
            sh_r       <= {DW{1'b0}};
            tl         <= {DW{1'b0}};
            tr         <= {DW{1'b0}};
            rx_l       <= {DW{1'b0}};
            rx_r       <= {DW{1'b0}};
            frame_tick <= 1'b0;
        end else begin
            si_m <= sdata_i;
            si_s <= si_m;
            frame_tick <= 1'b0;

            // counters
            if (ph == 2*HALF - 1) begin
                ph   <= {PW{1'b0}};
                bitn <= bitn + 1'b1;
            end else begin
                ph <= ph + 1'b1;
            end

            // BCLK: low in the first half of the bit, high in the second
            bclk_r <= (ph >= HALF);

            // launch LRCLK and data at the start of each bit (falling BCLK)
            if (ph == 0) begin
                lrclk_r <= bitn[5];
                if (bitn == 6'd0) begin
                    // frame boundary: hand over received words, latch tx
                    rx_l       <= sh_l;
                    rx_r       <= sh_r;
                    frame_tick <= 1'b1;
                    tl         <= tx_l;
                    tr         <= tx_r;
                    sdo_r      <= 1'b0;             // slot 0 = I2S delay bit
                end else begin
                    sdo_r <= txbit(bitn, tl, tr);
                end
            end

            // capture codec data near the end of the BCLK high phase
            if (ph == SAMP) begin
                if (bitn >= 1 && bitn <= DW)
                    sh_l <= {sh_l[DW-2:0], si_s};
                else if (bitn >= 33 && bitn <= 32 + DW)
                    sh_r <= {sh_r[DW-2:0], si_s};
            end
        end
    end

    // dedicated output flops (single load each) so they pack into the IOBs;
    // all three pins are delayed by the same one cycle
    always @(posedge clk) begin
        if (rst) begin
            bclk    <= 1'b0;
            lrclk   <= 1'b0;
            sdata_o <= 1'b0;
        end else begin
            bclk    <= bclk_r;
            lrclk   <= lrclk_r;
            sdata_o <= sdo_r;
        end
    end

endmodule
