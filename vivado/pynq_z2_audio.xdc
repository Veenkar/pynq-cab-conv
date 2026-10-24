## PYNQ-Z2 audio codec (ADAU1761) pins, from the PYNQ v2.7 base overlay

## I2C to codec (PS I2C1 via EMIO -> /dev/i2c-1)
set_property -dict {PACKAGE_PIN U9 IOSTANDARD LVCMOS33 PULLUP true} [get_ports IIC_1_scl_io]
set_property -dict {PACKAGE_PIN T9 IOSTANDARD LVCMOS33 PULLUP true} [get_ports IIC_1_sda_io]

## Arduino header SCL / SDA (PS I2C0 via EMIO -> /dev/i2c-0), e.g. SSD1306 OLED
set_property -dict {PACKAGE_PIN P15 IOSTANDARD LVCMOS33 PULLUP true} [get_ports IIC_0_scl_io]
set_property -dict {PACKAGE_PIN P16 IOSTANDARD LVCMOS33 PULLUP true} [get_ports IIC_0_sda_io]

## I2S + MCLK
set_property -dict {PACKAGE_PIN U5  IOSTANDARD LVCMOS33} [get_ports mclk]
set_property -dict {PACKAGE_PIN R18 IOSTANDARD LVCMOS33} [get_ports bclk]
set_property -dict {PACKAGE_PIN T17 IOSTANDARD LVCMOS33} [get_ports lrclk]
set_property -dict {PACKAGE_PIN G18 IOSTANDARD LVCMOS33} [get_ports sdata_o]
set_property -dict {PACKAGE_PIN F17 IOSTANDARD LVCMOS33} [get_ports sdata_i]

## codec I2C address select (driven high -> 0x3B)
set_property -dict {PACKAGE_PIN M17 IOSTANDARD LVCMOS33} [get_ports {codec_addr[0]}]
set_property -dict {PACKAGE_PIN M18 IOSTANDARD LVCMOS33} [get_ports {codec_addr[1]}]

## buttons / LEDs: BTN0 + LED0 = record, BTN1 + LED1 = bypass (LED1 on = cab on)
set_property -dict {PACKAGE_PIN D19 IOSTANDARD LVCMOS33} [get_ports btn_rec]
set_property -dict {PACKAGE_PIN R14 IOSTANDARD LVCMOS33} [get_ports led_rec]
set_property -dict {PACKAGE_PIN D20 IOSTANDARD LVCMOS33} [get_ports btn_byp]
set_property -dict {PACKAGE_PIN P14 IOSTANDARD LVCMOS33} [get_ports led_cab]
## LED2 / LED3: input / output level (PWM brightness)
set_property -dict {PACKAGE_PIN N16 IOSTANDARD LVCMOS33} [get_ports led_lvl_in]
set_property -dict {PACKAGE_PIN M14 IOSTANDARD LVCMOS33} [get_ports led_lvl_out]

## I2S runs at 3.072 MHz BCLK generated synchronously from the 122.88 MHz
## fabric clock; launch/capture points are chosen in RTL with >100 ns of
## margin, so the pins only need to be kept out of timing analysis.
set_false_path -to   [get_ports {mclk bclk lrclk sdata_o codec_addr[*] led_rec led_cab led_lvl_in led_lvl_out}]
set_false_path -from [get_ports {sdata_i btn_rec btn_byp}]

## pack I2S output flops into the IOBs for clean edges
set_property IOB TRUE [get_ports {bclk lrclk sdata_o}]
