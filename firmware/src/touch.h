#pragma once
#include <stdint.h>

// GT911 capacitive touch (E1003 only). On other boards every call is a no-op.
namespace touch {
void begin();                       // after display::begin()
void poll();                        // call often; latches one debounced tap
bool pending();                     // a tap is waiting to be sent
bool take(int16_t *x, int16_t *y);  // pops the tap (panel pixels)
void sleepPrep();                   // hold RST low through deep sleep
}
