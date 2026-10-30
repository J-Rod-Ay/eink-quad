#pragma once
#include <Arduino.h>
#include "board.h"

// The panel as a 1-bit framebuffer with two ways to show it:
//   full()    -- the classic flashing refresh, ~4s, clears all ghosting
//   partial() -- a windowed fast refresh, no flash, the thing that makes
//                animation possible at all on e-paper
//
// Bit layout everywhere: EPD_W x EPD_H (800x480 E1001, 1872x1404 E1003), MSB = leftmost pixel, set bit = white,
// W/8 bytes per row. That is PIL mode "1" packing, so server frames drop in.
namespace display {

static const int W = EPD_W;
static const int H = EPD_H;
static const int STRIDE = W / 8;
static const size_t BYTES = (size_t)STRIDE * H;   // 48000 / 328536

void begin();

// What is on the glass right now, as far as we know. Write into it, then call
// partial()/full() to make the glass match.
uint8_t *buffer();

// Refresh the window [x0,x1) x [y0,y1). x is rounded out to multiples of 8.
// Pushes both the previous contents and the new ones, so it is correct even
// straight after a controller reset (which loses the controller's own copy).
void partial(int x0, int y0, int x1, int y1);

// Full flashing refresh of the whole buffer.
void full();

// Full grey refresh. E1001: 4 levels, 2bpp packed, 4 px per byte, MSB first,
// 0=black..3=white. E1003: 16 levels, 4bpp, 2 px per byte, even x in the high
// nibble, 0=black..15=white. The 1-bit buffer() should already hold the
// thresholded version, which is what later partial refreshes diff against.
static const size_t GRAY_BYTES = (size_t)EPD_BUF_LEN;   // 96000 / 1314144
void fullGray(const uint8_t *g);

// Power the controller down. The image stays; partial() wakes it again.
void sleep();

// Text screens drawn on the device (wifi setup, errors). Full refresh.
struct MsgLine {
  const char *text;
  bool big;
};
void drawMessage(const char *title, const MsgLine *lines, int n,
                 const char *footer = nullptr);
}  // namespace display
