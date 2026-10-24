// debounce.v
// Push-button input: 2-FF synchroniser, then the debounced state follows the
// input only after it has differed for 2^DEB_BITS consecutive cycles.
// `press` is a 1-cycle pulse on each debounced rising edge.

`timescale 1ns / 1ps

module debounce #(
    parameter DEB_BITS = 22         // 2^22 cycles = 34 ms at 122.88 MHz
) (
    input  wire clk,
    input  wire rst,
    input  wire btn,                // asynchronous, active high
    output wire press
);

    reg [1:0]          sync;
    reg                deb, deb_q;
    reg [DEB_BITS-1:0] cnt;

    always @(posedge clk) begin
        sync <= {sync[0], btn};
        if (rst) begin
            deb   <= 1'b0;
            deb_q <= 1'b0;
            cnt   <= {DEB_BITS{1'b0}};
        end else begin
            deb_q <= deb;
            if (sync[1] == deb)
                cnt <= {DEB_BITS{1'b0}};
            else begin
                cnt <= cnt + 1'b1;
                if (&cnt) deb <= sync[1];
            end
        end
    end

    assign press = deb && !deb_q;

endmodule
