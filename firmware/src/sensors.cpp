#include "sensors.h"

#include <Wire.h>

#include "board.h"

namespace sensors {

static uint8_t crc8(const uint8_t *d, int n) {
  uint8_t crc = 0xFF;
  for (int i = 0; i < n; i++) {
    crc ^= d[i];
    for (int b = 0; b < 8; b++) {
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

bool readSHT4x(float *temp_c, float *rh) {
  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  Wire.beginTransmission(SHT4X_ADDR);
  Wire.write(0xFD);                       // high-precision single shot
  if (Wire.endTransmission() != 0) return false;
  delay(12);                              // datasheet says 8.3ms max

  uint8_t d[6];
  if (Wire.requestFrom(SHT4X_ADDR, 6) != 6) return false;
  for (int i = 0; i < 6; i++) d[i] = Wire.read();
  if (crc8(d, 2) != d[2] || crc8(d + 3, 2) != d[5]) return false;

  uint16_t rawT = ((uint16_t)d[0] << 8) | d[1];
  uint16_t rawH = ((uint16_t)d[3] << 8) | d[4];
  *temp_c = -45.0f + 175.0f * rawT / 65535.0f;
  *rh = constrain(-6.0f + 125.0f * rawH / 65535.0f, 0.0f, 100.0f);
  return true;
}

float readVBat() {
  pinMode(BATT_ENABLE_PIN, OUTPUT);
  digitalWrite(BATT_ENABLE_PIN, HIGH);
  analogReadResolution(12);
  analogSetPinAttenuation(BATT_ADC_PIN, ADC_11db);
  // Seeed's note: the divider needs a moment before the first conversion is
  // trustworthy. Cheap insurance on a reading we log every wake.
  delay(10);

  uint32_t sum = 0;
  for (int i = 0; i < 8; i++) sum += analogReadMilliVolts(BATT_ADC_PIN);
  digitalWrite(BATT_ENABLE_PIN, LOW);

  return (sum / 8.0f) / 1000.0f * 2.0f;   // 2:1 divider
}

// SY6974B, read-only. Only REG0B (part id) and REG08 (status) are read, one
// byte at a time: REG09 clears its fault bits when read, and a write to REG0B
// bit 7 would reset the charger's settings, so neither is ever touched.
static bool chargerReg(uint8_t reg, uint8_t *v) {
  Wire1.beginTransmission(CHARGER_ADDR);
  Wire1.write(reg);
  if (Wire1.endTransmission(false) != 0) return false;
  if (Wire1.requestFrom((uint8_t)CHARGER_ADDR, (uint8_t)1) != 1) return false;
  *v = Wire1.read();
  return true;
}

bool readCharger(bool *usb, uint8_t *chg) {
  static int8_t present = -1;   // unknown until the first try after each wake
  static bool begun = false;
  if (present == 0) return false;
  if (!begun) {
    Wire1.begin(CHG_SDA_PIN, CHG_SCL_PIN, 100000);
    begun = true;
  }
  if (present < 0) {
    uint8_t id;
    present = (chargerReg(0x0B, &id) && (id & 0x78) == 0x40) ? 1 : 0;   // PN = 1000
    if (!present) return false;
  }
  uint8_t st;
  if (!chargerReg(0x08, &st)) return false;
  uint8_t bus = st >> 5;        // 000 no input, 111 OTG, anything else = a USB source
  bool powerGood = st & 0x04;
  *usb = powerGood || (bus != 0 && bus != 7);
  *chg = (st >> 3) & 0x03;
  return true;
}

int batteryPercent(float v) {
  if (v >= 4.15f) return 100;
  if (v <= 3.30f) return 0;
  return (int)((v - 3.30f) / (4.15f - 3.30f) * 100.0f + 0.5f);
}

}  // namespace sensors
