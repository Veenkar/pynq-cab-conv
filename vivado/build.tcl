# build.tcl - build the cab_conv overlay for PYNQ-Z2
#
#   cd pynq-cab-conv
#   vivado -mode batch -source vivado/build.tcl
#
# Output: build/cab_conv.bit + build/cab_conv.hwh (copy both to the board),
#         build/utilization.rpt, build/timing.rpt
# Optional: -tclargs <jobs>   (default 4)

set jobs 4
if {[llength $argv] > 0} { set jobs [lindex $argv 0] }

set here [file dirname [file normalize [info script]]]
set root [file dirname $here]
set out  [file join $root build]
set proj cab_conv
set part xc7z020clg400-1

file mkdir $out
create_project $proj [file join $out vivado] -part $part -force
set_property target_language Verilog [current_project]

add_files -norecurse [glob [file join $root rtl *.v]]
add_files -fileset constrs_1 -norecurse [file join $here pynq_z2_audio.xdc]
update_compile_order -fileset sources_1

source [file join $here ps7_pynq_z2.tcl]

# newest installed version of a Xilinx IP
proc ipvlnv {name} {
    # match on VLNV: the VENDOR property is "Xilinx, Inc.", not xilinx.com
    set defs [get_ipdefs -all xilinx.com:ip:${name}:*]
    if {[llength $defs] == 0} { error "IP $name not found" }
    return [lindex [lsort -dictionary $defs] end]
}

# ----------------------------------------------------------------------
# Block design
# ----------------------------------------------------------------------
create_bd_design system

# Zynq PS
set ps [create_bd_cell -type ip -vlnv [ipvlnv processing_system7] ps7]
apply_pynq_z2_ps7_config $ps
connect_bd_intf_net [create_bd_intf_port -mode Master \
    -vlnv xilinx.com:interface:ddrx_rtl:1.0 DDR] [get_bd_intf_pins ps7/DDR]
connect_bd_intf_net [create_bd_intf_port -mode Master \
    -vlnv xilinx.com:display_processing_system7:fixedio_rtl:1.0 FIXED_IO] \
    [get_bd_intf_pins ps7/FIXED_IO]
connect_bd_intf_net [create_bd_intf_port -mode Master \
    -vlnv xilinx.com:interface:iic_rtl:1.0 IIC_1] [get_bd_intf_pins ps7/IIC_1]
connect_bd_intf_net [create_bd_intf_port -mode Master \
    -vlnv xilinx.com:interface:iic_rtl:1.0 IIC_0] [get_bd_intf_pins ps7/IIC_0]

# 122.88 MHz audio/DSP clock from FCLK0 (100 MHz); MCLK = clk / 10
set cw [create_bd_cell -type ip -vlnv [ipvlnv clk_wiz] clk_wiz]
set_property -dict [list \
    CONFIG.PRIM_IN_FREQ {100.000} \
    CONFIG.CLKOUT1_REQUESTED_OUT_FREQ {122.880} \
    CONFIG.USE_LOCKED {true} \
    CONFIG.RESET_TYPE {ACTIVE_LOW} \
    CONFIG.RESET_PORT {resetn} \
] $cw
connect_bd_net [get_bd_pins ps7/FCLK_CLK0] [get_bd_pins clk_wiz/clk_in1]
connect_bd_net [get_bd_pins ps7/FCLK_RESET0_N] [get_bd_pins clk_wiz/resetn]

set rst [create_bd_cell -type ip -vlnv [ipvlnv proc_sys_reset] rst_audio]
connect_bd_net [get_bd_pins clk_wiz/clk_out1] [get_bd_pins rst_audio/slowest_sync_clk]
connect_bd_net [get_bd_pins ps7/FCLK_RESET0_N] [get_bd_pins rst_audio/ext_reset_in]
connect_bd_net [get_bd_pins clk_wiz/locked] [get_bd_pins rst_audio/dcm_locked]

# the convolver (RTL module reference)
create_bd_cell -type module -reference cab_conv_top cab_conv

