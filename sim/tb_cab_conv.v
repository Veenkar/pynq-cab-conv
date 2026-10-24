// tb_cab_conv.v
// Testbench: AXI4-Lite master BFM + ADAU1761 I2S slave model.
// Driven by sim/run_sim.py, which generates stimulus files and checks the
// captured output bit-exactly against the Python model.
//
// Sequence (frame counts from plusargs):
//   reset -> F0 frames with power-on IR (bank 0 = unity)
//   load IR A into bank 1, switch            -> F1 frames
//   load IR B into bank 0, switch mid-stream -> F2 frames
//   record into the capture ring (wraps), stop, dump ring to cap.txt
//   record button: glitch, bouncing press / release x2, led_rec blinks only while REC
//   bypass button (during mute): press x2 toggles CTRL[0], led_cab check
//   level LEDs (PWM) lit while audio plays
//   bypass on                                -> F3 frames
//   mute on                                  -> until input runs out

`timescale 1ns / 1ps

module tb_cab_conv;

`ifndef NLANE
`define NLANE 4
`endif
`ifndef LW
`define LW 6
`endif
`ifndef HALF
`define HALF 10
`endif
`ifndef CAP_LOG2
`define CAP_LOG2 5
`endif
`ifndef BLINK_BIT
`define BLINK_BIT 3
`endif
`ifndef DEB_BITS
`define DEB_BITS 4
`endif

    localparam NLANE = `NLANE;
    localparam LW    = `LW;
    localparam HALF  = `HALF;
    localparam CAP_LOG2 = `CAP_LOG2;
    localparam CAPN  = 1 << CAP_LOG2;       // frames in the capture ring
    localparam DEB_BITS = `DEB_BITS;
    localparam BLINK_BIT = `BLINK_BIT;
    localparam NTAPS = NLANE << LW;
    localparam MAXN  = 65536;

    reg aclk = 0;
    always #4.069 aclk = ~aclk;           // ~122.88 MHz
    reg aresetn = 0;

    // AXI
    reg  [16:0] awaddr = 0;  reg awvalid = 0; wire awready;
    reg  [31:0] wdata = 0;   reg wvalid = 0;  wire wready;
    wire [1:0]  bresp;       wire bvalid;     reg bready = 0;
    reg  [16:0] araddr = 0;  reg arvalid = 0; wire arready;
    wire [31:0] rdata;       wire [1:0] rresp; wire rvalid; reg rready = 0;

    wire mclk, bclk, lrclk, sdata_o;
    reg  sdata_i = 0;
    wire [1:0] codec_addr;
    reg  btn_rec = 0;
    wire led_rec;
    reg  btn_byp = 0;
    wire led_cab;
    wire led_lvl_in, led_lvl_out;
    integer lvl_in_on = 0, lvl_out_on = 0;
    always @(posedge aclk) begin
        if (led_lvl_in)  lvl_in_on  = lvl_in_on + 1;
        if (led_lvl_out) lvl_out_on = lvl_out_on + 1;
    end

    cab_conv_top #(.NLANE(NLANE), .LW(LW), .HALF(HALF),
                   .CAP_LOG2(CAP_LOG2), .DEB_BITS(DEB_BITS), .BLINK_BIT(BLINK_BIT)) dut (
        .aclk(aclk), .aresetn(aresetn),
        .s_axi_awaddr(awaddr), .s_axi_awprot(3'b000), .s_axi_awvalid(awvalid),
        .s_axi_awready(awready),
        .s_axi_wdata(wdata), .s_axi_wstrb(4'hF), .s_axi_wvalid(wvalid),
        .s_axi_wready(wready),
        .s_axi_bresp(bresp), .s_axi_bvalid(bvalid), .s_axi_bready(bready),
        .s_axi_araddr(araddr), .s_axi_arprot(3'b000), .s_axi_arvalid(arvalid),
        .s_axi_arready(arready),
        .s_axi_rdata(rdata), .s_axi_rresp(rresp), .s_axi_rvalid(rvalid),
        .s_axi_rready(rready),
        .mclk(mclk), .bclk(bclk), .lrclk(lrclk), .sdata_o(sdata_o),
        .sdata_i(sdata_i), .codec_addr(codec_addr),
        .btn_rec(btn_rec), .led_rec(led_rec),
        .btn_byp(btn_byp), .led_cab(led_cab),
        .led_lvl_in(led_lvl_in), .led_lvl_out(led_lvl_out)
    );

    // ------------------------------------------------------------------
    // AXI BFM
    // ------------------------------------------------------------------
    task axi_write(input [16:0] a, input [31:0] d);
        begin
            @(posedge aclk);
            awaddr <= a; wdata <= d; awvalid <= 1; wvalid <= 1;
            @(posedge aclk);
            while (!(awready && wready)) @(posedge aclk);
            awvalid <= 0; wvalid <= 0; bready <= 1;
            while (!bvalid) @(posedge aclk);
            @(posedge aclk);
            bready <= 0;
        end
    endtask

    task axi_read(input [16:0] a, output [31:0] d);
        begin
            @(posedge aclk);
            araddr <= a; arvalid <= 1;
            @(posedge aclk);
            while (!arready) @(posedge aclk);
            arvalid <= 0; rready <= 1;
            while (!rvalid) @(posedge aclk);
            d = rdata;
            @(posedge aclk);
            rready <= 0;
        end
    endtask

    // ------------------------------------------------------------------
    // Stimulus files
    // ------------------------------------------------------------------
    reg [23:0] inl [0:MAXN-1];
    reg [23:0] inr [0:MAXN-1];
    reg [17:0] coefa [0:NTAPS-1];
    reg [17:0] coefb [0:NTAPS-1];
    integer nin, sha, shb, f0, f1, f2, f3;
    integer fout;

    // ------------------------------------------------------------------
    // ADAU1761 model (I2S slave): drives ADC data after BCLK falling,
    // samples DAC data on BCLK rising.
    // ------------------------------------------------------------------
    integer slot = 0;
    reg     lr_prev = 0;
    integer fidx = 0;           // index of the frame being sent
    reg [23:0] cur_l = 0, cur_r = 0;
    reg [23:0] cap_l = 0, cap_r = 0;
    integer frames_out = 0;
    reg     started = 0;

    always @(negedge bclk) begin
        #2;
        if (lrclk !== lr_prev) begin
            slot = 0;
            lr_prev = lrclk;
            if (lrclk == 1'b0) begin
                // new frame: next ADC sample
                if (fidx < nin) begin
                    cur_l = inl[fidx];
                    cur_r = inr[fidx];
                end else begin
                    cur_l = 0; cur_r = 0;
                end
                fidx = fidx + 1;
                started = 1;
            end
        end else begin
            slot = slot + 1;
        end
        #8;                      // codec tCO
        if (slot >= 1 && slot <= 24)
            sdata_i = lrclk ? cur_r[24 - slot] : cur_l[24 - slot];
        else
            sdata_i = 1'b0;
    end

    always @(posedge bclk) begin
        #2;
        if (started && slot >= 1 && slot <= 24) begin
            if (lrclk == 1'b0) cap_l = {cap_l[22:0], sdata_o};
            else               cap_r = {cap_r[22:0], sdata_o};
            if (lrclk == 1'b1 && slot == 24) begin
                $fdisplay(fout, "%0d %0d", $signed(cap_l), $signed(cap_r));
                frames_out = frames_out + 1;
            end
        end
    end

    // MCLK must be exactly aclk/10
    integer mclk_edges = 0;
    always @(posedge mclk) mclk_edges = mclk_edges + 1;

    // ------------------------------------------------------------------
    // Test sequence
    // ------------------------------------------------------------------
    reg [31:0] r;
    integer i, t_start;

    task wait_frames(input integer n);
        integer target;
        begin
            target = fidx + n;
            while (fidx < target) @(posedge aclk);
        end
    endtask

    task wait_bank(input integer b);
        begin
            axi_read(16'h000C, r);
            while (r[0] !== b[0]) axi_read(16'h000C, r);
        end
    endtask

    // drive the record button for n aclk cycles
    task btn(input v, input integer n);
        begin
            btn_rec <= v;
            repeat (n) @(posedge aclk);
        end
    endtask

    // press / release with contact bounce; every bounce is < 2^DEB_BITS cycles
    task btn_bounce(input v);
        begin
            btn(v, 3);   btn(!v, 2);  btn(v, 4);  btn(!v, 3);
            btn(v, 2);   btn(!v, 5);  btn(v, 3);
            btn(v, 4 * (1 << DEB_BITS));            // stable
        end
    endtask

    // same for the bypass button
    task bbtn(input v, input integer n);
        begin
            btn_byp <= v;
            repeat (n) @(posedge aclk);
        end
    endtask

    task bbtn_bounce(input v);
        begin
            bbtn(v, 3);  bbtn(!v, 2);  bbtn(v, 4);  bbtn(!v, 3);
            bbtn(v, 4 * (1 << DEB_BITS));
        end
    endtask

    // led_cab must be the inverse of CTRL[0]; CTRL[1] (mute) must stay set
    task check_cab(input exp_led, input [255:0] what);
        begin
            axi_read(17'h0008, r);
            if (led_cab !== exp_led || r[0] !== !exp_led || r[1] !== 1'b1)
                $display("FAIL: %0s (led_cab=%b ctrl=%02x, expected led %b)",
                         what, led_cab, r[7:0], exp_led);
        end
    endtask

    integer fcap, wp0, wp1, nrec;
    reg seen0, seen1;
    task check_led(input exp, input [255:0] what);
        begin
            axi_read(17'h0008, r);
            // over two blink periods the LED must show both levels while
            // recording and stay dark otherwise
            seen0 = 0; seen1 = 0;
            repeat (4 << BLINK_BIT) begin
                @(posedge aclk);
                if (led_rec === 1'b0) seen0 = 1;
                if (led_rec === 1'b1) seen1 = 1;
            end
            if (r[6] !== exp || seen1 !== exp || seen0 !== 1'b1 && exp) begin
                $display("FAIL: %0s (led seen0=%b seen1=%b ctrl6=%b, expected %b)",
                         what, seen0, seen1, r[6], exp);
            end
            if (!exp && seen1) $display("FAIL: %0s (led_rec lit while not recording)", what);
        end
    endtask

    initial begin
        if (!$value$plusargs("NIN=%d", nin)) nin = 1000;
        if (!$value$plusargs("SHA=%d", sha)) sha = 16;
        if (!$value$plusargs("SHB=%d", shb)) shb = 16;
        if (!$value$plusargs("F0=%d", f0)) f0 = 100;
        if (!$value$plusargs("F1=%d", f1)) f1 = 100;
        if (!$value$plusargs("F2=%d", f2)) f2 = 100;
        if (!$value$plusargs("F3=%d", f3)) f3 = 100;
        $readmemh("inl.hex", inl);
        $readmemh("inr.hex", inr);
        $readmemh("coefa.hex", coefa);
        $readmemh("coefb.hex", coefb);
        fout = $fopen("out.txt", "w");

        repeat (20) @(posedge aclk);
        aresetn = 1;
        repeat (2) @(posedge aclk);
        if (led_cab !== 1'b1) $display("FAIL: led_cab not on after reset");

        axi_read(16'h0000, r);
        $display("ID      = %08x", r);
        if (r !== 32'h43414256) begin $display("FAIL: bad ID"); $finish; end
        axi_read(16'h0004, r);
        $display("PARAMS  = %08x (taps=%0d)", r, r[15:0]);
        if (r[15:0] !== NTAPS) begin $display("FAIL: bad PARAMS"); $finish; end

        wait_frames(f0);

        // ---- IR A -> bank 1 ----
        $display("[%0d] loading IR A into bank 1", fidx);
        for (i = 0; i < NTAPS; i = i + 1)
            axi_write(16'h8000 + 4*i, {14'd0, coefa[i]});
        axi_write(16'h0014, sha);
        axi_write(16'h0008, 32'h10);
        wait_bank(1);
        $display("[%0d] bank 1 active", fidx);
        wait_frames(f1);

        // ---- IR B -> bank 0 (written while bank 1 is playing) ----
        $display("[%0d] loading IR B into bank 0", fidx);
        for (i = 0; i < NTAPS; i = i + 1)
            axi_write(16'h8000 + 4*i, {14'd0, coefb[i]});
        axi_write(16'h0010, shb);
        axi_write(16'h0008, 32'h00);
        wait_bank(0);
        $display("[%0d] bank 0 active", fidx);
        wait_frames(10);

        // ---- record: set REC via AXI, run past the ring size, stop ----
        $display("[%0d] REC on", fidx);
        axi_write(17'h0008, 32'h40);
        check_led(1'b1, "led_rec after REC set");
        nrec = 3 * CAPN + 7;
        wait_frames(nrec);
        axi_write(17'h0008, 32'h00);
        check_led(1'b0, "led_rec after REC clear");
        axi_read(17'h0028, r);
        wp0 = r;
        axi_read(17'h0028, r);
        $display("[%0d] REC off, REC_WPTR = %0d (ring %0d)", fidx, wp0, CAPN);
        if (r !== wp0 || wp0 < nrec - 2 || wp0 > nrec + 8)
            $display("FAIL: REC_WPTR %0d / %0d", wp0, r);
        // dump: wptr, then {x, y} for every ring frame
        fcap = $fopen("cap.txt", "w");
        $fdisplay(fcap, "%0d", wp0);
        for (i = 0; i < CAPN; i = i + 1) begin
            axi_read(17'h10000 + 8*i, r);   $fwrite(fcap, "%0d ", $signed(r));
            axi_read(17'h10000 + 8*i + 4, r); $fdisplay(fcap, "%0d", $signed(r));
        end
        $fclose(fcap);
        axi_read(17'h0028, r);
        if (r !== wp0) $display("FAIL: REC_WPTR moved while REC=0");

        // ---- record button: a short glitch must be ignored ----
        $display("[%0d] button test", fidx);
        btn(1, 5);  btn(0, 3 * (1 << DEB_BITS));
        check_led(1'b0, "led_rec after glitch");
        // bouncing press toggles REC on
        btn_bounce(1'b1);
        check_led(1'b1, "led_rec after press 1");
        wait_frames(20);
        axi_read(17'h0028, r);
        if (r < 15 || r > 25) $display("FAIL: REC_WPTR after button start = %0d", r);
        // bouncing release does not toggle
        btn_bounce(1'b0);
        check_led(1'b1, "led_rec after release 1");
        // second press toggles REC off
        btn_bounce(1'b1);
        check_led(1'b0, "led_rec after press 2");
        btn_bounce(1'b0);
        check_led(1'b0, "led_rec after release 2");
        axi_read(17'h0028, r);  wp1 = r;
        wait_frames(5);
        axi_read(17'h0028, r);
        if (r !== wp1) $display("FAIL: REC_WPTR moved while REC=0 (button)");
        // AXI set/clear still works after button use
        axi_write(17'h0008, 32'h40);
        check_led(1'b1, "led_rec after AXI set (2)");
        axi_write(17'h0008, 32'h00);
        check_led(1'b0, "led_rec after AXI clear (2)");

        wait_frames(f2);

        axi_read(16'h000C, r);
        $display("STATUS  = %08x (in_clip=%0d out_clip=%0d overrun=%0d)",
                 r, r[1], r[2], r[3]);
        axi_read(16'h0020, r);  $display("PEAK_IN = %06x", r);
        axi_read(16'h0024, r);  $display("PEAK_OUT= %06x", r);
        axi_read(16'h001C, r);  $display("SAMPLES = %0d", r);

        // ---- bypass ----
        $display("[%0d] bypass", fidx);
        axi_write(16'h0008, 32'h01);
        @(posedge aclk);
        if (led_cab !== 1'b0) $display("FAIL: led_cab on during AXI bypass");
        wait_frames(f3);

        // level LEDs must have been lit (PWM) while audio was playing
        $display("level LEDs: in on %0d cycles, out on %0d cycles", lvl_in_on, lvl_out_on);
        if (lvl_in_on == 0 || lvl_out_on == 0) $display("FAIL: level LED never lit");

        // ---- mute ----
        $display("[%0d] mute", fidx);
        axi_write(16'h0008, 32'h02);
        axi_write(16'h0018, 32'h1);         // clear flags
        axi_read(16'h000C, r);
        if (r[2] !== 1'b0) $display("FAIL: clear did not work");

        // ---- bypass button (mute hides the output, so the audio check is unaffected) ----
        $display("[%0d] bypass button test", fidx);
        check_cab(1'b1, "led_cab after mute write (bypass 0)");
        bbtn(1, 5);  bbtn(0, 3 * (1 << DEB_BITS));
        check_cab(1'b1, "led_cab after glitch");
        bbtn_bounce(1'b1);
        check_cab(1'b0, "led_cab after bypass press 1");
        bbtn_bounce(1'b0);
        check_cab(1'b0, "led_cab after bypass release 1");
        bbtn_bounce(1'b1);
        check_cab(1'b1, "led_cab after bypass press 2");
        bbtn_bounce(1'b0);
        check_cab(1'b1, "led_cab after bypass release 2");
        while (fidx < nin + 4) @(posedge aclk);

        axi_read(16'h000C, r);
        if (r[3]) $display("FAIL: engine overrun");
        else      $display("no engine overrun");
        $display("MCLK edges = %0d over %0d frames (%0d per frame)",
                 mclk_edges, fidx, mclk_edges / fidx);
        $display("frames captured = %0d", frames_out);
        $fclose(fout);
        $display("DONE");
        $finish;
    end

endmodule
