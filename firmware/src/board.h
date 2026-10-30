#pragma once
// Pin maps for the two supported reTerminals. Default is the E1001; build with
// -DQUAD_E1003 (env quad-e1003) for the 10.3" E1003. None of these are
// guessable -- sources: Seeed User_Setups/Setup520 / Setup522 and the Seeed
// wiki "Arduino Cookbook: Onboard Peripherals (reTerminal E Series)".

#ifdef QUAD_E1003
// ---------------------------------------------------------------- E1003
#define QUAD_MODEL "e1003"
// ePaper ED103TC2 via IT8951-class TCON (SPI). Driven by Seeed_GFX (setup 522).
#define EPD_SCK_PIN    7
#define EPD_MISO_PIN   8
#define EPD_MOSI_PIN   9
#define EPD_CS_PIN     10
#define EPD_DC_PIN     -1    // no D/C line on the TCON bus
#define EPD_BUSY_PIN   13    // TCON HRDY
#define EPD_RST_PIN    12
#define EPD_ENABLE_PIN 11    // EPD drive enable
#define ITE_ENABLE_PIN 21    // TCON VCC enable -- owned by Seeed_GFX, do not touch

#define EPD_W 1872
#define EPD_H 1404
#define EPD_GRAY_LEVELS 16
#define EPD_GRAY_BPP    4    // 2 px per byte, even x in the high nibble

#define LED_PIN     16    // inverted: LOW = lit
#define BATT_ADC_PIN    1
#define BATT_ENABLE_PIN 40   // NOT 21 on the E1003 (21 is the TCON's power)
#define SD_ENABLE_PIN   39

// GT911 capacitive touch. v1 firmware does not use it (see E1003.md).
#define TOUCH_SDA_PIN 19
#define TOUCH_SCL_PIN 20
#define TOUCH_INT_PIN 2
#define TOUCH_RST_PIN 48
#define TOUCH_ADDR    0x5D   // or 0x14, depending on INT level at reset

#else
// ---------------------------------------------------------------- E1001
#define QUAD_MODEL "e1001"
// ePaper (UC8179) -- driven by Seeed_GFX, listed here for reference only.
#define EPD_SCK_PIN   7
#define EPD_MOSI_PIN  9
#define EPD_CS_PIN    10
#define EPD_DC_PIN    11
#define EPD_RST_PIN   12
#define EPD_BUSY_PIN  13

#define EPD_W 800
#define EPD_H 480
#define EPD_GRAY_LEVELS 4
#define EPD_GRAY_BPP    2    // 4 px per byte, MSB first

#define LED_PIN     6     // inverted: LOW = lit
#define BATT_ADC_PIN    1
#define BATT_ENABLE_PIN 21
#endif

// Grey frame bytes on the wire.
#define EPD_ROW_BYTES (EPD_W * EPD_GRAY_BPP / 8)
#define EPD_BUF_LEN   (EPD_ROW_BYTES * EPD_H)   // E1001 96000, E1003 1314144

// Three user buttons, all active low. KEY0 is the one Seeed labels "refresh",
// so it is the one that means "update now" here too. Same GPIOs on both.
#define KEY0_PIN 3
#define KEY1_PIN 4
#define KEY2_PIN 5

#define BUZZER_PIN  45

#define I2C_SDA_PIN 19
#define I2C_SCL_PIN 20
#define SHT4X_ADDR  0x44
