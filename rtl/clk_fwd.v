// clk_fwd.v
// Forward a fabric-generated clock (here: codec MCLK) to a pin through an
// IOB ODDR, the recommended way on 7-series. Plain flop in simulation.

`timescale 1ns / 1ps

module clk_fwd (
    input  wire clk,
    input  wire d,
    output wire q
);
`ifdef SIMULATION
    reg r = 1'b0;
    always @(posedge clk) r <= d;
    assign q = r;
`else
    ODDR #(
        .DDR_CLK_EDGE ("SAME_EDGE"),
        .INIT         (1'b0),
        .SRTYPE       ("SYNC")
    ) u_oddr (
        .Q  (q),
        .C  (clk),
        .CE (1'b1),
        .D1 (d),
        .D2 (d),
        .R  (1'b0),
        .S  (1'b0)
    );
`endif
endmodule
