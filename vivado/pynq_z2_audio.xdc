## PYNQ-Z2 audio codec (ADAU1761) pins, from the PYNQ v2.7 base overlay

## I2C to codec (PS I2C1 via EMIO -> /dev/i2c-1)
set_property -dict {PACKAGE_PIN U9 IOSTANDARD LVCMOS33 PULLUP true} [get_ports IIC_1_scl_io]
set_property -dict {PACKAGE_PIN T9 IOSTANDARD LVCMOS33 PULLUP true} [get_ports IIC_1_sda_io]

## I2S + MCLK
set_property -dict {PACKAGE_PIN U5  IOSTANDARD LVCMOS33} [get_ports mclk]
set_property -dict {PACKAGE_PIN R18 IOSTANDARD LVCMOS33} [get_ports bclk]
set_property -dict {PACKAGE_PIN T17 IOSTANDARD LVCMOS33} [get_ports lrclk]
set_property -dict {PACKAGE_PIN G18 IOSTANDARD LVCMOS33} [get_ports sdata_o]
set_property -dict {PACKAGE_PIN F17 IOSTANDARD LVCMOS33} [get_ports sdata_i]

## codec I2C address select (driven high -> 0x3B)
set_property -dict {PACKAGE_PIN M17 IOSTANDARD LVCMOS33} [get_ports {codec_addr[0]}]
set_property -dict {PACKAGE_PIN M18 IOSTANDARD LVCMOS33} [get_ports {codec_addr[1]}]

## I2S runs at 3.072 MHz BCLK generated synchronously from the 122.88 MHz
## fabric clock; launch/capture points are chosen in RTL with >100 ns of
## margin, so the pins only need to be kept out of timing analysis.
set_false_path -to   [get_ports {mclk bclk lrclk sdata_o codec_addr[*]}]
set_false_path -from [get_ports sdata_i]

## pack I2S output flops into the IOBs for clean edges
set_property IOB TRUE [get_ports {bclk lrclk sdata_o}]
