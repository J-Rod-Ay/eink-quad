#pragma once
#include <Arduino.h>

namespace sensors {
// SHT4x over I2C. Returns false and leaves the outputs alone if the sensor
// does not answer or the CRC fails -- a wrong indoor temperature on the screen
// is worse than no indoor temperature.
bool readSHT4x(float *temp_c, float *rh);

// Battery volts, via the divider on GPIO1 behind the enable on GPIO21.
float readVBat();

// Rough state of charge. A LiPo's curve is flat through the middle, so this is
// a four-bar gauge pretending to be a percentage, not a fuel computer.
int batteryPercent(float v);
}  // namespace sensors
