#include "touch.h"
#include "board.h"

#ifdef QUAD_E1003
#include <Arduino.h>
#include <Wire.h>
#include <driver/gpio.h>

namespace touch {

static uint8_t addr = 0;
static uint16_t maxX = EPD_W, maxY = EPD_H;
static bool wasDown = false;
static uint32_t lastTapMs = 0;
static volatile bool have = false;
static int16_t tapX = 0, tapY = 0;

static bool rd(uint16_t reg, uint8_t *buf, uint8_t n) {
  Wire.beginTransmission(addr);
  Wire.write(reg >> 8);
  Wire.write(reg & 0xFF);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom((int)addr, (int)n) != n) return false;
  for (uint8_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}

static bool wr8(uint16_t reg, uint8_t v) {
  Wire.beginTransmission(addr);
  Wire.write(reg >> 8);
  Wire.write(reg & 0xFF);
  Wire.write(v);
  return Wire.endTransmission() == 0;
}

static bool probe(uint8_t a) {
  Wire.beginTransmission(a);
  return Wire.endTransmission() == 0;
}

void begin() {
  gpio_hold_dis((gpio_num_t)TOUCH_RST_PIN);
  gpio_deep_sleep_hold_dis();
  // GT911 reset: INT low during RST rising edge selects 0x5D.
  pinMode(TOUCH_RST_PIN, OUTPUT);
  pinMode(TOUCH_INT_PIN, OUTPUT);
  digitalWrite(TOUCH_RST_PIN, LOW);
  digitalWrite(TOUCH_INT_PIN, LOW);
  delay(11);
  digitalWrite(TOUCH_RST_PIN, HIGH);
  delay(6);
  pinMode(TOUCH_INT_PIN, INPUT);
  delay(55);

  Wire.begin(TOUCH_SDA_PIN, TOUCH_SCL_PIN);   // same bus as the SHT4x
  if (probe(TOUCH_ADDR)) addr = TOUCH_ADDR;
  else if (probe(0x14)) addr = 0x14;
  if (!addr) { Serial.println("[touch] GT911 not found"); return; }

  uint8_t id[4] = {0}, cfg[4] = {0};
  rd(0x8140, id, 4);
  if (rd(0x8048, cfg, 4)) {   // config: X max (LE), Y max (LE)
    uint16_t mx = cfg[0] | (cfg[1] << 8), my = cfg[2] | (cfg[3] << 8);
    if (mx > 0 && my > 0 && mx < 10000 && my < 10000) { maxX = mx; maxY = my; }
  }
  wr8(0x814E, 0);
  Serial.printf("[touch] GT911 '%c%c%c%c' @0x%02X, reports %ux%u -> panel %ux%u\n",
                id[0], id[1], id[2], id[3], addr, maxX, maxY, EPD_W, EPD_H);
}

void poll() {
  if (!addr) return;
  uint8_t st;
  if (!rd(0x814E, &st, 1)) return;
  if (!(st & 0x80)) return;               // buffer not ready
  uint8_t n = st & 0x0F;
  bool down = n > 0;
  if (down && !wasDown && !have && millis() - lastTapMs > 250) {
    uint8_t p[8];
    if (rd(0x8150, p, 4)) {               // first point: X lo/hi, Y lo/hi
      uint16_t rx = p[0] | (p[1] << 8), ry = p[2] | (p[3] << 8);
      long x = (long)rx * EPD_W / maxX, y = (long)ry * EPD_H / maxY;
      tapX = constrain(x, 0, EPD_W - 1);
      tapY = constrain(y, 0, EPD_H - 1);
      have = true;
      lastTapMs = millis();
      Serial.printf("[touch] raw %u,%u -> panel %d,%d\n", rx, ry, tapX, tapY);
    }
  }
  wasDown = down;
  wr8(0x814E, 0);
}

bool pending() { return have; }

bool take(int16_t *x, int16_t *y) {
  if (!have) return false;
  *x = tapX; *y = tapY;
  have = false;
  return true;
}

void sleepPrep() {
  pinMode(TOUCH_RST_PIN, OUTPUT);
  digitalWrite(TOUCH_RST_PIN, LOW);
  gpio_hold_en((gpio_num_t)TOUCH_RST_PIN);
  gpio_deep_sleep_hold_en();
}

}  // namespace touch

#else
namespace touch {
void begin() {}
void poll() {}
bool pending() { return false; }
bool take(int16_t *, int16_t *) { return false; }
void sleepPrep() {}
}
#endif
