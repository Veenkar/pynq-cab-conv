// level_led.v
// Signal-level LED: peak envelope (instant attack, exponential release) of
// |sample|, shown as PWM brightness that doubles every 6 dB:
//   envelope MSB 23 (>= -6 dBFS)  -> 100 %
//   envelope MSB 22               ->  50 %
//   ...
//   envelope MSB 15 (~ -48 dBFS)  -> 1/256, below that dark.
// PWM period 2^16 clocks (1.9 kHz at 122.88 MHz).

`timescale 1ns / 1ps

module level_led #(
    parameter DW  = 24,
    parameter REL = 12              // release: env -= env >> REL per sample (2^12 = 85 ms)
) (
    input  wire          clk,
    input  wire          rst,
    input  wire          valid,     // one pulse per sample
    input  wire [DW-1:0] level,     // |sample|
    output reg           led
);

    reg [DW-1:0] env;
    reg [4:0]    msb;
    reg [8:0]    duty;              // 0..256 of 256
    reg [15:0]   pwm;

    integer i;
    always @(posedge clk) begin
        if (rst) begin
            env  <= {DW{1'b0}};
            msb  <= 5'd0;
            duty <= 9'd0;
            pwm  <= 16'd0;
            led  <= 1'b0;
        end else begin
            if (valid)
                env <= (level > env) ? level : env - (env >> REL);

            // highest set bit of the envelope (0 when env = 0 or 1)
            msb <= 5'd0;
            for (i = 1; i < DW; i = i + 1)
                if (env[i]) msb <= i;

            duty <= (msb >= DW - 9) ? (9'd1 << (msb - (DW - 9))) : 9'd0;

            pwm <= pwm + 1'b1;
            led <= ({1'b0, pwm[15:8]} < duty);
        end
    end

endmodule
