#pragma once
// reTerminal E1001 pin map, from Seeed's Arduino cookbook and
// User_Setups/Setup520_Seeed_reTerminal_E1001.h. None of these are guessable.

// ePaper (UC8179) -- driven by Seeed_GFX, listed here for reference only.
#define EPD_SCK_PIN   7
#define EPD_MOSI_PIN  9
#define EPD_CS_PIN    10
#define EPD_DC_PIN    11
#define EPD_RST_PIN   12
#define EPD_BUSY_PIN  13

#define EPD_W 800
#define EPD_H 480
// 2 bits per pixel, 4 levels, 200 bytes per row.
#define EPD_ROW_BYTES (EPD_W / 4)
#define EPD_BUF_LEN   (EPD_ROW_BYTES * EPD_H)   // 96000

// Three user buttons, all active low. KEY0 is the one Seeed labels "refresh",
// so it is the one that means "update now" here too.
#define KEY0_PIN 3
#define KEY1_PIN 4
#define KEY2_PIN 5

#define LED_PIN     6     // inverted: LOW = lit
#define BUZZER_PIN  45

#define I2C_SDA_PIN 19
#define I2C_SCL_PIN 20
#define SHT4X_ADDR  0x44

#define BATT_ADC_PIN    1
#define BATT_ENABLE_PIN 21