# AXI: GP0 -> interconnect (AXI3 -> AXI4-Lite) -> cab_conv, all on 122.88 MHz
set ic [create_bd_cell -type ip -vlnv [ipvlnv axi_interconnect] axi_ic]
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {1}] $ic
connect_bd_intf_net [get_bd_intf_pins ps7/M_AXI_GP0] [get_bd_intf_pins axi_ic/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins axi_ic/M00_AXI] [get_bd_intf_pins cab_conv/s_axi]

set clk [get_bd_pins clk_wiz/clk_out1]
connect_bd_net $clk [get_bd_pins ps7/M_AXI_GP0_ACLK] [get_bd_pins axi_ic/ACLK] \
    [get_bd_pins axi_ic/S00_ACLK] [get_bd_pins axi_ic/M00_ACLK] \
    [get_bd_pins cab_conv/aclk]
connect_bd_net [get_bd_pins rst_audio/interconnect_aresetn] [get_bd_pins axi_ic/ARESETN]
connect_bd_net [get_bd_pins rst_audio/peripheral_aresetn] \
    [get_bd_pins axi_ic/S00_ARESETN] [get_bd_pins axi_ic/M00_ARESETN] \
    [get_bd_pins cab_conv/aresetn]

# codec pins
foreach p {mclk bclk lrclk sdata_o} {
    create_bd_port -dir O $p
    connect_bd_net [get_bd_ports $p] [get_bd_pins cab_conv/$p]
}
create_bd_port -dir I sdata_i
connect_bd_net [get_bd_ports sdata_i] [get_bd_pins cab_conv/sdata_i]
create_bd_port -dir O -from 1 -to 0 codec_addr
connect_bd_net [get_bd_ports codec_addr] [get_bd_pins cab_conv/codec_addr]
create_bd_port -dir I btn_rec
connect_bd_net [get_bd_ports btn_rec] [get_bd_pins cab_conv/btn_rec]
create_bd_port -dir O led_rec
connect_bd_net [get_bd_ports led_rec] [get_bd_pins cab_conv/led_rec]
create_bd_port -dir I btn_byp
connect_bd_net [get_bd_ports btn_byp] [get_bd_pins cab_conv/btn_byp]
create_bd_port -dir O led_cab
connect_bd_net [get_bd_ports led_cab] [get_bd_pins cab_conv/led_cab]

# address map: 128 KiB at 0x43C00000
assign_bd_address
foreach seg [get_bd_addr_segs -of_objects [get_bd_addr_spaces ps7/Data]] {
    set_property range 128K $seg
    set_property offset 0x43C00000 $seg
}

validate_bd_design
save_bd_design

set bd [get_files system.bd]
make_wrapper -files $bd -top
add_files -norecurse [glob [file join $out vivado $proj.gen sources_1 bd system hdl system_wrapper.v] \
                           [file join $out vivado $proj.srcs sources_1 bd system hdl system_wrapper.v]]
set_property top system_wrapper [current_fileset]
update_compile_order -fileset sources_1

# ----------------------------------------------------------------------
# Build
# ----------------------------------------------------------------------
launch_runs impl_1 -to_step write_bitstream -jobs $jobs
wait_on_run impl_1

if {[get_property PROGRESS [get_runs impl_1]] != "100%"} {
    error "implementation failed, see [file join $out vivado $proj.runs impl_1 runme.log]"
}

open_run impl_1
report_utilization    -file [file join $out utilization.rpt]
report_timing_summary -file [file join $out timing.rpt]
set wns [get_property STATS.WNS [get_runs impl_1]]

file copy -force [file join $out vivado $proj.runs impl_1 system_wrapper.bit] \
                 [file join $out cab_conv.bit]
set hwh [glob -nocomplain \
    [file join $out vivado $proj.gen sources_1 bd system hw_handoff system.hwh] \
    [file join $out vivado $proj.srcs sources_1 bd system hw_handoff system.hwh]]
file copy -force [lindex $hwh 0] [file join $out cab_conv.hwh]

puts "=================================================================="
puts " build/cab_conv.bit + build/cab_conv.hwh ready"
puts " WNS = $wns ns  [expr {$wns < 0 ? {-- TIMING NOT MET} : {(timing met)}}]"
puts "=================================================================="
